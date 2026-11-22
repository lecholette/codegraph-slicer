"""Definition signatures come from one line split per file and match `ast.get_source_segment` byte-for-byte."""
import ast
import io
import tokenize

import pytest

from codegraph_slicer import ast_extractor, extract_file

SOURCE = (
    "# -*- coding: utf-8 -*-\n"
    "\"\"\"Módulo con identificadores no ASCII: 数据, ü, emoji 🚀.\"\"\"\n"
    "def größe(x: 'Mäßig', y=\"ß\") -> 'Ergebnis':  # kommentar ✓\n"
    "    return x\n"
    "\n"
    "class Bäckerei(dict, metaclass=type):\n"
    "    \"\"\"Doc ✓.\"\"\"\n"
    "    @staticmethod\n"
    "    def 数据(a: dict[str, 'é'] = {'k': 'ü'},\n"
    "             *args: '…',\n"
    "             **kw: 'λ') -> 'ñ':\n"
    "        class Innen: pass\n"
    "        return a\n"
    "\n"
    "    async def wait(self, t: 'café' = 'naïve'): await t\n"
    "\f\n"
    "def tabbed(\ta: int,\n"
    "\t\tb: str = '\\u00e9'\n"
    "\t) -> None:\n"
    "\tpass\n"
    "x = 1; y = lambda q: q  # 🚀 after\n"
    "z = ('é🚀', [\n"
    "    'ü', 2])\n"
    "def crlf(a,\r\n"
    "         b): return 'é'\r\n"
    "def lone_cr(a): pass\r"
    "def after_cr(z: '🚀'): pass\n"
)


def _reference_signature(node, text):
    """The pre-optimization implementation, built on `ast.get_source_segment`."""
    segment = ast.get_source_segment(text, node) or ""
    depth = 0
    for token in tokenize.generate_tokens(io.StringIO(segment).readline):
        if token.type == tokenize.OP:
            if token.string in "([{":
                depth += 1
            elif token.string in ")]}":
                depth -= 1
            elif token.string == ":" and depth == 0:
                lines = segment.splitlines(keepends=True)
                return "".join(lines[: token.end[0] - 1]) + lines[token.end[0] - 1][: token.end[1]]
    raise AssertionError("no header")


def test_source_segment_matches_stdlib_for_every_node():
    tree = ast.parse(SOURCE)
    lines = ast_extractor._source_lines(SOURCE)
    nodes = [n for n in ast.walk(tree) if hasattr(n, "lineno")]
    assert len(nodes) > 50
    for node in nodes:
        assert ast_extractor._source_segment(lines, node) == ast.get_source_segment(SOURCE, node), ast.dump(node)


def test_signatures_identical_to_reference_including_non_ascii():
    tree = ast.parse(SOURCE)
    lines = ast_extractor._source_lines(SOURCE)
    defs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    assert {d.name for d in defs} >= {"größe", "Bäckerei", "数据", "Innen", "wait", "tabbed", "crlf", "after_cr"}
    for node in defs:
        assert ast_extractor._signature(node, lines) == _reference_signature(node, SOURCE), node.name


def test_extract_file_signatures_and_skeleton_unchanged(monkeypatch):
    expected = {}
    tree = ast.parse(SOURCE)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            expected[node.name] = _reference_signature(node, SOURCE)
    expected_skeleton = ast.unparse(ast_extractor._Strip().visit(ast.parse(SOURCE))) + "\n"

    # The per-definition re-split of the whole file is what this replaces; it must not come back.
    def _no_segment(*_a, **_k):
        raise AssertionError("ast.get_source_segment re-splits the file per definition")

    monkeypatch.setattr(ast_extractor.ast, "get_source_segment", _no_segment)
    analysis = extract_file("pkg/m.py", SOURCE.encode("utf-8"))
    got = {s.symbol_id.rsplit(".", 1)[-1].rsplit(":", 1)[-1]: s.signature for s in analysis.symbols if s.kind != "module"}
    for name, sig in expected.items():
        assert got[name] == sig, name
    assert analysis.skeleton == expected_skeleton


@pytest.mark.parametrize("encoding", ["latin-1", "utf-8"])
def test_declared_encoding_offsets(encoding):
    src = f"# -*- coding: {encoding} -*-\ndef f(a='é', b: 'ü' = 1) -> 'ß': pass\n"
    analysis = extract_file("m.py", src.encode(encoding))
    (sym,) = [s for s in analysis.symbols if s.kind == "function"]
    assert sym.signature == "def f(a='é', b: 'ü' = 1) -> 'ß':"
