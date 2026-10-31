import hashlib
import json
import os
import subprocess

import pytest

from codegraph_slicer import FlowQueryError, GraphStore, query_flow
from codegraph_slicer.cli import main

SOURCE = """def calculate(amount):
    billed = amount * 2
    if billed > 10:
        total = billed + 1
    else:
        total = billed
    return total
"""


def _snapshot(tmp_path, source=SOURCE):
    root = tmp_path / "src"
    root.mkdir()
    (root / "sample.py").write_text(source)
    db = tmp_path / "graph.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(root)
    return root, db


def test_cli_flow_reads_cached_snapshot_readonly_and_emits_identity(tmp_path, capsys):
    root, db = _snapshot(tmp_path)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    (root / "sample.py").write_text("def calculate(amount):\n    return 999\n")
    assert main(["flow", "sample.py:calculate", "--db", str(db), "--snapshot", "--variable", "total", "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    graph = report["flow"]
    assert graph["identity"]["path"] == "sample.py"
    assert graph["identity"]["blob_sha"].startswith("sha256:")
    assert graph["source_sha256"] == hashlib.sha256(SOURCE.encode()).hexdigest()
    assert any("billed" in node["text"] for node in graph["nodes"])
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_flow_refuses_class_anchor_and_unsupported_cached_source(tmp_path, capsys):
    _root, db = _snapshot(tmp_path, "class Worker:\n    def run(self):\n        return lambda: 1\n")
    with (
        GraphStore(db, readonly=True) as store,
        pytest.raises(FlowQueryError, match="function or method"),
    ):
        query_flow(store, "sample.py:")
    with pytest.raises(SystemExit, match="2"):
        main(["flow", "sample.py:Worker.run", "--db", str(db), "--snapshot"])
    assert "unsupported Lambda in sample.py:3" in capsys.readouterr().err


def test_flow_incremental_sync_changes_cached_source_identity(tmp_path, capsys):
    root, db = _snapshot(tmp_path)
    assert main(["flow", "sample.py:calculate", "--db", str(db), "--variable", "total"]) == 0
    first = json.loads(capsys.readouterr().out)["flow"]
    updated = "def calculate(amount):\n    answer = amount + 7\n    return answer\n"
    (root / "sample.py").write_text(updated)
    with pytest.raises(SystemExit, match="2"):
        main(["flow", "sample.py:calculate", "--db", str(db), "--variable", "answer"])
    assert "stale" in capsys.readouterr().err.lower()
    with GraphStore(db) as store:
        store.sync_workspace(root)
    assert main(["flow", "sample.py:calculate", "--db", str(db), "--variable", "answer"]) == 0
    second = json.loads(capsys.readouterr().out)["flow"]
    assert second["source_sha256"] == hashlib.sha256(updated.encode()).hexdigest()
    assert second["source_sha256"] != first["source_sha256"]
    assert any("answer = amount + 7" == node["text"] for node in second["nodes"])


def test_flow_markdown_shows_nodes_and_source_identity(tmp_path, capsys):
    _, db = _snapshot(tmp_path)
    assert main(["flow", "sample.py:calculate", "--db", str(db), "--variable", "total", "--format", "markdown"]) == 0
    output = capsys.readouterr().out
    assert "# Python local flow: `calculate`" in output
    assert f"SHA-256 `{hashlib.sha256(SOURCE.encode()).hexdigest()}`" in output
    assert "billed = amount * 2" in output
    assert "## Limitations" in output


def test_flow_never_executes_indexed_module(tmp_path, capsys):
    marker = tmp_path / "module-executed"
    source = SOURCE + f"\nopen({str(marker)!r}, 'w').write('executed')\n"
    _, db = _snapshot(tmp_path, source)
    assert main(["flow", "sample.py:calculate", "--db", str(db), "--variable", "total"]) == 0
    capsys.readouterr()
    assert not marker.exists()


def test_flow_cli_refuses_complete_graph_that_exceeds_budget(tmp_path, capsys):
    _, db = _snapshot(tmp_path)
    with pytest.raises(SystemExit, match="2") as error:
        main(["flow", "sample.py:calculate", "--db", str(db), "--max-tokens", "1"])
    assert error.value.code == 2
    assert "budget" in capsys.readouterr().err.lower()


def test_flow_cli_rejects_invalid_direction_before_query(tmp_path, capsys):
    _, db = _snapshot(tmp_path)
    with pytest.raises(SystemExit, match="2") as error:
        main(["flow", "sample.py:calculate", "--db", str(db), "--direction", "sideways"])
    assert error.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_default_flow_refuses_stale_workspace_and_snapshot_option_reads_cached_bytes(tmp_path, capsys):
    root, db = _snapshot(tmp_path)
    before = hashlib.sha256(db.read_bytes()).hexdigest()
    (root / "sample.py").write_text("def calculate(amount):\n    return 999\n")
    with pytest.raises(SystemExit, match="2") as error:
        main(["flow", "sample.py:calculate", "--db", str(db)])
    assert error.value.code == 2
    assert "stale" in capsys.readouterr().err.lower()
    assert main(["flow", "sample.py:calculate", "--db", str(db), "--snapshot"]) == 0
    report = json.loads(capsys.readouterr().out)["flow"]
    assert report["source_sha256"] == hashlib.sha256(SOURCE.encode()).hexdigest()
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before


def test_default_map_refuses_python_edit(tmp_path, capsys):
    root, db = _snapshot(tmp_path)
    (root / "sample.py").write_text("def calculate(amount):\n    return amount + 1\n")
    with pytest.raises(SystemExit, match="2") as error:
        main(["map", "--db", str(db)])
    assert error.value.code == 2
    assert "stale" in capsys.readouterr().err.lower()


def test_default_slice_refuses_python_edit(tmp_path, capsys):
    root, db = _snapshot(tmp_path)
    (root / "sample.py").write_text("def calculate(amount):\n    return amount + 1\n")
    with pytest.raises(SystemExit, match="2") as error:
        main(["slice", "--db", str(db), "sample:calculate"])
    assert error.value.code == 2
    assert "stale" in capsys.readouterr().err.lower()


@pytest.mark.parametrize("change", ["add", "delete"])
def test_default_map_and_slice_refuse_workspace_python_file_set_changes(tmp_path, capsys, change):
    root, db = _snapshot(tmp_path)
    path = root / ("extra.py" if change == "add" else "sample.py")
    if change == "add":
        path.write_text("def extra():\n    return 1\n")
    else:
        path.unlink()
    argv = (["map", "--db", str(db)] if change == "add" else ["slice", "--db", str(db), "sample:calculate"])
    with pytest.raises(SystemExit, match="2") as error:
        main(argv)
    assert error.value.code == 2
    assert "stale" in capsys.readouterr().err.lower()


def test_freshness_ignores_non_python_workspace_change_and_refuses_missing_metadata(tmp_path, capsys):
    root, db = _snapshot(tmp_path)
    (root / "notes.txt").write_text("documentation changed")
    assert main(["flow", "sample.py:calculate", "--db", str(db)]) == 0
    capsys.readouterr()
    import sqlite3
    with sqlite3.connect(db) as connection:
        connection.execute("DELETE FROM metadata WHERE key LIKE 'snapshot_%'")
    with pytest.raises(SystemExit, match="2") as error:
        main(["flow", "sample.py:calculate", "--db", str(db)])
    assert error.value.code == 2
    assert "cannot be verified" in capsys.readouterr().err.lower()


def test_committed_snapshot_refuses_python_edits_but_ignores_non_python_edits(tmp_path, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "sample.py").write_text(SOURCE)
    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, "GIT_CONFIG_NOSYSTEM":"1", "GIT_CONFIG_GLOBAL":os.devnull})
    git("init", "-q")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "--allow-empty", "-qm", "base")
    git("add", "sample.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "source")
    db = tmp_path / "git.sqlite"
    with GraphStore(db) as store:
        store.sync_git_tree(root, "HEAD")
    (root / "notes.txt").write_text("ignored by Python graph")
    assert main(["flow", "sample.py:calculate", "--db", str(db)]) == 0
    capsys.readouterr()
    (root / "sample.py").write_text("def calculate(amount):\n    return 999\n")
    with pytest.raises(SystemExit, match="2") as error:
        main(["flow", "sample.py:calculate", "--db", str(db)])
    assert error.value.code == 2
    message = capsys.readouterr().err.lower()
    assert "stale" in message
    assert "sync root --db db" in message
    assert "current working tree" in message
    assert main(["sync", str(root), "--db", str(db)]) == 0
    capsys.readouterr()
    assert main(["flow", "sample.py:calculate", "--db", str(db)]) == 0
    refreshed = json.loads(capsys.readouterr().out)["flow"]
    assert refreshed["source_sha256"] == hashlib.sha256((root / "sample.py").read_bytes()).hexdigest()


def test_committed_snapshot_refuses_untracked_python_addition(tmp_path, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "sample.py").write_text(SOURCE)
    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, "GIT_CONFIG_NOSYSTEM":"1", "GIT_CONFIG_GLOBAL":os.devnull})
    git("init", "-q")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "add", "sample.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "source")
    db = tmp_path / "git.sqlite"
    with GraphStore(db) as store:
        store.sync_git_tree(root, "HEAD")
    (root / "new.py").write_text("def new():\n    return 2\n")
    with pytest.raises(SystemExit, match="2") as error:
        main(["map", "--db", str(db)])
    assert error.value.code == 2
    assert "python source differs at new.py" in capsys.readouterr().err.lower()


