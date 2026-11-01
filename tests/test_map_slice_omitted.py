import json

import pytest

from codegraph_slicer import GraphStore
from codegraph_slicer.cli import main


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


def _invoke(capsys, argv):
    assert main(argv) == 0
    return capsys.readouterr().out


def test_map_json_counts_omitted_files_and_has_no_note_when_complete(tmp_path, capsys):
    db = _snapshot(tmp_path / "src", {
        "pkg/a.py": "def alpha():\n    return 1\n",
        "pkg/b.py": "def bravo():\n    return 2\n",
        "pkg/c.py": "def charlie():\n    return 3\n",
    })
    complete_text = _invoke(capsys, ["map", "--db", str(db), "--max-tokens", "2000"])
    complete = json.loads(complete_text)
    assert complete["items"]
    assert "omitted" not in complete
    assert "Omitted records" not in complete_text
    total = len(complete["items"])

    partial_text = _invoke(capsys, ["map", "--db", str(db), "--max-tokens", "55"])
    partial = json.loads(partial_text)
    assert partial["items"] == [complete["items"][0]]
    assert partial["omitted"] == {"files": 2}
    assert partial["retrieve_details"] == "rerun with --max-tokens N"
    assert partial["tokens"] == len(partial_text) // 4 + 1
    assert len(partial["items"]) + partial["omitted"]["files"] == total
    markdown = _invoke(capsys, ["map", "--db", str(db), "--format", "markdown", "--max-tokens", "55"])
    assert "Omitted **files** records: 2" in markdown
    assert "rerun with --max-tokens N" in markdown

    retry_db = _snapshot(tmp_path / "retry", {
        "pkg/a.py": '"""' + "x" * 500 + '"""\ndef alpha():\n    return 1\n',
        "pkg/b.py": "def bravo():\n    return 2\n",
        "pkg/c.py": "def charlie():\n    return 3\n",
    })
    retry = json.loads(_invoke(capsys, ["map", "--db", str(retry_db), "--max-tokens", "80"]))
    assert [item["path"] for item in retry["items"]] == ["pkg/b.py", "pkg/c.py"]
    assert retry["omitted"] == {"files": 1}


def test_map_cli_additive_accounting_preserves_ranked_selection_and_print_size(
    tmp_path, capsys
):
    db = _snapshot(tmp_path / "ranked", {
        "pkg/a.py": "def alpha():\n    return bravo()\n",
        "pkg/b.py": "def bravo():\n    return 7\n",
        "pkg/c.py": 'def charlie():\n    return "雪 `ticks`"\n',
        "pkg/d.py": "def delta():\n    return charlie()\n",
        "pkg/e.py": "def echo():\n    return 99\n",
        "pkg/large.py": '"""' + "x" * 10000 + '"""\ndef huge():\n    return 4\n',
    })
    expected = {
        "json": {
            24: [],
            45: ["pkg/a.py"],
            65: ["pkg/a.py", "pkg/b.py"],
            87: ["pkg/a.py", "pkg/b.py", "pkg/c.py"],
            100: ["pkg/a.py", "pkg/b.py", "pkg/c.py"],
            108: ["pkg/a.py", "pkg/b.py", "pkg/c.py", "pkg/d.py"],
            128: ["pkg/a.py", "pkg/b.py", "pkg/c.py", "pkg/d.py", "pkg/e.py"],
        },
        "markdown": {
            27: [],
            42: ["pkg/a.py"],
            57: ["pkg/a.py", "pkg/b.py"],
            87: ["pkg/a.py", "pkg/b.py", "pkg/c.py", "pkg/d.py"],
            102: ["pkg/a.py", "pkg/b.py", "pkg/c.py", "pkg/d.py", "pkg/e.py"],
        },
    }
    expected_sizes = {
        "json": {24: 24, 45: 45, 65: 65, 87: 87, 100: 87, 108: 108, 128: 128},
        "markdown": {27: 27, 42: 42, 57: 57, 87: 87, 102: 102},
    }
    for output_format, cases in expected.items():
        for budget, paths in cases.items():
            printed = _invoke(capsys, [
                "map", "--db", str(db), "--snapshot", "--format", output_format,
                "--max-tokens", str(budget),
            ])
            stdout_tokens = len(printed) // 4 + 1
            assert stdout_tokens == expected_sizes[output_format][budget]
            if output_format == "json":
                payload = json.loads(printed)
                assert [item["path"] for item in payload["items"]] == paths
                assert payload["tokens"] == stdout_tokens <= budget
                assert len(payload["items"]) + payload.get("omitted", {}).get("files", 0) == 6
            else:
                import re

                rendered_paths = re.findall(r"\n```python\n([^\n]+):1\n", printed)
                assert rendered_paths == paths
                estimate = int(re.search(r"Estimated context tokens: (\d+)", printed)[1])
                assert estimate == stdout_tokens <= budget
                omitted = re.search(r"Omitted \*\*files\*\* records: (\d+)", printed)
                assert (int(omitted[1]) if omitted else 0) == 6 - len(paths)
            assert "pkg/large.py" not in paths


