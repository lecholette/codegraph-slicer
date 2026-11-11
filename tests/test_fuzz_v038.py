"""Property and fuzz tests for v0.3.8 extractors, slicer, router, flow, delta, and CLI."""

import random
import warnings
from pathlib import Path

from codegraph_slicer.ast_extractor import (
    CodeGraphError,
    CodeGraphParseError,
    extract_file,
)
from codegraph_slicer.cli import main as cli_main
from codegraph_slicer.delta import compute_delta, project_delta_v2, summarize_delta
from codegraph_slicer.flow import (
    FlowBudgetExceeded,
    FlowQueryError,
    UnsupportedFlowError,
)
from codegraph_slicer.flow_query import query_flow
from codegraph_slicer.pagerank import build_repo_map, build_repo_outline
from codegraph_slicer.router import route_prompt
from codegraph_slicer.slicer import (
    AnchorNotFoundError,
    IndexedSymbol,
    InvalidBudgetError,
    RequiredAnchorOverflow,
    _append_brace_class_or_method_doc_preamble,
    _format_condensed_method_body,
    _preceding_doc_comment_start,
    build_slice_outline,
    query_call_impact,
    slice_context,
)
from codegraph_slicer.store import GraphStore


def test_fuzz_preamble_and_method_condensation(tmp_path: Path):
    rng = random.Random(20260308)
    tokens = [
        "/**",
        " * @param {string} x",
        " * @returns {boolean}",
        " */",
        "/* block comment */",
        "// single line comment",
        "@Override",
        '@Deprecated(since = "1.0")',
        '@SuppressWarnings({"unchecked", "rawtypes"})',
        "package com.acme;",
        "import java.util.List;",
        "public class Sample {",
        "  private final int count = 1;",
        "  public void run(int x) {",
        "    if (x > 0) {",
        "      return;",
        "    }",
        "  }",
        "}",
        "",
        "   ",
        "*/",
        "/** inline javadoc */",
        "@Unclosed(",
    ]
    exts = [".java", ".ts", ".tsx", ".js", ".jsx", ".py"]
    kinds = ["class", "method", "function", "interface", "enum", "record", "module"]

    db_path = tmp_path / "empty.sqlite"
    with GraphStore(db_path) as store:
        for _ in range(1500):
            n_lines = rng.randint(0, 60)
            lines = [rng.choice(tokens) for _ in range(n_lines)]
            full_src = "\n".join(lines)
            start_line = rng.randint(1, max(1, n_lines + 5))
            end_line = start_line + rng.randint(0, 15)
            header_end_line = rng.randint(0, start_line)
            emitted_headers = {"pkg/Sample.java"} if rng.random() < 0.3 else set()
            body_slice = lines[max(0, start_line - 1) : end_line]
            body = "\n".join(body_slice)
            ext = rng.choice(exts)
            kind = rng.choice(kinds)
            doc_start = _preceding_doc_comment_start(lines, start_line)
            assert 1 <= doc_start <= start_line

            sym = IndexedSymbol(
                symbol_id="pkg.Sample:Sample.run" if kind == "method" else "pkg.Sample:Sample",
                file_path=f"pkg/Sample{ext}",
                module="pkg.Sample",
                qualname="Sample.run" if kind == "method" else "Sample",
                kind=kind,
                start_line=start_line,
                end_line=end_line,
                signature="void run(int x)",
                body=body,
            )
            header_lines: list[str] = []
            new_end = _append_brace_class_or_method_doc_preamble(
                store,
                sym,
                {},
                lines,
                header_lines,
                header_end_line,
                emitted_headers,
            )
            assert isinstance(new_end, int)
            assert new_end >= header_end_line

            condensed = _format_condensed_method_body(
                full_src,
                rng.choice(["#", "//"]),
                max_method_lines=rng.choice([0, 1, 5, 18, 22, 26, 40, 100]),
                signature=rng.choice(["", "def f():", "public void run(int x) {"]),
            )
            assert isinstance(condensed, str)


def test_fuzz_extract_file_all_languages():
    rng = random.Random(20260309)
    exts = [".py", ".java", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"]
    vocab = [
        "class ",
        "interface ",
        "enum ",
        "record ",
        "function ",
        "def ",
        "async ",
        "export ",
        "default ",
        "import ",
        "from ",
        "require(",
        "module.exports = ",
        "exports.",
        "prototype.",
        "this.",
        "super(",
        "new ",
        "return ",
        "if ",
        "else ",
        "for ",
        "while ",
        "try ",
        "catch ",
        "finally ",
        "switch ",
        "case ",
        "/** @param {Foo} x @returns {Bar} */",
        "/* ",
        "*/",
        "//",
        "#",
        "@Override\n",
        "{",
        "}",
        "(",
        ")",
        "[",
        "]",
        "<",
        ">",
        ";",
        ":",
        ",",
        ".",
        "=",
        "=>",
        '"str"',
        "'chr'",
        "`tpl ${x}`",
        "\0",
        "\r\n",
        "\n",
        "\t",
        "λ",
    ]
    separators = ["", " ", "\n", "a", "Foo", "bar", "0", "_"]

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        for i in range(1500):
            ext = rng.choice(exts)
            if i % 6 == 0:
                raw = bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 256)))
            else:
                pieces = [
                    rng.choice(vocab) + rng.choice(separators)
                    for _ in range(rng.randint(0, 45))
                ]
                raw = "".join(pieces).encode("utf-8", errors="surrogatepass")
            try:
                analysis = extract_file(f"fuzz/mod_{i}{ext}", raw)
                assert analysis.path == f"fuzz/mod_{i}{ext}"
                assert len(analysis.symbols) >= 1
            except (CodeGraphParseError, CodeGraphError):
                pass


