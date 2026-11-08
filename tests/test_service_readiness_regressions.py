"""Regression tests for service-readiness review findings (F1-F6)."""

import contextlib
import io
import json
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET

import pytest

from codegraph_slicer import GraphStore, slice_context
from codegraph_slicer.cli import main
from codegraph_slicer.java_extractor import (
    _base_type_name,
    _extract_param_types,
    _infer_java_arg_type,
    _param_matches_arg,
    _select_overload_targets,
    extract_java_file,
)
from codegraph_slicer.slicer import (
    AnchorNotFoundError,
    RequiredAnchorOverflow,
    _extract_py_class_fields,
    _is_public_class_field_stmt,
    _parse_session_cache_file,
    _read_session_dedup,
    _resolve_anchor_batch_with_status,
    _resolve_batch_method_fallback,
    _session_scope_key,
)
from codegraph_slicer.treesitter_support import (
    _group_private_lines_by_class,
    _is_class_boundary_line,
)


@contextlib.contextmanager
def _workspace_fixture(files: dict[str, str]):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "repo"
        root.mkdir()
        for rel_path, content in files.items():
            full_path = root / rel_path
            full_path.parent.mkdir(parents=True, exist_ok=True)
            full_path.write_text(content, encoding="utf-8")
        db_path = Path(tmp) / "graph.sqlite"
        with GraphStore(db_path) as store:
            store.sync_workspace(root)
            yield store, root, db_path


def test_f1_session_dedup_detects_body_change_and_scopes_sessions():
    """F1: Same-length body edit after sync must NOT be suppressed as cached, and distinct sessions do not collide."""
    files = {
        "a.py": (
            "class Config:\n"
            "    host: str\n"
            "    port: int = 80\n"
        ),
        "b.py": (
            "from a import Config\n"
            "def run(c: Config) -> str:\n"
            "    return str(c)\n"
        ),
    }
    with _workspace_fixture(files) as (store, root, db_path):
        s1 = slice_context(store, ["b:run"], database=db_path, dedup_session="sess-A")
        t1 = s1["sections"]["types_and_schemas"][0]["text"]
        assert "host: str" in t1
        assert "[cached in earlier slice]" not in t1

        # Second call in same session with unchanged code -> deduplicated
        s2 = slice_context(store, ["b:run"], database=db_path, dedup_session="sess-A")
        t2 = s2["sections"]["types_and_schemas"][0]["text"]
        assert "[cached in earlier slice]" in t2

        # Different session ID ("sess-B") on the same DB must NOT be suppressed by sess-A
        s_other = slice_context(store, ["b:run"], database=db_path, dedup_session="sess-B")
        t_other = s_other["sections"]["types_and_schemas"][0]["text"]
        assert "host: str" in t_other
        assert "[cached in earlier slice]" not in t_other

        # Same-length edit in a.py: host: str -> host: int
        (root / "a.py").write_text(
            "class Config:\n"
            "    host: int\n"
            "    port: int = 80\n",
            encoding="utf-8",
        )
        store.sync_workspace(root)

        # sess-A must receive the updated Config definition, NOT "[cached in earlier slice]"
        s3 = slice_context(store, ["b:run"], database=db_path, dedup_session="sess-A")
        t3 = s3["sections"]["types_and_schemas"][0]["text"]
        assert "host: int" in t3
        assert "[cached in earlier slice]" not in t3


