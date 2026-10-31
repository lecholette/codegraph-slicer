import hashlib
import json
import os
import subprocess
import pytest

from codegraph_slicer import FlowQueryError, GraphStore, UnsupportedFlowError, query_flow
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


def test_flow_refuses_class_anchor_and_unsupported_cached_source(tmp_path):
    root, db = _snapshot(tmp_path, "class Worker:\n    def run(self):\n        try:\n            return 1\n        except ValueError:\n            return 2\n")
    with GraphStore(db, readonly=True) as store:
        with pytest.raises(FlowQueryError, match="function or method"):
            query_flow(store, "sample.py:")
        with pytest.raises(UnsupportedFlowError, match="Try.*sample.py:3"):
            query_flow(store, "sample.py:Worker.run")


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
