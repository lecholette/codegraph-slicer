import pytest

from codegraph_slicer.flow import (
    FlowQueryError,
    UnsupportedFlowError,
    analyze_flow,
    slice_flow,
)


def test_while_natural_exit_connects_following_return():
    graph = analyze_flow(
        "def f(condition):\n    while condition:\n        condition = False\n    return 1\n",
        "f",
        path="round1.py",
    )
    predicate = next(n for n in graph["nodes"] if n["kind"] == "predicate")
    returned = next(n for n in graph["nodes"] if n["kind"] == "return")
    assert returned["reachable"] is True
    assert any(
        edge["kind"] == "CFG"
        and edge["source"] == predicate["id"]
        and edge["target"] == returned["id"]
        and edge["label"] == "false"
        for edge in graph["edges"]
    )


def test_break_after_inner_loop_in_outer_if_arm_bypasses_outer_else():
    graph = analyze_flow(
        "def f(outer, inner, stop):\n    for item in outer:\n        if stop:\n            for nested in inner:\n                pass\n            break\n    else:\n        completed = 1\n    after = 2\n    return after\n",
        "f",
        path="round1.py",
    )
    breaker = next(n for n in graph["nodes"] if n["kind"] == "break")
    completed = next(n for n in graph["nodes"] if n["text"] == "completed = 1")
    after = next(n for n in graph["nodes"] if n["text"] == "after = 2")
    targets = {
        edge["target"]
        for edge in graph["edges"]
        if edge["kind"] == "CFG"
        and edge["source"] == breaker["id"]
        and edge["label"] == "break"
    }
    assert targets == {after["id"]}
    assert completed["id"] not in targets


def test_if_else_definitions_control_branches_but_not_merged_return():
    graph = analyze_flow(
        "def f(flag):\n    if flag:\n        value = 1\n    else:\n        value = 2\n    return value\n",
        "f",
        path="round1.py",
    )
    predicate = next(n for n in graph["nodes"] if n["kind"] == "predicate")
    true_def = next(n for n in graph["nodes"] if n["text"] == "value = 1")
    false_def = next(n for n in graph["nodes"] if n["text"] == "value = 2")
    returned = next(n for n in graph["nodes"] if n["kind"] == "return")
    controls = {
        edge["target"]: edge["label"]
        for edge in graph["edges"]
        if edge["kind"] == "CONTROL_DEP" and edge["source"] == predicate["id"]
    }
    assert controls == {true_def["id"]: "true", false_def["id"]: "false"}
    assert returned["id"] not in controls


def test_root_rebinding_kills_old_receiver_access_path():
    graph = analyze_flow(
        "def f(value):\n    value['a'] = 1\n    value = {}\n    return value['a']\n",
        "f",
        path="round2.py",
    )
    old_write = next(n for n in graph["nodes"] if n["text"] == "value['a'] = 1")
    rebind = next(n for n in graph["nodes"] if n["text"] == "value = {}")
    returned = next(n for n in graph["nodes"] if n["kind"] == "return")
    data = [
        edge
        for edge in graph["edges"]
        if edge["kind"] == "DATA_DEP" and edge["target"] == returned["id"]
    ]
    assert not any(edge["source"] == old_write["id"] for edge in data)
    assert any(
        edge["source"] == rebind["id"] and edge["label"] == "value" for edge in data
    )
    assert any(
        diagnostic["code"] == "unknown-live-in" and diagnostic["node"] == returned["id"]
        for diagnostic in graph["diagnostics"]
    )


def test_subpath_rebinding_kills_old_nested_member_access_path():
    graph = analyze_flow(
        "def f(root):\n    root['a']['leaf'] = 1\n    root['a'] = {}\n    return root['a']['leaf']\n",
        "f",
        path="round2.py",
    )
    old_write = next(n for n in graph["nodes"] if n["text"] == "root['a']['leaf'] = 1")
    returned = next(n for n in graph["nodes"] if n["kind"] == "return")
    assert not any(
        edge["kind"] == "DATA_DEP"
        and edge["source"] == old_write["id"]
        and edge["target"] == returned["id"]
        for edge in graph["edges"]
    )


def test_named_expression_read_after_definition_has_intra_statement_flow():
    graph = analyze_flow(
        "def f():\n    result = (value := 1) + value\n    return result\n",
        "f",
        path="round2.py",
    )
    assignment = next(
        n for n in graph["nodes"] if n["text"] == "result = (value := 1) + value"
    )
    assert any(
        edge["kind"] == "DATA_DEP"
        and edge["source"] == assignment["id"]
        and edge["target"] == assignment["id"]
        and edge["label"] == "value"
        for edge in graph["edges"]
    )
    assert not any(
        diagnostic["code"] == "possibly-unbound-local"
        and diagnostic["node"] == assignment["id"]
        and "value" in diagnostic["message"]
        for diagnostic in graph["diagnostics"]
    )


