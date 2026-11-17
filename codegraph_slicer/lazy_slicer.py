"""Target-seeded lazy slicing without a full repository-wide index."""

from __future__ import annotations

import hashlib
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
from .slicer import build_slice_outline, serialized_tokens, slice_context
from .store import (
    GraphStore,
    _dedupe_analysis_symbols,
    _disambiguate_analysis,
    _iter_workspace_files,
    _read_ts_or_js_config,
    _should_skip_rel_path,
    git_env,
)

_JAVA_ROOT_MARKERS = ("src/main/java/", "src/test/java/", "src/", "app/", "java/")


def _find_repo_files_index(root: Path) -> dict[str, list[str]]:
    """Build a basename/suffix index of source files using git ls-files or rg --files."""
    paths: list[str] = []
    if (root / ".git").exists():
        res = subprocess.run(
            ["git", "-C", str(root), "ls-files", "-z"],
            env=git_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if res.returncode == 0:
            for raw in res.stdout.split(b"\0"):
                if not raw:
                    continue
                p = raw.decode("utf-8", "replace")
                if is_supported_source_path(p) and not _should_skip_rel_path(PurePosixPath(p).parts):
                    paths.append(p)
    if not paths and shutil.which("rg"):
        res = subprocess.run(
            ["rg", "--files", "--hidden", "-g", "!.git"],
            cwd=str(root),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if res.returncode == 0:
            for line in res.stdout.decode("utf-8", "replace").splitlines():
                if is_supported_source_path(line) and not _should_skip_rel_path(PurePosixPath(line).parts):
                    paths.append(line)
    by_suffix: dict[str, list[str]] = {}
    for p in paths:
        by_suffix.setdefault(PurePosixPath(p).name, []).append(p)
    return by_suffix


def _grep_candidate_files(root: Path, needle: str, limit: int = 15) -> list[str]:
    if not needle or len(needle) < 2:
        return []
    rg_bin = shutil.which("rg")
    out: list[str] = []
    if rg_bin:
        cmd = [
            rg_bin,
            "-l",
            "--fixed-strings",
            "--no-messages",
            "--max-count",
            "1",
            needle,
            str(root),
        ]
        res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        if res.returncode in (0, 1):
            for raw in res.stdout.decode("utf-8", "replace").splitlines():
                try:
                    rel = Path(raw).resolve().relative_to(root).as_posix()
                except ValueError:
                    continue
                if is_supported_source_path(rel) and not _should_skip_rel_path(PurePosixPath(rel).parts):
                    out.append(rel)
                    if len(out) >= limit:
                        break
        return out
    if (root / ".git").exists():
        res = subprocess.run(
            ["git", "-C", str(root), "grep", "-l", "-F", needle],
            env=git_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if res.returncode in (0, 1):
            for raw in res.stdout.decode("utf-8", "replace").splitlines():
                if is_supported_source_path(raw) and not _should_skip_rel_path(PurePosixPath(raw).parts):
                    out.append(raw)
                    if len(out) >= limit:
                        break
        return out
    needle_bytes = needle.encode("utf-8")
    for path in _iter_workspace_files(root):
        rel = path.relative_to(root).as_posix()
        if _should_skip_rel_path(PurePosixPath(rel).parts):
            continue
        try:
            if needle_bytes in path.read_bytes():
                out.append(rel)
                if len(out) >= limit:
                    break
        except OSError:
            continue
    return out


def _anchor_seed_info(root: Path, anchor: str) -> tuple[str | None, str]:
    """Return (direct_rel_path_or_None, search_needle) for an anchor string."""
    if ":" in anchor:
        head, tail = anchor.split(":", 1)
        cand = root / head
        if cand.is_file():
            return cand.resolve().relative_to(root).as_posix(), tail.split(".")[-1] if tail and not tail.isdigit() else ""
        # module:qualname or dotted class
        leaf = tail.split(".")[-1] if tail else head.split(".")[-1]
        return None, leaf
    cand = root / anchor
    if cand.is_file():
        return cand.resolve().relative_to(root).as_posix(), PurePosixPath(anchor).stem
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
    root: Path,
    target: str,
    from_file: str,
    java_prefixes: list[str],
    files_by_name: dict[str, list[str]],
) -> list[str]:
    clean = target.split(MODULE_END, 1)[0].split(":", 1)[0].strip()
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
                if (root / cand).is_file():
                    found.append(cand)
                    return found
            leaf_name = sub[-1] + ".java"
            for cand in files_by_name.get(leaf_name, ()):
                if cand.endswith("/" + rel_tail) or cand == rel_tail:
                    found.append(cand)
                    return found
    elif from_file.endswith(".py"):
        rel_mod = clean.replace(".", "/")
        for suffix in (".py", "/__init__.py"):
            cand = rel_mod + suffix
            if (root / cand).is_file():
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
            if (root / cand).is_file():
                found.append(cand)
                return found
    return found


def _populate_lazy_store(
    root: Path,
    anchors: list[str],
    *,
    max_files: int = 40,
    max_hops: int = 2,
    include_callers: bool = True,
) -> tuple[GraphStore, dict]:
    started = monotonic()
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise CodeGraphError(f"workspace root is not a directory: {root}")
    max_files = max(1, max_files)
    max_hops = max(0, max_hops)

    seed_paths: list[str] = []
    direct_seed_paths: set[str] = set()
    needles: list[str] = []
    for anchor in anchors:
        direct_path, needle = _anchor_seed_info(root, anchor)
        if direct_path:
            direct_seed_paths.add(direct_path)
            if direct_path not in seed_paths:
                seed_paths.append(direct_path)
        if needle and needle not in needles:
            needles.append(needle)
        if not direct_path and needle:
            for cand in _grep_candidate_files(root, needle, limit=min(5, max_files)):
                if cand not in seed_paths:
                    seed_paths.append(cand)

    if not seed_paths:
        raise CodeGraphError(f"could not locate any seed files for anchors: {anchors!r}")

    files_by_name = _find_repo_files_index(root) if any(p.endswith(".java") for p in seed_paths) else {}
    java_prefixes = _java_source_prefixes(seed_paths)

    tsconfig_bytes = _read_ts_or_js_config(root)
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
        full = root / rel_p
        if full.is_file() and is_supported_source_path(rel_p) and not _should_skip_rel_path(PurePosixPath(rel_p).parts):
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
        for rel_p in batch:
            explored_raw[rel_p] = (root / rel_p).read_bytes()
        source_root, packages = detect_layout(explored_raw.keys())
        for rel_p in batch:
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
                            root, edge.target, rel_p, java_prefixes, files_by_name
                        ):
                            _enqueue(dep)

    # Bounded candidate caller discovery via grep
    if include_callers and len(explored_raw) < max_files:
        for needle in needles:
            if len(explored_raw) >= max_files:
                truncated = True
                break
            remaining = max_files - len(explored_raw)
            for cand in _grep_candidate_files(root, needle, limit=min(15, remaining)):
                if cand not in explored_raw:
                    explored_raw[cand] = (root / cand).read_bytes()

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
        for key, value in (
            ("snapshot_root", str(root)),
            ("snapshot_kind", "lazy"),
            ("snapshot_revision", ""),
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


def _wrap_coverage_estimator(render_estimator, coverage: dict):
    base = render_estimator or serialized_tokens
    return lambda candidate: base({**candidate, "coverage": coverage})


def lazy_slice_context(
    root: str | Path,
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
):
    store, coverage = _populate_lazy_store(
        Path(root),
        anchors,
        max_files=max_files,
        max_hops=max_hops,
        include_callers=include_callers,
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
        )
        result["coverage"] = coverage
        if wrapped is not None:
            result["tokens"] = wrapped(result)
        return result
    finally:
        store.close()


def lazy_slice_outline(
    root: str | Path,
    anchors: list[str],
    max_tokens: int = 8000,
    *,
    include_imports: bool = False,
    max_files: int = 40,
    max_hops: int = 2,
    include_callers: bool = True,
    render_estimator=None,
):
    store, coverage = _populate_lazy_store(
        Path(root),
        anchors,
        max_files=max_files,
        max_hops=max_hops,
        include_callers=include_callers,
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
