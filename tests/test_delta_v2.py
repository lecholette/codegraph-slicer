import json

import pytest

from codegraph_slicer import GraphStore
from codegraph_slicer.cli import main
from codegraph_slicer.cli import _markdown
from codegraph_slicer.delta import project_delta_v2
from codegraph_slicer.slicer import InvalidBudgetError, rendered_tokens


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


def _run(capsys, base, head, *options):
    assert main(["delta", "--base-db", str(base), "--head-db", str(head),
                 "--projection", "v2", *options]) == 0
    return capsys.readouterr().out


def test_legacy_map_and_slice_json_match_base_budget_goldens(tmp_path, capsys):
    db = _snapshot(tmp_path / "legacy", {
        "pkg/a.py": "def alpha():\n    return 1\n",
    })

    assert main(["map", "--db", str(db), "--snapshot", "--max-tokens", "27"]) == 0
    assert capsys.readouterr().out == (
        '{"tokens":27,"items":[{"symbol":"pkg.a:","path":"pkg/a.py",'
        '"text":"pkg/a.py:1\\ndef alpha():\\n    ...\\n"}]}\n'
    )

    assert main([
        "slice", "--db", str(db), "pkg.a:alpha", "--snapshot", "--max-tokens", "51",
    ]) == 0
    assert capsys.readouterr().out == (
        '{"tokens":51,"sections":{"types_and_schemas":[],"upstream_callers":[],'
        '"target_implementations":[{"symbol":"pkg.a:alpha",'
        '"text":"pkg/a.py:1-2\\ndef alpha():\\n    return 1"}],'
        '"downstream_dependencies":[]}}\n'
    )


def test_v2_filters_and_bounded_edge_samples_keep_filtered_totals(tmp_path, capsys):
    base = _snapshot(tmp_path / "base", {
        "pkg/a.py": "def target():\n    return 1\n",
        "pkg/b.py": "def other():\n    return 2\n",
        "pkg/use.py": "def caller():\n    return 0\n",
    })
    head = _snapshot(tmp_path / "head", {
        "pkg/a.py": "def target():\n    return 1\n",
        "pkg/b.py": "def other():\n    return 2\n",
        "pkg/use.py": (
            "from pkg.a import target\n"
            "from pkg.b import other\n"
            "def caller():\n"
            "    target()\n"
            "    target()\n"
            "    return other()\n"
        ),
    })
    text = _run(capsys, base, head, "--anchor", "pkg.use:caller", "--kind", "CALLS",
                "--edge-sample-limit", "1", "--max-tokens", "1600")
    payload = json.loads(text)

    assert payload["schema"] == "python-delta-v2"
    assert payload["filters"] == {"anchors": ["pkg.use:caller"], "kinds": ["CALLS"]}
    assert payload["totals"]["added_edges"] == 2
    assert payload["samples"]["added_edges"] == [["pkg.use:caller", "pkg.a:target", "CALLS", 4]]
    assert payload["omitted"]["added_edges"] == 1
    assert payload["totals"]["import_changes"] == 0
    assert payload["serialized_tokens"] == len(text) // 4 + 1
    assert payload["serialized_tokens"] <= 1600

    assert main(["delta", "--base-db", str(base), "--head-db", str(head), "--full",
                 "--max-tokens", "100000"]) == 0
    legacy = json.loads(capsys.readouterr().out)
    assert [edge for edge in legacy["added_edges"] if edge[2] == "CALLS"] == [
        ["pkg.use:caller", "pkg.a:target", "CALLS", 5],
        ["pkg.use:caller", "pkg.b:other", "CALLS", 6],
    ]

    category_text = _run(
        capsys, base, head, "--anchor", "pkg.use:caller", "--kind", "added_edges",
        "--edge-sample-limit", "10", "--max-tokens", "1600",
    )
    category_payload = json.loads(category_text)
    assert category_payload["totals"]["added_edges"] == 2
    assert [edge[2] for edge in category_payload["samples"]["added_edges"]] == [
        "CALLS", "CALLS"
    ]


