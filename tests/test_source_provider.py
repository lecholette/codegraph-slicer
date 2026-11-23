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
    cache = BlobCache(tmp_path / "cache", max_bytes=20, clock=lambda: now)

    cache.put_tree("acme", "svc", "a" * 40, {"paths": ["a.py"], "truncated": False})
    assert cache.get_tree("acme", "svc", "a" * 40) == {"paths": ["a.py"], "truncated": False}

    # Expire tree after 30 days
    now += 31 * 86400
    assert cache.get_tree("acme", "svc", "a" * 40) is None

    # Blob LRU eviction under 20-byte budget
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