def test_map_json_accounting_closes_root_and_handles_omitted_digit_transition(
    tmp_path, capsys
):
    files = {
        f"pkg/f{index:02}.py": f"def item_{index}():\n    return {index}\n"
        for index in range(12)
    }
    files["pkg/zz_large.py"] = '"""' + "x" * 10000 + '"""\ndef huge():\n    return 1\n'
    db = _snapshot(tmp_path / "many", files)
    expected = {
        24: [],
        46: [],
        47: ["pkg/f00.py"],
        91: ["pkg/f00.py", "pkg/f01.py"],
        92: ["pkg/f00.py", "pkg/f01.py", "pkg/f02.py"],
        114: ["pkg/f00.py", "pkg/f01.py", "pkg/f02.py", "pkg/f03.py"],
        294: [f"pkg/f{index:02}.py" for index in range(11)],
        295: [f"pkg/f{index:02}.py" for index in range(12)],
    }
    expected_sizes = {24: 24, 46: 24, 47: 47, 91: 69, 92: 92, 114: 114, 294: 272, 295: 295}
    for budget, paths in expected.items():
        printed = _invoke(capsys, [
            "map", "--db", str(db), "--snapshot", "--max-tokens", str(budget),
        ])
        payload = json.loads(printed)
        assert [item["path"] for item in payload["items"]] == paths
        assert payload["tokens"] == len(printed) // 4 + 1 == expected_sizes[budget]
        assert payload["tokens"] <= budget
        assert len(paths) + payload["omitted"]["files"] == 13
        assert "pkg/zz_large.py" not in paths

    complete_db = _snapshot(tmp_path / "complete", {
        f"pkg/f{index:02}.py": f"def item_{index}():\n    return {index}\n"
        for index in range(12)
    })
    before_complete = json.loads(_invoke(capsys, [
        "map", "--db", str(complete_db), "--snapshot", "--max-tokens", "276",
    ]))
    assert len(before_complete["items"]) == 11
    assert before_complete["omitted"] == {"files": 1}
    assert before_complete["tokens"] == 272
    complete_text = _invoke(capsys, [
        "map", "--db", str(complete_db), "--snapshot", "--max-tokens", "277",
    ])
    complete = json.loads(complete_text)
    assert len(complete["items"]) == 12
    assert "omitted" not in complete
    assert complete["tokens"] == len(complete_text) // 4 + 1 == 277


