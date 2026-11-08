from codegraph_slicer import GraphStore, compute_delta, rank, slice_context


def _write_files(root, files: dict[str, str]):
    for rel, content in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")


def test_probe1_container_vs_element_type(tmp_path):
    _write_files(
        tmp_path,
        {
            "pkg/items.py": (
                "class Item:\n"
                "    def ping(self) -> int:\n"
                "        return 1\n"
                "\n"
                "def bad_container_call(items: list[Item]) -> None:\n"
                "    items.ping()\n"
                "\n"
                "def good_iter_and_index(items: list[Item]) -> int:\n"
                "    total = items[0].ping()\n"
                "    for x in items:\n"
                "        total += x.ping()\n"
                "    return total\n"
            ),
        },
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        calls = {
            (r["source_id"], r["target_id"])
            for r in store.rows("SELECT source_id,target_id FROM edges WHERE kind='CALLS'")
        }
        assert ("pkg.items:bad_container_call", "pkg.items:Item.ping") not in calls
        assert ("pkg.items:good_iter_and_index", "pkg.items:Item.ping") in calls


def test_probe2_abc_vs_protocol_structural_matching(tmp_path):
    _write_files(
        tmp_path,
        {
            "pkg/contracts.py": (
                "from abc import ABC, abstractmethod\n"
                "from typing import Protocol\n"
                "\n"
                "class AbstractWorker(ABC):\n"
                "    @abstractmethod\n"
                "    def run(self, x: int) -> int:\n"
                "        ...\n"
                "\n"
                "class UnrelatedCoincidence:\n"
                "    def run(self, x: int) -> int:\n"
                "        return x\n"
                "\n"
                "class NominalWorker(AbstractWorker):\n"
                "    def run(self, x: int) -> int:\n"
                "        return x + 1\n"
                "\n"
                "class RunnerProto(Protocol):\n"
                "    def step(self, a: int) -> int:\n"
                "        ...\n"
                "\n"
                "class StructuralMatch:\n"
                "    def step(self, a: int) -> int:\n"
                "        return a * 2\n"
                "\n"
                "class WrongArity:\n"
                "    def step(self, a: int, b: int) -> int:\n"
                "        return a + b\n"
                "\n"
                "def invoke_abc(w: AbstractWorker) -> int:\n"
                "    return w.run(1)\n"
                "\n"
                "def invoke_proto(p: RunnerProto) -> int:\n"
                "    return p.step(1)\n"
            ),
        },
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        calls = {
            (r["source_id"], r["target_id"])
            for r in store.rows("SELECT source_id,target_id FROM edges WHERE kind='CALLS'")
        }
        assert ("pkg.contracts:invoke_abc", "pkg.contracts:NominalWorker.run") in calls
        assert ("pkg.contracts:invoke_abc", "pkg.contracts:UnrelatedCoincidence.run") not in calls
        assert ("pkg.contracts:invoke_proto", "pkg.contracts:StructuralMatch.step") in calls
        assert ("pkg.contracts:invoke_proto", "pkg.contracts:WrongArity.step") not in calls


def test_probe3_diamond_multiple_inheritance_c3_mro(tmp_path):
    _write_files(
        tmp_path,
        {
            "pkg/diamond.py": (
                "class A:\n"
                "    def run(self) -> int:\n"
                "        return 1\n"
                "\n"
                "class B(A):\n"
                "    pass\n"
                "\n"
                "class C(A):\n"
                "    def run(self) -> int:\n"
                "        return 2\n"
                "\n"
                "class D(B, C):\n"
                "    def work(self) -> int:\n"
                "        return self.run()\n"
            ),
        },
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        calls = {
            (r["source_id"], r["target_id"])
            for r in store.rows("SELECT source_id,target_id FROM edges WHERE kind='CALLS'")
        }
        assert ("pkg.diamond:D.work", "pkg.diamond:C.run") in calls
        assert ("pkg.diamond:D.work", "pkg.diamond:A.run") not in calls


def test_probe4_import_only_test_does_not_suppress_untested_modified_symbols(tmp_path):
    base_root = tmp_path / "base"
    head_root = tmp_path / "head"
    _write_files(
        base_root,
        {
            "pkg/mod.py": "def work() -> int:\n    return 1\n",
            "tests/test_x.py": "from pkg.mod import work\n\ndef test_other() -> None:\n    assert 1 == 1\n",
        },
    )
    _write_files(
        head_root,
        {
            "pkg/mod.py": "def work() -> int:\n    return 2\n",
            "tests/test_x.py": "from pkg.mod import work\n\ndef test_other() -> None:\n    assert 2 == 2\n",
        },
    )
    with GraphStore(":memory:") as base, GraphStore(":memory:") as head:
        base.sync_workspace(base_root)
        head.sync_workspace(head_root)
        tests_edges = head.rows("SELECT source_id,target_id FROM edges WHERE kind='TESTS'")
        assert ("tests.test_x:", "pkg.mod:work") not in {
            (r["source_id"], r["target_id"]) for r in tests_edges
        }
        delta = compute_delta(base, head)
        assert "pkg.mod:work" in delta["untested_modified_symbols"]


def test_probe5_cross_file_store_facts_subclass_factory_field_protocol(tmp_path):
    _write_files(
        tmp_path,
        {
            "pkg/__init__.py": "",
            "pkg/base.py": (
                "from typing import Protocol\n"
                "\n"
                "class Base:\n"
                "    shared_job: 'Job'\n"
                "    def base_hook(self) -> int:\n"
                "        return 1\n"
                "\n"
                "class Runner(Protocol):\n"
                "    def execute(self, x: int) -> int:\n"
                "        ...\n"
                "\n"
                "class Job:\n"
                "    def finish(self) -> int:\n"
                "        return 2\n"
                "\n"
                "def make_job() -> Job:\n"
                "    return Job()\n"
            ),
            "pkg/impl.py": (
                "from pkg.base import Base\n"
                "\n"
                "class Sub(Base):\n"
                "    def run(self) -> int:\n"
                "        self.shared_job.finish()\n"
                "        return self.base_hook()\n"
                "\n"
                "class ConcreteRunner:\n"
                "    def execute(self, x: int) -> int:\n"
                "        return x + 1\n"
                "\n"
                "class TooManyArgsRunner:\n"
                "    def execute(self, x: int, y: int) -> int:\n"
                "        return x + y\n"
                "\n"
                "class TooFewArgsRunner:\n"
                "    def execute(self) -> int:\n"
                "        return 0\n"
            ),
            "pkg/service.py": (
                "from pkg.base import Job, Runner, make_job\n"
                "\n"
                "class Service:\n"
                "    job: Job\n"
                "\n"
                "    def process_factory(self) -> int:\n"
                "        j = make_job()\n"
                "        return j.finish()\n"
                "\n"
                "    def process_field(self) -> int:\n"
                "        return self.job.finish()\n"
                "\n"
                "    def process_proto(self, r: Runner) -> int:\n"
                "        return r.execute(1)\n"
            ),
        },
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        calls = {
            (r["source_id"], r["target_id"])
            for r in store.rows("SELECT source_id,target_id FROM edges WHERE kind='CALLS'")
        }
        assert ("pkg.impl:Sub.run", "pkg.base:Base.base_hook") in calls
        assert ("pkg.impl:Sub.run", "pkg.base:Job.finish") in calls
        assert ("pkg.service:Service.process_factory", "pkg.base:Job.finish") in calls
        assert ("pkg.service:Service.process_field", "pkg.base:Job.finish") in calls
        assert ("pkg.service:Service.process_proto", "pkg.base:Runner.execute") in calls
        assert ("pkg.service:Service.process_proto", "pkg.impl:ConcreteRunner.execute") in calls
        assert ("pkg.service:Service.process_proto", "pkg.impl:TooManyArgsRunner.execute") not in calls
        assert ("pkg.service:Service.process_proto", "pkg.impl:TooFewArgsRunner.execute") not in calls


def test_probe6_symbol_seeded_slice_context_ranks_local_neighborhood_over_global_hub(tmp_path):
    files = {
        "pkg/__init__.py": "",
        "pkg/hub.py": "def log_event(msg: str) -> str:\n    return msg\n",
        "pkg/local_dep.py": (
            "def rare_helper(val: int) -> int:\n"
            "    return val + 1\n"
            "\n"
            "def rare_chain(val: int) -> int:\n"
            "    return rare_helper(val)\n"
        ),
        "pkg/target.py": (
            "from pkg.local_dep import rare_chain\n"
            "\n"
            "def focus(x: int) -> int:\n"
            "    return rare_chain(x)\n"
        ),
    }
    for i in range(12):
        files[f"pkg/noise_{i}.py"] = (
            "from pkg.hub import log_event\n"
            f"def caller_{i}() -> str:\n"
            f"    return log_event('n{i}')\n"
        )
    _write_files(tmp_path, files)
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        seeded = rank(store, seed_symbols=("pkg.target:focus",))
        assert seeded["pkg.local_dep:rare_chain"] > seeded["pkg.hub:log_event"]
        assert seeded["pkg.local_dep:rare_helper"] > seeded["pkg.hub:log_event"]


def test_p0_1_java_overload_identity_and_arity_routing(tmp_path):
    _write_files(
        tmp_path,
        {
            "src/main/java/com/app/Overloaded.java": (
                "package com.app;\n"
                "import java.util.List;\n"
                "public class Overloaded {\n"
                "    public int compute(int a) {\n"
                "        return a;\n"
                "    }\n"
                "    public int compute(int a, int b) {\n"
                "        return a + b;\n"
                "    }\n"
                "    public int callOne() {\n"
                "        return compute(1);\n"
                "    }\n"
                "    public int callTwo() {\n"
                "        return compute(1, 2);\n"
                "    }\n"
                "    public int containerSize(List<Overloaded> items) {\n"
                "        return items.size();\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        calls = {
            (r["source_id"], r["target_id"])
            for r in store.rows("SELECT source_id,target_id FROM edges WHERE kind='CALLS'")
        }
        assert ("com.app.Overloaded:Overloaded.compute", "com.app.Overloaded:Overloaded.compute#2") not in calls
        assert ("com.app.Overloaded:Overloaded.compute#2", "com.app.Overloaded:Overloaded.compute") not in calls
        assert ("com.app.Overloaded:Overloaded.callOne", "com.app.Overloaded:Overloaded.compute") in calls
        assert ("com.app.Overloaded:Overloaded.callOne", "com.app.Overloaded:Overloaded.compute#2") not in calls
        assert ("com.app.Overloaded:Overloaded.callTwo", "com.app.Overloaded:Overloaded.compute#2") in calls
        assert ("com.app.Overloaded:Overloaded.callTwo", "com.app.Overloaded:Overloaded.compute") not in calls


def test_p0_2_java_and_ts_interface_dispatch_to_implementers(tmp_path):
    _write_files(
        tmp_path,
        {
            "src/main/java/com/app/Repo.java": (
                "package com.app;\n"
                "public interface Repo {\n"
                "    String find(String id);\n"
                "}\n"
            ),
            "src/main/java/com/app/SqlRepo.java": (
                "package com.app;\n"
                "public class SqlRepo implements Repo {\n"
                "    public String find(String id) {\n"
                "        return id;\n"
                "    }\n"
                "}\n"
            ),
            "src/main/java/com/app/Service.java": (
                "package com.app;\n"
                "public class Service {\n"
                "    public String run(Repo repo) {\n"
                "        return repo.find(\"k\");\n"
                "    }\n"
                "}\n"
            ),
            "web/store.ts": (
                "export interface Store {\n"
                "    load(key: string): string;\n"
                "}\n"
                "export class MemStore implements Store {\n"
                "    load(key: string): string {\n"
                "        return key;\n"
                "    }\n"
                "}\n"
                "export function fetchKey(s: Store): string {\n"
                "    return s.load('a');\n"
                "}\n"
            ),
        },
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        calls = {
            (r["source_id"], r["target_id"])
            for r in store.rows("SELECT source_id,target_id FROM edges WHERE kind='CALLS'")
        }
        assert ("com.app.Service:Service.run", "com.app.Repo:Repo.find") in calls
        assert ("com.app.Service:Service.run", "com.app.SqlRepo:SqlRepo.find") in calls
        assert ("web.store:fetchKey", "web.store:Store.load") in calls
        assert ("web.store:fetchKey", "web.store:MemStore.load") in calls


def test_p0_3_ts_inferred_factory_receiver_and_unknown_receiver_diagnostics(tmp_path):
    _write_files(
        tmp_path,
        {
            "web/client.ts": (
                "export class ApiClient {\n"
                "    send(payload: string): boolean {\n"
                "        return payload.length > 0;\n"
                "    }\n"
                "}\n"
                "export function createClient(): ApiClient {\n"
                "    return new ApiClient();\n"
                "}\n"
                "export function runFactory(): boolean {\n"
                "    const client = createClient();\n"
                "    return client.send('hi');\n"
                "}\n"
                "export function runUnknown(untyped: any): any {\n"
                "    return untyped.get('key');\n"
                "}\n"
            ),
        },
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        calls = {
            (r["source_id"], r["target_id"])
            for r in store.rows("SELECT source_id,target_id FROM edges WHERE kind='CALLS'")
        }
        assert ("web.client:runFactory", "web.client:ApiClient.send") in calls
        diags = store.rows("SELECT source_id,target FROM diagnostics WHERE source_id='web.client:runUnknown'")
        assert any("get" in d["target"] for d in diags)


def test_p0_4_javadoc_tsdoc_contracts_and_compact_cross_file_callees(tmp_path):
    _write_files(
        tmp_path,
        {
            "src/main/java/com/app/Policy.java": (
                "package com.app;\n"
                "/**\n"
                " * Must preserve monotonic sequence numbers.\n"
                " */\n"
                "public class Policy {\n"
                "    private final int minSeq = 1;\n"
                "    public boolean isValid(int seq) {\n"
                "        return seq >= minSeq;\n"
                "    }\n"
                "}\n"
            ),
            "src/main/java/com/app/Engine.java": (
                "package com.app;\n"
                "public class Engine {\n"
                "    public boolean check(Policy p, int seq) {\n"
                "        return p.isValid(seq);\n"
                "    }\n"
                "}\n"
            ),
        },
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        sliced = slice_context(store, ["com.app.Engine:Engine.check"], max_tokens=2000)
        types_text = "\n".join(item["text"] for item in sliced["sections"]["types_and_schemas"])
        downstream_text = "\n".join(item["text"] for item in sliced["sections"]["downstream_dependencies"])
        assert "Must preserve monotonic sequence numbers." in types_text
        assert "minSeq" in types_text
        assert "isValid" in downstream_text


def test_v031_python_state_coupling_properties_and_dispatcher_boundary_window(tmp_path):
    """v0.3.1: Python class summaries retain __init__ state fields, @property methods, and
    state-coupled sibling methods (e.g. resolve_config reading self.active_versions), and
    upstream_callers retains entry-boundary route splitting lines above deep call sites."""
    _write_files(
        tmp_path,
        {
            "pkg/service.py": (
                "class ConfigStore:\n"
                "    def __init__(self):\n"
                "        self.active_versions = {}\n"
                "        self.reviews_by_actor = {}\n"
                "        self.resolution = None\n"
                "\n"
                "    def m1(self): return 1\n"
                "    def m2(self): return 2\n"
                "    def m3(self): return 3\n"
                "    def m4(self): return 4\n"
                "    def m5(self): return 5\n"
                "\n"
                "    @property\n"
                "    def status(self) -> str:\n"
                "        return self.resolution or 'open'\n"
                "\n"
                "    def merge_proposal(self, key: str, ver: int) -> None:\n"
                "        self.active_versions[key] = ver\n"
                "\n"
                "    def resolve_config(self, key: str) -> int:\n"
                "        return self.active_versions.get(key, 0)\n"
                "\n"
                "def do_post(store: ConfigStore, path: str) -> None:\n"
                "    segments = path.strip('/').split('/')\n"
                "    resource, sep, action = segments[-1].partition(':')\n"
                "    x1 = 1\n"
                "    x2 = 2\n"
                "    x3 = 3\n"
                "    x4 = 4\n"
                "    x5 = 5\n"
                "    x6 = 6\n"
                "    if action == 'merge':\n"
                "        store.merge_proposal(resource, x6)\n"
            ),
        },
    )
    with GraphStore(":memory:") as store:
        store.sync_workspace(tmp_path)
        sliced = slice_context(store, ["pkg.service:ConfigStore.merge_proposal"], max_tokens=4000)
        types_text = "\n".join(item["text"] for item in sliced["sections"]["types_and_schemas"])
        callers_text = "\n".join(item["text"] for item in sliced["sections"]["upstream_callers"])
        assert "self.active_versions" in types_text
        assert "def status(" in types_text
        assert "def resolve_config(" in types_text
        assert "segments = path.strip" in callers_text
        assert "partition(':')" in callers_text

    from codegraph_slicer.slicer import (
        _caller_boundary_lines,
        _extract_py_init_fields,
        _is_priority_compact_method,
        _select_compact_methods,
    )

    b_lines = ["def f():", "    a = x.split('/')", "    b = y.rsplit(':', 1)", "    c = z.partition(':')", "    d = w.strip()", "    call()"]
    assert _caller_boundary_lines(b_lines, 1, 1) == []
    assert _caller_boundary_lines(b_lines, 1, 5) == [
        (1, "a = x.split('/')"),
        (2, "b = y.rsplit(':', 1)"),
        (3, "c = z.partition(':')"),
    ]
    assert _extract_py_init_fields([{"qualname": "C.other", "body": "def other(self):\n    self.x = 1"}]) == []
    init_body = "def __init__(self):\n    self._priv = 0\n    self.a = 1\n    self.b = 2\n    self.c = 3\n    self.d = 4\n    self.e = 5\n    self.f = 6\n    self.g = 7"
    assert _extract_py_init_fields([{"qualname": "C.__init__", "body": init_body}]) == [
        "        self.a = 1",
        "        self.b = 2",
        "        self.c = 3",
        "        self.d = 4",
        "        self.e = 5",
        "        self.f = 6",
    ]
    assert _is_priority_compact_method({"qualname": "C.foo", "body": "def foo(self): pass"}, {"__init__"}, {"foo"}, set()) is True
    assert _is_priority_compact_method({"qualname": "C.bar", "body": "@property\ndef bar(self): pass"}, set(), set(), set()) is True
    assert _is_priority_compact_method({"qualname": "C.baz", "body": "def baz(self): return self.state"}, set(), set(), {"state"}) is True
    assert _is_priority_compact_method({"qualname": "C.qux", "body": "def qux(self): return self.other"}, set(), set(), {"state"}) is False

    py_methods = [
        {"qualname": f"C.m{i}", "signature": f"def m{i}(self):", "body": f"def m{i}(self): pass"}
        for i in range(6)
    ]
    assert len(_select_compact_methods({"file_path": "a.py"}, py_methods, set(), {"m4"}, set())) == 4
    assert len(_select_compact_methods({"file_path": "a.py"}, py_methods, set(), {"m0", "m1", "m2", "m3", "m4"}, set())) == 5
    java_methods = [
        {"qualname": f"C.m{i}", "signature": ("private void m0()" if i == 0 else f"public void m{i}()"), "body": f"void m{i}() {{}}"}
        for i in range(19)
    ]
    selected_java = _select_compact_methods({"file_path": "A.java"}, java_methods, set(), set(), set())
    assert len(selected_java) == 18
    assert all(m["qualname"] != "C.m0" for m in selected_java)


