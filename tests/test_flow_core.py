import os
import subprocess
import sys

import pytest

from codegraph_slicer.flow import (
    FlowBudgetExceeded,
    UnsupportedFlowError,
    analyze_flow,
    slice_flow,
)


def test_branch_join_keeps_both_definitions_and_backward_slice():
    graph = analyze_flow(
        "def choose(flag):\n    if flag:\n        value = 10\n    else:\n        value = 20\n    return value\n",
        "choose",
        path="example.py",
    )
    ret = next(n for n in graph["nodes"] if n["kind"] == "return")
    reaching = {
        e["source"]
        for e in graph["edges"]
        if e["kind"] == "DATA_DEP"
        and e["target"] == ret["id"]
        and e["label"] == "value"
    }
    assert {graph["nodes"][int(n[1:])]["text"] for n in reaching} == {
        "value = 10",
        "value = 20",
    }
    sliced = slice_flow(graph, variable="value", line=ret["line"])
    assert sliced["flow"]["query"] == {
        "variable": "value",
        "line": ret["line"],
        "direction": "backward",
        "include_control": True,
    }
    assert {n["text"] for n in sliced["flow"]["nodes"]} >= {
        "value = 10",
        "value = 20",
        "return value",
    }


def test_false_early_return_guard_controls_continuation():
    graph = analyze_flow(
        "def f(flag):\n    if flag:\n        return 1\n    result = 2\n    return result\n",
        "f",
        path="example.py",
    )
    predicate = next(n for n in graph["nodes"] if n["kind"] == "predicate")
    continuation = next(n for n in graph["nodes"] if n["text"] == "result = 2")
    assert {
        e["target"]
        for e in graph["edges"]
        if e["kind"] == "CONTROL_DEP"
        and e["source"] == predicate["id"]
        and e["label"] == "false"
    } == {
        continuation["id"],
        next(n["id"] for n in graph["nodes"] if n["text"] == "return result"),
    }


def test_partial_branch_definition_keeps_missing_path_diagnostic():
    graph = analyze_flow(
        "def f(flag):\n    if flag:\n        value = 1\n    return value\n",
        "f",
        path="example.py",
    )
    ret = next(n for n in graph["nodes"] if n["kind"] == "return")
    assert any(
        d["code"] == "possibly-unbound-local"
        and d["node"] == ret["id"]
        and "value" in d["message"]
        for d in graph["diagnostics"]
    )


def test_slice_budget_refuses_complete_causal_report():
    graph = analyze_flow(
        "def f(x):\n    y = x + 1\n    return y\n", "f", path="example.py"
    )
    with pytest.raises(FlowBudgetExceeded, match="needs"):
        slice_flow(graph, variable="y", max_tokens=1)


def test_loop_carried_definition_and_zero_iteration_target_diagnostic():
    graph = analyze_flow(
        "def f(items, total):\n    for item in items:\n        total += item\n    return item\n",
        "f",
        path="example.py",
    )
    aug = next(n for n in graph["nodes"] if n["kind"] == "augassign")
    item_return = next(n for n in graph["nodes"] if n["kind"] == "return")
    item_in = {
        e["source"]
        for e in graph["edges"]
        if e["kind"] == "DATA_DEP"
        and e["target"] == aug["id"]
        and e["label"] == "total"
    }
    assert {graph["nodes"][int(n[1:])]["kind"] for n in item_in} == {
        "parameter",
        "augassign",
    }
    assert any(
        d["code"] == "possibly-unbound-local"
        and d["node"] == item_return["id"]
        and "item" in d["message"]
        for d in graph["diagnostics"]
    )


