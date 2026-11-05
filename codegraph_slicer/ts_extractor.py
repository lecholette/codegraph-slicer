"""Static Tree-sitter extractor for TypeScript and TSX source files (.ts, .tsx)."""

import hashlib
import json
import re
from pathlib import PurePosixPath

from .ast_extractor import (
    MODULE_END,
    CodeGraphParseError,
    Edge,
    FileAnalysis,
    Symbol,
    is_test_path,
)
from .treesitter_support import (
    binary_logic,
    build_brace_skeleton,
    child_of_type,
    compute_complexity,
    descendants,
    end_line,
    node_text,
    parse_tree,
    scoped_descendants,
    signature_before_body,
    split_source_lines,
    start_line,
)

_TS_DECISIONS = {
    "if_statement",
    "for_statement",
    "for_in_statement",
    "while_statement",
    "do_statement",
    "catch_clause",
    "ternary_expression",
    "switch_case",
    "switch_default",
}
_TS_TYPE_NODES = {
    "class_declaration",
    "abstract_class_declaration",
    "interface_declaration",
    "enum_declaration",
}
_TS_FUNCTION_NODES = {
    "function_declaration",
    "method_definition",
    "arrow_function",
    "function_expression",
}
_TS_BUILTINS = {
    "string",
    "number",
    "boolean",
    "symbol",
    "bigint",
    "any",
    "unknown",
    "never",
    "void",
    "undefined",
    "null",
    "object",
    "Function",
    "Object",
    "String",
    "Number",
    "Boolean",
    "Symbol",
    "BigInt",
    "Error",
    "TypeError",
    "RangeError",
    "SyntaxError",
    "Promise",
    "Array",
    "ReadonlyArray",
    "Record",
    "Partial",
    "Required",
    "Readonly",
    "Pick",
    "Omit",
    "Exclude",
    "Extract",
    "NonNullable",
    "ReturnType",
    "Parameters",
    "InstanceType",
    "Awaited",
    "Map",
    "Set",
    "WeakMap",
    "WeakSet",
    "Date",
    "RegExp",
    "Buffer",
    "Uint8Array",
    "Int32Array",
    "Float64Array",
    "ArrayBuffer",
    "DataView",
    "TextEncoder",
    "TextDecoder",
    "URL",
    "URLSearchParams",
    "AbortController",
    "Intl",
    "Reflect",
    "React",
    "FC",
    "ReactNode",
    "JSX",
    "console",
    "Math",
    "JSON",
    "process",
    "globalThis",
}
_TS_GLOBAL_FUNCTIONS = {
    "describe",
    "it",
    "test",
    "beforeEach",
    "afterEach",
    "beforeAll",
    "afterAll",
    "expect",
    "require",
    "setTimeout",
    "clearTimeout",
    "setInterval",
    "clearInterval",
    "queueMicrotask",
    "structuredClone",
    "parseInt",
    "parseFloat",
    "isNaN",
    "isFinite",
    "encodeURIComponent",
    "decodeURIComponent",
    "encodeURI",
    "decodeURI",
    "atob",
    "btoa",
    "fetch",
}
_TS_BUILTIN_PROTOTYPE_METHODS = {
    "push",
    "pop",
    "shift",
    "unshift",
    "slice",
    "splice",
    "map",
    "filter",
    "reduce",
    "reduceRight",
    "forEach",
    "some",
    "every",
    "find",
    "findIndex",
    "includes",
    "indexOf",
    "lastIndexOf",
    "join",
    "split",
    "sort",
    "reverse",
    "flat",
    "flatMap",
    "at",
    "fill",
    "concat",
    "entries",
    "keys",
    "values",
    "trim",
    "trimStart",
    "trimEnd",
    "startsWith",
    "endsWith",
    "replace",
    "replaceAll",
    "toLowerCase",
    "toUpperCase",
    "padStart",
    "padEnd",
    "charAt",
    "charCodeAt",
    "codePointAt",
    "substring",
    "match",
    "matchAll",
    "search",
    "localeCompare",
    "normalize",
    "repeat",
    "toString",
    "valueOf",
    "toFixed",
    "toISOString",
    "getTime",
    "getFullYear",
    "getUTCFullYear",
    "getMonth",
    "getUTCMonth",
    "getDate",
    "getUTCDate",
    "getDay",
    "getUTCDay",
    "getHours",
    "getUTCHours",
    "getMinutes",
    "getUTCMinutes",
    "getSeconds",
    "getUTCSeconds",
    "getMilliseconds",
    "getUTCMilliseconds",
    "set",
    "get",
    "has",
    "delete",
    "clear",
    "add",
    "test",
    "exec",
    "subarray",
    "write",
    "readUInt8",
    "readUInt32BE",
    "readUInt32LE",
    "writeUInt32BE",
    "writeUInt32LE",
    "update",
    "digest",
    "format",
    "formatToParts",
    "then",
    "catch",
    "finally",
}
_STRIP_EXTS = (".tsx", ".mts", ".cts", ".jsx", ".mjs", ".cjs", ".ts", ".js")


