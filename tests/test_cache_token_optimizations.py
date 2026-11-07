import json
import pytest

from codegraph_slicer import GraphStore, compute_delta, extract_file, slice_context
from codegraph_slicer.ast_extractor import _is_py_private_method
from codegraph_slicer.cli import main
from codegraph_slicer.slicer import (
    _apply_session_dedup,
    _build_target_entry,
    _compact_cached_entry,
    _extract_brace_class_fields,
    _resolve_anchor_batch,
    _resolve_suffix_path_anchor,
    _resolve_unique_qualname_anchor,
    _target_skeleton_item,
)
from codegraph_slicer.treesitter_support import (
    _fold_brace_private_methods,
    _is_private_skeleton_line,
    build_brace_skeleton,
)


def _write_files(root, files: dict[str, str]):
    root.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


def test_forgiving_anchor_resolves_bare_path_trailing_colon_suffix_and_unique_qualname(tmp_path):
    root = tmp_path / "repo"
    _write_files(
        root,
        {
            "src/pkg/__init__.py": "",
            "src/pkg/service.py": (
                "class UniqueService:\n"
                "    def execute_job(self) -> int:\n"
                "        return 42\n"
            ),
        },
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(root)
        # 1. Trailing colon on file path
        s1 = slice_context(store, ["src/pkg/service.py:"])
        assert s1["sections"]["target_implementations"][0]["symbol"] == "pkg.service:"
        # 2. Bare file path (exact and suffix without src/)
        s2 = slice_context(store, ["pkg/service.py"])
        assert s2["sections"]["target_implementations"][0]["symbol"] == "pkg.service:"
        # 3. Suffix file path with qualname
        s3 = slice_context(store, ["service.py:UniqueService.execute_job"])
        assert s3["sections"]["target_implementations"][0]["symbol"] == "pkg.service:UniqueService.execute_job"
        # 4. Unique bare qualname without module prefix
        s4 = slice_context(store, ["UniqueService"])
        assert s4["sections"]["target_implementations"][0]["symbol"] == "pkg.service:UniqueService"
        s5 = slice_context(store, ["UniqueService.execute_job"])
        assert s5["sections"]["target_implementations"][0]["symbol"] == "pkg.service:UniqueService.execute_job"


def test_proactive_target_condensation_and_line_range_hints_prevent_double_context(tmp_path):
    root = tmp_path / "repo"
    helpers = "\n".join(
        f"    def _helper_{i}(self, x: int) -> int:\n"
        + "".join(f"        v_{i}_{j} = x + {j}\n" for j in range(18))
        + f"        return v_{i}_17\n"
        for i in range(6)
    )
    _write_files(
        root,
        {
            "pkg/engine.py": (
                "import os\n"
                "class BigEngine:\n"
                "    def __init__(self, name: str):\n"
                "        self.name = name\n"
                f"{helpers}\n"
                "    def execute(self, val: int) -> int:\n"
                "        return self._helper_0(val) + 1\n"
            ),
            "pkg/worker.py": (
                "from pkg.engine import BigEngine\n"
                "def run_worker(e: BigEngine) -> int:\n"
                + "".join(f"    step_w_{k} = {k} * 100 + {k}\n" for k in range(85))
                + "    return e.execute(step_w_0)\n"
                "def run_second(e: BigEngine) -> int:\n"
                + "".join(f"    step_s_{k} = {k} * 200 + {k}\n" for k in range(85))
                + "    return e.execute(step_s_0)\n"
            ),
        },
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(root)
        # Proactive condensation on single large class/module target when max_tokens >= 2000
        res = slice_context(store, ["pkg.engine:BigEngine"], max_tokens=2500)
        target_text = res["sections"]["target_implementations"][0]["text"]
        assert "def execute(self, val: int) -> int:" in target_text
        assert "internal helper methods condensed" in target_text
        assert "v_4_10" not in target_text
        assert "Read" in target_text

        # Multi-anchor condensation uses line-range Read instructions rather than recursive slice
        res_multi = slice_context(
            store, ["pkg.worker:run_worker", "pkg.worker:run_second"], max_tokens=1000
        )
        combined_targets = "\n".join(
            t["text"] for t in res_multi["sections"]["target_implementations"]
        )
        assert "lines condensed;" in combined_targets
        assert "Read offset=" in combined_targets
        assert "codegraph-slicer slice" not in combined_targets


def test_python_class_anchor_seeds_method_uses_type_and_refills_callees_first(tmp_path):
    root = tmp_path / "repo"
    _write_files(
        root,
        {
            "pkg/models.py": (
                "class ConfigSchema:\n"
                "    key: str\n"
                "    timeout: int\n"
            ),
            "pkg/validator.py": (
                "from pkg.models import ConfigSchema\n"
                "def validate_schema(cfg: ConfigSchema) -> bool:\n"
                "    return bool(cfg.key)\n"
            ),
            "pkg/service.py": (
                "from pkg.models import ConfigSchema\n"
                "from pkg.validator import validate_schema\n"
                "class ConfigHandler:\n"
                "    def apply(self, cfg: ConfigSchema) -> bool:\n"
                "        return validate_schema(cfg)\n"
                "def other_target() -> int:\n"
                "    return 1\n"
            ),
            "pkg/callers.py": "\n".join(
                f"from pkg.service import ConfigHandler\n"
                f"def caller_{i}(h: ConfigHandler, c):\n"
                f"    return h.apply(c)\n"
                for i in range(6)
            ),
        },
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(root)
        # Slicing Python class ConfigHandler seeds USES_TYPE from its methods
        cls_slice = slice_context(store, ["pkg.service:ConfigHandler"], max_tokens=4000)
        type_syms = [t["symbol"] for t in cls_slice["sections"]["types_and_schemas"]]
        assert "pkg.models:ConfigSchema" in type_syms

        # Multi-anchor slice with both callees and 6 callers caps callers at 4 and refills callees first
        multi_slice = slice_context(
            store,
            ["pkg.service:ConfigHandler.apply", "pkg.service:other_target"],
            max_tokens=4000,
        )
        assert [c["symbol"] for c in multi_slice["sections"]["downstream_dependencies"]] == [
            "pkg.validator:validate_schema"
        ]
        assert len(multi_slice["sections"]["upstream_callers"]) <= 4


def test_types_and_schemas_compaction_java_fill_limit_dto_and_nested_class_bounds(tmp_path):
    root = tmp_path / "repo"
    dto_fields = "\n".join(f"    private final String field{i};" for i in range(12))
    dto_assigns = "\n".join(f"        this.field{i} = field{i};" for i in range(12))
    extra_methods = "\n".join(
        f"    public int extraMethod{i}(int x) {{\n        return x + {i};\n    }}"
        for i in range(10)
    )
    _write_files(
        root,
        {
            "src/com/app/RecordDto.java": (
                "package com.app;\n"
                "public class RecordDto {\n"
                f"{dto_fields}\n"
                "    public RecordDto(String field0) {\n"
                f"{dto_assigns}\n"
                "    }\n"
                "    public static class NestedBuilder {\n"
                "        private String nestedSecret;\n"
                "        public NestedBuilder() {}\n"
                "    }\n"
                "}\n"
            ),
            "src/com/app/HelperService.java": (
                "package com.app;\n"
                "public class HelperService {\n"
                "    public int usedMethod(int a) {\n"
                "        return a * 2;\n"
                "    }\n"
                f"{extra_methods}\n"
                "}\n"
            ),
            "src/com/app/Runner.java": (
                "package com.app;\n"
                "public class Runner {\n"
                "    public int run(RecordDto dto, HelperService svc) {\n"
                "        return svc.usedMethod(1);\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(root)
        res = slice_context(store, ["com.app.Runner:Runner.run"], max_tokens=4000)
        types_by_sym = {t["symbol"]: t["text"] for t in res["sections"]["types_and_schemas"]}
        dto_text = types_by_sym["com.app.RecordDto:RecordDto"]
        # DTO with 0 non-ctor methods is auto-summarized: constructor signature kept, this.field assignments and nestedSecret omitted
        assert "public RecordDto(String field0)" in dto_text
        assert "this.field5 =" not in dto_text
        assert "nestedSecret" not in dto_text

        svc_text = types_by_sym["com.app.HelperService:HelperService"]
        assert "usedMethod" in svc_text
        assert "// ... (+7 more methods)" in svc_text
        assert "extraMethod8" not in svc_text


def test_skeleton_folds_excess_private_methods_in_python_ts_and_java():
    py_src = (
        "class Service:\n"
        "    def pub_one(self) -> int:\n"
        "        return 1\n"
        "    def pub_two(self) -> int:\n"
        "        return 2\n"
        + "".join(
            f"    def _priv_{i}(self, x: int) -> int:\n        return x + {i}\n"
            for i in range(6)
        )
    ).encode("utf-8")
    py_analysis = extract_file("pkg/service.py", py_src)
    assert "def pub_one" in py_analysis.skeleton
    assert "def pub_two" in py_analysis.skeleton
    assert "def _priv_0" in py_analysis.skeleton
    assert "def _priv_5" not in py_analysis.skeleton
    assert "+3 private methods" in py_analysis.skeleton

    ts_src = (
        "export class TsService {\n"
        "  public pubOne(): number { return 1; }\n"
        + "".join(
            f"  private priv{i}(x: number): number {{ return x + {i}; }}\n"
            for i in range(6)
        )
        + "}\n"
    ).encode("utf-8")
    ts_analysis = extract_file("src/service.ts", ts_src)
    assert "pubOne(): number" in ts_analysis.skeleton
    assert "priv0(x: number)" in ts_analysis.skeleton
    assert "priv5(x: number)" not in ts_analysis.skeleton
    assert "// ... (+3 private methods)" in ts_analysis.skeleton

    java_src = (
        "package com.example;\n"
        "public class JavaService {\n"
        "  public int pubOne() { return 1; }\n"
        + "".join(
            f"  private int priv{i}(int x) {{ return x + {i}; }}\n"
            for i in range(6)
        )
        + "}\n"
    ).encode("utf-8")
    java_analysis = extract_file("src/com/example/JavaService.java", java_src)
    assert "pubOne()" in java_analysis.skeleton
    assert "priv0(int x)" in java_analysis.skeleton
    assert "priv5(int x)" not in java_analysis.skeleton
    assert "// ... (+3 private methods)" in java_analysis.skeleton


def test_delta_ignores_self_recursion_in_new_cycles(tmp_path):
    base_root, head_root = tmp_path / "base", tmp_path / "head"
    _write_files(base_root, {"pkg/walk.py": "def walk(n: int) -> int:\n    return n\n"})
    _write_files(
        head_root,
        {
            "pkg/walk.py": (
                "def walk(n: int) -> int:\n"
                "    if n <= 0:\n"
                "        return 0\n"
                "    return walk(n - 1)\n"
            )
        },
    )
    with GraphStore(":memory:") as base, GraphStore(":memory:") as head:
        base.sync_workspace(base_root)
        head.sync_workspace(head_root)
        delta = compute_delta(base, head)
        assert delta["new_cycles"] == []


def test_slice_target_mode_skeleton_and_session_deduplication(tmp_path, capsys):
    root = tmp_path / "repo"
    _write_files(
        root,
        {
            "pkg/types.py": (
                "class SharedConfig:\n"
                "    host: str\n"
                "    port: int\n"
            ),
            "pkg/handlers.py": (
                "from pkg.types import SharedConfig\n"
                "def handle_a(cfg: SharedConfig) -> str:\n"
                "    secret_body_line_a = cfg.host.strip()\n"
                "    return secret_body_line_a\n"
                "def handle_b(cfg: SharedConfig) -> int:\n"
                "    secret_body_line_b = cfg.port + 10\n"
                "    return secret_body_line_b\n"
            ),
        },
    )
    db = tmp_path / "graph.sqlite"
    assert main(["sync", str(root), "--db", str(db)]) == 0
    capsys.readouterr()

    # 1. --target-mode skeleton omits target body statements while keeping types_and_schemas
    assert (
        main(
            [
                "slice",
                "--db",
                str(db),
                "pkg.handlers:handle_a",
                "--target-mode",
                "skeleton",
                "--format",
                "json",
            ]
        )
        == 0
    )
    skel_payload = json.loads(capsys.readouterr().out)
    target_txt = skel_payload["sections"]["target_implementations"][0]["text"]
    assert "def handle_a(cfg: SharedConfig) -> str:" in target_txt
    assert "secret_body_line_a" not in target_txt
    assert "host: str" in skel_payload["sections"]["types_and_schemas"][0]["text"]

    # 2. --dedup-session elides unchanged types_and_schemas in the second slice call
    assert (
        main(
            [
                "slice",
                "--db",
                str(db),
                "pkg.handlers:handle_a",
                "--dedup-session",
                "--format",
                "json",
            ]
        )
        == 0
    )
    first_call = json.loads(capsys.readouterr().out)
    assert "host: str" in first_call["sections"]["types_and_schemas"][0]["text"]

    assert (
        main(
            [
                "slice",
                "--db",
                str(db),
                "pkg.handlers:handle_b",
                "--dedup-session",
                "--format",
                "json",
            ]
        )
        == 0
    )
    second_call = json.loads(capsys.readouterr().out)
    second_type_txt = second_call["sections"]["types_and_schemas"][0]["text"]
    assert "cached in earlier slice" in second_type_txt
    assert "host: str" not in second_type_txt


def test_direct_unit_helpers_for_skeleton_folding_and_slicer_compaction(tmp_path):
    assert _is_private_skeleton_line("  private foo(x: int) { ... }") is True
    assert _is_private_skeleton_line("  #baz(x: int) { ... }") is True
    assert _is_private_skeleton_line("  public foo(x: int) { ... }") is False
    assert _is_private_skeleton_line("  private int count;") is False

    text_five = "\n".join(f"  private m{i}() {{ ... }}" for i in range(5)) + "\n"
    folded = _fold_brace_private_methods(text_five)
    assert "// ... (+2 private methods)" in folded
    text_three = "\n".join(f"  private m{i}() {{ ... }}" for i in range(3)) + "\n"
    assert _fold_brace_private_methods(text_three) == text_three

    from types import SimpleNamespace

    raw = b"class Box {\n  private m0() { return 0; }\n}\n"
    node = SimpleNamespace(start_byte=raw.index(b"{ return"), end_byte=raw.rindex(b"}\n") - 2)
    skel = build_brace_skeleton(raw, [node])
    assert "{ ... }" in skel

    import ast

    cls_tree = ast.parse(
        "class C:\n"
        "    def __init__(self): pass\n"
        "    def _helper(self): pass\n"
        "    def pub(self): pass\n"
    )
    cls_node = cls_tree.body[0]
    assert _is_py_private_method(cls_node.body[0]) is False
    assert _is_py_private_method(cls_node.body[1]) is True
    assert _is_py_private_method(cls_node.body[2]) is False

    body_str = "\n".join(
        [
            "public class Sample {",
            "    private int a;",
            "    public Sample() {",
            "        this.a = 1;",
            "        return;",
            "    }",
            "    public static class Inner {",
            "        private int innerField;",
            "    }",
            "}",
        ]
    )
    fields = _extract_brace_class_fields(
        {"body": body_str, "start_line": 10},
        [{"start_line": 12}],
        [{"start_line": 16}],
    )
    assert fields == ["    private int a;"]

    item = {
        "symbol": "pkg.mod:Cls",
        "text": "pkg/mod.py:10-30\nclass Cls:\n    x: int = 1\n",
    }
    compacted = _compact_cached_entry(dict(item))
    assert "cached in earlier slice" in compacted["text"]

    skel_item = _target_skeleton_item(
        {
            "symbol_id": "pkg.mod:fn",
            "file_path": "pkg/mod.py",
            "start_line": 5,
            "end_line": 9,
            "signature": "def fn(x: int) -> int:",
            "body": 'def fn(x: int) -> int:\n    """Compute fn."""\n    return x + 1\n',
        }
    )
    assert "def fn(x: int) -> int:" in skel_item["text"]
    assert "Compute fn." in skel_item["text"]

    root = tmp_path / "repo"
    _write_files(root, {"a/b/mod.py": "class UniqueThing:\n    pass\n"})
    with GraphStore(":memory:") as store:
        store.sync_workspace(root)
        r1 = _resolve_suffix_path_anchor(store, "b/mod.py", "")
        assert r1 is not None and r1["module"] == "a.b.mod"
        assert _resolve_suffix_path_anchor(store, "", "") is None
        r2 = _resolve_unique_qualname_anchor(store, "UniqueThing")
        assert r2 is not None and r2["qualname"] == "UniqueThing"
        assert _resolve_unique_qualname_anchor(store, "NonExistent") is None
        entry = _build_target_entry(store, r2, {}, 2500, set(), "skeleton")
        assert entry["symbol"] == "a.b.mod:UniqueThing"
        sections = {
            "types_and_schemas": [dict(item)],
            "upstream_callers": [],
            "downstream_dependencies": [],
        }
        db_file = tmp_path / "sess.sqlite"
        _apply_session_dedup(sections, db_file)
        _apply_session_dedup(sections, db_file)
        assert "cached in earlier slice" in sections["types_and_schemas"][0]["text"]
        batch = _resolve_anchor_batch(store, ["a.b.mod:UniqueThing", "a.b.mod:MissingMethod"])
        assert "a.b.mod:UniqueThing" in batch
