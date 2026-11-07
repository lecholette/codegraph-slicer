"""Transactional index, retaining raw references for rebinding across snapshots."""

from collections import defaultdict
import contextlib
import hashlib
import importlib.metadata
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path, PurePosixPath
from time import monotonic
from urllib.parse import quote
from .ast_extractor import (
    MODULE_END,
    SUPPORTED_EXTENSIONS,
    TS_JS_EXTENSIONS,
    CodeGraphError,
    CodeGraphParseError,
    Edge,
    FileAnalysis,
    Symbol,
    compute_c3_mro,
    detect_layout,
    extract_file,
    is_supported_source_path,
    is_test_path,
    module_name,
    plain_target,
    valid_path,
)

_RAW_SOURCE_EXTS = tuple(ext.encode("ascii") for ext in SUPPORTED_EXTENSIONS)
_RAW_EXCLUDED_EXTS = (b".d.ts", b".min.js", b".bundle.js")

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
    "build",
    ".gradle",
    ".next",
}


def _should_skip_rel_path(rel_parts: tuple[str, ...], include_hidden: bool = False) -> bool:
    if not rel_parts:
        return False
    if any(
        part in _SKIP_DIRS or (not include_hidden and part.startswith("."))
        for part in rel_parts[:-1]
    ):
        return True
    if not include_hidden and rel_parts[-1].startswith("."):
        return True
    return False


def _lang_group(path: str) -> str:
    if path.endswith(".py"):
        return "py"
    if path.endswith(".java"):
        return "java"
    if path.endswith(TS_JS_EXTENSIONS):
        return "ts"
    return ""


def _raw_stem_module(path: str, source_root: str) -> str:
    if path.endswith(".py"):
        return module_name(path, source_root)
    if path.endswith(TS_JS_EXTENSIONS):
        from .ts_extractor import ts_module_name

        return ts_module_name(path, source_root)
    p = PurePosixPath(path).with_suffix("").as_posix()
    for pfx in ("src/main/java/", "src/test/java/", "src/", "app/", "java/"):
        if p.startswith(pfx):
            p = p[len(pfx) :]
            break
    return p.replace("/", ".")


def _disambiguate_analysis(
    analysis: FileAnalysis, taken_modules: set[str]
) -> FileAnalysis:
    old_mod = analysis.module
    if old_mod not in taken_modules:
        taken_modules.add(old_mod)
        return analysis
    ext_tag = PurePosixPath(analysis.path).suffix.lstrip(".")
    candidate = f"{old_mod}_{ext_tag}" if ext_tag else f"{old_mod}_2"
    if candidate in taken_modules:
        path_mod = PurePosixPath(analysis.path).with_suffix("").as_posix().replace("/", ".")
        candidate = f"{path_mod}_{ext_tag}" if path_mod != old_mod else f"{old_mod}_{len(taken_modules)}"
    idx = 2
    base_cand = candidate
    while candidate in taken_modules:
        candidate = f"{base_cand}_{idx}"
        idx += 1
    taken_modules.add(candidate)

    def _rewrite(ref: str) -> str:
        if ref == old_mod:
            return candidate
        for sep in (":", ".", MODULE_END):
            prefix = f"{old_mod}{sep}"
            if ref.startswith(prefix):
                return f"{candidate}{sep}{ref[len(prefix):]}"
        return ref

    new_symbols = tuple(
        Symbol(
            _rewrite(s.symbol_id),
            s.kind,
            candidate if s.kind == "module" else s.signature,
            s.body,
            s.start,
            s.end,
            s.cx,
        )
        for s in analysis.symbols
    )
    new_edges = tuple(
        Edge(_rewrite(e.source), _rewrite(e.target), e.kind, e.line)
        for e in analysis.edges
    )
    return FileAnalysis(
        analysis.path,
        candidate,
        analysis.blob_sha,
        analysis.skeleton,
        new_symbols,
        new_edges,
    )


def _iter_workspace_files(root: Path) -> list[Path]:
    found: set[Path] = set()
    for ext in SUPPORTED_EXTENSIONS:
        for path in root.rglob(f"*{ext}"):
            if is_supported_source_path(path.name):
                found.add(path)
    return sorted(found)


def _read_ts_or_js_config(root: Path) -> bytes:
    for cfg_name in ("tsconfig.json", "jsconfig.json"):
        cfg_path = root / cfg_name
        if cfg_path.is_file() and not cfg_path.is_symlink():
            return cfg_path.read_bytes()
    return b""


class InvalidRevisionError(CodeGraphError):
    pass


class StaleSnapshotError(CodeGraphError):
    pass


def git_env() -> dict[str, str]:
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    env.update(
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
    )
    return env


def _arity_ok(req: tuple[int, int | None], cand: tuple[int, int | None]) -> bool:
    r_min, _ = req
    c_min, c_max = cand
    return c_min <= r_min and (c_max is None or c_max >= r_min)


def _satisfies_protocol(
    csid: str,
    reqs: dict[str, tuple[int, int | None]],
    method_arities: dict[str, tuple[int, int | None]],
) -> bool:
    return all(
        f"{csid}.{m}" in method_arities
        and _arity_ok(req_a, method_arities[f"{csid}.{m}"])
        for m, req_a in reqs.items()
    )


