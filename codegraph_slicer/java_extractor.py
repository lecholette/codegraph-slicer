"""Static Tree-sitter extractor for Java source files (.java)."""

import hashlib
from pathlib import PurePosixPath

from .ast_extractor import MODULE_END, Edge, FileAnalysis, Symbol, is_test_path
from .treesitter_support import (
    binary_logic,
    build_brace_skeleton,
    child_of_type,
    compute_complexity,
    descendants,
    end_line,
    node_text,
    parse_tree,
    signature_before_body,
    start_line,
)

_JAVA_TYPE_NODES = {
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
    "annotation_type_declaration",
}
_JAVA_DECISIONS = {
    "if_statement",
    "for_statement",
    "enhanced_for_statement",
    "while_statement",
    "do_statement",
    "catch_clause",
    "ternary_expression",
    "switch_label",
}
_JAVA_BUILTINS = {
    "boolean",
    "byte",
    "char",
    "short",
    "int",
    "long",
    "float",
    "double",
    "void",
    "var",
    "String",
    "Object",
    "Integer",
    "Long",
    "Double",
    "Float",
    "Boolean",
    "Byte",
    "Short",
    "Character",
    "Void",
    "Number",
    "CharSequence",
    "Throwable",
    "Exception",
    "RuntimeException",
    "Error",
    "IllegalArgumentException",
    "IllegalStateException",
    "NullPointerException",
    "UnsupportedOperationException",
    "IndexOutOfBoundsException",
    "Class",
    "System",
    "Math",
    "Thread",
    "Runnable",
    "Comparable",
    "Iterable",
    "Override",
    "Deprecated",
    "SuppressWarnings",
    "FunctionalInterface",
    "SafeVarargs",
    "List",
    "ArrayList",
    "LinkedList",
    "Map",
    "HashMap",
    "LinkedHashMap",
    "TreeMap",
    "Set",
    "HashSet",
    "TreeSet",
    "Collection",
    "Collections",
    "Optional",
    "Stream",
    "Arrays",
    "Objects",
}
_TEST_ANNOTATIONS = {"Test", "ParameterizedTest", "RepeatedTest", "TestFactory"}


def _is_java_decision(node) -> bool:
    return node.type in _JAVA_DECISIONS or binary_logic(node)


def _skip_java_nested(node) -> bool:
    return node.type in _JAVA_TYPE_NODES or node.type in {
        "class_body",
        "interface_body",
        "enum_body",
        "lambda_expression",
    }


def _strip_java_source_root(path: str, source_root: str) -> str:
    rel = path.removeprefix(source_root)
    for prefix in ("src/main/java/", "src/test/java/", "src/"):
        if rel.startswith(prefix):
            return rel[len(prefix) :]
        marker = "/" + prefix
        if marker in rel:
            return rel.split(marker, 1)[1]
    return rel


def java_module_name(path: str, package: str | None, source_root: str = "") -> str:
    stem = PurePosixPath(path).stem
    if package:
        return f"{package}.{stem}"
    rel = _strip_java_source_root(path, source_root)
    if rel.endswith(".java"):
        rel = rel[:-5]
    return rel.replace("/", ".")


def _extract_package(data: bytes, root) -> str | None:
    pkg_node = child_of_type(root, "package_declaration")
    if pkg_node is None:
        return None
    ident = child_of_type(pkg_node, "scoped_identifier", "identifier")
    return node_text(data, ident) if ident is not None else None


def _base_type_name(data: bytes, type_node) -> str | None:
    if type_node is None:
        return None
    if type_node.type == "generic_type":
        inner = child_of_type(type_node, "type_identifier", "scoped_type_identifier")
        return node_text(data, inner) if inner is not None else None
    if type_node.type == "array_type":
        elem = child_of_type(
            type_node, "type_identifier", "scoped_type_identifier", "generic_type"
        )
        return _base_type_name(data, elem)
    if type_node.type in {"type_identifier", "scoped_type_identifier", "identifier"}:
        return node_text(data, type_node)
    return None


def _all_type_identifiers(data: bytes, node) -> list[tuple[str, int]]:
    if node is None:
        return []
    found: list[tuple[str, int]] = []
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "scoped_type_identifier":
            found.append((node_text(data, current), start_line(current)))
            continue
        if current.type == "type_identifier":
            found.append((node_text(data, current), start_line(current)))
            continue
        stack.extend(reversed(current.children))
    return found


def _has_test_annotation(data: bytes, modifiers_node) -> bool:
    if modifiers_node is None:
        return False
    for child in modifiers_node.children:
        if child.type in {"marker_annotation", "annotation"}:
            ident = child_of_type(child, "identifier", "scoped_identifier")
            if ident is not None:
                name = node_text(data, ident).rsplit(".", 1)[-1]
                if name in _TEST_ANNOTATIONS:
                    return True
    return False


