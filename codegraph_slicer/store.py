"""Transactional index, retaining raw references for rebinding across snapshots."""

from collections import defaultdict
import hashlib
import importlib.metadata
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
    CodeGraphError,
    Edge,
    FileAnalysis,
    Symbol,
    detect_layout,
    extract_file,
    is_supported_source_path,
    module_name,
    plain_target,
    valid_path,
)

_SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".venv",
    "venv",
    "__pycache__",
    "node_modules",
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
    if path.endswith((".ts", ".tsx")):
        return "ts"
    return ""


def _raw_stem_module(path: str, source_root: str) -> str:
    if path.endswith(".py"):
        return module_name(path, source_root)
    if path.endswith((".ts", ".tsx")):
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
            if not path.name.endswith(".d.ts"):
                found.add(path)
    return sorted(found)


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

    def sync_workspace(self, root, *, include_hidden: bool = False):
        root = Path(root).resolve(strict=True)
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
        tsconfig_path = root / "tsconfig.json"
        tsconfig_bytes = (
            tsconfig_path.read_bytes()
            if tsconfig_path.is_file() and not tsconfig_path.is_symlink()
            else b""
        )
        return self._sync(
            files,
            snapshot_root=str(root),
            snapshot_kind="workspace",
            tsconfig_bytes=tsconfig_bytes,
            include_hidden=include_hidden,
        )

    def sync_git_tree(self, root, revision, *, include_hidden: bool = False):
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
        for entry in git("ls-tree", "-r", "-z", rev).split(b"\0"):
            if not entry:
                continue
            meta, raw_path = entry.split(b"\t", 1)
            mode, typ, oid = meta.split()
            if raw_path == b"tsconfig.json" and typ == b"blob":
                tsconfig_oid = oid.decode("ascii")
                continue
            if not raw_path.endswith((b".py", b".java", b".ts", b".tsx")) or raw_path.endswith(b".d.ts"):
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
        tsconfig_bytes = git("cat-file", "blob", tsconfig_oid) if tsconfig_oid else b""
        return self._sync(
            files,
            loader=lambda oid: git("cat-file", "blob", oid),
            snapshot_root=str(root),
            snapshot_kind="git",
            snapshot_revision=rev,
            tsconfig_bytes=tsconfig_bytes,
            include_hidden=include_hidden,
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
    ):
        started = monotonic()
        digest = hashlib.sha256()
        for source_name in (
            "ast_extractor.py",
            "treesitter_support.py",
            "java_extractor.py",
            "ts_extractor.py",
            "store.py",
        ):
            source_path = Path(__file__).with_name(source_name)
            if source_path.exists():
                digest.update(source_path.read_bytes())
                digest.update(b"\0")
        digest.update(f"{sys.version_info.major}.{sys.version_info.minor}".encode())
        for pkg_name in ("tree-sitter", "tree-sitter-language-pack"):
            try:
                ver = importlib.metadata.version(pkg_name)
            except importlib.metadata.PackageNotFoundError:
                ver = ""
            digest.update(f"\0{pkg_name}:{ver}".encode())
        source_root, packages = detect_layout(files)
        # A layout change renames modules, so every file is re-extracted.
        digest.update(f"\0{source_root}\0{','.join(sorted(packages))}".encode())
        if tsconfig_bytes:
            digest.update(b"\0")
            digest.update(tsconfig_bytes)
        engine_sha = digest.hexdigest()
        tsconfig_sha = (
            "sha256:" + hashlib.sha256(tsconfig_bytes).hexdigest()
            if tsconfig_bytes
            else ""
        )
        path_aliases = None
        if tsconfig_bytes:
            from .ts_extractor import parse_tsconfig_paths

            path_aliases = parse_tsconfig_paths(tsconfig_bytes)
        self.db.execute("BEGIN IMMEDIATE")
        with self.db:
            old_rows = list(self.db.execute("SELECT path,blob_sha,module FROM files"))
            old = {row["path"]: row["blob_sha"] for row in old_rows}
            old_modules = {row["path"]: row["module"] for row in old_rows}
            prior_engine = self.db.execute(
                "SELECT value FROM metadata WHERE key='engine_sha'"
            ).fetchone()
            engine_matches = prior_engine is not None and prior_engine[0] == engine_sha
            removed = set(old) - set(files)
            changed_set = {
                path
                for path, (identity, _) in files.items()
                if not engine_matches or old.get(path) != identity
            }
            if changed_set or removed:
                stem_groups: dict[str, set[str]] = defaultdict(set)
                for p in set(files) | set(old):
                    stem_groups[_raw_stem_module(p, source_root)].add(p)
                affected_stems = {
                    _raw_stem_module(p, source_root) for p in (changed_set | removed)
                }
                for p, mod in old_modules.items():
                    raw_m = _raw_stem_module(p, source_root)
                    if mod != raw_m:
                        affected_stems.add(raw_m)
                        affected_stems.add(mod)
                for stem in affected_stems:
                    for p in stem_groups.get(stem, ()):
                        if p in files:
                            changed_set.add(p)
            changed = sorted(changed_set)
            analyses = [
                extract_file(
                    path,
                    loader(files[path][1]) if loader else files[path][1],
                    packages,
                    source_root,
                    *( [path_aliases] if path_aliases else [] ),
                )
                for path in changed
            ]
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
            ):
                self.db.execute(
                    "INSERT INTO metadata(key,value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, value),
                )
            return {
                "parsed_files": len(analyses),
                "cached_files": len(files) - len(analyses),
                "deleted_files": len(removed),
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

    def assert_fresh(self):
        """Refuse ordinary queries unless the indexed Python source is current."""
        metadata = {
            row["key"]: row["value"]
            for row in self.db.execute(
                "SELECT key,value FROM metadata WHERE key IN ('snapshot_root','snapshot_kind','snapshot_revision','include_hidden','tsconfig_sha')"
            )
        }
        root_text = metadata.get("snapshot_root")
        kind = metadata.get("snapshot_kind")
        include_hidden = metadata.get("include_hidden") == "1"
        if not root_text or kind not in {"workspace", "git"}:
            raise StaleSnapshotError("snapshot freshness cannot be verified: missing source metadata; run codegraph-slicer sync ROOT --db DB")
        try:
            root = Path(root_text).resolve(strict=True)
            tsconfig_path = root / "tsconfig.json"
            actual_tsconfig_sha = (
                "sha256:" + hashlib.sha256(tsconfig_path.read_bytes()).hexdigest()
                if tsconfig_path.is_file() and not tsconfig_path.is_symlink()
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
                    if not raw_path.endswith((b".py", b".java", b".ts", b".tsx")) or raw_path.endswith(b".d.ts"):
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
                candidate = ".".join(parts[:index]) + ":" + ".".join(parts[index:])
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
                    return direct(target, module_parts)

        self.db.execute("DELETE FROM edges")
        self.db.execute("DELETE FROM diagnostics")
        for row in self.db.execute(
            "SELECT * FROM raw_edges WHERE kind NOT IN ('ALIAS','WILDCARD_EXPORT','WILDCARD_IMPORT') "
            "ORDER BY source_file,source_id,target_id,kind,line"
        ).fetchall():
            raw_target = _rewrite_lang_target(
                row["target_id"], file_lang.get(row["source_file"], "py")
            )
            target = resolve(raw_target)
            if target is None:
                self.db.execute(
                    "INSERT INTO diagnostics VALUES(?,?,?,?,?,?)",
                    (
                        row["source_file"],
                        row["source_id"],
                        plain_target(raw_target),
                        row["kind"],
                        row["line"],
                        "external, dynamic, shadowed, or unobserved reference",
                    ),
                )
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
        for row in self.db.execute(
            "SELECT symbol_id,file_path,module,qualname FROM symbols WHERE kind!='module'"
        ).fetchall():
            qualname = row["qualname"]
            parent = row["module"] + ":" + qualname.rpartition(".")[0]
            self.db.execute(
                "INSERT OR IGNORE INTO edges VALUES(?,?,?,?,?)",
                (row["file_path"], parent, row["symbol_id"], "DEFINES", 0),
            )

    def rows(self, sql, args=()):
        return [dict(row) for row in self.db.execute(sql, args)]

    def upstream(self, symbol_id):
        """Reverse static reachability, including the containing module's importers."""
        return [
            row["node"]
            for row in self.db.execute(
                """
            WITH RECURSIVE upstream(node) AS (
                SELECT ?
                UNION SELECT e.source_id FROM edges e JOIN upstream u ON e.target_id=u.node
                    WHERE e.kind IN ('CALLS','IMPORTS','TESTS','DEFINES')
            ) SELECT node FROM upstream ORDER BY node
        """,
                (symbol_id,),
            )
        ]