class GraphStore:
    def __init__(self, path, *, readonly=False):
        if readonly:
            uri = "file:" + quote(str(Path(path).resolve()), safe="/") + "?mode=ro"
            self.db = sqlite3.connect(uri, uri=True)
        else:
            self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA trusted_schema=OFF")
        if readonly:
            self.db.execute("PRAGMA query_only=ON")
            self.db.execute("SELECT path FROM files LIMIT 0")
        else:
            self.db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS files(
                    path TEXT PRIMARY KEY, module TEXT NOT NULL, blob_sha TEXT NOT NULL,
                    loc INTEGER NOT NULL, skeleton TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS symbols(
                    symbol_id TEXT PRIMARY KEY, file_path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
                    module TEXT NOT NULL, qualname TEXT NOT NULL, kind TEXT NOT NULL,
                    start_line INTEGER NOT NULL, end_line INTEGER NOT NULL, signature TEXT NOT NULL,
                    body TEXT NOT NULL, cx INTEGER NOT NULL, token_est INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS raw_edges(
                    source_file TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
                    source_id TEXT NOT NULL, target_id TEXT NOT NULL, kind TEXT NOT NULL, line INTEGER NOT NULL,
                    PRIMARY KEY(source_file,source_id,target_id,kind,line));
                CREATE TABLE IF NOT EXISTS edges(
                    source_file TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
                    source_id TEXT NOT NULL, target_id TEXT NOT NULL, kind TEXT NOT NULL, line INTEGER NOT NULL,
                    PRIMARY KEY(source_file,source_id,target_id,kind,line));
                CREATE TABLE IF NOT EXISTS diagnostics(
                    source_file TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
                    source_id TEXT NOT NULL, target TEXT NOT NULL, kind TEXT NOT NULL,
                    line INTEGER NOT NULL, reason TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS ei ON edges(source_id,kind);
                CREATE INDEX IF NOT EXISTS et ON edges(target_id,kind);
                CREATE INDEX IF NOT EXISTS sm ON symbols(module);
                CREATE INDEX IF NOT EXISTS sq ON symbols(qualname);
            """)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self.db.close()

    @contextlib.contextmanager
    def read_snapshot(self):
        if self.db.in_transaction:
            yield
            return
        self.db.execute("BEGIN DEFERRED")
        try:
            self.db.execute("SELECT 1 FROM metadata LIMIT 1").fetchall()
            yield
        finally:
            if self.db.in_transaction:
                self.db.rollback()

    def sync_workspace(self, root, *, include_hidden: bool = False, skip_unparseable: bool = False):
        root = Path(root).resolve(strict=True)
        if not root.is_dir():
            raise CodeGraphError(f"workspace root is not a directory: {root}")
        if not include_hidden and os.environ.get("CODEGRAPH_INCLUDE_HIDDEN") == "1":
            include_hidden = True
        files = {}
        for path in _iter_workspace_files(root):
            rel = path.relative_to(root)
            if _should_skip_rel_path(rel.parts, include_hidden):
                continue
            if path.is_symlink() or any(
                parent.is_symlink()
                for parent in path.parents
                if parent != root and root in parent.parents
            ):
                raise CodeGraphError(f"symlink source refused: {rel.as_posix()}")
            name = valid_path(rel.as_posix())
            data = path.read_bytes()
            files[name] = ("sha256:" + hashlib.sha256(data).hexdigest(), data)
        tsconfig_bytes = _read_ts_or_js_config(root)
        return self._sync(
            files,
            snapshot_root=str(root),
            snapshot_kind="workspace",
            tsconfig_bytes=tsconfig_bytes,
            include_hidden=include_hidden,
            skip_unparseable=skip_unparseable,
        )

    def sync_git_tree(self, root, revision, *, include_hidden: bool = False, skip_unparseable: bool = False):
        root = Path(root).resolve(strict=True)
        if not include_hidden and os.environ.get("CODEGRAPH_INCLUDE_HIDDEN") == "1":
            include_hidden = True
        if (
            not revision
            or revision.startswith("-")
            or any(ord(c) < 32 for c in revision)
        ):
            raise InvalidRevisionError("invalid revision")

        def git(*args):
            result = subprocess.run(
                ["git", "-C", str(root), *args],
                env=git_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if result.returncode:
                raise InvalidRevisionError(
                    result.stderr.decode("utf8", "replace").strip()
                )
            return result.stdout

        rev = git("rev-parse", "--verify", revision + "^{commit}").decode().strip()
        files = {}
        tsconfig_oid = None
        jsconfig_oid = None
        for entry in git("ls-tree", "-r", "-z", rev).split(b"\0"):
            if not entry:
                continue
            meta, raw_path = entry.split(b"\t", 1)
            mode, typ, oid = meta.split()
            if raw_path == b"tsconfig.json" and typ == b"blob":
                tsconfig_oid = oid.decode("ascii")
                continue
            if raw_path == b"jsconfig.json" and typ == b"blob":
                jsconfig_oid = oid.decode("ascii")
                continue
            if not raw_path.endswith(_RAW_SOURCE_EXTS) or raw_path.endswith(_RAW_EXCLUDED_EXTS):
                continue
            if typ != b"blob" or mode not in {b"100644", b"100755"}:
                raise CodeGraphError(f"non-regular Python source refused: {raw_path!r}")
            try:
                name = valid_path(raw_path.decode("utf8"))
            except UnicodeError as exc:
                raise CodeGraphError(f"non-UTF8 Python path: {raw_path!r}") from exc
            if _should_skip_rel_path(PurePosixPath(name).parts, include_hidden):
                continue
            files[name] = ("git:" + oid.decode("ascii"), oid.decode("ascii"))
        cfg_oid = tsconfig_oid or jsconfig_oid
        tsconfig_bytes = git("cat-file", "blob", cfg_oid) if cfg_oid else b""
        return self._sync(
            files,
            loader=lambda oid: git("cat-file", "blob", oid),
            snapshot_root=str(root),
            snapshot_kind="git",
            snapshot_revision=rev,
            tsconfig_bytes=tsconfig_bytes,
            include_hidden=include_hidden,
            skip_unparseable=skip_unparseable,
        )

    def _sync(
        self,
        files,
        loader=None,
        *,
        snapshot_root,
        snapshot_kind,
        snapshot_revision="",
        tsconfig_bytes=b"",
        include_hidden=False,
        skip_unparseable=False,
    ):
        started = monotonic()
        base_digest = hashlib.sha256()
        for source_name in (
            "ast_extractor.py",
            "treesitter_support.py",
            "java_extractor.py",
            "ts_extractor.py",
            "store.py",
        ):
            source_path = Path(__file__).with_name(source_name)
            if source_path.exists():
                base_digest.update(source_path.read_bytes())
                base_digest.update(b"\0")
        base_digest.update(f"{sys.version_info.major}.{sys.version_info.minor}".encode())
        for pkg_name in ("tree-sitter", "tree-sitter-language-pack"):
            try:
                ver = importlib.metadata.version(pkg_name)
            except importlib.metadata.PackageNotFoundError:
                ver = ""
            base_digest.update(f"\0{pkg_name}:{ver}".encode())
        tsconfig_sha = (
            "sha256:" + hashlib.sha256(tsconfig_bytes).hexdigest()
            if tsconfig_bytes
            else ""
        )
        path_aliases = None
        if tsconfig_bytes:
            from .ts_extractor import parse_tsconfig_paths

            path_aliases = parse_tsconfig_paths(tsconfig_bytes)
        raw_cache = {}

        def get_raw(p):
            if p not in raw_cache:
                raw_cache[p] = loader(files[p][1]) if loader else files[p][1]
            return raw_cache[p]

        self.db.execute("BEGIN IMMEDIATE")
        with self.db:
            old_rows = list(self.db.execute("SELECT path,blob_sha,module FROM files"))
            old = {row["path"]: row["blob_sha"] for row in old_rows}
            old_modules = {row["path"]: row["module"] for row in old_rows}
            skipped = {}
            if skip_unparseable:
                for path, (identity, _) in sorted(files.items()):
                    if path.endswith("__init__.py") and old.get(path) != identity:
                        try:
                            extract_file(path, get_raw(path))
                        except CodeGraphParseError as exc:
                            skipped[path] = identity
                            sys.stderr.write(
                                f"codegraph-slicer: warning: skipping unparseable file {path}: {exc}\n"
                            )
            source_root, packages = detect_layout(p for p in files if p not in skipped)
            digest = base_digest.copy()
            # A layout change renames modules, so every file is re-extracted.
            digest.update(f"\0{source_root}\0{','.join(sorted(packages))}".encode())
            if tsconfig_bytes:
                digest.update(b"\0")
                digest.update(tsconfig_bytes)
            engine_sha = digest.hexdigest()
            prior_engine = self.db.execute(
                "SELECT value FROM metadata WHERE key='engine_sha'"
            ).fetchone()
            engine_matches = prior_engine is not None and prior_engine[0] == engine_sha
            deleted = set(old) - set(files)
            changed_set = {
                path
                for path, (identity, _) in files.items()
                if path not in skipped and (not engine_matches or old.get(path) != identity)
            }
            if changed_set or deleted:
                stem_groups: dict[str, set[str]] = defaultdict(set)
                for p in (set(files) - set(skipped)) | set(old):
                    stem_groups[_raw_stem_module(p, source_root)].add(p)
                affected_stems = {
                    _raw_stem_module(p, source_root) for p in (changed_set | deleted)
                }
                for p, mod in old_modules.items():
                    raw_m = _raw_stem_module(p, source_root)
                    if mod != raw_m:
                        affected_stems.add(raw_m)
                        affected_stems.add(mod)
                for stem in affected_stems:
                    for p in stem_groups.get(stem, ()):
                        if p in files and p not in skipped:
                            changed_set.add(p)
            changed = sorted(changed_set)
            analyses = []
            for path in changed:
                try:
                    analyses.append(
                        extract_file(
                            path,
                            get_raw(path),
                            packages,
                            source_root,
                            *( [path_aliases] if path_aliases else [] ),
                        )
                    )
                except CodeGraphParseError as exc:
                    if not skip_unparseable:
                        raise
                    skipped[path] = files[path][0]
                    sys.stderr.write(
                        f"codegraph-slicer: warning: skipping unparseable file {path}: {exc}\n"
                    )
            removed = deleted | (set(old) & set(skipped))
            for path in sorted(removed | set(changed)):
                self.db.execute("DELETE FROM files WHERE path=?", (path,))
            taken_modules = {
                row[0] for row in self.db.execute("SELECT module FROM files")
            }
            for raw_analysis in analyses:
                analysis = _disambiguate_analysis(raw_analysis, taken_modules)
                self.db.execute(
                    "INSERT INTO files VALUES(?,?,?,?,?)",
                    (
                        analysis.path,
                        analysis.module,
                        files[analysis.path][0],
                        analysis.symbols[0].end,
                        analysis.skeleton,
                    ),
                )
                for symbol in analysis.symbols:
                    self.db.execute(
                        "INSERT INTO symbols VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            symbol.symbol_id,
                            analysis.path,
                            analysis.module,
                            symbol.symbol_id.split(":", 1)[1],
                            symbol.kind,
                            symbol.start,
                            symbol.end,
                            symbol.signature,
                            symbol.body,
                            symbol.cx,
                            len(symbol.body) // 4 + 1,
                        ),
                    )
                self.db.executemany(
                    "INSERT INTO raw_edges VALUES(?,?,?,?,?)",
                    [
                        (analysis.path, e.source, e.target, e.kind, e.line)
                        for e in analysis.edges
                    ],
                )
            if changed or removed:
                self._rebind(source_root=source_root)
            self.db.execute(
                "INSERT INTO metadata(key,value) VALUES('engine_sha',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (engine_sha,),
            )
            for key, value in (
                ("snapshot_root", snapshot_root),
                ("snapshot_kind", snapshot_kind),
                ("snapshot_revision", snapshot_revision),
                ("source_root", source_root),
                ("packages", ",".join(sorted(packages))),
                ("include_hidden", "1" if include_hidden else "0"),
                ("tsconfig_sha", tsconfig_sha),
                ("skipped_files", json.dumps(skipped, sort_keys=True)),
            ):
                self.db.execute(
                    "INSERT INTO metadata(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            res = {
                "parsed_files": len(analyses),
                "cached_files": len(files) - len(analyses) - len(skipped),
                "deleted_files": len(deleted),
                "total_symbols": self.db.execute(
                    "SELECT count(*) FROM symbols"
                ).fetchone()[0],
                "total_edges": self.db.execute("SELECT count(*) FROM edges").fetchone()[0],
                "unresolved_references": self.db.execute(
                    "SELECT count(*) FROM diagnostics"
                ).fetchone()[0],
                "source_root": source_root,
                "packages": sorted(packages),
                "elapsed_ms": round((monotonic() - started) * 1000, 3),
            }
            if skip_unparseable:
                res["skipped_unparseable"] = sorted(skipped)
            return res

    def assert_fresh(self):
        """Refuse ordinary queries unless the indexed Python source is current."""
        metadata = {
            row["key"]: row["value"]
            for row in self.db.execute(
                "SELECT key,value FROM metadata WHERE key IN ('snapshot_root','snapshot_kind','snapshot_revision','include_hidden','tsconfig_sha','skipped_files')"
            )
        }
        root_text = metadata.get("snapshot_root")
        kind = metadata.get("snapshot_kind")
        include_hidden = metadata.get("include_hidden") == "1"
        if not root_text or kind not in {"workspace", "git"}:
            raise StaleSnapshotError("snapshot freshness cannot be verified: missing source metadata; run codegraph-slicer sync ROOT --db DB")
        try:
            root = Path(root_text).resolve(strict=True)
            cfg_bytes = _read_ts_or_js_config(root)
            actual_tsconfig_sha = (
                "sha256:" + hashlib.sha256(cfg_bytes).hexdigest()
                if cfg_bytes
                else ""
            )
            if "tsconfig_sha" in metadata and actual_tsconfig_sha != metadata.get("tsconfig_sha", ""):
                raise StaleSnapshotError(
                    "snapshot is stale: Python source differs at tsconfig.json; run codegraph-slicer sync ROOT --db DB"
                )
            if kind == "workspace":
                actual = {}
                for path in _iter_workspace_files(root):
                    rel = path.relative_to(root)
                    if _should_skip_rel_path(rel.parts, include_hidden):
                        continue
                    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root and root in parent.parents):
                        raise StaleSnapshotError(f"snapshot is stale: symlink source refused: {rel.as_posix()}")
                    name = valid_path(rel.as_posix())
                    actual[name] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
            else:
                def git(*args, input=None):
                    result = subprocess.run(["git", "-C", str(root), *args], env=git_env(), input=input, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
                    if result.returncode:
                        raise StaleSnapshotError("snapshot freshness cannot be verified: " + result.stderr.decode("utf8", "replace").strip())
                    return result.stdout

                head = git("rev-parse", "--verify", "HEAD^{commit}").decode().strip()
                if head != metadata.get("snapshot_revision"):
                    raise StaleSnapshotError("snapshot is stale: repository HEAD moved; run codegraph-slicer sync ROOT --revision HEAD --db DB")
                object_format = git("rev-parse", "--show-object-format").decode("ascii").strip()
                hash_function = {"sha1": hashlib.sha1, "sha256": hashlib.sha256}.get(object_format)
                if hash_function is None:
                    raise StaleSnapshotError(f"snapshot freshness cannot be verified: unsupported Git object format {object_format!r}")
                actual = {}
                for entry in git("ls-tree", "-r", "-z", head).split(b"\0"):
                    if not entry:
                        continue
                    meta, raw_path = entry.split(b"\t", 1)
                    mode, typ, oid = meta.split()
                    if not raw_path.endswith(_RAW_SOURCE_EXTS) or raw_path.endswith(_RAW_EXCLUDED_EXTS):
                        continue
                    if typ != b"blob" or mode not in {b"100644", b"100755"}:
                        raise StaleSnapshotError(f"snapshot source cannot be verified: non-regular Python source {raw_path!r}")
                    name = valid_path(raw_path.decode("utf8"))
                    if _should_skip_rel_path(PurePosixPath(name).parts, include_hidden):
                        continue
                    actual[name] = "git:" + oid.decode("ascii")
                working = {}
                for path in _iter_workspace_files(root):
                    rel = path.relative_to(root)
                    if ".git" in rel.parts:
                        continue
                    if _should_skip_rel_path(rel.parts, include_hidden) and rel.as_posix() not in actual:
                        continue
                    if path.is_symlink() or any(parent.is_symlink() for parent in path.parents if parent != root and root in parent.parents):
                        raise StaleSnapshotError(f"snapshot freshness cannot be verified: symlink source refused: {rel.as_posix()}")
                    name = valid_path(rel.as_posix())
                    data = path.read_bytes()
                    oid = hash_function(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
                    working[name] = "git:" + oid
                if working != actual:
                    changed = sorted(set(working) ^ set(actual) or {path for path in working if working.get(path) != actual.get(path)})
                    detail = changed[0] if changed else "Python file set"
                    raise StaleSnapshotError(f"snapshot is stale: Python source differs at {detail}; run codegraph-slicer sync ROOT --db DB to index the current working tree")
        except StaleSnapshotError:
            raise
        except (OSError, UnicodeError, ValueError) as exc:
            raise StaleSnapshotError(f"snapshot freshness cannot be verified: {exc}; run codegraph-slicer sync ROOT --db DB") from exc
        indexed = {row["path"]: row["blob_sha"] for row in self.db.execute("SELECT path,blob_sha FROM files")}
        if metadata.get("skipped_files"):
            indexed.update(json.loads(metadata["skipped_files"]))
        if actual != indexed:
            changed = sorted(set(actual) ^ set(indexed) or {path for path in actual if actual.get(path) != indexed.get(path)})
            detail = changed[0] if changed else "Python file set"
            raise StaleSnapshotError(f"snapshot is stale: Python source differs at {detail}; run codegraph-slicer sync ROOT --db DB")

    def _rebind(self, source_root: str = ""):
        symbols = {
            row["symbol_id"]: row["kind"]
            for row in self.db.execute("SELECT symbol_id,kind FROM symbols")
        }
        file_rows = self.db.execute("SELECT path,module FROM files").fetchall()
        modules = {row["module"] for row in file_rows}
        file_lang = {row["path"]: _lang_group(row["path"]) for row in file_rows}
        ts_modules = {row["module"] for row in file_rows if file_lang.get(row["path"]) == "ts"}
        lang_mod_map: dict[tuple[str, str], str] = {}
        for row in file_rows:
            raw_mod = _raw_stem_module(row["path"], source_root)
            if row["module"] != raw_mod:
                lang_mod_map[(_lang_group(row["path"]), raw_mod)] = row["module"]

        def _rewrite_lang_target(t: str, lg: str) -> str:
            if not lang_mod_map or t.startswith("?"):
                return t
            if MODULE_END in t:
                mod_part, sep, rest = t.partition(MODULE_END)
                mapped = lang_mod_map.get((lg, mod_part))
                if mapped:
                    return f"{mapped}{sep}{rest}"
                return t
            if ":" in t:
                mod_part, sep, rest = t.partition(":")
                mapped = lang_mod_map.get((lg, mod_part))
                if mapped:
                    return f"{mapped}{sep}{rest}"
                return t
            parts = t.split(".")
            for idx in range(len(parts), 0, -1):
                prefix = ".".join(parts[:idx])
                mapped = lang_mod_map.get((lg, prefix))
                if mapped:
                    return ".".join([mapped, *parts[idx:]])
            return t

        # Module-level import bindings: "pkg.Name" -> dotted import target.
        aliases = {
            row["source_id"]: _rewrite_lang_target(
                row["target_id"], file_lang.get(row["source_file"], "py")
            )
            for row in self.db.execute(
                "SELECT source_file,source_id,target_id FROM raw_edges WHERE kind='ALIAS'"
            )
        }
        wildcard_exports = defaultdict(list)
        module_exports: dict[str, set[str]] = {}
        for row in self.db.execute(
            "SELECT source_id,target_id FROM raw_edges WHERE kind='MODULE_EXPORTS'"
        ):
            module_exports[row["source_id"]] = (
                set(row["target_id"].split(",")) if row["target_id"] else set()
            )
        for row in self.db.execute(
            "SELECT source_file,source_id,target_id FROM raw_edges WHERE kind='WILDCARD_EXPORT'"
        ):
            wildcard_exports[row["source_id"]].append(
                _rewrite_lang_target(
                    row["target_id"], file_lang.get(row["source_file"], "py")
                )
            )
        if wildcard_exports:
            changed_wc = True
            while changed_wc:
                changed_wc = False
                for src_mod, tgt_mods in wildcard_exports.items():
                    for tgt_mod in tgt_mods:
                        prefix = tgt_mod + "."
                        for key, val in list(aliases.items()):
                            if key.startswith(prefix):
                                exported = key[len(prefix) :]
                                if "." not in exported and f"{src_mod}.{exported}" not in aliases:
                                    aliases[f"{src_mod}.{exported}"] = val
                                    if src_mod in module_exports:
                                        module_exports[src_mod].add(exported)
                                    changed_wc = True
        wildcard_imports = defaultdict(list)
        for row in self.db.execute(
            "SELECT source_file,source_id,target_id FROM raw_edges WHERE kind='WILDCARD_IMPORT'"
        ):
            wildcard_imports[row["source_id"]].append(
                _rewrite_lang_target(
                    row["target_id"], file_lang.get(row["source_file"], "py")
                )
            )
        for src_mod, tgt_pkgs in wildcard_imports.items():
            for tgt_pkg in tgt_pkgs:
                prefix = tgt_pkg + "."
                for key, val in list(aliases.items()):
                    if key.startswith(prefix):
                        member = key[len(prefix) :]
                        if "." not in member and f"{src_mod}.{member}" not in aliases:
                            aliases[f"{src_mod}.{member}"] = val
                class_sid = aliases.get(tgt_pkg)
                if class_sid and class_sid in symbols:
                    cprefix = class_sid + "."
                    for sym_id in symbols:
                        if sym_id.startswith(cprefix):
                            member = sym_id[len(cprefix) :]
                            if "." not in member and f"{src_mod}.{member}" not in aliases:
                                aliases[f"{src_mod}.{member}"] = sym_id

        def direct(target, module_parts=1):
            if target in symbols:
                return target
            if target.startswith("?"):
                return None
            if target in modules:
                return target + ":"
            parts = target.split(".")
            # An import's module path names a module, never a package attribute.
            for index in range(len(parts) - 1, max(module_parts, 1) - 1, -1):
                mod_name = ".".join(parts[:index])
                qual = ".".join(parts[index:])
                if mod_name in module_exports and qual.split(".", 1)[0] not in module_exports[mod_name]:
                    continue
                candidate = f"{mod_name}:{qual}"
                if candidate in symbols:
                    return candidate
            return None

        def resolve(target):
            applied = set()  # an alias applied twice is a cycle, even if the target grows
            while True:
                # The import system resolves the module path; after it, follow the
                # attribute chain left to right, as Python does: the first prefix bound
                # in its module decides, and the final binding wins over a shadowed
                # definition.
                module_parts = len(target.partition(MODULE_END)[0].split(".")) if MODULE_END in target else 0
                target = plain_target(target)
                parts = target.split(".")
                for index in range(module_parts + 1, len(parts) + 1):
                    prefix = ".".join(parts[:index])
                    bound = aliases.get(prefix)
                    if bound is None or plain_target(bound) == prefix:  # `from . import sub`
                        continue
                    if prefix in applied:
                        return None
                    applied.add(prefix)
                    target = ".".join([bound, *parts[index:]])
                    break
                else:
                    found = direct(target, module_parts)
                    if found is not None:
                        return found
                    if module_parts >= 1 and len(parts) > module_parts and parts[module_parts].startswith("__cjs__"):
                        mod_prefix = ".".join(parts[:module_parts])
                        rest = [parts[module_parts][len("__cjs__") :], *parts[module_parts + 1 :]]
                        target = ".".join([mod_prefix, *rest])
                        continue
                    return None

        _META_KINDS = (
            "ALIAS",
            "WILDCARD_EXPORT",
            "WILDCARD_IMPORT",
            "MODULE_EXPORTS",
            "RETURN_TYPE",
            "FIELD_TYPE",
            "PROTOCOL_METHOD",
            "METHOD_ARITY",
            "ABC_METHOD",
            "DEFERRED_CALL",
        )
        self.db.execute("DELETE FROM edges")
        self.db.execute("DELETE FROM diagnostics")
        unresolved_rows = []
        for row in self.db.execute(
            f"SELECT * FROM raw_edges WHERE kind NOT IN ({','.join('?' for _ in _META_KINDS)}) "
            "ORDER BY source_file,source_id,target_id,kind,line",
            _META_KINDS,
        ).fetchall():
            raw_target = _rewrite_lang_target(
                row["target_id"], file_lang.get(row["source_file"], "py")
            )
            target = resolve(raw_target)
            if target is None:
                unresolved_rows.append((row, raw_target))
            else:
                self.db.execute(
                    "INSERT OR IGNORE INTO edges VALUES(?,?,?,?,?)",
                    (
                        row["source_file"],
                        row["source_id"],
                        target,
                        row["kind"],
                        row["line"],
                    ),
                )
        self._resolve_cross_file_calls(
            symbols, file_lang, _rewrite_lang_target, resolve, unresolved_rows
        )
        for row in self.db.execute(
            "SELECT symbol_id,file_path,module,qualname FROM symbols WHERE kind!='module'"
        ).fetchall():
            qualname = row["qualname"]
            parent = row["module"] + ":" + qualname.rpartition(".")[0]
            self.db.execute(
                "INSERT OR IGNORE INTO edges VALUES(?,?,?,?,?)",
                (row["file_path"], parent, row["symbol_id"], "DEFINES", 0),
            )

    def _load_type_facts(self, file_lang, rewrite_target, resolve):
        class_bases_list: dict[str, list[str]] = defaultdict(list)
        for r in self.db.execute(
            "SELECT source_id,target_id FROM edges WHERE kind='INHERITS' ORDER BY line"
        ):
            if r["target_id"] not in class_bases_list[r["source_id"]]:
                class_bases_list[r["source_id"]].append(r["target_id"])
        class_bases = {k: tuple(v) for k, v in class_bases_list.items()}

        return_types: dict[str, list[str]] = defaultdict(list)
        for r in self.db.execute(
            "SELECT source_file,source_id,target_id FROM raw_edges WHERE kind='RETURN_TYPE'"
        ):
            rt = resolve(rewrite_target(r["target_id"], file_lang.get(r["source_file"], "py")))
            if rt and rt not in return_types[r["source_id"]]:
                return_types[r["source_id"]].append(rt)

        field_types: dict[tuple[str, str], list[str]] = defaultdict(list)
        for r in self.db.execute(
            "SELECT source_file,source_id,target_id FROM raw_edges WHERE kind='FIELD_TYPE'"
        ):
            cls_sid, _, fname = r["source_id"].rpartition(".")
            ft = resolve(rewrite_target(r["target_id"], file_lang.get(r["source_file"], "py")))
            if cls_sid and fname and ft and ft not in field_types[(cls_sid, fname)]:
                field_types[(cls_sid, fname)].append(ft)
        return class_bases, return_types, field_types

    def _emit_resolved_call(self, src_file: str, src_id: str, tgt: str, line: int):
        self.db.execute(
            "INSERT OR IGNORE INTO edges VALUES(?,?,?,?,?)",
            (src_file, src_id, tgt, "CALLS", line),
        )
        if is_test_path(src_file):
            self.db.execute(
                "INSERT OR IGNORE INTO edges VALUES(?,?,?,?,?)",
                (src_file, src_id, tgt, "TESTS", line),
            )

    def _emit_diagnostic(self, src_file: str, src_id: str, tgt: str, kind: str, line: int):
        self.db.execute(
            "INSERT INTO diagnostics VALUES(?,?,?,?,?,?)",
            (
                src_file,
                src_id,
                tgt,
                kind,
                line,
                "external, dynamic, shadowed, or unobserved reference",
            ),
        )

    def _resolve_unresolved_call(
        self, row, raw_target: str, symbols, return_types, resolve, mro_method
    ) -> bool:
        if row["kind"] != "CALLS" or "." not in raw_target or raw_target.startswith("?"):
            return False
        prefix, _, mname = raw_target.rpartition(".")
        owner_sid = resolve(prefix)
        candidates = (
            [owner_sid]
            if owner_sid in symbols and symbols[owner_sid] in ("class", "interface")
            else return_types.get(owner_sid or "", [])
        )
        resolved = [mt for c in candidates if (mt := mro_method(c, mname))]
        for mt in resolved:
            self._emit_resolved_call(row["source_file"], row["source_id"], mt, row["line"])
        return bool(resolved)

    def _resolve_deferred_targets(
        self, parts: list[str], lg: str, class_bases, return_types, field_types, rewrite_target, resolve, mro_method
    ) -> list[str]:
        if parts[0] == "RET" and len(parts) >= 3:
            fn_sid = resolve(rewrite_target(parts[1], lg))
            return [mt for rt in return_types.get(fn_sid or "", ()) if (mt := mro_method(rt, parts[2]))]
        if parts[0] == "FLD" and len(parts) >= 4:
            cls_sid = resolve(rewrite_target(parts[1], lg))
            if not cls_sid:
                return []
            fname, mname = parts[2], parts[3]
            out = []
            for base in compute_c3_mro(cls_sid, class_bases):
                for ft_sid in field_types.get((base, fname), ()):
                    for cand_cls in return_types.get(ft_sid, [ft_sid]):
                        if mt := mro_method(cand_cls, mname):
                            out.append(mt)
            return out
        return []

    def _resolve_cross_file_calls(
        self, symbols, file_lang, rewrite_target, resolve, unresolved_rows
    ):
        class_bases, return_types, field_types = self._load_type_facts(
            file_lang, rewrite_target, resolve
        )

        def mro_method(cls_sid: str, mname: str) -> str | None:
            return next(
                (f"{b}.{mname}" for b in compute_c3_mro(cls_sid, class_bases) if f"{b}.{mname}" in symbols),
                None,
            )

        for row, raw_target in unresolved_rows:
            if not self._resolve_unresolved_call(
                row, raw_target, symbols, return_types, resolve, mro_method
            ):
                self._emit_diagnostic(
                    row["source_file"],
                    row["source_id"],
                    plain_target(raw_target).replace(".__cjs__", "."),
                    row["kind"],
                    row["line"],
                )

        for row in self.db.execute(
            "SELECT * FROM raw_edges WHERE kind='DEFERRED_CALL' ORDER BY source_file,source_id,line"
        ).fetchall():
            lg = file_lang.get(row["source_file"], "py")
            parts = row["target_id"].split("\t")
            targets = self._resolve_deferred_targets(
                parts, lg, class_bases, return_types, field_types, rewrite_target, resolve, mro_method
            )
            for mt in targets:
                self._emit_resolved_call(row["source_file"], row["source_id"], mt, row["line"])
            if not targets and len(parts) >= 4 and parts[-1].startswith("?"):
                self._emit_diagnostic(row["source_file"], row["source_id"], parts[-1], "CALLS", row["line"])

        self._fanout_subtype_calls(symbols, class_bases, mro_method)

    def _read_protocol_and_abc_contracts(self, symbols):
        method_arities = {
            r["source_id"]: (int(p[0]), int(p[1]) if p[1] else None)
            for r in self.db.execute("SELECT source_id,target_id FROM raw_edges WHERE kind='METHOD_ARITY'")
            if len(p := r["target_id"].split("\t")) == 2
        }
        protocol_reqs: dict[str, dict[str, tuple[int, int | None]]] = defaultdict(dict)
        for r in self.db.execute("SELECT source_id,target_id FROM raw_edges WHERE kind='PROTOCOL_METHOD'"):
            p = r["target_id"].split("\t")
            protocol_reqs[r["source_id"]][p[0]] = (
                (int(p[1]), int(p[2]) if p[2] else None) if len(p) == 3 else (0, None)
            )
        abc_sids = {
            r["source_id"] for r in self.db.execute("SELECT source_id FROM raw_edges WHERE kind='ABC_METHOD'")
        } | {sid for sid, k in symbols.items() if k == "interface"}
        return method_arities, protocol_reqs, abc_sids

    def _load_subtype_impls(self, symbols, class_bases) -> dict[str, list[str]]:
        method_arities, protocol_reqs, abc_sids = self._read_protocol_and_abc_contracts(symbols)
        if not abc_sids and not protocol_reqs:
            return {}
        sym_lang = {
            r["symbol_id"]: _lang_group(r["file_path"])
            for r in self.db.execute("SELECT symbol_id,file_path FROM symbols WHERE kind IN ('class','interface')")
        }
        concrete = sorted(
            sid for sid, k in symbols.items() if k == "class" and sid not in abc_sids and sid not in protocol_reqs
        )
        mro_cache = {csid: set(compute_c3_mro(csid, class_bases)) for csid in concrete}
        impls_of = {
            isid: [c for c in concrete if sym_lang.get(c) == sym_lang.get(isid, "py") and isid in mro_cache[c]]
            for isid in abc_sids
        }
        for psid, reqs in protocol_reqs.items():
            lg = sym_lang.get(psid, "py")
            impls_of[psid] = [
                c
                for c in concrete
                if sym_lang.get(c) == lg
                and (psid in mro_cache[c] or _satisfies_protocol(c, reqs, method_arities))
            ]
        return impls_of

    def _fanout_subtype_calls(self, symbols, class_bases, mro_method):
        impls_of = self._load_subtype_impls(symbols, class_bases)
        if not impls_of:
            return
        for row in self.db.execute(
            "SELECT source_file,source_id,target_id,line FROM edges WHERE kind='CALLS'"
        ).fetchall():
            cls_part, _, mname = row["target_id"].rpartition(".")
            resolved = [
                mt
                for csid in impls_of.get(cls_part, ())
                if (mt := mro_method(csid, mname)) is not None and mt != row["target_id"]
            ]
            if 1 <= len(resolved) <= 4:
                for mt in resolved:
                    self.db.execute(
                        "INSERT OR IGNORE INTO edges VALUES(?,?,?,?,?)",
                        (row["source_file"], row["source_id"], mt, "CALLS", row["line"]),
                    )

    def rows(self, sql, args=()):
        return [dict(row) for row in self.db.execute(sql, args)]

    def _upstream_with_imports(self, symbol_id: str, import_cond: str) -> list[str]:
        sql = f"""
            WITH RECURSIVE upstream(node) AS (
                SELECT ?
                UNION SELECT e.source_id FROM edges e JOIN upstream u ON e.target_id=u.node
                    WHERE e.kind='TESTS'
                       OR (e.kind='IMPORTS' AND {import_cond})
                       OR (e.kind='CALLS' AND NOT (e.source_id LIKE '%:' AND u.node LIKE e.source_id || '%'))
                       OR (e.kind='DEFINES' AND e.source_id NOT LIKE '%:')
            ) SELECT node FROM upstream ORDER BY node
        """
        return [row["node"] for row in self.db.execute(sql, (symbol_id,))]

    def upstream(self, symbol_id):
        """Reverse static reachability, excluding module-level DEFINES and self-module entrypoint hub climbing."""
        return self._upstream_with_imports(symbol_id, "1=1")

    def test_upstream(self, symbol_id):
        """Reverse call/test reachability for test coverage, excluding import-only edges to non-module symbols."""
        return self._upstream_with_imports(symbol_id, "u.node LIKE '%:'")