def test_tuple_reaching_defs_and_ordered_loop_value_provenance():
    graph = analyze_flow(
        "def f(items):\n    a, b = 1, 2\n    a = 3\n    for item in items:\n        first = item\n        second = first\n    return b + second\n",
        "f",
        path="example.py",
    )
    ret = next(n for n in graph["nodes"] if n["kind"] == "return")
    return_deps = {
        e["label"]: e["source"]
        for e in graph["edges"]
        if e["kind"] == "DATA_DEP" and e["target"] == ret["id"]
    }
    assert graph["nodes"][int(return_deps["b"][1:])]["text"] == "a, b = 1, 2"
    first = next(n for n in graph["nodes"] if n["text"] == "first = item")
    second = next(n for n in graph["nodes"] if n["text"] == "second = first")
    assert any(
        e["kind"] == "DATA_DEP"
        and e["target"] == first["id"]
        and graph["nodes"][int(e["source"][1:])]["kind"] == "target_binding"
        for e in graph["edges"]
    )
    assert any(
        e["kind"] == "DATA_DEP"
        and e["source"] == first["id"]
        and e["target"] == second["id"]
        and e["label"] == "first"
        for e in graph["edges"]
    )
    bare = slice_flow(graph, max_tokens=10000)["flow"]
    assert any(e["kind"] == "CFG" for e in bare["edges"])


def test_break_bypasses_loop_else_and_reaches_following_statement():
    graph = analyze_flow(
        "def f(items, stop):\n    for item in items:\n        if stop:\n            break\n    else:\n        completed = 1\n    after = 2\n    return after\n",
        "f",
        path="example.py",
    )
    breaker = next(n for n in graph["nodes"] if n["kind"] == "break")
    completed = next(n for n in graph["nodes"] if n["text"] == "completed = 1")
    after = next(n for n in graph["nodes"] if n["text"] == "after = 2")
    break_edges = [
        e
        for e in graph["edges"]
        if e["kind"] == "CFG" and e["source"] == breaker["id"] and e["label"] == "break"
    ]
    assert [e["target"] for e in break_edges] == [after["id"]]
    assert all(e["target"] != completed["id"] for e in break_edges)


def test_access_path_reads_and_dynamic_writes_remain_alias_conservative():
    graph = analyze_flow(
        "def f(obj, items, index):\n    before = obj.value\n    obj.value = 3\n    obj.value += 1\n    early = items[index]\n    items[index] = 7\n    later = items[0]\n    return before + obj.value + early + later\n",
        "f",
        path="example.py",
    )
    update = next(n for n in graph["nodes"] if n["text"] == "obj.value += 1")
    before = next(n for n in graph["nodes"] if n["text"] == "before = obj.value")
    early = next(n for n in graph["nodes"] if n["text"] == "early = items[index]")
    later = next(n for n in graph["nodes"] if n["text"] == "later = items[0]")
    update_in = {
        e["source"]
        for e in graph["edges"]
        if e["kind"] == "DATA_DEP"
        and e["target"] == update["id"]
        and e["label"] == "obj.value"
    }
    assert any(
        graph["nodes"][int(source[1:])]["text"] == "obj.value = 3"
        for source in update_in
    )
    assert "obj" not in update["defs"]
    assert any(
        d["code"] == "unknown-live-in" and d["node"] == before["id"]
        for d in graph["diagnostics"]
    )
    early_source = next(n for n in graph["nodes"] if n["text"] == "items[index] = 7")
    assert not any(
        e["kind"] == "DATA_DEP"
        and e["source"] == early_source["id"]
        and e["target"] == early["id"]
        for e in graph["edges"]
    )
    assert any(
        e["kind"] == "DATA_DEP"
        and e["source"] == early_source["id"]
        and e["target"] == later["id"]
        and e["label"] == "items[*]"
        for e in graph["edges"]
    )
    assert any(d["code"] == "dynamic-index-write" for d in graph["diagnostics"])
    assert len(graph["engine_sha256"]) == 64 and graph["python_version"]


