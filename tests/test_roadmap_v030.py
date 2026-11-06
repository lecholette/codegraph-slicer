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