def test_v2_import_projection_and_markdown_are_versioned_and_budgeted(tmp_path, capsys):
    base = _snapshot(tmp_path / "base", {
        "pkg/a.py": "def old():\n    return 1\n",
        "pkg/use.py": "from pkg.a import old\n",
    })
    head = _snapshot(tmp_path / "head", {
        "pkg/a.py": "def new():\n    return 1\n",
        "pkg/use.py": "from pkg.a import new\n",
    })
    markdown = _run(capsys, base, head, "--anchor", "pkg.use:", "--kind", "IMPORTS",
                    "--edge-sample-limit", "1", "--format", "markdown", "--max-tokens", "1600")
    assert markdown.startswith("# Static delta report (python-delta-v2)")
    assert "- **import changes:** 2" in markdown
    assert "Omitted **import changes** records: 1" in markdown
    assert "Estimated compact JSON tokens:" in markdown
    assert rendered_tokens(markdown.rstrip("\n")) <= 1600

    json_text = _run(capsys, base, head, "--anchor", "pkg.use:", "--kind", "IMPORTS",
                     "--edge-sample-limit", "1", "--max-tokens", "1600")
    payload = json.loads(json_text)
    assert payload["totals"]["import_changes"] == 2
    assert payload["totals"]["added_edges"] == 0
    assert payload["totals"]["removed_edges"] == 0
    assert payload["samples"]["import_changes"] == [
        {"change": "added", "source": "pkg.use:", "target": "pkg.a:new", "line": 1},
    ]
    with pytest.raises(SystemExit, match="2") as error:
        main(["delta", "--base-db", str(base), "--head-db", str(head), "--projection", "v2", "--full"])
    assert error.value.code == 2
    with pytest.raises(SystemExit, match="2") as error:
        main(["delta", "--base-db", str(base), "--head-db", str(head),
              "--projection", "legacy", "--anchor", "pkg.use:"])
    assert error.value.code == 2


def test_v2_markdown_shows_test_selection_and_static_gap_note():
    markdown = _markdown({
        "schema": "python-delta-v2",
        "filters": {"anchors": [], "kinds": []},
        "totals": {}, "samples": {}, "omitted": {},
        "test_selection": "selected",
        "test_gap_note": "Static TESTS reachability only; tests were not executed.",
        "serialized_tokens": 1,
    })

    assert "Test selection requested: `selected`." in markdown
    assert "Static TESTS reachability only; tests were not executed." in markdown


def test_v2_edge_sample_limit_must_be_at_least_one(tmp_path, capsys):
    with pytest.raises(InvalidBudgetError, match="positive integer"):
        project_delta_v2({}, edge_sample_limit=0)
    base = _snapshot(tmp_path / "base", {"pkg/a.py": "VALUE = 1\n"})
    head = _snapshot(tmp_path / "head", {"pkg/a.py": "VALUE = 1\n"})
    with pytest.raises(SystemExit) as error:
        main(["delta", "--base-db", str(base), "--head-db", str(head),
              "--projection", "v2", "--edge-sample-limit", "0"])
    assert error.value.code == 2
    assert "--edge-sample-limit must be at least 1" in capsys.readouterr().err


def test_v2_projection_deduplicates_identical_import_records():
    result = {
        "added_edges": [
            ["pkg.use:", "pkg.a:target", "IMPORTS", 3],
            ["pkg.use:", "pkg.a:target", "IMPORTS", 3],
        ],
        "removed_edges": [],
        "boundary_violations": [],
        "new_cycles": [],
        "complexity_drift": [],
        "modified_symbols": [],
        "affected_entrypoints": [],
        "untested_modified_symbols": [],
        "test_coverage": [],
        "test_gap_note": "static only",
    }

    payload = project_delta_v2(result, max_tokens=1000, kinds=["IMPORTS"])

    assert payload["totals"]["import_changes"] == 1
    assert payload["totals"]["added_edges"] == 0
    assert payload["samples"]["import_changes"] == [
        {"change": "added", "source": "pkg.use:", "target": "pkg.a:target", "line": 3}
    ]
