from pathlib import Path
import json
import pytest

from codegraph_slicer import GraphStore
from codegraph_slicer.ast_extractor import CodeGraphError, CodeGraphParseError, is_test_path
from codegraph_slicer.cli import main
from codegraph_slicer.lazy_slicer import lazy_slice_context, lazy_slice_outline
from codegraph_slicer.slicer import serialized_tokens


def _make_java_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    pkg = root / "src/main/java/com/acme"
    pkg.mkdir(parents=True)
    (pkg / "Model.java").write_text(
        "package com.acme;\npublic class Model {\n    public String id() { return \"x\"; }\n}\n",
        encoding="utf-8",
    )
    (pkg / "Service.java").write_text(
        "package com.acme;\nimport com.acme.Model;\npublic class Service {\n    public String run(Model m) { return m.id(); }\n}\n",
        encoding="utf-8",
    )
    (pkg / "Caller.java").write_text(
        "package com.acme;\nimport com.acme.Service;\npublic class Caller {\n    public String invoke(Service s, Model m) { return s.run(m); }\n}\n",
        encoding="utf-8",
    )
    return root


def test_lazy_slice_context_expands_deps_and_callers_without_db(tmp_path: Path) -> None:
    root = _make_java_repo(tmp_path)
    res = lazy_slice_context(
        root,
        ["src/main/java/com/acme/Service.java:Service.run"],
        max_files=10,
        max_hops=2,
    )
    assert res["coverage"]["mode"] == "lazy_target_seeded"
    assert res["coverage"]["explored_files"] == 3
    targets = {item["symbol"] for item in res["sections"]["target_implementations"]}
    assert any(t.endswith(":Service.run") for t in targets)
    callers = {item["symbol"] for item in res["sections"]["upstream_callers"]}
    assert any(c.endswith(":Caller.invoke") for c in callers)


def test_lazy_slice_cli_root_without_db(tmp_path: Path, capsys) -> None:
    root = _make_java_repo(tmp_path)
    rc = main(
        [
            "slice",
            "--root",
            str(root),
            "src/main/java/com/acme/Service.java:Service.run",
            "--format",
            "json",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["coverage"]["mode"] == "lazy_target_seeded"
    assert out["sections"]["target_implementations"]


def test_lazy_slice_outline_without_db(tmp_path: Path) -> None:
    root = _make_java_repo(tmp_path)
    out = lazy_slice_outline(
        root,
        ["src/main/java/com/acme/Service.java:Service.run"],
        max_files=10,
    )
    assert out["coverage"]["mode"] == "lazy_target_seeded"
    assert out["entities"]


def test_lazy_slice_xml_and_markdown_render_coverage(tmp_path: Path, capsys) -> None:
    root = _make_java_repo(tmp_path)
    for fmt, marker in (("xml", "<coverage "), ("markdown", "## Coverage")):
        rc = main(
            [
                "slice",
                "--root",
                str(root),
                "src/main/java/com/acme/Service.java:Service.run",
                "--format",
                fmt,
            ]
        )
        assert rc == 0
        assert marker in capsys.readouterr().out
        rc_out = main(
            [
                "slice",
                "--root",
                str(root),
                "src/main/java/com/acme/Service.java:Service.run",
                "--outline",
                "--format",
                fmt,
            ]
        )
        assert rc_out == 0
        assert marker in capsys.readouterr().out


def test_lazy_slice_raises_on_unparseable_seed_and_records_unparseable_caller(tmp_path: Path, capsys) -> None:
    root = _make_java_repo(tmp_path)
    (root / "src/main/java/com/acme/Broken.java").write_text("public class Broken { @@@", encoding="utf-8")
    with pytest.raises(CodeGraphParseError):
        lazy_slice_context(root, ["src/main/java/com/acme/Broken.java:1"])

    (root / "src/main/java/com/acme/BrokenCaller.java").write_text(
        "package com.acme;\npublic class BrokenCaller { void f() { run(); @@@ } }\n",
        encoding="utf-8",
    )
    res = lazy_slice_context(root, ["src/main/java/com/acme/Service.java:Service.run"])
    err = capsys.readouterr().err
    assert "skipping unparseable file" in err
    assert any(f["path"].endswith("BrokenCaller.java") for f in res["coverage"]["failed_files"])


def test_sync_scope_filters_files_and_assert_fresh(tmp_path: Path) -> None:
    root = tmp_path / "mono"
    (root / "mod-a/src/main/java/com/acme").mkdir(parents=True)
    (root / "mod-b/src/main/java/com/acme").mkdir(parents=True)
    (root / "mod-a/src/main/java/com/acme/A.java").write_text("package com.acme;\npublic class A {}\n", encoding="utf-8")
    (root / "mod-b/src/main/java/com/acme/B.java").write_text("package com.acme;\npublic class B {}\n", encoding="utf-8")
    db = tmp_path / "scoped.sqlite"
    assert main(["sync", str(root), "--scope", "mod-a", "--db", str(db)]) == 0
    with GraphStore(db, readonly=True) as store:
        paths = [r[0] for r in store.db.execute("SELECT path FROM files").fetchall()]
        assert paths == ["mod-a/src/main/java/com/acme/A.java"]
        (root / "mod-b/src/main/java/com/acme/B.java").write_text("package com.acme;\npublic class B { int x; }\n", encoding="utf-8")
        store.assert_fresh()


def test_java_dash_test_module_classified_as_test() -> None:
    assert is_test_path("flights-common-test/src/main/java/com/acme/Helper.java") is True
    assert is_test_path("my-tests/src/main/java/com/acme/Helper.java") is True
    assert is_test_path("contest/src/main/java/com/acme/App.java") is False


def test_scope_rejects_slash_and_lazy_json_budget_accounts_for_coverage(tmp_path: Path) -> None:
    root = _make_java_repo(tmp_path)
    db = tmp_path / "bad.sqlite"
    with GraphStore(db) as store:
        with pytest.raises(CodeGraphError):
            store.sync_workspace(root, scope=["/"])

    res = lazy_slice_context(
        root,
        ["src/main/java/com/acme/Service.java:Service.run"],
        max_tokens=300,
        include_callers=False,
    )
    assert res["tokens"] == serialized_tokens(res)
    assert res["tokens"] <= 300
