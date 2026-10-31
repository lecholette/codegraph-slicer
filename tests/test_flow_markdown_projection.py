import json
import re
from collections import Counter

import pytest

from codegraph_slicer import GraphStore
from codegraph_slicer.cli import _markdown, main

SOURCE = """def admit(jobs, cap):
    liability = 0
    for job in jobs:
        if job.open:
            liability += job.size
    if liability > cap:
        return False
    return True
"""

ARROWS = {"data ←": "DATA_DEP", "control ←": "CONTROL_DEP"}
HEADER = re.compile(r"^- (\S+) ")
SOURCE_REF = re.compile(r"(\S+) \(([^)]*)\)")


def _db(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "spend.py").write_text(SOURCE)
    db = tmp_path / "graph.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(root)
    return db


def _run(db, capsys, *extra):
    assert main(["flow", "spend.py:admit", "--db", str(db), *extra]) == 0
    return capsys.readouterr().out


def _synced_source_db(tmp_path, source, filename="render.py"):
    root = tmp_path / "src"
    root.mkdir()
    (root / filename).write_text(source)
    db = tmp_path / "graph.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(root)
    return db


def _expected_ref(graph):
    per_line = Counter(n["line"] for n in graph["nodes"] if n["kind"] not in ("entry", "exit"))
    def ref(node):
        if node["kind"] in ("entry", "exit"):
            return node["kind"]
        return f"L{node['line']}" if per_line[node["line"]] == 1 else f"L{node['line']}#{node['id']}"
    return {n["id"]: ref(n) for n in graph["nodes"]}


def _markdown_edges(text):
    edges, target = Counter(), None
    for line in text.split("## Diagnostics", 1)[0].splitlines():
        header = HEADER.match(line)
        if header:
            target = header.group(1)
            continue
        stripped = line.strip()
        for arrow, kind in ARROWS.items():
            if stripped.startswith(f"- {arrow} "):
                for source, label in SOURCE_REF.findall(stripped.split(arrow, 1)[1]):
                    edges[source, target, kind, label] += 1
    return edges


@pytest.mark.parametrize("extra", [(), ("--variable", "liability", "--line", "6")])
def test_markdown_dependency_edges_equal_the_json_edges(tmp_path, capsys, extra):
    db = _db(tmp_path)
    graph = json.loads(_run(db, capsys, *extra))["flow"]
    markdown = _run(db, capsys, *extra, "--format", "markdown")
    ref = _expected_ref(graph)
    expected = Counter(
        (ref[e["source"]], ref[e["target"]], e["kind"], e["label"])
        for e in graph["edges"] if e["kind"] in ARROWS.values()
    )
    assert {kind for _, _, kind, _ in expected} == {"DATA_DEP", "CONTROL_DEP"}
    assert _markdown_edges(markdown) == expected


def test_markdown_disambiguates_nodes_that_share_a_line(tmp_path, capsys):
    db = _db(tmp_path)
    graph = json.loads(_run(db, capsys))["flow"]
    markdown = _run(db, capsys, "--format", "markdown")
    params = [n["id"] for n in graph["nodes"] if n["kind"] == "parameter"]
    assert len(params) == 2
    for nid in params:
        assert re.search(rf"^- L1#{nid} parameter", markdown, re.MULTILINE)


def test_markdown_states_what_it_omits_and_drops_cfg_and_ast(tmp_path, capsys):
    markdown = _run(_db(tmp_path), capsys, "--format", "markdown")
    assert "CFG and AST edges are omitted; `--format json` has the full record." in markdown
    assert "next →" not in markdown and "ast ←" not in markdown
    assert not re.search(r"^- entry ", markdown, re.MULTILINE)


def test_markdown_groups_repeated_diagnostic_messages(tmp_path, capsys):
    db = _db(tmp_path)
    graph = json.loads(_run(db, capsys))["flow"]
    markdown = _run(db, capsys, "--format", "markdown")
    ref = _expected_ref(graph)
    repeated = Counter((d["code"], d["message"]) for d in graph["diagnostics"])
    (code, message), count = repeated.most_common(1)[0]
    assert count > 1
    assert markdown.count(message) == 1
    group = next(line for line in markdown.splitlines() if message in line)
    nodes = [d["node"] for d in graph["diagnostics"] if (d["code"], d["message"]) == (code, message)]
    assert group == f"- `{code}` {message}: " + ", ".join(ref[n] for n in nodes)


def test_markdown_prints_matching_source_hash_once(tmp_path, capsys):
    db = _db(tmp_path)
    graph = json.loads(_run(db, capsys))["flow"]
    markdown = _run(db, capsys, "--format", "markdown")
    assert markdown.count(graph["source_sha256"]) == 1


def test_markdown_reports_its_own_size(tmp_path, capsys):
    markdown = _run(_db(tmp_path), capsys, "--format", "markdown")
    stated = int(re.search(r"this view ≈ (\d+) tokens", markdown).group(1))
    assert stated == len(markdown[:-1]) // 4 + 1


def test_cli_markdown_uses_fence_longer_than_docstring_backtick_runs(tmp_path, capsys):
    db = _synced_source_db(
        tmp_path,
        'def render():\n    """Docstring mentions ```fences``` and `inline` text."""\n    return 1\n',
    )
    assert main(["flow", "render.py:render", "--db", str(db), "--format", "markdown"]) == 0
    markdown = capsys.readouterr().out
    lines = markdown.splitlines()
    assert '- L2 expr: ````"""Docstring mentions ```fences``` and `inline` text."""````' in lines


def test_cli_markdown_fences_multiline_statement_verbatim(tmp_path, capsys):
    db = _synced_source_db(
        tmp_path,
        '''def render():
    message = (
        "first ``` marker"
        " second ~~~~ marker"
    )
    return message
''',
    )
    assert main(["flow", "render.py:render", "--db", str(db), "--format", "markdown"]) == 0
    markdown = capsys.readouterr().out
    lines = markdown.splitlines()
    multiline = lines.index("- L2 assign (to L5):")
    assert lines[multiline + 1:multiline + 7] == [
        "  `````",
        "  message = (",
        '          "first ``` marker"',
        '          " second ~~~~ marker"',
        "      )",
        "  `````",
    ]
    assert markdown.endswith("\n")


def test_markdown_pads_inline_code_that_begins_and_ends_with_backticks(tmp_path, capsys):
    result = json.loads(_run(_db(tmp_path), capsys))
    node = next(n for n in result["flow"]["nodes"] if n["kind"] == "assign")
    ref = _expected_ref(result["flow"])[node["id"]]
    span = "" if node["line"] == node["end_line"] else f" (to L{node['end_line']})"
    node["text"] = "`edge`"
    markdown = _markdown(result)
    assert f"- {ref} assign{span}: `` `edge` ``" in markdown.splitlines()


def test_markdown_refuses_flow_without_profile(tmp_path, capsys):
    result = json.loads(_run(_db(tmp_path), capsys))
    del result["flow"]["profile"]
    with pytest.raises(KeyError, match="profile"):
        _markdown(result)


def test_json_output_is_the_compact_string_the_budget_measures(tmp_path, capsys):
    out = _run(_db(tmp_path), capsys).strip()
    result = json.loads(out)
    assert out == json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    flow_only = json.dumps(result["flow"], ensure_ascii=False, separators=(",", ":"))
    assert result["tokens"] == len(flow_only) // 4 + 1
