"""Conservative statement-level intraprocedural data-flow graphs."""

import ast
import hashlib
import sys
from collections import defaultdict, deque
from pathlib import Path

from .ast_extractor import CodeGraphError
from .slicer import InvalidBudgetError, serialized_tokens


class UnsupportedFlowError(CodeGraphError):
    pass


class FlowQueryError(CodeGraphError):
    pass


class FlowBudgetExceeded(CodeGraphError):
    pass


_UNSUPPORTED = (
    ast.Try,
    ast.With,
    ast.AsyncWith,
    ast.AsyncFor,
    ast.AsyncFunctionDef,
    ast.FunctionDef,
    ast.ClassDef,
    ast.Import,
    ast.ImportFrom,
    ast.Delete,
    ast.Global,
    ast.Nonlocal,
)
_SUPPORTED_STMTS = (
    ast.Assign,
    ast.AnnAssign,
    ast.AugAssign,
    ast.Expr,
    ast.If,
    ast.For,
    ast.While,
    ast.Break,
    ast.Continue,
    ast.Return,
    ast.Raise,
    ast.Assert,
    ast.Pass,
)


def _names(node):
    return {
        n.id
        for n in ast.walk(node)
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)
    }


def _paths(node):
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load):
            out.add(ast.unparse(n))
            out.add(_wildcard_path(n))
        elif isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Load):
            out.add(ast.unparse(n))
            out.add(_wildcard_path(n))
    return out


def _target_names(node):
    return {
        n.id
        for n in ast.walk(node)
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del))
    }


def _access_targets(node):
    targets = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Store):
            targets.add(ast.unparse(n))
            targets.add(_wildcard_path(n))
        elif isinstance(n, ast.Subscript) and isinstance(n.ctx, ast.Store):
            targets.add(ast.unparse(n))
            targets.add(_wildcard_path(n))
    return targets


def _wildcard_path(node):
    return f"{ast.unparse(node.value)}[*]"


def _mutation_label(node):
    path = ast.unparse(node)
    if isinstance(node, ast.Subscript) and not isinstance(node.slice, ast.Constant):
        return path + " [dynamic-index]"
    return path


def _expression_events(node):
    """Name reads and named-expression writes in conservative evaluation order."""
    events = []

    def visit(expr):
        if expr is None:
            return
        if isinstance(expr, ast.Name):
            if isinstance(expr.ctx, ast.Load):
                events.append(("load", expr.id))
            return
        if isinstance(expr, ast.NamedExpr):
            visit(expr.value)
            events.append(("store", expr.target.id))
            return
        if isinstance(expr, ast.Dict):
            for key, value in zip(expr.keys, expr.values):
                visit(key)
                visit(value)
            return
        for child in ast.iter_child_nodes(expr):
            visit(child)

    visit(node)
    return events


def _inline_name_flow(node):
    defined = set()
    reads_after = set()
    reads_before = set()
    for kind, name in _expression_events(node):
        if kind == "store":
            defined.add(name)
        elif name in defined:
            reads_after.add(name)
        else:
            reads_before.add(name)
    return reads_after, reads_before


def _target_access_roots(node):
    return _names(node)


def _dynamic_access_targets(node):
    weak = set()
    for target in ast.walk(node):
        if not isinstance(target, (ast.Attribute, ast.Subscript)) or not isinstance(
            target.ctx, ast.Store
        ):
            continue
        current = target
        dynamic = False
        while isinstance(current, (ast.Attribute, ast.Subscript)):
            if isinstance(current, ast.Subscript) and not isinstance(
                current.slice, ast.Constant
            ):
                dynamic = True
            current = current.value
        if dynamic:
            weak.update((ast.unparse(target), _wildcard_path(target)))
    return weak


def _killed_by_rebinding(access_path, definitions, weak_definitions):
    return any(
        definition not in weak_definitions
        and not definition.endswith("[*]")
        and (
            access_path == definition
            or access_path.startswith(definition + ".")
            or access_path.startswith(definition + "[")
        )
        for definition in definitions
    )


