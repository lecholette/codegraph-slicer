from __future__ import annotations

import base64
import hashlib
import io
import json
import urllib.error
from pathlib import Path

import pytest

from codegraph_slicer.ast_extractor import CodeGraphError
from codegraph_slicer.lazy_slicer import lazy_slice_context
from codegraph_slicer.source_provider import BlobCache, GitHubHttpProvider, NoOpCache


def _git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(f"blob {len(data)}\0".encode("ascii") + data).hexdigest()


class _FakeHttpxResp:
    def __init__(self, status_code: int, payload: object, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text or (json.dumps(payload) if isinstance(payload, (dict, list)) else str(payload))

    def json(self):
        if isinstance(self._payload, ValueError):
            raise self._payload
        return self._payload


class _FakeHttpxClient:
    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls: list[tuple[str, str, dict, dict | None, dict | None]] = []
        self.closed = False

    def request(self, method: str, url: str, *, headers=None, params=None, json=None, timeout=None):
        self.calls.append((method, url, dict(headers or {}), params, json))
        return self._handler(method, url, headers or {}, params, json)

    def close(self) -> None:
        self.closed = True


def test_github_http_authenticated_graphql_and_cache(tmp_path: Path) -> None:
    commit = "a" * 40
    app_src = b"def greet(name: str) -> str:\n    return f'hi {name}'\n"
    caller_src = b"from app import greet\n\ndef run() -> str:\n    return greet('world')\n"
    app_sha = _git_blob_sha(app_src)
    caller_sha = _git_blob_sha(caller_src)

    def handler(method, url, headers, params, body):
        assert headers["Authorization"] == "Bearer ghp_secret123456"
        if url.endswith("/repos/acme/demo/commits/main") or url.endswith("/repos/acme/demo/commits/HEAD"):
            return _FakeHttpxResp(200, {"sha": commit})
        if url.endswith(f"/repos/acme/demo/git/trees/{commit}"):
            return _FakeHttpxResp(
                200,
                {
                    "truncated": False,
                    "tree": [
                        {"path": "app.py", "type": "blob", "sha": app_sha},
                        {"path": "runner.py", "type": "blob", "sha": caller_sha},
                    ],
                },
            )
        if url.endswith("/graphql"):
            return _FakeHttpxResp(
                200,
                {
                    "data": {
                        "repository": {
                            "f0": {"text": app_src.decode(), "byteSize": len(app_src), "isBinary": False},
                            "f1": {"text": caller_src.decode(), "byteSize": len(caller_src), "isBinary": False},
                        }
                    }
                },
            )
        if url.endswith("/search/code"):
            return _FakeHttpxResp(200, {"items": [{"path": "runner.py"}]})
        raise AssertionError(f"unexpected {method} {url}")

    client = _FakeHttpxClient(handler)
    cache = BlobCache(tmp_path / "cache")
    with GitHubHttpProvider("acme/demo", ref="main", token="ghp_secret123456", cache=cache, client=client) as p:
        assert "ghp_secret123456" not in repr(p)
        assert p.resolve_ref() == commit
        assert p.list_files() == ["app.py", "runner.py"]
        assert p.grep("greet") == ["runner.py"]
        out = p.read_files_batch(["app.py", "runner.py"])
        assert out == {"app.py": app_src, "runner.py": caller_src}
        assert p.coverage_metadata()["provider"] == "github_http"

    # Second provider hits BlobCache for tree + blobs (only commit SHA resolved)
    client2 = _FakeHttpxClient(handler)
    with GitHubHttpProvider("acme/demo", ref=commit, token="ghp_secret123456", cache=cache, client=client2) as p2:
        assert p2.list_files() == ["app.py", "runner.py"]
        assert p2.read_files_batch(["app.py", "runner.py"]) == {"app.py": app_src, "runner.py": caller_src}
        assert len(client2.calls) == 0


def test_github_http_unauthenticated_rest_blob_fallback_and_lazy_slice(tmp_path: Path) -> None:
    commit = "b" * 40
    app_src = b"def helper(x: int) -> int:\n    return x + 1\n"
    main_src = b"from app import helper\n\ndef entry(n: int) -> int:\n    return helper(n)\n"
    app_sha = _git_blob_sha(app_src)
    main_sha = _git_blob_sha(main_src)

    def handler(method, url, headers, params, body):
        assert "Authorization" not in headers
        if url.endswith(f"/repos/acme/pub/git/trees/{commit}"):
            return _FakeHttpxResp(
                200,
                {
                    "truncated": False,
                    "tree": [
                        {"path": "app.py", "type": "blob", "sha": app_sha},
                        {"path": "main.py", "type": "blob", "sha": main_sha},
                    ],
                },
            )
        if url.endswith(f"/git/blobs/{main_sha}"):
            return _FakeHttpxResp(200, {"encoding": "base64", "content": base64.b64encode(main_src).decode()})
        if url.endswith(f"/git/blobs/{app_sha}"):
            return _FakeHttpxResp(200, {"encoding": "base64", "content": base64.b64encode(app_src).decode()})
        raise AssertionError(f"unexpected {method} {url}")

    client = _FakeHttpxClient(handler)
    provider = GitHubHttpProvider("acme/pub", ref=commit, token=None, cache=NoOpCache(), client=client)
    result = lazy_slice_context(None, ["main.py:entry"], include_callers=False, provider=provider)
    assert result["coverage"]["provider"] == "github_http"
    assert result["coverage"]["indexed_files"] == 2
    assert any(s["symbol"].endswith(":entry") for s in result["sections"]["target_implementations"])
    # Unauthenticated grep skips remote code search gracefully
    assert provider.grep("nonexistent_sym") == []
    assert provider.grep_note is not None


def test_github_http_security_guards_redirects_redaction_and_invalid_token() -> None:
    with pytest.raises(CodeGraphError, match="invalid GitHub token"):
        GitHubHttpProvider("acme/demo", token="ghp_bad\nheader")

    def redirect_handler(method, url, headers, params, body):
        return _FakeHttpxResp(302, {}, text="Found")

    p_redir = GitHubHttpProvider(
        "acme/demo", ref="main", token="ghp_tok999", cache=NoOpCache(), client=_FakeHttpxClient(redirect_handler)
    )
    with pytest.raises(CodeGraphError, match="redirect refused"):
        p_redir.resolve_ref()

    def err_handler(method, url, headers, params, body):
        return _FakeHttpxResp(401, {}, text="bad token ghp_tok999")

    p_err = GitHubHttpProvider(
        "acme/demo", ref="main", token="ghp_tok999", cache=NoOpCache(), client=_FakeHttpxClient(err_handler)
    )
    with pytest.raises(CodeGraphError) as exc_info:
        p_err.resolve_ref()
    assert "ghp_tok999" not in str(exc_info.value)
    assert "[REDACTED]" in str(exc_info.value)


def test_github_http_urllib_fallback_path(monkeypatch) -> None:
    commit = "c" * 40

    class _FakeResp:
        def __init__(self, data: bytes) -> None:
            self._data = data

        def read(self) -> bytes:
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return None

    class _FakeOpener:
        def open(self, req, timeout=None):
            if req.full_url.endswith("/commits/main"):
                return _FakeResp(json.dumps({"sha": commit}).encode())
            if "/graphql" in req.full_url:
                return _FakeResp(json.dumps({"data": {"repository": {}}}).encode())
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b"not found"))

    provider = GitHubHttpProvider("acme/demo", ref="main", token="ghp_test123", cache=NoOpCache())
    monkeypatch.setattr(provider, "_get_httpx_client", lambda: None)
    monkeypatch.setattr("codegraph_slicer.source_provider.urllib.request.build_opener", lambda *_: _FakeOpener())

    assert provider.resolve_ref() == commit
    assert provider._request_via_urllib("POST", "/graphql", params={"a": "1"}, json_body={"query": "{}"}) == {
        "data": {"repository": {}}
    }
    with pytest.raises(CodeGraphError, match="404"):
        provider.list_files()