def _is_ts_decision(node) -> bool:
    if node.type in _TS_DECISIONS or binary_logic(node, ("&&", "||", "??")):
        return True
    if node.type == "optional_chain":
        return True
    return node.type == "?." and (
        node.parent is None or node.parent.type != "optional_chain"
    )


def _skip_ts_nested(node) -> bool:
    return node.type in _TS_TYPE_NODES or node.type in {
        "function_declaration",
        "method_definition",
    }


def _strip_ext(path_str: str) -> str:
    for ext in _STRIP_EXTS:
        if path_str.endswith(ext):
            return path_str[: -len(ext)]
    return path_str


def _strip_ts_source_root(path: str, source_root: str) -> str:
    rel = path.removeprefix(source_root)
    if rel.startswith("src/"):
        return rel[4:]
    return rel


def ts_module_name(path: str, source_root: str = "") -> str:
    rel = _strip_ext(_strip_ts_source_root(path, source_root))
    dotted = rel.replace("/", ".")
    if dotted != "index" and dotted.endswith(".index"):
        return dotted[: -len(".index")]
    return dotted


def parse_tsconfig_paths(raw: bytes) -> dict[str, str]:
    try:
        text = raw.decode("utf-8")
        cleaned = re.sub(
            r'"(?:\\.|[^"\\])*"|(//[^\n]*|/\*[\s\S]*?\*/)',
            lambda m: "" if m.group(1) else m.group(0),
            text,
        )
        cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
        data = json.loads(cleaned)
        compiler = data.get("compilerOptions") or {}
        base_url = (compiler.get("baseUrl") or "").strip().strip("/").strip(".")
        paths = compiler.get("paths") or {}
        result: dict[str, str] = {}
        for pattern, targets in paths.items():
            if isinstance(targets, list) and targets and isinstance(targets[0], str):
                target = targets[0].lstrip("./")
                if base_url and not target.startswith(base_url + "/"):
                    target = f"{base_url}/{target}"
                result[str(pattern)] = target
        return result
    except Exception:
        return {}


def _resolve_ts_import(
    spec: str,
    current_path: str,
    source_root: str,
    path_aliases: dict[str, str],
) -> str:
    cleaned = _strip_ext(spec.strip())
    for prefix, replacement in path_aliases.items():
        if prefix.endswith("/*") and cleaned.startswith(prefix[:-1]):
            rest = cleaned[len(prefix) - 1 :]
            target_prefix = replacement[:-1] if replacement.endswith("/*") else replacement
            cleaned = f"{target_prefix.rstrip('/')}/{rest.lstrip('/')}"
            cleaned = _strip_ts_source_root(cleaned, source_root)
            break
        elif cleaned == prefix:
            cleaned = _strip_ts_source_root(replacement, source_root)
            break
    else:
        if cleaned.startswith("@/"):
            cleaned = cleaned[2:]
        elif cleaned.startswith("."):
            rel_file = current_path.removeprefix(source_root)
            parent_parts = list(PurePosixPath(rel_file).parent.parts)
            if parent_parts == ["."]:
                parent_parts = []
            for part in cleaned.split("/"):
                if not part or part == ".":
                    continue
                if part == "..":
                    if not parent_parts:
                        raise CodeGraphParseError(
                            f"relative import escapes root: {current_path}"
                        )
                    parent_parts.pop()
                else:
                    parent_parts.append(part)
            cleaned = _strip_ts_source_root("/".join(parent_parts), "")
    dotted = cleaned.replace("/", ".")
    if dotted != "index" and dotted.endswith(".index"):
        dotted = dotted[: -len(".index")]
    return dotted


def _string_literal_value(data: bytes, node) -> str | None:
    str_node = child_of_type(node, "string")
    if str_node is None:
        return None
    fragments = [
        node_text(data, c) for c in str_node.children if c.type == "string_fragment"
    ]
    if fragments:
        return "".join(fragments)
    raw = node_text(data, str_node)
    if len(raw) >= 2 and raw[0] in {"'", '"', "`"} and raw[-1] == raw[0]:
        return raw[1:-1]
    return None


