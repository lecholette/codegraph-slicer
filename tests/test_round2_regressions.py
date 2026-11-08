"""Regression and contract tests for Round-2 static analysis review (R2-01..R2-12)."""

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile

import pytest

from codegraph_slicer import GraphStore, slice_context
from codegraph_slicer import slicer
from codegraph_slicer.cli import (
    _OutlineMarkdownEstimator,
    _markdown,
    _xml,
    main,
    render_markdown_report,
    render_xml_report,
)
from codegraph_slicer.delta import DeltaReport
from codegraph_slicer.flow_query import query_flow
from codegraph_slicer.java_extractor import (
    _candidate_overloads_for_arity,
    _extract_call_args,
    _infer_java_arg_type,
    _is_varargs_param_tuple,
    extract_java_file,
)
from codegraph_slicer.pagerank import build_repo_map, build_repo_outline
from codegraph_slicer.slicer import IndexedSymbol, _extract_py_class_fields, _read_session_dedup
from codegraph_slicer.store import UnresolvedCallRef
from codegraph_slicer.treesitter_support import _pop_closed_classes, parser_for, scoped_descendants


@contextlib.contextmanager
def workspace(files: dict[str, str]):
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "repo"
        root.mkdir()
        db = Path(td) / "graph.sqlite"
        for name, src in files.items():
            p = root / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(src, encoding="utf-8")
        with GraphStore(db) as store:
            store.sync_workspace(root)
            yield store, root, db


@pytest.mark.parametrize("renderer", [_xml, _markdown])
def test_partial_anchor_status_survives_projection(renderer):
    """R2-04: Unresolved anchors in a partial batch must be visible in XML and Markdown projections."""
    with workspace({"a.py": "def f():\n    return 1\n"}) as (s, r, d):
        report = slice_context(s, ["a:f", "a:missing"])
        assert "a:missing" in renderer(report)


def test_outline_diagnostic_counts_survive_xml():
    """R2-04: Slice outline diagnostic_counts must survive XML projection."""
    with workspace({"a.py": "def f():\n    return mystery()\ndef g():\n    return f() + unknown()\n"}) as (s, r, d):
        report = slicer.build_slice_outline(s, ["a:f"])
        assert report["diagnostic_counts"]
        assert "diagnostic_counts" in _xml(report)


def test_impact_completeness_survives_xml():
    """R2-04: Call impact unknown_boundary_count and truncated status must survive XML projection."""
    with workspace({"a.py": "def f():\n    return mystery()\ndef g():\n    return f()\n"}) as (s, r, d):
        report = slicer.query_call_impact(s, "a:f", unknown_boundary_limit=1)
        assert "unknown_boundary_count" in _xml(report)


def test_cache_merge_preserves_other_sessions(tmp_path):
    """R2-06: Concurrent session cache commits must re-read and merge rather than clobbering other sessions."""
    p = tmp_path / "g.session.json"
    a = slicer._parse_session_cache_file(p)
    b = slicer._parse_session_cache_file(p)
    slicer._commit_session_dedup(p, "A", a, {"a:f": "one"})
    slicer._commit_session_dedup(p, "B", b, {"a:g": "two"})
    assert set(json.loads(p.read_text(encoding="utf-8"))) == {"A", "B"}


def test_summary_delivery_does_not_cache_full_class():
    """R2-02: A low-budget summary response must not record full-body hashes that suppress later high-budget requests."""
    source = (
        "class Config:\n    host: str\n"
        + "".join(
            f"    def method{i}(self):\n"
            + "".join(f"        a{j} = {j}\n" for j in range(10))
            + "        return 0\n"
            for i in range(2)
        )
        + "def run(c: Config):\n    return c\n"
    )
    with workspace({"a.py": source}) as (s, r, d):
        first = slice_context(s, ["a:run"], database=d, max_tokens=150, dedup_session="A")
        assert "Expansion" in first["sections"]["types_and_schemas"][0]["text"]
        second = slice_context(s, ["a:run"], database=d, max_tokens=8000, dedup_session="A")
        assert "a9 = 9" in second["sections"]["types_and_schemas"][0]["text"]


