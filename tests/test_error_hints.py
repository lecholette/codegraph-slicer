import subprocess
import sqlite3

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


@pytest.mark.parametrize("guard_order_by", [False, True], ids=["normal", "guarded-connection"])
def test_anchor_error_has_three_nearest_anchors_and_snapshot_guidance(
    tmp_path, capsys, monkeypatch, guard_order_by
):
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
    if guard_order_by:
        original_init = GraphStore.__init__

        class GuardedConnection:
            def __init__(self, connection):
                self._connection = connection

            def execute(self, query, *args, **kwargs):
                if "ORDER BY file_path,qualname" in query:
                    raise sqlite3.OperationalError("disk I/O error")
                return self._connection.execute(query, *args, **kwargs)

            def __getattr__(self, name):
                return getattr(self._connection, name)

        def guarded_init(instance, *args, **kwargs):
            original_init(instance, *args, **kwargs)
            instance.db = GuardedConnection(instance.db)

        monkeypatch.setattr(GraphStore, "__init__", guarded_init)
    hints = _error(capsys, ["slice", "--db", str(db), "sample:calculate_totals"],
                   "AnchorNotFoundError", "sample:calculate_totals")
    candidates = [line for line in hints if line.startswith("hint: nearest anchor:")]
    assert candidates == ["hint: nearest anchor: sample.py:calculate_total",
                          "hint: nearest anchor: sample.py:calculate_tax",
                          "hint: nearest anchor: sample.py:calculate_rate"]
    assert hints == [*candidates,
                     f"hint: a symbol added on a branch is absent from a base-commit DB (recorded commit: {revision}); query a snapshot containing that symbol"]


