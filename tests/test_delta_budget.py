import json
import re

import pytest

from codegraph_slicer import GraphStore
from codegraph_slicer.cli import main
from codegraph_slicer.slicer import serialized_tokens


def _snapshot(root, source, test_source=None):
    root.mkdir()
    (root / "pkg").mkdir()
    (root / "pkg" / "mod.py").write_text(source)
    if test_source is not None:
        (root / "tests").mkdir()
        (root / "tests" / "test_mod.py").write_text(test_source)
    db = root.parent / f"{root.name}.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(root)
    return db


def _snapshot_files(root, files):
    root.mkdir()
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    db = root.parent / f"{root.name}.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(root)
    return db


def _markdown_section_count(markdown, title):
    match = re.search(rf"(?ms)^## {re.escape(title)}\n\n(.*?)(?=^## |\Z)", markdown)
    return 0 if match is None else sum(line.startswith("- ") for line in match.group(1).splitlines())


def _delta(tmp_path, capsys, base_source, head_source, *options, test_source=None):
    base = _snapshot(tmp_path / "base", base_source, test_source)
    head = _snapshot(tmp_path / "head", head_source, test_source)
    code = main([
        "delta", "--base-db", str(base), "--head-db", str(head), *options
    ])
    captured = capsys.readouterr()
    assert code == 0
    return captured.out


def test_delta_summary_has_prioritized_counts_and_exact_omissions(tmp_path, capsys):
    base = "def target():\n    return 1\n\ndef caller():\n    return target()\n"
    head = "def target():\n    return 2\n\ndef caller():\n    return target()\n"
    payload = json.loads(_delta(tmp_path, capsys, base, head, "--max-tokens", "1200"))

    assert payload["summary"]["counts"]["modified_symbols"] == 2
    assert payload["summary"]["counts"]["added_edges"] == 0
    assert payload["omitted"]["modified_symbols"] == 0
    assert payload["omitted"]["test_coverage"] == 0
    assert "added_edges" not in payload or payload["added_edges"] == []
    assert payload["serialized_tokens"] == serialized_tokens(payload)


def test_delta_full_preserves_raw_details_and_refuses_over_budget(tmp_path, capsys):
    base = "def target():\n    return 1\n\ndef caller():\n    return 0\n"
    head = "def target():\n    return 1\n\ndef caller():\n    return target()\n"
    text = _delta(tmp_path, capsys, base, head, "--full", "--max-tokens", "1200")
    payload = json.loads(text)
    assert payload["added_edges"] == [["pkg.mod:caller", "pkg.mod:target", "CALLS", 5]]
    assert payload["modified_symbols"] == ["pkg.mod:", "pkg.mod:caller"]
    summary_root = tmp_path / "summary"
    summary_root.mkdir()
    summary = json.loads(_delta(summary_root, capsys, base, head, "--max-tokens", "1200"))
    assert "added_edges" not in summary
    assert summary["omitted"]["added_edges"] == 1
    assert summary["omitted"]["import_changes"] == 0

    # Rebuild distinct fixture paths since the helper creates immutable snapshots.
    base_db = _snapshot(tmp_path / "tiny-base", base)
    head_db = _snapshot(tmp_path / "tiny-head", head)
    with pytest.raises(SystemExit, match="2") as error:
        main(["delta", "--base-db", str(base_db), "--head-db", str(head_db), "--full", "--max-tokens", "1"])
    assert error.value.code == 2
    assert "budget" in capsys.readouterr().err.lower()


def test_delta_distinguishes_selected_and_unselected_static_test_gaps(tmp_path, capsys):
    test = "from pkg.mod import target\n\ndef test_target():\n    assert target() == 1\n"
    base = "def target():\n    return 1\n"
    head = "def target():\n    return 2\n"
    payload = json.loads(_delta(
        tmp_path, capsys, base, head, "--max-tokens", "1600",
        "--selected-test", "tests.test_mod:test_target", test_source=test
    ))
    summary = payload["summary"]
    assert summary["test_selection"] == "selected"
    assert summary["test_gap_status_counts"]["updated_or_selected_evidence"] == 1
    assert summary["test_gap_status_counts"]["reachable_but_unselected"] == 1
    coverage = payload["test_coverage"]
    assert sorted(item["status"] for item in coverage) == [
        "reachable_but_unselected", "updated_or_selected_evidence"
    ]