@pytest.mark.parametrize("kind", ["map", "flow"])
def test_query_uses_one_revision(kind):
    """R2-01: Multi-query readers (build_repo_map, query_flow, etc.) must pin a consistent read snapshot."""
    with workspace({"a.py": "def f():\n    return 1\n"}) as (s, r, d):
        with GraphStore(d, readonly=True) as reader:
            original = reader.rows
            fired = False

            def hook(sql, args=()):
                nonlocal fired
                result = original(sql, args)
                trigger = (
                    ("SELECT body FROM symbols WHERE file_path=" in sql)
                    if kind == "flow"
                    else sql == "SELECT symbol_id FROM symbols"
                )
                if trigger and not fired:
                    fired = True
                    (r / "a.py").write_text(
                        "def " + ("g" if kind == "map" else "f") + "():\n    return 2\n",
                        encoding="utf-8",
                    )
                    with GraphStore(d) as w:
                        w.sync_workspace(r)
                return result

            reader.rows = hook
            if kind == "map":
                result = build_repo_map(reader)
                assert result["items"]
            else:
                report = query_flow(reader, "a:f")["flow"]
                old_sha = "sha256:" + hashlib.sha256(b"def f():\n    return 1\n").hexdigest()
                assert report["identity"]["blob_sha"] == old_sha


@pytest.mark.parametrize(
    "methods,body,expected",
    [
        ("void f(int x) {} void f(double x) {}", "f(1L);", "Demo:Demo.f#2"),
        ("void f(int x) {} void f(String x) {}", 'f(/* comment */ "x");', "Demo:Demo.f#2"),
        ("void f(int x) {} void f(String... xs) {}", 'f("a", "b");', "Demo:Demo.f#2"),
        ("Demo(int x) {} Demo(String x) {}", 'new Demo("x");', "Demo:Demo.Demo#2"),
    ],
)
def test_java_retains_actual_target(methods, body, expected):
    """R2-03: Java overload resolution must handle numeric suffixes/widening, commented args, varargs, and constructors."""
    src = f"public class Demo {{\n {methods}\n void run() {{\n {body}\n }}\n}}\n"
    a = extract_java_file("Demo.java", src.encode("utf-8"))
    targets = {e.target for e in a.edges if e.source.endswith(".run") and e.kind == "CALLS"}
    assert expected in targets


def test_nested_private_folding_keeps_class_ownership():
    """R2-09: Nested classes must not reset the enclosing class's private-method folding group."""
    src = """public class Outer {
  private void a1() {}
  private void a2() {}
  private void a3() {}
  class Inner {
    private void i1() {}
    private void i2() {}
  }
  private void a4() {}
  private void a5() {}
}
"""
    skel = extract_java_file("Outer.java", src.encode("utf-8")).skeleton
    assert "a4()" not in skel
    assert "i2()" in skel
    assert "+2 private methods" in skel


def test_bare_dedup_flag_preserves_anchor_position():
    """R2-07: Bare --dedup-session before positional anchors must not swallow the first anchor."""
    with workspace({"a.py": "def f():\n    return 1\n"}) as (s, r, d):
        with contextlib.redirect_stdout(io.StringIO()):
            assert main(["slice", "--db", str(d), "--dedup-session", "a:f"]) == 0


def test_library_rejects_invalid_target_mode():
    """R2-07: slice_context must validate target_mode at the library boundary."""
    with workspace({"a.py": "def f():\n    return 1\n"}) as (s, r, d):
        with pytest.raises(ValueError):
            slice_context(s, ["a:f"], target_mode="typo")


def test_snapshot_context_preserves_caller_transaction():
    """R2-01: read_snapshot() must preserve caller-owned transactions."""
    with workspace({"a.py": "def f():\n    return 1\n"}) as (s, r, d):
        assert not s.db.in_transaction
        s.db.execute("UPDATE metadata SET value=value WHERE key='snapshot_root'")
        with s.read_snapshot():
            slice_context(s, ["a:f"])
        assert s.db.in_transaction
        s.db.rollback()


def test_truncated_repository_outline_renders_markdown():
    """R2-05: Truncated build_repo_outline must render cleanly in Markdown without KeyError/TypeError."""
    source = "".join(
        f"def long_function_name_{i}(some_parameter: int) -> str:\n    return str(some_parameter)\n"
        for i in range(25)
    )
    with workspace({"a.py": source}) as (s, r, d):
        result = build_repo_outline(s, max_tokens=300, render_estimator=_OutlineMarkdownEstimator())
        assert result["omitted"]["entities"]
        assert "Omitted" in _markdown(result)


