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
    if tree.root_node.has_error:
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
    return text if text.endswith("\n") else text + "\n"


def signature_before_body(data: bytes, decl_node, body_node) -> str:
    if body_node is not None and body_node.start_byte > decl_node.start_byte:
        raw = data[decl_node.start_byte : body_node.start_byte].decode("utf-8").rstrip()
    else:
        raw = node_text(data, decl_node).splitlines()[0].rstrip()
    return " ".join(line.strip() for line in raw.splitlines() if line.strip())