def _base_ts_type_name(data: bytes, node) -> str | None:
    if node is None:
        return None
    if node.type == "type_annotation":
        for child in node.children:
            if child.type != ":":
                return _base_ts_type_name(data, child)
        return None
    if node.type == "generic_type":
        ident = child_of_type(node, "type_identifier", "nested_type_identifier", "identifier")
        if ident is not None:
            name = node_text(data, ident)
            if name in {"Promise", "Array", "ReadonlyArray", "Partial", "Required", "Readonly"}:
                targs = child_of_type(node, "type_arguments")
                if targs is not None:
                    for arg in targs.children:
                        if arg.type not in {"<", ">", ","}:
                            return _base_ts_type_name(data, arg)
            return name
    if node.type in {"type_identifier", "nested_type_identifier", "identifier"}:
        return node_text(data, node)
    return None


def _all_ts_type_identifiers(data: bytes, node) -> list[tuple[str, int]]:
    if node is None:
        return []
    found: list[tuple[str, int]] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type in {"type_identifier", "nested_type_identifier"}:
            found.append((node_text(data, current), start_line(current)))
            continue
        stack.extend(reversed(current.children))
    return found


def extract_typescript_file(
    path: str,
    source: bytes,
    source_root: str = "",
    path_aliases: dict[str, str] | None = None,
) -> FileAnalysis:
    grammar = "tsx" if path.endswith(".tsx") else "typescript"
    text, data, root = parse_tree(source, path, grammar)
    module = ts_module_name(path, source_root)
    lines = split_source_lines(text)
    file_is_test = is_test_path(path)
    aliases_cfg = path_aliases or {}

    symbols: list[Symbol] = []
    symbol_index: dict[str, int] = {}
    edges: list[Edge] = []
    block_nodes: list = []
    bindings: dict[str, str] = {}
    deferred_local_exports: list[tuple[str, str]] = []

    def add_or_merge_symbol(sym: Symbol, prefer_new_sig: bool = True):
        idx = symbol_index.get(sym.symbol_id)
        if idx is None:
            symbol_index[sym.symbol_id] = len(symbols)
            symbols.append(sym)
            return
        prev = symbols[idx]
        s_line = min(prev.start, sym.start)
        e_line = max(prev.end, sym.end)
        sig = sym.signature if prefer_new_sig else prev.signature
        cx = max(prev.cx, sym.cx)
        symbols[idx] = Symbol(
            sym.symbol_id,
            sym.kind,
            sig,
            "\n".join(lines[s_line - 1 : e_line]),
            s_line,
            e_line,
            cx,
        )

    def emit(owner_sid: str, target: str, kind: str, line: int):
        edges.append(Edge(owner_sid, target, kind, line))
        if file_is_test and kind in {"CALLS", "IMPORTS"}:
            edges.append(Edge(owner_sid, target, "TESTS", line))

    def resolve_ref(name: str, local_types: dict[str, str] | None = None) -> str:
        if local_types and name in local_types:
            ltype = local_types[name]
            if ltype and ltype != "?":
                return resolve_ref(ltype)
            return f"?{name}"
        if "." in name:
            head, _, tail = name.partition(".")
            if local_types and head in local_types:
                ltype = local_types[head]
                if ltype and ltype != "?":
                    return f"{resolve_ref(ltype)}.{tail}"
                return f"?{name}"
            if head in bindings:
                base = bindings[head]
                if base.endswith(MODULE_END):
                    return f"{base}{tail}"
                return f"{base}.{tail}"
            return f"?{name}"
        if name in bindings:
            return bindings[name]
        return f"?{name}"

    def emit_type_uses(owner_sid: str, node, ignore_type_params: set[str] = frozenset()):
        for tname, line in _all_ts_type_identifiers(data, node):
            base = tname.split(".")[0]
            if (
                tname in _TS_BUILTINS
                or base in _TS_BUILTINS
                or tname in ignore_type_params
                or (len(tname) == 1 and tname.isupper())
            ):
                continue
            emit(owner_sid, resolve_ref(tname), "USES_TYPE", line)

    def _collect_top_level_bindings(node, is_default_export: bool = False):
        if node.type == "import_statement":
            line = start_line(node)
            spec = _string_literal_value(data, node)
            if not spec:
                return
            mod_target = _resolve_ts_import(spec, path, source_root, aliases_cfg)
            emit(f"{module}:", f"{mod_target}{MODULE_END}", "IMPORTS", line)
            clause = child_of_type(node, "import_clause")
            if clause is None:
                return
            for child in clause.children:
                if child.type == "identifier":
                    local_name = node_text(data, child)
                    target = f"{mod_target}{MODULE_END}default"
                    bindings[local_name] = target
                    emit(f"{module}:", target, "IMPORTS", line)
                elif child.type == "namespace_import":
                    ns_ident = child_of_type(child, "identifier")
                    if ns_ident is not None:
                        bindings[node_text(data, ns_ident)] = f"{mod_target}{MODULE_END}"
                elif child.type == "named_imports":
                    for spec_node in child.children:
                        if spec_node.type == "import_specifier":
                            idents = [
                                c
                                for c in spec_node.children
                                if c.type in {"identifier", "type_identifier"}
                            ]
                            if idents:
                                orig = node_text(data, idents[0])
                                local = node_text(data, idents[-1])
                                target = f"{mod_target}{MODULE_END}{orig}"
                                bindings[local] = target
                                emit(f"{module}:", target, "IMPORTS", line)
        elif node.type == "export_statement":
            line = start_line(node)
            spec = _string_literal_value(data, node)
            if spec is not None:
                mod_target = _resolve_ts_import(spec, path, source_root, aliases_cfg)
                emit(f"{module}:", f"{mod_target}{MODULE_END}", "IMPORTS", line)
                eclause = child_of_type(node, "export_clause")
                ns_export = child_of_type(node, "namespace_export")
                if eclause is not None:
                    for spec_node in eclause.children:
                        if spec_node.type == "export_specifier":
                            idents = [
                                c
                                for c in spec_node.children
                                if c.type in {"identifier", "type_identifier"}
                            ]
                            if idents:
                                orig = node_text(data, idents[0])
                                exported = node_text(data, idents[-1])
                                target = f"{mod_target}{MODULE_END}{orig}"
                                bindings[exported] = target
                                emit(f"{module}:", target, "IMPORTS", line)
                elif ns_export is not None:
                    ns_ident = child_of_type(ns_export, "identifier", "type_identifier")
                    if ns_ident is not None:
                        bindings[node_text(data, ns_ident)] = f"{mod_target}{MODULE_END}"
                elif any(c.type == "*" for c in node.children):
                    edges.append(Edge(module, mod_target, "WILDCARD_EXPORT", line))
                return
            eclause = child_of_type(node, "export_clause")
            if eclause is not None:
                for spec_node in eclause.children:
                    if spec_node.type == "export_specifier":
                        idents = [
                            c
                            for c in spec_node.children
                            if c.type in {"identifier", "type_identifier"}
                        ]
                        if idents:
                            orig = node_text(data, idents[0])
                            exported = node_text(data, idents[-1])
                            deferred_local_exports.append((orig, exported))
                return
            is_def = any(c.type == "default" for c in node.children)
            for child in node.children:
                if child.type in {
                    *_TS_TYPE_NODES,
                    "type_alias_declaration",
                    "function_declaration",
                    "function_signature",
                    "arrow_function",
                    "function_expression",
                    "lexical_declaration",
                    "variable_declaration",
                    "identifier",
                }:
                    _collect_top_level_bindings(child, is_default_export=is_def)
        elif node.type in {
            *_TS_TYPE_NODES,
            "type_alias_declaration",
            "function_declaration",
            "function_signature",
        }:
            ident = child_of_type(node, "type_identifier", "identifier")
            if ident is not None:
                name = node_text(data, ident)
                sid = f"{module}:{name}"
                bindings[name] = sid
                if is_default_export:
                    bindings["default"] = sid
            elif is_default_export:
                bindings["default"] = f"{module}:default"
        elif node.type in {"arrow_function", "function_expression"} and is_default_export:
            bindings["default"] = f"{module}:default"
        elif node.type in {"lexical_declaration", "variable_declaration"}:
            for decl in node.children:
                if decl.type == "variable_declarator":
                    ident = child_of_type(decl, "identifier")
                    val = child_of_type(decl, "arrow_function", "function_expression")
                    if ident is not None and val is not None:
                        name = node_text(data, ident)
                        sid = f"{module}:{name}"
                        bindings[name] = sid
                        if is_default_export:
                            bindings["default"] = sid
        elif node.type == "identifier" and is_default_export:
            name = node_text(data, node)
            deferred_local_exports.append((name, "default"))

    for child in root.children:
        _collect_top_level_bindings(child)

    for orig, exported in deferred_local_exports:
        if orig in bindings:
            bindings[exported] = bindings[orig]
        else:
            bindings[exported] = f"{module}:{orig}"

    for name, target in bindings.items():
        edges.append(Edge(f"{module}.{name}", target, "ALIAS", 0))

    def _type_params_set(node) -> set[str]:
        tparams = child_of_type(node, "type_parameters")
        if tparams is None:
            return set()
        return {
            node_text(data, t)
            for t in descendants(tparams)
            if t.type == "type_identifier"
        }

    def _scan_body_expressions(
        body_node,
        owner_sid: str,
        class_sid: str | None,
        field_types: dict[str, str],
        local_types: dict[str, str],
        superclass_target: str | None,
        tparams: set[str],
    ):
        if body_node is None:
            return
        for desc in scoped_descendants(
            body_node,
            {*_TS_TYPE_NODES, "function_declaration", "method_definition"},
        ):
            if desc.type in {"lexical_declaration", "variable_declaration"}:
                for decl in desc.children:
                    if decl.type != "variable_declarator":
                        continue
                    vident = child_of_type(decl, "identifier")
                    tann = child_of_type(decl, "type_annotation")
                    emit_type_uses(owner_sid, tann, tparams)
                    fn_init = child_of_type(decl, "arrow_function", "function_expression")
                    if vident is not None and fn_init is not None:
                        local_types[node_text(data, vident)] = "__closure__"
                        continue
                    base_t = _base_ts_type_name(data, tann)
                    new_expr = child_of_type(decl, "new_expression")
                    if base_t is None and new_expr is not None:
                        ctor = child_of_type(
                            new_expr, "identifier", "type_identifier", "member_expression"
                        )
                        if ctor is not None:
                            base_t = node_text(data, ctor)
                    if vident is not None:
                        local_types[node_text(data, vident)] = base_t or "?"
            elif desc.type == "new_expression":
                ctor = child_of_type(
                    desc, "identifier", "type_identifier", "member_expression"
                )
                if ctor is not None:
                    cname = node_text(data, ctor)
                    if cname not in _TS_BUILTINS and cname.split(".", 1)[0] not in _TS_BUILTINS:
                        emit(owner_sid, resolve_ref(cname, local_types), "CALLS", start_line(desc))
                targs = child_of_type(desc, "type_arguments")
                emit_type_uses(owner_sid, targs, tparams)
            elif desc.type in {"jsx_opening_element", "jsx_self_closing_element"}:
                tag = child_of_type(desc, "identifier", "member_expression")
                if tag is not None:
                    tname = node_text(data, tag)
                    if tname and tname[0].isupper() and tname.split(".")[0] not in _TS_BUILTINS:
                        emit(owner_sid, resolve_ref(tname, local_types), "CALLS", start_line(desc))
            elif desc.type == "call_expression":
                _handle_ts_call(
                    desc,
                    owner_sid,
                    class_sid,
                    field_types,
                    local_types,
                    superclass_target,
                    tparams,
                )

    def _handle_ts_call(
        call_node,
        owner_sid: str,
        class_sid: str | None,
        field_types: dict[str, str],
        local_types: dict[str, str],
        superclass_target: str | None,
        tparams: set[str],
    ):
        line = start_line(call_node)
        targs = child_of_type(call_node, "type_arguments")
        emit_type_uses(owner_sid, targs, tparams)
        callee = None
        for child in call_node.children:
            if child.type not in {"arguments", "type_arguments", "optional_chain"}:
                callee = child
                break
        if callee is None:
            return
        if callee.type == "super":
            if superclass_target:
                emit(owner_sid, superclass_target, "CALLS", line)
            return
        if callee.type == "identifier":
            fn_name = node_text(data, callee)
            if (
                fn_name in _TS_BUILTINS
                or fn_name in _TS_GLOBAL_FUNCTIONS
                or local_types.get(fn_name) == "__closure__"
            ):
                return
            emit(owner_sid, resolve_ref(fn_name, local_types), "CALLS", line)
            return
        if callee.type == "member_expression":
            prop = child_of_type(callee, "property_identifier", "private_property_identifier")
            if prop is None:
                return
            mname = node_text(data, prop)
            obj = callee.children[0] if callee.children else None
            if obj is None:
                return
            if obj.type == "this":
                if mname in field_types:
                    ftype = field_types[mname]
                    if ftype and ftype not in _TS_BUILTINS:
                        emit(owner_sid, resolve_ref(ftype), "CALLS", line)
                        return
                if class_sid:
                    emit(owner_sid, f"{class_sid}.{mname}", "CALLS", line)
                return
            if obj.type == "super":
                if superclass_target:
                    emit(owner_sid, f"{superclass_target}.{mname}", "CALLS", line)
                return
            if obj.type == "member_expression":
                inner_obj = obj.children[0] if obj.children else None
                inner_prop = child_of_type(obj, "property_identifier", "private_property_identifier")
                if inner_obj is not None and inner_obj.type == "this" and inner_prop is not None:
                    fname = node_text(data, inner_prop)
                    ftype = field_types.get(fname)
                    if ftype and ftype not in _TS_BUILTINS:
                        emit(owner_sid, f"{resolve_ref(ftype)}.{mname}", "CALLS", line)
                        return
                    if mname not in _TS_BUILTIN_PROTOTYPE_METHODS:
                        emit(owner_sid, f"?this.{fname}.{mname}", "CALLS", line)
                    return
            if obj.type == "identifier":
                rname = node_text(data, obj)
                if rname in _TS_BUILTINS or rname in {"expect", "vi", "jest", "assert"}:
                    return
                if rname in local_types:
                    ltype = local_types[rname]
                    if ltype == "__closure__":
                        return
                    if ltype and ltype != "?" and ltype not in _TS_BUILTINS:
                        emit(owner_sid, f"{resolve_ref(ltype)}.{mname}", "CALLS", line)
                        return
                    if mname not in _TS_BUILTIN_PROTOTYPE_METHODS and ltype not in _TS_BUILTINS:
                        emit(owner_sid, f"?{rname}.{mname}", "CALLS", line)
                    return
                if rname in bindings:
                    base = bindings[rname]
                    if base.endswith(MODULE_END):
                        emit(owner_sid, f"{base}{mname}", "CALLS", line)
                    else:
                        emit(owner_sid, f"{base}.{mname}", "CALLS", line)
                    return
                if mname not in _TS_BUILTIN_PROTOTYPE_METHODS:
                    emit(owner_sid, f"?{rname}.{mname}", "CALLS", line)
                return
            raw_obj = node_text(data, obj).strip()
            root_ident = re.split(r"[.(?\[]", raw_obj, maxsplit=1)[0].strip()
            if (
                not raw_obj.startswith(("console.", "expect("))
                and root_ident not in _TS_BUILTINS
                and mname not in _TS_BUILTIN_PROTOTYPE_METHODS
            ):
                emit(owner_sid, f"?{raw_obj}.{mname}", "CALLS", line)

    def _extract_params(params_node, owner_sid: str, tparams: set[str], field_types: dict[str, str] | None = None) -> dict[str, str]:
        local_types: dict[str, str] = {}
        if params_node is None:
            return local_types
        if params_node.type == "identifier":
            local_types[node_text(data, params_node)] = "?"
            return local_types
        for param in params_node.children:
            if param.type in {"required_parameter", "optional_parameter", "rest_pattern"}:
                tann = child_of_type(param, "type_annotation")
                emit_type_uses(owner_sid, tann, tparams)
                base_t = _base_ts_type_name(data, tann)
                pident = child_of_type(param, "identifier")
                if pident is not None:
                    pname = node_text(data, pident)
                    local_types[pname] = base_t or "?"
                    is_prop = any(
                        c.type in {"accessibility_modifier", "readonly", "override_modifier"}
                        for c in param.children
                    )
                    if is_prop and field_types is not None and base_t is not None:
                        field_types[pname] = base_t
        return local_types

    def visit_class_or_interface(node, outer_node=None):
        ident = child_of_type(node, "type_identifier", "identifier")
        is_def = outer_node is not None and any(c.type == "default" for c in outer_node.children)
        if ident is None and not is_def:
            return
        cname = node_text(data, ident) if ident is not None else "default"
        sid = f"{module}:{cname}"
        decl_node = outer_node or node
        body_node = child_of_type(node, "class_body", "interface_body", "enum_body")
        s_line = start_line(decl_node)
        e_line = end_line(decl_node)
        sig = signature_before_body(data, decl_node, body_node)
        add_or_merge_symbol(
            Symbol(
                sid,
                "class",
                sig,
                "\n".join(lines[s_line - 1 : e_line]),
                s_line,
                e_line,
                1,
            )
        )
        tparams = _type_params_set(node)
        superclass_target: str | None = None

        heritage = child_of_type(node, "class_heritage")
        if heritage is not None:
            for clause in heritage.children:
                if clause.type == "extends_clause":
                    for child in clause.children:
                        if child.type not in {"extends", ",", "type_arguments"}:
                            bname = _base_ts_type_name(data, child)
                            if bname and bname not in _TS_BUILTINS:
                                superclass_target = resolve_ref(bname)
                                emit(sid, superclass_target, "INHERITS", start_line(child))
                        elif child.type == "type_arguments":
                            emit_type_uses(sid, child, tparams)
                elif clause.type == "implements_clause":
                    for child in clause.children:
                        if child.type not in {"implements", ","}:
                            iname = _base_ts_type_name(data, child)
                            if iname and iname not in _TS_BUILTINS:
                                emit(sid, resolve_ref(iname), "INHERITS", start_line(child))
                            if child.type == "generic_type":
                                emit_type_uses(sid, child_of_type(child, "type_arguments"), tparams)

        ext_iface = child_of_type(node, "extends_type_clause")
        if ext_iface is not None:
            for child in ext_iface.children:
                if child.type not in {"extends", ","}:
                    iname = _base_ts_type_name(data, child)
                    if iname and iname not in _TS_BUILTINS:
                        emit(sid, resolve_ref(iname), "INHERITS", start_line(child))
                    if child.type == "generic_type":
                        emit_type_uses(sid, child_of_type(child, "type_arguments"), tparams)

        if body_node is None:
            return

        field_types: dict[str, str] = {}
        for member in body_node.children:
            if member.type in {"public_field_definition", "property_signature"}:
                pident = child_of_type(
                    member, "property_identifier", "private_property_identifier", "identifier"
                )
                tann = child_of_type(member, "type_annotation")
                emit_type_uses(sid, tann, tparams)
                base_t = _base_ts_type_name(data, tann)
                if pident is not None and base_t is not None:
                    field_types[node_text(data, pident)] = base_t
            elif member.type == "method_definition":
                mident = child_of_type(member, "property_identifier", "identifier")
                if mident is not None and node_text(data, mident) == "constructor":
                    params = child_of_type(member, "formal_parameters")
                    _extract_params(params, sid, tparams, field_types=field_types)

        for member in body_node.children:
            if member.type in {"method_definition", "method_signature"}:
                mident = child_of_type(
                    member, "property_identifier", "private_property_identifier", "identifier"
                )
                if mident is None:
                    continue
                mname = node_text(data, mident)
                msid = f"{sid}.{mname}"
                mbody = child_of_type(member, "statement_block")
                if mbody is not None:
                    block_nodes.append(mbody)
                ms_line = start_line(member)
                me_line = end_line(member)
                msig = signature_before_body(data, member, mbody)
                mcx = (
                    compute_complexity(member, _is_ts_decision, _skip_ts_nested)
                    if mbody is not None
                    else 1
                )
                add_or_merge_symbol(
                    Symbol(
                        msid,
                        "method",
                        msig,
                        "\n".join(lines[ms_line - 1 : me_line]),
                        ms_line,
                        me_line,
                        mcx,
                    ),
                    prefer_new_sig=(mbody is not None),
                )
                mtparams = tparams | _type_params_set(member)
                params = child_of_type(member, "formal_parameters")
                local_types = _extract_params(params, msid, mtparams)
                for child in member.children:
                    if child.type == "type_annotation":
                        emit_type_uses(msid, child, mtparams)
                _scan_body_expressions(
                    mbody,
                    msid,
                    sid,
                    field_types,
                    local_types,
                    superclass_target,
                    mtparams,
                )
            elif member.type == "public_field_definition":
                pident = child_of_type(
                    member, "property_identifier", "private_property_identifier", "identifier"
                )
                fn_val = child_of_type(member, "arrow_function", "function_expression")
                if pident is not None and fn_val is not None:
                    mname = node_text(data, pident)
                    msid = f"{sid}.{mname}"
                    mbody = child_of_type(fn_val, "statement_block")
                    if mbody is not None:
                        block_nodes.append(mbody)
                    ms_line = start_line(member)
                    me_line = end_line(member)
                    msig = signature_before_body(data, member, mbody)
                    mcx = compute_complexity(fn_val, _is_ts_decision, _skip_ts_nested)
                    add_or_merge_symbol(
                        Symbol(
                            msid,
                            "method",
                            msig,
                            "\n".join(lines[ms_line - 1 : me_line]),
                            ms_line,
                            me_line,
                            mcx,
                        )
                    )
                    mtparams = tparams | _type_params_set(fn_val)
                    params = child_of_type(fn_val, "formal_parameters") or child_of_type(
                        fn_val, "identifier"
                    )
                    local_types = _extract_params(params, msid, mtparams)
                    for child in fn_val.children:
                        if child.type == "type_annotation":
                            emit_type_uses(msid, child, mtparams)
                    _scan_body_expressions(
                        mbody or fn_val,
                        msid,
                        sid,
                        field_types,
                        local_types,
                        superclass_target,
                        mtparams,
                    )
                else:
                    _scan_body_expressions(
                        member, sid, sid, field_types, {}, superclass_target, tparams
                    )

    def visit_function(node, outer_node=None):
        ident = child_of_type(node, "identifier")
        is_def = outer_node is not None and any(c.type == "default" for c in outer_node.children)
        fbody = child_of_type(node, "statement_block")
        if ident is None and not is_def:
            return
        fname = node_text(data, ident) if ident is not None else "default"
        sid = f"{module}:{fname}"
        decl_node = outer_node or node
        if fbody is not None:
            block_nodes.append(fbody)
        s_line = start_line(decl_node)
        e_line = end_line(decl_node)
        sig = signature_before_body(data, decl_node, fbody)
        cx = (
            compute_complexity(node, _is_ts_decision, _skip_ts_nested)
            if fbody is not None
            else 1
        )
        add_or_merge_symbol(
            Symbol(
                sid,
                "function",
                sig,
                "\n".join(lines[s_line - 1 : e_line]),
                s_line,
                e_line,
                cx,
            ),
            prefer_new_sig=(fbody is not None),
        )
        tparams = _type_params_set(node)
        params = child_of_type(node, "formal_parameters") or (
            child_of_type(node, "identifier") if node.type == "arrow_function" else None
        )
        local_types = _extract_params(params, sid, tparams)
        for child in node.children:
            if child.type == "type_annotation":
                emit_type_uses(sid, child, tparams)
        _scan_body_expressions(
            fbody or (node if node.type in {"arrow_function", "function_expression"} else None),
            sid,
            None,
            {},
            local_types,
            None,
            tparams,
        )

    def visit_lexical_functions(node, outer_node=None):
        decl_root = outer_node or node
        for decl in node.children:
            if decl.type != "variable_declarator":
                continue
            ident = child_of_type(decl, "identifier")
            fn_val = child_of_type(decl, "arrow_function", "function_expression")
            if ident is None or fn_val is None:
                decl_tann = child_of_type(decl, "type_annotation")
                emit_type_uses(f"{module}:", decl_tann, set())
                _scan_body_expressions(decl, f"{module}:", None, {}, {}, None, set())
                continue
            fname = node_text(data, ident)
            sid = f"{module}:{fname}"
            fbody = child_of_type(fn_val, "statement_block")
            if fbody is not None:
                block_nodes.append(fbody)
            s_line = start_line(decl_root)
            e_line = end_line(decl)
            sig = signature_before_body(data, decl_root, fbody)
            cx = compute_complexity(fn_val, _is_ts_decision, _skip_ts_nested)
            add_or_merge_symbol(
                Symbol(
                    sid,
                    "function",
                    sig,
                    "\n".join(lines[s_line - 1 : e_line]),
                    s_line,
                    e_line,
                    cx,
                )
            )
            tparams = _type_params_set(fn_val)
            decl_tann = child_of_type(decl, "type_annotation")
            emit_type_uses(sid, decl_tann, tparams)
            params = child_of_type(fn_val, "formal_parameters") or child_of_type(
                fn_val, "identifier"
            )
            local_types = _extract_params(params, sid, tparams)
            for child in fn_val.children:
                if child.type == "type_annotation":
                    emit_type_uses(sid, child, tparams)
            _scan_body_expressions(
                fbody or fn_val, sid, None, {}, local_types, None, tparams
            )

    def visit_type_alias(node, outer_node=None):
        ident = child_of_type(node, "type_identifier", "identifier")
        if ident is None:
            return
        tname = node_text(data, ident)
        sid = f"{module}:{tname}"
        decl_node = outer_node or node
        s_line = start_line(decl_node)
        e_line = end_line(decl_node)
        body = "\n".join(lines[s_line - 1 : e_line])
        sig = " ".join(line.strip() for line in body.splitlines() if line.strip())
        add_or_merge_symbol(Symbol(sid, "type_alias", sig, body, s_line, e_line, 1))
        tparams = _type_params_set(node) | {tname}
        emit_type_uses(sid, node, tparams)

    for top in root.children:
        target_node = top
        outer = None
        if top.type == "export_statement":
            outer = top
            inner = child_of_type(
                top,
                *_TS_TYPE_NODES,
                "type_alias_declaration",
                "function_declaration",
                "function_signature",
                "arrow_function",
                "function_expression",
                "lexical_declaration",
                "variable_declaration",
            )
            if inner is None:
                continue
            target_node = inner
        if target_node.type in _TS_TYPE_NODES:
            visit_class_or_interface(target_node, outer)
        elif target_node.type in {
            "function_declaration",
            "function_signature",
            "arrow_function",
            "function_expression",
        }:
            visit_function(target_node, outer)
        elif target_node.type in {"lexical_declaration", "variable_declaration"}:
            visit_lexical_functions(target_node, outer)
        elif target_node.type == "type_alias_declaration":
            visit_type_alias(target_node, outer)
        elif target_node.type == "expression_statement":
            _scan_body_expressions(
                target_node, f"{module}:", None, {}, {}, None, set()
            )

    skeleton = build_brace_skeleton(data, block_nodes)
    symbols.insert(
        0, Symbol(f"{module}:", "module", module, text, 1, max(1, len(lines)), 1)
    )
    return FileAnalysis(
        path,
        module,
        hashlib.sha256(source).hexdigest(),
        skeleton,
        tuple(symbols),
        tuple(sorted(set(edges), key=lambda e: (e.source, e.target, e.kind, e.line))),
    )
