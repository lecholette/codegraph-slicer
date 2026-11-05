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


def test_slice_format_xml(tmp_path, capsys):
    db = _snapshot(tmp_path / "src", {
        "pkg/a.py": (
            "class OptionGroup:\n"
            "    def __init__(self, name: str):\n"
            "        self.name = name\n\n"
            "def helper():\n"
            "    return OptionGroup('test')\n"
        ),
    })
    exit_code = main(["slice", "--db", str(db), "pkg.a:OptionGroup", "--format", "xml"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "<context tokens=" in out
    assert "<target_implementations>" in out
    assert '<code_slice symbol="pkg.a:OptionGroup"' in out
    assert "class OptionGroup:" in out
    assert "</code_slice>" in out
    assert "<upstream_callers>" in out
    assert '<code_slice symbol="pkg.a:helper"' in out
    assert "</context>" in out


def test_slice_outline_format_xml(tmp_path, capsys):
    db = _snapshot(tmp_path / "src", {
        "pkg/a.py": (
            "def foo():\n"
            "    return bar()\n\n"
            "def bar():\n"
            "    return 1\n"
        ),
    })
    exit_code = main(["slice", "--db", str(db), "pkg.a:foo", "--outline", "--format", "xml"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "<slice_outline" in out
    assert "<entities>" in out
    assert '<entity id=' in out
    assert 'symbol="pkg.a:foo"' in out
    assert "<edges>" in out
    assert '<edge kind="CALLS"' in out
    assert "</slice_outline>" in out


def test_map_format_xml(tmp_path, capsys):
    db = _snapshot(tmp_path / "src", {
        "pkg/a.py": "def f(): pass\n",
        "pkg/b.py": "def g(): pass\n",
    })
    exit_code = main(["map", "--db", str(db), "--format", "xml"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "<repository_map tokens=" in out
    assert "<file path=" in out
    assert "</repository_map>" in out


def test_impact_format_xml(tmp_path, capsys):
    db = _snapshot(tmp_path / "src", {
        "pkg/a.py": "def leaf(): pass\n",
        "pkg/b.py": "from pkg.a import leaf\ndef caller(): leaf()\n",
    })
    exit_code = main(["impact", "pkg.a:leaf", "--db", str(db), "--format", "xml"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "<call_impact" in out
    assert '<caller id=' in out
    assert 'symbol="pkg.b:caller"' in out
    assert "</call_impact>" in out