def test_slice_json_omissions_count_records_not_body_fallback(tmp_path, capsys, monkeypatch):
    db = _snapshot(tmp_path / "src", {
        "pkg/target.py": "def target():\n    return helper()\n\ndef helper():\n" + "\n".join(f"    value_{i} = {i}" for i in range(200)) + "\n    return 7\n",
        "pkg/callers.py": "from pkg.target import target\n\ndef caller_one():\n    return target()\n\ndef caller_two():\n    return target()\n",
    })
    full = json.loads(_invoke(capsys, ["slice", "--db", str(db), "pkg.target:target", "--max-tokens", "5000"]))
    counts = {name: len(items) for name, items in full["sections"].items()}
    assert "omitted" not in full
    assert counts["upstream_callers"] == 2
    assert counts["downstream_dependencies"] == 1
    partial_text = _invoke(capsys, ["slice", "--db", str(db), "pkg.target:target", "--max-tokens", "171", "--include-callee-bodies"])
    partial = json.loads(partial_text)
    assert partial["omitted"] == {
        "types_and_schemas": 0, "upstream_callers": 2,
        "target_implementations": 0, "downstream_dependencies": 1,
    }
    assert partial["sections"]["downstream_dependencies"] == []
    assert "Omitted: " in partial["retrieve_details"]
    assert "pkg.callers:caller_one (10 tokens)" in partial["retrieve_details"]
    assert "pkg.callers:caller_two (10 tokens)" in partial["retrieve_details"]
    assert "pkg.target:helper (952 tokens)" in partial["retrieve_details"]
    assert f"--db {db} pkg.callers:caller_one pkg.callers:caller_two pkg.target:helper --snapshot --max-tokens 1944" in partial["retrieve_details"]
    assert sum(map(len, partial["sections"].values())) + sum(partial["omitted"].values()) == sum(counts.values())
    assert partial["tokens"] == len(partial_text) // 4 + 1
    fallback = json.loads(_invoke(capsys, ["slice", "--db", str(db), "pkg.target:target", "--max-tokens", "400", "--include-callee-bodies"]))
    assert fallback["omitted"] == {
        "types_and_schemas": 0,
        "upstream_callers": 0,
        "target_implementations": 0,
        "downstream_dependencies": 0,
    }
    fallback_callee = fallback["sections"]["downstream_dependencies"][0]["text"]
    assert "def helper():" in fallback_callee
    assert "value_199" not in fallback_callee and "return 7" not in fallback_callee
    assert "Body summarized: pkg.target:helper (952 tokens)" in fallback["retrieve_details"]
    assert sum(map(len, fallback["sections"].values())) == sum(counts.values())
    monkeypatch.chdir(tmp_path)
    markdown = _invoke(capsys, ["slice", "--db", db.name, "pkg.target:target", "--format", "markdown", "--max-tokens", "164", "--include-callee-bodies"])
    assert "Omitted **upstream callers** records: 2" in markdown
    assert "Omitted **downstream dependencies** records: 1" in markdown
    assert "pkg.callers:caller_one (10 tokens)" in markdown
    assert "pkg.target:helper (952 tokens)" in markdown
    assert markdown.index("## target implementations") < markdown.index("Omitted **upstream callers** records: 2")


def test_map_and_slice_output_budgets_exact_and_refuse_without_stdout(tmp_path, capsys):
    files = {
        "pkg/aa.py": '"""xyzw"""\ndef alpha():\n    return 10000\n',
        "pkg/b.py": "def bravo():\n    return 2\n",
        "pkg/c.py": "def charlie():\n    return 3\n",
    }
    db = _snapshot(tmp_path / "src", files)
    for output_format in ("json", "markdown"):
        for command in ("map", "slice"):
            args = [command, "--db", str(db), "--format", output_format]
            anchors = [] if command == "map" else ["pkg.aa:alpha"]
            args += anchors
            with pytest.raises(SystemExit, match="2") as error:
                main([command, "--db", str(db), *anchors, "--max-tokens", "1"])
            assert error.value.code == 2
            captured = capsys.readouterr()
            assert captured.out == ""
            assert "budget" in captured.err.lower()
            full = _invoke(capsys, [*args, "--max-tokens", "5000"])
            if output_format == "json":
                payload = json.loads(full)
                assert "omitted" not in payload
                assert "retrieve_details" not in payload
                full_estimate = len(full) // 4 + 1
                assert payload["tokens"] == full_estimate
            else:
                assert "Omitted **" not in full
                assert "rerun with --max-tokens N" not in full
                assert len(full) % 4 == 0
                full_estimate = len(full) // 4 + 1
                assert f"Estimated context tokens: {full_estimate}" in full
            first_success = None
            for budget in range(1, 1000):
                try:
                    main([*args, "--max-tokens", str(budget)])
                except SystemExit as error:
                    assert error.code == 2
                    capsys.readouterr()
                    continue
                printed = capsys.readouterr().out
                first_success = (budget, printed)
                break
            assert first_success is not None
            first_budget, printed = first_success
            if output_format == "json":
                payload = json.loads(printed)
                assert payload["tokens"] == len(printed) // 4 + 1 == first_budget
            else:
                assert f"Estimated context tokens: {len(printed) // 4 + 1}" in printed
            with pytest.raises(SystemExit, match="2") as error:
                main([*args, "--max-tokens", str(first_budget - 1)])
            assert error.value.code == 2
            captured = capsys.readouterr()
            assert captured.out == ""
            assert "budget" in captured.err.lower()

