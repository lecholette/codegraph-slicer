import pytest

from codegraph_slicer import GraphStore
from codegraph_slicer.cli import main


def test_empty_git_revision_is_rejected_without_indexing_workspace(tmp_path, capsys):
    root = tmp_path / "project"
    root.mkdir()
    (root / "sample.py").write_text("def indexed():\n    return True\n")
    db = tmp_path / "graph.sqlite"

    with pytest.raises(SystemExit, match="2") as error:
        main(["sync", str(root), "--revision", "", "--db", str(db)])

    assert error.value.code == 2
    assert "invalid revision" in capsys.readouterr().err
    with GraphStore(db, readonly=True) as store:
        assert store.rows("SELECT path FROM files") == []
