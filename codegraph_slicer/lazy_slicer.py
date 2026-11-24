"""Target-seeded lazy slicing without a full repository-wide index."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
from time import monotonic

from .ast_extractor import (
    MODULE_END,
    SUPPORTED_EXTENSIONS,
    TS_JS_EXTENSIONS,
    CodeGraphError,
    CodeGraphParseError,
    detect_layout,
    extract_file,
    is_supported_source_path,
    valid_path,
)
from .slicer import (
    build_slice_outline,
    rendered_tokens,
    slice_context,
)
from .source_provider import LocalGitProvider, SourceProvider
from .store import (
    GraphStore,
    _dedupe_analysis_symbols,
    _disambiguate_analysis,
    _read_ts_or_js_config,
    _should_skip_rel_path,
)

_JAVA_ROOT_MARKERS = ("src/main/java/", "src/test/java/", "src/", "app/", "java/")


def _find_repo_files_index(provider: SourceProvider) -> dict[str, list[str]]:
    """Build a basename/suffix index of source files from the provider."""
    by_suffix: dict[str, list[str]] = {}
    for p in provider.list_files():
        by_suffix.setdefault(PurePosixPath(p).name, []).append(p)
    return by_suffix


def _anchor_seed_info(provider: SourceProvider, anchor: str) -> tuple[str | None, str]:
    """Return (direct_rel_path_or_None, search_needle) for an anchor string."""
    if ":" in anchor:
        head, tail = anchor.split(":", 1)
        if provider.file_exists(head):
            return head, tail.split(".")[-1] if tail and not tail.isdigit() else ""
        # module:qualname or dotted class
        leaf = tail.split(".")[-1] if tail else head.split(".")[-1]
        return None, leaf
    if provider.file_exists(anchor):
        return anchor, PurePosixPath(anchor).stem
    return None, anchor.split(".")[-1]


def _java_source_prefixes(seed_paths: list[str]) -> list[str]:
    prefixes: list[str] = []
    for sp in seed_paths:
        for marker in _JAVA_ROOT_MARKERS:
            idx = sp.find(marker)
            if idx != -1:
                pfx = sp[: idx + len(marker)]
                if pfx not in prefixes:
                    prefixes.append(pfx)
    for marker in _JAVA_ROOT_MARKERS:
        if marker not in prefixes:
            prefixes.append(marker)
    return prefixes


def _resolve_target_to_paths(
    provider: SourceProvider,
    target: str,
    from_file: str,
    java_prefixes: list[str],
    files_by_name: dict[str, list[str]],
) -> list[str]:
    clean = target.split(MODULE_END, 1)[0].split(":", 1)[0].strip()
    if from_file.endswith(".java") and MODULE_END in target:
        sym_tail = target.split(MODULE_END, 1)[1].split(":", 1)[0].strip()
        head_ident = sym_tail.split(".", 1)[0]
        if head_ident[:1].isupper():
            pkg_prefix = clean.rpartition(".")[0]
            clean = f"{pkg_prefix}.{head_ident}" if pkg_prefix else head_ident
    if not clean or clean.startswith("?"):
        return []
    found: list[str] = []
    if from_file.endswith(".java"):
        parts = clean.split(".")
        # Try ClassName.java or outer class if Member.Class
        for depth in (len(parts), max(1, len(parts) - 1)):
            sub = parts[:depth]
            rel_tail = "/".join(sub) + ".java"
            for pfx in java_prefixes:
                cand = pfx + rel_tail
                if provider.file_exists(cand):
                    found.append(cand)
                    return found
            leaf_name = sub[-1] + ".java"
            for cand in files_by_name.get(leaf_name, ()):
                if cand.endswith("/" + rel_tail) or cand == rel_tail:
                    found.append(cand)
                    return found
    elif from_file.endswith(".py"):
        rel_mod = clean.replace(".", "/")
        prefixes = ["", "src/"]
        if "/src/" in from_file:
            prefixes.insert(0, from_file.split("/src/", 1)[0] + "/src/")
        for pfx in prefixes:
            for suffix in (".py", "/__init__.py"):
                cand = pfx + rel_mod + suffix
                if provider.file_exists(cand):
                    found.append(cand)
                    return found
    elif from_file.endswith(TS_JS_EXTENSIONS):
        base_dir = PurePosixPath(from_file).parent
        if clean.startswith("."):
            joined = PurePosixPath(os.path.normpath((base_dir / clean).as_posix())).as_posix()
        else:
            joined = clean.replace(".", "/")
        for ext in ("", *TS_JS_EXTENSIONS, *(f"/index{e}" for e in TS_JS_EXTENSIONS)):
            cand = joined + ext
            if provider.file_exists(cand):
                found.append(cand)
                return found
    return found


def _populate_lazy_store(
    root: Path | None,
    anchors: list[str],
    *,
    max_files: int = 40,
    max_hops: int = 2,
    include_callers: bool = True,
    provider: SourceProvider | None = None,
) -> tuple[GraphStore, dict]:
    started = monotonic()
    if provider is None:
        if root is None:
            raise CodeGraphError("lazy slice requires root or provider")
        provider = LocalGitProvider(root)
    max_files = max(1, max_files)
    max_hops = max(0, max_hops)

    seed_paths: list[str] = []
    direct_seed_paths: set[str] = set()
    needles: list[str] = []
    for anchor in anchors:
        direct_path, needle = _anchor_seed_info(provider, anchor)
        if direct_path:
            direct_seed_paths.add(direct_path)
            if direct_path not in seed_paths:
                seed_paths.append(direct_path)
        if needle and needle not in needles:
            needles.append(needle)
        if not direct_path and needle:
            for cand in provider.grep(needle, limit=min(5, max_files)):
                if cand not in seed_paths:
                    seed_paths.append(cand)

    if not seed_paths:
        raise CodeGraphError(f"could not locate any seed files for anchors: {anchors!r}")

    files_by_name = _find_repo_files_index(provider) if any(p.endswith(".java") for p in seed_paths) else {}
    java_prefixes = _java_source_prefixes(seed_paths)

    if isinstance(provider, LocalGitProvider):
        tsconfig_bytes = _read_ts_or_js_config(provider.root)
    else:
        cfg_map = provider.read_files_batch(
            [p for p in ("tsconfig.json", "jsconfig.json") if provider.file_exists(p)]
        )
        tsconfig_bytes = cfg_map.get("tsconfig.json") or cfg_map.get("jsconfig.json") or b""
    path_aliases = None
    if tsconfig_bytes:
        from .ts_extractor import parse_tsconfig_paths

        path_aliases = parse_tsconfig_paths(tsconfig_bytes)

    explored_raw: dict[str, bytes] = {}
    frontier: list[str] = []
    truncated = False

    def _enqueue(rel_p: str) -> None:
        nonlocal truncated
        if rel_p in explored_raw or rel_p in frontier:
            return
        if len(explored_raw) + len(frontier) >= max_files:
            truncated = True
            return
        if provider.file_exists(rel_p) and is_supported_source_path(rel_p) and not _should_skip_rel_path(PurePosixPath(rel_p).parts):
            frontier.append(rel_p)

    for sp in seed_paths:
        _enqueue(sp)

    # Bounded hop expansion over imports / types / calls
    analyses_by_path = {}
    parse_failures: dict[str, str] = {}
    for hop in range(max_hops + 1):
        if not frontier:
            break
        batch = frontier[:]
        frontier.clear()
        batch_bytes = provider.read_files_batch(batch)
        for rel_p in batch:
            if rel_p in batch_bytes:
                explored_raw[rel_p] = batch_bytes[rel_p]
        source_root, packages = detect_layout(explored_raw.keys())
        for rel_p in batch:
            if rel_p not in explored_raw:
                continue
            try:
                analysis = extract_file(
                    rel_p,
                    explored_raw[rel_p],
                    packages,
                    source_root,
                    *([path_aliases] if path_aliases else []),
                )
            except CodeGraphParseError as exc:
                if rel_p in direct_seed_paths:
                    raise
                if rel_p not in parse_failures:
                    parse_failures[rel_p] = str(exc)
                    sys.stderr.write(
                        f"codegraph-slicer: warning: skipping unparseable file {rel_p}: {exc}\n"
                    )
                continue
            analyses_by_path[rel_p] = analysis
            if hop < max_hops:
                for edge in analysis.edges:
                    if edge.kind in ("ALIAS", "USES_TYPE", "INHERITS", "CALLS", "WILDCARD_IMPORT"):
                        for dep in _resolve_target_to_paths(
                            provider, edge.target, rel_p, java_prefixes, files_by_name
                        ):
                            _enqueue(dep)

    # Bounded candidate caller discovery via grep
    caller_discovery = "skipped"
    if include_callers:
        if len(explored_raw) >= max_files:
            caller_discovery = "budget_exhausted"
            truncated = True
        else:
            caller_cands: list[str] = []
            for needle in needles:
                if len(explored_raw) + len(caller_cands) >= max_files:
                    truncated = True
                    break
                remaining = max_files - (len(explored_raw) + len(caller_cands))
                for cand in provider.grep(needle, limit=min(15, remaining)):
                    if cand not in explored_raw and cand not in caller_cands and provider.file_exists(cand):
                        caller_cands.append(cand)
            if caller_cands:
                explored_raw.update(provider.read_files_batch(caller_cands))
            post_meta = provider.coverage_metadata()
            if post_meta.get("grep_note"):
                caller_discovery = "skipped_non_default_ref"
            elif provider.provider_name == "github_cli":
                caller_discovery = "default_branch_hint"
            else:
                caller_discovery = "local_grep"

    # Re-extract with unified layout and insert into an in-memory GraphStore
    source_root, packages = detect_layout(explored_raw.keys())
    store = GraphStore(":memory:")
    indexed_count = 0
    store.db.execute("BEGIN IMMEDIATE")
    with store.db:
        taken_modules: set[str] = set()
        for rel_p in sorted(explored_raw):
            raw = explored_raw[rel_p]
            sha = "sha256:" + hashlib.sha256(raw).hexdigest()
            try:
                raw_analysis = extract_file(
                    rel_p,
                    raw,
                    packages,
                    source_root,
                    *([path_aliases] if path_aliases else []),
                )
            except CodeGraphParseError as exc:
                if rel_p in direct_seed_paths:
                    raise
                if rel_p not in parse_failures:
                    parse_failures[rel_p] = str(exc)
                    sys.stderr.write(
                        f"codegraph-slicer: warning: skipping unparseable file {rel_p}: {exc}\n"
                    )
                continue
            analysis = _dedupe_analysis_symbols(_disambiguate_analysis(raw_analysis, taken_modules))
            store.db.execute(
                "INSERT INTO files VALUES(?,?,?,?,?)",
                (analysis.path, analysis.module, sha, analysis.symbols[0].end, analysis.skeleton),
            )
            store.db.executemany(
                "INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                [
                    (
                        s.symbol_id,
                        analysis.path,
                        analysis.module,
                        s.symbol_id.split(":", 1)[1],
                        s.kind,
                        s.start,
                        s.end,
                        s.signature,
                        s.body,
                        s.cx,
                        len(s.body) // 4 + 1,
                    )
                    for s in analysis.symbols
                ],
            )
            store.db.executemany(
                "INSERT INTO raw_edges VALUES(?,?,?,?,?)",
                [(analysis.path, e.source, e.target, e.kind, e.line) for e in analysis.edges],
            )
            indexed_count += 1
        store._rebind(source_root=source_root)
        provider_meta = provider.coverage_metadata()
        snapshot_root = str(provider.root) if isinstance(provider, LocalGitProvider) else str(provider_meta.get("repo", ""))
        snapshot_revision = str(provider_meta.get("commit", ""))
        for key, value in (
            ("snapshot_root", snapshot_root),
            ("snapshot_kind", "lazy"),
            ("snapshot_revision", snapshot_revision),
            ("source_root", source_root),
            ("packages", ",".join(sorted(packages))),
        ):
            store.db.execute(
                "INSERT INTO metadata(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )

    coverage = {
        "mode": "lazy_target_seeded",
        **provider_meta,
        "caller_discovery": caller_discovery,
        "explored_files": len(explored_raw),
        "indexed_files": indexed_count,
        "failed_files": [
            {"path": p, "error": err} for p, err in sorted(parse_failures.items())
        ],
        "max_files": max_files,
        "max_hops": max_hops,
        "truncated": truncated,
        "elapsed_ms": round((monotonic() - started) * 1000, 3),
        "note": (
            "Only seed files, resolved imports/types, and grep-matched candidate callers "
            "within budget were indexed; callers outside explored scope are not represented."
        ),
    }
    return store, coverage


def _rendered_json_tokens(candidate) -> int:
    return rendered_tokens(
        json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
    )


def _wrap_coverage_estimator(render_estimator, coverage: dict):
    base = render_estimator or (lambda candidate: _rendered_json_tokens(candidate))
    return lambda candidate: base({**candidate, "coverage": coverage})


def lazy_slice_context(
    root: str | Path | None,
    anchors: list[str],
    max_tokens: int = 8000,
    include_callee_bodies: bool = False,
    *,
    max_files: int = 40,
    max_hops: int = 2,
    include_callers: bool = True,
    render_estimator=None,
    target_mode: str = "full",
    dedup_session: bool | str = False,
    contracts: bool | None = None,
    provider: SourceProvider | None = None,
):
    store, coverage = _populate_lazy_store(
        Path(root) if root is not None else None,
        anchors,
        max_files=max_files,
        max_hops=max_hops,
        include_callers=include_callers,
        provider=provider,
    )
    try:
        wrapped = _wrap_coverage_estimator(render_estimator, coverage)
        result = slice_context(
            store,
            anchors,
            max_tokens=max_tokens,
            include_callee_bodies=include_callee_bodies,
            render_estimator=wrapped,
            target_mode=target_mode,
            dedup_session=dedup_session,
            contracts=contracts,
        )
        result["coverage"] = coverage
        if wrapped is not None:
            result["tokens"] = wrapped(result)
        return result
    finally:
        store.close()


def lazy_slice_outline(
    root: str | Path | None,
    anchors: list[str],
    max_tokens: int = 8000,
    *,
    include_imports: bool = False,
    max_files: int = 40,
    max_hops: int = 2,
    include_callers: bool = True,
    render_estimator=None,
    provider: SourceProvider | None = None,
):
    store, coverage = _populate_lazy_store(
        Path(root) if root is not None else None,
        anchors,
        max_files=max_files,
        max_hops=max_hops,
        include_callers=include_callers,
        provider=provider,
    )
    try:
        wrapped = _wrap_coverage_estimator(render_estimator, coverage)
        result = build_slice_outline(
            store,
            anchors,
            max_tokens=max_tokens,
            include_imports=include_imports,
            render_estimator=wrapped,
        )
        result["coverage"] = coverage
        if wrapped is not None:
            result["tokens"] = wrapped(result)
        return result
    finally:
        store.close()
