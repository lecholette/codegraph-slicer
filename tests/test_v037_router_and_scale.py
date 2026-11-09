"""Tests for codegraph-slicer v0.3.7 monolith/coupling scale guard, 1M-LOC index speed, and workflow planner."""
from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time

import pytest

from codegraph_slicer import GraphStore, __version__, build_repo_map, inspect_scale_structure, rank, route_prompt
from codegraph_slicer.cli import _classify_scale, _scale_recommendation, main
from codegraph_slicer.pagerank import _ns_and_subpkg
from codegraph_slicer.router import (
    FourAxisScores,
    StructuralMetrics,
    _is_ctor_qualname,
    _norm_linear,
    _norm_log2,
    _rank_slice_targets,
    _select_tier_and_workflow,
    extract_prompt_signals,
    score_four_axes,
)
from codegraph_slicer.slicer import AnchorNotFoundError, _anchor
from codegraph_slicer.treesitter_support import _fold_brace_private_methods
from codegraph_slicer.ts_extractor import parse_tsconfig_paths


class _BrokenDB:
    def execute(self, _sql: str, _args: tuple = ()) -> object:
        raise sqlite3.OperationalError("broken db")


class _BrokenStore:
    db = _BrokenDB()


class _NoDBStore:
    db = None


def test_version_and_scale_guard_helpers() -> None:
    assert __version__ == "0.3.8"
    assert parse_tsconfig_paths(b"not-json") == {}
    assert parse_tsconfig_paths(b"[]") == {}
    assert parse_tsconfig_paths(b'{"compilerOptions": {"baseUrl": "src", "paths": {"@/*": ["app/*"]}}}') == {"@/*": "src/app/*"}
    assert _norm_log2(0.0, 10.0) == 0.0
    assert _norm_log2(5.0, 0.0) == 0.0
    assert _norm_linear(5.0, 10.0, 10.0) == 0.0
    assert inspect_scale_structure(_NoDBStore(), []) == inspect_scale_structure(_NoDBStore(), [])
    assert inspect_scale_structure(_NoDBStore(), [{"path": "a.py", "loc": 50}]).max_file_loc == 50
    assert inspect_scale_structure(_BrokenStore(), [{"path": "a.py", "loc": 50}]).slice_targets == []

    assert _classify_scale(4, 900, ["python"], [], has_monolith=True) == (
        "slice_monoliths",
        False,
        [],
        "monolith_present",
    )
    assert _classify_scale(8, 1200, ["python"], [], has_monolith=False, cross_file_edges=45) == (
        "graph_optional",
        False,
        [],
        "coupled_graph",
    )


def test_scale_recommendation_detects_monolith_and_coupled_graph(tmp_path: Path) -> None:
    mono_root = tmp_path / "mono_repo"
    mono_root.mkdir()
    mono_lines = ["def step_0():\n    return 0\n"] + [f"    # padding {i}\n" for i in range(320)]
    (mono_root / "app.py").write_text("".join(mono_lines), encoding="utf-8")
    (mono_root / "helper.py").write_text("def h():\n    return 1\n", encoding="utf-8")
    mono_db = tmp_path / "mono.sqlite"
    with GraphStore(mono_db) as store:
        store.sync_workspace(mono_root)
        rec = _scale_recommendation(store)
        assert rec["mode"] == "slice_monoliths"
        assert rec["bypass_slicer"] is False
        assert rec["reason"] == "monolith_present"
        assert rec["slice_targets"] == ["app.py"]

    coupled_root = tmp_path / "coupled_repo"
    coupled_root.mkdir()
    (coupled_root / "core.py").write_text(
        "\n".join(f"def target_{i}(x: int) -> int:\n    return x + {i}\n" for i in range(12)),
        encoding="utf-8",
    )
    for idx in range(5):
        imports = ", ".join(f"target_{i}" for i in range(10))
        calls = " + ".join(f"target_{i}(v)" for i in range(10))
        (coupled_root / f"caller_{idx}.py").write_text(
            f"from core import {imports}\n\ndef run_{idx}(v: int) -> int:\n    return {calls}\n",
            encoding="utf-8",
        )
    coupled_db = tmp_path / "coupled.sqlite"
    with GraphStore(coupled_db) as store:
        store.sync_workspace(coupled_root)
        rec_c = _scale_recommendation(store)
        assert rec_c["mode"] == "graph_optional"
        assert rec_c["bypass_slicer"] is False
        assert rec_c["reason"] == "coupled_graph"


