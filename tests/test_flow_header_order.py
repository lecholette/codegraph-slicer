"""CLI regressions checked against actual CPython local load/store events."""

import asyncio
import contextlib
import dis
import json
import re
import runpy
import sys

import pytest

from codegraph_slicer.cli import main


class AsyncManager:
    async def __aenter__(self):
        return 7

    async def __aexit__(self, *exc):
        return False


# Each expected set is the COMPLETE projection for the named local, including
# old definitions when an exception can occur before a header finishes.
CASES = [
    ("except_star_intervening_type", "def f():\n    t = 0\n    try:\n        raise ExceptionGroup('group', [ValueError(), TypeError()])\n    except* ValueError:\n        pass\n    except* (t := KeyError):\n        pass\n    except* TypeError:\n        result = t\n    return result\n", (), "t", {(7, 10)}),
    ("augassign_no_invented_read", "def f(a):\n    x = 0\n    a[(x := 1)] += 2\n    return x\n", ({1: 7},), "x", {(3, 4)}),
    ("except_no_backflow", "def f():\n    t = 0\n    try:\n        raise ValueError()\n    except ValueError:\n        return t\n    except (t := TypeError):\n        pass\n", (), "t", {(2, 6)}),
    ("except_later_type_read", "def f():\n    t = 0\n    try:\n        raise ValueError()\n    except (t := TypeError):\n        pass\n    except (t, ValueError):\n        return t\n", (), "t", {(5, 7), (5, 8)}),
    ("except_alias_unmatched", "def f():\n    t = 0\n    try:\n        raise ValueError()\n    except TypeError as t:\n        pass\n    except ValueError:\n        return t\n", (), "t", {(2, 8)}),
    ("except_type_failure", "def f():\n    t = 0\n    try:\n        try:\n            raise ValueError()\n        except (t := 1):\n            pass\n    except TypeError:\n        return t\n", (), "t", {(2, 9), (6, 9)}),
    ("assign_starred", "def f(a):\n    x = []\n    *x, a[x[0]] = (1, 7)\n    return x\n", ({},), "x", {(3, 3), (3, 4)}),
    ("with_starred", "def f(cm, a):\n    x = []\n    with cm as (*x, a[x[0]]):\n        return x\n", (contextlib.nullcontext((1, 7)), {}), "x", {(3, 3), (3, 4)}),
    ("augassign_target_read", "def f(a):\n    x = 0\n    a[(x := x + 1) + x] += x\n    return x\n", ({2: 7},), "x", {(2, 3), (3, 3), (3, 4)}),
    ("except_own", "def f():\n    t = TypeError\n    try:\n        raise ValueError()\n    except (t := ValueError):\n        return t\n", (), "t", {(5, 6)}),
    ("except_later", "def f():\n    t = 0\n    try:\n        raise ValueError()\n    except (t := TypeError):\n        pass\n    except ValueError:\n        return t\n", (), "t", {(5, 8)}),
    ("except_inline", "def f():\n    t = 0\n    try:\n        raise ValueError()\n    except ((t := ValueError), t):\n        return t\n", (), "t", {(5, 5), (5, 6)}),
    ("except_star", "def f():\n    t = 0\n    try:\n        raise ExceptionGroup('group', [ValueError()])\n    except* (t := TypeError):\n        pass\n    except* ValueError:\n        result = t\n    return result\n", (), "t", {(5, 8)}),
    ("with_target", "def f(cm, a):\n    x = 0\n    with cm as a[(x := 1) + x]:\n        return x\n", (contextlib.nullcontext(7), {}), "x", {(3, 3), (3, 4)}),
    ("async_with_target", "async def f(cm, a):\n    x = 0\n    async with cm as a[(x := 1) + x]:\n        return x\n", (AsyncManager(), {}), "x", {(3, 3), (3, 4)}),
    ("for_target", "def f(a):\n    x = 0\n    for a[(x := 1) + x] in [7]:\n        result = x\n    return result\n", ({},), "x", {(3, 3), (3, 4)}),
    ("structured_for_target", "def f(cm, a):\n    x = 0\n    with cm:\n        for a[(x := 1) + x] in [7]:\n            result = x\n    return result\n", (contextlib.nullcontext(), {}), "x", {(4, 4), (4, 5)}),
    ("assign_target", "def f(a):\n    x = 0\n    a[(x := 1) + x] = 7\n    return x\n", ({},), "x", {(3, 3), (3, 4)}),
    ("annassign_target", "def f(a):\n    x = 0\n    a[(x := 1) + x]: int = 7\n    return x\n", ({},), "x", {(3, 3), (3, 4)}),
    ("augassign_target", "def f(a):\n    x = 0\n    a[(x := 1) + x] += x\n    return x\n", ({2: 7},), "x", {(3, 3), (3, 4)}),
    ("assign_rhs_before_target", "def f(a):\n    x = 0\n    a[(x := 1) + x] = x\n    return x\n", ({},), "x", {(2, 3), (3, 3), (3, 4)}),
    ("assign_tuple", "def f(a):\n    x = 0\n    x, a[x] = (1, 7)\n    return x\n", ({},), "x", {(3, 3), (3, 4)}),
    ("with_tuple", "def f(cm, a):\n    x = 0\n    with cm as (x, a[x]):\n        return x\n", (contextlib.nullcontext((1, 7)), {}), "x", {(3, 3), (3, 4)}),
    ("for_tuple", "def f(a):\n    x = 0\n    for x, a[x] in [(1, 7)]:\n        result = x\n    return result\n", ({},), "x", {(3, 3), (3, 4)}),
    ("structured_for_tuple", "def f(cm, a):\n    x = 0\n    with cm:\n        for x, a[x] in [(1, 7)]:\n            result = x\n    return result\n", (contextlib.nullcontext(), {}), "x", {(4, 4), (4, 5)}),
    ("annassign_bare_target", "def f(a):\n    x = 0\n    a[(x := 1) + x]: int\n    return x\n", ({},), "x", {(3, 3), (3, 4)}),
    ("raise_cause", "def f():\n    x = 0\n    try:\n        raise ValueError() from ((x := 1) + x)\n    except TypeError:\n        return x\n", (), "x", {(4, 4), (2, 6), (4, 6)}),
]


