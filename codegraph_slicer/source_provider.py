"""Source providers and local cache for local Git checkouts and GitHub CLI."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Protocol
from urllib.parse import quote, urlencode

from .ast_extractor import CodeGraphError, is_supported_source_path
from .store import _iter_workspace_files, _should_skip_rel_path, git_env

_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
_SLUG_PART_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_TOKEN_RE = re.compile(r"(?:gh[opurs]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)")

DEFAULT_CACHE_MAX_BYTES = 2 * 1024 * 1024 * 1024  # 2 GiB
TREE_TTL_SECONDS = 30 * 86400
BLOB_TTL_SECONDS = 90 * 86400
GRAPHQL_BATCH_SIZE = 25
DEFAULT_GH_TIMEOUT_SECONDS = 30.0


def default_gh_timeout() -> float:
    raw = os.environ.get("CODEGRAPH_SLICER_GH_TIMEOUT")
    if not raw:
        return DEFAULT_GH_TIMEOUT_SECONDS
    try:
        return max(1.0, float(raw))
    except ValueError:
        return DEFAULT_GH_TIMEOUT_SECONDS


def _redact(text: str) -> str:
    return _TOKEN_RE.sub("[REDACTED]", text)


def parse_github_repo(repo_spec: str) -> tuple[str, str]:
    """Parse 'owner/name' or a GitHub URL into (owner, name)."""
    raw = repo_spec.strip()
    if raw.startswith("https://github.com/"):
        raw = raw.removeprefix("https://github.com/")
    elif raw.startswith("http://github.com/"):
        raw = raw.removeprefix("http://github.com/")
    elif raw.startswith("git@github.com:"):
        raw = raw.removeprefix("git@github.com:")
    elif raw.startswith("ssh://git@github.com/"):
        raw = raw.removeprefix("ssh://git@github.com/")
    raw = raw.removesuffix(".git").strip("/")
    parts = raw.split("/")
    if len(parts) != 2 or not all(_SLUG_PART_RE.fullmatch(p) and p not in (".", "..") for p in parts):
        raise CodeGraphError(f"invalid GitHub repository specifier: {repo_spec!r}")
    return parts[0], parts[1]


def default_cache_dir() -> Path:
    env_dir = os.environ.get("CODEGRAPH_SLICER_CACHE_DIR")
    if env_dir:
        return Path(env_dir).expanduser()
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
    return base / "codegraph-slicer"


def default_cache_max_bytes() -> int:
    raw = os.environ.get("CODEGRAPH_SLICER_CACHE_MAX_BYTES")
    if not raw:
        return DEFAULT_CACHE_MAX_BYTES
    try:
        return max(1024, int(raw))
    except ValueError:
        return DEFAULT_CACHE_MAX_BYTES


class NoOpCache:
    """Inert cache implementation used when --no-cache disables persistent reads/writes."""

    def get_tree(self, owner: str, repo: str, commit: str) -> dict | None:
        return None

    def put_tree(self, owner: str, repo: str, commit: str, payload: dict) -> None:
        return None

    def get_blob(self, key: str) -> bytes | None:
        return None

    def put_blob(self, key: str, data: bytes) -> None:
        return None

    def delete_blob(self, key: str) -> None:
        return None


class BlobCache:
    """Disk-backed tree & blob cache with TTL expiration and LRU eviction."""

    def __init__(
        self,
        root: Path | None = None,
        *,
        max_bytes: int | None = None,
        tree_ttl_seconds: int = TREE_TTL_SECONDS,
        blob_ttl_seconds: int = BLOB_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.root = Path(root) if root is not None else default_cache_dir()
        self.max_bytes = max_bytes if max_bytes is not None else default_cache_max_bytes()
        self.tree_ttl_seconds = tree_ttl_seconds
        self.blob_ttl_seconds = blob_ttl_seconds
        self._clock = clock or time.time
        self.trees_dir = self.root / "trees"
        self.blobs_dir = self.root / "blobs"
        self.trees_dir.mkdir(parents=True, exist_ok=True)
        self.blobs_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "index.sqlite"
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS blobs ("
                "key TEXT PRIMARY KEY, "
                "disk_rel TEXT NOT NULL, "
                "size_bytes INTEGER NOT NULL, "
                "created_at REAL NOT NULL, "
                "last_accessed_at REAL NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS trees ("
                "cache_key TEXT PRIMARY KEY, "
                "disk_rel TEXT NOT NULL, "
                "size_bytes INTEGER NOT NULL DEFAULT 0, "
                "created_at REAL NOT NULL)"
            )
            cols = {r[1] for r in conn.execute("PRAGMA table_info(trees)").fetchall()}
            if "size_bytes" not in cols:
                conn.execute("ALTER TABLE trees ADD COLUMN size_bytes INTEGER NOT NULL DEFAULT 0")

    def _tree_rel(self, owner: str, repo: str, commit: str) -> str:
        digest = hashlib.sha256(f"github.com/{owner}/{repo}/{commit}".encode("utf-8")).hexdigest()
        return f"{digest[:2]}/{digest}.json"

    def _blob_rel(self, key: str) -> str:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return f"{digest[:2]}/{digest}"

    def get_tree(self, owner: str, repo: str, commit: str) -> dict | None:
        cache_key = f"github.com/{owner}/{repo}/{commit}"
        now = self._clock()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT disk_rel, created_at FROM trees WHERE cache_key = ?", (cache_key,)
            ).fetchone()
            if row is None:
                return None
            if now - float(row["created_at"]) > self.tree_ttl_seconds:
                conn.execute("DELETE FROM trees WHERE cache_key = ?", (cache_key,))
                (self.trees_dir / str(row["disk_rel"])).unlink(missing_ok=True)
                return None
            path = self.trees_dir / str(row["disk_rel"])
            if not path.is_file():
                conn.execute("DELETE FROM trees WHERE cache_key = ?", (cache_key,))
                return None
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None

    def put_tree(self, owner: str, repo: str, commit: str, payload: dict) -> None:
        raw_bytes = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(raw_bytes) > self.max_bytes:
            return
        cache_key = f"github.com/{owner}/{repo}/{commit}"
        rel = self._tree_rel(owner, repo, commit)
        target = self.trees_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        now = self._clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            try:
                tmp.write_bytes(raw_bytes)
                tmp.replace(target)
            finally:
                tmp.unlink(missing_ok=True)
            conn.execute(
                "INSERT INTO trees(cache_key, disk_rel, size_bytes, created_at) VALUES(?,?,?,?) "
                "ON CONFLICT(cache_key) DO UPDATE SET disk_rel=excluded.disk_rel, "
                "size_bytes=excluded.size_bytes, created_at=excluded.created_at",
                (cache_key, rel, len(raw_bytes), now),
            )
            self._evict_locked(conn, now)

    def get_blob(self, key: str) -> bytes | None:
        now = self._clock()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT disk_rel, created_at, last_accessed_at FROM blobs WHERE key = ?", (key,)
            ).fetchone()
            if row is None:
                return None
            if now - float(row["last_accessed_at"]) > self.blob_ttl_seconds:
                conn.execute("DELETE FROM blobs WHERE key = ?", (key,))
                (self.blobs_dir / str(row["disk_rel"])).unlink(missing_ok=True)
                return None
            path = self.blobs_dir / str(row["disk_rel"])
            if not path.is_file():
                conn.execute("DELETE FROM blobs WHERE key = ?", (key,))
                return None
            try:
                data = path.read_bytes()
            except OSError:
                return None
            conn.execute("UPDATE blobs SET last_accessed_at = ? WHERE key = ?", (now, key))
            return data

    def put_blob(self, key: str, data: bytes) -> None:
        if len(data) > self.max_bytes:
            return
        rel = self._blob_rel(key)
        target = self.blobs_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        now = self._clock()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tmp = target.with_name(f"{target.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
            try:
                tmp.write_bytes(data)
                tmp.replace(target)
            finally:
                tmp.unlink(missing_ok=True)
            conn.execute(
                "INSERT INTO blobs(key, disk_rel, size_bytes, created_at, last_accessed_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET disk_rel=excluded.disk_rel, "
                "size_bytes=excluded.size_bytes, last_accessed_at=excluded.last_accessed_at",
                (key, rel, len(data), now, now),
            )
            self._evict_locked(conn, now)

    def delete_blob(self, key: str) -> None:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT disk_rel FROM blobs WHERE key = ?", (key,)).fetchone()
            if row is not None:
                conn.execute("DELETE FROM blobs WHERE key = ?", (key,))
                (self.blobs_dir / str(row["disk_rel"])).unlink(missing_ok=True)

    def _evict_locked(self, conn: sqlite3.Connection, now: float) -> None:
        expired_trees = conn.execute(
            "SELECT cache_key, disk_rel FROM trees WHERE ? - created_at > ?",
            (now, self.tree_ttl_seconds),
        ).fetchall()
        for row in expired_trees:
            conn.execute("DELETE FROM trees WHERE cache_key = ?", (row["cache_key"],))
            (self.trees_dir / str(row["disk_rel"])).unlink(missing_ok=True)
        expired_blobs = conn.execute(
            "SELECT key, disk_rel FROM blobs WHERE ? - last_accessed_at > ?",
            (now, self.blob_ttl_seconds),
        ).fetchall()
        for row in expired_blobs:
            conn.execute("DELETE FROM blobs WHERE key = ?", (row["key"],))
            (self.blobs_dir / str(row["disk_rel"])).unlink(missing_ok=True)

        total = int(conn.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM blobs").fetchone()[0]) + int(
            conn.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM trees").fetchone()[0]
        )
        if total <= self.max_bytes:
            return
        candidates = conn.execute(
            "SELECT 'blob' AS kind, key AS id, disk_rel, size_bytes, last_accessed_at AS ts FROM blobs "
            "UNION ALL "
            "SELECT 'tree' AS kind, cache_key AS id, disk_rel, size_bytes, created_at AS ts FROM trees "
            "ORDER BY ts ASC"
        ).fetchall()
        for row in candidates:
            if total <= self.max_bytes:
                break
            if row["kind"] == "blob":
                conn.execute("DELETE FROM blobs WHERE key = ?", (row["id"],))
                (self.blobs_dir / str(row["disk_rel"])).unlink(missing_ok=True)
            else:
                conn.execute("DELETE FROM trees WHERE cache_key = ?", (row["id"],))
                (self.trees_dir / str(row["disk_rel"])).unlink(missing_ok=True)
            total -= int(row["size_bytes"])


class SourceProvider(Protocol):
    provider_name: str

    def resolve_ref(self) -> str: ...
    def list_files(self) -> list[str]: ...
    def file_exists(self, rel_path: str) -> bool: ...
    def read_files_batch(self, rel_paths: list[str]) -> dict[str, bytes]: ...
    def grep(self, needle: str, limit: int = 15) -> list[str]: ...
    def coverage_metadata(self) -> dict: ...


class LocalGitProvider:
    """Source provider backed by a local workspace directory or an explicit Git commit."""

    provider_name = "local"

    def __init__(self, root: str | Path, *, ref: str | None = None) -> None:
        resolved = Path(root).resolve(strict=True)
        if not resolved.is_dir():
            raise CodeGraphError(f"workspace root is not a directory: {root}")
        self.root = resolved
        self.ref = ref
        self._commit: str | None = None
        self._files_cache: list[str] | None = None
        self._tree_all_paths: set[str] | None = None
        if self.ref is not None and not (self.root / ".git").exists():
            raise CodeGraphError(f"cannot resolve --ref {self.ref!r}: {root} is not a git repository")

    def origin_github_repo(self) -> tuple[str, str] | None:
        if not (self.root / ".git").exists():
            return None
        res = subprocess.run(
            ["git", "-C", str(self.root), "remote", "get-url", "origin"],
            env=git_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if res.returncode != 0:
            return None
        url = res.stdout.decode("utf-8", "replace").strip()
        if not url:
            return None
        try:
            return parse_github_repo(url)
        except CodeGraphError:
            return None

    def resolve_ref(self) -> str:
        if self._commit is not None:
            return self._commit
        if not (self.root / ".git").exists():
            return ""
        target = self.ref if self.ref is not None else "HEAD"
        res = subprocess.run(
            ["git", "-C", str(self.root), "rev-parse", "--verify", f"{target}^{{commit}}"],
            env=git_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        sha = res.stdout.decode("utf-8", "replace").strip() if res.returncode == 0 else ""
        if self.ref is not None and not _SHA40_RE.fullmatch(sha):
            err = res.stderr.decode("utf-8", "replace").strip()
            raise CodeGraphError(f"failed to resolve --ref {self.ref!r} in {self.root}: {err or 'unknown revision'}")
        if _SHA40_RE.fullmatch(sha):
            self._commit = sha
            return sha
        return ""

    def _ensure_commit_tree(self) -> None:
        if self._tree_all_paths is not None and self._files_cache is not None:
            return
        commit = self.resolve_ref()
        res = subprocess.run(
            ["git", "-C", str(self.root), "ls-tree", "-r", "-z", "--name-only", commit],
            env=git_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if res.returncode != 0:
            err = res.stderr.decode("utf-8", "replace").strip()
            raise CodeGraphError(f"git ls-tree failed for {commit}: {err}")
        all_paths: set[str] = set()
        src_paths: list[str] = []
        for raw in res.stdout.split(b"\0"):
            if not raw:
                continue
            p = raw.decode("utf-8", "replace")
            all_paths.add(p)
            if is_supported_source_path(p) and not _should_skip_rel_path(PurePosixPath(p).parts):
                src_paths.append(p)
        self._tree_all_paths = all_paths
        self._files_cache = src_paths

    def list_files(self) -> list[str]:
        if self.ref is not None:
            self._ensure_commit_tree()
            return list(self._files_cache or ())
        if self._files_cache is not None:
            return self._files_cache
        paths: list[str] = []
        if (self.root / ".git").exists():
            res = subprocess.run(
                ["git", "-C", str(self.root), "ls-files", "-z"],
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
                cwd=str(self.root),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if res.returncode == 0:
                for line in res.stdout.decode("utf-8", "replace").splitlines():
                    if is_supported_source_path(line) and not _should_skip_rel_path(PurePosixPath(line).parts):
                        paths.append(line)
        self._files_cache = paths
        return paths

    def _is_safe(self, rel_path: str) -> bool:
        full = self.root / rel_path
        return not full.is_symlink() and full.is_file() and full.resolve().is_relative_to(self.root)

    def file_exists(self, rel_path: str) -> bool:
        if self.ref is not None:
            self._ensure_commit_tree()
            return rel_path in (self._tree_all_paths or ())
        return self._is_safe(rel_path)

    def read_files_batch(self, rel_paths: list[str]) -> dict[str, bytes]:
        if self.ref is not None:
            commit = self.resolve_ref()
            unique = list(dict.fromkeys(rel_paths))
            if not unique:
                return {}
            specs = "".join(f"{commit}:{p}\n" for p in unique).encode("utf-8")
            res = subprocess.run(
                ["git", "-C", str(self.root), "cat-file", "--batch"],
                env=git_env(),
                input=specs,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            if res.returncode != 0:
                err = res.stderr.decode("utf-8", "replace").strip()
                raise CodeGraphError(f"git cat-file --batch failed for {commit}: {err}")
            buf = res.stdout
            pos = 0
            out: dict[str, bytes] = {}
            for rel_p in unique:
                nl = buf.find(b"\n", pos)
                if nl == -1:
                    break
                header = buf[pos:nl].decode("utf-8", "replace")
                pos = nl + 1
                if header.endswith(" missing") or header.endswith(" ambiguous"):
                    continue
                parts = header.split(" ")
                if len(parts) != 3:
                    continue
                _, obj_type, size_str = parts
                size = int(size_str)
                data = buf[pos : pos + size]
                pos += size + 1
                if obj_type == "blob":
                    out[rel_p] = data
            return out
        out = {}
        for rel_p in rel_paths:
            if self._is_safe(rel_p):
                out[rel_p] = (self.root / rel_p).read_bytes()
        return out

    def grep(self, needle: str, limit: int = 15) -> list[str]:
        if not needle or len(needle) < 2:
            return []
        if self.ref is not None:
            commit = self.resolve_ref()
            res = subprocess.run(
                ["git", "-C", str(self.root), "grep", "-l", "-F", needle, commit],
                env=git_env(),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            out: list[str] = []
            if res.returncode in (0, 1):
                prefix = f"{commit}:"
                for raw in res.stdout.decode("utf-8", "replace").splitlines():
                    rel = raw.removeprefix(prefix)
                    if is_supported_source_path(rel) and not _should_skip_rel_path(PurePosixPath(rel).parts):
                        out.append(rel)
                        if len(out) >= limit:
                            break
            return out
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
                str(self.root),
            ]
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
            if res.returncode in (0, 1):
                for raw in res.stdout.decode("utf-8", "replace").splitlines():
                    try:
                        rel = Path(raw).resolve().relative_to(self.root).as_posix()
                    except ValueError:
                        continue
                    if is_supported_source_path(rel) and not _should_skip_rel_path(PurePosixPath(rel).parts):
                        out.append(rel)
                        if len(out) >= limit:
                            break
            return out
        if (self.root / ".git").exists():
            res = subprocess.run(
                ["git", "-C", str(self.root), "grep", "-l", "-F", needle],
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
        for path in _iter_workspace_files(self.root):
            entry = self._read_safe_workspace_bytes(path)
            if entry is None:
                continue
            rel, raw_bytes = entry
            if needle_bytes in raw_bytes:
                out.append(rel)
                if len(out) >= limit:
                    break
        return out

    def _read_safe_workspace_bytes(self, path: Path) -> tuple[str, bytes] | None:
        if path.is_symlink() or not path.is_file():
            return None
        try:
            resolved = path.resolve()
            if not resolved.is_relative_to(self.root):
                return None
            rel = path.relative_to(self.root).as_posix()
            if _should_skip_rel_path(PurePosixPath(rel).parts):
                return None
            return rel, path.read_bytes()
        except (OSError, ValueError):
            return None

    def coverage_metadata(self) -> dict:
        meta: dict = {"provider": self.provider_name}
        if self._commit:
            meta["commit"] = self._commit
        if self.ref is not None:
            meta["ref"] = self.ref
        return meta


GITHUB_API_BASE = "https://api.github.com"
_GITHUB_API_VERSION = "2022-11-28"
_GITHUB_USER_AGENT = "codegraph-slicer/0.3.14"


def _verify_git_blob_sha1(rel_path: str, content: bytes, expected_sha: str | None) -> None:
    if expected_sha and _SHA40_RE.fullmatch(expected_sha):
        actual_sha = hashlib.sha1(f"blob {len(content)}\0".encode("ascii") + content).hexdigest()
        if actual_sha != expected_sha:
            raise CodeGraphError(
                f"blob SHA-1 mismatch for {rel_path}: expected {expected_sha}, got {actual_sha}"
            )


def _extract_tree_entries(payload: dict[str, Any]) -> tuple[list[str], dict[str, str], bool]:
    truncated = bool(payload.get("truncated", False))
    raw_paths: list[str] = []
    blob_shas: dict[str, str] = {}
    for entry in payload.get("tree", []):
        if entry.get("type") != "blob":
            continue
        p = str(entry.get("path", ""))
        if not p:
            continue
        raw_paths.append(p)
        if entry.get("sha"):
            blob_shas[p] = str(entry["sha"])
    return raw_paths, blob_shas, truncated


def _build_graphql_blob_query(commit: str, chunk: list[str]) -> str:
    aliases = [
        f"f{i}: object(expression: {json.dumps(f'{commit}:{p}')}) {{ ... on Blob {{ text byteSize isBinary }} }}"
        for i, p in enumerate(chunk)
    ]
    return f"query($owner:String!,$name:String!){{ repository(owner:$owner,name:$name){{ {' '.join(aliases)} }} }}"


class _BaseGitHubProvider:
    """Shared tree/blob caching, SHA-1 integrity verification, and ref logic for GitHub providers."""

    provider_name = "github"

    def __init__(
        self,
        repo: str,
        *,
        ref: str = "HEAD",
        cache: BlobCache | NoOpCache | None = None,
        refresh_cache: bool = False,
        timeout: float | None = None,
    ) -> None:
        self.owner, self.repo = parse_github_repo(repo)
        self.ref = ref or "HEAD"
        self.cache = cache if cache is not None else BlobCache()
        self.refresh_cache = refresh_cache
        self.timeout = timeout if timeout is not None else default_gh_timeout()
        self._commit: str | None = self.ref if _SHA40_RE.fullmatch(self.ref) else None
        self._pinned_sha = self._commit is not None
        self._default_head_commit: str | None = None
        self._paths: list[str] | None = None
        self._path_set: set[str] | None = None
        self._blob_shas: dict[str, str] = {}
        self._mem_blobs: dict[str, bytes] = {}
        self.tree_truncated = False
        self.api_calls = 0
        self.cache_hits = 0
        self.grep_note: str | None = None

    def _fetch_commit_sha_remote(self, ref_name: str) -> str:
        raise NotImplementedError

    def _fetch_tree_payload_remote(self, commit: str) -> dict[str, Any]:
        raise NotImplementedError

    def _fetch_blobs_remote(self, commit: str, missing: list[str]) -> dict[str, bytes]:
        raise NotImplementedError

    def _search_code_remote(self, needle: str, limit: int) -> list[str]:
        raise NotImplementedError

    def resolve_ref(self) -> str:
        if self._commit is not None:
            return self._commit
        sha = self._fetch_commit_sha_remote(self.ref).strip()
        if not _SHA40_RE.fullmatch(sha):
            raise CodeGraphError(
                f"{self.provider_name} returned invalid commit SHA for {self.owner}/{self.repo}@{self.ref}: {sha!r}"
            )
        self._commit = sha
        if self.ref == "HEAD":
            self._default_head_commit = sha
        return sha

    def _resolve_default_head(self) -> str:
        if self._default_head_commit is not None:
            return self._default_head_commit
        sha = self._fetch_commit_sha_remote("HEAD").strip()
        if not _SHA40_RE.fullmatch(sha):
            raise CodeGraphError(
                f"{self.provider_name} returned invalid HEAD SHA for {self.owner}/{self.repo}: {sha!r}"
            )
        self._default_head_commit = sha
        return sha

    def _ensure_tree(self) -> None:
        if self._paths is not None:
            return
        commit = self.resolve_ref()
        cached = None if self.refresh_cache else self.cache.get_tree(self.owner, self.repo, commit)
        if cached is not None:
            self.cache_hits += 1
            raw_paths = list(cached.get("paths", []))
            self._blob_shas = dict(cached.get("blob_shas", {}))
            self.tree_truncated = bool(cached.get("truncated", False))
        else:
            payload = self._fetch_tree_payload_remote(commit)
            raw_paths, blob_shas, self.tree_truncated = _extract_tree_entries(payload)
            self._blob_shas = blob_shas
            self.cache.put_tree(
                self.owner,
                self.repo,
                commit,
                {"paths": raw_paths, "blob_shas": blob_shas, "truncated": self.tree_truncated},
            )
        self._paths = [
            p
            for p in raw_paths
            if is_supported_source_path(p) and not _should_skip_rel_path(PurePosixPath(p).parts)
        ]
        self._path_set = set(raw_paths)

    def list_files(self) -> list[str]:
        self._ensure_tree()
        return list(self._paths or ())

    def file_exists(self, rel_path: str) -> bool:
        self._ensure_tree()
        return rel_path in (self._path_set or ())

    def _blob_cache_key(self, commit: str, rel_path: str) -> str:
        sha = self._blob_shas.get(rel_path)
        if sha:
            return f"git:{sha}"
        return f"gh:{self.owner}/{self.repo}/{commit}/{rel_path}"

    def _read_cached_blob(self, commit: str, rel_p: str) -> bytes | None:
        cache_key = self._blob_cache_key(commit, rel_p)
        cached = self.cache.get_blob(cache_key)
        if cached is None:
            return None
        expected_sha = self._blob_shas.get(rel_p)
        if expected_sha and _SHA40_RE.fullmatch(expected_sha):
            actual_sha = hashlib.sha1(f"blob {len(cached)}\0".encode("ascii") + cached).hexdigest()
            if actual_sha != expected_sha:
                self.cache.delete_blob(cache_key)
                return None
        return cached

    def _decode_graphql_entry(self, rel_p: str, blob: object) -> bytes | None:
        if not isinstance(blob, dict) or blob.get("isBinary") or blob.get("text") is None:
            return None
        content = str(blob["text"]).encode("utf-8")
        expected_size = blob.get("byteSize")
        if expected_size is not None and len(content) != int(expected_size):
            raise CodeGraphError(
                f"github graphql byteSize mismatch for {rel_p}: expected {expected_size}, got {len(content)}"
            )
        _verify_git_blob_sha1(rel_p, content, self._blob_shas.get(rel_p))
        return content

    def _parse_graphql_blobs(self, chunk: list[str], parsed: dict[str, Any]) -> dict[str, bytes]:
        errors = parsed.get("errors")
        if isinstance(errors, list) and errors:
            msgs = [str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in errors]
            raise CodeGraphError(f"github graphql returned errors: {_redact('; '.join(msgs))}")
        data = (parsed.get("data") or {}).get("repository") or {}
        out: dict[str, bytes] = {}
        for i, rel_p in enumerate(chunk):
            content = self._decode_graphql_entry(rel_p, data.get(f"f{i}"))
            if content is not None:
                out[rel_p] = content
        return out

    def read_files_batch(self, rel_paths: list[str]) -> dict[str, bytes]:
        commit = self.resolve_ref()
        out: dict[str, bytes] = {}
        missing: list[str] = []
        for rel_p in dict.fromkeys(rel_paths):
            if rel_p in self._mem_blobs:
                out[rel_p] = self._mem_blobs[rel_p]
                continue
            cached = self._read_cached_blob(commit, rel_p)
            if cached is not None:
                self.cache_hits += 1
                self._mem_blobs[rel_p] = cached
                out[rel_p] = cached
            else:
                missing.append(rel_p)
        if missing:
            fetched = self._fetch_blobs_remote(commit, missing)
            for rel_p, content in fetched.items():
                self.cache.put_blob(self._blob_cache_key(commit, rel_p), content)
                self._mem_blobs[rel_p] = content
                out[rel_p] = content
        return out

    def _can_remote_grep(self) -> bool:
        if self._pinned_sha:
            self.grep_note = "github code search skipped on pinned commit SHA (GitHub indexes default branch only)"
            return False
        if self.ref != "HEAD":
            if self.resolve_ref() != self._resolve_default_head():
                self.grep_note = (
                    f"github code search skipped for non-default ref {self.ref!r} "
                    "(GitHub indexes default branch only)"
                )
                return False
        return True

    def _grep_mem_blobs(self, needle_bytes: bytes, limit: int) -> list[str]:
        out: list[str] = []
        for rel_p, raw in self._mem_blobs.items():
            if is_supported_source_path(rel_p) and needle_bytes in raw:
                out.append(rel_p)
                if len(out) >= limit:
                    break
        return out

    def _is_eligible_grep_hit(self, path: str, seen: list[str]) -> bool:
        return bool(
            path
            and path not in seen
            and is_supported_source_path(path)
            and not _should_skip_rel_path(PurePosixPath(path).parts)
        )

    def grep(self, needle: str, limit: int = 15) -> list[str]:
        if not needle or len(needle) < 2:
            return []
        out = self._grep_mem_blobs(needle.encode("utf-8"), limit)
        if len(out) >= limit or not self._can_remote_grep():
            return out
        for p in self._search_code_remote(needle, limit):
            if self._is_eligible_grep_hit(p, out):
                out.append(p)
                if len(out) >= limit:
                    break
        return out

    def coverage_metadata(self) -> dict:
        meta: dict = {
            "provider": self.provider_name,
            "repo": f"{self.owner}/{self.repo}",
            "commit": self._commit or "",
            "api_calls": self.api_calls,
            "cache_hits": self.cache_hits,
            "tree_truncated": self.tree_truncated,
        }
        if self.grep_note:
            meta["grep_note"] = self.grep_note
        return meta


class GitHubCliProvider(_BaseGitHubProvider):
    """Source provider backed by the `gh` CLI (REST tree + batched GraphQL blobs) and BlobCache."""

    provider_name = "github_cli"

    def _run_gh(self, args: list[str]) -> str:
        cmd = ["gh", *args]
        env = os.environ.copy()
        env["GH_HOST"] = "github.com"
        try:
            res = subprocess.run(
                cmd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CodeGraphError("gh CLI is not installed on PATH") from exc
        except subprocess.TimeoutExpired as exc:
            raise CodeGraphError(f"gh command timed out after {self.timeout:.1f}s: {' '.join(cmd[:4])}") from exc
        self.api_calls += 1
        if res.returncode != 0:
            err = _redact((res.stderr or res.stdout or "").strip())
            raise CodeGraphError(f"gh api failed ({res.returncode}): {err}")
        return res.stdout

    def _fetch_commit_sha_remote(self, ref_name: str) -> str:
        encoded_ref = quote(ref_name, safe="")
        return self._run_gh(["api", f"repos/{self.owner}/{self.repo}/commits/{encoded_ref}", "--jq", ".sha"])

    def _fetch_tree_payload_remote(self, commit: str) -> dict[str, Any]:
        raw = self._run_gh(["api", f"repos/{self.owner}/{self.repo}/git/trees/{commit}?recursive=1"])
        try:
            return json.loads(raw)
        except ValueError as exc:
            raise CodeGraphError(f"invalid JSON from gh tree API: {exc}") from exc

    def _fetch_blobs_remote(self, commit: str, missing: list[str]) -> dict[str, bytes]:
        out: dict[str, bytes] = {}
        for idx in range(0, len(missing), GRAPHQL_BATCH_SIZE):
            chunk = missing[idx : idx + GRAPHQL_BATCH_SIZE]
            query = _build_graphql_blob_query(commit, chunk)
            raw = self._run_gh(
                [
                    "api",
                    "graphql",
                    "-f",
                    f"owner={self.owner}",
                    "-f",
                    f"name={self.repo}",
                    "-f",
                    f"query={query}",
                ]
            )
            try:
                parsed = json.loads(raw)
            except ValueError as exc:
                raise CodeGraphError(f"invalid JSON from gh graphql API: {exc}") from exc
            out.update(self._parse_graphql_blobs(chunk, parsed))
        return out

    def _search_code_remote(self, needle: str, limit: int) -> list[str]:
        raw_out = self._run_gh(
            [
                "search",
                "code",
                "--repo",
                f"{self.owner}/{self.repo}",
                needle,
                "--json",
                "path",
                "--limit",
                str(limit),
            ]
        )
        try:
            hits = json.loads(raw_out)
        except ValueError:
            return []
        return [str(hit.get("path", "")) for hit in hits if isinstance(hit, dict)]


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[override]
        raise CodeGraphError(f"GitHub API redirect refused (HTTP {code})")


def _decode_rest_blob_payload(rel_path: str, payload: dict[str, Any], expected_sha: str | None) -> bytes | None:
    if payload.get("encoding") != "base64" or not isinstance(payload.get("content"), str):
        return None
    try:
        raw = base64.b64decode(payload["content"], validate=False)
    except ValueError:
        return None
    if b"\x00" in raw:
        return None
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    _verify_git_blob_sha1(rel_path, raw, expected_sha)
    return raw


def _parse_github_json_object(raw: str) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise CodeGraphError(f"invalid JSON from GitHub API: {exc}") from exc
    if not isinstance(parsed, dict):
        raise CodeGraphError("unexpected non-object JSON from GitHub API")
    return parsed


class GitHubHttpProvider(_BaseGitHubProvider):
    """Pure-Python GitHub API provider (httpx with urllib fallback) with per-instance token isolation."""

    provider_name = "github_http"

    def __init__(
        self,
        repo: str,
        *,
        ref: str = "HEAD",
        token: str | None = None,
        cache: BlobCache | NoOpCache | None = None,
        refresh_cache: bool = False,
        timeout: float | None = None,
        client: Any | None = None,
    ) -> None:
        super().__init__(repo, ref=ref, cache=cache, refresh_cache=refresh_cache, timeout=timeout)
        if token is not None:
            if not token or any(ch.isspace() for ch in token):
                raise CodeGraphError("invalid GitHub token")
        self._token = token
        self._client = client
        self._owns_client = False

    def __repr__(self) -> str:
        auth = "authenticated" if self._token else "public"
        return f"GitHubHttpProvider(repo={self.owner}/{self.repo!r}, ref={self.ref!r}, auth={auth})"

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None
            self._owns_client = False

    def __enter__(self) -> "GitHubHttpProvider":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _headers(self) -> dict[str, str]:
        hdrs = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": _GITHUB_API_VERSION,
            "User-Agent": _GITHUB_USER_AGENT,
        }
        if self._token:
            hdrs["Authorization"] = f"Bearer {self._token}"
        return hdrs

    def _get_httpx_client(self) -> Any | None:
        if self._client is not None:
            return self._client
        try:
            import httpx
        except ImportError:
            return None
        self._client = httpx.Client(
            base_url=GITHUB_API_BASE,
            follow_redirects=False,
            timeout=self.timeout,
        )
        self._owns_client = True
        return self._client

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        client = self._get_httpx_client()
        if client is not None:
            return self._request_via_httpx(client, method, path, params=params, json_body=json_body)
        return self._request_via_urllib(method, path, params=params, json_body=json_body)

    def _request_via_httpx(
        self,
        client: Any,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{GITHUB_API_BASE}{path}"
        try:
            resp = client.request(
                method,
                url,
                headers=self._headers(),
                params=params,
                json=json_body,
                timeout=self.timeout,
            )
        except Exception as exc:
            raise CodeGraphError(f"GitHub API request failed: {_redact(str(exc))}") from exc
        self.api_calls += 1
        if 300 <= resp.status_code < 400:
            raise CodeGraphError(f"GitHub API redirect refused (HTTP {resp.status_code})")
        if resp.status_code >= 400:
            detail = _redact((resp.text or "").strip()[:300])
            raise CodeGraphError(f"GitHub API failed ({resp.status_code}): {detail}")
        return _parse_github_json_object(resp.text)

    def _request_via_urllib(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{GITHUB_API_BASE}{path}" + (f"?{urlencode(params)}" if params else "")
        headers = self._headers()
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8") if json_body is not None else None
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        opener = urllib.request.build_opener(_NoRedirectHandler)
        self.api_calls += 1
        try:
            with opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = _redact(exc.read().decode("utf-8", "replace").strip()[:300])
            raise CodeGraphError(f"GitHub API failed ({exc.code}): {body}") from exc
        except CodeGraphError:
            raise
        except Exception as exc:
            raise CodeGraphError(f"GitHub API request failed: {_redact(str(exc))}") from exc
        return _parse_github_json_object(raw)

    def _fetch_commit_sha_remote(self, ref_name: str) -> str:
        encoded_ref = quote(ref_name, safe="")
        payload = self._request_json("GET", f"/repos/{self.owner}/{self.repo}/commits/{encoded_ref}")
        return str(payload.get("sha", ""))

    def _fetch_tree_payload_remote(self, commit: str) -> dict[str, Any]:
        return self._request_json(
            "GET",
            f"/repos/{self.owner}/{self.repo}/git/trees/{commit}",
            params={"recursive": "1"},
        )

    def _fetch_blobs_remote(self, commit: str, missing: list[str]) -> dict[str, bytes]:
        if self._token:
            out: dict[str, bytes] = {}
            for idx in range(0, len(missing), GRAPHQL_BATCH_SIZE):
                chunk = missing[idx : idx + GRAPHQL_BATCH_SIZE]
                query = _build_graphql_blob_query(commit, chunk)
                parsed = self._request_json(
                    "POST",
                    "/graphql",
                    json_body={"query": query, "variables": {"owner": self.owner, "name": self.repo}},
                )
                out.update(self._parse_graphql_blobs(chunk, parsed))
            return out
        return self._fetch_blobs_rest_public(missing)

    def _fetch_blobs_rest_public(self, missing: list[str]) -> dict[str, bytes]:
        self._ensure_tree()
        out: dict[str, bytes] = {}
        for rel_p in missing:
            blob_sha = self._blob_shas.get(rel_p)
            if not blob_sha or not _SHA40_RE.fullmatch(blob_sha):
                continue
            payload = self._request_json("GET", f"/repos/{self.owner}/{self.repo}/git/blobs/{blob_sha}")
            content = _decode_rest_blob_payload(rel_p, payload, blob_sha)
            if content is not None:
                out[rel_p] = content
        return out

    def _search_code_remote(self, needle: str, limit: int) -> list[str]:
        if not self._token:
            self.grep_note = "github code search requires authentication; pass explicit file:symbol anchors"
            return []
        payload = self._request_json(
            "GET",
            "/search/code",
            params={"q": f"{needle} repo:{self.owner}/{self.repo}", "per_page": str(max(1, min(limit, 100)))},
        )
        items = payload.get("items")
        if not isinstance(items, list):
            return []
        return [str(item.get("path", "")) for item in items if isinstance(item, dict)]