def test_nested_loop_break_targets_outer_following_statement():
    graph = analyze_flow(
        "def f(outer, inner):\n    for left in outer:\n        for right in inner:\n            pass\n        break\n    done = 1\n    return done\n",
        "f",
        path="example.py",
    )
    breaker = next(n for n in graph["nodes"] if n["kind"] == "break")
    done = next(n for n in graph["nodes"] if n["text"] == "done = 1")
    assert any(
        e["kind"] == "CFG"
        and e["source"] == breaker["id"]
        and e["target"] == done["id"]
        and e["label"] == "break"
        for e in graph["edges"]
    )


def test_abrupt_flow_and_assertion_failure_edges():
    graph = analyze_flow(
        "def f(flag):\n    if flag:\n        raise ValueError()\n    assert flag\n    return 1\n    unreachable = 2\n",
        "f",
        path="example.py",
    )
    raise_node = next(n for n in graph["nodes"] if n["kind"] == "raise")
    assertion = next(n for n in graph["nodes"] if n["kind"] == "assert")
    unreachable = next(n for n in graph["nodes"] if n["text"] == "unreachable = 2")
    assert any(
        e["kind"] == "CFG"
        and e["source"] == raise_node["id"]
        and e["label"] == "abrupt-exit"
        for e in graph["edges"]
    )
    assert any(
        e["kind"] == "CFG"
        and e["source"] == assertion["id"]
        and e["label"] == "assert-failure"
        for e in graph["edges"]
    )
    assert unreachable["reachable"] is False


def test_annotation_only_is_not_a_value_definition():
    annotated = analyze_flow(
        "def f():\n    value: int\n    return value\n",
        "f",
        path="example.py",
    )
    ret = next(n for n in annotated["nodes"] if n["kind"] == "return")
    assert (
        "value"
        not in next(n for n in annotated["nodes"] if n["kind"] == "annassign")["defs"]
    )
    assert any(
        d["code"] == "possibly-unbound-local" and d["node"] == ret["id"]
        for d in annotated["diagnostics"]
    )


def test_for_iterable_walrus_definition_reaches_after_loop():
    iterable = analyze_flow(
        "def g(source):\n    for value in (items := source):\n        pass\n    return items\n",
        "g",
        path="example.py",
    )
    item_init = next(n for n in iterable["nodes"] if n["kind"] == "iterator_init")
    ret_items = next(n for n in iterable["nodes"] if n["kind"] == "return")
    assert "items" in item_init["defs"]
    assert any(
        e["kind"] == "DATA_DEP"
        and e["source"] == item_init["id"]
        and e["target"] == ret_items["id"]
        and e["label"] == "items"
        for e in iterable["edges"]
    )


def test_conditionally_evaluated_named_expression_refuses():
    with pytest.raises(
        UnsupportedFlowError, match=r"conditional named expression.*example.py:2"
    ):
        analyze_flow(
            "def f(left, right, other):\n    if left < right < (value := other):\n        return value\n    return None\n",
            "f",
            path="example.py",
        )


def test_flow_json_is_stable_across_hash_seeds():
    source = "def f(flag):\n    if flag:\n        value = 1\n    else:\n        value = 2\n    return value\n"
    script = (
        "import json; from codegraph_slicer.flow import analyze_flow; "
        f"print(json.dumps(analyze_flow({source!r}, 'f', path='example.py'), "
        "sort_keys=True, separators=(',', ':')))"
    )
    outputs = []
    for seed in ("13", "47"):
        env = os.environ.copy()
        env["PYTHONHASHSEED"] = seed
        result = subprocess.run(
            [sys.executable, "-c", script],
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        outputs.append(result.stdout)
    assert outputs[0] == outputs[1]


def test_loop_body_and_else_returns_do_not_fall_through():
    graph = analyze_flow(
        "def f(items):\n    for item in items:\n        return item\n    else:\n        return 0\n    dead = 1\n",
        "f",
        path="example.py",
    )
    dead = next(n for n in graph["nodes"] if n["text"] == "dead = 1")
    assert dead["reachable"] is False
