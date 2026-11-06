"""Regression tests for the 11 findings (F01-F11) from the gpt-6-astra certification audit."""
import contextlib
import io
import json
from pathlib import Path
import tempfile
import xml.etree.ElementTree as ET

from codegraph_slicer import GraphStore, slice_context
from codegraph_slicer.cli import main, _xml
from codegraph_slicer.slicer import query_call_impact, AnchorNotFoundError


@contextlib.contextmanager
def _fixture(files):
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory) / "source"
        root.mkdir()
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        db = Path(directory) / "graph.sqlite"
        with GraphStore(db) as graph:
            graph.sync_workspace(root)
            yield graph, root, db


def _calls(graph):
    return graph.rows("SELECT source_id,target_id,line FROM edges WHERE kind='CALLS'")


def test_f01_sync_file_rejected_and_preserves_snapshot():
    with _fixture({"m.py": "def f(): pass\n"}) as (g, root, db):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                rc = main(["sync", str(root / "m.py"), "--db", str(db)])
            except SystemExit as exc:
                rc = exc.code
        assert rc != 0
        assert len(g.rows("SELECT * FROM symbols")) >= 2


def test_f02_python_receiver_reassignment_retains_both_bindings():
    python_types = (
        "class A:\n"
        "    def run(self): pass\n"
        "class B:\n"
        "    def run(self): pass\n"
        "def caller():\n"
        "    x = A()\n"
        "    x.run()\n"
        "    x = B()\n"
        "    x.run()\n"
    )
    with _fixture({"m.py": python_types}) as (g, root, db):
        assert any(r["target_id"] == "m:A.run" for r in _calls(g))
        assert any(r["target_id"] == "m:B.run" for r in _calls(g))
        assert len(query_call_impact(g, "m:A.run")["callers"]) == 1


def test_f03_callback_bindings_do_not_leak_into_enclosing_scopes():
    ts_callback = (
        "class Alpha { run() {} }\n"
        "class Beta { run() {} }\n"
        "export function outer(a: Alpha) {\n"
        "    const callback = () => { const a = new Beta(); a.run(); };\n"
        "    a.run();\n"
        "}\n"
    )
    java_callback = (
        "class Alpha { void run() {} }\n"
        "class Beta { void run() {} }\n"
        "class Main {\n"
        "    Alpha value;\n"
        "    void outer() {\n"
        "        Runnable callback = () -> { Beta value = new Beta(); value.run(); };\n"
        "        value.run();\n"
        "    }\n"
        "}\n"
    )
    for name, source, line in [("m.ts", ts_callback, 5), ("Main.java", java_callback, 7)]:
        with _fixture({name: source}) as (g, root, db):
            assert any(r["target_id"].endswith(":Alpha.run") and r["line"] == line for r in _calls(g))
            assert not any(r["target_id"].endswith(":Beta.run") and r["line"] == line for r in _calls(g))


def test_f04_unparenthesized_default_ts_arrow_export():
    with _fixture(
        {
            "a.ts": "export default x => x + 1;\n",
            "b.ts": "import bump from './a'; export function call(){ return bump(1); }\n",
        }
    ) as (g, root, db):
        assert g.rows("SELECT symbol_id FROM symbols WHERE symbol_id='a:default'")
        assert any(r["source_id"] == "b:call" and r["target_id"] == "a:default" for r in _calls(g))


def test_f05_user_defined_ts_fetch_resolves():
    with _fixture({"m.ts": "export function fetch() {}\nexport function call() { fetch(); }\n"}) as (g, root, db):
        assert any(r["source_id"] == "m:call" and r["target_id"] == "m:fetch" for r in _calls(g))


def test_f06_java_compact_record_constructor_extracted():
    with _fixture(
        {"Receipt.java": "record Receipt(String value) { Receipt { validate(); } static void validate() {} }\n"}
    ) as (g, root, db):
        assert any(r["target_id"] == "Receipt:Receipt.validate" for r in _calls(g))


