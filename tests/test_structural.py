import json
import sqlite3
import subprocess

import pytest

from codegraph_slicer import (
    CodeGraphError,
    CodeGraphParseError,
    GraphStore,
    InvalidRevisionError,
    UnknownSeedError,
    build_repo_map,
    compute_delta,
    extract_file,
    rank,
    slice_context,
    RequiredAnchorOverflow,
)
from codegraph_slicer.cli import main
from codegraph_slicer.slicer import InvalidBudgetError


def write(root, files):
    for name, source in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)


def test_extractor_preserves_contracts_and_strips_implementations():
    source = b'''"""module doc"""
from dataclasses import dataclass
@dataclass
class Record:
    value: int
    async def read(self, x: int = 2) -> str:
        """read doc"""
        if x:
            return str(self.value)
        return "empty"
type Records = list[Record]
'''
    analysis = extract_file("pkg/record.py", source)
    symbols = {s.symbol_id: s for s in analysis.symbols}
    assert symbols["pkg.record:Record"].signature == "class Record:"
    assert (
        symbols["pkg.record:Record.read"].signature
        == "async def read(self, x: int = 2) -> str:"
    )
    assert symbols["pkg.record:Record.read"].kind == "method"
    assert symbols["pkg.record:Record.read"].cx == 2
    assert (
        analysis.skeleton
        == '''"""module doc"""
from dataclasses import dataclass

@dataclass
class Record:
    value: int

    async def read(self, x: int=2) -> str:
        """read doc"""
        ...
type Records = list[Record]
'''
    )
    assert "return str(self.value)" in symbols["pkg.record:Record.read"].body
    assert symbols["pkg.record:Records"].kind == "type_alias"
    assert symbols["pkg.record:Records"].signature == "type Records = list[Record]"
    assert ("pkg.record:Records", "pkg.record:Record", "USES_TYPE") in {
        (e.source, e.target, e.kind) for e in analysis.edges
    }


def test_resolved_callers_annotations_tests_and_shadowing(tmp_path):
    write(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/base.py": "class Base:\n    pass\n",
            "pkg/types.py": "from .base import Base\nclass Payload(Base):\n    value: Base\n",
            "pkg/logic.py": """from .types import Payload

def work(x: Payload) -> "Payload":
    return x

def caller(x):
    return work(x)

class Worker:
    def work(self):
        return 0
    def run(self):
        work(None)
        return self.work()

def dynamic(work):
    return work()

if unknown_condition:
    from .logic import work as maybe

def conditional():
    return maybe(None)
""",
            "tests/test_logic.py": "from pkg.logic import work as target\ndef test_work():\n    assert target(None) is None\n",
        },
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        calls = {
            (r["source_id"], r["target_id"])
            for r in store.rows("SELECT * FROM edges WHERE kind='CALLS'")
        }
        assert calls == {
            ("pkg.logic:caller", "pkg.logic:work"),
            ("pkg.logic:Worker.run", "pkg.logic:work"),
            ("pkg.logic:Worker.run", "pkg.logic:Worker.work"),
            ("tests.test_logic:test_work", "pkg.logic:work"),
        }
        annotations = {
            (r["source_id"], r["target_id"])
            for r in store.rows("SELECT * FROM edges WHERE kind='USES_TYPE'")
        }
        assert annotations == {
            ("pkg.logic:work", "pkg.types:Payload"),
            ("pkg.types:Payload", "pkg.base:Base"),
        }
        assert store.rows(
            "SELECT target_id FROM edges WHERE source_id='pkg.types:Payload' AND kind='INHERITS'"
        ) == [{"target_id": "pkg.base:Base"}]
        assert store.rows(
            "SELECT target_id FROM edges WHERE source_id='tests.test_logic:test_work' AND kind='TESTS'"
        ) == [{"target_id": "pkg.logic:work"}]
        assert store.rows(
            "SELECT target FROM diagnostics WHERE source_id='pkg.logic:dynamic' AND kind='CALLS'"
        ) == [{"target": "?work"}]
        assert store.rows(
            "SELECT target FROM diagnostics WHERE source_id='pkg.logic:conditional' AND kind='CALLS'"
        ) == [{"target": "?maybe"}]
        assert store.rows(
            "SELECT line FROM raw_edges WHERE source_id='pkg.logic:work' AND kind='USES_TYPE' ORDER BY line"
        ) == [{"line": 3}]


