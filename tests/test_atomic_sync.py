import json
import sqlite3

import pytest

from codegraph_slicer import GraphStore
from codegraph_slicer import store as store_module
from codegraph_slicer.cli import main


def _make(tmp_path, name, source):
    root = tmp_path / name
    root.mkdir()
    (root / "sample.py").write_text(source)
    return root


def _sync(root, db):
    main(["sync", str(root), "--db", str(db)])


def _only(tmp_path):
    return sorted(p.name for p in tmp_path.iterdir() if p.is_file())


def _boom_on_extract(monkeypatch, hook):
    real = store_module.extract_file

    def extract(*args, **kwargs):
        hook()
        return real(*args, **kwargs)

    monkeypatch.setattr(store_module, "extract_file", extract)


def test_failed_sync_keeps_old_db_byte_identical_and_leaves_no_temp(tmp_path, monkeypatch, capsys):
    root = _make(tmp_path, "src", "def calculate():\n    return 1\n")
    db = tmp_path / "graph.sqlite"
    _sync(root, db)
    capsys.readouterr()
    before = db.read_bytes()
    (root / "sample.py").write_text("def calculate():\n    return 2\n")

    def fail():
        raise RuntimeError("parser exploded")

    _boom_on_extract(monkeypatch, fail)
    with pytest.raises(RuntimeError, match="parser exploded"):
        _sync(root, db)
    assert db.read_bytes() == before
    assert _only(tmp_path) == ["graph.sqlite"]


def test_failed_first_sync_leaves_no_file_at_db_path(tmp_path, monkeypatch):
    root = _make(tmp_path, "src", "def calculate():\n    return 1\n")
    db = tmp_path / "graph.sqlite"

    def fail():
        raise RuntimeError("parser exploded")

    _boom_on_extract(monkeypatch, fail)
    with pytest.raises(RuntimeError, match="parser exploded"):
        _sync(root, db)
    assert _only(tmp_path) == []


def test_query_during_first_sync_gets_wait_error_not_missing_metadata(tmp_path, monkeypatch, capsys):
    root = _make(tmp_path, "src", "def calculate():\n    return 1\n")
    db = tmp_path / "graph.sqlite"
    seen = []

    def query_midway():
        if seen:
            return
        with pytest.raises(SystemExit) as caught:
            main(["map", "--db", str(db)])
        seen.append((caught.value.code, capsys.readouterr().err))

    _boom_on_extract(monkeypatch, query_midway)
    _sync(root, db)
    code, err = seen[0]
    assert code == 2
    assert err == (
        f"codegraph-slicer: CodeGraphError: no completed sync exists yet for {db}; "
        "wait for the running sync to finish, then retry\n"
    )
    assert not db.with_name("graph.sqlite-wal").exists()


def test_query_during_resync_sees_previous_complete_db(tmp_path, monkeypatch, capsys):
    root = _make(tmp_path, "src", "def calculate():\n    return 1\n")
    db = tmp_path / "graph.sqlite"
    _sync(root, db)
    capsys.readouterr()
    main(["map", "--db", str(db), "--snapshot"])
    old_map = capsys.readouterr().out
    (root / "other.py").write_text("def added():\n    return 3\n")
    seen = []

    def query_midway():
        if seen:
            return
        main(["map", "--db", str(db), "--snapshot"])
        seen.append(capsys.readouterr().out)

    _boom_on_extract(monkeypatch, query_midway)
    _sync(root, db)
    assert seen == [old_map]
    main(["map", "--db", str(db)])
    assert "added" in capsys.readouterr().out


def test_sync_discards_wal_left_by_writer_of_replaced_db(tmp_path, monkeypatch, capsys):
    root = _make(tmp_path, "src", "def calculate():\n    return 1\n")
    db = tmp_path / "graph.sqlite"
    _sync(root, db)
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA wal_autocheckpoint=0")

    def old_db_written_mid_sync():
        writer.execute("INSERT INTO metadata(key,value) VALUES('poison','1')")
        writer.commit()

    _boom_on_extract(monkeypatch, old_db_written_mid_sync)
    (root / "sample.py").write_text("def calculate():\n    return 2\n")
    _sync(root, db)
    capsys.readouterr()
    main(["map", "--db", str(db)])
    capsys.readouterr()
    with GraphStore(db, readonly=True) as store:
        assert store.db.execute("SELECT count(*) FROM metadata WHERE key='poison'").fetchone()[0] == 0
    writer.close()


