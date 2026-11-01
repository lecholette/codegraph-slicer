import subprocess

import pytest

from codegraph_slicer import GraphStore
from codegraph_slicer.cli import main


def _indexed(tmp_path, source):
    root = tmp_path / "src"
    root.mkdir()
    (root / "sample.py").write_text(source)
    db = tmp_path / "graph.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(root)
    return root, db


def _error(capsys, argv, name, message):
    with pytest.raises(SystemExit, match="2") as caught:
        main(argv)
    assert caught.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    lines = captured.err.splitlines()
    assert lines[0] == f"codegraph-slicer: {name}: {message}"
    return lines[1:]


def test_anchor_error_has_three_nearest_anchors_and_snapshot_guidance(tmp_path, capsys):
    root = tmp_path / "src"
    root.mkdir()
    (root / "sample.py").write_text("""def calculate_total():\n    return 1\ndef calculate_tax():\n    return 2\ndef calculate_rate():\n    return 3\ndef calculate_fee():\n    return 4\n""")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "hints@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Hint Fixture"], cwd=root, check=True)
    subprocess.run(["git", "add", "sample.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    revision = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True,
                             capture_output=True, text=True).stdout.strip()
    db = tmp_path / "graph.sqlite"
    with GraphStore(db) as store:
        store.sync_git_tree(root, "HEAD")
    hints = _error(capsys, ["slice", "--db", str(db), "sample:calculate_totals"],
                   "AnchorNotFoundError", "sample:calculate_totals")
    candidates = [line for line in hints if line.startswith("hint: nearest anchor:")]
    assert candidates == ["hint: nearest anchor: sample.py:calculate_total",
                          "hint: nearest anchor: sample.py:calculate_tax",
                          "hint: nearest anchor: sample.py:calculate_rate"]
    assert hints == [*candidates,
                     f"hint: a symbol added on a branch is absent from a base-commit DB (recorded commit: {revision}); query a snapshot containing that symbol"]


def test_flow_query_error_lists_observed_lines_for_variable(tmp_path, capsys):
    _, db = _indexed(tmp_path, "@decorate\ndef calculate(amount):\n    total = (\n        amount * 2\n    )\n    total = (\n        total + 1\n    )\n    total = (\n        total + 1\n    )\n    total = (\n        total + 1\n    )\n    total = (\n        total + 1\n    )\n    return total\n")
    hints = _error(capsys, ["flow", "sample:calculate", "--db", str(db),
                            "--snapshot", "--variable", "total", "--line", "99"],
                   "FlowQueryError", "no flow node matches variable='total', line=99")
    assert hints == ["hint: observed def/use lines for this variable in the selected function: 3, 6, 7, 9, 10"]
    hints = _error(capsys, ["flow", "sample:calculate", "--db", str(db),
                            "--snapshot", "--variable", "total", "--line", "-1"],
                   "FlowQueryError", "line must be a positive integer")
    assert hints == ["hint: observed def/use lines for this variable in the selected function: 3, 6, 7, 9, 10"]


def test_missing_flow_variable_does_not_invent_line_hints(tmp_path, capsys):
    _, db = _indexed(tmp_path, "def calculate(amount):\n    return amount\n")
    hints = _error(capsys, ["flow", "sample:calculate", "--db", str(db),
                            "--snapshot", "--variable", "absent"],
                   "FlowQueryError", "no flow node matches variable='absent', line=None")
    assert hints == []


def test_unsupported_flow_has_slice_hint(tmp_path, capsys):
    _, db = _indexed(tmp_path, "async def calculate(items):\n    return await items\n")
    hints = _error(capsys, ["flow", "sample:calculate", "--db", str(db), "--snapshot"],
                   "UnsupportedFlowError", "unsupported Await in sample.py:2")
    assert hints == ["hint: use slice for this function"]


def test_open_database_operational_error_names_resolved_path(tmp_path, capsys, monkeypatch):
    _indexed(tmp_path, "def calculate():\n    return 1\n")
    monkeypatch.chdir(tmp_path)
    blocker = tmp_path / "blocked-parent"
    blocker.write_text("file, not a directory")
    db = blocker / "graph.sqlite"
    hints = _error(capsys, ["slice", "--db", "blocked-parent/graph.sqlite", "sample:calculate"],
                   "OperationalError", "unable to open database file")
    assert hints == [f"hint: database {db.resolve()}; exists=False, parent_writable=False"]


def test_stale_snapshot_has_no_additional_hint(tmp_path, capsys):
    root, db = _indexed(tmp_path, "def calculate():\n    return 1\n")
    (root / "sample.py").write_text("def calculate():\n    return 2\n")
    hints = _error(capsys, ["slice", "--db", str(db), "sample:calculate"],
                   "StaleSnapshotError", "snapshot is stale: Python source differs at sample.py; run codegraph-slicer sync ROOT --db DB")
    assert hints == []


def test_required_anchor_overflow_has_no_additional_hint(tmp_path, capsys):
    _, db = _indexed(tmp_path, 'def calculate():\n    return "' + "x" * 100 + '"\n')
    hints = _error(capsys, ["slice", "--db", str(db), "sample:calculate", "--max-tokens", "2"],
                   "RequiredAnchorOverflow", "required definitions need 80 tokens including omission metadata, budget is 2")
    assert hints == []
