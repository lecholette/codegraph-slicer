"""Shared Tree-sitter parsing and AST utilities for Java and TypeScript."""

from functools import lru_cache


@lru_cache(maxsize=None)
def parser_for(grammar: str):
    try:
        from tree_sitter_language_pack import get_parser
    except ImportError as exc:
        from .ast_extractor import CodeGraphError

        raise CodeGraphError(
            f"tree-sitter-language-pack is required to parse {grammar} files"
        ) from exc
    return get_parser(grammar)


def _has_non_jsx_syntax_error(root, data: bytes) -> bool:
    for node in descendants(root):
        if node.type == "ERROR" or node.is_missing:
            if node.is_missing:
                return True
            raw_err = data[node.start_byte : node.end_byte]
            if b"&" not in raw_err:
                return True
            curr = node.parent
            in_jsx_text = False
            while curr is not None:
                if curr.type == "jsx_expression":
                    return True
                if curr.type in {"jsx_element", "jsx_self_closing_element", "jsx_Opening_element"} or curr.type.startswith("jsx_"):
                    in_jsx_text = True
                    break
                curr = curr.parent
            if not in_jsx_text:
                return True
    return False


def split_source_lines(text: str) -> list[str]:
    if not text:
        return []
    trimmed = text[:-1] if text.endswith("\n") else text
    return [line.rstrip("\r") for line in trimmed.split("\n")]


def parse_tree(source: bytes, path: str, grammar: str):
    from .ast_extractor import CodeGraphParseError

    if b"\0" in source:
        raise CodeGraphParseError(f"binary source: {path}")
    try:
        text = source.decode("utf-8")
    except UnicodeError as exc:
        raise CodeGraphParseError(f"cannot parse {path}: {exc}") from exc
    data = text.encode("utf-8")
    tree = parser_for(grammar).parse(data)
    if tree.root_node.has_error and (
        grammar != "tsx" or _has_non_jsx_syntax_error(tree.root_node, data)
    ):
        raise CodeGraphParseError(f"cannot parse {path}: syntax error")
    return text, data, tree.root_node


def node_text(data: bytes, node) -> str:
    return data[node.start_byte : node.end_byte].decode("utf-8")


def start_line(node) -> int:
    return node.start_point[0] + 1


def end_line(node) -> int:
    row, col = node.end_point
    if col == 0:
        return max(start_line(node), row)
    return row + 1


def child_of_type(node, *types: str):
    for child in node.children:
        if child.type in types:
            return child
    return None


def children_of_type(node, *types: str) -> list:
    return [child for child in node.children if child.type in types]


def descendants(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def scoped_descendants(
    node,
    skip_types: set[str] | frozenset[str],
    yield_skipped_types: set[str] | frozenset[str] = frozenset(),
):
    root_id = node.id
    stack = [node]
    while stack:
        current = stack.pop()
        if current.id != root_id and current.type in skip_types:
            if current.type in yield_skipped_types:
                yield current
            continue
        yield current
        stack.extend(reversed(current.children))


def compute_complexity(node, is_decision, skip) -> int:
    score = 1
    root_id = node.id
    stack = [node]
    while stack:
        current = stack.pop()
        if current.id != root_id and skip(current):
            continue
        if is_decision(current):
            score += 1
        stack.extend(reversed(current.children))
    return score


def binary_logic(node, operators=("&&", "||")) -> bool:
    return node.type == "binary_expression" and any(
        child.type in operators for child in node.children
    )


def _is_private_skeleton_line(line: str) -> bool:
    s = line.lstrip()
    return "{ ... }" in s and s.startswith(("private ", "#"))


def _fold_brace_private_methods(text: str) -> str:
    lines = text.splitlines()
    priv_idxs = [i for i, ln in enumerate(lines) if _is_private_skeleton_line(ln)]
    if len(priv_idxs) <= 3:
        return text
    omitted = len(priv_idxs) - 3
    drop_set = set(priv_idxs[3:])
    last_kept = priv_idxs[2]
    indent = lines[last_kept][: len(lines[last_kept]) - len(lines[last_kept].lstrip())]
    out_lines: list[str] = []
    for i, ln in enumerate(lines):
        if i in drop_set:
            continue
        out_lines.append(ln)
        if i == last_kept:
            out_lines.append(f"{indent}// ... (+{omitted} private methods)")
    return "\n".join(out_lines) + "\n"


def build_brace_skeleton(data: bytes, block_nodes: list) -> str:
    span_set = {(n.start_byte, n.end_byte) for n in block_nodes}
    outer = [
        n
        for n in block_nodes
        if not any(
            s <= n.start_byte and n.end_byte <= e and (s, e) != (n.start_byte, n.end_byte)
            for s, e in span_set
        )
    ]
    outer.sort(key=lambda n: n.start_byte, reverse=True)
    out = bytearray(data)
    for block in outer:
        out[block.start_byte : block.end_byte] = b"{ ... }"
    text = out.decode("utf-8")
    normalized = text if text.endswith("\n") else text + "\n"
    return _fold_brace_private_methods(normalized)


def signature_before_body(data: bytes, decl_node, body_node) -> str:
    if body_node is not None and body_node.start_byte > decl_node.start_byte:
        raw = data[decl_node.start_byte : body_node.start_byte].decode("utf-8").rstrip()
    else:
        raw = node_text(data, decl_node).splitlines()[0].rstrip()
    return " ".join(line.strip() for line in raw.splitlines() if line.strip())