def test_delta_budget_omissions_are_exact_and_boundary_is_prioritized(tmp_path, capsys):
    base = "from pkg.secret import value\n\n" + "\n".join(
        f"def function_{i}():\n    return {i}\n" for i in range(14)
    )
    head = "from pkg.secret import value\n\n" + "\n".join(
        f"def function_{i}():\n    return {i + 100}\n" for i in range(14)
    )
    # A boundary violation is decision-critical and must rank ahead of routine symbol details.
    base_db = _snapshot(tmp_path / "base", base)
    head_db = _snapshot(tmp_path / "head", head)
    out = main(["delta", "--base-db", str(base_db), "--head-db", str(head_db),
                "--forbid-import", "pkg", "pkg.secret", "--max-tokens", "500"])
    assert out == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["summary"]["counts"]["modified_symbols"] == 15
    assert payload["omitted"]["modified_symbols"] == 14
    assert len(payload["modified_symbols"]) == 1
    assert payload["summary"]["counts"]["boundary_violations"] == 2
    assert payload["omitted"]["boundary_violations"] == 0
    assert len(payload["boundary_violations"]) == 2
    assert all(item["target"].startswith("pkg.secret") for item in payload["boundary_violations"])
    assert "added_edges" not in payload and "removed_edges" not in payload
    assert payload["omitted"]["added_edges"] == payload["summary"]["counts"]["added_edges"]
    assert payload["summary"]["retrieve_details"] == "rerun with --full --max-tokens N"
    assert payload["serialized_tokens"] <= 500
    assert payload["serialized_tokens"] == serialized_tokens(payload)


def test_delta_markdown_is_readable_and_budgeted(tmp_path, capsys):
    base = _snapshot(tmp_path / "base", "def target():\n    return 1\n")
    head = _snapshot(tmp_path / "head", "def target():\n    return 2\n")
    assert main(["delta", "--base-db", str(base), "--head-db", str(head),
                 "--format", "markdown", "--max-tokens", "1200"]) == 0
    markdown = capsys.readouterr().out
    assert markdown.startswith("# Static delta report")
    assert "## Summary" in markdown
    assert "Static TESTS reachability" in markdown
    assert "--full --max-tokens N" in markdown
    assert "```json" not in markdown
    with pytest.raises(SystemExit, match="2"):
        main(["delta", "--base-db", str(base), "--head-db", str(head),
              "--format", "markdown", "--max-tokens", "100"])
    assert "budget" in capsys.readouterr().err


def test_delta_reports_no_reachable_tests_as_static_gap(tmp_path, capsys):
    base = "def target():\n    return 1\n"
    head = "def target():\n    return 2\n"
    payload = json.loads(_delta(tmp_path, capsys, base, head, "--max-tokens", "1200"))
    assert payload["summary"]["test_selection"] == "none_selected"
    assert payload["summary"]["test_gap_status_counts"]["no_static_test_reaches"] == 2
    assert payload["summary"]["test_gap_status_counts"]["reachable_but_unselected"] == 0


def test_delta_refuses_when_real_snapshot_metadata_cannot_fit(tmp_path, capsys):
    base = _snapshot(tmp_path / "base", "def target():\n    return 1\n")
    head = _snapshot(tmp_path / "head", "def target():\n    return 2\n")
    with pytest.raises(SystemExit, match="2") as error:
        main(["delta", "--base-db", str(base), "--head-db", str(head), "--max-tokens", "1"])
    assert error.value.code == 2
    diagnostic = capsys.readouterr().err
    assert "summary metadata requires" in diagnostic
    assert "budget is 1" in diagnostic