def test_fuzz_valid_grammar_mutations_and_multi_anchor_slicing(tmp_path: Path):
    rng = random.Random(20260310)
    repo = tmp_path / "repo"
    (repo / "src/com/acme").mkdir(parents=True)
    (repo / "web").mkdir(parents=True)
    (repo / "pypkg").mkdir(parents=True)

    for idx in range(4):
        methods_java = []
        for m in range(rng.randint(2, 10)):
            lines = [f"        int v{k} = {k};" for k in range(rng.randint(1, 35))]
            lines.append("        if (v0 >= 0) { return v0; }")
            lines.append("        return -1;")
            methods_java.append(
                f"    /** Javadoc for step{m} on Service{idx}. */\n"
                f"    public int step{m}(int arg) {{\n"
                + "\n".join(lines)
                + "\n    }"
            )
        java_code = (
            "package com.acme;\n\n"
            f"/** Class Javadoc for Service{idx}. */\n"
            "@Deprecated\n"
            f"public class Service{idx} {{\n"
            f"    private final int seed = {idx};\n"
            f"    public Service{idx}() {{}}\n\n"
            + "\n\n".join(methods_java)
            + "\n}\n"
        )
        (repo / f"src/com/acme/Service{idx}.java").write_text(java_code, encoding="utf-8")

        methods_js = []
        for m in range(rng.randint(2, 8)):
            lines = [f"    const x{k} = {k};" for k in range(rng.randint(1, 30))]
            lines.append("    if (x0 >= 0) { return x0; }")
            lines.append("    return 0;")
            methods_js.append(
                f"  /** JSDoc for handle{m} on Worker{idx} (@param {{number}} v). */\n"
                f"  handle{m}(v) {{\n"
                + "\n".join(lines)
                + "\n  }"
            )
        js_code = (
            f"/** JSDoc for Worker{idx}. */\n"
            f"class Worker{idx} {{\n"
            f"  constructor() {{ this.id = {idx}; }}\n\n"
            + "\n\n".join(methods_js)
            + f"\n}}\nmodule.exports = {{ Worker{idx} }};\n"
        )
        (repo / f"web/worker{idx}.js").write_text(js_code, encoding="utf-8")

        ts_code = (
            f"export interface Config{idx} {{ enabled: boolean; }}\n"
            f"/** TSDoc for Controller{idx}. */\n"
            f"export class Controller{idx} {{\n"
            f"  /** Executes controller {idx}. */\n"
            f"  run(cfg: Config{idx}): boolean {{ return cfg.enabled; }}\n"
            f"}}\n"
        )
        (repo / f"web/controller{idx}.ts").write_text(ts_code, encoding="utf-8")

        py_code = (
            f"class Processor{idx}:\n"
            f'    """Docstring for Processor{idx}."""\n'
            f"    def __init__(self) -> None:\n"
            f"        self.value = {idx}\n\n"
            f"    def compute(self, x: int) -> int:\n"
            f"        total = x + self.value\n"
            f"        if total > 10:\n"
            f"            total += 1\n"
            f"        return total\n"
        )
        (repo / f"pypkg/proc{idx}.py").write_text(py_code, encoding="utf-8")

    (repo / "web/broken.js").write_text("class Broken { function (((", encoding="utf-8")

    db_path = tmp_path / "graph.sqlite"
    with GraphStore(db_path) as store:
        res = store.sync_workspace(repo, skip_unparseable=True)
        assert res["parsed_files"] == 16
        assert res["skipped_unparseable"] == ["web/broken.js"]

    with GraphStore(db_path, readonly=True) as store:
        candidate_anchors = [
            f"src/com/acme/Service{i}.java:Service{i}" for i in range(4)
        ] + [
            f"src/com/acme/Service{i}.java:Service{i}.step0" for i in range(4)
        ] + [
            f"web/worker{i}.js:Worker{i}" for i in range(4)
        ] + [
            f"web/worker{i}.js:Worker{i}.handle0" for i in range(4)
        ] + [
            f"web/controller{i}.ts:Controller{i}.run" for i in range(4)
        ] + [
            f"pypkg/proc{i}.py:Processor{i}.compute" for i in range(4)
        ]

        for _ in range(120):
            k = rng.randint(1, 10)
            chosen = rng.sample(candidate_anchors, k=min(k, len(candidate_anchors)))
            budget = rng.choice([1, 50, 200, 600, 1500, 4000, 8000])
            t_mode = rng.choice(["full", "skeleton"])
            try:
                sliced = slice_context(
                    store,
                    chosen,
                    max_tokens=budget,
                    include_callee_bodies=rng.choice([False, True]),
                    target_mode=t_mode,
                )
                assert "target_implementations" in sliced.get("sections", sliced)
            except (InvalidBudgetError, RequiredAnchorOverflow):
                assert budget <= 4000

        for _ in range(40):
            anchor = rng.choice(candidate_anchors)
            budget = rng.choice([200, 1000, 4000])
            try:
                outline = build_slice_outline(
                    store, [anchor], max_tokens=budget, include_imports=rng.choice([False, True])
                )
                assert "entities" in outline
            except (InvalidBudgetError, RequiredAnchorOverflow):
                pass

        for _ in range(40):
            budget = rng.choice([200, 1000, 4000])
            seed = rng.choice([(), ["com.acme"], ["web"], ["pypkg"]])
            try:
                rmap = build_repo_map(store, seed, max_tokens=budget)
                assert "items" in rmap
                rout = build_repo_outline(store, seed, max_tokens=budget)
                assert "files" in rout
            except (InvalidBudgetError, CodeGraphError):
                pass

        for i in range(4):
            try:
                flow_res = query_flow(
                    store,
                    f"pypkg/proc{i}.py:Processor{i}.compute",
                    variable=rng.choice([None, "total", "x", "missing"]),
                    line=rng.choice([None, 7, 9, 99]),
                    direction=rng.choice(["backward", "forward"]),
                    include_control=rng.choice([True, False]),
                    max_tokens=4000,
                )
                assert "flow" in flow_res
            except (FlowQueryError, FlowBudgetExceeded, UnsupportedFlowError):
                pass

        adversarial_prompts = [
            "",
            "   \n\t  ",
            "?" * 5000,
            "SELECT * FROM symbols; DROP TABLE files;--",
            "../../../etc/passwd",
            "Service0.step0 Worker1.handle0 Controller2.run Processor3.compute",
            "([{\\^$|?*+.",
            "Fix bug in `src/com/acme/Service0.java:Service0.step0` and `web/worker2.js`",
            "🚀✨🔥" * 200,
        ]
        for prompt in adversarial_prompts:
            routed = route_prompt(store, prompt)
            assert routed["schema"] == "codegraph-route-v1"
            assert "workflow_mode" in routed

        bad_anchors = [
            ":",
            ":::",
            "nonexistent:Foo",
            "src/com/acme/Service0.java:NonExistentMethod",
            "web/worker0.js:Worker0.missingMethod",
            "a" * 1000,
            "\n\t",
        ]
        for bad in bad_anchors:
            try:
                slice_context(store, [bad], max_tokens=2000)
            except (AnchorNotFoundError, CodeGraphError):
                pass

    # Mutate files and fuzz delta
    head_db = tmp_path / "head.sqlite"
    head_db.write_bytes(db_path.read_bytes())
    (repo / "pypkg/proc0.py").write_text(
        "class Processor0:\n    def compute(self, x: int) -> int:\n        return x * 2\n",
        encoding="utf-8",
    )
    with GraphStore(head_db) as hstore:
        hstore.sync_workspace(repo, skip_unparseable=True)
    with GraphStore(db_path, readonly=True) as bstore, GraphStore(head_db, readonly=True) as hstore:
        raw_delta = compute_delta(bstore, hstore, [["com.acme", "web"]], selected_tests=[])
        s1 = summarize_delta(raw_delta, 4000, full=False, selected_tests=False)
        assert "summary" in s1
        s2 = project_delta_v2(
            raw_delta,
            4000,
            anchors=["pypkg.proc0:Processor0"],
            kinds=["modified_symbols"],
            edge_sample_limit=5,
            selected_tests=False,
        )
        assert "samples" in s2


