import sqlite3

import pytest

import codegraph_slicer.store as store_module
from codegraph_slicer import GraphStore
from codegraph_slicer.ast_extractor import CodeGraphParseError


def _symbols(store):
    return {
        row["symbol_id"]
        for row in store.rows("SELECT symbol_id FROM symbols ORDER BY symbol_id")
    }


def _graph_contents(store):
    return {
        "files": store.rows("SELECT * FROM files ORDER BY path"),
        "symbols": store.rows("SELECT * FROM symbols ORDER BY symbol_id"),
        "raw_edges": store.rows(
            "SELECT * FROM raw_edges ORDER BY source_file,source_id,target_id,kind,line"
        ),
        "edges": store.rows(
            "SELECT * FROM edges ORDER BY source_file,source_id,target_id,kind,line"
        ),
        "diagnostics": store.rows(
            "SELECT * FROM diagnostics ORDER BY source_file,source_id,target,kind,line,reason"
        ),
    }


def test_stale_engine_reparses_all_files_and_failed_sync_keeps_engine(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    (root / "a.py").write_text("def alpha():\n    return 1\n")
    (root / "b.py").write_text("def beta():\n    return 2\n")
    database = tmp_path / "graph.sqlite"

    with GraphStore(database) as graph:
        first = graph.sync_workspace(root)
        assert (first["parsed_files"], first["cached_files"]) == (2, 0)
        expected = _graph_contents(graph)
        graph.db.execute(
            "UPDATE metadata SET value='stale' WHERE key='engine_sha'"
        )
        graph.db.commit()

        reparsed = graph.sync_workspace(root)
        assert (reparsed["parsed_files"], reparsed["cached_files"]) == (2, 0)
        assert _graph_contents(graph) == expected
        cached = graph.sync_workspace(root)
        assert (cached["parsed_files"], cached["cached_files"]) == (0, 2)
        graph.db.execute(
            "UPDATE metadata SET value='stale-again' WHERE key='engine_sha'"
        )
        graph.db.commit()
        engine_before_failure = graph.db.execute(
            "SELECT value FROM metadata WHERE key='engine_sha'"
        ).fetchone()[0]

        (root / "a.py").write_text("def broken(:\n    pass\n")
        with pytest.raises(CodeGraphParseError, match="cannot parse a.py"):
            graph.sync_workspace(root)
        assert graph.db.execute(
            "SELECT value FROM metadata WHERE key='engine_sha'"
        ).fetchone()[0] == engine_before_failure
        assert _graph_contents(graph) == expected


def test_sync_serializes_snapshot_read_parse_and_write(tmp_path, monkeypatch):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    (root_a / "a.py").write_text("def from_a():\n    return 1\n")
    (root_b / "b.py").write_text("def from_b():\n    return 2\n")
    database = tmp_path / "graph.sqlite"
    contender = GraphStore(database)
    contender.db.execute("PRAGMA busy_timeout=0")
    original_extract = store_module.extract_file
    lock_errors = []

    def extract_with_competing_sync(path, source, *layout):
        monkeypatch.setattr(store_module, "extract_file", original_extract)
        try:
            contender.sync_workspace(root_b)
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            lock_errors.append(str(exc))
        return original_extract(path, source, *layout)

    monkeypatch.setattr(store_module, "extract_file", extract_with_competing_sync)
    try:
        with GraphStore(database) as graph:
            graph.sync_workspace(root_a)
            assert len(lock_errors) == 1
            assert graph.rows("SELECT path FROM files ORDER BY path") == [
                {"path": "a.py"}
            ]
            assert _symbols(graph) == {"a:", "a:from_a"}
    finally:
        contender.close()
