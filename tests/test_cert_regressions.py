"""Regression tests for static analysis certification findings (F01-F11)."""
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


def test_v033_static_audit_regressions():
    # F1: Python default fallback regression (import alias `as default` or `default = C` must not resolve missing symbol)
    for a_src in ("from other import C as default\n", "class C:\n    def work(self): pass\ndefault = C\n"):
        files = {"a.py": a_src, "b.py": "from a import work\ndef run():\n    work()\n"}
        if "other" in a_src:
            files["other.py"] = "class C:\n    def work(self): pass\n"
        with _fixture(files) as (g, root, db):
            assert not any(r["source_id"] == "b:run" for r in _calls(g))
            assert any(
                r["source_id"] == "b:run" and r["target"] == "a.work"
                for r in g.rows("SELECT source_id,target FROM diagnostics WHERE kind='CALLS'")
            )

    # F2: Python parameter shadowing builtin `open` must emit unresolved `?open` diagnostic
    with _fixture({"a.py": "def run(open):\n    return open()\n"}) as (g, root, db):
        assert any(
            r["source_id"] == "a:run" and r["target"] == "?open"
            for r in g.rows("SELECT source_id,target FROM diagnostics WHERE kind='CALLS'")
        )

    # F3: JSDoc import("./a").Client and import("./b").Client must not collide or shadow module import
    with _fixture(
        {
            "a.js": "export class Client { work() {} }\n",
            "b.js": "export class Client { work() {} }\n",
            "c.js": (
                'import {Client} from "./a";\n'
                '/** @param {import("./a").Client} x */\n'
                "export function fa(x) { x.work(); }\n"
                '/** @param {import("./b").Client} x */\n'
                "export function fb(x) { x.work(); }\n"
            ),
        }
    ) as (g, root, db):
        assert any(r["source_id"] == "c:fa" and r["target_id"] == "a:Client.work" for r in _calls(g))
        assert any(r["source_id"] == "c:fb" and r["target_id"] == "b:Client.work" for r in _calls(g))
        assert not any(r["source_id"] == "c:fb" and r["target_id"] == "a:Client.work" for r in _calls(g))

    # F4: Shadowed `require` parameter must not be treated as CommonJS module loader
    with _fixture(
        {
            "a.js": "exports.work = function() {};\n",
            "b.js": 'function run(require) { const x = require("./a"); x.work(); }\n',
        }
    ) as (g, root, db):
        assert not any(r["source_id"] == "b:run" and r["target_id"] == "a:work" for r in _calls(g))
        assert not g.rows("SELECT * FROM edges WHERE source_id='b:run' AND kind='IMPORTS'")

    # F5: Constructor parameter `A` shadowing class `A` must not bind `this.x` to module-level `A`
    with _fixture(
        {
            "a.js": "class A { work() {} }\nclass C { constructor(A) { this.x = new A(); }\nrun() { this.x.work(); } }\n"
        }
    ) as (g, root, db):
        assert not any(r["source_id"] == "a:C.run" and r["target_id"] == "a:A.work" for r in _calls(g))

    # F6: Prototype method assignment `C.prototype.work = function() { helper(); }`
    with _fixture(
        {
            "a.js": (
                "function C() {}\n"
                "function helper() {}\n"
                "C.prototype.work = function() { helper(); };\n"
                "function run() { const c = new C(); c.work(); }\n"
            )
        }
    ) as (g, root, db):
        assert g.rows("SELECT symbol_id FROM symbols WHERE symbol_id='a:C.work'")
        assert any(r["source_id"] == "a:C.work" and r["target_id"] == "a:helper" for r in _calls(g))
        assert any(r["source_id"] == "a:run" and r["target_id"] == "a:C.work" for r in _calls(g))

    # F7: CommonJS namespace re-export `exports.lib = require("./a")` resolves `lib.work()`
    with _fixture(
        {
            "a.js": "exports.work = function() {};\n",
            "b.js": 'exports.lib = require("./a");\n',
            "c.js": 'const {lib} = require("./b");\nfunction run() { lib.work(); }\n',
        }
    ) as (g, root, db):
        assert any(r["source_id"] == "c:run" and r["target_id"] == "a:work" for r in _calls(g))

    # F8: CommonJS private symbol leak (`hidden` not exported from `a.js` when `exports.public` is present)
    with _fixture(
        {
            "a.js": "function hidden() {}\nexports.public = function() {};\n",
            "b.js": 'const {hidden} = require("./a");\nfunction run() { hidden(); }\n',
        }
    ) as (g, root, db):
        assert not any(r["source_id"] == "b:run" and r["target_id"] == "a:hidden" for r in _calls(g))

    # F9: `_condense_class_public_methods` must not silently drop public methods beyond 8
    private_body = "  _helper() {\n" + "    void 0;\n" * 900 + "  }\n"
    long_pub = "  longPub() {\n" + "\n".join(f"    const line{i} = {i};" for i in range(1, 50)) + "\n    return line49;\n  }\n"
    public_body = "".join(f"  method{i}() {{ return {i}; }}\n" for i in range(1, 13)) + long_pub
    with _fixture(
        {
            "a.js": f"export class A {{\n  field = 1;\n{private_body}{public_body}}}\nexport function other() {{\n"
            + "  const z = 1;\n" * 40
            + "}\n"
        }
    ) as (
        g,
        root,
        db,
    ):
        res = slice_context(g, ["a.js:A", "a.js:other"], max_tokens=2000)
        target_map = {t["symbol"]: t["text"] for t in res["sections"]["target_implementations"]}
        target_txt = target_map["a:A"]
        row_a = g.rows("SELECT * FROM symbols WHERE symbol_id='a:A'")[0]
        from codegraph_slicer.slicer import (
            _condense_class_public_methods,
            _format_condensed_method_body,
            tokens_for_length,
        )

        assert "field = 1;" in target_txt
        assert f"--max-tokens {tokens_for_length(len(row_a['body'])) * 2}" in target_txt
        assert "const line19 = 19;" in target_txt
        assert "const line20 = 20;" not in target_txt
        assert "... (24 lines condensed)" in target_txt
        assert "const line44 = 44;" in target_txt
        assert "return line49;" in target_txt
        for i in range(1, 13):
            assert f"method{i}()" in target_txt
        assert "export function other()" in target_map["a:other"]
        # Ensure non-class target or class without both public & internal methods returns None
        row_other = g.rows("SELECT * FROM symbols WHERE symbol_id='a:other'")[0]
        assert _condense_class_public_methods(g, "a:other", row_other, "GRAPH", 2000) is None
        with _fixture({"pub_only.js": "export class PubOnly { m1() {} m2() {} }\n"}) as (g2, _, _):
            row_po = g2.rows("SELECT * FROM symbols WHERE symbol_id='pub_only:PubOnly'")[0]
            assert _condense_class_public_methods(g2, "pub_only:PubOnly", row_po, "GRAPH", 2000) is None
        with _fixture({"int_only.js": "export class IntOnly { _h1() {} _h2() {} }\n"}) as (g3, _, _):
            row_io = g3.rows("SELECT * FROM symbols WHERE symbol_id='int_only:IntOnly'")[0]
            assert _condense_class_public_methods(g3, "int_only:IntOnly", row_io, "GRAPH", 2000) is None
        with _fixture({"compact_hdr.js": "export class Compact { _h() {\n" + "  void 0;\n" * 40 + "} m() { return 1; } }\n"}) as (g4, _, _):
            row_ch = g4.rows("SELECT * FROM symbols WHERE symbol_id='compact_hdr:Compact'")[0]
            cond_ch = _condense_class_public_methods(g4, "compact_hdr:Compact", row_ch, "GRAPH", 2000)
            assert cond_ch is not None and cond_ch.startswith("export class Compact { _h() {\n    // ...")

    # F10: `_callee_item` return hint must not extract return from nested closure
    with _fixture(
        {
            "factory.js": (
                "export function factory() {\n"
                + "  // filler\n" * 22
                + "  return () => {\n    return 42;\n  };\n"
                + "}\n"
                + "export function multiReturn(flag) {\n"
                + "  // filler\n" * 22
                + "  if (flag) {\n    return 1;\n  }\n"
                + "  return 99;\n"
                + "}\n"
            ),
            "use.js": 'import {factory, multiReturn} from "./factory";\nexport function run() { return factory() + multiReturn(true); }\n',
        }
    ) as (g, root, db):
        res = slice_context(g, ["use.js:run"])
        dep_txt = "\n".join(d["text"] for d in res["sections"]["downstream_dependencies"])
        assert "returns: 42;" not in dep_txt
        assert "returns: 99;" in dep_txt

    # F14: Unit assertions for `_extract_require_spec` and `_unwrap_cjs_assignment` helpers
    from codegraph_slicer.ts_extractor import (
        _collect_pattern_identifiers,
        _extract_object_pattern_pairs,
        _extract_require_call_spec,
        _extract_require_spec,
        _preceding_jsdoc,
        _unwrap_cjs_assignment,
        parse_tree,
    )

    assert _collect_pattern_identifiers(b"", None) == []
    assert _extract_require_call_spec(b"", None) is None
    assert _extract_require_spec(b"", None) is None
    assert _format_condensed_method_body("\n".join(f"line{i}" for i in range(45)), "//") == "\n".join(
        f"line{i}" for i in range(45)
    )
    _, d1, r1 = parse_tree(
        b'const a = require("./a").sub;\n'
        b'Object.assign(exports, require("./b"));\n'
        b"exports = module.exports = class X {};\n"
        b"C.prototype.m = function() {};\n"
        b"other.prop = 1;\n"
        b'require("./c")[0];\n'
        b"Object.assign(exports);\n"
        b"a = b = 1;\n"
        b"module.other = 1;\n"
        b"other = module.exports = class Y {};\n"
        b'const { x, y = 1, k: v, m: n = 2, p: { q } } = require("./d");\n'
        b'exports["dyn"] = 1;\n'
        b"/** @type {number} */\n\nconst j1 = 1;\n"
        b"/** @type {number} */\n\n\nconst j2 = 2;\n"
        b"// line comment\nconst j3 = 3;\n",
        "t.js",
        "tsx",
    )
    stmts = r1.children
    decl = stmts[0].children[1]
    assert _extract_require_spec(d1, decl.children[-1]) == ("./a", "sub")
    assert _unwrap_cjs_assignment(d1, stmts[0]) is None
    c1 = _unwrap_cjs_assignment(d1, stmts[1])
    assert c1 is not None and c1[0] == "assign" and len(c1[2]) == 1
    c2 = _unwrap_cjs_assignment(d1, stmts[2])
    assert c2 is not None and c2[0] == "default" and c2[1] is None
    c3 = _unwrap_cjs_assignment(d1, stmts[3])
    assert c3 is not None and c3[0] == "proto" and c3[1] == "C.m"
    assert _unwrap_cjs_assignment(d1, stmts[4]) is None
    assert _extract_require_spec(d1, stmts[5].children[0]) is None
    assert _unwrap_cjs_assignment(d1, stmts[6]) is None
    assert _unwrap_cjs_assignment(d1, stmts[7]) is None
    assert _unwrap_cjs_assignment(d1, stmts[8]) is None
    assert _unwrap_cjs_assignment(d1, stmts[9]) is None
    pat_node = stmts[10].children[1].children[0]
    assert _extract_object_pattern_pairs(d1, pat_node) == [("x", "x"), ("y", "y"), ("k", "v"), ("m", "n")]
    assert _collect_pattern_identifiers(d1, pat_node) == ["x", "y", "v", "n", "q"]
    assert _extract_object_pattern_pairs(d1, None) == []
    assert _unwrap_cjs_assignment(d1, stmts[11]) is None
    assert _preceding_jsdoc(d1, stmts[13]) == ("/** @type {number} */", 13)
    assert _preceding_jsdoc(d1, stmts[15]) is None
    assert _preceding_jsdoc(d1, stmts[17]) is None

    # F3b: JSDoc import('./a/b').Client and import('./a_b').Client must not collide
    with _fixture(
        {
            "a/b.js": "export class Client { work() {} }\n",
            "a_b.js": "export class Client { work() {} }\n",
            "c.js": (
                "export class C {\n"
                '  /** @type {import("./a/b").Client} */\n'
                "  x;\n"
                '  /** @type {import("./a_b").Client} */\n'
                "  y;\n"
                "  run() {\n"
                "    this.x.work();\n"
                "    this.y.work();\n"
                "  }\n"
                "}\n"
            ),
        }
    ) as (g, root, db):
        assert any(r["source_id"] == "c:C.run" and r["target_id"] == "a.b:Client.work" for r in _calls(g))
        assert any(r["source_id"] == "c:C.run" and r["target_id"] == "a_b:Client.work" for r in _calls(g))

    # F4b: Module-level shadowed `require` (including hoisted and re-exports) and local `const require = loader`
    for b_src in (
        'function require(name) { return {}; }\nconst x = require("./a");\nfunction run() { x.work(); }\n',
        'const x = require("./a");\nfunction require(name) { return {}; }\nfunction run() { x.work(); }\n',
        'function run(loader) { const require = loader; const x = require("./a"); x.work(); }\n',
    ):
        with _fixture({"a.js": "exports.work = function() {};\n", "b.js": b_src}) as (g, root, db):
            assert not any(r["source_id"] == "b:run" and r["target_id"] == "a:work" for r in _calls(g))
    for b_re in (
        'function require(name) { return {}; }\nexports.lib = require("./a");\n',
        'function require(name) { return {}; }\nmodule.exports = require("./a");\n',
        'var {require} = loader;\nexports.lib = require("./a");\n',
    ):
        c_re = (
            'const {lib} = require("./b");\nfunction run() { lib.work(); }\n'
            if "exports.lib" in b_re
            else 'const x = require("./b");\nfunction run() { x.work(); }\n'
        )
        with _fixture({"a.js": "exports.work = function() {};\n", "b.js": b_re, "c.js": c_re}) as (g, root, db):
            assert not any(r["source_id"] == "c:run" and r["target_id"] == "a:work" for r in _calls(g))

    # F5b: Constructor local/destructured/hoisted/nested-fn `A` shadowing module-level `class A`
    for ctor_src in (
        "constructor(make) { const A = make; this.x = new A(); }",
        "constructor(make) { const {A} = make; this.x = new A(); }",
        "constructor(make) { const [A] = make; this.x = new A(); }",
        "constructor({A = 1}, [B] = []) { this.x = new A(); this.y = new B(); }",
        "constructor(make) { this.x = new A(); var A = make; }",
        "constructor() { function A() {} this.x = new A(); }",
    ):
        with _fixture(
            {"a.js": f"class A {{ work() {{}} }}\nclass C {{\n  {ctor_src}\n  run() {{ this.x.work(); }}\n}}\n"}
        ) as (g, root, db):
            assert not any(r["source_id"] == "a:C.run" and r["target_id"] == "a:A.work" for r in _calls(g))
            assert not any(r["source_id"] == "a:C.constructor" and r["target_id"] == "a:A" for r in _calls(g))

    # F8b: JS file with no exports, named default export, default class instance method, or ESM default class name
    for a_src, b_src in (
        ("function hidden() {}\n", 'const {hidden} = require("./a");\nfunction run() { hidden(); }\n'),
        ("module.exports = function hidden() {};\n", 'const {hidden} = require("./a");\nfunction run() { hidden(); }\n'),
        ("module.exports = class Hidden { work() {} };\n", 'const {Hidden} = require("./a");\nfunction run() { const h = new Hidden(); h.work(); }\n'),
        ("module.exports = class Hidden { work() {} };\n", 'const {work} = require("./a");\nfunction run() { work(); }\n'),
        ("module.exports = class Hidden { work() {} };\n", 'const a = require("./a");\nfunction run() { a.work(); }\n'),
        ("export default class Hidden { work() {} }\n", 'import {Hidden} from "./a";\nfunction run() { const h = new Hidden(); h.work(); }\n'),
    ):
        with _fixture({"a.js": a_src, "b.js": b_src}) as (g, root, db):
            assert not any(r["source_id"] == "b:run" and r["target_id"].startswith("a:") for r in _calls(g))

    # F10b: Return hints ignore braces inside single/multiline comments, strings, templates, and regexes
    for filler_extra in (
        "  // }\n",
        '  const s = "}";\n',
        "  /*\n  }\n  */\n",
        "  const s = `\n  }\n  `;\n",
        "  const s = /}/;\n",
        "  const s = /[/}]/;\n",
    ):
        with _fixture(
            {
                "factory.js": (
                    "export function factory() {\n"
                    + "  // filler\n" * 22
                    + filler_extra
                    + "  return () => {\n    return 42;\n  };\n}\n"
                    + "export class Helper {\n  make() {\n"
                    + "    // filler\n" * 22
                    + "    return 77;\n  }\n}\n"
                ),
                "use.js": 'import {factory, Helper} from "./factory";\nexport function run() { const h = new Helper(); return factory() + h.make(); }\n',
            }
        ) as (g, root, db):
            res = slice_context(g, ["use.js:run"])
            dep_txt = "\n".join(d["text"] for d in res["sections"]["downstream_dependencies"])
            assert "returns: 42;" not in dep_txt
            assert "returns: 77;" in dep_txt

    # F11b: Single-quoted module docstring and parenthesized import with `# )` comment
    for header in (
        '"module docs"\nimport os\n',
        "from os import (\n    path, # )\n    sep,\n)\n",
    ):
        with _fixture({"a.py": header + "\ndef f():\n    return 1\n"}) as (g, root, db):
            res = slice_context(g, ["a.py:f"])
            target_txt = res["sections"]["target_implementations"][0]["text"]
            assert "import " in target_txt
            if "sep" in header:
                assert "sep," in target_txt


    # F11: Python header preamble supports raw docstring (`r"""..."""`) and backslash-continued import
    for header in ('r"""module docs\nmore docs\n"""\nimport os\n', "from os import \\\n    path\n"):
        with _fixture({"a.py": header + "\ndef f():\n    return path\n"}) as (g, root, db):
            res = slice_context(g, ["a.py:f"])
            target_txt = res["sections"]["target_implementations"][0]["text"]
            assert "import " in target_txt

    # F12: `map --snapshot INVALID_REV` must fail with exit 2
    with _fixture({"a.py": "def f(): pass\n"}) as (g, root, db):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            try:
                rc = main(["map", "--db", str(db), "--snapshot", "THIS_REVISION_DOES_NOT_EXIST"])
            except SystemExit as exc:
                rc = exc.code
        assert rc == 2

    # F14b: Additional coverage for JSDoc local types, constructor factory assignments, prototype `this`, and CJS object pair methods
    with _fixture(
        {
            "ext.js": "export class B { work() {} }\nexport function makeB() { return new B(); }\n",
            "a.js": (
                'import {makeB} from "./ext";\n'
                "class A { work() {} }\n"
                "function makeA() { return new A(); }\n"
                "function unknownFn() {}\n"
                "/**\n * @param {A<T>} p1\n * @param {{x: number}} p2\n */\n"
                "function jsdocCheck(p1, p2) { p1.work(); }\n"
                "class C {\n"
                "  constructor() {\n"
                "    let tmp = 0;\n"
                "    tmp = 1;\n"
                "    this.a = makeA();\n"
                "    this.b = makeB();\n"
                "    this.u = unknownFn();\n"
                "  }\n"
                "  run() { this.a.work(); this.b.work(); this.u.work(); }\n"
                "}\n"
                "function Proto() {}\n"
                "Proto.prototype.other = function() {};\n"
                "Proto.prototype.main = function() { this.other(); };\n"
                "function helper() {}\n"
                "module.exports = { work: function() { helper(); } };\n"
            ),
        }
    ) as (g, root, db):
        calls = _calls(g)
        assert any(r["source_id"] == "a:jsdocCheck" and r["target_id"] == "a:A.work" for r in calls)
        assert any(r["source_id"] == "a:C.run" and r["target_id"] == "a:A.work" for r in calls)
        assert any(r["source_id"] == "a:C.run" and r["target_id"] == "ext:B.work" for r in calls)
        assert any(r["source_id"] == "a:Proto.main" and r["target_id"] == "a:Proto.other" for r in calls)
        assert any(r["source_id"] == "a:work" and r["target_id"] == "a:helper" for r in calls)