def test_named_expression_after_a_read_keeps_unbound_warning():
    graph = analyze_flow(
        "def f():\n    result = value + (value := 1) + value\n    return result\n",
        "f",
        path="round2.py",
    )
    assignment = next(
        n
        for n in graph["nodes"]
        if n["text"] == "result = value + (value := 1) + value"
    )
    assert any(
        diagnostic["code"] == "possibly-unbound-local"
        and diagnostic["node"] == assignment["id"]
        and "value" in diagnostic["message"]
        for diagnostic in graph["diagnostics"]
    )


def test_named_expression_post_store_read_does_not_depend_on_old_definition():
    graph = analyze_flow(
        "def f(seed):\n    value = seed\n    result = (value := 1) + value\n    return result\n",
        "f",
        path="round2.py",
    )
    old = next(n for n in graph["nodes"] if n["text"] == "value = seed")
    assignment = next(
        n for n in graph["nodes"] if n["text"] == "result = (value := 1) + value"
    )
    assert any(
        edge["kind"] == "DATA_DEP"
        and edge["source"] == assignment["id"]
        and edge["target"] == assignment["id"]
        and edge["label"] == "value"
        for edge in graph["edges"]
    )
    assert not any(
        edge["kind"] == "DATA_DEP"
        and edge["source"] == old["id"]
        and edge["target"] == assignment["id"]
        and edge["label"] == "value"
        for edge in graph["edges"]
    )


def test_ordinary_assignment_reads_old_value():
    graph = analyze_flow(
        "def f(seed):\n    value = seed\n    value = value + 1\n    return value\n",
        "f",
        path="round2.py",
    )
    old = next(n for n in graph["nodes"] if n["text"] == "value = seed")
    update = next(n for n in graph["nodes"] if n["text"] == "value = value + 1")
    assert any(
        edge["kind"] == "DATA_DEP"
        and edge["source"] == old["id"]
        and edge["target"] == update["id"]
        and edge["label"] == "value"
        for edge in graph["edges"]
    )


def test_augmented_assignment_reads_old_value():
    graph = analyze_flow(
        "def f(seed):\n    count = seed\n    count += 1\n    return count\n",
        "f",
        path="round2.py",
    )
    old = next(n for n in graph["nodes"] if n["text"] == "count = seed")
    update = next(n for n in graph["nodes"] if n["kind"] == "augassign")
    assert any(
        edge["kind"] == "DATA_DEP"
        and edge["source"] == old["id"]
        and edge["target"] == update["id"]
        and edge["label"] == "count"
        for edge in graph["edges"]
    )


def test_overlapping_assignment_target_refuses():
    with pytest.raises(
        UnsupportedFlowError, match=r"overlapping assignment target.*round2.py:2"
    ):
        analyze_flow(
            "def f(value):\n    value.field = value = object()\n",
            "f",
            path="round2.py",
        )


def test_augmented_access_target_refuses_receiver_rebinding_in_rhs():
    with pytest.raises(
        UnsupportedFlowError, match=r"overlapping augmented target.*round2.py:2"
    ):
        analyze_flow(
            "def f(value, other):\n    value.field += (value := other)\n",
            "f",
            path="round2.py",
        )


def test_data_only_omits_control_edges_from_full_and_selected_reports():
    graph = analyze_flow(
        "def f(flag):\n    if flag:\n        value = flag\n    else:\n        value = flag\n    return value\n",
        "f",
        path="round3.py",
    )
    full = slice_flow(graph, include_control=False, max_tokens=16000)["flow"]
    selected = slice_flow(
        graph,
        variable="flag",
        direction="forward",
        include_control=False,
        max_tokens=16000,
    )["flow"]
    assert all(edge["kind"] != "CONTROL_DEP" for edge in full["edges"])
    assert all(edge["kind"] != "CONTROL_DEP" for edge in selected["edges"])


@pytest.mark.parametrize("line", [True, 0, -1, "2"])
def test_line_selection_requires_positive_nonbool_integer(line):
    graph = analyze_flow("def f():\n    return 1\n", "f", path="round3.py")
    with pytest.raises(FlowQueryError, match="line must be a positive integer"):
        slice_flow(graph, line=line)


def test_async_selected_function_uses_unsupported_error_with_location():
    with pytest.raises(UnsupportedFlowError, match=r"AsyncFunctionDef.*round3.py:1"):
        analyze_flow("async def f():\n    return 1\n", "f", path="round3.py")