def test_f2_session_dedup_does_not_record_failed_or_first_turn_overflow():
    """F2: Overflowing slice or first-turn budget fallback must not mark undelivered items as cached."""
    files = {
        "a.py": (
            "class Config:\n"
            "    host: str = 'localhost'\n"
            "    port: int = 8080\n\n"
            "def helper_one(x: int) -> int:\n"
            "    return x + 1\n\n"
            "def helper_two(x: int) -> int:\n"
            "    return x + 2\n\n"
            "def run(c: Config) -> int:\n"
            "    a = helper_one(c.port)\n"
            "    b = helper_two(a)\n"
            "    return a + b\n"
        ),
    }
    with _workspace_fixture(files) as (store, root, db_path):
        # 1. Malformed .session.json ("[]") must not crash
        Path(f"{db_path}.session.json").write_text("[]", encoding="utf-8")

        # 2. Failing request with max_tokens=1 must NOT record Config or helpers as delivered
        with pytest.raises(RequiredAnchorOverflow):
            slice_context(store, ["a:run"], max_tokens=1, database=db_path, dedup_session=True)

        # 3. First valid budget-constrained slice that exercises fallback path must NOT say "cached in earlier slice"
        res = slice_context(store, ["a:run"], max_tokens=140, database=db_path, dedup_session=True)
        for sec_items in res["sections"].values():
            for item in sec_items:
                assert "[cached in earlier slice]" not in item["text"]


def test_f3_read_snapshot_prevents_mixed_revisions_during_concurrent_sync():
    """F3: A slice pinned with store.read_snapshot() sees a consistent single revision even if a writer syncs."""
    files = {
        "a.py": (
            "class Config:\n"
            "    field: str = 'old_value'\n\n"
            "def run(c: Config) -> str:\n"
            "    return 'old_impl'\n"
        ),
    }
    with _workspace_fixture(files) as (store, root, db_path):
        with GraphStore(db_path, readonly=True) as reader:
            with reader.read_snapshot():
                target_rows_before = reader.rows("SELECT body FROM symbols WHERE symbol_id='a:run'")
                assert "old_impl" in target_rows_before[0]["body"]

                # Writer updates workspace and commits a new revision while reader holds read_snapshot
                (root / "a.py").write_text(
                    "class Config:\n"
                    "    field: str = 'new_value'\n\n"
                    "def run(c: Config) -> str:\n"
                    "    return 'new_impl'\n",
                    encoding="utf-8",
                )
                with GraphStore(db_path) as writer:
                    writer.sync_workspace(root)

                # Inside the same read_snapshot(), subsequent SELECTs must still see 'old_value'
                type_rows_during = reader.rows("SELECT body FROM symbols WHERE symbol_id='a:Config'")
                assert "old_value" in type_rows_during[0]["body"]
                assert "new_value" not in type_rows_during[0]["body"]

            # Once read_snapshot() exits, a new read sees the committed 'new_value'
            type_rows_after = reader.rows("SELECT body FROM symbols WHERE symbol_id='a:Config'")
            assert "new_value" in type_rows_after[0]["body"]


def test_f4_python_compact_type_summary_preserves_dataclass_schema_fields():
    """F4: Automatic Python type summaries must retain class-level annotated fields and decorators."""
    methods_code = "\n".join(
        f"    def method_{i}(self, x: int) -> int:\n"
        + "\n".join(f"        v_{j} = x + {j}" for j in range(12))
        + "\n        return v_0\n"
        for i in range(4)
    )
    files = {
        "models.py": (
            "from dataclasses import dataclass\n\n"
            "@dataclass(frozen=True)\n"
            "class ServiceSettings:\n"
            "    required_host: str\n"
            "    critical_timeout: int = 30\n"
            f"{methods_code}"
        ),
        "runner.py": (
            "from models import ServiceSettings\n\n"
            "def execute(cfg: ServiceSettings) -> int:\n"
            "    return cfg.method_0(1)\n"
        ),
    }
    with _workspace_fixture(files) as (store, root, db_path):
        res = slice_context(store, ["runner:execute"], max_tokens=8000, database=db_path)
        types_text = "\n".join(item["text"] for item in res["sections"]["types_and_schemas"])
        assert "required_host: str" in types_text
        assert "critical_timeout: int = 30" in types_text


