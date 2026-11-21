"""Lightweight polyglot Hoare-contract stub extraction (@requires, @raises, @mutates)."""
from __future__ import annotations

import ast
import textwrap
from dataclasses import dataclass

from .treesitter_support import (
    child_of_type,
    node_text,
    parser_for,
    scoped_descendants,
)

_PY_MUTATING_METHODS = frozenset(
    {"clear", "pop", "append", "extend", "update", "add", "remove", "discard", "setdefault"}
)
_BRACE_MUTATING_METHODS = frozenset(
    {"set", "delete", "clear", "push", "pop", "shift", "unshift", "splice", "put", "remove", "add"}
)
_TS_SKIP_SCOPES = frozenset(
    {
        "class_declaration",
        "abstract_class_declaration",
        "function_declaration",
        "generator_function_declaration",
        "arrow_function",
        "function_expression",
    }
)
_JAVA_SKIP_SCOPES = frozenset(
    {"class_declaration", "interface_declaration", "enum_declaration", "record_declaration", "lambda_expression"}
)
_BROAD_CATCH_NAMES = frozenset({"*", "Exception", "BaseException", "Throwable", "Error", "RuntimeException"})
_COMMENT_NODES = frozenset({"comment", "line_comment", "block_comment"})
_MAX_LITERAL_LEN = 80


@dataclass(frozen=True, slots=True)
class StubContract:
    requires: tuple[tuple[str, str], ...] = ()
    raises: tuple[tuple[str, str], ...] = ()
    caught_types: frozenset[str] = frozenset()
    mutates: tuple[str, ...] = ()


def _sanitize_text(raw: str, max_len: int = 120) -> str:
    chars: list[str] = []
    for ch in raw:
        code = ord(ch)
        if code < 32 or code == 127:
            chars.append(" " if ch in {"\t", "\n", "\r"} else f"\\x{code:02x}")
        else:
            chars.append(ch)
    collapsed = " ".join("".join(chars).split())
    return (collapsed[: max_len - 3] + "...") if len(collapsed) > max_len else collapsed


def _dedupe_cap(items: list[str], limit: int) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        cleaned = _sanitize_text(item)
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            out.append(cleaned)
            if len(out) >= limit:
                break
    return tuple(out)


def _dedupe_pairs(pairs: list[tuple[str, str]], limit: int) -> tuple[tuple[str, str], ...]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for expr, short in pairs:
        norm = _sanitize_text(expr)
        if norm and norm not in seen:
            seen.add(norm)
            out.append((norm, short))
            if len(out) >= limit:
                break
    return tuple(out)


def _short_exc_name(expr_name: str) -> str:
    return expr_name.split("(")[0].strip().rsplit(".", 1)[-1]


def _escape_str_literal(val: str) -> str:
    trimmed = (val[: _MAX_LITERAL_LEN - 3] + "...") if len(val) > _MAX_LITERAL_LEN else val
    parts: list[str] = []
    for ch in trimmed:
        code = ord(ch)
        if ch == "\\":
            parts.append("\\\\")
        elif ch == '"':
            parts.append('\\"')
        elif code < 32 or code == 127:
            parts.append(f"\\x{code:02x}")
        else:
            parts.append(ch)
    return f'"{"".join(parts)}"'


def _format_py_literal_arg(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Constant):
        return None
    if isinstance(node.value, str):
        return _escape_str_literal(node.value)
    if type(node.value) is int and abs(node.value).bit_length() <= 256:
        return repr(node.value)
    if isinstance(node.value, float):
        return repr(node.value)
    return None


def _safe_unparse(node: ast.AST) -> str | None:
    try:
        return ast.unparse(node).strip()
    except (ValueError, RecursionError):
        return None


def _format_py_call_raise(exc: ast.Call) -> tuple[str, str] | None:
    callee = _safe_unparse(exc.func)
    if not callee:
        return None
    lit = _format_py_literal_arg(exc.args[0]) if exc.args else None
    rendered = f"{callee}({lit})" if lit is not None else callee
    return rendered, _short_exc_name(callee)


def _format_py_raise(exc: ast.AST) -> tuple[str, str] | None:
    if isinstance(exc, ast.Call):
        return _format_py_call_raise(exc)
    if isinstance(exc, (ast.Name, ast.Attribute)):
        callee = _safe_unparse(exc)
        return (callee, _short_exc_name(callee)) if callee else None
    return None


def _extract_py_self_attr(target: ast.AST) -> str | None:
    curr = target
    while isinstance(curr, ast.Subscript):
        curr = curr.value
    if isinstance(curr, ast.Attribute) and isinstance(curr.value, ast.Name) and curr.value.id == "self":
        return f"self.{curr.attr}"
    return None