def test_delta_markdown_truncates_to_rendered_budget(tmp_path, capsys):
    base_source = "\n".join(f"def function_{i}():\n    return {i}\n" for i in range(18))
    head_source = "\n".join(f"def function_{i}():\n    return {i + 100}\n" for i in range(18))
    base = _snapshot(tmp_path / "base", base_source)
    head = _snapshot(tmp_path / "head", head_source)
    assert main(["delta", "--base-db", str(base), "--head-db", str(head),
                 "--format", "markdown", "--max-tokens", "500"]) == 0
    markdown = capsys.readouterr().out
    assert markdown.startswith("# Static delta report")
    assert serialized_tokens(markdown) <= 500
    assert "Omitted **modified symbols** records: 17" in markdown
    assert "Omitted **test coverage** records: 14" in markdown


def test_delta_help_exposes_budget_and_full_retrieval(capsys):
    with pytest.raises(SystemExit, match="0") as error:
        main(["delta", "--help"])
    assert error.value.code == 0
    help_text = capsys.readouterr().out
    assert "--max-tokens" in help_text
    assert "--full" in help_text
    assert "priority-ranked summary" in help_text
    assert "default: 8000" in help_text


def test_delta_default_budget_matches_explicit_8000_on_large_snapshot(tmp_path, capsys):
    base_source = "\n".join(f"def function_{i:03d}():\n    return {i}\n" for i in range(600))
    head_source = "\n".join(f"def function_{i:03d}():\n    return {i + 1000}\n" for i in range(600))
    base = _snapshot(tmp_path / "base", base_source)
    head = _snapshot(tmp_path / "head", head_source)
    common = ["delta", "--base-db", str(base), "--head-db", str(head)]
    assert main(common) == 0
    default_output = capsys.readouterr().out
    assert main([*common, "--max-tokens", "8000"]) == 0
    explicit_output = capsys.readouterr().out
    payload = json.loads(default_output)
    assert default_output == explicit_output
    assert payload["serialized_tokens"] <= 8000
    assert payload["summary"]["counts"]["modified_symbols"] == 601
    assert payload["omitted"]["modified_symbols"] > 0