def test_f5_brace_private_method_folding_preserves_same_line_public_and_per_class_counts():
    """F5: Line-based folding must not delete public methods sharing a line with a private method, and counts per class."""
    files = {
        "service.js": (
            "export class Alpha {\n"
            "  #h1() { return 1; }\n"
            "  #h2() { return 2; }\n"
            "  #h3() { return 3; }\n"
            "  #h4() { return 4; } publicAPI() { return 42; }\n"
            "}\n"
            "export class Beta {\n"
            "  #b1() { return 10; }\n"
            "  #b2() { return 20; }\n"
            "}\n"
        ),
    }
    with _workspace_fixture(files) as (store, root, db_path):
        skel = store.rows("SELECT skeleton FROM files WHERE path='service.js'")[0]["skeleton"]
        assert "publicAPI()" in skel
        assert "+0 private methods" not in skel
        assert "#b1()" in skel
        assert "#b2()" in skel


def test_f6_partial_anchor_batch_reports_unresolved_without_silent_class_substitution():
    """F6: Requesting a:run and a:nonexistent must not silently substitute a:Config and must report unresolved_anchors."""
    files = {
        "a.py": (
            "class Config:\n"
            "    host: str = 'localhost'\n\n"
            "def run() -> str:\n"
            "    return 'ok'\n"
        ),
    }
    with _workspace_fixture(files) as (store, root, db_path):
        res = slice_context(store, ["a:run", "a:nonexistent"], database=db_path)
        target_symbols = [item["symbol"] for item in res["sections"]["target_implementations"]]
        assert target_symbols == ["a:run"]
        assert "a:Config" not in target_symbols
        assert res.get("unresolved_anchors") == ["a:nonexistent"]


def test_p2_java_same_arity_overload_resolution_by_argument_type():
    """P2: Java same-arity overloads f(int) and f(String) resolve to the matching overload on literal/typed arguments."""
    files = {
        "src/main/java/demo/OverloadDemo.java": (
            "package demo;\n"
            "public class OverloadDemo {\n"
            "    public int f(int x) { return x + 1; }\n"
            "    public String f(String s) { return s.trim(); }\n"
            "    public String caller() {\n"
            "        return f(\"hello\");\n"
            "    }\n"
            "}\n"
        ),
    }
    with _workspace_fixture(files) as (store, root, db_path):
        edges = store.rows(
            "SELECT target_id FROM edges WHERE source_id='demo.OverloadDemo:OverloadDemo.caller' AND kind='CALLS'"
        )
        targets = {r["target_id"] for r in edges}
        assert "demo.OverloadDemo:OverloadDemo.f#2" in targets
        assert "demo.OverloadDemo:OverloadDemo.f" not in targets


def test_p2_xml_outline_diagnostics_and_report_escaping():
    """P2: XML slice outline includes diagnostics and generic XML fallback escapes '<' characters."""
    files = {
        "a.py": (
            "def f(x: int) -> int:\n"
            "    if x < 3:\n"
            "        return mystery_call(x)\n"
            "    return x\n"
        ),
    }
    with _workspace_fixture(files) as (store, root, db_path):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = main(["slice", "--db", str(db_path), "a:f", "--outline", "--format", "xml"])
        assert rc == 0
        xml_out = buf.getvalue()
        root_el = ET.fromstring(xml_out)
        assert "mystery_call" in xml_out
        assert root_el.find("diagnostics") is not None

        buf_flow = io.StringIO()
        with contextlib.redirect_stdout(buf_flow):
            rc_flow = main(["flow", "a:f", "--db", str(db_path), "--format", "xml"])
        assert rc_flow == 0
        ET.fromstring(buf_flow.getvalue())