def _collect_py_caught_types(handler: ast.ExceptHandler) -> set[str]:
    if any(isinstance(n, ast.Raise) for n in ast.walk(handler)):
        return set()
    if handler.type is None:
        return {"*"}
    elts = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return {_short_exc_name(u) for elt in elts if (u := _safe_unparse(elt))}


def _is_single_raise_if(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.If)
        and not node.orelse
        and len(node.body) == 1
        and isinstance(node.body[0], ast.Raise)
    )


def _if_raise_precondition(node: ast.If) -> tuple[str, str] | None:
    cond = _safe_unparse(node.test)
    if not cond:
        return None
    exc_node = node.body[0].exc  # type: ignore[attr-defined]
    exc_pair = _format_py_raise(exc_node) if exc_node is not None else None
    return (f"not ({cond})", exc_pair[1] if exc_pair else "")


def _py_node_precondition(node: ast.AST) -> tuple[str, str] | None:
    if isinstance(node, ast.Assert):
        txt = _safe_unparse(node.test)
        return (txt, "AssertionError") if txt else None
    if _is_single_raise_if(node):
        return _if_raise_precondition(node)  # type: ignore[arg-type]
    return None


def _py_call_self_mutation(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return None
    if node.func.attr not in _PY_MUTATING_METHODS:
        return None
    return _extract_py_self_attr(node.func.value)


def _py_node_mutations(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Assign):
        return [a for tgt in node.targets if (a := _extract_py_self_attr(tgt))]
    if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        attr = _extract_py_self_attr(node.target)
        return [attr] if attr else []
    call_mut = _py_call_self_mutation(node)
    return [call_mut] if call_mut else []


def _walk_py_fn_nodes(fn_node: ast.FunctionDef | ast.AsyncFunctionDef):
    stack: list[ast.AST] = list(reversed(fn_node.body))
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        yield node
        stack.extend(reversed(list(ast.iter_child_nodes(node))))


def _scan_py_fn(fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> StubContract:
    reqs: list[tuple[str, str]] = []
    raises: list[tuple[str, str]] = []
    caught: set[str] = set()
    muts: list[str] = []
    is_ctor = fn_node.name in {"__init__", "__new__"}

    for node in _walk_py_fn_nodes(fn_node):
        pre = _py_node_precondition(node)
        if pre:
            reqs.append(pre)
        if isinstance(node, ast.Raise) and node.exc is not None:
            formatted = _format_py_raise(node.exc)
            if formatted:
                raises.append(formatted)
        elif isinstance(node, ast.Try):
            for handler in node.handlers:
                caught.update(_collect_py_caught_types(handler))
        elif not is_ctor:
            muts.extend(_py_node_mutations(node))

    return StubContract(
        requires=_dedupe_pairs(reqs, 2),
        raises=_dedupe_pairs(raises, 3),
        caught_types=frozenset(caught),
        mutates=_dedupe_cap(muts, 3),
    )


def _extract_py_contract(body: str) -> StubContract:
    try:
        tree = ast.parse(textwrap.dedent(body))
    except (SyntaxError, ValueError, RecursionError):
        return StubContract()
    for stmt in tree.body:
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return _scan_py_fn(stmt)
    return StubContract()


def _grammar_for_path(file_path: str) -> str:
    if file_path.endswith(".java"):
        return "java"
    if file_path.endswith((".ts", ".tsx", ".jsx")):
        return "tsx"
    return "javascript"


def _is_brace_ctor_node(data: bytes, node, grammar: str) -> bool:
    if grammar == "java":
        return node.type == "constructor_declaration"
    if node.type != "method_definition":
        return False
    name_node = child_of_type(node, "identifier", "property_identifier")
    return bool(name_node and node_text(data, name_node) == "constructor")


def _parse_brace_method_body(body: str, grammar: str) -> tuple[bytes, object | None, bool]:
    parser = parser_for(grammar)
    data = body.encode("utf-8")
    root = parser.parse(data).root_node
    if root.has_error:
        data = f"class _ {{\n{body}\n}}".encode("utf-8")
        root = parser.parse(data).root_node
    block_type = "block" if grammar == "java" else "statement_block"
    is_ctor = False
    for node in scoped_descendants(root, set()):
        if _is_brace_ctor_node(data, node, grammar):
            is_ctor = True
        if node.type == block_type:
            return data, node, is_ctor
    return data, None, False


def _normalize_brace_literal(raw: str) -> str:
    if len(raw) >= 2 and raw[0] in {"'", '"'} and raw[-1] == raw[0]:
        return _escape_str_literal(raw[1:-1])
    return _sanitize_text(raw, _MAX_LITERAL_LEN)


def _first_brace_literal_arg(data: bytes, args_node) -> str | None:
    if args_node is None:
        return None
    lit_types = {"string_literal", "string", "decimal_integer_literal", "number"}
    for child in args_node.children:
        if child.type in {"(", ")", ","}:
            continue
        has_subst = any(c.type == "template_substitution" for c in child.children)
        if child.type in lit_types and not has_subst:
            return _normalize_brace_literal(node_text(data, child).strip())
        return None
    return None


def _format_brace_throw(data: bytes, throw_node) -> tuple[str, str] | None:
    new_expr = child_of_type(throw_node, "object_creation_expression", "new_expression")
    if new_expr is None:
        return None
    type_node = child_of_type(
        new_expr, "type_identifier", "scoped_type_identifier", "generic_type", "identifier", "member_expression"
    )
    if type_node is None:
        return None
    exc_type = node_text(data, type_node).split("<", 1)[0].strip()
    lit = _first_brace_literal_arg(data, child_of_type(new_expr, "argument_list", "arguments"))
    rendered = f"{exc_type}({lit})" if lit is not None else exc_type
    return rendered, _short_exc_name(exc_type)


def _single_throw_from_consequence(node):
    if node is None:
        return None
    if node.type == "throw_statement":
        return node
    if node.type not in {"block", "statement_block"}:
        return None
    stmts = [c for c in node.children if c.type not in {"{", "}", *_COMMENT_NODES}]
    return stmts[0] if len(stmts) == 1 and stmts[0].type == "throw_statement" else None


def _consequence_after_cond(if_node, cond_node):
    seen_cond = False
    for child in if_node.children:
        if child.id == cond_node.id:
            seen_cond = True
        elif seen_cond and child.type not in _COMMENT_NODES:
            return child
    return None


def _extract_if_guard_pair(data: bytes, if_node) -> tuple[str, str] | None:
    if any(c.type in {"else", "else_clause"} for c in if_node.children):
        return None
    cond_node = child_of_type(if_node, "parenthesized_expression")
    throw_stmt = _single_throw_from_consequence(_consequence_after_cond(if_node, cond_node)) if cond_node else None
    if cond_node is None or throw_stmt is None:
        return None
    inner = node_text(data, cond_node).strip()
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1].strip()
    throw_pair = _format_brace_throw(data, throw_stmt)
    return (f"not ({inner})", throw_pair[1] if throw_pair else "")


def _this_field_from_lhs(lhs: str) -> str | None:
    if not lhs.startswith("this."):
        return None
    attr = lhs[5:].split(".", 1)[0].split("[", 1)[0].strip()
    return f"this.{attr}" if attr.isidentifier() else None


def _this_field_from_call(head: str) -> str | None:
    if not head.startswith("this."):
        return None
    parts = head.split(".")
    if len(parts) >= 3 and parts[1].isidentifier() and parts[2] in _BRACE_MUTATING_METHODS:
        return f"this.{parts[1]}"
    return None


def _extract_this_mutation(data: bytes, node) -> str | None:
    if node.type in {"assignment_expression", "augmented_assignment_expression"} and node.children:
        return _this_field_from_lhs(node_text(data, node.children[0]).strip())
    if node.type == "update_expression":
        cleaned = node_text(data, node).strip().lstrip("+-").rstrip("+-").strip()
        return _this_field_from_lhs(cleaned)
    if node.type in {"call_expression", "method_invocation"} and node.children:
        return _this_field_from_call(node_text(data, node.children[0]).strip())
    return None


def _catch_rethrows(body_node) -> bool:
    if body_node is None:
        return False
    skip = _TS_SKIP_SCOPES | _JAVA_SKIP_SCOPES
    return any(d.type == "throw_statement" for d in scoped_descendants(body_node, skip))


def _java_catch_type_names(data: bytes, catch_node) -> set[str]:
    param = child_of_type(catch_node, "catch_formal_parameter")
    catch_type = child_of_type(param, "catch_type", "type_identifier") if param is not None else None
    if catch_type is None:
        return {"*"}
    return {_short_exc_name(p.strip()) for p in node_text(data, catch_type).split("|") if p.strip()}


def _collect_brace_catch_types(data: bytes, catch_node, grammar: str) -> set[str]:
    if _catch_rethrows(child_of_type(catch_node, "block", "statement_block")):
        return set()
    return _java_catch_type_names(data, catch_node) if grammar == "java" else {"*"}


def _visit_brace_contract_node(
    data: bytes,
    node,
    grammar: str,
    is_ctor: bool,
    reqs: list[tuple[str, str]],
    raises: list[tuple[str, str]],
    caught: set[str],
    muts: list[str],
) -> None:
    if node.type == "if_statement":
        guard = _extract_if_guard_pair(data, node)
        if guard:
            reqs.append(guard)
    elif node.type == "throw_statement":
        formatted = _format_brace_throw(data, node)
        if formatted:
            raises.append(formatted)
    elif node.type == "catch_clause":
        caught.update(_collect_brace_catch_types(data, node, grammar))
    elif not is_ctor:
        mut = _extract_this_mutation(data, node)
        if mut:
            muts.append(mut)


def _extract_brace_contract(body: str, file_path: str) -> StubContract:
    grammar = _grammar_for_path(file_path)
    data, fbody, is_ctor = _parse_brace_method_body(body, grammar)
    if fbody is None:
        return StubContract()
    skip = _JAVA_SKIP_SCOPES if grammar == "java" else _TS_SKIP_SCOPES
    reqs: list[tuple[str, str]] = []
    raises: list[tuple[str, str]] = []
    caught: set[str] = set()
    muts: list[str] = []

    for node in scoped_descendants(fbody, skip):
        _visit_brace_contract_node(data, node, grammar, is_ctor, reqs, raises, caught, muts)

    return StubContract(
        requires=_dedupe_pairs(reqs, 2),
        raises=_dedupe_pairs(raises, 3),
        caught_types=frozenset(caught),
        mutates=_dedupe_cap(muts, 3),
    )


def extract_direct_contract(body: str, file_path: str) -> StubContract:
    if not body:
        return StubContract()
    return _extract_py_contract(body) if file_path.endswith(".py") else _extract_brace_contract(body, file_path)


def _is_caught(exc_short: str, caught_types: frozenset[str]) -> bool:
    if not caught_types:
        return False
    if not exc_short or (caught_types & _BROAD_CATCH_NAMES):
        return True
    return exc_short in caught_types


def _append_callee_contracts(
    own: StubContract,
    callee_contract: StubContract,
    callee_short: str,
    req_lines: list[str],
    seen_reqs: set[str],
    raise_lines: list[str],
    seen_raises: set[str],
) -> None:
    for req_expr, req_exc in callee_contract.requires:
        if len(req_lines) < 2 and not _is_caught(req_exc, own.caught_types) and req_expr not in seen_reqs:
            seen_reqs.add(req_expr)
            req_lines.append(f"{req_expr} [via {callee_short}]")
    for expr, exc_short in callee_contract.raises:
        if len(raise_lines) < 3 and not _is_caught(exc_short, own.caught_types) and expr not in seen_raises:
            seen_raises.add(expr)
            raise_lines.append(f"{expr} [via {callee_short}]")


def _get_cached_contract(memo: dict[str, StubContract], sid: str, body: str, file_path: str) -> StubContract:
    if sid not in memo:
        memo[sid] = extract_direct_contract(body, file_path)
    return memo[sid]


def format_symbol_contract_lines(
    store,
    row,
    comment_pfx: str,
    indent: str = "    ",
    cache: dict[str, StubContract] | None = None,
) -> list[str]:
    sid = row["symbol_id"]
    memo = cache if cache is not None else {}
    own = _get_cached_contract(memo, sid, row["body"], row["file_path"])
    req_lines = [expr for expr, _ in own.requires]
    seen_reqs = set(req_lines)
    raise_lines = [expr for expr, _ in own.raises]
    seen_raises = set(raise_lines)

    callee_rows = store.rows(
        """SELECT DISTINCT s.symbol_id, s.qualname, s.file_path, s.body
           FROM symbols s JOIN edges e ON s.symbol_id = e.target_id
           WHERE e.source_id = ? AND e.kind = 'CALLS'
           ORDER BY e.line, s.symbol_id""",
        (sid,),
    )
    for crow in callee_rows:
        if crow["symbol_id"] == sid:
            continue
        ccontract = _get_cached_contract(memo, crow["symbol_id"], crow["body"], crow["file_path"])
        _append_callee_contracts(
            own, ccontract, crow["qualname"].rsplit(".", 1)[-1], req_lines, seen_reqs, raise_lines, seen_raises
        )

    lines = [f"{indent}{comment_pfx} @requires: {req}" for req in req_lines[:2]]
    lines.extend(f"{indent}{comment_pfx} @raises: {r_expr}" for r_expr in raise_lines[:3])
    if own.mutates:
        lines.append(f"{indent}{comment_pfx} @mutates: {', '.join(own.mutates)}")
    return lines
