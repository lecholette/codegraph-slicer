"""Fast structural workflow planner and monolith/coupling scale inspector for codegraph-slicer (v0.3.7)."""
from __future__ import annotations

from dataclasses import dataclass
import math
import re
import sqlite3
import time

from .ast_extractor import is_test_path
from .store import GraphStore

PATH_TOKEN_RE = re.compile(
    r"(?:[A-Za-z0-9_.-]+/)+[A-Za-z0-9_.-]+\.(?:py|ts|tsx|js|jsx|java)\b"
    r"|\b[A-Za-z0-9_.-]+\.(?:py|ts|tsx|js|jsx|java)\b"
)
QUALIFIED_ANCHOR_RE = re.compile(
    r"\b([A-Za-z0-9_./-]+\.(?:py|ts|tsx|js|jsx|java)):([A-Za-z_][A-Za-z0-9_.]*)\b"
)
BACKTICK_IDENT_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_.]*)`")
DOTTED_IDENT_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_.]*)\b")
CODE_IDENT_RE = re.compile(
    r"\b(?:[A-Z][a-z0-9]+(?:[A-Z][A-Za-z0-9]*)+|[a-z_][a-z0-9]*_[a-z0-9_]+)\b"
)
BROAD_VERBS_RE = re.compile(
    r"\b(all|every|across|repository|codebase|migrate|refactor|audit|system-wide|global)\b",
    re.IGNORECASE,
)
CONSTRAINT_RE = re.compile(
    r"\b(must|shall|never|cannot|without|preserve|backward[- ]compatible|strictly)\b",
    re.IGNORECASE,
)

STOPWORDS = frozenset({
    "true", "false", "none", "null", "self", "this", "return", "class", "function",
    "method", "module", "file", "files", "test", "tests", "error", "value", "string",
})


@dataclass(frozen=True)
class ScaleStructure:
    """Structural monolith and cross-file coupling summary over non-test sources."""

    slice_targets: list[str]
    max_file_loc: int
    max_file_defs: int
    cross_file_edges: int
    max_cc: int


@dataclass(frozen=True)
class PromptSignals:
    """Prompt lexical and anchor signals extracted in <1 ms."""

    explicit_paths: list[str]
    qualified_anchors: list[tuple[str, str]]
    identifiers: list[str]
    broad_count: int
    constraint_count: int


@dataclass(frozen=True)
class StructuralMetrics:
    """Subgraph and repository structural metrics for context and slicing workflow planning."""

    anchor_mode: str
    resolved_anchors: list[str]
    target_files: list[str]
    slice_targets: list[str]
    source_files: int
    source_loc: int
    target_loc: int
    max_file_loc: int
    max_file_defs: int
    max_cc: int
    cross_file_edges: int
    caller_files: int
    ambiguity: float
    broad_count: int
    constraint_count: int


@dataclass(frozen=True)
class FourAxisScores:
    """Normalized [0, 1] 4-axis structural scores and composite risk R_struct."""

    d_loc: float
    d_coupling: float
    d_monolith: float
    d_spec: float
    r_struct: float


def _norm_log2(value: float, cap: float) -> float:
    if value <= 0.0 or cap <= 0.0:
        return 0.0
    return min(1.0, max(0.0, math.log2(1.0 + value) / math.log2(1.0 + cap)))


def _norm_linear(value: float, low: float, high: float) -> float:
    if high <= low:
        return 0.0
    return min(1.0, max(0.0, (value - low) / (high - low)))


def _query_symbol_file_stats(
    db: sqlite3.Connection,
    prod_paths: set[str],
) -> tuple[dict[str, int], dict[str, int]]:
    defs_by_file: dict[str, int] = {}
    cc_by_file: dict[str, int] = {}
    if not prod_paths:
        return defs_by_file, cc_by_file
    if len(prod_paths) <= 32:
        ordered = sorted(prod_paths)
        placeholders = ",".join("?" for _ in ordered)
        cur = db.execute(
            f"SELECT file_path, COUNT(*), MAX(COALESCE(cx, 1)) "
            f"FROM symbols WHERE file_path IN ({placeholders}) AND kind != 'module' "
            f"GROUP BY file_path",
            tuple(ordered),
        )
    else:
        cur = db.execute(
            "SELECT file_path, COUNT(*), MAX(COALESCE(cx, 1)) "
            "FROM symbols WHERE kind != 'module' GROUP BY file_path"
        )
    for row in cur.fetchall():
        path = str(row[0])
        if path in prod_paths:
            defs_by_file[path] = int(row[1] or 0)
            cc_by_file[path] = int(row[2] or 1)
    return defs_by_file, cc_by_file


def _query_cross_file_edges(db: sqlite3.Connection, prod_paths: set[str]) -> int:
    if len(prod_paths) > 60:
        cur = db.execute(
            "SELECT e.source_file, t.file_path "
            "FROM (SELECT source_file, target_id FROM edges "
            "WHERE kind IN ('CALLS', 'USES_TYPE', 'INHERITS') LIMIT 2000) e "
            "JOIN symbols t ON t.symbol_id = e.target_id "
            "WHERE e.source_file != t.file_path"
        )
        return sum(
            1 for r in cur.fetchall() if str(r[0]) in prod_paths and str(r[1]) in prod_paths
        )
    cur = db.execute(
        "SELECT e.source_file, t.file_path, COUNT(*) "
        "FROM edges e "
        "JOIN symbols t ON t.symbol_id = e.target_id "
        "WHERE e.kind IN ('CALLS', 'USES_TYPE', 'INHERITS') AND e.source_file != t.file_path "
        "GROUP BY e.source_file, t.file_path"
    )
    total = 0
    for row in cur.fetchall():
        if str(row[0]) in prod_paths and str(row[1]) in prod_paths:
            total += int(row[2] or 0)
    return total


def _is_slice_monolith(loc: int, defs: int, cc: int) -> bool:
    if loc >= 300 and defs >= 1:
        return True
    if defs >= 15 and loc >= 200:
        return True
    return cc >= 25


def inspect_scale_structure(store: object, production_rows: list[dict[str, object]]) -> ScaleStructure:
    """Inspect non-test files for monoliths (>=300 LOC or >=15 defs & >=200 LOC or CC>=25) and cross-file edges."""
    if not production_rows:
        return ScaleStructure([], 0, 0, 0, 1)
    loc_by_file = {str(r["path"]): int(r["loc"]) for r in production_rows}
    prod_paths = set(loc_by_file)
    max_loc = max(loc_by_file.values(), default=0)
    db = getattr(store, "db", None)
    if db is None:
        return ScaleStructure([], max_loc, 0, 0, 1)
    try:
        defs_by_file, cc_by_file = _query_symbol_file_stats(db, prod_paths)
        xfile_edges = _query_cross_file_edges(db, prod_paths)
    except (sqlite3.Error, AttributeError, TypeError, ValueError):
        return ScaleStructure([], max_loc, 0, 0, 1)

    slice_targets = sorted(
        path
        for path, loc in loc_by_file.items()
        if _is_slice_monolith(loc, defs_by_file.get(path, 0), cc_by_file.get(path, 1))
    )
    max_defs = max(defs_by_file.values(), default=0)
    max_cc = max(cc_by_file.values(), default=1)
    return ScaleStructure(slice_targets, max_loc, max_defs, xfile_edges, max_cc)


def _append_ident_candidates(idents: list[str], raw_ident: str) -> None:
    if len(raw_ident) >= 3 and raw_ident.lower() not in STOPWORDS:
        idents.append(raw_ident)
    leaf = raw_ident.rsplit(".", 1)[-1]
    if leaf != raw_ident and len(leaf) >= 3 and leaf.lower() not in STOPWORDS:
        idents.append(leaf)


def extract_prompt_signals(prompt: str) -> PromptSignals:
    """Extract path, qualified anchor, identifier, and scope tokens from a task prompt."""
    qual_matches = QUALIFIED_ANCHOR_RE.findall(prompt)
    qual_paths = [m[0] for m in qual_matches]
    raw_paths = PATH_TOKEN_RE.findall(prompt)
    paths = list(dict.fromkeys([*qual_paths, *raw_paths]))

    idents: list[str] = []
    for _, sym in qual_matches:
        _append_ident_candidates(idents, sym)
    for m in BACKTICK_IDENT_RE.findall(prompt):
        _append_ident_candidates(idents, m)
    for m in DOTTED_IDENT_RE.findall(prompt):
        _append_ident_candidates(idents, m)
    for m in CODE_IDENT_RE.findall(prompt):
        if len(m) >= 4 and m.lower() not in STOPWORDS:
            idents.append(m)
    dedup_idents = list(dict.fromkeys(idents))[:32]
    broad = len(BROAD_VERBS_RE.findall(prompt))
    constraints = len(CONSTRAINT_RE.findall(prompt))
    return PromptSignals(paths, qual_matches, dedup_idents, broad, constraints)


def _path_matches_suffix(candidate: str, norm_token: str) -> bool:
    return candidate == norm_token or candidate.endswith("/" + norm_token) or norm_token.endswith("/" + candidate)


def _match_prompt_paths(prod_paths: list[str], explicit_paths: list[str]) -> list[str]:
    matched: list[str] = []
    for token in explicit_paths:
        norm = token.lstrip("./")
        for candidate in prod_paths:
            if _path_matches_suffix(candidate, norm) and candidate not in matched:
                matched.append(candidate)
    return matched


def _resolve_qualified_anchors(
    db: sqlite3.Connection,
    prod_set: set[str],
    qualified_anchors: list[tuple[str, str]],
    matched_paths: list[str],
) -> tuple[list[str], list[str]]:
    resolved_syms: list[str] = []
    target_files = list(matched_paths)
    for path_tok, qual in qualified_anchors:
        norm = path_tok.lstrip("./")
        scoped_files = [p for p in matched_paths if _path_matches_suffix(p, norm)]
        if scoped_files:
            placeholders = ",".join("?" for _ in scoped_files)
            rows = db.execute(
                f"SELECT symbol_id, file_path FROM symbols "
                f"WHERE file_path IN ({placeholders}) AND (qualname = ? OR qualname GLOB ?)",
                (*scoped_files, qual, f"*.{qual}"),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT symbol_id, file_path FROM symbols WHERE qualname = ? LIMIT 25",
                (qual,),
            ).fetchall()
        for sym_id, fpath in rows:
            f_str = str(fpath)
            if f_str in prod_set and _path_matches_suffix(f_str, norm):
                resolved_syms.append(str(sym_id))
                if f_str not in target_files:
                    target_files.append(f_str)
    return resolved_syms, target_files


def _query_ident_rows(db: sqlite3.Connection, ident: str, allow_suffix_scan: bool) -> list[tuple[object, object]]:
    exact = db.execute(
        "SELECT symbol_id, file_path FROM symbols WHERE kind != 'module' AND qualname = ? LIMIT 25",
        (ident,),
    ).fetchall()
    if exact or not allow_suffix_scan or "." in ident:
        return exact
    return db.execute(
        "SELECT symbol_id, file_path FROM symbols "
        "WHERE kind != 'module' AND qualname LIKE ? LIMIT 25",
        (f"%.{ident}",),
    ).fetchall()


def _resolve_identifier_anchors(
    db: sqlite3.Connection,
    prod_set: set[str],
    identifiers: list[str],
) -> tuple[list[str], list[str]]:
    resolved_syms: list[str] = []
    candidate_files: list[str] = []
    allow_suffix = len(prod_set) <= 500
    for ident in identifiers[:12]:
        rows = _query_ident_rows(db, ident, allow_suffix)
        for sym_id, fpath in rows[:6]:
            f_str = str(fpath)
            if f_str in prod_set:
                resolved_syms.append(str(sym_id))
                if f_str not in candidate_files:
                    candidate_files.append(f_str)
    return list(dict.fromkeys(resolved_syms)), candidate_files


def _resolve_symbols_for_signals(
    db: sqlite3.Connection,
    prod_set: set[str],
    signals: PromptSignals,
    matched_paths: list[str],
) -> tuple[str, list[str], list[str], float]:
    resolved_syms, target_files = _resolve_qualified_anchors(
        db, prod_set, signals.qualified_anchors, matched_paths
    )
    if resolved_syms or target_files:
        return "explicit", resolved_syms[:16], target_files[:16], 0.10 if resolved_syms else 0.25

    inferred_syms, candidate_files = _resolve_identifier_anchors(db, prod_set, signals.identifiers)
    if candidate_files:
        ambig = min(1.0, max(0.15, (len(candidate_files) - 1) / 8.0))
        return "identifier_inferred", inferred_syms[:16], candidate_files[:12], ambig
    return "repository_fallback", [], [], 0.90


def _summarize_target_edges(
    db: sqlite3.Connection,
    prod_set: set[str],
    target_files: list[str],
) -> tuple[int, int]:
    placeholders = ",".join("?" for _ in target_files)
    out_rows = db.execute(
        f"SELECT e.source_file, t.file_path, COUNT(*) FROM edges e "
        f"JOIN symbols t ON t.symbol_id = e.target_id "
        f"WHERE e.source_file IN ({placeholders}) "
        f"AND e.kind IN ('CALLS', 'USES_TYPE', 'INHERITS') AND e.source_file != t.file_path "
        f"GROUP BY e.source_file, t.file_path",
        tuple(target_files),
    ).fetchall()
    in_rows = db.execute(
        f"SELECT e.source_file, t.file_path, COUNT(*) FROM symbols t "
        f"JOIN edges e ON e.target_id = t.symbol_id "
        f"WHERE t.file_path IN ({placeholders}) "
        f"AND e.kind IN ('CALLS', 'USES_TYPE', 'INHERITS') AND e.source_file != t.file_path "
        f"GROUP BY e.source_file, t.file_path",
        tuple(target_files),
    ).fetchall()
    pair_counts: dict[tuple[str, str], int] = {}
    for src_f, dst_f, cnt in (*out_rows, *in_rows):
        s_str, d_str = str(src_f), str(dst_f)
        if s_str in prod_set and d_str in prod_set:
            pair_counts[(s_str, d_str)] = int(cnt or 0)
    target_set = set(target_files)
    caller_set = {s_str for (s_str, d_str) in pair_counts if d_str in target_set and s_str not in target_set}
    return sum(pair_counts.values()), len(caller_set)


def _collect_metrics(store: GraphStore, signals: PromptSignals) -> StructuralMetrics:
    rows = store.rows("SELECT path, loc FROM files")
    prod_rows = [dict(r) for r in rows if not is_test_path(str(r["path"]))]
    prod_loc = {str(r["path"]): int(r["loc"]) for r in prod_rows}
    prod_paths = sorted(prod_loc)
    prod_set = set(prod_paths)

    matched_paths = _match_prompt_paths(prod_paths, signals.explicit_paths)
    mode, resolved_syms, target_files, ambig = _resolve_symbols_for_signals(
        store.db, prod_set, signals, matched_paths
    )
    if not target_files:
        scale = inspect_scale_structure(store, prod_rows)
        return StructuralMetrics(
            anchor_mode=mode,
            resolved_anchors=[],
            target_files=[],
            slice_targets=scale.slice_targets[:10],
            source_files=len(prod_paths),
            source_loc=sum(prod_loc.values()),
            target_loc=sum(prod_loc.values()),
            max_file_loc=scale.max_file_loc,
            max_file_defs=scale.max_file_defs,
            max_cc=scale.max_cc,
            cross_file_edges=scale.cross_file_edges,
            caller_files=min(len(prod_paths), max(0, scale.cross_file_edges // 4)),
            ambiguity=ambig,
            broad_count=signals.broad_count,
            constraint_count=signals.constraint_count,
        )

    defs_map, cc_map = _query_symbol_file_stats(store.db, set(target_files))
    x_edges, caller_count = _summarize_target_edges(store.db, prod_set, target_files)
    target_slice = [
        f
        for f in target_files
        if _is_slice_monolith(prod_loc.get(f, 0), defs_map.get(f, 0), cc_map.get(f, 1))
    ]
    return StructuralMetrics(
        anchor_mode=mode,
        resolved_anchors=resolved_syms,
        target_files=target_files,
        slice_targets=target_slice,
        source_files=len(prod_paths),
        source_loc=sum(prod_loc.values()),
        target_loc=sum(prod_loc.get(f, 0) for f in target_files),
        max_file_loc=max((prod_loc.get(f, 0) for f in target_files), default=0),
        max_file_defs=max((defs_map.get(f, 0) for f in target_files), default=0),
        max_cc=max((cc_map.get(f, 1) for f in target_files), default=1),
        cross_file_edges=x_edges,
        caller_files=caller_count,
        ambiguity=ambig,
        broad_count=signals.broad_count,
        constraint_count=signals.constraint_count,
    )


def score_four_axes(m: StructuralMetrics) -> FourAxisScores:
    """Compute D_loc, D_coupling, D_monolith, D_spec and composite R_struct in [0, 1]."""
    fallback_flag = 1.0 if m.anchor_mode == "repository_fallback" else 0.0
    d_loc = (
        0.45 * m.ambiguity
        + 0.35 * fallback_flag
        + 0.20 * _norm_log2(float(max(1, len(m.target_files)) if m.target_files else m.source_files), 32.0)
    )
    d_coupling = (
        0.55 * _norm_log2(float(m.cross_file_edges), 64.0)
        + 0.45 * _norm_log2(float(m.caller_files), 16.0)
    )
    d_monolith = (
        0.40 * _norm_linear(float(m.max_file_loc), 200.0, 900.0)
        + 0.35 * _norm_linear(float(m.max_file_defs), 10.0, 45.0)
        + 0.25 * _norm_linear(float(m.max_cc), 8.0, 35.0)
    )
    d_spec = (
        0.55 * _norm_linear(float(m.broad_count), 0.0, 5.0)
        + 0.45 * _norm_linear(float(m.constraint_count), 0.0, 8.0)
    )
    base_risk = 0.24 * d_loc + 0.31 * d_coupling + 0.27 * d_monolith + 0.18 * d_spec
    synergy = 0.18 * max(d_coupling * d_monolith, d_loc * d_coupling)
    r_struct = min(1.0, round(base_risk + synergy, 4))
    return FourAxisScores(
        d_loc=round(min(1.0, d_loc), 4),
        d_coupling=round(min(1.0, d_coupling), 4),
        d_monolith=round(min(1.0, d_monolith), 4),
        d_spec=round(min(1.0, d_spec), 4),
        r_struct=r_struct,
    )


def _is_compact_direct_read(m: StructuralMetrics) -> bool:
    return m.source_files <= 3 and m.source_loc < 400 and m.max_file_loc < 250 and m.max_cc < 12


def _has_monolith_or_hub(m: StructuralMetrics) -> bool:
    return _is_slice_monolith(m.max_file_loc, m.max_file_defs, m.max_cc) or m.cross_file_edges >= 40


def _is_localized_compact_target(m: StructuralMetrics, s: FourAxisScores, has_mono_hub: bool) -> bool:
    if m.anchor_mode == "repository_fallback" or has_mono_hub:
        return False
    return 1 <= len(m.target_files) <= 2 and m.target_loc < 300 and s.d_coupling < 0.30


def _select_workflow(m: StructuralMetrics, s: FourAxisScores) -> tuple[str, bool]:
    if _is_compact_direct_read(m):
        return "bypass_direct_read", True
    has_mono_hub = _has_monolith_or_hub(m)
    if _is_localized_compact_target(m, s, has_mono_hub):
        return "repo_map_and_path_hint", True
    if s.r_struct >= 0.30 or has_mono_hub or (m.anchor_mode == "repository_fallback" and m.source_loc >= 2500):
        return "mandatory_symbol_slice", False
    return "targeted_symbol_slice", False


def route_prompt(store: GraphStore, prompt: str) -> dict[str, object]:
    """Plan slicing workflow and localize target symbols/monoliths in <25 ms (model-agnostic)."""
    t0 = time.perf_counter()
    signals = extract_prompt_signals(prompt)
    metrics = _collect_metrics(store, signals)
    scores = score_four_axes(metrics)
    workflow, bypass = _select_workflow(metrics, scores)
    elapsed_ms = round((time.perf_counter() - t0) * 1000.0, 3)
    return {
        "schema": "codegraph-route-v1",
        "workflow_mode": workflow,
        "bypass_slicer": bypass,
        "anchor_mode": metrics.anchor_mode,
        "resolved_anchors": metrics.resolved_anchors,
        "target_files": metrics.target_files,
        "slice_targets": metrics.slice_targets,
        "scores": {
            "D_loc": scores.d_loc,
            "D_coupling": scores.d_coupling,
            "D_monolith": scores.d_monolith,
            "D_spec": scores.d_spec,
            "R_struct": scores.r_struct,
        },
        "metrics": {
            "source_files": metrics.source_files,
            "source_loc": metrics.source_loc,
            "target_loc": metrics.target_loc,
            "max_file_loc": metrics.max_file_loc,
            "max_file_defs": metrics.max_file_defs,
            "max_cc": metrics.max_cc,
            "cross_file_edges": metrics.cross_file_edges,
            "caller_files": metrics.caller_files,
        },
        "latency_ms": elapsed_ms,
    }
