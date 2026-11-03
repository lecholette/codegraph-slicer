import json

from codegraph_slicer import GraphStore
from codegraph_slicer.cli import main

SOURCE = """\
def leaf():
    return 1

def one():
    return leaf()

def two():
    return one()

def three():
    return two()

def noisy():
    return leaf() + missing()

def cycle_left():
    return cycle_right()

def cycle_right():
    return cycle_left()

def branch_target():
    return 2

def branch_alpha():
    return branch_target()

def branch_beta():
    return branch_target()

def branch_parent_alpha():
    return branch_alpha()

def branch_parent_beta():
    return branch_beta()

def branch_grandparent():
    return branch_parent_alpha()

def unrelated():
    return never_observed()

def converged_root():
    return 1

def converged_a():
    return converged_root()

def converged_b():
    return converged_root()

def converged_x():
    return converged_a()

def converged_y():
    return converged_x() + converged_b()
"""


def _db(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "impactmod.py").write_text(SOURCE)
    db = tmp_path / "graph.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(root)
    return db


def _impact(db, capsys, anchor, *options):
    assert main(["impact", anchor, "--db", str(db), "--snapshot", *options]) == 0
    return json.loads(capsys.readouterr().out)


def test_impact_walks_three_caller_hops_with_witness_edges(tmp_path, capsys):
    result = _impact(
        _db(tmp_path), capsys, "impactmod:leaf", "--max-depth", "3", "--max-nodes", "20"
    )
    assert result["profile"] == "python-call-impact-v1"
    callers = {entry["symbol"]: entry["depth"] for entry in result["callers"]}
    assert callers == {
        "impactmod:one": 1,
        "impactmod:two": 2,
        "impactmod:three": 3,
        "impactmod:noisy": 1,
    }
    witnesses = {entry["symbol"]: entry["witness"] for entry in result["callers"]}
    assert (witnesses["impactmod:one"]["to"], witnesses["impactmod:one"]["kind"]) == (
        "impactmod:leaf", "CALLS"
    )
    assert (witnesses["impactmod:two"]["to"], witnesses["impactmod:two"]["kind"]) == (
        "impactmod:one", "CALLS"
    )
    assert (witnesses["impactmod:three"]["to"], witnesses["impactmod:three"]["kind"]) == (
        "impactmod:two", "CALLS"
    )
    assert all(edge["line"] > 0 for edge in witnesses.values())


def test_impact_marks_depth_and_node_bound_truncation(tmp_path, capsys):
    db = _db(tmp_path)
    shallow = _impact(db, capsys, "impactmod:leaf", "--max-depth", "1")
    assert all(entry["depth"] == 1 for entry in shallow["callers"])
    assert shallow["truncated"] is True
    bounded = _impact(
        db, capsys, "impactmod:leaf", "--max-depth", "3", "--max-nodes", "1"
    )
    assert len(bounded["callers"]) == 1
    assert len(bounded["callers"]) == 1
    assert bounded["callers"][0]["witness"]["kind"] == "CALLS"
    assert bounded["truncated"] is True


def test_impact_keeps_sibling_callers_when_one_branch_hits_depth_bound(tmp_path, capsys):
    result = _impact(
        _db(tmp_path), capsys, "impactmod:branch_target", "--max-depth", "2"
    )
    assert [(item["symbol"], item["depth"]) for item in result["callers"]] == [
        ("impactmod:branch_alpha", 1),
        ("impactmod:branch_beta", 1),
        ("impactmod:branch_parent_alpha", 2),
        ("impactmod:branch_parent_beta", 2),
    ]
    assert result["truncated"] is True


def test_impact_does_not_mark_depth_truncated_when_paths_converge(tmp_path, capsys):
    result = _impact(
        _db(tmp_path), capsys, "impactmod:converged_root", "--max-depth", "2"
    )
    assert {item["symbol"] for item in result["callers"]} == {
        "impactmod:converged_a",
        "impactmod:converged_b",
        "impactmod:converged_x",
        "impactmod:converged_y",
    }
    assert result["truncated"] is False


def test_impact_cycle_terminates_without_returning_anchor_as_caller(tmp_path, capsys):
    result = _impact(
        _db(tmp_path), capsys, "impactmod:cycle_left", "--max-depth", "8", "--max-nodes", "20"
    )
    assert result["callers"] == [{
        "symbol": "impactmod:cycle_right",
        "depth": 1,
        "witness": {
            "from": "impactmod:cycle_right",
            "to": "impactmod:cycle_left",
            "kind": "CALLS",
            "line": 20,
        },
    }]


def test_impact_reports_unresolved_callee_boundary(tmp_path, capsys):
    result = _impact(
        _db(tmp_path), capsys, "impactmod:leaf", "--unknown-boundaries", "20"
    )
    boundaries = {(item["from"], item["target"], item["kind"]) for item in result["unknown_boundaries"]}
    assert ("impactmod:noisy", "?missing", "CALLS") in boundaries
    assert ("impactmod:unrelated", "?never_observed", "CALLS") in boundaries
    assert result["unknown_boundary_count"] == 2
    assert result["unknown_boundary_scope"] == "indexed_snapshot; unresolved calls are not linked to the anchor"


def test_impact_embeds_witness_and_summarizes_unknowns_by_default(tmp_path, capsys):
    result = _impact(
        _db(tmp_path), capsys, "impactmod:leaf", "--max-depth", "1", "--max-nodes", "3"
    )
    caller = next(item for item in result["callers"] if item["symbol"] == "impactmod:one")
    assert caller == {
        "symbol": "impactmod:one",
        "depth": 1,
        "witness": {
            "from": "impactmod:one",
            "to": "impactmod:leaf",
            "kind": "CALLS",
            "line": 5,
        },
    }
    assert result["unknown_boundary_count"] == 2
    assert result["unknown_boundary_scope"] == (
        "indexed_snapshot; unresolved calls are not linked to the anchor"
    )
    assert result["unknown_boundaries"] == []
    assert result["unknown_boundaries_omitted"] == 2


def test_impact_unknown_boundary_sample_has_independent_bound(tmp_path, capsys):
    result = _impact(
        _db(tmp_path), capsys, "impactmod:leaf", "--max-nodes", "1",
        "--unknown-boundaries", "2",
    )
    assert len(result["callers"]) == 1
    assert result["callers"][0]["witness"]["to"] == "impactmod:leaf"
    assert result["unknown_boundary_count"] == 2
    assert result["unknown_boundaries"] == [
        {
            "from": "impactmod:noisy",
            "target": "?missing",
            "kind": "CALLS",
            "line": 14,
            "reason": "external, dynamic, shadowed, or unobserved reference",
        },
        {
            "from": "impactmod:unrelated",
            "target": "?never_observed",
            "kind": "CALLS",
            "line": 41,
            "reason": "external, dynamic, shadowed, or unobserved reference",
        },
    ]
    assert result["unknown_boundaries_omitted"] == 0