def test_f07_nested_java_class_reference_resolves():
    with _fixture(
        {"Outer.java": "class Outer { static class Inner { void run() {} } void call(Inner inner) { inner.run(); } }\n"}
    ) as (g, root, db):
        assert any(r["source_id"] == "Outer:Outer.call" and r["target_id"] == "Outer:Outer.Inner.run" for r in _calls(g))


def test_f08_unrelated_diagnostic_not_promoted_to_caller():
    with _fixture(
        {
            "m.py": (
                "import socket\n"
                "class Account:\n"
                "    def close(self): return 1\n"
                "def close_socket():\n"
                "    socket.socket().close()\n"
            )
        }
    ) as (g, root, db):
        result = slice_context(g, ["m:Account.close"])
        assert not any(item["symbol"] == "m:close_socket" for item in result["sections"]["upstream_callers"])


def test_f09_explicit_callee_body_included_for_same_class_helper():
    service = (
        "class Service:\n"
        "    def __init__(self): pass\n"
        "    def target(self): return self.helper()\n"
        "    def helper(self): return 'UNIQUE_CALLEE_IMPLEMENTATION'\n"
        "    def other(self): pass\n"
        "    def more(self): pass\n"
    )
    with _fixture({"m.py": service}) as (g, root, db):
        result = slice_context(g, ["m:Service.target"], include_callee_bodies=True)
        assert "UNIQUE_CALLEE_IMPLEMENTATION" in json.dumps(result)


def test_f10_and_f11_xml_well_formed_on_columnar_impact_and_escaped_slices():
    with _fixture(
        {"m.py": "def leaf(): pass\ndef first(): leaf()\ndef second(): leaf()\ndef third(): leaf()\n"}
    ) as (g, root, db):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert main(["impact", "m:leaf", "--db", str(db), "--format", "xml"]) == 0
        ET.fromstring(buf.getvalue())

    with _fixture({"m.py": "def compare(a, b):\n    return a < b\n"}) as (g, root, db):
        result = slice_context(g, ["m:compare"])
        ET.fromstring(_xml(result))


def test_f12_to_f15_lambda_traversal_object_callbacks_import_precedence_and_same_module_diagnostic():
    # Case 1: Java lambda traversal preserves outer -> leaf call edge and impact
    with _fixture(
        {"Main.java": "class Main { static void leaf() {} void outer() { Runnable r = () -> leaf(); r.run(); } }\n"}
    ) as (g, root, db):
        assert any(r["source_id"] == "Main:Main.outer" and r["target_id"] == "Main:Main.leaf" for r in _calls(g))
        impact = query_call_impact(g, "Main:Main.leaf")
        assert any(c["symbol"] == "Main:Main.outer" for c in impact["callers"])

    # Case 2: TypeScript object-property arrow callback preserves outer -> leaf call edge
    with _fixture(
        {"m.ts": "export function leaf() {}\nexport function outer() { const handlers = { run: () => leaf() }; handlers.run(); }\n"}
    ) as (g, root, db):
        assert any(r["source_id"] == "m:outer" and r["target_id"] == "m:leaf" for r in _calls(g))

    # Case 3: Java explicit import beats unrelated sibling nested type of the same short name
    with _fixture(
        {
            "p/Main.java": (
                "package p; import q.Inner;\n"
                "class Outer { static class Inner { void run() {} } }\n"
                "class Main { void caller(Inner x) { x.run(); } }\n"
            ),
            "q/Inner.java": "package q; public class Inner { public void run() {} }\n",
        }
    ) as (g, root, db):
        assert any(r["source_id"] == "p.Main:Main.caller" and r["target_id"] == "q.Inner:Inner.run" for r in _calls(g))
        assert not any(
            r["source_id"] == "p.Main:Main.caller" and r["target_id"] == "p.Main:Outer.Inner.run" for r in _calls(g)
        )

    # Case 4: Same-module unrelated `this.socket.close()` is not promoted to `Account.close` caller
    with _fixture(
        {"m.ts": "class Account { close() {} }\nclass Other { stop() { this.socket.close(); } }\n"}
    ) as (g, root, db):
        result = slice_context(g, ["m:Account.close"])
        assert not any(item["symbol"] == "m:Other.stop" for item in result["sections"]["upstream_callers"])

