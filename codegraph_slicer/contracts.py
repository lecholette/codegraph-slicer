"""Lightweight polyglot Hoare-contract stub extraction (@requires, @raises, @mutates)."""
from __future__ import annotations

import ast
import os
import textwrap
from dataclasses import dataclass
from pathlib import Path

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
_GRAMMAR_BY_EXT = {".java": "java", ".ts": "tsx", ".tsx": "tsx", ".jsx": "tsx"}
_ACTIVE_ENV_TOKENS = frozenset({"1", "true", "yes", "on"})
_MAX_LITERAL_LEN = 80


def is_contracts_enabled(flag: bool | None, env_val: str | None = None) -> bool:
    if flag is not None:
        return bool(flag)
    raw = os.environ.get("CODEGRAPH_CONTRACTS") if env_val is None else env_val
    if raw is None:
        return False
    return raw.strip().lower() in _ACTIVE_ENV_TOKENS


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


def _short_exc_name(expr_name: str) -> str:
    return expr_name.split("(")[0].strip().rsplit(".", 1)[-1]


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


def _append_uncaught_raise(
    raises: list[tuple[str, str]],
    formatted: tuple[str, str] | None,
    active_caught: frozenset[str],
) -> None:
    if formatted and not StubContract(caught_types=active_caught).catches(formatted[1]):
        raises.append(formatted)


@dataclass(frozen=True, slots=True)
class StubContract:
    requires: tuple[tuple[str, str], ...] = ()
    raises: tuple[tuple[str, str], ...] = ()
    caught_types: frozenset[str] = frozenset()
    mutates: tuple[str, ...] = ()

    def catches(self, exc_short: str) -> bool:
        if not self.caught_types:
            return False
        if not exc_short or (self.caught_types & _BROAD_CATCH_NAMES):
            return True
        return exc_short in self.caught_types

    def _merge_callee_into(
        self,
        callee_short: str,
        callee: StubContract,
        req_lines: list[str],
        seen_reqs: set[str],
        raise_lines: list[str],
        seen_raises: set[str],
    ) -> None:
        for req_expr, req_exc in callee.requires:
            if len(req_lines) < 2 and not self.catches(req_exc) and req_expr not in seen_reqs:
                seen_reqs.add(req_expr)
                req_lines.append(f"{req_expr} [via {callee_short}]")
        for expr, exc_short in callee.raises:
            if len(raise_lines) < 3 and not self.catches(exc_short) and expr not in seen_raises:
                seen_raises.add(expr)
                raise_lines.append(f"{expr} [via {callee_short}]")

    def render_with_callees(
        self,
        callees: list[tuple[str, StubContract]],
        comment_pfx: str,
        indent: str = "    ",
    ) -> list[str]:
        req_lines = [expr for expr, _ in self.requires]
        seen_reqs = set(req_lines)
        raise_lines = [expr for expr, _ in self.raises]
        seen_raises = set(raise_lines)
        for callee_short, ccontract in callees:
            self._merge_callee_into(callee_short, ccontract, req_lines, seen_reqs, raise_lines, seen_raises)

        lines = [f"{indent}{comment_pfx} @requires: {req}" for req in req_lines[:2]]
        lines.extend(f"{indent}{comment_pfx} @raises: {r_expr}" for r_expr in raise_lines[:3])
        if self.mutates:
            lines.append(f"{indent}{comment_pfx} @mutates: {', '.join(self.mutates)}")
        return lines