def _span(node):
    return node.lineno, getattr(node, "end_lineno", node.lineno)


def _check(node, path):
    if isinstance(node, _UNSUPPORTED):
        raise UnsupportedFlowError(
            f"unsupported {type(node).__name__} in {path}:{node.lineno}"
        )
    if isinstance(
        node,
        (
            ast.Yield,
            ast.YieldFrom,
            ast.Await,
            ast.Lambda,
            ast.ListComp,
            ast.SetComp,
            ast.DictComp,
            ast.GeneratorExp,
        ),
    ):
        raise UnsupportedFlowError(
            f"unsupported {type(node).__name__} in {path}:{node.lineno}"
        )
    for child in ast.iter_child_nodes(node):
        _check(child, path)


def analyze_flow(source: str, qualname: str, *, path: str) -> dict:
    """Build a deterministic CFG/data-dependence graph for one observed function."""
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise FlowQueryError(f"invalid source {path}:{exc.lineno}: {exc.msg}") from exc
    candidates = []

    def visit(body, prefix):
        for item in body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = ".".join((*prefix, item.name))
                if q == qualname:
                    candidates.append(item)
                visit(item.body, (*prefix, item.name))
            elif isinstance(item, ast.ClassDef):
                visit(item.body, (*prefix, item.name))

    visit(tree.body, [])
    if len(candidates) == 1 and isinstance(candidates[0], ast.AsyncFunctionDef):
        raise UnsupportedFlowError(
            f"unsupported AsyncFunctionDef in {path}:{candidates[0].lineno}"
        )
    if len(candidates) != 1 or not isinstance(candidates[0], ast.FunctionDef):
        raise FlowQueryError(
            f"expected exactly one synchronous function {qualname!r} in {path}"
        )
    fn = candidates[0]
    for stmt in fn.body:
        _check(stmt, path)
    for parent in ast.walk(fn):
        if isinstance(parent, (ast.BoolOp, ast.IfExp)):
            for child in ast.walk(parent):
                if isinstance(child, ast.NamedExpr):
                    raise UnsupportedFlowError(
                        f"unsupported conditional named expression in {path}:{child.lineno}"
                    )
        if isinstance(parent, ast.Compare):
            for child in ast.walk(
                ast.Tuple(elts=parent.comparators[1:], ctx=ast.Load())
            ):
                if isinstance(child, ast.NamedExpr):
                    raise UnsupportedFlowError(
                        f"unsupported conditional named expression in {path}:{child.lineno}"
                    )
        if isinstance(parent, ast.Assert) and parent.msg is not None:
            for child in ast.walk(parent.msg):
                if isinstance(child, ast.NamedExpr):
                    raise UnsupportedFlowError(
                        f"unsupported conditional named expression in {path}:{child.lineno}"
                    )
    for stmt in ast.walk(fn):
        if isinstance(stmt, ast.Assign):
            target_names = set().union(*(_target_names(t) for t in stmt.targets))
            walrus_names = _target_names(stmt.value)
            for target in stmt.targets:
                if isinstance(target, (ast.Attribute, ast.Subscript)):
                    if _target_access_roots(target) & (target_names | walrus_names):
                        raise UnsupportedFlowError(
                            f"unsupported overlapping assignment target in {path}:{stmt.lineno}"
                        )
        elif isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
            if isinstance(stmt.target, (ast.Attribute, ast.Subscript)) and (
                _target_access_roots(stmt.target) & _target_names(stmt.value)
            ):
                raise UnsupportedFlowError(
                    f"unsupported overlapping assignment target in {path}:{stmt.lineno}"
                )
        elif isinstance(stmt, ast.AugAssign) and isinstance(
            stmt.target, (ast.Attribute, ast.Subscript)
        ):
            if _target_access_roots(stmt.target) & _target_names(stmt.value):
                raise UnsupportedFlowError(
                    f"unsupported overlapping augmented target in {path}:{stmt.lineno}"
                )
    nodes, edges, diagnostics = [], [], []

    def add(kind, node=None, text="", defs=(), uses=(), synthetic=False):
        line, end = _span(node) if node is not None else (fn.lineno, fn.lineno)
        ident = f"n{len(nodes):04d}"
        nodes.append(
            {
                "id": ident,
                "kind": kind,
                "line": line,
                "end_line": end,
                "text": text
                or (
                    ast.get_source_segment(source, node) or ""
                    if node is not None
                    else ""
                ),
                "defs": sorted(set(defs)),
                "uses": sorted(set(uses)),
                "mutation_targets": [],
                "reachable": True,
                "synthetic": synthetic,
            }
        )
        return ident

    def edge(a, b, k, label=""):
        if a and b:
            edges.append({"source": a, "target": b, "kind": k, "label": label})

    entry = add("entry", synthetic=True)
    exit_id = add("exit", synthetic=True)
    param_tail = entry
    arguments = fn.args.posonlyargs + fn.args.args + fn.args.kwonlyargs
    arguments += [arg for arg in (fn.args.vararg, fn.args.kwarg) if arg is not None]
    for arg in arguments:
        nid = add("parameter", arg, arg.arg, (arg.arg,))
        edge(entry, nid, "AST", "parameter")
        edge(param_tail, nid, "CFG", "parameter")
        param_tail = nid
    # Each source statement gets a node; predicates use only their header span.
    stmt_id = {}
    inline_after = {}
    inline_before = {}
    weak_definitions = {}

    def remember_inline(nid, expression, before=()):
        after, earlier = _inline_name_flow(expression)
        inline_after[nid] = after
        inline_before[nid] = earlier | set(before)

    def collect(stmts):
        for st in stmts:
            if isinstance(st, (ast.If, ast.For, ast.While)):
                expr = st.test if isinstance(st, (ast.If, ast.While)) else None
                nid = add(
                    "predicate",
                    expr or st,
                    (ast.get_source_segment(source, expr) or "for iteration")
                    if expr is not None
                    else "for iteration",
                    _target_names(expr) | _access_targets(expr) if expr else (),
                    _names(expr) | _paths(expr) if expr else (),
                )
                if expr is not None:
                    remember_inline(nid, expr)
                if isinstance(st, ast.For):
                    nodes[int(nid[1:])]["line"] = st.lineno
                    nodes[int(nid[1:])]["end_line"] = getattr(
                        st.iter, "end_lineno", st.lineno
                    )
                    nodes[int(nid[1:])]["text"] = (
                        f"for {ast.unparse(st.target)} in {ast.get_source_segment(source, st.iter)}"
                    )
                stmt_id[st] = nid
                collect(st.body)
                collect(st.orelse)
            else:
                d = _target_names(st) | _access_targets(st)
                if isinstance(st, ast.AnnAssign) and st.value is None:
                    d = set()
                u = _names(st) | _paths(st)
                if isinstance(st, ast.AnnAssign):
                    u = (
                        _names(st.value) | _paths(st.value)
                        if st.value is not None
                        else set()
                    )
                if isinstance(st, ast.AugAssign):
                    u |= _target_names(st.target) | _access_targets(st.target)
                nid = add(type(st).__name__.lower(), st, defs=d, uses=u)
                stmt_id[st] = nid
                weak_definitions[nid] = _dynamic_access_targets(st)
                expression = (
                    st.value
                    if isinstance(st, (ast.Assign, ast.AnnAssign, ast.AugAssign))
                    else st.value
                    if isinstance(st, (ast.Expr, ast.Return))
                    else st.exc
                    if isinstance(st, ast.Raise)
                    else st.test
                    if isinstance(st, ast.Assert)
                    else None
                )
                prior = (
                    _target_names(st.target) | _access_targets(st.target)
                    if isinstance(st, ast.AugAssign)
                    else ()
                )
                remember_inline(nid, expression, prior)
                writes = (
                    ast.walk(st)
                    if not isinstance(st, ast.AnnAssign) or st.value is not None
                    else ()
                )
                for sub in writes:
                    if isinstance(sub, (ast.Attribute, ast.Subscript)) and isinstance(
                        sub.ctx, (ast.Store, ast.Del)
                    ):
                        nodes[-1]["mutation_targets"].append(_mutation_label(sub))
                        if isinstance(sub, ast.Subscript) and not isinstance(
                            sub.slice, ast.Constant
                        ):
                            diagnostics.append(
                                {
                                    "code": "dynamic-index-write",
                                    "node": nid,
                                    "line": sub.lineno,
                                    "message": "dynamic index write may overlap other access paths",
                                }
                            )

    collect(fn.body)
    for st in fn.body:
        edge(entry, stmt_id[st], "AST", "body")
    for st in ast.walk(fn):
        if isinstance(st, ast.stmt) and not isinstance(
            st, _SUPPORTED_STMTS + _UNSUPPORTED
        ):
            raise UnsupportedFlowError(
                f"unsupported {type(st).__name__} in {path}:{st.lineno}"
            )
    for st, nid in stmt_id.items():
        if isinstance(st, ast.For):
            continue
        expression = st.test if isinstance(st, (ast.If, ast.While)) else st
        if any(isinstance(child, ast.Call) for child in ast.walk(expression)):
            diagnostics.append(
                {
                    "code": "opaque-call-effects",
                    "node": nid,
                    "line": nodes[int(nid[1:])]["line"],
                    "message": "call effects and implicit exceptions are unknown",
                }
            )
    # AST statement hierarchy edges and normal CFG, with explicit abrupt exits.

    def block(stmts, incoming, follow, loop=None):
        cur = set(incoming)
        for index, st in enumerate(stmts):
            n = stmt_id[st]
            following = (
                {stmt_id[stmts[index + 1]]} if index + 1 < len(stmts) else follow
            )
            for p in cur:
                edge(p, n, "CFG", "next")
            if isinstance(st, ast.If):
                for child in st.body + st.orelse:
                    edge(
                        n,
                        stmt_id[child],
                        "AST",
                        "body" if child in st.body else "orelse",
                    )
                if st.body:
                    edge(n, stmt_id[st.body[0]], "CFG", "true")
                if st.orelse:
                    edge(n, stmt_id[st.orelse[0]], "CFG", "false")
                left = block(st.body, {n}, following, loop)
                right = block(st.orelse, {n}, following, loop) if st.orelse else {n}
                if not st.orelse and following:
                    edge(n, next(iter(following)), "CFG", "false")
                cur = left | right
            elif isinstance(st, (ast.For, ast.While)):
                body_ids = [stmt_id[x] for x in st.body]
                else_ids = [stmt_id[x] for x in st.orelse]
                for child in st.body + st.orelse:
                    edge(
                        n,
                        stmt_id[child],
                        "AST",
                        "body" if child in st.body else "orelse",
                    )
                if isinstance(st, ast.For):
                    it = add(
                        "iterator_init",
                        st,
                        ast.get_source_segment(source, st.iter) or "iterable",
                        defs=_target_names(st.iter),
                        synthetic=True,
                        uses=_names(st.iter) | _paths(st.iter),
                    )
                    remember_inline(it, st.iter)
                    if any(isinstance(child, ast.Call) for child in ast.walk(st.iter)):
                        diagnostics.append(
                            {
                                "code": "opaque-call-effects",
                                "node": it,
                                "line": st.lineno,
                                "message": "call effects and implicit exceptions are unknown",
                            }
                        )
                    pred = add("iterator_test", st, "iterator test", synthetic=True)
                    bind = add(
                        "target_binding",
                        st,
                        f"bind {ast.unparse(st.target)}",
                        synthetic=True,
                        defs=_target_names(st.target),
                    )
                    for machinery in (it, pred, bind):
                        nodes[int(machinery[1:])]["line"] = st.lineno
                        nodes[int(machinery[1:])]["end_line"] = getattr(
                            st.iter, "end_lineno", st.lineno
                        )
                    edge(n, it, "AST", "iterator-init")
                    edge(n, pred, "AST", "iterator-test")
                    edge(n, bind, "AST", "target-binding")
                    edge(it, bind, "DATA_DEP", "iterator-value")
                    edge(n, it, "CFG", "evaluate-iterable")
                    edge(it, pred, "CFG", "initialize")
                    edge(pred, bind, "CFG", "iterate")
                    if not body_ids:
                        edge(bind, pred, "CFG", "empty-iteration")
                    edge(
                        pred,
                        next(iter(else_ids), next(iter(following), exit_id)),
                        "CFG",
                        "zero-or-done",
                    )
                    start = bind
                else:
                    pred = n
                    edge(n, next(iter(body_ids), pred), "CFG", "true")
                    edge(
                        n,
                        next(iter(else_ids), next(iter(following), exit_id)),
                        "CFG",
                        "false",
                    )
                    start = n
                after = next(iter(following), exit_id)
                child_loop = (after, pred)
                inner = block(st.body, {start}, {pred}, child_loop)
                for x in inner:
                    edge(x, pred, "CFG", "backedge")
                tail = block(st.orelse, {pred}, following, loop) if st.orelse else set()
                cur = set(tail)
            elif isinstance(st, ast.Break):
                if loop:
                    edge(n, loop[0], "CFG", "break")
                cur = set()
            elif isinstance(st, ast.Continue):
                if loop:
                    edge(n, loop[1], "CFG", "continue")
                cur = set()
            elif isinstance(st, (ast.Return, ast.Raise)):
                edge(n, exit_id, "CFG", "abrupt-exit")
                cur = set()
            elif isinstance(st, ast.Assert):
                edge(n, exit_id, "CFG", "assert-failure")
                cur = {n}
            else:
                cur = {n}
        return cur

    tails = block(fn.body, {param_tail}, {exit_id})
    for tail in tails:
        edge(tail, exit_id, "CFG", "exit")
    # Postdominators identify control dependence, including a false guard that
    # continues after an early return in the true arm.
    cfg_nodes = {n["id"] for n in nodes}
    successors = defaultdict(set)
    for e in edges:
        if e["kind"] == "CFG":
            successors[e["source"]].add(e["target"])
    reachable = {entry}
    queue = deque([entry])
    while queue:
        for target in successors[queue.popleft()] - reachable:
            reachable.add(target)
            queue.append(target)
    for n in nodes:
        n["reachable"] = n["id"] in reachable
    reachable_cfg = reachable & cfg_nodes
    postdom = {n: set(reachable_cfg) for n in reachable_cfg}
    postdom[exit_id] = {exit_id}
    changed = True
    while changed:
        changed = False
        for nid in reachable_cfg - {exit_id}:
            succ = successors[nid] & reachable_cfg
            new = {nid} | (
                set.intersection(*(postdom[s] for s in succ)) if succ else {exit_id}
            )
            if new != postdom[nid]:
                postdom[nid] = new
                changed = True
    for nid, succ in successors.items():
        if nid not in reachable_cfg:
            continue
        succ &= reachable_cfg
        if len(succ) < 2:
            continue
        for branch in sorted(succ):
            controlled = postdom[branch] - postdom[nid]
            for target in sorted(controlled):
                if target != nid and target != exit_id:
                    edge(
                        nid,
                        target,
                        "CONTROL_DEP",
                        next(
                            (
                                e["label"]
                                for e in edges
                                if e["kind"] == "CFG"
                                and e["source"] == nid
                                and e["target"] == branch
                            ),
                            "branch",
                        ),
                    )
    # Reaching definitions fixed point over CFG, preserving distinct parameters.
    cfg = defaultdict(set)
    for e in edges:
        if e["kind"] == "CFG":
            cfg[e["source"]].add(e["target"])
    byid = {n["id"]: n for n in nodes}
    gen = {n["id"]: {(n["id"], name) for name in n["defs"]} for n in nodes}
    in_defs = {n["id"]: set() for n in nodes}
    out_defs = {n["id"]: set() for n in nodes}
    changed = True
    while changed:
        changed = False
        for n in nodes:
            nid = n["id"]
            newin = (
                set().union(
                    *(
                        out_defs[e["source"]]
                        for e in edges
                        if e["kind"] == "CFG"
                        and e["target"] == nid
                        and byid[e["source"]]["reachable"]
                    )
                )
                if n["reachable"]
                else set()
            )
            kills = {
                item
                for item in newin
                if _killed_by_rebinding(
                    item[1], n["defs"], weak_definitions.get(nid, set())
                )
            }
            newout = (newin - kills) | gen[nid]
            if newin != in_defs[nid] or newout != out_defs[nid]:
                in_defs[nid] = newin
                out_defs[nid] = newout
                changed = True
    params = {a.arg for a in arguments}
    locals_ = set(params)
    for stmt in fn.body:
        locals_.update(_target_names(stmt))
    must_in = {n["id"]: set(locals_) for n in nodes}
    must_out = {n["id"]: set(locals_) for n in nodes}
    must_in[entry] = set()
    must_out[entry] = set()
    changed = True
    while changed:
        changed = False
        for n in nodes:
            nid = n["id"]
            if nid not in reachable:
                continue
            preds = [
                e["source"]
                for e in edges
                if e["kind"] == "CFG"
                and e["target"] == nid
                and byid[e["source"]]["reachable"]
            ]
            newin = set.intersection(*(must_out[p] for p in preds)) if preds else set()
            newout = newin | set(n["defs"])
            if newin != must_in[nid] or newout != must_out[nid]:
                must_in[nid] = newin
                must_out[nid] = newout
                changed = True
    for n in nodes:
        if not n["reachable"]:
            continue
        if n["kind"] in (
            "entry",
            "exit",
            "parameter",
            "iterator_test",
            "target_binding",
        ):
            continue
        for name in n["uses"]:
            found = sorted(d for d, defined in in_defs[n["id"]] if name == defined)
            inline_after_use = name in inline_after.get(n["id"], set())
            inline_before_use = name in inline_before.get(n["id"], set())
            if found and not (inline_after_use and not inline_before_use):
                for d in found:
                    edge(d, n["id"], "DATA_DEP", name)
            if (
                name in locals_
                and name not in must_in[n["id"]]
                and not (inline_after_use and not inline_before_use)
            ):
                diagnostics.append(
                    {
                        "code": "possibly-unbound-local",
                        "node": n["id"],
                        "line": n["line"],
                        "message": f"local {name!r} may have no reaching definition",
                    }
                )
            if inline_after_use:
                edge(n["id"], n["id"], "DATA_DEP", name)
            elif (
                name not in locals_ or "." in name or name.endswith("[*]")
            ) and not found:
                diagnostics.append(
                    {
                        "code": "unknown-live-in",
                        "node": n["id"],
                        "line": n["line"],
                        "message": f"{name!r} has no observed local definition; provenance is live-in/unknown",
                    }
                )
    edges = [
        {"source": source_id, "target": target_id, "kind": kind, "label": label}
        for kind, source_id, target_id, label in sorted(
            {(e["kind"], e["source"], e["target"], e["label"]) for e in edges}
        )
    ]
    diagnostics_by_key = {
        (d["line"], d["node"], d["code"], d["message"]): d for d in diagnostics
    }
    diagnostics = [diagnostics_by_key[key] for key in sorted(diagnostics_by_key)]
    digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
    engine_digest = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return {
        "profile": "python-local-cpg-v1",
        "path": path,
        "function": qualname,
        "source_sha256": digest,
        "engine_sha256": engine_digest,
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "nodes": nodes,
        "edges": edges,
        "diagnostics": diagnostics,
        "limitations": [
            "Calls are opaque; aliases and unknown call side effects can affect memory dependencies.",
            "Statement-level reads are conservative; wildcard object/subscript paths retain possible overlaps, but aliases are not resolved and implicit exception flow is not modeled.",
        ],
    }


