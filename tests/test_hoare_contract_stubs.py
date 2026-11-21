"""TDD tests for polyglot Hoare-contract stub annotations (@requires/@raises/@mutates) in slice."""
from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from codegraph_slicer.lazy_slicer import lazy_slice_context
from codegraph_slicer.slicer import slice_context
from codegraph_slicer.store import GraphStore, sync_atomically


def _write(root: Path, rel: str, content: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def test_python_contracts_stub_requires_literal_raises_1hop_and_mutates(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(
        repo,
        "config_service/store.py",
        '''"""Config store with multi-method threshold for auto-summarization."""
from typing import Any


class errors:
    @staticmethod
    def not_found(msg: str) -> Exception:
        return LookupError(msg)

    @staticmethod
    def invalid_input(msg: str) -> Exception:
        return ValueError(msg)


class ConfigStore:
    def __init__(self) -> None:
        self._entries: dict[tuple[str, str], dict[str, Any]] = {}
        self._cache: dict[tuple[str, str], dict[str, Any]] = {}

    def _ensure_valid_name(self, name: str) -> None:
        if not name:
            raise errors.invalid_input("config name required")

    def _get_entry(self, name: str, scope_key: str) -> dict[str, Any]:
        assert scope_key != ""
        self._ensure_valid_name(name)
        entry = self._entries.get((name, scope_key))
        if entry is None:
            raise errors.not_found("config not found")
        return entry

    def get_resolved(self, name: str, scope_key: str) -> dict[str, Any]:
        if (name, scope_key) in self._cache:
            return dict(self._cache[(name, scope_key)])
        entry = self._get_entry(name, scope_key)
        resolved = dict(entry)
        self._cache[(name, scope_key)] = resolved
        return resolved

    def put_entry(self, name: str, scope_key: str, payload: dict[str, Any]) -> None:
        self._ensure_valid_name(name)
        self._entries[(name, scope_key)] = dict(payload)
        self._writes: int = 0
        self._writes += 1
        self._cache.clear()

    def list_names(self) -> list[str]:
        """Return sorted unique config names across all registered scopes."""
        return sorted({k[0] for k in self._entries})

    def count_entries(self) -> int:
        """Return total number of scoped configuration records in store."""
        return len(self._entries)

    def clear_cache(self) -> None:
        """Invalidate all cached resolved configurations in memory."""
        self._cache.clear()
''',
    )
    _write(
        repo,
        "config_service/policies.py",
        '''from config_service.store import ConfigStore


class PolicyAPI:
    def __init__(self, store: ConfigStore) -> None:
        self.store = store

    def evaluate(self, name: str, scope_key: str) -> dict:
        cfg = self.store.get_resolved(name, scope_key)
        return {"ok": bool(cfg)}
''',
    )

    db_path = tmp_path / "graph.sqlite"
    sync_atomically(db_path, lambda s: s.sync_workspace(repo))

    with GraphStore(db_path, readonly=True) as store:
        off_res = slice_context(store, ["config_service.policies:PolicyAPI.evaluate"], max_tokens=8000)
        on_res = slice_context(
            store,
            ["config_service.policies:PolicyAPI.evaluate"],
            max_tokens=8000,
            contracts=True,
        )

    off_text = "\n".join(
        entry["text"] for group in off_res["sections"].values() for entry in group
    )
    assert "@requires" not in off_text
    assert "@raises" not in off_text
    assert "@mutates" not in off_text

    on_text = "\n".join(
        entry["text"] for group in on_res["sections"].values() for entry in group
    )
    assert '@raises: errors.not_found("config not found")' in on_text
    assert "[via _get_entry]" in on_text
    assert "@mutates: self._cache" in on_text
    assert "@requires:" in on_text
    assert "@ensures" not in on_text


def test_python_caught_callee_exception_is_not_propagated(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _write(
        repo,
        "pkg/helpers.py",
        '''def parse_strict(raw: str) -> int:
    if not raw:
        raise ValueError("empty input")
    return int(raw)


def parse_safe(raw: str) -> int:
    """Long enough body (>1 lines) rendered as callee stub in downstream_dependencies."""
    try:
        return parse_strict(raw)
    except ValueError:
        return 0
''',
    )
    _write(
        repo,
        "pkg/caller.py",
        '''from pkg.helpers import parse_safe


def handle(raw: str) -> int:
    return parse_safe(raw)
''',
    )
    db_path = tmp_path / "graph.sqlite"
    sync_atomically(db_path, lambda s: s.sync_workspace(repo))
    with GraphStore(db_path, readonly=True) as store:
        on_res = slice_context(store, ["pkg.caller:handle"], max_tokens=8000, contracts=True)

    dep_texts = "\n".join(e["text"] for e in on_res["sections"]["downstream_dependencies"])
    assert "ValueError" not in dep_texts
    assert "@requires:" not in dep_texts


def test_java_contracts_stub_in_db_and_lazy_root(tmp_path: Path) -> None:
    repo = tmp_path / "java_repo"
    _write(
        repo,
        "src/main/java/com/acme/OrderValidator.java",
        '''package com.acme;

public class OrderValidator {
    private int validationCount = 0;

    public void validateAmount(int amount, String currency) {
        if (amount <= 0) {
            throw new IllegalArgumentException("E_SCHEMA: amount must be > 0");
        }
        if (currency == null) {
            throw new IllegalArgumentException("E_SCHEMA: currency required");
        }
        this.validationCount = this.validationCount + 1;
        int a = amount + 1;
        int b = a + 2;
        int c = b + 3;
        int d = c + 4;
        int e = d + 5;
        int f = e + 6;
        int g = f + 7;
        int h = g + 8;
        int i = h + 9;
        int j = i + 10;
        int k = j + 11;
    }
}
''',
    )
    _write(
        repo,
        "src/main/java/com/acme/CheckoutHandler.java",
        '''package com.acme;

public class CheckoutHandler {
    private final OrderValidator validator;

    public CheckoutHandler(OrderValidator validator) {
        this.validator = validator;
    }

    public String checkout(int amount, String currency) {
        this.validator.validateAmount(amount, currency);
        return "OK";
    }
}
''',
    )

    lazy_res = lazy_slice_context(
        repo,
        ["src/main/java/com/acme/CheckoutHandler.java:CheckoutHandler.checkout"],
        max_tokens=8000,
        contracts=True,
    )
    lazy_text = "\n".join(
        entry["text"] for group in lazy_res["sections"].values() for entry in group
    )
    assert '// @requires: not (amount <= 0)' in lazy_text
    assert '// @raises: IllegalArgumentException("E_SCHEMA: amount must be > 0")' in lazy_text
    assert "// @mutates: this.validationCount" in lazy_text


def test_ts_and_js_contracts_with_1hop_throw_propagation_and_cli_flag(tmp_path: Path) -> None:
    repo = tmp_path / "ts_repo"
    _write(
        repo,
        "src/revisionStore.ts",
        '''export class RevisionNotFoundError extends Error {}
export class ConflictError extends Error {}

export class RevisionStore {
  private revisions: Map<string, string> = new Map();
  private activeHead: string = "";

  private assertNoConflict(revId: string, force: boolean): void {
    if (!force && this.activeHead === "locked") {
      throw new ConflictError("ERR_CONFLICT");
    }
  }

  public applyRevision(revId: string, force: boolean = false): string {
    if (!revId) {
      throw new RevisionNotFoundError("ERR_REV_EMPTY");
    }
    this.assertNoConflict(revId, force);
    this.activeHead = revId;
    const step1 = revId.trim();
    const step2 = step1.toLowerCase();
    const step3 = step2 + ":v1";
    const step4 = step3 + ":ok";
    const step5 = step4.slice(0);
    const step6 = step5.slice(0);
    const step7 = step6.slice(0);
    const step8 = step7.slice(0);
    const step9 = step8.slice(0);
    return step9;
  }
}
''',
    )
    _write(
        repo,
        "src/controller.ts",
        '''import { RevisionStore } from "./revisionStore";

export class Controller {
  constructor(private store: RevisionStore) {}

  public handleApply(revId: string): string {
    return this.store.applyRevision(revId, false);
  }
}
''',
    )

    db_path = tmp_path / "ts.sqlite"
    sync_atomically(db_path, lambda s: s.sync_workspace(repo))

    t0 = time.perf_counter()
    with GraphStore(db_path, readonly=True) as store:
        base_res = slice_context(store, ["src/controller.ts:Controller.handleApply"], max_tokens=8000)
    t_base = (time.perf_counter() - t0) * 1000.0

    t1 = time.perf_counter()
    with GraphStore(db_path, readonly=True) as store:
        contract_res = slice_context(
            store,
            ["src/controller.ts:Controller.handleApply"],
            max_tokens=8000,
            contracts=True,
        )
    t_contracts = (time.perf_counter() - t1) * 1000.0
    assert (t_contracts - t_base) < 25.0

    txt = "\n".join(
        entry["text"] for group in contract_res["sections"].values() for entry in group
    )
    assert '// @requires: not (!revId)' in txt or '// @requires: revId' in txt
    assert '// @raises: RevisionNotFoundError("ERR_REV_EMPTY")' in txt
    assert '// @raises: ConflictError("ERR_CONFLICT") [via assertNoConflict]' in txt
    assert "// @mutates: this.activeHead" in txt

    # Verify CLI --contracts flag and CODEGRAPH_CONTRACTS=1 env parity
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "codegraph_slicer",
            "slice",
            "--db",
            str(db_path),
            "--contracts",
            "--format",
            "xml",
            "src/controller.ts:Controller.handleApply",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert "ERR_REV_EMPTY" in proc.stdout
    assert "ERR_CONFLICT" in proc.stdout

    env = os.environ.copy()
    env["CODEGRAPH_CONTRACTS"] = "1"
    proc_env = subprocess.run(
        [
            sys.executable,
            "-m",
            "codegraph_slicer",
            "slice",
            "--db",
            str(db_path),
            "--format",
            "xml",
            "src/controller.ts:Controller.handleApply",
        ],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    assert proc_env.stdout == proc.stdout


def test_java_and_js_caught_throws_are_not_propagated_1hop(tmp_path: Path) -> None:
    repo = tmp_path / "mixed_repo"
    _write(
        repo,
        "src/main/java/com/acme/SafeCaller.java",
        '''package com.acme;

public class SafeCaller {
    private int count = 0;

    private void risky(int x) {
        if (x < 0) {
            throw new IllegalArgumentException("bad_x");
        }
    }

    public int safeRun(int x) {
        try {
            this.risky(x);
        } catch (IllegalArgumentException ex) {
            this.count++;
        }
        int a = x + 1;
        int b = a + 1;
        int c = b + 1;
        int d = c + 1;
        int e = d + 1;
        int f = e + 1;
        int g = f + 1;
        int h = g + 1;
        int i = h + 1;
        int j = i + 1;
        int k = j + 1;
        int l = k + 1;
        return l;
    }
}
''',
    )
    _write(
        repo,
        "src/main/java/com/acme/App.java",
        '''package com.acme;

public class App {
    private final SafeCaller safe = new SafeCaller();

    public int entry(int x) {
        return this.safe.safeRun(x);
    }
}
''',
    )
    res = lazy_slice_context(
        repo,
        ["src/main/java/com/acme/App.java:App.entry"],
        max_tokens=8000,
        contracts=True,
    )
    txt = "\n".join(e["text"] for group in res["sections"].values() for e in group)
    assert "bad_x" not in txt
    assert "@requires:" not in txt
    assert "// @mutates: this.count" in txt


def test_xml_c0_control_sanitization_huge_int_safety_and_symlink_refusal(tmp_path: Path) -> None:
    import xml.etree.ElementTree as ET

    outside = tmp_path / "outside_secret.py"
    outside.write_text("SECRET = 'leak'\n", encoding="utf-8")

    repo = tmp_path / "sec_repo"
    hex_huge = "0x1" + ("0" * 1200)
    _write(
        repo,
        "pkg/callee.py",
        f'''def bad_callee(x: int) -> int:
    if x < 0:
        raise ValueError("bad\\x00null\\x1besc")
    if x > 999:
        raise RuntimeError({hex_huge})
    a = x + 1
    return a
''',
    )
    _write(
        repo,
        "pkg/entry.py",
        '''from pkg.callee import bad_callee
from pkg.escape_link import something


def run_entry(x: int) -> int:
    return bad_callee(x)
''',
    )
    (repo / "pkg" / "escape_link.py").symlink_to(outside)

    lazy_res = lazy_slice_context(repo, ["pkg/entry.py:run_entry"], max_tokens=8000, contracts=True)
    full_txt = "\n".join(e["text"] for g in lazy_res["sections"].values() for e in g)
    assert "SECRET" not in full_txt

    db_path = tmp_path / "sec.sqlite"
    (repo / "pkg" / "escape_link.py").unlink()
    _write(repo, "pkg/escape_link.py", "something = 1\n")
    sync_atomically(db_path, lambda s: s.sync_workspace(repo))

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "codegraph_slicer",
            "slice",
            "--db",
            str(db_path),
            "--contracts",
            "--format",
            "xml",
            "pkg.entry:run_entry",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    # Must parse as valid XML 1.0 with zero C0 control char errors
    root_el = ET.fromstring(proc.stdout)
    assert root_el.tag == "context"
    assert "\x00" not in proc.stdout
    assert "\x1b" not in proc.stdout


