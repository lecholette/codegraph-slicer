"""Transactional index, retaining raw references for rebinding across snapshots."""

import hashlib
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from time import monotonic
from urllib.parse import quote
from .ast_extractor import MODULE_END, CodeGraphError, detect_layout, extract_file, plain_target, valid_path


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

    def sync_workspace(self, root):
        root = Path(root).resolve(strict=True)
        files = {}
        for path in sorted(root.rglob("*.py")):
            rel = path.relative_to(root)
            if any(
                part in {".git", ".venv", "venv", "__pycache__"} for part in rel.parts
            ):
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
        return self._sync(files, snapshot_root=str(root), snapshot_kind="workspace")

    def sync_git_tree(self, root, revision):
        root = Path(root).resolve(strict=True)
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
        for entry in git("ls-tree", "-r", "-z", rev).split(b"\0"):
            if not entry:
                continue
            meta, raw_path = entry.split(b"\t", 1)
            mode, typ, oid = meta.split()
            if not raw_path.endswith(b".py"):
                continue
            if typ != b"blob" or mode not in {b"100644", b"100755"}:
                raise CodeGraphError(f"non-regular Python source refused: {raw_path!r}")
            try:
                name = valid_path(raw_path.decode("utf8"))
            except UnicodeError as exc:
                raise CodeGraphError(f"non-UTF8 Python path: {raw_path!r}") from exc
            files[name] = ("git:" + oid.decode("ascii"), oid.decode("ascii"))
        return self._sync(
            files,
            loader=lambda oid: git("cat-file", "blob", oid),
            snapshot_root=str(root),
            snapshot_kind="git",
            snapshot_revision=rev,
        )

    def _sync(self, files, loader=None, *, snapshot_root, snapshot_kind, snapshot_revision=""):
        started = monotonic()
        digest = hashlib.sha256()
        for source_path in (Path(__file__).with_name("ast_extractor.py"), Path(__file__)):
            digest.update(source_path.read_bytes())
            digest.update(b"\0")
        digest.update(f"{sys.version_info.major}.{sys.version_info.minor}".encode())
        source_root, packages = detect_layout(files)
        # A layout change renames modules, so every file is re-extracted.
        digest.update(f"\0{source_root}\0{','.join(sorted(packages))}".encode())
        engine_sha = digest.hexdigest()
        self.db.execute("BEGIN IMMEDIATE")
        with self.db:
            old = {
                row["path"]: row["blob_sha"]
                for row in self.db.execute("SELECT path,blob_sha FROM files")
            }
            prior_engine = self.db.execute(
                "SELECT value FROM metadata WHERE key='engine_sha'"
            ).fetchone()
            engine_matches = prior_engine is not None and prior_engine[0] == engine_sha
            removed = set(old) - set(files)
            changed = sorted(
                path
                for path, (identity, _) in files.items()
                if not engine_matches or old.get(path) != identity
            )
            analyses = [
                extract_file(
                    path, loader(files[path][1]) if loader else files[path][1], packages, source_root
                )
                for path in changed
            ]
            for path in sorted(removed | set(changed)):
                self.db.execute("DELETE FROM files WHERE path=?", (path,))
            for analysis in analyses:
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
                self._rebind()
            self.db.execute(
                "INSERT INTO metadata(key,value) VALUES('engine_sha',?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (engine_sha,),
            )
            for key, value in (("snapshot_root", snapshot_root), ("snapshot_kind", snapshot_kind), ("snapshot_revision", snapshot_revision), ("source_root", source_root), ("packages", ",".join(sorted(packages)))):
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
                "SELECT key,value FROM metadata WHERE key IN ('snapshot_root','snapshot_kind','snapshot_revision')"
            )
        }
        root_text = metadata.get("snapshot_root")
        kind = metadata.get("snapshot_kind")
        if not root_text or kind not in {"workspace", "git"}:
            raise StaleSnapshotError("snapshot freshness cannot be verified: missing source metadata; run codegraph-slicer sync ROOT --db DB")
        try:
            root = Path(root_text).resolve(strict=True)
            if kind == "workspace":
                actual = {}
                for path in sorted(root.rglob("*.py")):
                    rel = path.relative_to(root)
                    if any(part in {".git", ".venv", "venv", "__pycache__"} for part in rel.parts):
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
                    if not raw_path.endswith(b".py"):
                        continue
                    if typ != b"blob" or mode not in {b"100644", b"100755"}:
                        raise StaleSnapshotError(f"snapshot source cannot be verified: non-regular Python source {raw_path!r}")
                    name = valid_path(raw_path.decode("utf8"))
                    actual[name] = "git:" + oid.decode("ascii")
                working = {}
                for path in sorted(root.rglob("*.py")):
                    rel = path.relative_to(root)
                    if ".git" in rel.parts:
                        continue
                    if any(part in {".venv", "venv", "__pycache__"} for part in rel.parts) and rel.as_posix() not in actual:
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

    def _rebind(self):
        symbols = {
            row["symbol_id"]: row["kind"]
            for row in self.db.execute("SELECT symbol_id,kind FROM symbols")
        }
        modules = {row["module"] for row in self.db.execute("SELECT module FROM files")}
        # Module-level import bindings: "pkg.Name" -> dotted import target.
        aliases = {
            row["source_id"]: row["target_id"]
            for row in self.db.execute("SELECT source_id,target_id FROM raw_edges WHERE kind='ALIAS'")
        }
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
            "SELECT * FROM raw_edges WHERE kind!='ALIAS' ORDER BY source_file,source_id,target_id,kind,line"
        ).fetchall():
            target = resolve(row["target_id"])
            if target is None:
                self.db.execute(
                    "INSERT INTO diagnostics VALUES(?,?,?,?,?,?)",
                    (
                        row["source_file"],
                        row["source_id"],
                        plain_target(row["target_id"]),
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