def test_cached_references_rebind_and_invalid_sync_is_atomic(tmp_path):
    write(
        tmp_path, {"a.py": "from b import target\ndef caller():\n    return target()\n"}
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        assert store.rows("SELECT * FROM edges WHERE kind='CALLS'") == []
        write(tmp_path, {"b.py": "def target():\n    return 7\n"})
        stats = store.sync_workspace(tmp_path)
        assert (stats["parsed_files"], stats["cached_files"]) == (1, 1)
        assert store.rows(
            "SELECT source_id,target_id FROM edges WHERE kind='CALLS'"
        ) == [{"source_id": "a:caller", "target_id": "b:target"}]
        (tmp_path / "b.py").unlink()
        stats = store.sync_workspace(tmp_path)
        assert (
            stats["parsed_files"],
            stats["cached_files"],
            stats["deleted_files"],
        ) == (0, 1, 1)
        assert store.rows("SELECT * FROM edges WHERE kind='CALLS'") == []
        before = store.rows("SELECT * FROM files")
        write(tmp_path, {"a.py": "def invalid(:\n"})
        with pytest.raises(CodeGraphParseError, match="cannot parse a.py"):
            store.sync_workspace(tmp_path)
        assert store.rows("SELECT * FROM files") == before


def test_git_blob_cache_and_environment_are_isolated(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
    for arguments in (
        ["init", "-q"],
        ["config", "user.email", "test@example.invalid"],
        ["config", "user.name", "Fixture"],
    ):
        subprocess.run(["git", "-C", str(root), *arguments], env=env, check=True)
    write(root, {"a.py": "def f():\n    return 1\n"})
    subprocess.run(["git", "-C", str(root), "add", "a.py"], env=env, check=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-qm", "fixture"], env=env, check=True
    )
    real_run = subprocess.run
    captures = []

    def capture(args, **kwargs):
        captures.append((args, kwargs["env"]))
        return real_run(args, **kwargs)

    monkeypatch.setenv("GIT_DIR", "/nonexistent/poison")
    monkeypatch.setenv("GIT_WORK_TREE", "/nonexistent/poison")
    monkeypatch.setattr(subprocess, "run", capture)
    with GraphStore(":memory:") as store:
        assert store.sync_git_tree(root, "HEAD")["parsed_files"] == 1
        assert sum("cat-file" in args for args, _ in captures) == 1
        captures.clear()
        assert store.sync_git_tree(root, "HEAD")["cached_files"] == 1
        assert [args[3] for args, _ in captures] == ["rev-parse", "ls-tree"]
        assert [env["GIT_CONFIG_GLOBAL"] for _, env in captures] == [
            "/dev/null",
            "/dev/null",
        ]
        assert [env.get("GIT_DIR") for _, env in captures] == [None, None]
        assert store.rows("SELECT blob_sha FROM files")[0]["blob_sha"].startswith(
            "git:"
        )
        with pytest.raises(InvalidRevisionError, match="invalid revision"):
            store.sync_git_tree(root, "--upload-pack=bad")


def test_slice_type_closure_line_anchors_and_exact_budget(tmp_path):
    callee_signature = "def callee(" + ", ".join(
        f"argument_{index:02d}: int" for index in range(30)
    ) + "):\n    return 8\n\n"
    write(
        tmp_path,
        {
            "m.py": """class Root:
    flag: bool
class Value(Root):
    number: int

""" + callee_signature + """def target(x: Value) -> Value:
    callee()
    return x

def caller(x):
    return target(x)
""",
        },
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        result = slice_context(store, ["m.py:target"])
        sections = result["sections"]
        assert list(sections) == [
            "types_and_schemas",
            "upstream_callers",
            "target_implementations",
            "downstream_dependencies",
        ]
        assert [r["symbol"] for r in sections["types_and_schemas"]] == [
            "m:Root",
            "m:Value",
        ]
        assert [r["symbol"] for r in sections["upstream_callers"]] == ["m:caller"]
        assert [r["symbol"] for r in sections["target_implementations"]] == ["m:target"]
        assert [row["symbol"] for row in sections["downstream_dependencies"]] == ["m:callee"]
        assert sections["downstream_dependencies"][0]["text"].startswith("m.py:6-7\ndef callee(")
        assert slice_context(store, ["m.py:10"]) == result
        assert result["tokens"] == (len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) + 1) // 4 + 1
        required_sections = {
            name: rows if name in {"types_and_schemas", "target_implementations"} else []
            for name, rows in sections.items()
        }
        expected = {
            "tokens": 0,
            "sections": required_sections,
            "omitted": {
                "types_and_schemas": 0,
                "upstream_callers": 1,
                "target_implementations": 0,
                "downstream_dependencies": 1,
            },
            "retrieve_details": "rerun with --max-tokens N",
        }
        while True:
            budget = (len(json.dumps(expected, ensure_ascii=False, separators=(",", ":"))) + 1) // 4 + 1
            if expected["tokens"] == budget:
                break
            expected["tokens"] = budget
        bounded = slice_context(store, ["m:target"], max_tokens=budget)
        assert bounded == expected
        with pytest.raises(
            RequiredAnchorOverflow, match="required definitions need .* including omission metadata"
        ):
            slice_context(store, ["m:target"], max_tokens=budget - 1)
        bodies = slice_context(store, ["m:target"], include_callee_bodies=True)
        assert "return 8" in bodies["sections"]["downstream_dependencies"][0]["text"]


def test_delta_cycles_boundaries_body_drift_and_selected_test_gaps(tmp_path):
    base_root, head_root = tmp_path / "base", tmp_path / "head"
    shared = {
        "a.py": "def work():\n    return 1\n",
        "b.py": "def helper():\n    return 2\n",
        "cli.py": "from a import work\ndef main():\n    return work()\n",
        "tests/test_a.py": "from a import work\ndef test_work():\n    assert work() == 1\n",
    }
    write(base_root, shared)
    write(
        head_root,
        shared
        | {
            "a.py": "import b\ndef work():\n    if b.helper():\n        return 2\n    return 0\n",
            "b.py": "import a\ndef helper():\n    return 2\n",
        },
    )
    with GraphStore(":memory:") as base, GraphStore(":memory:") as head:
        base.sync_workspace(base_root)
        head.sync_workspace(head_root)
        result = compute_delta(base, head, [("a", "b")])
        assert result["new_cycles"] == [["a", "b"]]
        assert result["boundary_violations"] == [{"source": "a", "target": "b"}]
        assert "a:work" in result["modified_symbols"]
        assert "cli:main" in result["affected_entrypoints"]
        assert "a:work" in result["untested_modified_symbols"]
        assert {
            "symbol": "a:work",
            "cx_delta": 1,
            "fan_out_delta": 1,
            "threshold_exceeded": False,
        } in result["complexity_drift"]
        selected = compute_delta(base, head, selected_tests=["tests.test_a:test_work"])
        assert "a:work" not in selected["untested_modified_symbols"]
        assert ["a:work", "b:helper", "CALLS", 3] in result["added_edges"]
        assert compute_delta(head, head)["new_cycles"] == []
        write(
            head_root, {"a.py": "import outside.forbidden\ndef work():\n    return 3\n"}
        )
        head.sync_workspace(head_root)
        assert compute_delta(base, head, [("a", "outside")])["boundary_violations"] == [
            {"source": "a", "target": "outside.forbidden"}
        ]


def test_map_budget_deduplication_and_unknown_seeds(tmp_path):
    write(tmp_path, {"m.py": "def f():\n    return 1\ndef g():\n    return f()\n"})
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        result = build_repo_map(store, seed_paths=["m.py"])
        assert [r["path"] for r in result["items"]] == ["m.py"]
        assert result["tokens"] == (len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) + 1) // 4 + 1
        with pytest.raises(InvalidBudgetError, match="map omission metadata requires"):
            build_repo_map(store, max_tokens=1)
        assert rank(store)["m:f"] > rank(store)["m:g"]
        assert sum(rank(store).values()) == pytest.approx(1)
        assert rank(store) == rank(store)
        with pytest.raises(UnknownSeedError, match="missing"):
            rank(store, seeds=["missing"])


def test_cli_json_markdown_and_readonly_missing_database(tmp_path, capsys):
    root = tmp_path / "src"
    write(root, {"m.py": "def f():\n    return 1\n"})
    db = tmp_path / "graph.sqlite"
    assert main(["sync", str(root), "--db", str(db)]) == 0
    assert json.loads(capsys.readouterr().out)["parsed_files"] == 1
    assert main(["slice", "--db", str(db), "m:f", "--format", "markdown"]) == 0
    markdown = capsys.readouterr().out
    assert "## target implementations" in markdown
    assert "def f():\n    return 1" in markdown
    absent = tmp_path / "absent.sqlite"
    with pytest.raises(SystemExit, match="2"):
        main(["map", "--db", str(absent)])
    assert absent.exists() is False
    assert "OperationalError" in capsys.readouterr().err
    with GraphStore(db, readonly=True) as store:
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            store.db.execute("DELETE FROM files")


def test_unsafe_paths_symlinks_and_duplicate_definitions(tmp_path):
    with pytest.raises(CodeGraphError, match="invalid Python path"):
        extract_file("../escape.py", b"x=1")
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 1")
    root = tmp_path / "root"
    root.mkdir()
    (root / "link.py").symlink_to(outside)
    with GraphStore(":memory:") as store:
        with pytest.raises(CodeGraphError, match="symlink source refused"):
            store.sync_workspace(root)
        assert store.rows("SELECT * FROM files") == []
    analysis = extract_file(
        "props.py",
        b"class C:\n    @property\n    def x(self):\n        return 1\n    @x.setter\n    def x(self, value):\n        self._x = value\n",
    )
    prop = next(s for s in analysis.symbols if s.symbol_id == "props:C.x")
    assert "@property" in prop.body and "@x.setter" in prop.body

    rebound = extract_file(
        "repeat.py",
        b"def f():\n    def old():\n        return 1\n    return old()\ndef f():\n    return 2\n",
    )
    assert [s.symbol_id for s in rebound.symbols] == ["repeat:", "repeat:f"]
