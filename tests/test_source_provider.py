from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from codegraph_slicer.ast_extractor import CodeGraphError
from codegraph_slicer.source_provider import (
    BlobCache,
    GitHubCliProvider,
    LocalGitProvider,
    parse_github_repo,
)


def test_parse_github_repo_accepts_slug_and_urls() -> None:
    assert parse_github_repo("navancorp/sabre-flight-svc") == ("navancorp", "sabre-flight-svc")
    assert parse_github_repo("https://github.com/navancorp/sabre-flight-svc.git") == (
        "navancorp",
        "sabre-flight-svc",
    )
    assert parse_github_repo("git@github.com:navancorp/sabre-flight-svc.git") == (
        "navancorp",
        "sabre-flight-svc",
    )
    with pytest.raises(CodeGraphError):
        parse_github_repo("not a valid repo!!")


def test_blob_cache_roundtrip_ttl_and_lru_eviction(tmp_path: Path) -> None:
    now = 1_000_000.0
    tree_cache = BlobCache(tmp_path / "tree_cache", max_bytes=1024, clock=lambda: now)

    tree_cache.put_tree("acme", "svc", "a" * 40, {"paths": ["a.py"], "truncated": False})
    assert tree_cache.get_tree("acme", "svc", "a" * 40) == {"paths": ["a.py"], "truncated": False}

    # Expire tree after 30 days
    now += 31 * 86400
    assert tree_cache.get_tree("acme", "svc", "a" * 40) is None

    # Blob LRU eviction under 20-byte budget
    cache = BlobCache(tmp_path / "cache", max_bytes=20, clock=lambda: now)
    cache.put_blob("sha1", b"0123456789")  # 10 bytes
    now += 1
    cache.put_blob("sha2", b"abcdefghij")  # 10 bytes -> total 20
    now += 1
    assert cache.get_blob("sha1") == b"0123456789"  # touch sha1 so sha2 is LRU
    now += 1
    cache.put_blob("sha3", b"klmnopqrst")  # 10 bytes -> evicts sha2
    assert cache.get_blob("sha2") is None
    assert cache.get_blob("sha1") == b"0123456789"
    assert cache.get_blob("sha3") == b"klmnopqrst"

    # Unreferenced blob expires after 90 days
    now += 91 * 86400
    assert cache.get_blob("sha1") is None


def test_github_cli_provider_caches_tree_and_batches_graphql(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    commit = "1" * 40

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["gh", "api", f"repos/acme/svc/git/trees/{commit}?recursive=1"]:
            payload = {
                "sha": commit,
                "truncated": True,
                "tree": [
                    {"path": "src/App.java", "type": "blob", "sha": "blob_app"},
                    {"path": "src/Helper.java", "type": "blob", "sha": "blob_helper"},
                    {"path": "README.md", "type": "blob", "sha": "blob_readme"},
                    {"path": "src", "type": "tree", "sha": "tree_src"},
                ],
            }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")
        if cmd[:3] == ["gh", "api", "graphql"]:
            # Return both requested aliases
            resp = {
                "data": {
                    "repository": {
                        "f0": {"text": "class App {}", "byteSize": 12, "isBinary": False},
                        "f1": {"text": "class Helper {}", "byteSize": 15, "isBinary": False},
                    }
                }
            }
            return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(resp), stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr("codegraph_slicer.source_provider.subprocess.run", fake_run)
    cache = BlobCache(tmp_path / "cache")

    provider = GitHubCliProvider("acme/svc", ref=commit, cache=cache)
    assert provider.resolve_ref() == commit
    assert calls == []  # 40-char SHA needs no API call

    files = provider.list_files()
    assert files == ["src/App.java", "src/Helper.java"]
    assert provider.tree_truncated is True
    assert len(calls) == 1

    # Batch read both files -> 1 GraphQL call
    blobs = provider.read_files_batch(["src/App.java", "src/Helper.java"])
    assert blobs == {
        "src/App.java": b"class App {}",
        "src/Helper.java": b"class Helper {}",
    }
    assert len(calls) == 2

    # Second provider instance on same commit hits tree + blob cache with 0 API calls
    provider2 = GitHubCliProvider("acme/svc", ref=commit, cache=cache)
    assert provider2.list_files() == ["src/App.java", "src/Helper.java"]
    assert provider2.read_files_batch(["src/App.java", "src/Helper.java"]) == blobs
    assert len(calls) == 2
    assert provider2.cache_hits >= 3


def test_github_cli_provider_resolves_branch_and_handles_gh_error(tmp_path: Path, monkeypatch) -> None:
    commit = "2" * 40

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["gh", "api", "repos/acme/svc/commits/main"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=commit + "\n", stderr="")
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="HTTP 404: Not Found")

    monkeypatch.setattr("codegraph_slicer.source_provider.subprocess.run", fake_run)
    provider = GitHubCliProvider("acme/svc", ref="main", cache=BlobCache(tmp_path / "cache"))
    assert provider.resolve_ref() == commit
    with pytest.raises(CodeGraphError, match="gh api failed"):
        provider.list_files()