def test_fuzz_cli_entrypoints_never_uncaught_traceback(tmp_path: Path, capsys):
    repo = tmp_path / "cli_repo"
    repo.mkdir()
    (repo / "app.py").write_text(
        "def helper(v: int) -> int:\n    res = v + 1\n    return res\n\n"
        "def main_fn(x: int) -> int:\n    return helper(x)\n",
        encoding="utf-8",
    )
    db = tmp_path / "cli.sqlite"
    assert cli_main(["sync", str(repo), "--db", str(db)]) == 0
    capsys.readouterr()

    rng = random.Random(20260311)
    formats = ["json", "markdown", "xml"]
    for _ in range(50):
        fmt = rng.choice(formats)
        cmd = rng.choice(
            [
                ["map", "--db", str(db), "--recommend-workflow", "--format", fmt],
                ["route", "--db", str(db), "--prompt", "update main_fn and helper", "--format", fmt],
                ["slice", "--db", str(db), "app.py:main_fn", "--format", fmt],
                ["impact", "--db", str(db), "app.py:helper", "--format", fmt],
                ["flow", "--db", str(db), "app.py:helper", "--variable", "res", "--line", "2", "--format", fmt],
            ]
        )
        rc = cli_main(cmd)
        assert rc == 0
        capsys.readouterr()
