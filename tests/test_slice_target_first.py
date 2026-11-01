import json

import pytest

from codegraph_slicer import GraphStore
from codegraph_slicer.cli import main
from codegraph_slicer.slicer import RequiredAnchorOverflow, slice_context


def _db(tmp_path, source):
    root = tmp_path / "src"
    root.mkdir()
    (root / "sample.py").write_text(source)
    db = tmp_path / "graph.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(root)
    return db


def _large_class(lines=100):
    return "class Context:\n    \"\"\"Public API docs.\"\"\"\n    def value(self, arg: int) -> int:\n        \"\"\"Return public value.\"\"\"\n" + "".join(
        f"        local_{i} = {i}\n" for i in range(lines)
    ) + "        return arg\n\n"


def _target_method():
    return "class Worker:\n    def target(self, value: Context) -> int:\n        return value.value(3)\n"


def test_markdown_places_target_first_and_preserves_source_spans(tmp_path, capsys):
    db = _db(tmp_path, _large_class() + _target_method())
    assert main(["slice", "--db", str(db), "sample:Worker.target", "--snapshot", "--format", "markdown", "--max-tokens", "2000"]) == 0
    output = capsys.readouterr().out
    assert output.index("## target implementations") < output.index("## types and schemas")
    target = "sample.py:108-109\n    def target(self, value: Context) -> int:"
    related = "sample.py:1-105\nclass Context:"
    assert output.index(target) < output.index(related)


def test_related_large_class_is_summarized_at_strict_threshold_with_fetch_note(tmp_path, capsys):
    db = _db(tmp_path, _large_class(400) + _target_method())
    with GraphStore(db, readonly=True) as store:
        class_row = store.rows("SELECT body FROM symbols WHERE symbol_id='sample:Context'")[0]
        body_tokens = len(class_row["body"]) // 4 + 1
    boundary = body_tokens * 2
    assert main(["slice", "--db", str(db), "sample:Worker.target", "--snapshot", "--format", "markdown", "--max-tokens", str(boundary)]) == 0
    at_boundary = capsys.readouterr().out
    assert "local_399" in at_boundary
    assert "Expansion " not in at_boundary
    assert main(["slice", "--db", str(db), "sample:Worker.target", "--snapshot", "--format", "markdown", "--max-tokens", str(boundary - 1)]) == 0
    output = capsys.readouterr().out
    assert "Public API docs." in output
    assert "def value(self, arg: int) -> int:" in output
    assert "Return public value." in output
    assert "local_399" not in output
    exact_budget = max(boundary - 1, body_tokens * 2)
    assert f"Expansion sample:Context ({body_tokens} tokens): codegraph-slicer slice --db {db} sample:Context --snapshot --max-tokens {exact_budget}" in output


def test_small_slice_keeps_full_class_text_and_symbol_list(tmp_path, capsys):
    db = _db(tmp_path, "class Context:\n    value: int\n\nclass Worker:\n    def target(self, value: Context) -> int:\n        return value.value\n")
    assert main(["slice", "--db", str(db), "sample:Worker.target", "--snapshot", "--format", "json", "--max-tokens", "500"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [r["symbol"] for section in payload["sections"].values() for r in section] == ["sample:Context", "sample:Worker.target"]
    assert "class Context:" in payload["sections"]["types_and_schemas"][0]["text"]
    assert "value: int" in payload["sections"]["types_and_schemas"][0]["text"]
    assert "omitted" not in payload


def test_required_anchor_overflow_keeps_exact_error_text(tmp_path, capsys):
    db = _db(tmp_path, _large_class() + _target_method())
    with GraphStore(db, readonly=True) as store, pytest.raises(
        RequiredAnchorOverflow,
        match=r"^required definitions need 688 tokens including omission metadata, budget is 1$",
    ):
        slice_context(store, ["sample:Context", "sample:Worker.target"], max_tokens=1)
    with pytest.raises(SystemExit) as cli_error:
        main(["slice", "--db", str(db), "sample:Context", "sample:Worker.target", "--snapshot", "--format", "json", "--max-tokens", "1"])
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "RequiredAnchorOverflow: required definitions need 688 tokens including omission metadata, budget is 1" in captured.err
    assert cli_error.value.code == 2