def test_github_cli_provider_grep_skips_code_search_on_pinned_sha(tmp_path: Path, monkeypatch) -> None:
    calls: list[list[str]] = []
    commit = "3" * 40

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["gh", "search", "code"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps([{"path": "src/Caller.java"}]), stderr=""
            )
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr("codegraph_slicer.source_provider.subprocess.run", fake_run)
    cache = BlobCache(tmp_path / "cache")
    cache.put_tree("acme", "svc", commit, {"paths": ["src/A.java", "src/Caller.java"], "truncated": False})
    cache.put_blob("sha_a", b"class A { void needle() {} }")

    pinned = GitHubCliProvider("acme/svc", ref=commit, cache=cache)
    assert pinned.grep("needle", limit=10) == []
    assert calls == []  # pinned commit does not query default-branch gh search code

    head = GitHubCliProvider("acme/svc", ref="HEAD", cache=cache)
    head._commit = commit
    assert head.grep("needle", limit=10) == ["src/Caller.java"]
    assert len(calls) == 1


def test_local_git_provider_basic(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/a.py").write_text("def hello():\n    return 1\n", encoding="utf-8")
    provider = LocalGitProvider(repo)
    assert provider.file_exists("src/a.py") is True
    assert provider.read_files_batch(["src/a.py"]) == {"src/a.py": b"def hello():\n    return 1\n"}
    assert provider.grep("hello", limit=5) == ["src/a.py"]


def test_local_git_provider_explicit_ref_reads_commit_not_dirty_workspace(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src/a.py").write_text("def v1():\n    return 1\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "test@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "Test"], check=True)
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, stdout=subprocess.PIPE)
    sha = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()

    # Dirty the workspace and add an untracked file
    (repo / "src/a.py").write_text("def dirty():\n    return 2\n", encoding="utf-8")
    (repo / "src/untracked.py").write_text("def untracked():\n    return 3\n", encoding="utf-8")

    pinned = LocalGitProvider(repo, ref=sha)
    assert pinned.resolve_ref() == sha
    assert pinned.list_files() == ["src/a.py"]
    assert pinned.file_exists("src/untracked.py") is False
    assert pinned.read_files_batch(["src/a.py"]) == {"src/a.py": b"def v1():\n    return 1\n"}
    assert pinned.grep("v1", limit=5) == ["src/a.py"]
    assert pinned.grep("dirty", limit=5) == []

    bad = LocalGitProvider(repo, ref="0" * 40)
    with pytest.raises(CodeGraphError, match="failed to resolve --ref"):
        bad.resolve_ref()


def test_github_cli_provider_skips_search_on_non_default_branch(tmp_path: Path, monkeypatch) -> None:
    branch_sha = "4" * 40
    head_sha = "5" * 40
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if cmd[:3] == ["gh", "api", "repos/acme/svc/commits/release%2Fold"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=branch_sha + "\n", stderr="")
        if cmd[:3] == ["gh", "api", "repos/acme/svc/commits/HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=head_sha + "\n", stderr="")
        raise AssertionError(f"unexpected command: {cmd}")

    monkeypatch.setattr("codegraph_slicer.source_provider.subprocess.run", fake_run)
    provider = GitHubCliProvider("acme/svc", ref="release/old", cache=BlobCache(tmp_path / "cache"))
    assert provider.grep("needle", limit=5) == []
    assert provider.grep_note is not None
    assert "non-default ref" in provider.grep_note
    assert all(c[:3] != ["gh", "search", "code"] for c in calls)


def test_github_cli_provider_validates_graphql_errors_bytesize_sha1_and_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    import hashlib

    commit = "6" * 40
    good_bytes = b"class Ok {}"
    good_sha = hashlib.sha1(f"blob {len(good_bytes)}\0".encode("ascii") + good_bytes).hexdigest()

    def run_errors(cmd, **kwargs):
        if "graphql" in cmd:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"errors": [{"message": "rate limit ghp_secret123"}]}), stderr=""
            )
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"sha": commit, "truncated": False, "tree": [{"path": "A.java", "type": "blob", "sha": good_sha}]}),
            stderr="",
        )

    monkeypatch.setattr("codegraph_slicer.source_provider.subprocess.run", run_errors)
    p1 = GitHubCliProvider("acme/svc", ref=commit, cache=BlobCache(tmp_path / "c1"))
    p1.list_files()
    with pytest.raises(CodeGraphError, match=r"\[REDACTED\]"):
        p1.read_files_batch(["A.java"])

    def run_bad_sha(cmd, **kwargs):
        if "graphql" in cmd:
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps({"data": {"repository": {"f0": {"text": "class Tampered {}", "byteSize": 17, "isBinary": False}}}}),
                stderr="",
            )
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"sha": commit, "truncated": False, "tree": [{"path": "A.java", "type": "blob", "sha": good_sha}]}),
            stderr="",
        )

    monkeypatch.setattr("codegraph_slicer.source_provider.subprocess.run", run_bad_sha)
    p2 = GitHubCliProvider("acme/svc", ref=commit, cache=BlobCache(tmp_path / "c2"))
    p2.list_files()
    with pytest.raises(CodeGraphError, match="blob SHA-1 mismatch"):
        p2.read_files_batch(["A.java"])

    def run_timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=5.0)

    monkeypatch.setattr("codegraph_slicer.source_provider.subprocess.run", run_timeout)
    p3 = GitHubCliProvider("acme/svc", ref="main", cache=BlobCache(tmp_path / "c3"), timeout=5.0)
    with pytest.raises(CodeGraphError, match="timed out"):
        p3.resolve_ref()