def test_router_workflows_model_agnostic_and_cli(tmp_path: Path, capsys) -> None:
    # 1. Tiny single-file repo -> bypass_direct_read
    tiny_root = tmp_path / "tiny_repo"
    tiny_root.mkdir()
    (tiny_root / "calc.py").write_text(
        "def compute_tax(amount: int) -> int:\n    return amount * 2\n",
        encoding="utf-8",
    )
    tiny_db = tmp_path / "tiny.sqlite"
    with GraphStore(tiny_db) as store:
        store.sync_workspace(tiny_root)
        res_tiny = route_prompt(store, "Fix `compute_tax` in calc.py:compute_tax")
        assert res_tiny["recommended_tier"] == "low"
        assert res_tiny["workflow_mode"] == "bypass_direct_read"
        assert res_tiny["bypass_slicer"] is True
        assert res_tiny["anchor_mode"] == "explicit"

    # 2. Multi-file repo with modular leaf + monolith + coupled hub
    ent_root = tmp_path / "ent_repo"
    ent_root.mkdir()
    for i in range(4):
        (ent_root / f"leaf_{i}.py").write_text(
            f"class LeafService{i}:\n    def process_item_{i}(self, x: int) -> int:\n        return x + {i}\n"
            + "".join(f"# line {k}\n" for k in range(110)),
            encoding="utf-8",
        )
    mono_body = "\n".join(
        f"    def handle_route_{i}(self, v: int) -> int:\n        return v + {i}\n"
        for i in range(18)
    ) + "".join(f"# pad {k}\n" for k in range(260))
    (ent_root / "monolith.py").write_text(f"class GiantGateway:\n{mono_body}", encoding="utf-8")

    ent_db = tmp_path / "ent.sqlite"
    with GraphStore(ent_db) as store:
        store.sync_workspace(ent_root)
        res_leaf = route_prompt(store, "Update leaf_0.py to adjust return value")
        assert res_leaf["recommended_tier"] == "low"
        assert res_leaf["workflow_mode"] == "repo_map_and_path_hint"
        assert res_leaf["bypass_slicer"] is True

        res_mono = route_prompt(
            store,
            "Update `GiantGateway.handle_route_1` so it must preserve backward-compatible behavior",
        )
        assert res_mono["anchor_mode"] == "identifier_inferred"
        assert res_mono["recommended_tier"] in ("mid", "high")
        assert res_mono["bypass_slicer"] is False
        assert res_mono["workflow_mode"] == "mandatory_symbol_slice"
        assert res_mono["slice_targets"] == ["monolith.py"]

        res_broad = route_prompt(
            store,
            "Audit and refactor all modules across the entire repository to strictly enforce global invariants",
        )
        assert res_broad["anchor_mode"] == "repository_fallback"
        assert res_broad["recommended_tier"] in ("mid", "high")
        assert res_broad["bypass_slicer"] is False
        assert res_broad["workflow_mode"] == "mandatory_symbol_slice"

    low_slice_metrics = StructuralMetrics(
        anchor_mode="identifier_inferred",
        resolved_anchors=["a:f", "b:g", "c:h"],
        target_files=["a.py", "b.py", "c.py"],
        slice_targets=[],
        source_files=10,
        source_loc=1200,
        target_loc=240,
        max_file_loc=100,
        max_file_defs=5,
        max_cc=4,
        cross_file_edges=4,
        caller_files=1,
        ambiguity=0.2,
        broad_count=0,
        constraint_count=0,
    )
    low_scores = score_four_axes(low_slice_metrics)
    assert _select_tier_and_workflow(low_slice_metrics, low_scores) == ("low", "targeted_symbol_slice", False)
    high_scores = FourAxisScores(0.8, 0.8, 0.8, 0.8, 0.75)
    assert _select_tier_and_workflow(low_slice_metrics, high_scores) == ("high", "mandatory_symbol_slice", False)

    assert main(["route", "--db", str(ent_db), "--prompt", "Inspect GiantGateway in monolith.py", "--format", "json"]) == 0
    cli_json = json.loads(capsys.readouterr().out)
    assert cli_json["schema"] == "codegraph-route-v1"
    assert cli_json["recommended_tier"] in ("mid", "high")
    assert main(["route", "--db", str(ent_db), "--prompt", "Inspect GiantGateway", "--format", "markdown"]) == 0
    assert "codegraph-route-v1" in capsys.readouterr().out
    assert main(["route", "--db", str(ent_db), "--prompt", "Inspect GiantGateway", "--format", "xml"]) == 0
    assert "<codegraph_report>" in capsys.readouterr().out

    sig = extract_prompt_signals("Check `pkg.Service.run_task` and CamelCaseToken in src/app.py:Service.run_task")
    assert "src/app.py" in sig.explicit_paths
    assert ("src/app.py", "Service.run_task") in sig.qualified_anchors
    assert "CamelCaseToken" in sig.identifiers

    assert _is_ctor_qualname("Service.Service") is True
    assert _is_ctor_qualname("Service.Service#2") is True
    assert _is_ctor_qualname("Service.execute") is False
    assert _rank_slice_targets(["b.py", "a.py"], ()) == ["b.py", "a.py"]
    assert _rank_slice_targets(["src/alpha.py", "src/strategy/pack.py"], ("strategy", "pack")) == [
        "src/strategy/pack.py",
        "src/alpha.py",
    ]
    assert _ns_and_subpkg("com.enterprise.strategy.PackHandler") == ("com.enterprise", "com.enterprise.strategy")
    assert _ns_and_subpkg("com.example.Main") == ("com.example", "com.example")
    assert _ns_and_subpkg("services.billing.splitter") == ("services", "services.billing")
    assert _ns_and_subpkg("standalone") == ("", "")

    with GraphStore(ent_db) as store:
        res_kw = route_prompt(store, "inspect monolith route handling behavior")
        assert res_kw["anchor_mode"] == "keyword_match"
        assert "monolith.py" in res_kw["target_files"]
        assert any("GiantGateway" in a for a in res_kw["resolved_anchors"])

    # 3. Multi-namespace unseeded PageRank prioritizes diverse application subpackages over dense single-subpackage library
    multi_root = tmp_path / "multi_ns"
    for sub in ("cli", "orchestration", "strategy", "storage"):
        d = multi_root / "com" / "enterprise" / sub
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{sub}_service.py").write_text(
            f"def run_{sub}(x: int) -> int:\n    return x + 1\n",
            encoding="utf-8",
        )
    lib_dir = multi_root / "com" / "vendor" / "gson"
    lib_dir.mkdir(parents=True, exist_ok=True)
    for idx in range(10):
        (lib_dir / f"lib_{idx}.py").write_text(
            f"def parse_{idx}(v: int) -> int:\n    return v + {idx}\n",
            encoding="utf-8",
        )
    multi_db = tmp_path / "multi.sqlite"
    with GraphStore(multi_db) as store:
        store.sync_workspace(multi_root)
        scores = rank(store)
        assert sum(scores.values()) == pytest.approx(1.0)
        rmap = build_repo_map(store, max_tokens=2000)
        assert rmap["items"][0]["path"].startswith("com/enterprise/")

    folded = _fold_brace_private_methods(
        "class C {\n  _p1() { ... }\n\n  _p2() { ... }\n\n  _p3() { ... }\n\n  _p4() { ... }\n\n  _p5() { ... }\n\n  pub() { ... }\n}\n"
    )
    assert "// ... (+2 private methods)\n\n  pub() { ... }" in folded
    assert "\n\n\n" not in folded


