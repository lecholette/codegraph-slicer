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
    ast.AsyncFor,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
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
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.TryStar,
)


def _names(node):
    result = set()

    def visit(n, bound=frozenset()):
        if isinstance(n, ast.Name):
            if isinstance(n.ctx, ast.Load) and n.id not in bound:
                result.add(n.id)
            return
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
            targets = set().union(*(_target_names(gen.target) for gen in n.generators))
            local = set(bound) | targets
            for index, gen in enumerate(n.generators):
                visit(gen.iter, bound if index == 0 else frozenset(local))
                for condition in gen.ifs:
                    visit(condition, frozenset(local))
            if isinstance(n, ast.DictComp):
                visit(n.key, frozenset(local))
                visit(n.value, frozenset(local))
            else:
                visit(n.elt, frozenset(local))
            return
        for child in ast.iter_child_nodes(n):
            visit(child, bound)

    if node is not None:
        visit(node)
    return result


def _paths(node):
    out = set()

    def visit(n, bound=frozenset()):
        if n is None:
            return
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            local = bound | set().union(*(_target_names(gen.target) for gen in n.generators))
            for index, gen in enumerate(n.generators):
                visit(gen.iter, bound if index == 0 else local)
                for condition in gen.ifs:
                    visit(condition, local)
            for part in ((n.key, n.value) if isinstance(n, ast.DictComp) else (n.elt,)):
                visit(part, local)
            return
        if isinstance(n, (ast.Attribute, ast.Subscript)) and isinstance(n.ctx, ast.Load):
            root = n
            while isinstance(root, (ast.Attribute, ast.Subscript)):
                root = root.value
            if not isinstance(root, ast.Name) or root.id not in bound:
                out.update((ast.unparse(n), _wildcard_path(n)))
        for child in ast.iter_child_nodes(n):
            visit(child, bound)

    visit(node)
    return out


def _target_names(node):
    found = set()
    def visit(n):
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            return
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            found.add(n.id)
        for child in ast.iter_child_nodes(n):
            visit(child)
    if node is not None:
        visit(node)
    return found


def _plain_target_names(node):
    """Names assigned by a target, excluding walruses inside its expressions."""
    found = set()

    def visit(n):
        if isinstance(n, ast.NamedExpr):
            return
        if isinstance(n, ast.Name) and isinstance(n.ctx, (ast.Store, ast.Del)):
            found.add(n.id)
        for child in ast.iter_child_nodes(n):
            visit(child)

    if node is not None:
        visit(node)
    return found