def test_anchor_error_promotes_exact_leaf_match_and_drops_branch_hint(tmp_path, capsys):
    root = tmp_path / "src"
    root.mkdir()
    (root / "colab_controller.py").write_text(
        "class ColabController:\n"
        "    def execute(self):\n"
        "        return _verify_receipt()\n\n"
        "def _verify_receipt():\n"
        "    return True\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "hints@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Hint Fixture"], cwd=root, check=True)
    subprocess.run(["git", "add", "colab_controller.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    db = tmp_path / "graph.sqlite"
    with GraphStore(db) as store:
        store.sync_git_tree(root, "HEAD")

    hints = _error(
        capsys,
        ["slice", "--db", str(db), "colab_controller.py:ColabController._verify_receipt"],
        "AnchorNotFoundError",
        "colab_controller.py:ColabController._verify_receipt",
    )
    assert hints[0] == "hint: did you mean: colab_controller.py:_verify_receipt"
    assert not any("added on a branch" in line for line in hints)


def test_anchor_error_reports_no_symbol_named_in_any_module_for_guessed_name(tmp_path, capsys):
    root = tmp_path / "src"
    root.mkdir()
    (root / "gpu_orchestration.py").write_text(
        "def _file_sha256():\n    return 1\n"
        "def _offer_sha256():\n    return 2\n"
        "def _canonical_sha256():\n    return 3\n"
    )
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "hints@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Hint Fixture"], cwd=root, check=True)
    subprocess.run(["git", "add", "gpu_orchestration.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    db = tmp_path / "graph.sqlite"
    with GraphStore(db) as store:
        store.sync_git_tree(root, "HEAD")

    hints = _error(
        capsys,
        ["slice", "--db", str(db), "gpu_orchestration:_bundle_sha256"],
        "AnchorNotFoundError",
        "gpu_orchestration:_bundle_sha256",
    )
    assert "hint: no symbol named _bundle_sha256 in any module" in hints
    assert not any("added on a branch" in line for line in hints)


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


def test_changed_python_source_stale_snapshot_adds_refresh_hint(tmp_path, capsys):
    root, db = _indexed(tmp_path, "def calculate():\n    return 1\n")
    (root / "sample.py").write_text("def calculate():\n    return 2\n")
    hints = _error(capsys, ["slice", "--db", str(db), "sample:calculate"],
                   "StaleSnapshotError", "snapshot is stale: Python source differs at sample.py; run codegraph-slicer sync ROOT --db DB")
    assert hints == ["hint: for symbols you did not edit, add --snapshot to read the indexed bytes; re-run sync for symbols you edited"]


def test_moved_git_head_stale_snapshot_has_no_additional_hint(tmp_path, capsys):
    root = tmp_path / "src"
    root.mkdir()
    source = root / "sample.py"
    source.write_text("def calculate():\n    return 1\n")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "hints@example.invalid"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Hint Fixture"], cwd=root, check=True)
    subprocess.run(["git", "add", "sample.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
    db = tmp_path / "graph.sqlite"
    with GraphStore(db) as store:
        store.sync_git_tree(root, "HEAD")
    source.write_text("def calculate():\n    return 2\n")
    subprocess.run(["git", "add", "sample.py"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "move head"], cwd=root, check=True)
    hints = _error(capsys, ["slice", "--db", str(db), "sample:calculate"],
                   "StaleSnapshotError", "snapshot is stale: repository HEAD moved; run codegraph-slicer sync ROOT --revision HEAD --db DB")
    assert hints == []


def test_required_anchor_overflow_prints_exact_rerun_replacing_max_tokens(tmp_path, capsys):
    _, db = _indexed(tmp_path, 'def calculate():\n    return "' + "x" * 100 + '"\n')
    hints = _error(capsys, ["slice", "--db", str(db), "sample:calculate", "--max-tokens", "2"],
                   "RequiredAnchorOverflow", "required definitions need 80 tokens including omission metadata, budget is 2")
    assert hints == [f"rerun: codegraph-slicer slice --db {db} sample:calculate --max-tokens 80"]


def test_required_anchor_overflow_rerun_replaces_equals_form_and_quotes(tmp_path, capsys):
    _, db = _indexed(tmp_path, 'def calculate():\n    return "' + "x" * 100 + '"\n')
    hints = _error(capsys, ["slice", "--max-tokens=2", "--db", str(db), "sample:calculate"],
                   "RequiredAnchorOverflow", "required definitions need 80 tokens including omission metadata, budget is 2")
    assert hints == [f"rerun: codegraph-slicer slice --db {db} sample:calculate --max-tokens 80"]


def test_required_anchor_overflow_rerun_adds_flag_when_absent(tmp_path, capsys):
    _, db = _indexed(tmp_path, "def calculate():\n    return '" + "x" * 40000 + "'\n")
    hints = _error(capsys, ["slice", "--db", str(db), "sample:calculate"],
                   "RequiredAnchorOverflow", "required definitions need 10055 tokens including omission metadata, budget is 8000")
    assert hints == [f"rerun: codegraph-slicer slice --db {db} sample:calculate --max-tokens 10055"]


def test_required_anchor_overflow_rerun_for_slice_outline(tmp_path, capsys):
    _, db = _indexed(tmp_path, 'def calculate():\n    return 1\n')
    with pytest.raises(SystemExit):
        main(["slice", "--outline", "--db", str(db), "sample:calculate", "--max-tokens", "1"])
    lines = capsys.readouterr().err.splitlines()
    assert lines[0].startswith("codegraph-slicer: RequiredAnchorOverflow: required slice outline needs ")
    needed = lines[0].split(" needs ")[1].split(" tokens")[0]
    assert lines[1:] == [f"rerun: codegraph-slicer slice --outline --db {db} sample:calculate --max-tokens {needed}"]


def test_required_anchor_overflow_rerun_succeeds_when_budget_digit_length_grows(tmp_path, capsys):
    import shlex

    root = tmp_path / "repo"
    pkg = root / "src/main/java/com/acme/reporting"
    pkg.mkdir(parents=True)

    num_dtos = 66
    for i in range(num_dtos):
        fields = "\n".join(f'    public String field_{i}_{j} = "value_{j}";' for j in range(10))
        (pkg / f"ReportFilterDto{i:02d}.java").write_text(
            f"package com.acme.reporting;\npublic class ReportFilterDto{i:02d} {{\n{fields}\n}}\n",
            encoding="utf-8",
        )

    for c in range(3):
        imports = "\n".join(f"import com.acme.reporting.ReportFilterDto{i:02d};" for i in range(num_dtos))
        type_params = ", ".join(f"ReportFilterDto{i:02d} dto{i}" for i in range(c * 22, (c + 1) * 22))
        body = "\n".join(f'        String step_{k} = "{k}";' for k in range(40))
        (pkg / f"ReportingComponent{c}.java").write_text(
            f"package com.acme.reporting;\n{imports}\npublic class ReportingComponent{c} {{\n"
            f"    public String build({type_params}) {{\n{body}\n        return step_0;\n    }}\n}}\n",
            encoding="utf-8",
        )

    db = tmp_path / "graph.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(root)

    anchors = [
        f"src/main/java/com/acme/reporting/ReportingComponent{c}.java:ReportingComponent{c}"
        for c in range(3)
    ]
    with pytest.raises(SystemExit, match="2"):
        main(["slice", "--db", str(db), "--max-tokens", "8000", "--format", "xml", *anchors])
    err_lines = capsys.readouterr().err.splitlines()
    rerun_line = next(line for line in err_lines if line.startswith("rerun: "))
    rerun_argv = shlex.split(rerun_line.removeprefix("rerun: "))[1:]
    assert main(rerun_argv) == 0
    out = capsys.readouterr().out
    assert "<context tokens=" in out


def test_embedded_expansion_budgets_depend_on_source_size_not_enclosing_budget(tmp_path):
    from codegraph_slicer.slicer import (
        _build_type_summaries,
        _condense_class_public_methods,
        tokens_for_length,
    )

    root = tmp_path / "repo"
    root.mkdir()
    helpers = "\n".join(
        f"    def _helper_{i}(self, x: int) -> int:\n"
        + "".join(f"        v_{i}_{j} = x + {j}\n" for j in range(12))
        + f"        return v_{i}_0\n"
        for i in range(6)
    )
    pub = (
        "    def run(self, x: int) -> int:\n"
        + "".join(f"        step_{k} = self._helper_0(x + {k})\n" for k in range(20))
        + "        return step_0\n"
    )
    (root / "service.py").write_text(
        f"class BigService:\n{helpers}{pub}\n",
        encoding="utf-8",
    )
    db = tmp_path / "graph.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(root)
        row = store.rows("SELECT * FROM symbols WHERE symbol_id='service:BigService'")[0]
        full_size = tokens_for_length(len(row["body"]))
        expected_budget = full_size * 2

        summaries_small, _, _ = _build_type_summaries(
            store, [("service:BigService", row)], set(), db, 8000
        )
        summaries_large, _, _ = _build_type_summaries(
            store, [("service:BigService", row)], set(), db, 11005
        )
        assert f"--max-tokens {expected_budget}" in summaries_small["service:BigService"]["text"]
        assert summaries_small["service:BigService"]["text"] == summaries_large["service:BigService"]["text"]

        condensed_small = _condense_class_public_methods(
            store, "service:BigService", row, str(db), 8000, min_internal=1
        )
        condensed_large = _condense_class_public_methods(
            store, "service:BigService", row, str(db), 11005, min_internal=1
        )
        assert condensed_small is not None
        assert f"--max-tokens {expected_budget})" in condensed_small
        assert condensed_small == condensed_large


def test_java_method_signature_does_not_crash_python_docstring_parser(tmp_path):
    from codegraph_slicer.slicer import (
        _first_docstring_line,
        _parse_python_first_stmt_docstring,
        slice_context,
    )

    assert _parse_python_first_stmt_docstring("List<String> get();") is None
    assert _first_docstring_line("List<String> get();") is None
    assert _parse_python_first_stmt_docstring("def fn():\n    \"\"\"Func doc.\"\"\"\n    return 1\n") == "Func doc."
    assert _parse_python_first_stmt_docstring("async def afn():\n    \"\"\"Async doc.\"\"\"\n    return 1\n") == "Async doc."
    assert _parse_python_first_stmt_docstring("class Cls:\n    \"\"\"Class doc.\"\"\"\n    pass\n") == "Class doc."
    with pytest.raises(TypeError):
        _parse_python_first_stmt_docstring(123)  # type: ignore[arg-type]

    root = tmp_path / "repo"
    pkg = root / "src/main/java/com/acme"
    pkg.mkdir(parents=True)
    (pkg / "Provider.java").write_text(
        "package com.acme;\nimport java.util.List;\npublic interface Provider {\n    List<String> get();\n    List<String> listAll();\n}\n",
        encoding="utf-8",
    )
    (pkg / "ReportingJob.java").write_text(
        "package com.acme;\npublic class ReportingJob {\n    public String execute(Provider provider) {\n        return provider.get().toString();\n    }\n}\n",
        encoding="utf-8",
    )
    db = tmp_path / "graph.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(root)
        res = slice_context(
            store,
            ["src/main/java/com/acme/ReportingJob.java:ReportingJob.execute"],
            max_tokens=400,
        )
        assert res["sections"]["target_implementations"]
        skel = slice_context(
            store,
            ["src/main/java/com/acme/Provider.java:Provider.get"],
            max_tokens=400,
            target_mode="skeleton",
        )
        assert "List<String> get();" in skel["sections"]["target_implementations"][0]["text"]