def _observed_pairs(function, args):
    """Trace opcodes, including Python 3.14's combined fast-local operations."""
    instructions = {item.offset: item for item in dis.get_instructions(function)}
    definitions = {}
    observed = set()
    local_op = re.compile(r"LOAD_FAST(?:_BORROW|_CHECK|_AND_CLEAR)?|STORE_FAST|DELETE_FAST|LOAD_DEREF|STORE_DEREF|DELETE_DEREF")

    def trace(frame, event, arg):
        if frame.f_code is not function.__code__:
            return None
        # Reassert on every event: Python 3.14 can reset this after a call.
        frame.f_trace_opcodes = True
        if event == "call":
            for name in frame.f_locals:
                definitions.setdefault(name, function.__code__.co_firstlineno)
        elif event == "opcode":
            instruction = instructions[frame.f_lasti]
            operations = local_op.findall(instruction.opname)
            if operations:
                assert "_".join(operations) == instruction.opname, instruction.opname
                names = instruction.argval if isinstance(instruction.argval, tuple) else (instruction.argval,)
                assert len(names) == len(operations)
                for operation, name in zip(operations, names):
                    line = instruction.positions.lineno
                    if operation.startswith("LOAD") and name in definitions:
                        observed.add((name, definitions[name], line))
                    if operation.startswith("STORE"):
                        definitions[name] = line
                    elif operation.startswith("DELETE") or operation.endswith("AND_CLEAR"):
                        definitions.pop(name, None)
        return trace

    previous = sys.gettrace()
    # Python 3.12 enables opcode monitoring only when an existing frame opts in
    # before settrace; setting it solely on the callee misses the first run.
    caller = sys._getframe()
    previous_opcodes = caller.f_trace_opcodes
    caller.f_trace_opcodes = True
    sys.settrace(trace)
    try:
        result = function(*args)
        if asyncio.iscoroutine(result):
            asyncio.run(result)
    finally:
        sys.settrace(previous)
        caller.f_trace_opcodes = previous_opcodes
    return observed


@pytest.mark.parametrize("case,source,args,variable,expected", CASES, ids=[case[0] for case in CASES])
def test_header_order_matches_runtime(tmp_path, capsys, case, source, args, variable, expected):
    root = tmp_path / "fixture"
    root.mkdir()
    module = root / "sample.py"
    module.write_text(source)
    db = tmp_path / "graph.sqlite"
    assert main(["sync", str(root), "--db", str(db)]) == 0
    capsys.readouterr()
    assert main(["flow", "sample.py:f", "--db", str(db), "--snapshot", "--max-tokens", "100000"]) == 0
    graph = json.loads(capsys.readouterr().out)["flow"]
    by_id = {node["id"]: node for node in graph["nodes"]}
    pairs = {
        (edge["label"], by_id[edge["source"]]["line"], by_id[edge["target"]]["line"])
        for edge in graph["edges"] if edge["kind"] == "DATA_DEP"
    }
    assert {(start, end) for name, start, end in pairs if name == variable} == expected
    function = runpy.run_path(str(module))["f"]
    observed = _observed_pairs(function, args)
    assert observed <= pairs, f"runtime pairs missing from flow: {sorted(observed - pairs)}"
    assert {(variable, start, end) for start, end in expected} & observed


@pytest.mark.parametrize("header", [
    "with cm as a[(a := 1)]:",
    "async with cm as a[(a := 1)]:",
    "for a[(a := 1)] in [7]:",
    "a[(a := 1)] = 7",
    "a[(a := 1)]: int = 7",
    "a[(a := 1)] += 2",
], ids=["with", "async_with", "for", "assign", "annassign", "augassign"])
def test_receiver_rebinding_refuses_instead_of_guessing(tmp_path, capsys, header):
    prefix = "async " if header.startswith("async ") else ""
    body = "        return a" if header.endswith(":") else "    return a"
    source = f"{prefix}def f(cm, a):\n    {header}\n{body}\n"
    root = tmp_path / "fixture"
    root.mkdir()
    module = root / "sample.py"
    module.write_text(source)
    function = runpy.run_path(str(module))["f"]
    manager = AsyncManager() if prefix else contextlib.nullcontext(7)
    observed = _observed_pairs(function, (manager, {1: 7}))
    # The receiver is the old mapping; the name is rebound while computing its
    # index. A syntactic access path cannot safely name both versions of a.
    assert {(start, end) for name, start, end in observed if name == "a"} == {(1, 2), (2, 3)}
    db = tmp_path / "graph.sqlite"
    assert main(["sync", str(root), "--db", str(db)]) == 0
    capsys.readouterr()
    with pytest.raises(SystemExit, match="2"):
        main(["flow", "sample.py:f", "--db", str(db), "--snapshot"])
    assert "unsupported target receiver rebinding in sample.py:2" in capsys.readouterr().err