def test_gh_provider_enforces_github_host_and_url_encodes_ref(tmp_path: Path, monkeypatch) -> None:
    seen: list[tuple[list[str], str | None]] = []
    commit = "d" * 40

    def fake_run(cmd, **kwargs):
        seen.append((list(cmd), (kwargs.get("env") or {}).get("GH_HOST")))
        return subprocess.CompletedProcess(cmd, 0, stdout=f"{commit}\n", stderr="")

    monkeypatch.setenv("GH_HOST", "ghe.internal.example")
    monkeypatch.setattr("codegraph_slicer.source_provider.subprocess.run", fake_run)
    p = GitHubCliProvider("acme/svc", ref="release#2/{repo}", cache=BlobCache(tmp_path / "c"))
    assert p.resolve_ref() == commit
    assert seen[0][1] == "github.com"
    assert "commits/release%232%2F%7Brepo%7D" in seen[0][0][2]


def test_gh_provider_invalidates_corrupted_cached_blob_and_refetches(tmp_path: Path, monkeypatch) -> None:
    import hashlib

    real_bytes = b"class A { int ok = 1; }\n"
    real_sha = hashlib.sha1(f"blob {len(real_bytes)}\0".encode("ascii") + real_bytes).hexdigest()
    commit = "e" * 40
    cache = BlobCache(tmp_path / "cache")
    cache.put_blob(f"git:{real_sha}", b"class A { int tampered = 2; }\n")

    graphql_calls = 0

    def fake_run(cmd, **kwargs):
        nonlocal graphql_calls
        if "graphql" in cmd:
            graphql_calls += 1
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps(
                    {"data": {"repository": {"f0": {"text": real_bytes.decode("utf-8"), "byteSize": len(real_bytes), "isBinary": False}}}}
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"sha": commit, "truncated": False, "tree": [{"path": "A.java", "type": "blob", "sha": real_sha}]}),
            stderr="",
        )

    monkeypatch.setattr("codegraph_slicer.source_provider.subprocess.run", fake_run)
    p = GitHubCliProvider("acme/svc", ref=commit, cache=cache)
    p.list_files()
    files = p.read_files_batch(["A.java"])
    assert files["A.java"] == real_bytes
    assert graphql_calls == 1
    assert cache.get_blob(f"git:{real_sha}") == real_bytes


def test_blob_cache_accounts_for_tree_size_and_reclaims_expired_trees(tmp_path: Path) -> None:
    now = 1000.0
    cache = BlobCache(tmp_path / "cache", max_bytes=300, tree_ttl_seconds=10, clock=lambda: now)
    for i in range(6):
        cache.put_tree("acme", "svc", f"{i}" * 40, {"paths": [f"src/file_{i}_" + ("x" * 60) + ".py"]})
    with cache._connect() as conn:
        total_trees = conn.execute("SELECT COALESCE(SUM(size_bytes), 0) FROM trees").fetchone()[0]
    assert total_trees <= 300

    now = 2000.0
    cache.put_blob("git:" + "f" * 40, b"x" * 20)
    with cache._connect() as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM trees").fetchone()[0]
    assert remaining == 0
