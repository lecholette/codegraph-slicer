from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codegraph_slicer.cli import main
from codegraph_slicer.lazy_slicer import lazy_slice_context
from codegraph_slicer.source_provider import BlobCache, GitHubCliProvider, LocalGitProvider


def _make_java_files() -> dict[str, str]:
    return {
        "src/main/java/com/acme/Model.java": (
            'package com.acme;\npublic class Model {\n    public String id() { return "x"; }\n}\n'
        ),
        "src/main/java/com/acme/Service.java": (
            "package com.acme;\nimport com.acme.Model;\n"
            "public class Service {\n    public String run(Model m) { return m.id(); }\n}\n"
        ),
        "src/main/java/com/acme/Caller.java": (
            "package com.acme;\nimport com.acme.Service;\n"
            "public class Caller {\n    public String invoke(Service s, Model m) { return s.run(m); }\n}\n"
        ),
    }


def _mock_gh_for_files(monkeypatch, files: dict[str, str], commit: str):
    real_run = subprocess.run

    def fake_run(cmd, **kwargs):
        if not cmd or cmd[0] != "gh":
            return real_run(cmd, **kwargs)
        if cmd[:3] == ["gh", "api", f"repos/acme/svc/commits/HEAD"]:
            return subprocess.CompletedProcess(cmd, 0, stdout=commit + "\n", stderr="")
        if cmd[:3] == ["gh", "api", f"repos/acme/svc/git/trees/{commit}?recursive=1"]:
            tree = [
                {"path": p, "type": "blob", "sha": f"sha_{i}"}
                for i, p in enumerate(sorted(files))
            ]
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"sha": commit, "truncated": False, "tree": tree}), stderr=""
            )
        if cmd[:3] == ["gh", "api", "graphql"]:
            query = cmd[cmd.index("-f") + 1 :]
            # Extract query string from -f query=...
            q_arg = next(a for a in cmd if isinstance(a, str) and a.startswith("query="))
            import re

            aliases = re.findall(r'(f\d+):\s*object\(expression:\s*"([^"]+)"\)', q_arg)
            repo_data = {}
            for alias, expr in aliases:
                _, rel_p = expr.split(":", 1)
                text = files.get(rel_p)
                if text is not None:
                    repo_data[alias] = {"text": text, "byteSize": len(text), "isBinary": False}
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps({"data": {"repository": repo_data}}), stderr=""
            )
        if cmd[:3] == ["gh", "search", "code"]:
            return subprocess.CompletedProcess(
                cmd, 0, stdout=json.dumps([{"path": "src/main/java/com/acme/Caller.java"}]), stderr=""
            )
        raise AssertionError(f"unexpected gh command: {cmd}")

    monkeypatch.setattr("codegraph_slicer.source_provider.subprocess.run", fake_run)


def test_lazy_slice_github_parity_with_local(tmp_path: Path, monkeypatch) -> None:
    files = _make_java_files()
    root = tmp_path / "repo"
    for rel_p, content in files.items():
        full = root / rel_p
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(content, encoding="utf-8")

    commit = "a" * 40
    _mock_gh_for_files(monkeypatch, files, commit)

    local_res = lazy_slice_context(
        root,
        ["src/main/java/com/acme/Service.java:Service.run"],
        max_files=10,
        max_hops=2,
        include_callers=False,
    )
    gh_provider = GitHubCliProvider("acme/svc", ref=commit, cache=BlobCache(tmp_path / "cache"))
    gh_res = lazy_slice_context(
        None,
        ["src/main/java/com/acme/Service.java:Service.run"],
        max_files=10,
        max_hops=2,
        include_callers=False,
        provider=gh_provider,
    )

    assert [x["symbol"] for x in gh_res["sections"]["target_implementations"]] == [
        x["symbol"] for x in local_res["sections"]["target_implementations"]
    ]
    assert [x["text"] for x in gh_res["sections"]["target_implementations"]] == [
        x["text"] for x in local_res["sections"]["target_implementations"]
    ]
    assert gh_res["coverage"]["provider"] == "github_cli"
    assert gh_res["coverage"]["commit"] == commit
    assert gh_res["coverage"]["api_calls"] >= 2


def test_cli_slice_repo_and_formats(tmp_path: Path, monkeypatch, capsys) -> None:
    files = _make_java_files()
    commit = "b" * 40
    _mock_gh_for_files(monkeypatch, files, commit)
    monkeypatch.setenv("CODEGRAPH_SLICER_CACHE_DIR", str(tmp_path / "cache"))

    for fmt, expected in (("json", '"github_cli"'), ("xml", 'provider="github_cli"'), ("markdown", "github_cli")):
        rc = main(
            [
                "slice",
                "--repo",
                "acme/svc",
                "--ref",
                commit,
                "--no-callers",
                "src/main/java/com/acme/Service.java:Service.run",
                "--format",
                fmt,
            ]
        )
        assert rc == 0
        out = capsys.readouterr().out
        assert expected in out