def test_writer_connected_across_handoff_cannot_alter_new_db(tmp_path, capsys):
    root = _make(tmp_path, "src", "def calculate():\n    return 1\n")
    db = tmp_path / "graph.sqlite"
    _sync(root, db)
    writer = sqlite3.connect(db)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("INSERT INTO metadata(key,value) VALUES('before','1')")
    writer.commit()
    (root / "sample.py").write_text("def calculate():\n    return 2\n")
    _sync(root, db)
    writer.execute("INSERT INTO metadata(key,value) VALUES('after','1')")
    writer.commit()
    assert _only(tmp_path) == ["graph.sqlite"]
    capsys.readouterr()
    main(["map", "--db", str(db)])
    capsys.readouterr()
    with GraphStore(db, readonly=True) as store:
        keys = {r[0] for r in store.db.execute("SELECT key FROM metadata WHERE key IN ('before','after')")}
        assert keys == {"before"}
        assert store.db.execute("PRAGMA integrity_check").fetchall()[0][0] == "ok"
    writer.close()


def test_sync_refuses_to_replace_db_under_an_in_flight_writer(tmp_path, monkeypatch, capsys):
    root = _make(tmp_path, "src", "def calculate():\n    return 1\n")
    db = tmp_path / "graph.sqlite"
    _sync(root, db)
    capsys.readouterr()
    monkeypatch.setattr(store_module, "_REPLACE_LOCK_TIMEOUT", 0.05)
    writer = sqlite3.connect(db)
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO metadata(key,value) VALUES('inflight','1')")
    (root / "sample.py").write_text("def calculate():\n    return 2\n")
    with pytest.raises(SystemExit):
        _sync(root, db)
    assert "database is locked" in capsys.readouterr().err
    writer.commit()
    writer.close()
    assert [n for n in _only(tmp_path) if n.endswith(".tmp")] == []
    with GraphStore(db, readonly=True) as store:
        assert store.db.execute("SELECT value FROM metadata WHERE key='inflight'").fetchone()[0] == "1"


def test_concurrent_syncs_leave_one_valid_db_last_finisher_wins(tmp_path, monkeypatch, capsys):
    root_a = _make(tmp_path, "a", "def from_a():\n    return 1\n")
    root_b = _make(tmp_path, "b", "def from_b():\n    return 2\n")
    db = tmp_path / "graph.sqlite"
    ran = []

    def inner_sync_finishes_first():
        if ran:
            return
        ran.append(1)
        _sync(root_b, db)

    _boom_on_extract(monkeypatch, inner_sync_finishes_first)
    _sync(root_a, db)
    assert _only(tmp_path) == ["graph.sqlite"]
    capsys.readouterr()
    main(["map", "--db", str(db), "--snapshot"])
    out = capsys.readouterr().out
    assert "from_a" in out and "from_b" not in out
    with GraphStore(db, readonly=True) as store:
        assert [r["path"] for r in store.db.execute("SELECT path FROM files")] == ["sample.py"]


def test_query_on_missing_db_does_not_create_it(tmp_path, capsys):
    db = tmp_path / "nope.sqlite"
    with pytest.raises(SystemExit):
        main(["map", "--db", str(db)])
    assert not db.exists()
    assert _only(tmp_path) == []


def test_resync_is_incremental_against_existing_db(tmp_path, capsys):
    root = _make(tmp_path, "src", "def calculate():\n    return 1\n")
    (root / "other.py").write_text("def other():\n    return 2\n")
    db = tmp_path / "graph.sqlite"
    _sync(root, db)
    capsys.readouterr()
    (root / "other.py").write_text("def other():\n    return 3\n")
    _sync(root, db)
    result = json.loads(capsys.readouterr().out)
    assert (result["parsed_files"], result["cached_files"]) == (1, 1)
