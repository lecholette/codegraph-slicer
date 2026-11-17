from pathlib import Path
import json

from codegraph_slicer.cli import main
from codegraph_slicer.lazy_slicer import lazy_slice_context, lazy_slice_outline


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