def extract_java_file(
    path: str, source: bytes, source_root: str = ""
) -> FileAnalysis:
    text, data, root = parse_tree(source, path, "java")
    package = _extract_package(data, root)
    module = java_module_name(path, package, source_root)
    lines = text.splitlines()
    file_is_test = is_test_path(path)

    symbols: list[Symbol] = []
    edges: list[Edge] = []
    block_nodes: list = []

    imports: dict[str, str] = {}
    local_types_defined: set[str] = set()

    if package:
        edges.append(Edge(module, package, "WILDCARD_IMPORT", 0))

    for child in root.children:
        if child.type in _JAVA_TYPE_NODES:
            ident = child_of_type(child, "identifier")
            if ident is not None:
                tname = node_text(data, ident)
                local_types_defined.add(tname)
                sid = f"{module}:{tname}"
                edges.append(Edge(f"{module}.{tname}", sid, "ALIAS", 0))
                if package:
                    edges.append(Edge(f"{package}.{tname}", sid, "ALIAS", 0))

    for child in root.children:
        if child.type != "import_declaration":
            continue
        line = start_line(child)
        ident = child_of_type(child, "scoped_identifier", "identifier")
        if ident is None:
            continue
        imp_target = node_text(data, ident)
        is_wildcard = any(c.type == "asterisk" for c in child.children)
        edges.append(Edge(f"{module}:", imp_target, "IMPORTS", line))
        if file_is_test:
            edges.append(Edge(f"{module}:", imp_target, "TESTS", line))
        if is_wildcard:
            edges.append(Edge(module, imp_target, "WILDCARD_IMPORT", 0))
        else:
            short = imp_target.rsplit(".", 1)[-1]
            imports[short] = imp_target
            edges.append(Edge(f"{module}.{short}", imp_target, "ALIAS", 0))

    def resolve_type_ref(type_name: str, enclosing_types: list[str]) -> str | None:
        if not type_name or type_name in _JAVA_BUILTINS or len(type_name) == 1 and type_name.isupper():
            return None
        if "." in type_name:
            head, _, tail = type_name.partition(".")
            if head in imports:
                return f"{imports[head]}.{tail}"
            return type_name
        if type_name in enclosing_types or type_name in local_types_defined:
            return f"{module}:{type_name}"
        if type_name in imports:
            return imports[type_name]
        return f"{module}{MODULE_END}{type_name}"

    def emit(owner_sid: str, target: str, kind: str, line: int, in_test: bool = False):
        edges.append(Edge(owner_sid, target, kind, line))
        if (file_is_test or in_test) and kind in {"CALLS", "IMPORTS"}:
            edges.append(Edge(owner_sid, target, "TESTS", line))

    def emit_type_uses(owner_sid: str, node, enclosing_types: list[str], in_test: bool = False):
        for tname, line in _all_type_identifiers(data, node):
            resolved = resolve_type_ref(tname, enclosing_types)
            if resolved is not None:
                emit(owner_sid, resolved, "USES_TYPE", line, in_test)

    def visit_type_decl(node, scope: list[str]):
        ident = child_of_type(node, "identifier")
        if ident is None:
            return
        type_name = node_text(data, ident)
        qual_parts = [*scope, type_name]
        qualname = ".".join(qual_parts)
        sid = f"{module}:{qualname}"
        body_node = child_of_type(
            node, "class_body", "interface_body", "enum_body", "annotation_type_body"
        )
        s_line = start_line(node)
        e_line = end_line(node)
        sig = signature_before_body(data, node, body_node)
        symbols.append(
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
        if scope:
            edges.append(Edge(f"{module}.{qualname}", sid, "ALIAS", 0))

        superclass_target: str | None = None
        super_node = child_of_type(node, "superclass")
        if super_node is not None:
            for type_child in super_node.children:
                if type_child.type in {"type_identifier", "scoped_type_identifier", "generic_type"}:
                    base_name = _base_type_name(data, type_child)
                    if base_name:
                        resolved_base = resolve_type_ref(base_name, qual_parts)
                        if resolved_base:
                            superclass_target = resolved_base
                            emit(sid, resolved_base, "INHERITS", start_line(type_child))
                    if type_child.type == "generic_type":
                        targs = child_of_type(type_child, "type_arguments")
                        emit_type_uses(sid, targs, qual_parts)

        for iface_clause in ("super_interfaces", "extends_interfaces"):
            clause = child_of_type(node, iface_clause)
            if clause is not None:
                tlist = child_of_type(clause, "type_list") or clause
                for type_child in tlist.children:
                    if type_child.type in {"type_identifier", "scoped_type_identifier", "generic_type"}:
                        iface_name = _base_type_name(data, type_child)
                        if iface_name:
                            resolved_iface = resolve_type_ref(iface_name, qual_parts)
                            if resolved_iface:
                                emit(sid, resolved_iface, "INHERITS", start_line(type_child))
                        if type_child.type == "generic_type":
                            targs = child_of_type(type_child, "type_arguments")
                            emit_type_uses(sid, targs, qual_parts)

        field_types: dict[str, str] = {}
        rec_params = child_of_type(node, "formal_parameters")
        if rec_params is not None:
            for param in rec_params.children:
                if param.type in {"formal_parameter", "spread_parameter"}:
                    ptype_node = child_of_type(
                        param,
                        "type_identifier",
                        "scoped_type_identifier",
                        "generic_type",
                        "array_type",
                    )
                    pident = child_of_type(param, "identifier")
                    emit_type_uses(sid, ptype_node, qual_parts)
                    base_t = _base_type_name(data, ptype_node)
                    if pident is not None and base_t is not None:
                        field_types[node_text(data, pident)] = base_t

        if body_node is None:
            return

        own_methods: set[str] = set()
        for member in body_node.children:
            if member.type == "method_declaration":
                m_ident = child_of_type(member, "identifier")
                if m_ident is not None:
                    own_methods.add(node_text(data, m_ident))
            elif member.type == "field_declaration":
                ftype_node = child_of_type(
                    member,
                    "type_identifier",
                    "scoped_type_identifier",
                    "generic_type",
                    "array_type",
                )
                emit_type_uses(sid, ftype_node, qual_parts)
                base_t = _base_type_name(data, ftype_node)
                for decl in member.children:
                    if decl.type == "variable_declarator":
                        fident = child_of_type(decl, "identifier")
                        if fident is not None and base_t is not None:
                            field_types[node_text(data, fident)] = base_t

        method_counts: dict[str, int] = {}
        for member in body_node.children:
            if member.type in _JAVA_TYPE_NODES:
                visit_type_decl(member, qual_parts)
            elif member.type in {"method_declaration", "constructor_declaration"}:
                visit_executable(
                    member,
                    sid,
                    qual_parts,
                    field_types,
                    own_methods,
                    superclass_target,
                    method_counts,
                )

    def visit_executable(
        node,
        class_sid: str,
        enclosing_types: list[str],
        field_types: dict[str, str],
        own_methods: set[str],
        superclass_target: str | None,
        method_counts: dict[str, int],
    ):
        ident = child_of_type(node, "identifier")
        if ident is None:
            return
        mname = node_text(data, ident)
        count = method_counts.get(mname, 0) + 1
        method_counts[mname] = count
        primary_sid = f"{class_sid}.{mname}"
        sid = primary_sid if count == 1 else f"{primary_sid}#{count}"

        body_block = child_of_type(node, "block", "constructor_body")
        if body_block is not None:
            block_nodes.append(body_block)

        s_line = start_line(node)
        e_line = end_line(node)
        if count > 1:
            edges.append(Edge(primary_sid, sid, "CALLS", s_line))

        sig = signature_before_body(data, node, body_block)
        cx = (
            compute_complexity(node, _is_java_decision, _skip_java_nested)
            if body_block is not None
            else 1
        )
        symbols.append(
            Symbol(
                sid,
                "method",
                sig,
                "\n".join(lines[s_line - 1 : e_line]),
                s_line,
                e_line,
                cx,
            )
        )

        modifiers = child_of_type(node, "modifiers")
        in_test = _has_test_annotation(data, modifiers)

        ret_type = child_of_type(
            node, "type_identifier", "scoped_type_identifier", "generic_type", "array_type"
        )
        emit_type_uses(sid, ret_type, enclosing_types, in_test)

        local_types: dict[str, str] = {}
        params = child_of_type(node, "formal_parameters")
        if params is not None:
            for param in params.children:
                if param.type in {"formal_parameter", "spread_parameter"}:
                    ptype = child_of_type(
                        param,
                        "type_identifier",
                        "scoped_type_identifier",
                        "generic_type",
                        "array_type",
                    )
                    emit_type_uses(sid, ptype, enclosing_types, in_test)
                    pident = child_of_type(param, "identifier")
                    if pident is None:
                        vdecl = child_of_type(param, "variable_declarator_id")
                        if vdecl is not None:
                            pident = child_of_type(vdecl, "identifier")
                    base_t = _base_type_name(data, ptype)
                    if pident is not None:
                        pname = node_text(data, pident)
                        local_types[pname] = base_t or "?"

        if body_block is None:
            return

        for desc in descendants(body_block):
            if desc.id != body_block.id and desc.type in _JAVA_TYPE_NODES:
                continue
            if desc.type == "local_variable_declaration":
                ltype = child_of_type(
                    desc,
                    "type_identifier",
                    "scoped_type_identifier",
                    "generic_type",
                    "array_type",
                )
                emit_type_uses(sid, ltype, enclosing_types, in_test)
                base_t = _base_type_name(data, ltype)
                for decl in desc.children:
                    if decl.type == "variable_declarator":
                        vident = child_of_type(decl, "identifier")
                        init_creation = child_of_type(decl, "object_creation_expression")
                        inferred_t = base_t
                        if (inferred_t is None or inferred_t == "var") and init_creation is not None:
                            ctype = child_of_type(
                                init_creation,
                                "type_identifier",
                                "scoped_type_identifier",
                                "generic_type",
                            )
                            inferred_t = _base_type_name(data, ctype)
                        if vident is not None:
                            local_types[node_text(data, vident)] = inferred_t or "?"
            elif desc.type == "object_creation_expression":
                ctype = child_of_type(
                    desc, "type_identifier", "scoped_type_identifier", "generic_type"
                )
                base_t = _base_type_name(data, ctype)
                if base_t:
                    resolved_t = resolve_type_ref(base_t, enclosing_types)
                    if resolved_t:
                        emit(sid, resolved_t, "CALLS", start_line(desc), in_test)
                if ctype is not None and ctype.type == "generic_type":
                    emit_type_uses(sid, child_of_type(ctype, "type_arguments"), enclosing_types, in_test)
            elif desc.type == "method_invocation":
                _handle_method_invocation(
                    desc,
                    sid,
                    class_sid,
                    enclosing_types,
                    field_types,
                    own_methods,
                    local_types,
                    superclass_target,
                    in_test,
                )

    def _handle_method_invocation(
        call_node,
        sid: str,
        class_sid: str,
        enclosing_types: list[str],
        field_types: dict[str, str],
        own_methods: set[str],
        local_types: dict[str, str],
        superclass_target: str | None,
        in_test: bool,
    ):
        line = start_line(call_node)
        non_arg_children = [
            c
            for c in call_node.children
            if c.type not in {"argument_list", "type_arguments", "."}
        ]
        if not non_arg_children:
            return
        callee_ident = non_arg_children[-1]
        if callee_ident.type != "identifier":
            return
        mname = node_text(data, callee_ident)

        if len(non_arg_children) == 1:
            if mname in imports:
                emit(sid, imports[mname], "CALLS", line, in_test)
            elif mname in own_methods:
                emit(sid, f"{class_sid}.{mname}", "CALLS", line, in_test)
            elif superclass_target:
                emit(sid, f"{superclass_target}.{mname}", "CALLS", line, in_test)
            else:
                emit(sid, f"{module}{MODULE_END}{mname}", "CALLS", line, in_test)
            return

        receiver = non_arg_children[0]
        if receiver.type == "this":
            emit(sid, f"{class_sid}.{mname}", "CALLS", line, in_test)
            return
        if receiver.type == "super":
            if superclass_target:
                emit(sid, f"{superclass_target}.{mname}", "CALLS", line, in_test)
            return
        if receiver.type == "field_access":
            fchildren = [c for c in receiver.children if c.type != "."]
            if len(fchildren) == 2 and fchildren[0].type == "this" and fchildren[1].type == "identifier":
                fname = node_text(data, fchildren[1])
                ftype = field_types.get(fname)
                if ftype:
                    resolved = resolve_type_ref(ftype, enclosing_types)
                    if resolved:
                        emit(sid, f"{resolved}.{mname}", "CALLS", line, in_test)
                        return
                emit(sid, f"?this.{fname}.{mname}", "CALLS", line, in_test)
                return
        if receiver.type in {"identifier", "scoped_identifier"}:
            rname = node_text(data, receiver)
            if rname in local_types:
                ltype = local_types[rname]
                if ltype and ltype != "?":
                    resolved = resolve_type_ref(ltype, enclosing_types)
                    if resolved:
                        emit(sid, f"{resolved}.{mname}", "CALLS", line, in_test)
                        return
                emit(sid, f"?{rname}.{mname}", "CALLS", line, in_test)
                return
            if rname in field_types:
                ftype = field_types[rname]
                resolved = resolve_type_ref(ftype, enclosing_types)
                if resolved:
                    emit(sid, f"{resolved}.{mname}", "CALLS", line, in_test)
                    return
            if rname in _JAVA_BUILTINS or rname.startswith("System."):
                return
            resolved_static = resolve_type_ref(rname, enclosing_types)
            if resolved_static:
                emit(sid, f"{resolved_static}.{mname}", "CALLS", line, in_test)
                return
        raw_recv = node_text(data, receiver).strip()
        if not raw_recv.startswith("System."):
            emit(sid, f"?{raw_recv}.{mname}", "CALLS", line, in_test)

    for child in root.children:
        if child.type in _JAVA_TYPE_NODES:
            visit_type_decl(child, [])

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