def test_round2_direct_helper_contracts(tmp_path):
    """Direct unit tests for Round-2 extracted models and helpers."""
    sym = IndexedSymbol.from_row({"symbol_id": "a:f", "file_path": "a.py", "start_line": 1, "end_line": 2})
    assert IndexedSymbol.from_row(sym) is sym
    assert sym.symbol_id == "a:f"

    ref = UnresolvedCallRef.from_row(
        {"source_file": "a.py", "source_id": "a:f", "kind": "CALLS", "line": 4}
    )
    assert UnresolvedCallRef.from_row(ref) is ref
    assert ref.line == 4

    raw_delta = {
        "boundary_violations": [],
        "new_cycles": [],
        "complexity_drift": [],
        "added_edges": [],
        "removed_edges": [],
        "modified_symbols": ["a:f"],
        "affected_entrypoints": [],
        "untested_modified_symbols": [],
        "test_coverage": [],
        "test_gap_note": "note",
    }
    dr = DeltaReport.from_dict(raw_delta)
    assert DeltaReport.from_dict(dr) is dr
    assert dr.modified_symbols == ["a:f"]

    assert _extract_call_args(None) == []
    assert _is_varargs_param_tuple(("String...",)) is True
    assert _is_varargs_param_tuple(("String",)) is False
    cands = _candidate_overloads_for_arity(
        {1: [(("int",), "Demo.f"), (("String...",), "Demo.f#2")]}, 2
    )
    assert cands == [(("String", "String"), "Demo.f#2")]

    j_tree = parser_for("java").parse(b"class T { void m() { f(1.5f, 2.5); } }")
    inv_nodes = [
        n
        for n in scoped_descendants(j_tree.root_node, set())
        if n.type == "method_invocation"
    ]
    args = _extract_call_args(inv_nodes[0].children[-1])
    assert _infer_java_arg_type(b"class T { void m() { f(1.5f, 2.5); } }", args[0], {}, {}) == "float"
    assert _infer_java_arg_type(b"class T { void m() { f(1.5f, 2.5); } }", args[1], {}, {}) == "double"

    stack = [(-1, [0]), (2, [1, 2])]
    _pop_closed_classes(stack, 2)
    assert len(stack) == 1

    many_fields = "class Big:\n    pub = _priv = 1\n" + "".join(f"    f_{i}: int = {i}\n" for i in range(14))
    extracted = _extract_py_class_fields({"body": many_fields})
    assert extracted[0] == "    pub = _priv = 1"
    assert extracted[-1] == "    # ... (+3 more fields)"
    assert _read_session_dedup(tmp_path / "db.sqlite", "   ") == (None, "", {})
    assert "<codegraph_report>" in render_xml_report({"custom": 1})
    assert "# Code graph report" in render_markdown_report({"custom": 1})

    with workspace(
        {
            "srv.py": "def create_handler():\n    class ConfigRequestHandler:\n        pass\n    return ConfigRequestHandler\n",
            "api.py": "class ConfigAPI:\n    pass\n",
        }
    ) as (s, r, d):
        res = slice_context(s, ["api.py:ConfigAPI", "srv.py:ConfigHTTPRequestHandler"])
        target_sids = {item["symbol"] for item in res["sections"]["target_implementations"]}
        assert "srv:create_handler.ConfigRequestHandler" in target_sids
        assert "unresolved_anchors" not in res