def test_delta_prioritizes_untested_symbols_and_coverage_before_imports(tmp_path, capsys):
    base = _snapshot_files(tmp_path / "base", {
        "pkg/a.py": "def target():\n    return 1\n",
        "pkg/b.py": "from pkg.a import target\n\ndef changed():\n    return 1\n",
        "pkg/c.py": "def other():\n    return 3\n",
    })
    head = _snapshot_files(tmp_path / "head", {
        "pkg/a.py": "from pkg.b import changed\n\ndef target():\n    return 1\n",
        "pkg/b.py": "from pkg.c import other\n\ndef changed():\n    return 2\n",
        "pkg/c.py": "from pkg.a import target\n\ndef other():\n    return 3\n",
    })
    assert main(["delta", "--base-db", str(base), "--head-db", str(head),
                 "--forbid-import", "pkg.a", "pkg.b", "--max-tokens", "1200"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["boundary_violations"] == [
        {"source": "pkg.a", "target": "pkg.b"},
        {"source": "pkg.a", "target": "pkg.b.changed"},
    ]
    assert payload["new_cycles"] == [["pkg.a", "pkg.b", "pkg.c"]]
    assert payload["untested_modified_symbols"] == ["pkg.a:", "pkg.b:", "pkg.b:changed", "pkg.c:"]
    assert payload["omitted"]["untested_modified_symbols"] == 0
    assert payload["test_coverage"] == [
        {"symbol": symbol, "reachable_tests": [], "updated_or_selected_tests": [],
         "status": "no_static_test_reaches"}
        for symbol in ["pkg.a:", "pkg.b:", "pkg.b:changed", "pkg.c:"]
    ]
    assert payload["omitted"]["test_coverage"] == 0
    assert len(payload["import_changes"]) == 8
    emitted_order = [key for key in payload if key in {
        "boundary_violations", "new_cycles", "untested_modified_symbols",
        "test_coverage", "import_changes",
    }]
    assert emitted_order == ["boundary_violations", "new_cycles", "untested_modified_symbols",
                             "test_coverage", "import_changes"]


def test_delta_full_emits_import_changes_and_accounts_for_every_kind(tmp_path, capsys):
    base = _snapshot_files(tmp_path / "base", {
        "pkg/a.py": "def target():\n    return 1\n",
        "pkg/b.py": "from pkg.a import target\n",
        "pkg/c.py": "def other():\n    return 3\n",
    })
    head = _snapshot_files(tmp_path / "head", {
        "pkg/a.py": "def target():\n    return 1\n",
        "pkg/b.py": "from pkg.c import other\n",
        "pkg/c.py": "def other():\n    return 3\n",
    })
    args = ["delta", "--base-db", str(base), "--head-db", str(head),
            "--full", "--max-tokens", "10000000"]
    assert main(args) == 0
    full_json = json.loads(capsys.readouterr().out)
    assert full_json["import_changes"] == [
        {"change": "added", "source": "pkg.b:", "target": "pkg.c:", "line": 1},
        {"change": "added", "source": "pkg.b:", "target": "pkg.c:other", "line": 1},
        {"change": "removed", "source": "pkg.b:", "target": "pkg.a:", "line": 1},
        {"change": "removed", "source": "pkg.b:", "target": "pkg.a:target", "line": 1},
    ]
    assert all(count == 0 for count in full_json["omitted"].values())
    totals = {
        "added_edges": 2, "removed_edges": 2, "complexity_drift": 0,
        "boundary_violations": 0, "modified_symbols": 1,
        "untested_modified_symbols": 1, "affected_entrypoints": 0,
        "new_cycles": 0, "test_coverage": 1, "import_changes": 4,
    }
    for kind, total in totals.items():
        assert len(full_json[kind]) + full_json["omitted"][kind] == total
        assert len(full_json[kind]) == total
    assert main([*args, "--format", "markdown"]) == 0
    markdown = capsys.readouterr().out
    for kind in ("added_edges", "removed_edges", "complexity_drift", "boundary_violations",
                 "modified_symbols", "untested_modified_symbols", "affected_entrypoints",
                 "new_cycles", "test_coverage", "import_changes"):
        assert _markdown_section_count(markdown, kind.replace("_", " ").title()) == totals[kind]
    assert "Omitted **import changes** records:" not in markdown


@pytest.mark.parametrize("output_format", ["json", "markdown"])
def test_delta_summary_emitted_plus_omitted_equals_total_for_every_kind(tmp_path, capsys, output_format):
    base = _snapshot_files(tmp_path / "base", {
        "pkg/a.py": "def target():\n    return 1\n",
        "pkg/b.py": "from pkg.a import target\n",
        "pkg/c.py": "def other():\n    return 3\n",
    })
    head = _snapshot_files(tmp_path / "head", {
        "pkg/a.py": "def target():\n    return 1\n",
        "pkg/b.py": "from pkg.c import other\n",
        "pkg/c.py": "def other():\n    return 3\n",
    })
    args = ["delta", "--base-db", str(base), "--head-db", str(head), "--max-tokens", "1200"]
    if output_format == "markdown":
        args.extend(["--format", "markdown"])
    assert main(args) == 0
    output = capsys.readouterr().out
    if output_format == "json":
        payload = json.loads(output)
        for kind, total in payload["summary"]["counts"].items():
            assert len(payload.get(kind, [])) + payload["omitted"][kind] == total
    else:
        for kind in ("boundary_violations", "new_cycles", "complexity_drift", "added_edges",
                     "removed_edges", "modified_symbols", "affected_entrypoints",
                     "untested_modified_symbols", "test_coverage", "import_changes"):
            label = kind.replace("_", " ")
            suffix = r" \(none selected\)" if kind == "untested_modified_symbols" else ""
            total_match = re.search(
                rf"(?m)^- \*\*{re.escape(label)}{suffix}:\*\* (\d+)$", output
            )
            assert total_match is not None
            total = int(total_match.group(1))
            omitted_match = re.search(rf"Omitted \*\*{re.escape(label)}\*\* records: (\d+)", output)
            omitted = 0 if omitted_match is None else int(omitted_match.group(1))
            title = label.title() + (" (none selected)" if kind == "untested_modified_symbols" else "")
            emitted = _markdown_section_count(output, title)
            assert emitted + omitted == total, kind


def test_delta_tight_budget_keeps_test_gaps_before_import_changes(tmp_path, capsys):
    base = _snapshot_files(tmp_path / "base", {
        "pkg/a.py": "def target():\n    return 1\n",
        "pkg/b.py": "from pkg.a import target\n\ndef changed():\n    return 1\n",
        "pkg/c.py": "def other():\n    return 3\n",
    })
    head = _snapshot_files(tmp_path / "head", {
        "pkg/a.py": "def target():\n    return 1\n",
        "pkg/b.py": "from pkg.c import other\n\ndef changed():\n    return 2\n",
        "pkg/c.py": "def other():\n    return 3\n",
    })
    assert main(["delta", "--base-db", str(base), "--head-db", str(head),
                 "--max-tokens", "300"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["untested_modified_symbols"] == ["pkg.b:", "pkg.b:changed"]
    assert [item["symbol"] for item in payload["test_coverage"]] == ["pkg.b:", "pkg.b:changed"]
    assert "import_changes" not in payload
    assert payload["omitted"]["import_changes"] == 4
    assert payload["omitted"]["untested_modified_symbols"] == 0
    assert payload["omitted"]["test_coverage"] == 0


@pytest.mark.parametrize("budget", [300, 301, 340, 342, 360])
def test_delta_markdown_printed_text_respects_budget_and_hides_zero_omissions(
    tmp_path, capsys, budget
):
    base = _snapshot_files(tmp_path / f"base-{budget}", {
        "pkg/a.py": "def target():\n    return 1\n",
        "pkg/b.py": "from pkg.a import target\ndef other():\n    return 3\n",
        "tests/test_a.py": "from pkg.a import target\ndef test_hit():\n    assert target() == 1\ndef test_miss():\n    assert 1 == 1\n",
    })
    head = _snapshot_files(tmp_path / f"head-{budget}", {
        "pkg/a.py": "from pkg.b import other\ndef target():\n    return 2\n",
        "pkg/b.py": "from pkg.a import target\ndef other():\n    return 3\n",
        "tests/test_a.py": "from pkg.a import target\ndef test_hit():\n    assert target() == 1\ndef test_miss():\n    assert 1 == 1\n",
    })
    assert main(["delta", "--base-db", str(base), "--head-db", str(head),
                 "--forbid-import", "pkg.a", "pkg.b", "--format", "markdown",
                 "--max-tokens", str(budget)]) == 0
    markdown = capsys.readouterr().out
    assert serialized_tokens(markdown) <= budget
    assert "Omitted **removed edges** records: 0" not in markdown


def test_delta_markdown_accounts_each_kind_and_names_empty_untested_selection(tmp_path, capsys):
    base = _snapshot_files(tmp_path / "base", {
        "pkg/mod.py": "def target():\n    return 1\n",
        "tests/test_mod.py": "from pkg.mod import target\n\ndef test_target():\n    assert target() == 1\n",
    })
    head = _snapshot_files(tmp_path / "head", {
        "pkg/mod.py": "def target():\n    return 2\n",
        "tests/test_mod.py": "from pkg.mod import target\n\ndef test_target():\n    assert target() == 2\n",
    })
    assert main(["delta", "--base-db", str(base), "--head-db", str(head),
                 "--format", "markdown", "--max-tokens", "10000"]) == 0
    markdown = capsys.readouterr().out
    assert "**untested modified symbols (none selected):** 0" in markdown
    assert "## Untested Modified Symbols" not in markdown
