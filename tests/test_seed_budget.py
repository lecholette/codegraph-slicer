import json

import pytest

from codegraph_slicer import GraphStore
from codegraph_slicer.cli import main


def _synced_fixture(tmp_path):
    root = tmp_path / "seed-threshold"
    root.mkdir()
    sources = {
        "pkg/seed.py": "from pkg.neighbor import bravo\ndef alpha(value: '" + "x" * 240 + "') -> str:\n    return bravo()\n",
        "pkg/neighbor.py": "def bravo():\n    return 1\n",
    }
    for name, source in sources.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    db = tmp_path / "seed-threshold.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(root)
    return db


def test_seed_only_refusal_reports_exact_smallest_admitting_cli_budget(tmp_path, capsys):
    db = _synced_fixture(tmp_path)
    args = ["map", "--db", str(db), "--outline", "--seed", "pkg.seed"]

    # Find the exact threshold by exercising the real CLI against a synced DB.
    threshold = None
    for budget in range(1, 1000):
        try:
            status = main([*args, "--max-tokens", str(budget)])
        except SystemExit as exc:
            assert exc.code == 2
            assert capsys.readouterr().out == ""
        else:
            assert status == 0
            printed = capsys.readouterr().out
            payload = json.loads(printed)
            threshold = budget
            assert payload["tokens"] == len(printed) // 4 + 1 <= threshold
            assert payload.get("omitted")
            break
    assert threshold is not None

    with pytest.raises(SystemExit, match="2") as exc:
        main([*args, "--max-tokens", str(threshold - 1)])
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"rerun with --max-tokens {threshold}" in captured.err
    assert main([*args, "--max-tokens", str(threshold)]) == 0
    admitted = json.loads(capsys.readouterr().out)
    assert admitted["tokens"] == threshold