def test_stage1_js_ts_di_union_and_monorepo_aliases():
    with workspace(
        {
            "svc.js": (
                "class DefaultValidator {\n"
                "  validate() { return true; }\n"
                "}\n"
                "class HandlerA { execute() { return 1; } }\n"
                "class HandlerB { execute() { return 2; } }\n"
                "class Registry {\n"
                "  /** @returns {HandlerA|HandlerB|undefined} */\n"
                "  resolve(k) { return k ? new HandlerA() : new HandlerB(); }\n"
                "}\n"
                "class Engine {\n"
                "  /** @param {DefaultValidator} [validator] */\n"
                "  constructor(validator = new DefaultValidator(), reg = new Registry()) {\n"
                "    this.validator = validator || new DefaultValidator();\n"
                "    this.reg = reg;\n"
                "  }\n"
                "  run(k) {\n"
                "    this.validator.validate();\n"
                "    const h = this.reg.resolve(k);\n"
                "    return h.execute();\n"
                "  }\n"
                "}\n"
            ),
            "packages/web/src/lib/util.ts": "export function cn(x: string): string { return x; }\n",
            "packages/web/src/store.ts": (
                "import { cn } from '@/lib/util';\n"
                "function create(fn: any) { return fn; }\n"
                "export const useStore = create(() => ({ label: cn('ok') }));\n"
            ),
            "packages/web/src/app.ts": (
                "import { useStore } from '@/store';\n"
                "export function boot() { return useStore(); }\n"
            ),
        }
    ) as (store, _, _):
        edges = {(r["source_id"], r["target_id"], r["kind"]) for r in store.rows("SELECT * FROM edges")}
        assert ("svc:Engine.run", "svc:DefaultValidator.validate", "CALLS") in edges
        assert ("svc:Engine.run", "svc:Registry.resolve", "CALLS") in edges
        assert ("svc:Engine.run", "svc:HandlerA.execute", "CALLS") in edges
        assert ("svc:Engine.run", "svc:HandlerB.execute", "CALLS") in edges
        assert ("packages.web.src.app:boot", "packages.web.src.store:useStore", "CALLS") in edges


def test_stage2_python_context_manager_type_inference():
    with workspace(
        {
            "ctx.py": (
                "from contextlib import ExitStack\n"
                "class Store:\n"
                "    def __enter__(self):\n"
                "        return self\n"
                "    def __exit__(self, *a):\n"
                "        pass\n"
                "    def query(self) -> int:\n"
                "        return 1\n"
                "    def sync(self) -> int:\n"
                "        return 2\n"
                "def run(stack: ExitStack):\n"
                "    with Store() as st:\n"
                "        st.query()\n"
                "    st2 = stack.enter_context(Store())\n"
                "    return st2.sync()\n"
            ),
        }
    ) as (store, _, _):
        edges = {(r["source_id"], r["target_id"], r["kind"]) for r in store.rows("SELECT * FROM edges")}
        assert ("ctx:run", "ctx:Store.query", "CALLS") in edges
        assert ("ctx:run", "ctx:Store.sync", "CALLS") in edges


def test_stage3_java_overload_slicing_and_subtype_fanout():
    with workspace(
        {
            "src/main/java/demo/Handler.java": (
                "package demo;\n"
                "public interface Handler {\n"
                "    String exec(String x);\n"
                "    String exec(String x, int y);\n"
                "}\n"
            ),
            "src/main/java/demo/PackHandler.java": (
                "package demo;\n"
                "public class PackHandler implements Handler {\n"
                "    public String helper() { return \"ok\"; }\n"
                "    @Override public String exec(String x) { return exec(x, 1); }\n"
                "    @Override public String exec(String x, int y) { return helper(); }\n"
                "}\n"
            ),
            "src/main/java/demo/Runner.java": (
                "package demo;\n"
                "public class Runner {\n"
                "    public String go(Handler h) { return h.exec(\"a\"); }\n"
                "}\n"
            ),
        }
    ) as (store, _, _):
        edges = {(r["source_id"], r["target_id"], r["kind"]) for r in store.rows("SELECT * FROM edges")}
        assert ("demo.Runner:Runner.go", "demo.PackHandler:PackHandler.exec", "CALLS") in edges
        assert ("demo.Runner:Runner.go", "demo.PackHandler:PackHandler.exec#2", "CALLS") in edges

        sl = slice_context(store, ["demo.PackHandler:PackHandler.exec"])
        t_sids = {item["symbol"] for item in sl["sections"]["target_implementations"]}
        d_sids = {item["symbol"] for item in sl["sections"]["downstream_dependencies"]}
        assert "demo.PackHandler:PackHandler.exec" in t_sids
        assert "demo.PackHandler:PackHandler.exec#2" in t_sids
        assert "demo.PackHandler:PackHandler.helper" in d_sids