def test_committed_snapshot_refuses_ignored_untracked_python_addition(tmp_path, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "sample.py").write_text(SOURCE)
    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, "GIT_CONFIG_NOSYSTEM":"1", "GIT_CONFIG_GLOBAL":os.devnull})
    git("init", "-q")
    (root / ".gitignore").write_text("hidden.py\n")
    git("add", "sample.py", ".gitignore")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "source")
    db = tmp_path / "git.sqlite"
    with GraphStore(db) as store:
        store.sync_git_tree(root, "HEAD")
    (root / "hidden.py").write_text("def hidden():\n    return 3\n")
    with pytest.raises(SystemExit, match="2") as error:
        main(["map", "--db", str(db)])
    assert error.value.code == 2
    assert "python source differs at hidden.py" in capsys.readouterr().err.lower()


def test_committed_snapshot_refuses_head_move(tmp_path, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "sample.py"
    source.write_text(SOURCE)
    notes = root / "notes.txt"
    notes.write_text("base note")

    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})

    git("init", "-q")
    git("add", "sample.py", "notes.txt")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "source")
    db = tmp_path / "git.sqlite"
    with GraphStore(db) as store:
        store.sync_git_tree(root, "HEAD")
    notes.write_text("updated note")
    git("add", "notes.txt")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "updated")
    with pytest.raises(SystemExit, match="2") as error:
        main(["flow", "sample.py:calculate", "--db", str(db)])
    assert error.value.code == 2
    message = capsys.readouterr().err.lower()
    assert "head moved" in message
    assert "sync root --revision head --db db" in message


