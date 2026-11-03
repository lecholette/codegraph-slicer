import json

import pytest

from codegraph_slicer.cli import main


def _snapshot(root, files):
    from codegraph_slicer import GraphStore

    root.mkdir()
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    db = root.parent / f"{root.name}.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(root)
    return db


def test_slice_outline_emits_closed_entity_graph_and_scoped_observations(tmp_path, capsys):
    db = _snapshot(tmp_path / "src", {
        "pkg/a.py": (
            "from pkg.b import Base, helper\n"
            "import external_pkg\n"
            "class Child(Base):\n"
            "    def run(self) -> Base:\n"
            "        return helper() + missing_call()\n"
            "\n"
            "def caller():\n"
            "    return Child().run()\n"
        ),
        "pkg/b.py": "class Base:\n    pass\n\ndef helper():\n    return missing_peer()\n",
        "pkg/unrelated.py": "def unrelated():\n    return 99\n",
    })
    assert main([
        "slice", "--db", str(db), "--outline", "pkg.a:Child.run", "--max-tokens", "4000",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert main([
        "slice", "--db", str(db), "--outline", "pkg.a:Child.run", "--max-tokens", "4000",
    ]) == 0
    assert json.loads(capsys.readouterr().out) == payload

    assert payload["schema_version"] == 1
    assert set(payload) == {
        "schema_version", "tokens", "entities", "edges", "diagnostics",
        "diagnostic_counts", "omitted",
    }
    assert {item["kind"] for item in payload["edges"]} >= {
        "CALLS", "USES_TYPE", "INHERITS", "DEFINES",
    }
    assert all(item["kind"] != "IMPORTS" for item in payload["edges"])
    entities = {item["id"]: item for item in payload["entities"]}
    assert [item["id"] for item in payload["entities"]] == [
        f"e{index:04d}" for index in range(1, len(payload["entities"]) + 1)
    ]
    assert all(edge["from"] in entities and edge["to"] in entities for edge in payload["edges"])
    call = next(edge for edge in payload["edges"] if edge["kind"] == "CALLS")
    assert call["line"] == 5
    assert all("body" not in entity and "text" not in entity for entity in payload["entities"])
    assert "unrelated" not in {entity["name"] for entity in payload["entities"]}
    missing = next(item for item in payload["diagnostics"] if item["target"] == "?missing_call")
    assert missing["kind"] == "CALLS"
    assert missing["line"] == 5
    assert missing["source"] in entities
    assert all(entity["kind"] != "module" for entity in entities.values())
    helper_id = next(entity["id"] for entity in entities.values() if entity["name"] == "helper")
    assert payload["diagnostic_counts"] == {helper_id: 1}
    assert payload["omitted"]["edges"] == 4


def test_slice_outline_import_context_is_opt_in(tmp_path, capsys):
    db = _snapshot(tmp_path / "src", {
        "pkg/a.py": "import pkg.b\ndef target():\n    return pkg.b.helper()\n",
        "pkg/b.py": "def helper():\n    return 1\n",
    })
    assert main([
        "slice", "--db", str(db), "--outline", "pkg.a:target",
        "--include-imports", "--max-tokens", "4000",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert any(entity["kind"] == "module" for entity in payload["entities"])
    assert any(edge["kind"] == "IMPORTS" for edge in payload["edges"])


def test_slice_outline_refuses_too_small_budget(tmp_path, capsys):
    db = _snapshot(tmp_path / "tiny", {"pkg/a.py": "def target():\n    return 1\n"})
    with pytest.raises(SystemExit, match="2") as exc:
        main(["slice", "--db", str(db), "--outline", "pkg.a:target", "--max-tokens", "1"])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "required slice outline" in captured.err


def test_slice_outline_keeps_required_graph_when_anchor_diagnostics_do_not_fit(
    tmp_path, capsys
):
    calls = " + ".join(f"missing_{index}()" for index in range(12))
    db = _snapshot(tmp_path / "diagnostic-heavy", {
        "pkg/a.py": f"def target():\n    return {calls}\n",
    })
    assert main([
        "slice", "--db", str(db), "--outline", "pkg.a:target", "--max-tokens", "80",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tokens"] == 67
    assert [entity["symbol"] for entity in payload["entities"]] == ["pkg.a:target"]
    assert payload["edges"] == []
    assert payload["diagnostics"] == []
    assert payload["omitted"] == {"edges": 1, "diagnostics": 12}