def test_1m_loc_graphstore_scale_route_and_anchor_recovery_speed(tmp_path: Path) -> None:
    """Verify <25ms warm latency on a 1,001,110-LOC GraphStore (5,000 files, 75,000 symbols, 248,684 edges)."""
    db_path = tmp_path / "enterprise_1m.sqlite"
    with GraphStore(db_path) as store:
        conn = store.db
        conn.execute("PRAGMA synchronous = OFF")
        file_rows = []
        sym_rows = []
        edge_rows = []

        for i in range(5000):
            pkg = f"services/domain_{i // 50}/mod_{i}.py"
            mod = f"services.domain_{i // 50}.mod_{i}"
            loc, cc_top = 200, 12
            if i == 0:
                pkg, mod, loc, cc_top = "services/billing/charge_splitter.py", "services.billing.charge_splitter", 140, 8
            elif i == 100:
                pkg, mod, loc, cc_top = "services/legacy/monolith_engine.py", "services.legacy.monolith_engine", 1450, 34
            elif i == 200:
                pkg, mod, loc, cc_top = "core/dag_orchestrator.py", "core.dag_orchestrator", 920, 28
            file_rows.append((pkg, mod, f"sha_{i}", loc, ""))

            sym_count = 15 if i in (100, 200) else 10
            for s in range(sym_count):
                qual = f"Class_{i}.func_{i}_{s}"
                if i == 0 and s == 0:
                    qual = "ChargeSplitter.split_charge"
                elif i == 100 and s == 0:
                    qual = "MonolithEngine.process_monolith_batch"
                elif i == 200 and s == 0:
                    qual = "DagOrchestrator.execute_dag_plan"
                sid = f"{mod}:{qual}"
                sym_rows.append((sid, pkg, mod, qual, "method", s * 10 + 1, s * 10 + 8, f"def {qual}():", "pass", cc_top if s == 0 else 4, 20))

        for e in range(420):
            nbr_idx = 201 + (e % 14)
            nbr_mod = f"services.domain_{nbr_idx // 50}.mod_{nbr_idx}"
            edge_rows.append(("core/dag_orchestrator.py", "core.dag_orchestrator:DagOrchestrator.execute_dag_plan", f"{nbr_mod}:Class_{nbr_idx}.func_{nbr_idx}_{e % 10}", "CALLS", e + 1))

        for i in range(25, 4990):
            if i in (100, 200):
                continue
            src_p = f"services/domain_{i // 50}/mod_{i}.py"
            src_m = f"services.domain_{i // 50}.mod_{i}"
            for k in range(50):
                tgt_idx = 25 + ((i + k + 1) % 4900)
                if tgt_idx in (100, 200):
                    tgt_idx += 1
                tgt_m = f"services.domain_{tgt_idx // 50}.mod_{tgt_idx}"
                edge_rows.append((src_p, f"{src_m}:Class_{i}.func_{i}_0", f"{tgt_m}:Class_{tgt_idx}.func_{tgt_idx}_{k % 10}", "CALLS", k + 1))

        conn.executemany("INSERT INTO files VALUES (?, ?, ?, ?, ?)", file_rows)
        conn.executemany("INSERT INTO symbols VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", sym_rows)
        conn.executemany("INSERT INTO edges VALUES (?, ?, ?, ?, ?)", edge_rows)
        conn.commit()

        assert sum(r[3] for r in file_rows) >= 1_000_000

        # 1. Localized leaf in 1M-LOC repo -> repo_map_and_path_hint, <25 ms
        r_leaf = route_prompt(store, "Fix rounding in services/billing/charge_splitter.py:ChargeSplitter.split_charge")
        assert r_leaf["latency_ms"] < 25.0
        assert r_leaf["workflow_mode"] == "repo_map_and_path_hint"
        assert r_leaf["bypass_slicer"] is True
        assert "services.billing.charge_splitter:ChargeSplitter.split_charge" in r_leaf["resolved_anchors"]

        # 2. Monolith by dotted identifier in 1M-LOC repo -> mandatory_symbol_slice, <25 ms
        r_mono = route_prompt(store, "Refactor `MonolithEngine.process_monolith_batch` to support batching")
        assert r_mono["latency_ms"] < 25.0
        assert r_mono["workflow_mode"] == "mandatory_symbol_slice"
        assert r_mono["bypass_slicer"] is False
        assert r_mono["slice_targets"] == ["services/legacy/monolith_engine.py"]

        # 3. High-coupling hub in 1M-LOC repo -> mandatory_symbol_slice, <25 ms
        r_hub = route_prompt(store, "Add timeout cancellation to core/dag_orchestrator.py:DagOrchestrator.execute_dag_plan")
        assert r_hub["latency_ms"] < 25.0
        assert r_hub["workflow_mode"] == "mandatory_symbol_slice"
        assert r_hub["metrics"]["cross_file_edges"] >= 400

        # 4. Scale recommendation (`map --recommend-workflow`) and AnchorNotFoundError recovery at 1M LOC
        t_scale = time.perf_counter()
        rec_1m = _scale_recommendation(store)
        assert (time.perf_counter() - t_scale) * 1000.0 < 35.0
        assert rec_1m["mode"] == "slice_monoliths"
        assert rec_1m["bypass_slicer"] is False
        assert rec_1m["slice_targets"] == ["core/dag_orchestrator.py", "services/legacy/monolith_engine.py"]

        t0 = time.perf_counter()
        with pytest.raises(AnchorNotFoundError) as exc_info:
            _anchor(store, "services/billing/charge_splitter.py:ChargeSplitter.split_charges")
        dt_ms = (time.perf_counter() - t0) * 1000.0
        assert dt_ms < 25.0
        assert "services/billing/charge_splitter.py:ChargeSplitter.split_charge" in exc_info.value.nearest_anchors

        with pytest.raises(AnchorNotFoundError) as exc_bare:
            _anchor(store, "ChargeSplitter.split_charges")
        assert "services/billing/charge_splitter.py:ChargeSplitter.split_charge" in exc_bare.value.nearest_anchors