def test_cli_slice_root_and_repo_fallback(tmp_path: Path, monkeypatch, capsys) -> None:
    files = _make_java_files()
    commit = "c" * 40
    _mock_gh_for_files(monkeypatch, files, commit)
    monkeypatch.setenv("CODEGRAPH_SLICER_CACHE_DIR", str(tmp_path / "cache"))

    # Non-existent --root with --repo falls back to github_cli
    rc = main(
        [
            "slice",
            "--root",
            str(tmp_path / "missing"),
            "--repo",
            "acme/svc",
            "--ref",
            commit,
            "--no-callers",
            "src/main/java/com/acme/Service.java:Service.run",
            "--format",
            "json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["coverage"]["provider"] == "github_cli"


def test_cli_source_mode_origin_mismatch_and_no_cache(tmp_path: Path, monkeypatch, capsys) -> None:
    files = _make_java_files()
    commit = "d" * 40
    _mock_gh_for_files(monkeypatch, files, commit)

    local_repo = tmp_path / "local_repo"
    local_repo.mkdir()
    subprocess.run(["git", "-C", str(local_repo), "init"], check=True, stdout=subprocess.PIPE)
    subprocess.run(["git", "-C", str(local_repo), "remote", "add", "origin", "https://github.com/other/repo.git"], check=True)

    # Mismatched origin fails closed in auto mode
    with pytest.raises(SystemExit, match="2"):
        main(["slice", "--root", str(local_repo), "--repo", "acme/svc", "--ref", commit, "Service.java:Service.run"])
    assert "does not match --repo" in capsys.readouterr().err

    # --source-mode remote bypasses mismatched --root, and --no-cache skips SQLite creation
    cache_dir = tmp_path / "nocache"
    rc = main(
        [
            "slice",
            "--root",
            str(local_repo),
            "--repo",
            "acme/svc",
            "--ref",
            commit,
            "--source-mode",
            "remote",
            "--cache-dir",
            str(cache_dir),
            "--no-cache",
            "--no-callers",
            "src/main/java/com/acme/Service.java:Service.run",
            "--format",
            "json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["coverage"]["provider"] == "github_cli"
    assert payload["coverage"]["caller_discovery"] == "skipped"
    assert not (cache_dir / "index.sqlite").exists()

    # --no-cache + --refresh-cache is rejected
    with pytest.raises(SystemExit, match="2"):
        main(["slice", "--repo", "acme/svc", "--no-cache", "--refresh-cache", "Service.java:Service.run"])
    capsys.readouterr()


def test_cli_unknown_origin_rejected_in_local_and_falls_back_in_auto(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    plain_dir = tmp_path / "plain_dir"
    plain_dir.mkdir()
    (plain_dir / "a.py").write_text("def hello():\n    return 1\n", encoding="utf-8")

    with pytest.raises(SystemExit, match="2"):
        main(
            [
                "slice",
                "--root",
                str(plain_dir),
                "--repo",
                "acme/svc",
                "--source-mode",
                "local",
                "a.py:hello",
            ]
        )
    assert "no verifiable GitHub origin" in capsys.readouterr().err

    files = _make_java_files()
    commit = "f" * 40
    _mock_gh_for_files(monkeypatch, files, commit)
    monkeypatch.setenv("CODEGRAPH_SLICER_CACHE_DIR", str(tmp_path / "cache"))
    rc = main(
        [
            "slice",
            "--root",
            str(plain_dir),
            "--repo",
            "acme/svc",
            "--ref",
            commit,
            "--no-callers",
            "com.acme.Service:Service.run",
            "--format",
            "json",
        ]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["coverage"]["provider"] == "github_cli"
    assert payload["sections"]["target_implementations"][0]["symbol"] == "com.acme.Service:Service.run"


def test_pinned_local_tsconfig_uses_commit_snapshot_and_remote_uses_base(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "ts_repo"
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "-C", str(repo), "config", "user.email", "t@example.com"], check=True)
    subprocess.run(["git", "-C", str(repo), "config", "user.name", "T"], check=True)
    (repo / "old").mkdir()
    (repo / "new").mkdir()
    (repo / "old" / "helper.ts").write_text("export function getVal() { return 1; }\n", encoding="utf-8")
    (repo / "new" / "helper.ts").write_text("export function getVal() { return 2; }\n", encoding="utf-8")
    (repo / "tsconfig.json").write_text(
        json.dumps({"compilerOptions": {"baseUrl": ".", "paths": {"@h/*": ["old/*"]}}}),
        encoding="utf-8",
    )
    (repo / "app.ts").write_text(
        "import { getVal } from '@h/helper';\nexport function run() { return getVal(); }\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-m", "init"], check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    sha = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, stdout=subprocess.PIPE, text=True).stdout.strip()

    # Mutate working-tree tsconfig.json without committing
    (repo / "tsconfig.json").write_text(
        json.dumps({"compilerOptions": {"baseUrl": ".", "paths": {"@h/*": ["new/*"]}}}),
        encoding="utf-8",
    )
    res_local = lazy_slice_context(repo, ["app.ts:run"], include_callers=False, provider=LocalGitProvider(repo, ref=sha))
    syms_local = {d["symbol"] for d in res_local["sections"]["downstream_dependencies"]}
    assert "old.helper:getVal" in syms_local
    assert "new.helper:getVal" not in syms_local

    # Remote snapshot with tsconfig.json extending tsconfig.base.json
    remote_files = {
        "tsconfig.json": '{"extends": "./tsconfig.base.json"}',
        "tsconfig.base.json": '{"compilerOptions": {"baseUrl": ".", "paths": {"@h/*": ["old/*"]}}}',
        "old/helper.ts": "export function getVal() { return 1; }\n",
        "app.ts": "import { getVal } from '@h/helper';\nexport function run() { return getVal(); }\n",
    }
    _mock_gh_for_files(monkeypatch, remote_files, sha)
    p = GitHubCliProvider("acme/svc", ref=sha, cache=BlobCache(tmp_path / "rcache"))
    res_remote = lazy_slice_context(None, ["app.ts:run"], provider=p, include_callers=False)
    assert any(d["symbol"] == "old.helper:getVal" for d in res_remote["sections"]["downstream_dependencies"])