def test_git_sha256_object_format_matches_working_python_blobs(tmp_path, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "sample.py").write_text(SOURCE)

    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})

    initialized = subprocess.run(["git", "init", "-q", "--object-format=sha256", str(root)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if initialized.returncode:
        pytest.skip("installed Git does not support SHA-256 object-format repositories")
    git("add", "sample.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "source")
    db = tmp_path / "git-sha256.sqlite"
    with GraphStore(db) as store:
        store.sync_git_tree(root, "HEAD")
    assert main(["flow", "sample.py:calculate", "--db", str(db)]) == 0
    report = json.loads(capsys.readouterr().out)["flow"]
    assert report["source_sha256"] == hashlib.sha256(SOURCE.encode()).hexdigest()


def test_committed_snapshot_refuses_working_symlink_source(tmp_path, capsys):
    root = tmp_path / "repo"
    root.mkdir()
    source = root / "sample.py"
    source.write_text(SOURCE)

    def git(*args):
        subprocess.run(["git", "-C", str(root), *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull})

    git("init", "-q")
    git("add", "sample.py")
    git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "source")
    db = tmp_path / "git.sqlite"
    with GraphStore(db) as store:
        store.sync_git_tree(root, "HEAD")
    source.unlink()
    backing = root / "backing.txt"
    backing.write_text(SOURCE)
    source.symlink_to(backing.name)
    with pytest.raises(SystemExit, match="2") as error:
        main(["flow", "sample.py:calculate", "--db", str(db)])
    assert error.value.code == 2
    assert "symlink source refused" in capsys.readouterr().err.lower()


def test_sync_rolls_back_source_graph_and_freshness_metadata_together(tmp_path):
    import sqlite3

    root, db = _snapshot(tmp_path)
    original_blob = hashlib.sha256(SOURCE.encode()).hexdigest()
    changed_root = tmp_path / "changed"
    changed_root.mkdir()
    (changed_root / "sample.py").write_text("def calculate(amount):\n    return amount + 8\n")
    with sqlite3.connect(db) as connection:
        connection.execute("""
            CREATE TRIGGER refuse_snapshot_metadata
            BEFORE UPDATE ON metadata WHEN OLD.key='snapshot_root'
            BEGIN SELECT RAISE(ABORT, 'reject freshness metadata'); END
        """)
    with GraphStore(db) as store:
        with pytest.raises(sqlite3.IntegrityError, match="reject freshness metadata"):
            store.sync_workspace(changed_root)
    with GraphStore(db, readonly=True) as store:
        metadata = store.rows("SELECT value FROM metadata WHERE key='snapshot_root'")[0]["value"]
        blob = store.rows("SELECT blob_sha FROM files WHERE path='sample.py'")[0]["blob_sha"]
    assert metadata == str(root.resolve())
    assert blob == "sha256:" + original_blob
    assert main(["flow", "sample.py:calculate", "--db", str(db)]) == 0


def test_sync_to_new_root_refreshes_metadata_before_default_query(tmp_path, capsys):
    _, db = _snapshot(tmp_path)
    new_root = tmp_path / "new-root"
    new_root.mkdir()
    (new_root / "sample.py").write_text("def calculate(amount):\n    answer = amount + 9\n    return answer\n")
    with GraphStore(db) as store:
        store.sync_workspace(new_root)
    assert main(["flow", "sample.py:calculate", "--db", str(db), "--variable", "answer"]) == 0
    report = json.loads(capsys.readouterr().out)["flow"]
    assert report["source_sha256"] == hashlib.sha256((new_root / "sample.py").read_bytes()).hexdigest()


def test_cli_flow_models_with_manager_and_suppression_paths(tmp_path, capsys):
    source = """def managed(manager, value, flag):
    before = value
    with manager as resource:
        inside = resource
        if flag:
            raise ValueError()
    after = inside
    return after
"""
    _, db = _snapshot(tmp_path, source)
    assert main(["flow", "sample.py:managed", "--db", str(db), "--snapshot", "--max-tokens", "100000", "--format", "json"]) == 0
    graph = json.loads(capsys.readouterr().out)["flow"]
    nodes_by_id = {n["id"]: n for n in graph["nodes"]}
    pairs = {(e["kind"], nodes_by_id[e["source"]]["text"], nodes_by_id[e["target"]]["text"], e["label"]) for e in graph["edges"]}
    assert ("DATA_DEP", "manager", "manager", "manager") in pairs
    assert ("DATA_DEP", "bind resource", "inside = resource", "resource") in pairs
    assert ("DATA_DEP", "inside = resource", "after = inside", "inside") in pairs
    assert ("CFG", "bind resource", "before Assign", "next") in pairs
    assert ("CFG", "exit manager 1 (raise)", "suppressed exception", "suppressed-exit") in pairs


def test_cli_flow_models_try_pre_and_post_state_handler_and_else(tmp_path, capsys):
    source = """def guarded(value, operation):
    value = 1
    try:
        value = operation()
    except ValueError:
        handled = value
    else:
        succeeded = value
    return value
"""
    _, db = _snapshot(tmp_path, source)
    assert main(["flow", "sample.py:guarded", "--db", str(db), "--snapshot", "--max-tokens", "100000", "--format", "json"]) == 0
    graph = json.loads(capsys.readouterr().out)["flow"]
    by_id = {node["id"]: node for node in graph["nodes"]}
    pairs = {(edge["kind"], by_id[edge["source"]]["text"], by_id[edge["target"]]["text"], edge["label"]) for edge in graph["edges"]}
    assert ("DATA_DEP", "value = 1", "handled = value", "value") in pairs
    assert ("DATA_DEP", "value = operation()", "handled = value", "value") in pairs
    assert ("DATA_DEP", "value = operation()", "succeeded = value", "value") in pairs
    assert ("CFG", "before Assign", "except ValueError", "exception-before-statement") in pairs


def test_cli_flow_comprehension_targets_are_local_to_expression(tmp_path, capsys):
    source = """def mapped(items, offset):
    values = [item + offset for item in items]
    return values
"""
    _, db = _snapshot(tmp_path, source)
    assert main(["flow", "sample.py:mapped", "--db", str(db), "--snapshot", "--max-tokens", "100000", "--format", "json"]) == 0
    graph = json.loads(capsys.readouterr().out)["flow"]
    by_id = {node["id"]: node for node in graph["nodes"]}
    pairs = {(edge["kind"], by_id[edge["source"]]["text"], by_id[edge["target"]]["text"], edge["label"]) for edge in graph["edges"]}
    assert ("DATA_DEP", "items", "values = [item + offset for item in items]", "items") in pairs
    assert ("DATA_DEP", "offset", "values = [item + offset for item in items]", "offset") in pairs
    statement = next(n for n in graph["nodes"] if n["text"] == "values = [item + offset for item in items]")
    assert statement["defs"] == ["values"]
    assert statement["uses"] == ["items", "offset"]
    assert not any(d["code"] == "possibly-unbound-local" and "item" in d["message"] for d in graph["diagnostics"])
    assert not any(node["text"] == "item" and "item" in node["defs"] for node in graph["nodes"])


def test_cli_flow_async_with_models_manager_and_binding(tmp_path, capsys):
    source = """async def managed(manager):
    async with manager as resource:
        result = resource
    return result
"""
    _, db = _snapshot(tmp_path, source)
    assert main(["flow", "sample.py:managed", "--db", str(db), "--snapshot", "--max-tokens", "100000", "--format", "json"]) == 0
    graph = json.loads(capsys.readouterr().out)["flow"]
    by_id = {node["id"]: node for node in graph["nodes"]}
    pairs = {(edge["kind"], by_id[edge["source"]]["text"], by_id[edge["target"]]["text"], edge["label"]) for edge in graph["edges"]}
    assert ("DATA_DEP", "manager", "manager", "manager") in pairs
    assert ("DATA_DEP", "bind resource", "result = resource", "resource") in pairs
    assert ("CFG", "bind resource", "before Assign", "next") in pairs


def test_cli_flow_try_star_keeps_sequential_handler_effects(tmp_path, capsys):
    source = """def grouped(group):
    try:
        raise group
    except* ValueError:
        first = 1
    except* TypeError:
        second = first
    return second
"""
    _, db = _snapshot(tmp_path, source)
    assert main(["flow", "sample.py:grouped", "--db", str(db), "--snapshot", "--max-tokens", "100000", "--format", "json"]) == 0
    graph = json.loads(capsys.readouterr().out)["flow"]
    by_id = {node["id"]: node for node in graph["nodes"]}
    pairs = {(edge["kind"], by_id[edge["source"]]["text"], by_id[edge["target"]]["text"], edge["label"]) for edge in graph["edges"]}
    assert ("CFG", "first = 1", "except TypeError", "except-star-next") in pairs
    assert ("DATA_DEP", "first = 1", "second = first", "first") in pairs


def test_cli_flow_runs_finally_before_return_exit(tmp_path, capsys):
    source = """def closing():
    try:
        return 1
    finally:
        cleanup = 2
"""
    _, db = _snapshot(tmp_path, source)
    assert main(["flow", "sample.py:closing", "--db", str(db), "--snapshot", "--max-tokens", "100000", "--format", "json"]) == 0
    graph = json.loads(capsys.readouterr().out)["flow"]
    by_id = {node["id"]: node for node in graph["nodes"]}
    pairs = {(edge["kind"], by_id[edge["source"]]["text"], by_id[edge["target"]]["text"], edge["label"]) for edge in graph["edges"]}
    assert ("CFG", "return 1", "finally return", "finally-return") in pairs
    assert ("CFG", "finally return", "before Assign", "next") in pairs
    assert ("CFG", "before Assign", "cleanup = 2", "complete") in pairs
    assert ("CFG", "return 1", "", "return-exit") not in pairs


def _new_flow(tmp_path, capsys, source):
    _, db = _snapshot(tmp_path, source)
    assert main(["flow", "sample.py:f", "--db", str(db), "--snapshot", "--max-tokens", "100000"]) == 0
    graph = json.loads(capsys.readouterr().out)["flow"]
    by_id = {node["id"]: node for node in graph["nodes"]}
    pairs = {(e["kind"], by_id[e["source"]]["text"], by_id[e["target"]]["text"], e["label"]) for e in graph["edges"]}
    return graph, pairs


@pytest.mark.parametrize("action", ["break", "continue"])
def test_cli_finally_routes_loop_completion(tmp_path, capsys, action):
    _graph, pairs = _new_flow(tmp_path, capsys, f"""def f(items):
    for item in items:
        try:
            {action}
        finally:
            cleanup = item
    return 9
""")
    assert ("CFG", action, "finally " + action, "finally-" + action) in pairs
    assert ("CFG", "finally " + action, "before Assign", "next") in pairs
    assert ("CFG", "before Assign", "cleanup = item", "complete") in pairs
    assert ("DATA_DEP", "bind item", "cleanup = item", "item") in pairs
    assert ("CFG", "cleanup = item", "iterator test", "backedge") in pairs if action == "continue" else ("CFG", "cleanup = item", "before Return", "next") in pairs
    assert ("CFG", action, "iterator test", action) not in pairs


def test_cli_nested_finally_overrides_pending_return(tmp_path, capsys):
    _graph, pairs = _new_flow(tmp_path, capsys, """def f():
    try:
        try:
            return 1
        finally:
            return 2
    finally:
        cleanup = 3
""")
    assert ("CFG", "return 1", "finally return", "finally-return") in pairs
    assert ("CFG", "return 2", "finally return", "finally-return") in pairs
    assert ("CFG", "return 1", "", "return-exit") not in pairs
    assert ("CFG", "return 2", "", "return-exit") not in pairs


def test_cli_finalizer_exception_preserves_pre_store_state(tmp_path, capsys):
    _graph, pairs = _new_flow(tmp_path, capsys, """def f(operation):
    value = 1
    try:
        try:
            return 2
        finally:
            value = operation()
    except ValueError:
        observed = value
    return observed
""")
    assert ("DATA_DEP", "value = 1", "observed = value", "value") in pairs
    assert ("DATA_DEP", "value = operation()", "observed = value", "value") in pairs
    assert ("CFG", "before Assign", "except ValueError", "exception-before-statement") in pairs


def test_cli_finally_copies_do_not_join_normal_and_return(tmp_path, capsys):
    graph, pairs = _new_flow(tmp_path, capsys, """def f(flag):
    value = 0
    try:
        if flag:
            value = 1
            return value
        value = 2
    finally:
        cleanup = value
    return value
""")
    by_id = {n["id"]: n for n in graph["nodes"]}
    last = next(n["id"] for n in graph["nodes"] if n["kind"] == "return" and n["line"] == 10)
    assert {(by_id[e["source"]]["text"], e["label"]) for e in graph["edges"] if e["kind"] == "DATA_DEP" and e["target"] == last} == {("value = 2", "value")}
    assert ("CONTROL_DEP", "flag", "true", "true") in pairs


def test_cli_context_cleanup_reverse_order_and_enter_failure(tmp_path, capsys):
    _graph, pairs = _new_flow(tmp_path, capsys, """def f(first, second):
    with first as left, second as right:
        return right
""")
    assert ("CFG", "return right", "exit manager 2 (return)", "context-return") in pairs
    assert ("CFG", "exit manager 2 (return)", "exit manager 1 (return)", "context-return") in pairs
    assert ("CFG", "second", "exit manager 1 (raise)", "context-raise") in pairs
    assert ("CFG", "second", "exit manager 2 (raise)", "context-raise") in pairs  # binding may fail after successful entry
    assert ("CFG", "return right", "suppressed exception", "suppressed-exit") not in pairs
    assert ("DATA_DEP", "bind right", "return right", "right") in pairs


def test_cli_context_expression_walrus_defines_following_manager(tmp_path, capsys):
    graph, pairs = _new_flow(tmp_path, capsys, """def f(factory):
    with (manager := factory()), manager as resource:
        result = resource
    return manager
""")
    assert ("DATA_DEP", "manager := factory()", "manager", "manager") in pairs
    assert ("DATA_DEP", "manager := factory()", "return manager", "manager") in pairs
    assert {(d["code"], d["message"]) for d in graph["diagnostics"] if d["code"] == "possibly-unbound-local"} == set()


@pytest.mark.parametrize("action", ["return error", "raise error", "break", "continue"])
def test_cli_handler_alias_deleted_on_abrupt_completion(tmp_path, capsys, action):
    _graph, pairs = _new_flow(tmp_path, capsys, f"""def f(items):
    for item in items:
        try:
            raise ValueError()
        except ValueError as error:
            {action}
    return 0
""")
    assert ("DATA_DEP", "except ValueError", action, "error") in pairs if "error" in action else ("CFG", action, "delete error", "implicit-delete") in pairs
    assert ("CFG", action, "delete error", "implicit-delete") in pairs
    assert ("CFG", action, "", "return-exit") not in pairs


def test_cli_deleted_alias_is_unbound_after_handler(tmp_path, capsys):
    graph, pairs = _new_flow(tmp_path, capsys, """def f():
    try:
        raise ValueError()
    except ValueError as error:
        saved = error
    return error
""")
    assert ("DATA_DEP", "except ValueError", "saved = error", "error") in pairs
    assert ("DATA_DEP", "except ValueError", "return error", "error") not in pairs
    assert ("DATA_DEP", "delete error", "return error", "error") not in pairs
    assert {(d["code"], d["message"]) for d in graph["diagnostics"] if d["line"] == 6} >= {("possibly-unbound-local", "local 'error' may have no reaching definition")}


def test_cli_try_star_raised_handler_still_runs_later_subset(tmp_path, capsys):
    _graph, pairs = _new_flow(tmp_path, capsys, """def f(group):
    try:
        raise group
    except* ValueError:
        first = 1
        raise RuntimeError()
    except* KeyError:
        middle = 2
    except* TypeError:
        last = first
    return last
""")
    assert ("CFG", "raise RuntimeError()", "except TypeError", "except-star-next") in pairs
    assert ("DATA_DEP", "first = 1", "last = first", "first") in pairs


@pytest.mark.parametrize("expression", [
    "[item.field for item in items if item.ready]",
    "{item.field for item in items}",
    "{item.key: item.value for item in items}",
    "(item for item in items)",
    "[later for item in items if later for later in item.children]",
    "[[child.field for child in item.children] for item in items]",
])
def test_cli_comprehension_scope_excludes_all_local_roots(tmp_path, capsys, expression):
    statement = "values = " + expression
    graph, pairs = _new_flow(tmp_path, capsys, f"def f(items, item, later):\n    {statement}\n    return item\n")
    node = next(n for n in graph["nodes"] if n["text"] == statement)
    assert node["defs"] == ["values"]
    assert node["uses"] == ["items"]
    assert ("DATA_DEP", "items", statement, "items") in pairs
    assert ("DATA_DEP", "item", "return item", "item") in pairs
    assert ("DATA_DEP", "later", statement, "later") not in pairs


@pytest.mark.parametrize("expression,message", [
    ("(item + outside for item in items)", "deferred generator capture"),
    ("[saved := item for item in items]", "named expression in comprehension"),
    ("[item async for item in items]", "async comprehension"),
    ("[holder.value for holder.value in items]", "comprehension target"),
])
def test_cli_refuses_unsound_comprehension_forms(tmp_path, capsys, expression, message):
    _, db = _snapshot(tmp_path, f"async def f(items, outside, holder):\n    return {expression}\n")
    with pytest.raises(SystemExit, match="2"):
        main(["flow", "sample.py:f", "--db", str(db), "--snapshot"])
    error = capsys.readouterr().err
    assert message in error
    assert "sample.py:2" in error


def test_cli_manager_evaluation_failure_keeps_pre_walrus_value(tmp_path, capsys):
    _graph, pairs = _new_flow(tmp_path, capsys, """def f(suppressor, old, make):
    value = old
    with suppressor:
        with (value := make()):
            pass
    return value
""")
    assert ("CFG", "before manager 1", "exit manager 1 (raise)", "context-raise") in pairs
    assert ("DATA_DEP", "value = old", "return value", "value") in pairs
    assert ("DATA_DEP", "value := make()", "return value", "value") in pairs


def test_cli_comprehension_first_iterable_uses_outer_shadowed_name(tmp_path, capsys):
    graph, pairs = _new_flow(tmp_path, capsys, """def f(item):
    values = [item.field for item in item]
    return item
""")
    assert ("DATA_DEP", "item", "values = [item.field for item in item]", "item") in pairs
    assert ("DATA_DEP", "item", "return item", "item") in pairs
    node = next(n for n in graph["nodes"] if n["text"].startswith("values ="))
    assert node["uses"] == ["item"]
    assert node["defs"] == ["values"]


def test_cli_nested_async_definition_still_refuses(tmp_path, capsys):
    _, db = _snapshot(tmp_path, "def f():\n    async def inner():\n        return 1\n    return inner\n")
    with pytest.raises(SystemExit, match="2"):
        main(["flow", "sample.py:f", "--db", str(db), "--snapshot"])
    assert "unsupported AsyncFunctionDef in sample.py:2" in capsys.readouterr().err


def test_cli_structured_for_binding_tracks_access_write(tmp_path, capsys):
    _graph, pairs = _new_flow(tmp_path, capsys, """def f(items, obj):
    try:
        for obj.field in items:
            seen = obj.field
    except ValueError:
        observed = obj.field
    return obj.field
""")
    assert ("DATA_DEP", "obj", "bind obj.field", "obj") in pairs
    assert ("DATA_DEP", "bind obj.field", "seen = obj.field", "obj.field") in pairs
    assert ("DATA_DEP", "bind obj.field", "observed = obj.field", "obj.field") in pairs


def test_cli_default_comprehension_preserves_body_only_metadata(tmp_path, capsys):
    graph, pairs = _new_flow(tmp_path, capsys, """def f(values=[x for x in seed]):
    return values
""")
    assert ("DATA_DEP", "values", "return values", "values") in pairs
    assert graph["profile"] == "python-local-cpg-v1"
    assert graph["limitations"] == [
        "Calls are opaque; aliases and unknown call side effects can affect memory dependencies.",
        "Statement-level reads are conservative; wildcard object/subscript paths retain possible overlaps, but aliases are not resolved and implicit exception flow is not modeled.",
    ]
