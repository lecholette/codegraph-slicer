import json
import re

import pytest

from codegraph_slicer import GraphStore
from codegraph_slicer.cli import main
from codegraph_slicer.pagerank import build_repo_outline, rank


def _snapshot(root, files):
    root.mkdir()
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    db = root.parent / f"{root.name}.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(root)
    return db


def test_outline_serializes_local_entities_and_typed_edges(tmp_path, capsys):
    db = _snapshot(tmp_path / "src", {
        "pkg/a.py": "from pkg.b import Base\nclass Child(Base):\n    def run(self) -> int:\n        return helper()\n\ndef helper():\n    return 3\n",
        "pkg/b.py": "class Base:\n    pass\n",
    })
    assert main(["map", "--db", str(db), "--outline", "--seed", "pkg.a", "--max-tokens", "2000"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema_version"] == 1
    a_file = next(item for item in payload["files"] if item["path"] == "pkg/a.py")
    entities = {item["name"]: item for item in a_file["entities"]}
    assert entities["Child"]["kind"] == "class"
    assert entities["Child"]["line"] == 2
    assert entities["run"]["signature"] == "def run(self) -> int:"
    assert all("id" in item for item in a_file["entities"])
    ids = {item["id"] for item in a_file["entities"]}
    a_edges = [edge for edge in payload["edges"] if edge["from"].startswith(a_file["id"] + ":")]
    assert all(edge["from"].split(":", 1)[1] in ids for edge in a_edges)
    assert {edge["kind"] for edge in a_edges} >= {"CALLS", "INHERITS", "DEFINES"}
    assert all(":" in edge["to"] for edge in a_edges)
    assert all(set(edge) == {"from", "to", "kind"} for edge in payload["edges"])


def test_outline_omissions_name_each_skipped_entity_and_path(tmp_path, capsys):
    db = _snapshot(tmp_path / "limited", {
        "pkg/seed.py": "def alpha():\n    return 1\n\ndef beta():\n    return alpha()\n",
        "pkg/other.py": "def unrelated():\n    return 9\n",
    })
    assert main(["map", "--db", str(db), "--outline", "--seed", "pkg.seed", "--max-tokens", "120"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    omitted_entities = payload["omitted"]["entities"]
    assert all(item["path"] and item["max_tokens"] > payload["tokens"] for item in omitted_entities)
    assert all("name" in item and "id" in item for item in omitted_entities)
    assert {item["name"] for item in omitted_entities} == {"beta"}
    assert {item["path"] for item in omitted_entities} == {"pkg/seed.py"}
    assert "files" not in payload["omitted"]
    assert payload["tokens"] == len(output) // 4 + 1 <= 120
    beta_budget = omitted_entities[0]["max_tokens"]
    assert main(["map", "--db", str(db), "--outline", "--seed", "pkg.seed", "--max-tokens", str(beta_budget)]) == 0
    retry = json.loads(capsys.readouterr().out)
    assert any(entity["name"] == "beta" for file in retry["files"] for entity in file["entities"])
    assert retry["tokens"] == beta_budget
    assert "omitted" not in retry


def test_outline_file_retry_budget_admits_the_file(tmp_path, capsys):
    db = _snapshot(tmp_path / "file-limit", {
        "pkg/seed.py": "from pkg.neighbor import bravo\ndef alpha():\n    return bravo()\n",
        "pkg/neighbor.py": "def bravo():\n    return 7\n",
    })
    assert main(["map", "--db", str(db), "--outline", "--seed", "pkg.seed", "--max-tokens", "120"]) == 0
    payload = json.loads(capsys.readouterr().out)
    omitted = next(item for item in payload["omitted"]["files"] if item["path"] == "pkg/neighbor.py")
    assert omitted["max_tokens"] > payload["tokens"]
    assert main(["map", "--db", str(db), "--outline", "--seed", "pkg.seed", "--max-tokens", str(omitted["max_tokens"])]) == 0
    retry = json.loads(capsys.readouterr().out)
    assert "pkg/neighbor.py" in [file["path"] for file in retry["files"]]
    assert retry["tokens"] == omitted["max_tokens"]
    assert "omitted" not in retry


def test_outline_complete_budget_skips_oversized_omission_metadata(tmp_path, capsys):
    functions = [f"def helper_{index}():\n    return {index}\n" for index in range(40)]
    long_path = f"pkg/{'long_component_' * 5}seed.py"
    db = _snapshot(tmp_path / "complete-budget", {
        long_path: "\n".join(functions),
    })
    assert main(["map", "--db", str(db), "--outline", "--seed", long_path, "--max-tokens", "10000"]) == 0
    complete = json.loads(capsys.readouterr().out)
    assert "omitted" not in complete
    assert main(["map", "--db", str(db), "--outline", "--seed", long_path, "--max-tokens", str(complete["tokens"])]) == 0
    exact = json.loads(capsys.readouterr().out)
    assert exact["tokens"] == complete["tokens"]
    assert exact == complete


def test_outline_lists_seed_files_before_higher_ranked_neighbors(tmp_path, capsys):
    imports = ", ".join(f"helper_{index}" for index in range(20))
    calls = "\n".join(f"    helper_{index}()" for index in range(20))
    neighbors = "\n".join(
        f"def helper_{index}():\n    return helper_{(index + 1) % 20}()"
        for index in range(20)
    )
    db = _snapshot(tmp_path / "seed-priority", {
        "pkg/seed.py": f"from pkg.neighbor import {imports}\ndef alpha():\n{calls}\n",
        "pkg/neighbor.py": neighbors + "\n",
    })
    with GraphStore(db, readonly=True) as store:
        scores = rank(store, ["pkg.seed"])
        paths = {row["symbol_id"]: row["file_path"] for row in store.rows(
            "SELECT symbol_id,file_path FROM symbols"
        )}
    by_file = {path: sum(score for symbol, score in scores.items() if paths[symbol] == path) for path in set(paths.values())}
    assert by_file["pkg/neighbor.py"] > by_file["pkg/seed.py"]
    assert main(["map", "--db", str(db), "--outline", "--seed", "pkg.seed", "--max-tokens", "10000"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["files"][0]["path"] == "pkg/seed.py"
    assert [entity["name"] for entity in payload["files"][0]["entities"][:2]] == ["", "alpha"]


def test_outline_preserves_repeated_seed_order(tmp_path, capsys):
    db = _snapshot(tmp_path / "multiple-seeds", {
        "pkg/a.py": "def alpha():\n    return 1\n",
        "pkg/b.py": "def bravo():\n    return 2\n",
    })
    assert main([
        "map", "--db", str(db), "--outline", "--seed", "pkg.b", "--seed", "pkg.a",
        "--max-tokens", "2000",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [file["path"] for file in payload["files"]] == ["pkg/b.py", "pkg/a.py"]


def test_outline_markdown_resolves_compact_edge_file_ids(tmp_path, capsys):
    db = _snapshot(tmp_path / "markdown-ids", {
        "pkg/seed.py": "from pkg.neighbor import helper\ndef alpha():\n    return helper()\n",
        "pkg/neighbor.py": "def helper():\n    return alpha()\n",
    })
    assert main([
        "map", "--db", str(db), "--outline", "--seed", "pkg.seed",
        "--format", "markdown", "--max-tokens", "2000",
    ]) == 0
    output = capsys.readouterr().out
    file_ids = set(re.findall(r"^## .* · `(f\d{4})`$", output, re.MULTILINE))
    edge_refs = re.findall(r"- (?:CALLS|IMPORTS|USES_TYPE|INHERITS|DEFINES): `(f\d{4}):e\d{4}` → `(f\d{4}):e\d{4}`", output)
    assert file_ids
    assert edge_refs
    assert all(source in file_ids and target in file_ids for source, target in edge_refs)


def test_outline_refuses_when_required_metadata_cannot_fit(tmp_path, capsys):
    db = _snapshot(tmp_path / "tiny", {"pkg/seed.py": "def alpha():\n    return 1\n"})
    with pytest.raises(SystemExit, match="2") as exc:
        main(["map", "--db", str(db), "--outline", "--seed", "pkg.seed", "--max-tokens", "1"])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "required outline metadata" in captured.err


def test_seeded_legacy_map_omits_zero_score_and_reports_top_file(tmp_path, capsys):
    db = _snapshot(tmp_path / "legacy", {
        "pkg/a_seed.py": '"""' + "x" * 5000 + '"""\nfrom pkg.b_neighbor import bravo\ndef alpha():\n    return bravo()\n',
        "pkg/b_neighbor.py": "def bravo():\n    return 7\n",
        "pkg/z_unrelated.py": "def unrelated():\n    return 8\n",
        "pkg/empty.py": "",
    })
    assert main(["map", "--db", str(db), "--seed", "pkg.a_seed", "--max-tokens", "120"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    paths = [item["path"] for item in payload["items"]]
    assert "pkg/z_unrelated.py" not in paths
    assert "pkg/empty.py" not in paths
    assert payload["top_file_omitted"]["path"] == "pkg/a_seed.py"
    assert payload["top_file_omitted"]["max_tokens"] > payload["tokens"]
    assert payload["tokens"] == len(output) // 4 + 1 <= 120
    retry_budget = payload["top_file_omitted"]["max_tokens"]
    assert main(["map", "--db", str(db), "--seed", "pkg.a_seed", "--max-tokens", str(retry_budget)]) == 0
    retry = json.loads(capsys.readouterr().out)
    assert "pkg/a_seed.py" in [item["path"] for item in retry["items"]]
    assert retry["tokens"] <= retry_budget
    assert main(["map", "--db", str(db), "--seed", "pkg.a_seed", "--max-tokens", "2000"]) == 0
    roomy = json.loads(capsys.readouterr().out)
    assert "pkg/z_unrelated.py" not in [item["path"] for item in roomy["items"]]
    assert "pkg/empty.py" not in [item["path"] for item in roomy["items"]]


def test_unseeded_legacy_map_output_remains_unchanged(tmp_path, capsys):
    db = _snapshot(tmp_path / "unseeded", {
        "pkg/a.py": "def alpha():\n    return 1\n",
        "pkg/b.py": "def bravo():\n    return 2\n",
        "pkg/empty.py": "",
    })
    assert main(["map", "--db", str(db), "--max-tokens", "2000"]) == 0
    assert capsys.readouterr().out == (
        '{"tokens":67,"items":[{"symbol":"pkg.a:","path":"pkg/a.py",'
        '"text":"pkg/a.py:1\\ndef alpha():\\n    ...\\n"},{"symbol":"pkg.b:",'
        '"path":"pkg/b.py","text":"pkg/b.py:1\\ndef bravo():\\n    ...\\n"},'
        '{"symbol":"pkg.empty:","path":"pkg/empty.py","text":"pkg/empty.py:1\\n\\n"}]}\n'
    )


def test_seeded_outline_excludes_zero_score_files(tmp_path, capsys):
    db = _snapshot(tmp_path / "disconnected", {
        "pkg/seed.py": "def alpha():\n    return 1\n",
        "pkg/disconnected.py": "def unrelated():\n    return 2\n",
    })
    assert main(["map", "--db", str(db), "--outline", "--seed", "pkg.seed", "--max-tokens", "2000"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert [file["path"] for file in payload["files"]] == ["pkg/seed.py"]
    assert "omitted" not in payload


def test_outline_api_accepts_each_rank_seed_form(tmp_path):
    db = _snapshot(tmp_path / "seed-forms", {
        "pkg/a.py": "def alpha():\n    return 1\n",
        "pkg/b.py": "def bravo():\n    return 2\n",
    })
    for kwargs in ({"seed_paths": ["pkg/a.py"]}, {"seed_modules": ["pkg.a"]}):
        with GraphStore(db, readonly=True) as store:
            payload = build_repo_outline(store, max_tokens=2000, **kwargs)
        assert [file["path"] for file in payload["files"]] == ["pkg/a.py"]