def test_direct_unit_coverage_of_service_readiness_helpers(tmp_path):
    """Direct unit tests for extracted helpers to ensure 100% branch and static graph reachability."""
    java_src = (
        b"package demo;\n"
        b"public class HelperDemo {\n"
        b"    private String label;\n"
        b"    public void run(int a) {}\n"
        b"    public void run(String b) {}\n"
        b"    public void run(HelperDemo c) {}\n"
        b"    public void testCalls() {\n"
        b"        String localStr = \"x\";\n"
        b"        this.run(10);\n"
        b"        this.run(localStr);\n"
        b"        this.run(new HelperDemo());\n"
        b"        this.run(unknownCall());\n"
        b"        this.run(label);\n"
        b"    }\n"
        b"}\n"
    )
    analysis = extract_java_file("src/main/java/demo/HelperDemo.java", java_src)
    call_targets = [
        (e.target, e.line)
        for e in analysis.edges
        if e.source == "demo.HelperDemo:HelperDemo.testCalls" and e.kind == "CALLS"
    ]
    assert ("demo.HelperDemo:HelperDemo.run", 9) in call_targets
    assert ("demo.HelperDemo:HelperDemo.run#2", 10) in call_targets
    assert ("demo.HelperDemo:HelperDemo.run#3", 11) in call_targets
    assert ("demo.HelperDemo:HelperDemo.run#2", 13) in call_targets
    assert ("demo.HelperDemo:HelperDemo.run", 13) not in call_targets
    # Unknown argument falls back to all 3 same-arity overloads
    line12_targets = {t for t, ln in call_targets if ln == 12}
    assert {
        "demo.HelperDemo:HelperDemo.run",
        "demo.HelperDemo:HelperDemo.run#2",
        "demo.HelperDemo:HelperDemo.run#3",
    } <= line12_targets

    assert _base_type_name(b"", None) is None
    assert _extract_param_types(b"", None) == ()
    assert _param_matches_arg("Integer", "int") is True
    assert _param_matches_arg("String", "int") is False
    assert _param_matches_arg(None, "int") is True
    assert _param_matches_arg("int", None) is True
    assert _param_matches_arg("int", "?") is True
    assert _select_overload_targets("missing", [], "pkg:Cls", {}, b"", {}, {}) == ["pkg:Cls.missing"]

    assert _extract_py_class_fields({"body": ""}) == []
    assert _extract_py_class_fields({"body": "def not_a_class():\n    pass\n"}) == []
    assert _extract_py_class_fields({"body": "class Broken(\n"}) == []
    assert _extract_py_class_fields(
        {"body": "class C:\n    DEFAULT_PORT = 80\n    obj.attr = 1\n    _secret: str = 'x'\n"}
    ) == ["    DEFAULT_PORT = 80"]

    assert _session_scope_key(True) == "default"
    monkeypatch_env = pytest.MonkeyPatch()
    monkeypatch_env.setenv("CODEGRAPH_DEDUP_SESSION", "env-sess")
    try:
        assert _session_scope_key(True) == "env-sess"
    finally:
        monkeypatch_env.undo()
    assert _read_session_dedup(":memory:", True) == (None, "", {})

    bad_cache = tmp_path / "bad.session.json"
    bad_cache.write_text("{invalid json", encoding="utf-8")
    assert _parse_session_cache_file(bad_cache) == {}

    assert _is_class_boundary_line("export class Foo {") is True
    assert _is_class_boundary_line("function foo() {") is False
    groups = _group_private_lines_by_class(
        [
            "class A {",
            "  #a1() { ... }",
            "}",
            "class Empty {",
            "}",
            "class B {",
            "  #b1() { ... }",
            "}",
        ]
    )
    assert groups == [[1], [6]]

    with _workspace_fixture(
        {"m.py": "class Server:\n    def handle_req(self) -> int:\n        return 1\n"}
    ) as (store, _, _):
        fb = _resolve_batch_method_fallback(store, "m:WrongClass.handle_req")
        assert fb is not None and fb["symbol_id"] == "m:Server.handle_req"
        assert _resolve_batch_method_fallback(store, "m:no_dot") is None
        with pytest.raises(AnchorNotFoundError):
            _resolve_anchor_batch_with_status(store, ["m:WrongClass.handle_req"])
        chosen, unresolved = _resolve_anchor_batch_with_status(
            store, ["m:WrongClass.handle_req", "m:missing"]
        )
        assert "m:Server.handle_req" in chosen
        assert unresolved == ["m:missing"]

