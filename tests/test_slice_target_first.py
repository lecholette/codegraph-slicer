import json

import pytest

from codegraph_slicer import GraphStore
from codegraph_slicer.cli import main
from codegraph_slicer.slicer import RequiredAnchorOverflow, slice_context


def _db(tmp_path, source, filename="sample.py"):
    root = tmp_path / "src"
    root.mkdir()
    (root / filename).write_text(source)
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


def test_related_large_class_is_summarized_only_when_full_slice_does_not_fit(tmp_path, capsys):
    db = _db(tmp_path, _large_class(400) + _target_method())
    with GraphStore(db, readonly=True) as store:
        class_row = store.rows("SELECT body FROM symbols WHERE symbol_id='sample:Context'")[0]
        body_tokens = len(class_row["body"]) // 4 + 1
    assert main(["slice", "--db", str(db), "sample:Worker.target", "--snapshot", "--format", "markdown", "--max-tokens", "10000"]) == 0
    at_boundary = capsys.readouterr().out
    assert "local_399" in at_boundary
    assert "Expansion " not in at_boundary
    summary_budget = body_tokens
    assert main(["slice", "--db", str(db), "sample:Worker.target", "--snapshot", "--format", "markdown", "--max-tokens", str(summary_budget)]) == 0
    output = capsys.readouterr().out
    assert "Public API docs." in output
    assert "def value(self, arg: int) -> int:" in output
    assert "Return public value." in output
    assert "local_399" not in output
    exact_budget = max(summary_budget, body_tokens * 2)
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


def test_cli_huge_line_anchor_fails_cleanly(tmp_path, capsys):
    db = _db(tmp_path, "def target():\n    return 1\n", filename="a.py")
    with pytest.raises(SystemExit) as error:
        main(["slice", "--db", str(db), "a.py:9223372036854775808", "--snapshot"])
    captured = capsys.readouterr()
    assert error.value.code == 2
    assert captured.out == ""
    assert captured.err == "codegraph-slicer: AnchorNotFoundError: a.py:9223372036854775808\n"


def test_cli_large_class_full_then_reported_budget_is_identical(tmp_path, capsys):
    source = "class C:\n" + "".join(f"    def m{i}(self): pass\n" for i in range(30)) + "\ndef f(x: C):\n    return x\n"
    db = _db(tmp_path, source, filename="a.py")
    args = ["slice", "--db", str(db), "a:f", "--snapshot"]
    assert main([*args, "--max-tokens", "10000"]) == 0
    generous = capsys.readouterr().out
    payload = json.loads(generous)
    budget = payload["tokens"]
    assert budget == len(generous) // 4 + 1
    class_item = payload["sections"]["types_and_schemas"][0]
    assert class_item == {"symbol": "a:C", "text": "a.py:1-31\n" + "class C:\n" + "".join(f"    def m{i}(self): pass\n" for i in range(30)).rstrip()}
    assert main([*args, "--max-tokens", str(budget)]) == 0
    assert capsys.readouterr().out == generous


def test_cli_many_callers_keeps_target_and_exact_omissions(tmp_path, capsys, monkeypatch):
    source = "def target():\n    return 1\n" + "".join(f"def caller_{i:03}():\n    return target()\n" for i in range(100))
    db = _db(tmp_path, source, filename="a.py")
    monkeypatch.chdir(tmp_path)
    assert main(["slice", "--db", db.name, "a:target", "--snapshot", "--max-tokens", "300"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["sections"]["target_implementations"][0] == {
        "symbol": "a:target", "text": "a.py:1-2\ndef target():\n    return 1"
    }
    assert payload["omitted"] == {
        "types_and_schemas": 0, "upstream_callers": 90,
        "target_implementations": 0, "downstream_dependencies": 0,
    }
    assert payload["tokens"] == len(output) // 4 + 1 <= 300
