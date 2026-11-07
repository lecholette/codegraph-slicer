import json
import xml.etree.ElementTree as ET

import pytest

from codegraph_slicer import GraphStore
from codegraph_slicer.cli import _scale_recommendation, main
from codegraph_slicer.slicer import rendered_tokens


class _DB:
    def __init__(self, skipped=None):
        self.skipped = json.dumps(skipped or {})

    def execute(self, sql):
        return _Row(self.skipped)


class _Row:
    def __init__(self, value):
        self.value = value

    def fetchone(self):
        return (self.value,)


class _Store:
    def __init__(self, files, skipped=None):
        self.files, self.db = files, _DB(skipped)

    def rows(self, sql):
        return [{"path": path, "loc": loc} for path, loc in self.files]


def _rec(files, skipped=None):
    return _scale_recommendation(_Store(files, skipped))


def test_scale_thresholds_are_and_boundaries_and_density_aware():
    assert _rec([(f"m{i}.js", 1) for i in range(28)] + [("tail.js", 4971)])["reason"] == "compact_scale"
    assert _rec([(f"m{i}.js", 1) for i in range(29)] + [("tail.js", 4970)])["reason"] == "graph_candidate"
    assert _rec([(f"m{i}.js", 1) for i in range(28)] + [("tail.js", 4972)])["reason"] == "graph_candidate"
    assert _rec([(f"m{i}.py", 1) for i in range(48)] + [("tail.py", 7451)])["reason"] == "compact_python"
    assert _rec([(f"m{i}.py", 1) for i in range(49)] + [("tail.py", 7450)])["reason"] == "graph_candidate"
    assert _rec([(f"m{i}.py", 1) for i in range(48)] + [("tail.py", 7452)])["reason"] == "graph_candidate"
    dense = _rec([(f"m{i}.py", 2000) for i in range(10)])
    assert dense["mode"] == "graph_optional" and dense["source_loc"] == 20000


def test_empty_test_only_mixed_and_incomplete_indexes():
    assert _rec([("tests/test_a.py", 10)])["reason"] == "empty_index"
    assert _rec([])["mode"] == "direct_read"
    mixed = _rec([("a.py", 100), ("b.tsx", 100)])
    assert mixed["languages"] == ["python", "typescript"]
    assert mixed["reason"] == "compact_scale"
    incomplete = _rec([("a.py", 2)], {"bad.py": "sha256:x"})
    assert (incomplete["mode"], incomplete["reason"], incomplete["source_files"]) == ("direct_read", "incomplete_index", 1)


def _database(tmp_path, skip=False):
    root = tmp_path / "src"
    root.mkdir()
    (root / "app.py").write_text("def run():\n    return 1\n")
    if skip:
        (root / "bad.py").write_text("def broken(:\n")
    db = tmp_path / "index.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(root, skip_unparseable=skip)
    return db


@pytest.mark.parametrize("outline", [False, True])
@pytest.mark.parametrize("fmt", ["json", "markdown", "xml"])
def test_map_recommendation_formats_and_legacy_parity(tmp_path, capsys, outline, fmt):
    db = _database(tmp_path)
    args = ["map", "--db", str(db), "--format", fmt]
    if outline:
        args.append("--outline")
    assert main(args) == 0
    legacy = capsys.readouterr().out
    assert main([*args, "--recommend-workflow"]) == 0
    recommended = capsys.readouterr().out
    assert "ScaleRecommendation" not in legacy and "scale_recommendation" not in legacy
    if fmt == "json":
        payload = json.loads(recommended)
        assert payload["scale_recommendation"]["mode"] == "map_then_read"
        reported_tokens = payload["tokens"]
        assert reported_tokens <= 2000
    elif fmt == "markdown":
        assert "ScaleRecommendation:" in recommended
        import re
        reported_tokens = int(re.search(r"Estimated context tokens: (\d+)", recommended).group(1))
    else:
        root = ET.fromstring(recommended)
        assert root.find("ScaleRecommendation") is not None
        reported_tokens = int(root.attrib["tokens"])
        assert root.find("ScaleRecommendation").attrib["bypass_slicer"] == "true"
    assert reported_tokens == rendered_tokens(recommended.rstrip("\n"))


def test_skipped_parse_is_advised_as_incomplete_and_advisory_is_budgeted(tmp_path, capsys):
    db = _database(tmp_path, skip=True)
    assert main(["map", "--db", str(db), "--recommend-workflow"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["scale_recommendation"]["reason"] == "incomplete_index"
    assert payload["scale_recommendation"]["source_files"] == 1
    with pytest.raises(SystemExit) as error:
        main(["map", "--db", str(db), "--recommend-workflow", "--max-tokens", "1"])
    assert error.value.code == 2
    assert "budget" in capsys.readouterr().err
    assert main(["map", "--db", str(db)]) == 0
    baseline = json.loads(capsys.readouterr().out)["tokens"]
    with pytest.raises(SystemExit):
        main(["map", "--db", str(db), "--recommend-workflow", "--max-tokens", str(baseline)])
    assert "budget" in capsys.readouterr().err


def test_tight_budget_keeps_a_subset_with_recommendation(tmp_path, capsys):
    root = tmp_path / "many"
    root.mkdir()
    for index in range(8):
        (root / f"m{index}.py").write_text(
            "".join(f"def f{item}():\n    return {item}\n\n" for item in range(30))
        )
    db = tmp_path / "many.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(root)

    assert main(["map", "--db", str(db), "--recommend-workflow", "--max-tokens", "270"]) == 0
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert len(payload["items"]) == 1
    assert payload["omitted"]["files"] == 7
    assert payload["scale_recommendation"]["mode"] == "map_then_read"
    assert payload["tokens"] == rendered_tokens(output.rstrip("\n"))
    assert payload["tokens"] <= 270