class _PyContractScanner:
    """Extracts @requires, @raises, caught types, and self.<field> @mutates from Python ASTs."""

    @staticmethod
    def _safe_unparse(node: ast.AST) -> str | None:
        try:
            return ast.unparse(node).strip()
        except (ValueError, RecursionError):
            return None

    @staticmethod
    def _format_literal_arg(node: ast.AST) -> str | None:
        if not isinstance(node, ast.Constant):
            return None
        if isinstance(node.value, str):
            return _escape_str_literal(node.value)
        if type(node.value) is int and abs(node.value).bit_length() <= 256:
            return repr(node.value)
        return repr(node.value) if isinstance(node.value, float) else None

    @classmethod
    def _format_raise(cls, exc: ast.AST) -> tuple[str, str] | None:
        if isinstance(exc, ast.Call):
            callee = cls._safe_unparse(exc.func)
            if not callee:
                return None
            lit = cls._format_literal_arg(exc.args[0]) if exc.args else None
            return (f"{callee}({lit})" if lit is not None else callee), _short_exc_name(callee)
        if isinstance(exc, (ast.Name, ast.Attribute)):
            callee = cls._safe_unparse(exc)
            return (callee, _short_exc_name(callee)) if callee else None
        return None

    @staticmethod
    def _single_raise_stmt(node: ast.AST) -> ast.Raise | None:
        if isinstance(node, ast.If) and not node.orelse and len(node.body) == 1 and isinstance(node.body[0], ast.Raise):
            return node.body[0]
        return None

    @classmethod
    def _node_precondition(cls, node: ast.AST) -> tuple[str, str] | None:
        if isinstance(node, ast.Assert):
            txt = cls._safe_unparse(node.test)
            return (txt, "AssertionError") if txt else None
        raise_stmt = cls._single_raise_stmt(node)
        cond = cls._safe_unparse(node.test) if raise_stmt is not None else None  # type: ignore[attr-defined]
        if not cond:
            return None
        exc_pair = cls._format_raise(raise_stmt.exc) if raise_stmt and raise_stmt.exc is not None else None
        return (f"not ({cond})", exc_pair[1] if exc_pair else "")

    @staticmethod
    def _self_attr(target: ast.AST) -> str | None:
        curr = target
        while isinstance(curr, ast.Subscript):
            curr = curr.value
        if isinstance(curr, ast.Attribute) and isinstance(curr.value, ast.Name) and curr.value.id == "self":
            return f"self.{curr.attr}"
        return None

    @classmethod
    def _node_mutations(cls, node: ast.AST) -> list[str]:
        if isinstance(node, ast.Assign):
            return [a for tgt in node.targets if (a := cls._self_attr(tgt))]
        if isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            attr = cls._self_attr(node.target)
            return [attr] if attr else []
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr in _PY_MUTATING_METHODS and (attr := cls._self_attr(node.func.value)):
                return [attr]
        return []

    @classmethod
    def _caught_types(cls, handler: ast.ExceptHandler) -> set[str]:
        if any(isinstance(n, ast.Raise) for n in ast.walk(handler)):
            return set()
        if handler.type is None:
            return {"*"}
        elts = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
        return {_short_exc_name(u) for elt in elts if (u := cls._safe_unparse(elt))}

    @classmethod
    def _leading_requires(cls, fn_node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[tuple[str, str]]:
        reqs: list[tuple[str, str]] = []
        for stmt in fn_node.body:
            if isinstance(stmt, ast.Expr) and isinstance(stmt.value, (ast.Constant, ast.Call)):
                continue
            pre = cls._node_precondition(stmt)
            if pre is None:
                break
            reqs.append(pre)
        return reqs

    @classmethod
    def _push_try_children(
        cls,
        stack: list[tuple[ast.AST, frozenset[str]]],
        node: ast.Try,
        active_caught: frozenset[str],
        all_caught: set[str],
    ) -> None:
        own: set[str] = set()
        for handler in node.handlers:
            own.update(cls._caught_types(handler))
        all_caught.update(own)
        inner_caught = active_caught | frozenset(own)
        for child in reversed([*node.finalbody, *node.orelse, *node.handlers]):
            stack.append((child, active_caught))
        for stmt in reversed(node.body):
            stack.append((stmt, inner_caught))

    @classmethod
    def _scan_body(
        cls, fn_node: ast.FunctionDef | ast.AsyncFunctionDef, is_ctor: bool
    ) -> tuple[list[tuple[str, str]], set[str], list[str]]:
        raises: list[tuple[str, str]] = []
        all_caught: set[str] = set()
        muts: list[str] = []
        stack: list[tuple[ast.AST, frozenset[str]]] = [(s, frozenset()) for s in reversed(fn_node.body)]
        while stack:
            node, active_caught = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if isinstance(node, ast.Try):
                cls._push_try_children(stack, node, active_caught, all_caught)
                continue
            if isinstance(node, ast.Raise) and node.exc is not None:
                _append_uncaught_raise(raises, cls._format_raise(node.exc), active_caught)
            elif not is_ctor:
                muts.extend(cls._node_mutations(node))
            for child in reversed(list(ast.iter_child_nodes(node))):
                stack.append((child, active_caught))
        return raises, all_caught, muts

    @classmethod
    def extract(cls, body: str) -> StubContract:
        try:
            tree = ast.parse(textwrap.dedent(body))
        except (SyntaxError, ValueError, RecursionError):
            return StubContract()
        fn_node = next((s for s in tree.body if isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
        if fn_node is None:
            return StubContract()

        reqs = cls._leading_requires(fn_node)
        is_ctor = fn_node.name in {"__init__", "__new__"}
        raises, caught, muts = cls._scan_body(fn_node, is_ctor)

        return StubContract(
            requires=_dedupe_pairs(reqs, 2),
            raises=_dedupe_pairs(raises, 3),
            caught_types=frozenset(caught),
            mutates=_dedupe_cap(muts, 3),
        )


class _BraceContractScanner:
    """Extracts @requires, @raises, caught types, and this.<field> @mutates for Java/TS/JS."""

    @staticmethod
    def _is_ctor_node(data: bytes, node, grammar: str) -> bool:
        if grammar == "java":
            return node.type == "constructor_declaration"
        if node.type != "method_definition":
            return False
        name_node = child_of_type(node, "identifier", "property_identifier")
        return bool(name_node and node_text(data, name_node) == "constructor")

    @classmethod
    def _parse_method_body(cls, body: str, grammar: str) -> tuple[bytes, object | None, bool]:
        parser = parser_for(grammar)
        data = body.encode("utf-8")
        root = parser.parse(data).root_node
        if root.has_error:
            data = f"class _ {{\n{body}\n}}".encode("utf-8")
            root = parser.parse(data).root_node
        block_type = "block" if grammar == "java" else "statement_block"
        is_ctor = False
        for node in scoped_descendants(root, set()):
            if cls._is_ctor_node(data, node, grammar):
                is_ctor = True
            if node.type == block_type:
                return data, node, is_ctor
        return data, None, False

    @staticmethod
    def _first_literal_arg(data: bytes, args_node) -> str | None:
        if args_node is None:
            return None
        lit_types = {"string_literal", "string", "decimal_integer_literal", "number"}
        for child in args_node.children:
            if child.type in {"(", ")", ","}:
                continue
            has_subst = any(c.type == "template_substitution" for c in child.children)
            if child.type not in lit_types or has_subst:
                return None
            raw = node_text(data, child).strip()
            if len(raw) >= 2 and raw[0] in {"'", '"'} and raw[-1] == raw[0]:
                return _escape_str_literal(raw[1:-1])
            return _sanitize_text(raw, _MAX_LITERAL_LEN)
        return None

    @classmethod
    def _format_throw(cls, data: bytes, throw_node) -> tuple[str, str] | None:
        new_expr = child_of_type(throw_node, "object_creation_expression", "new_expression")
        type_node = (
            child_of_type(
                new_expr, "type_identifier", "scoped_type_identifier", "generic_type", "identifier", "member_expression"
            )
            if new_expr is not None
            else None
        )
        if type_node is None:
            return None
        exc_type = node_text(data, type_node).split("<", 1)[0].strip()
        lit = cls._first_literal_arg(data, child_of_type(new_expr, "argument_list", "arguments"))
        return (f"{exc_type}({lit})" if lit is not None else exc_type), _short_exc_name(exc_type)

    @staticmethod
    def _consequence_after_cond(if_node, cond_node):
        seen_cond = False
        for child in if_node.children:
            if child.id == cond_node.id:
                seen_cond = True
            elif seen_cond and child.type not in _COMMENT_NODES:
                return child
        return None

    @classmethod
    def _single_throw_consequence(cls, if_node, cond_node):
        conseq = cls._consequence_after_cond(if_node, cond_node)
        if conseq is None:
            return None
        if conseq.type == "throw_statement":
            return conseq
        if conseq.type not in {"block", "statement_block"}:
            return None
        stmts = [c for c in conseq.children if c.type not in {"{", "}", *_COMMENT_NODES}]
        return stmts[0] if len(stmts) == 1 and stmts[0].type == "throw_statement" else None

    @classmethod
    def _if_guard_pair(cls, data: bytes, if_node) -> tuple[str, str] | None:
        if any(c.type in {"else", "else_clause"} for c in if_node.children):
            return None
        cond_node = child_of_type(if_node, "parenthesized_expression")
        throw_stmt = cls._single_throw_consequence(if_node, cond_node) if cond_node is not None else None
        if cond_node is None or throw_stmt is None:
            return None
        inner = node_text(data, cond_node).strip()
        if inner.startswith("(") and inner.endswith(")"):
            inner = inner[1:-1].strip()
        throw_pair = cls._format_throw(data, throw_stmt)
        return (f"not ({inner})", throw_pair[1] if throw_pair else "")

    @staticmethod
    def _this_lhs(lhs: str) -> str | None:
        if not lhs.startswith("this."):
            return None
        attr = lhs[5:].split(".", 1)[0].split("[", 1)[0].strip()
        return f"this.{attr}" if attr.isidentifier() else None

    @staticmethod
    def _this_call_field(head: str) -> str | None:
        parts = head.split(".")
        if len(parts) >= 3 and parts[0] == "this" and parts[1].isidentifier() and parts[2] in _BRACE_MUTATING_METHODS:
            return f"this.{parts[1]}"
        return None

    @classmethod
    def _this_mutation(cls, data: bytes, node) -> str | None:
        if node.type in {"assignment_expression", "augmented_assignment_expression"} and node.children:
            return cls._this_lhs(node_text(data, node.children[0]).strip())
        if node.type == "update_expression":
            return cls._this_lhs(node_text(data, node).strip().lstrip("+-").rstrip("+-").strip())
        if node.type in {"call_expression", "method_invocation"}:
            head = node_text(data, node).split("(", 1)[0].split("<", 1)[0].strip()
            return cls._this_call_field(head)
        return None

    @staticmethod
    def _catch_types(data: bytes, catch_node, grammar: str) -> set[str]:
        body_node = child_of_type(catch_node, "block", "statement_block")
        skip = _TS_SKIP_SCOPES | _JAVA_SKIP_SCOPES
        if body_node is not None and any(d.type == "throw_statement" for d in scoped_descendants(body_node, skip)):
            return set()
        if grammar != "java":
            return {"*"}
        param = child_of_type(catch_node, "catch_formal_parameter")
        catch_type = child_of_type(param, "catch_type", "type_identifier") if param is not None else None
        if catch_type is None:
            return {"*"}
        return {_short_exc_name(p.strip()) for p in node_text(data, catch_type).split("|") if p.strip()}

    @staticmethod
    def _is_pure_call_stmt(node) -> bool:
        if node.type != "expression_statement":
            return False
        exprs = [c for c in node.children if c.type not in {";", *_COMMENT_NODES}]
        return len(exprs) == 1 and exprs[0].type in {"call_expression", "method_invocation"}

    @classmethod
    def _leading_requires(cls, data: bytes, fbody) -> list[tuple[str, str]]:
        reqs: list[tuple[str, str]] = []
        for child in fbody.children:
            if child.type in {"{", "}", ";", *_COMMENT_NODES} or cls._is_pure_call_stmt(child):
                continue
            guard = cls._if_guard_pair(data, child) if child.type == "if_statement" else None
            if guard is None:
                break
            reqs.append(guard)
        return reqs

    @classmethod
    def _push_brace_try(
        cls,
        data: bytes,
        node,
        grammar: str,
        active_caught: frozenset[str],
        all_caught: set[str],
        stack: list[tuple[object, frozenset[str]]],
    ) -> None:
        own: set[str] = set()
        for child in node.children:
            if child.type == "catch_clause":
                own.update(cls._catch_types(data, child, grammar))
        all_caught.update(own)
        try_body = child_of_type(node, "block", "statement_block")
        inner_caught = active_caught | frozenset(own)
        for child in reversed(node.children):
            c_set = inner_caught if (try_body is not None and child.id == try_body.id) else active_caught
            stack.append((child, c_set))

    @classmethod
    def _scan_brace_body(
        cls, data: bytes, fbody, grammar: str, skip: frozenset[str], is_ctor: bool
    ) -> tuple[list[tuple[str, str]], set[str], list[str]]:
        raises: list[tuple[str, str]] = []
        all_caught: set[str] = set()
        muts: list[str] = []
        stack: list[tuple[object, frozenset[str]]] = [(c, frozenset()) for c in reversed(fbody.children)]
        while stack:
            node, active_caught = stack.pop()
            if node.type in skip:  # type: ignore[attr-defined]
                continue
            if node.type == "try_statement":  # type: ignore[attr-defined]
                cls._push_brace_try(data, node, grammar, active_caught, all_caught, stack)
                continue
            if node.type == "throw_statement":  # type: ignore[attr-defined]
                _append_uncaught_raise(raises, cls._format_throw(data, node), active_caught)
            elif not is_ctor:
                if mut := cls._this_mutation(data, node):
                    muts.append(mut)
            for child in reversed(node.children):  # type: ignore[attr-defined]
                stack.append((child, active_caught))
        return raises, all_caught, muts

    @classmethod
    def extract(cls, body: str, file_path: str) -> StubContract:
        grammar = _GRAMMAR_BY_EXT.get(Path(file_path).suffix.lower(), "javascript")
        data, fbody, is_ctor = cls._parse_method_body(body, grammar)
        if fbody is None:
            return StubContract()
        skip = _JAVA_SKIP_SCOPES if grammar == "java" else _TS_SKIP_SCOPES
        reqs = cls._leading_requires(data, fbody)
        raises, caught, muts = cls._scan_brace_body(data, fbody, grammar, skip, is_ctor)

        return StubContract(
            requires=_dedupe_pairs(reqs, 2),
            raises=_dedupe_pairs(raises, 3),
            caught_types=frozenset(caught),
            mutates=_dedupe_cap(muts, 3),
        )


def extract_direct_contract(body: str, file_path: str) -> StubContract:
    if not body:
        return StubContract()
    if Path(file_path).suffix.lower() == ".py":
        return _PyContractScanner.extract(body)
    return _BraceContractScanner.extract(body, file_path)


def format_symbol_contract_lines(
    store,
    row,
    comment_pfx: str,
    indent: str = "    ",
    cache: dict[str, StubContract] | None = None,
) -> list[str]:
    sid = row["symbol_id"]
    memo = cache if cache is not None else {}
    own = memo.get(sid)
    if own is None:
        own = extract_direct_contract(row["body"], row["file_path"])
        memo[sid] = own

    callee_rows = store.rows(
        """SELECT DISTINCT s.symbol_id, s.qualname, s.file_path, s.body
           FROM symbols s JOIN edges e ON s.symbol_id = e.target_id
           WHERE e.source_id = ? AND e.kind = 'CALLS' AND s.kind IN ('function', 'method')
           ORDER BY e.line, s.symbol_id""",
        (sid,),
    )
    callees: list[tuple[str, StubContract]] = []
    for crow in callee_rows:
        csid = crow["symbol_id"]
        if csid == sid:
            continue
        ccontract = memo.get(csid)
        if ccontract is None:
            ccontract = extract_direct_contract(crow["body"], crow["file_path"])
            memo[csid] = ccontract
        callees.append((crow["qualname"].rsplit(".", 1)[-1], ccontract))

    return own.render_with_callees(callees, comment_pfx=comment_pfx, indent=indent)