def slice_flow(
    graph: dict,
    *,
    variable=None,
    line=None,
    direction="backward",
    include_control=True,
    max_tokens=8000,
) -> dict:
    if direction not in ("backward", "forward"):
        raise FlowQueryError(f"invalid direction: {direction}")
    if line is not None and (
        not isinstance(line, int) or isinstance(line, bool) or line <= 0
    ):
        raise FlowQueryError("line must be a positive integer")
    if (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or max_tokens <= 0
    ):
        raise InvalidBudgetError("max_tokens must be a positive integer")
    nodes = graph["nodes"]
    {n["id"]: n for n in nodes}
    if variable is None and line is None:
        report = {
            k: v for k, v in graph.items() if k not in ("nodes", "edges", "diagnostics")
        }
        report.update(
            {
                "query": {
                    "variable": None,
                    "line": None,
                    "direction": direction,
                    "include_control": include_control,
                },
                "seeds": [],
                "nodes": nodes,
                "edges": [
                    edge
                    for edge in graph["edges"]
                    if include_control or edge["kind"] != "CONTROL_DEP"
                ],
                "diagnostics": graph.get("diagnostics", []),
            }
        )
        tokens = serialized_tokens(report)
        if tokens > max_tokens:
            raise FlowBudgetExceeded(
                f"flow needs {tokens} tokens, budget is {max_tokens}"
            )
        return {"tokens": tokens, "flow": report}
    candidates = [
        n
        for n in nodes
        if (variable is None or variable in n["defs"] or variable in n["uses"])
        and (line is None or n["line"] <= line <= n["end_line"])
    ]
    if (variable is not None or line is not None) and not candidates:
        raise FlowQueryError(
            f"no flow node matches variable={variable!r}, line={line!r}"
        )
    if variable is not None and line is None:
        defined = [n for n in candidates if variable in n["defs"]]
        candidates = defined or candidates
    seeds = [n["id"] for n in candidates]
    kinds = {"DATA_DEP"} | ({"CONTROL_DEP"} if include_control else set())
    adjacency = defaultdict(set)
    for e in graph["edges"]:
        if e["kind"] in kinds:
            a, b = (e["source"], e["target"])
            if direction == "backward":
                a, b = b, a
            adjacency[a].add(b)
    selected = set(seeds)
    q = deque(seeds)
    while q:
        for v in adjacency[q.popleft()] - selected:
            selected.add(v)
            q.append(v)
    report = {
        k: v for k, v in graph.items() if k not in ("nodes", "edges", "diagnostics")
    }
    report.update(
        {
            "query": {
                "variable": variable,
                "line": line,
                "direction": direction,
                "include_control": include_control,
            },
            "seeds": seeds,
            "nodes": [n for n in nodes if n["id"] in selected],
            "edges": [
                e
                for e in graph["edges"]
                if e["source"] in selected
                and e["target"] in selected
                and (include_control or e["kind"] != "CONTROL_DEP")
            ],
            "diagnostics": [
                d for d in graph.get("diagnostics", []) if d.get("node") in selected
            ],
        }
    )
    tokens = serialized_tokens(report)
    if tokens > max_tokens:
        raise FlowBudgetExceeded(f"flow needs {tokens} tokens, budget is {max_tokens}")
    return {"tokens": tokens, "flow": report}