def _walrus_names(node):
    return {
        child.target.id
        for child in ast.walk(node) if isinstance(child, ast.NamedExpr)
    } if node is not None else set()


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
        if isinstance(expr, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            for name in sorted(_names(expr)):
                events.append(("load", name))
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


def _target_events(node, *, read_only=False):
    """Events from evaluating and storing a target, preserving target order."""
    events = []

    def visit(target):
        if target is None:
            return
        if isinstance(target, ast.Name):
            if read_only:
                events.append(("load", target.id))
            elif isinstance(target.ctx, ast.Store):
                events.append(("store", target.id))
            return
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                visit(element)
            return
        if isinstance(target, ast.Starred):
            visit(target.value)
            return
        if isinstance(target, ast.Attribute):
            events.extend(_expression_events(target.value))
            return
        if isinstance(target, ast.Subscript):
            events.extend(_expression_events(target.value))
            events.extend(_expression_events(target.slice))
            return
        events.extend(_expression_events(target))

    visit(node)
    return events


def _target_write_events(node):
    """Events for the final local-name store after an augmented target read."""
    if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
        return [("store", node.id)]
    return []


def _ordered_expression(*nodes):
    """Return one event stream for expressions evaluated in the given order."""
    return [event for node in nodes for event in _expression_events(node)]


def _inline_name_flow(node):
    defined = set()
    reads_after = set()
    reads_before = set()
    events = node if isinstance(node, list) else _expression_events(node)
    for kind, name in events:
        if kind == "store":
            defined.add(name)
        elif name in defined:
            reads_after.add(name)
        else:
            reads_before.add(name)
    return reads_after, reads_before


def _target_access_roots(node):
    if not isinstance(node, (ast.Attribute, ast.Subscript)):
        return set()
    root = node
    while isinstance(root, (ast.Attribute, ast.Subscript)):
        root = root.value
    return {root.id} if isinstance(root, ast.Name) else set()


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
    if isinstance(node, (ast.Yield, ast.YieldFrom, ast.Await, ast.Lambda)):
        raise UnsupportedFlowError(
            f"unsupported {type(node).__name__} in {path}:{node.lineno}"
        )
    if (
        isinstance(node, (ast.Attribute, ast.Subscript))
        and isinstance(node.ctx, ast.Store)
        and _target_access_roots(node) & _walrus_names(node)
    ):
        raise UnsupportedFlowError(
            f"unsupported target receiver rebinding in {path}:{node.lineno}"
        )
    if isinstance(node, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
        for gen in node.generators:
            if not isinstance(gen.target, (ast.Name, ast.Tuple, ast.List)) or any(
                not isinstance(child, (ast.Name, ast.Tuple, ast.List, ast.Load, ast.Store))
                for child in ast.walk(gen.target)
            ):
                raise UnsupportedFlowError(f"unsupported comprehension target in {path}:{gen.target.lineno}")
        if any(gen.is_async for gen in node.generators):
            raise UnsupportedFlowError(f"unsupported async comprehension in {path}:{node.lineno}")
        if any(isinstance(child, ast.NamedExpr) for child in ast.walk(node)):
            raise UnsupportedFlowError(f"unsupported named expression in comprehension in {path}:{node.lineno}")
        if isinstance(node, ast.GeneratorExp):
            bound = set().union(*(_target_names(gen.target) for gen in node.generators))
            deferred = []
            for index, gen in enumerate(node.generators):
                names = _names(gen.iter) - (set() if index == 0 else bound)
                if index > 0:
                    deferred.extend(names)
                for condition in gen.ifs:
                    deferred.extend(_names(condition) - bound)
            payload = (node.key, node.value) if isinstance(node, ast.DictComp) else (node.elt,)
            for part in payload:
                deferred.extend(_names(part) - bound)
            if deferred:
                raise UnsupportedFlowError(f"unsupported deferred generator capture in {path}:{node.lineno}")
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
    if len(candidates) != 1 or not isinstance(candidates[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
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
            target_names = set().union(*(_plain_target_names(t) for t in stmt.targets))
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
    with_parts = {}
    try_parts = {}
    inline_after = {}
    inline_before = {}
    weak_definitions = {}

    def remember_inline(nid, expression, before=()):
        after, earlier = _inline_name_flow(expression)
        inline_after[nid] = after
        inline_before[nid] = earlier | set(before)

    def collect(stmts):
        for st in stmts:
            if isinstance(st, (ast.Try, ast.TryStar)):
                handlers = []
                for handler in st.handlers:
                    handler_node = add(
                        "except_handler", handler,
                        "except " + (ast.unparse(handler.type) if handler.type else "BaseException"),
                        defs=set([handler.name] if handler.name else ()) | _target_names(handler.type),
                        uses=_names(handler.type) | _paths(handler.type),
                    )
                    if handler.type is not None:
                        remember_inline(handler_node, handler.type)
                        unmatched_node = add(
                            "except_type_unmatched", handler,
                            "unmatched " + ast.unparse(handler.type),
                            defs=_target_names(handler.type),
                            uses=_names(handler.type) | _paths(handler.type),
                            synthetic=True,
                        )
                        remember_inline(unmatched_node, handler.type)
                    else:
                        unmatched_node = None
                    delete = bool(handler.name)
                    handlers.append((handler, handler_node, delete, unmatched_node))
                nid = add("trystar" if isinstance(st, ast.TryStar) else "try", st, text="try*" if isinstance(st, ast.TryStar) else "try")
                stmt_id[st] = nid
                try_parts[st] = handlers
                collect(st.body)
                for handler, _, _, _ in handlers:
                    collect(handler.body)
                collect(st.orelse)
                collect(st.finalbody)
                continue
            if isinstance(st, (ast.With, ast.AsyncWith)):
                parts = []
                for item in st.items:
                    manager = add(
                        "with_enter", item.context_expr,
                        ast.get_source_segment(source, item.context_expr) or "context manager",
                        defs=_target_names(item.context_expr),
                        uses=_names(item.context_expr) | _paths(item.context_expr),
                        synthetic=True,
                    )
                    remember_inline(manager, item.context_expr)
                    bind = None
                    if item.optional_vars is not None:
                        bind = add(
                            "with_binding", item.optional_vars,
                            f"bind {ast.unparse(item.optional_vars)}",
                            defs=_target_names(item.optional_vars) | _access_targets(item.optional_vars),
                            uses=_names(item.optional_vars) | _paths(item.optional_vars), synthetic=True,
                        )
                        weak_definitions[bind] = _dynamic_access_targets(item.optional_vars)
                        remember_inline(bind, _target_events(item.optional_vars))
                        edge(manager, bind, "DATA_DEP", "context-value")
                    parts.append((manager, bind))
                header = "async with " if isinstance(st, ast.AsyncWith) else "with "
                header += ", ".join(
                    ast.unparse(item.context_expr)
                    + (f" as {ast.unparse(item.optional_vars)}" if item.optional_vars else "")
                    for item in st.items
                ) + ":"
                nid = add("asyncwith" if isinstance(st, ast.AsyncWith) else "with", st, text=header,
                          uses=())
                stmt_id[st] = nid
                with_parts[st] = parts
                collect(st.body)
                continue
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
                if isinstance(st, ast.AnnAssign):
                    d = _target_names(st.target) | _access_targets(st.target)
                    if st.value is not None:
                        d |= _target_names(st.value)
                    else:
                        d = _walrus_names(st.target)
                u = _names(st) | _paths(st)
                if isinstance(st, ast.AnnAssign):
                    u = (
                        _names(st.value) | _paths(st.value)
                        if st.value is not None
                        else set()
                    )
                    u |= _names(st.target) | _paths(st.target)
                if isinstance(st, ast.AugAssign):
                    u |= _plain_target_names(st.target) | _access_targets(st.target)
                nid = add(type(st).__name__.lower(), st, defs=d, uses=u)
                stmt_id[st] = nid
                weak_definitions[nid] = _dynamic_access_targets(st)
                expression = (
                    _expression_events(st.value) + [
                        event for target in st.targets for event in _target_events(target)
                    ]
                    if isinstance(st, ast.Assign)
                    else _expression_events(st.value) + _target_events(st.target)
                    if isinstance(st, ast.AnnAssign) and st.value is not None
                    else _target_events(st.target, read_only=True)
                    if isinstance(st, ast.AnnAssign)
                    else _target_events(st.target, read_only=True)
                    + _expression_events(st.value)
                    + _target_write_events(st.target)
                    if isinstance(st, ast.AugAssign)
                    else st.value
                    if isinstance(st, (ast.Expr, ast.Return))
                    else _ordered_expression(st.exc, st.cause)
                    if isinstance(st, ast.Raise)
                    else st.test
                    if isinstance(st, ast.Assert)
                    else None
                )
                remember_inline(nid, expression)
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
            st, _SUPPORTED_STMTS + _UNSUPPORTED + (ast.AsyncFunctionDef,)
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
                        defs=_target_names(st.target) | _access_targets(st.target),
                        uses=_names(st.target) | _paths(st.target),
                    )
                    weak_definitions[bind] = _dynamic_access_targets(st.target)
                    remember_inline(bind, _target_events(st.target))
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

    def structured_block(stmts, incoming):
        """Return disjoint pending completions; cleanup cannot merge their tails."""
        result = defaultdict(set)
        cur = set(incoming)
        for st in stmts:
            n = stmt_id[st]
            pre = add("exception_point", st, "before " + type(st).__name__, synthetic=True)
            for tail in cur:
                edge(tail, pre, "CFG", "next")
            edge(pre, n, "CFG", "complete")
            pending = defaultdict(set)
            for field in ("body", "orelse", "finalbody"):
                for child in getattr(st, field, ()):
                    edge(n, stmt_id[child], "AST", field)
            for handler, hn, deletion, unmatched in try_parts.get(st, ()):
                edge(n, hn, "AST", "handler")
                if unmatched:
                    edge(n, unmatched, "AST", "handler-type-unmatched")
                for child in handler.body:
                    edge(hn, stmt_id[child], "AST", "body")
            pending["raise"].update((pre, n))
            if isinstance(st, ast.If):
                for label, suite in (("true", st.body), ("false", st.orelse)):
                    start = add("branch", st, label, synthetic=True)
                    edge(n, start, "CFG", label)
                    for key, tails in structured_block(suite, {start}).items():
                        pending[key].update(tails)
            elif isinstance(st, (ast.For, ast.While)):
                pred = n
                if isinstance(st, ast.For):
                    init = add("iterator_init", st, ast.get_source_segment(source, st.iter) or "iterable", defs=_target_names(st.iter), uses=_names(st.iter) | _paths(st.iter), synthetic=True)
                    remember_inline(init, st.iter)
                    pred = add("iterator_test", st, "iterator test", synthetic=True)
                    bind = add("target_binding", st, f"bind {ast.unparse(st.target)}", defs=_target_names(st.target) | _access_targets(st.target), uses=_names(st.target) | _paths(st.target), synthetic=True)
                    weak_definitions[bind] = _dynamic_access_targets(st.target)
                    remember_inline(bind, _target_events(st.target))
                    edge(n, init, "CFG", "evaluate-iterable")
                    edge(init, pred, "CFG", "initialize")
                    edge(init, bind, "DATA_DEP", "iterator-value")
                    edge(pred, bind, "CFG", "iterate")
                    pending["raise"].update((init, pred, bind))
                    start = bind
                else:
                    start = add("branch", st, "true", synthetic=True)
                    edge(n, start, "CFG", "true")
                inner = structured_block(st.body, {start})
                for tail in inner.pop("normal", set()) | inner.pop("continue", set()):
                    edge(tail, pred, "CFG", "backedge")
                broken = inner.pop("break", set())
                for key, tails in inner.items():
                    pending[key].update(tails)
                exhausted = add("branch", st, "loop exhausted", synthetic=True)
                edge(pred, exhausted, "CFG", "zero-or-done")
                for key, tails in structured_block(st.orelse, {exhausted}).items():
                    pending[key].update(tails)
                pending["normal"].update(broken)
            elif isinstance(st, (ast.Try, ast.TryStar)):
                body = structured_block(st.body, {n})
                exceptional = body.pop("raise", set())
                normal = body.pop("normal", set())
                for key, tails in body.items():
                    pending[key].update(tails)
                # The header itself cannot raise; protected-body exceptions do.
                pending["raise"].clear()
                for key, tails in structured_block(st.orelse, normal).items():
                    pending[key].update(tails)
                handlers = try_parts[st]
                star_previous = set()
                prior_unmatched = None
                for handler, hn, delete, unmatched in handlers:
                    if prior_unmatched is not None:
                        edge(prior_unmatched, hn, "CFG", "exception-unmatched")
                        if unmatched is not None:
                            edge(prior_unmatched, unmatched, "CFG", "exception-unmatched")
                    if prior_unmatched is None:
                        for tail in exceptional:
                            label = "exception-before-statement" if nodes[int(tail[1:])]["kind"] == "exception_point" else "exception-after-statement"
                            edge(tail, hn, "CFG", label)
                            if unmatched is not None:
                                edge(tail, unmatched, "CFG", label)
                    if isinstance(st, ast.TryStar):
                        for tail in star_previous:
                            edge(tail, hn, "CFG", "except-star-next")
                            if unmatched is not None:
                                edge(tail, unmatched, "CFG", "except-star-next")
                    handled = structured_block(handler.body, {hn})
                    if delete:
                        handled = cleanup_delete(handled, handler)
                    if isinstance(st, ast.TryStar):
                        if handler.type is not None and _target_names(handler.type):
                            # Route older matched subsets through this type test
                            # before they can reach later handlers, so its
                            # walrus definitions cannot be bypassed.
                            star_previous.clear()
                        star_previous.update(set().union(*handled.values()))
                    for key, tails in handled.items():
                        pending[key].update(tails)
                    prior_unmatched = unmatched
                    if unmatched is not None:
                        # Type lookup and exception matching happen after any
                        # walrus stores and can themselves raise outward.
                        pending["raise"].add(unmatched)
                if not any(handler.type is None for handler, _, _, _ in handlers):
                    pending["raise"].update(exceptional)
                if st.finalbody:
                    pending = cleanup_finally(pending, st.finalbody)
            elif isinstance(st, (ast.With, ast.AsyncWith)):
                pending.clear()
                # Acquiring manager i fails before it becomes an active cleanup.
                def acquire(index, starts, st=st):
                    if index == len(st.items):
                        return structured_block(st.body, starts)
                    manager, bind = with_parts[st][index]
                    evaluation = add("exception_point", st.items[index].context_expr, f"before manager {index + 1}", synthetic=True)
                    for tail in starts:
                        edge(tail, evaluation, "CFG", "evaluate-manager")
                    edge(evaluation, manager, "CFG", "complete")
                    acquired = {manager}
                    binding_failure = set()
                    if bind:
                        edge(manager, bind, "CFG", "bind-target")
                        binding_failure.update((manager, bind))
                        acquired = {bind}
                    inside = acquire(index + 1, acquired)
                    inside["raise"].update(binding_failure)
                    cleaned = defaultdict(set)
                    for key, tails in inside.items():
                        if not tails:
                            continue
                        leave = add("with_exit", st.items[index].context_expr, f"exit manager {index + 1} ({key})", synthetic=True)
                        for tail in tails:
                            edge(tail, leave, "CFG", "enter-body" if tail == bind else "context-" + key)
                        cleaned[key].add(leave)
                        cleaned["raise"].add(leave)
                        if key == "raise":
                            suppressed = add("with_suppressed", st, "suppressed exception", synthetic=True)
                            edge(leave, suppressed, "CFG", "suppressed-exit")
                            cleaned["normal"].add(suppressed)
                    # __enter__ failure does not call this manager's __exit__.
                    cleaned["raise"].update((evaluation, manager))
                    return cleaned
                pending = acquire(0, {n})
            elif isinstance(st, (ast.Return, ast.Raise, ast.Break, ast.Continue)):
                pending[type(st).__name__.lower()].add(n)
            else:
                pending["normal"].add(n)
            cur = pending.pop("normal", set())
            for key, tails in pending.items():
                result[key].update(tails)
        result["normal"].update(cur)
        return result

    def cleanup_delete(pending, handler):
        cleaned = defaultdict(set)
        for key, tails in pending.items():
            if tails:
                deletion = add("exception_name_delete", handler, f"delete {handler.name}", defs=[handler.name], synthetic=True)
                for tail in tails:
                    edge(tail, deletion, "CFG", "implicit-delete")
                cleaned[key].add(deletion)
        return cleaned

    def cleanup_finally(pending, suite):
        cleaned = defaultdict(set)
        first = True
        for key, tails in list(pending.items()):
            if not tails:
                continue
            saved = (stmt_id.copy(), with_parts.copy(), try_parts.copy())
            if not first:
                collect(suite)
            first = False
            bridge = add("finally_entry", suite[0], "finally " + key, synthetic=True)
            for tail in tails:
                edge(tail, bridge, "CFG", "finally-" + key)
            final = structured_block(suite, {bridge})
            cleaned[key].update(final.pop("normal", set()))
            for override, endings in final.items():
                cleaned[override].update(endings)
            stmt_id.clear()
            stmt_id.update(saved[0])
            with_parts.clear()
            with_parts.update(saved[1])
            try_parts.clear()
            try_parts.update(saved[2])
        return cleaned

    structured = any(isinstance(st, (ast.Try, ast.TryStar, ast.With, ast.AsyncWith)) for st in ast.walk(fn))
    if structured:
        completions = structured_block(fn.body, {param_tail})
        for key, tails in completions.items():
            for tail in tails:
                edge(tail, exit_id, "CFG", "exit" if key == "normal" else key + "-exit")
    else:
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
    gen = {n["id"]: {(n["id"], name) for name in n["defs"] if n["kind"] != "exception_name_delete"} for n in nodes}
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
    locals_.update(handler.name for handler in ast.walk(fn) if isinstance(handler, ast.ExceptHandler) and handler.name)
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
            newout = (newin - set(n["defs"])) if n["kind"] == "exception_name_delete" else newin | set(n["defs"])
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
    limitations = [
        "Calls are opaque; aliases and unknown call side effects can affect memory dependencies.",
        "Statement-level reads are conservative; wildcard object/subscript paths retain possible overlaps, but aliases are not resolved and implicit exception flow is not modeled.",
    ]
    if structured:
        limitations[1] = "Statement-level exception transfers preserve pre-store and post-store possibilities; exception matching and context-manager suppression are conservative, aliases are not resolved."
    if any(isinstance(item, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)) for statement in fn.body for item in ast.walk(statement)):
        limitations.append("Comprehensions collapse into enclosing statement reads; targets are scoped locally and deferred outer captures are refused.")
    return {
        "profile": "python-local-cpg-v2",
        "path": path,
        "function": qualname,
        "source_sha256": digest,
        "engine_sha256": engine_digest,
        "python_version": ".".join(map(str, sys.version_info[:3])),
        "nodes": nodes,
        "edges": edges,
        "diagnostics": diagnostics,
        "limitations": limitations,
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
