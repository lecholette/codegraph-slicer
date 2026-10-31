import pytest

from codegraph_slicer import CodeGraphError, GraphStore, compute_delta
from codegraph_slicer.delta import _cycles


def _write(root, source):
    path = root / "m.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)


def _write_test(root):
    path = root / "tests" / "test_m.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("from m import caller\n\ndef test_caller():\n    assert caller() == 1\n")


def test_line_drift_does_not_change_call_relationship_or_mark_caller_modified(tmp_path):
    base_root, head_root = tmp_path / "base", tmp_path / "head"
    _write(base_root, "def target():\n    return 1\n\ndef caller():\n    return target()\n")
    _write(head_root, "def target():\n    return 1\n\n\n\ndef caller():\n    return target()\n")
    _write_test(base_root)
    _write_test(head_root)
    with GraphStore(":memory:") as base, GraphStore(":memory:") as head:
        base.sync_workspace(base_root)
        head.sync_workspace(head_root)
        result = compute_delta(base, head)

    assert result["added_edges"] == []
    assert result["removed_edges"] == []
    assert "m:caller" not in result["modified_symbols"]
    assert "m:caller" not in result["untested_modified_symbols"]
    assert "tests.test_m:test_caller" not in result["modified_symbols"]
    assert all(not item["updated_or_selected_tests"] for item in result["test_coverage"])


def test_genuine_call_addition_keeps_actual_occurrence_line(tmp_path):
    base_root, head_root = tmp_path / "base", tmp_path / "head"
    _write(base_root, "def target():\n    return 1\n\ndef caller():\n    return 0\n")
    _write(head_root, "def target():\n    return 1\n\ndef caller():\n    return target()\n")
    with GraphStore(":memory:") as base, GraphStore(":memory:") as head:
        base.sync_workspace(base_root)
        head.sync_workspace(head_root)
        result = compute_delta(base, head)

    assert result["added_edges"] == [["m:caller", "m:target", "CALLS", 5]]
    assert result["removed_edges"] == []


def test_selected_test_survives_deleted_product_symbol_and_typo_still_refuses(tmp_path):
    base_root, head_root = tmp_path / "base", tmp_path / "head"
    _write(base_root, "def target():\n    return 1\n")
    _write(head_root, "")
    for root in (base_root, head_root):
        path = root / "tests" / "test_m.py"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "from m import target\n\ndef test_target():\n    assert target() == 1\n"
        )
    with GraphStore(":memory:") as base, GraphStore(":memory:") as head:
        base.sync_workspace(base_root)
        head.sync_workspace(head_root)
        selected = compute_delta(
            base, head, selected_tests=["tests.test_m:test_target"]
        )
        unselected = compute_delta(base, head)
        with pytest.raises(
            CodeGraphError,
            match="unobserved test selection: tests.test_m:test_missing",
        ):
            compute_delta(base, head, selected_tests=["tests.test_m:test_missing"])

    assert "m:target" not in selected["untested_modified_symbols"]
    target_coverage = next(
        item for item in selected["test_coverage"] if item["symbol"] == "m:target"
    )
    assert "tests.test_m:test_target" in target_coverage["reachable_tests"]
    assert target_coverage["updated_or_selected_tests"] == [
        "tests.test_m:test_target"
    ]
    assert "m:target" in unselected["untested_modified_symbols"]


def test_iterative_cycles_match_reachability_oracle_for_three_node_graphs():
    nodes = range(3)
    possible_edges = [(source, target) for source in nodes for target in nodes]
    for mask in range(1 << len(possible_edges)):
        graph = {node: set() for node in nodes}
        for bit, (source, target) in enumerate(possible_edges):
            if mask & (1 << bit):
                graph[source].add(target)

        reachable = {(node, node) for node in nodes}
        reachable.update(
            (source, target)
            for source, targets in graph.items()
            for target in targets
        )
        for via in nodes:
            reachable |= {
                (source, target)
                for source in nodes
                for target in nodes
                if (source, via) in reachable and (via, target) in reachable
            }
        expected = set()
        remaining = set(nodes)
        while remaining:
            node = min(remaining)
            component = {
                other
                for other in nodes
                if (node, other) in reachable and (other, node) in reachable
            }
            if len(component) > 1 or node in graph[node]:
                expected.add(tuple(sorted(component)))
            remaining -= component

        assert _cycles(graph) == expected, f"adjacency mask {mask}"
