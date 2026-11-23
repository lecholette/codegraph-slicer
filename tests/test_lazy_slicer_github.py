from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from codegraph_slicer.cli import main
from codegraph_slicer.lazy_slicer import lazy_slice_context
from codegraph_slicer.source_provider import BlobCache, GitHubCliProvider


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
    def fake_run(cmd, **kwargs):
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
