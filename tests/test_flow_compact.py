import json
import re
from collections import Counter

from codegraph_slicer import GraphStore
from codegraph_slicer.cli import main

SOURCE = """def f(items, obj):
    total = 0
    for item in items:
        try:
            total += obj.value
        finally:
            obj.value = total; total = missing
    return total
"""


def _flow(tmp_path, capsys, *flags):
    root = tmp_path / "src"
    root.mkdir(exist_ok=True)
    (root / "sample.py").write_text(SOURCE)
    db = tmp_path / "graph.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(root)
    assert main(["flow", "sample.py:f", "--db", str(db), "--snapshot", *flags]) == 0
    output = capsys.readouterr().out
    return output if "markdown" in flags else json.loads(output)


def test_compact_flow_preserves_facts_identity_and_same_line_cleanup_nodes(tmp_path, capsys):
    full = _flow(tmp_path, capsys, "--max-tokens", "100000")
    compact = _flow(tmp_path, capsys, "--compact", "--max-tokens", "100000")["flow"]
    compact_markdown = _flow(
        tmp_path, capsys, "--compact", "--format", "markdown", "--max-tokens", "100000"
    )
    graph = full["flow"]

    assert compact["schema"] == "python-local-cpg-flow-compact-v1"
    assert compact["identity"]["path"] == graph["path"]
    assert compact["identity"]["engine_sha256"] == graph["engine_sha256"]
    assert compact["query"] == graph["query"]
    assert compact["seeds"] == graph["seeds"]
    assert "# Compact Python local flow" in compact_markdown
    assert '"schema": "python-local-cpg-flow-compact-v1"' in compact_markdown

    node_columns = compact["nodes"]["columns"]
    assert node_columns == [
        "id", "kind", "line", "end_line", "text", "reachable", "synthetic",
        "defs", "uses", "mutation_targets",
    ]
    compact_nodes = [dict(zip(node_columns, row)) for row in compact["nodes"]["rows"]]
    assert compact_nodes == graph["nodes"]
    edge_columns = compact["edges"]["columns"]
    assert edge_columns == ["source", "target", "kind", "label"]
    compact_edges = [dict(zip(edge_columns, row)) for row in compact["edges"]["rows"]]
    assert compact_edges == graph["edges"]
    same_line_cleanup = [node for node in compact_nodes if node["line"] == 7]
    assert len(same_line_cleanup) >= 2
    assert len({node["id"] for node in same_line_cleanup}) == len(same_line_cleanup)
    assert any(node["mutation_targets"] == ["obj.value"] for node in compact_nodes)
    assert any("missing" in node["uses"] for node in compact_nodes)

    diagnostic_rows = compact["diagnostics"]["rows"]
    assert compact["diagnostics"]["columns"] == ["code", "message", "occurrences"]
    projected_diagnostics = Counter(
        (code, node, line, message)
        for code, message, occurrences in diagnostic_rows
        for node, line in occurrences
    )
    assert projected_diagnostics == Counter(
        (item["code"], item["node"], item["line"], item["message"])
        for item in graph["diagnostics"]
    )
    live_ins = [row for row in diagnostic_rows if row[0] == "unknown-live-in"]
    assert live_ins
    assert any(len(row[2]) > 1 for row in live_ins)


def test_compact_markdown_uses_printed_token_header_and_unindented_json(tmp_path, capsys):
    markdown = _flow(
        tmp_path, capsys, "--compact", "--format", "markdown", "--max-tokens", "100000"
    )
    output_tokens = len(markdown) // 4 + 1
    header = int(re.search(r"Estimated context tokens: (\d+)", markdown).group(1))
    assert header == output_tokens
    fenced_json = markdown.split("```json\n", 1)[1].split("\n```", 1)[0]
    assert "\n  \"schema\"" not in fenced_json
    assert '"schema": "python-local-cpg-flow-compact-v1"' in fenced_json
    assert json.loads(fenced_json)["schema"] == "python-local-cpg-flow-compact-v1"
