import json
import pytest

from codegraph_slicer import (
    CodeGraphError,
    CodeGraphParseError,
    GraphStore,
    UnsupportedFlowError,
    build_repo_map,
    compute_delta,
    extract_file,
    query_flow,
    slice_context,
)
from codegraph_slicer.cli import main
from codegraph_slicer.slicer import query_call_impact


def _write(root, files):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def test_java_extraction_field_dispatch_overloads_and_tests(tmp_path, capsys):
    root = tmp_path / "java_repo"
    _write(
        root,
        {
            "src/main/java/com/acme/model/Order.java": """package com.acme.model;

public record Order(String id, int quantity) {
    public boolean isValid() {
        return id != null && quantity > 0;
    }
}
""",
            "src/main/java/com/acme/util/Checks.java": """package com.acme.util;

public class Checks {
    public static void ensurePositive(int value) {
        if (value <= 0) {
            throw new IllegalArgumentException("must be positive");
        }
    }
}
""",
            "src/main/java/com/acme/repo/Repository.java": """package com.acme.repo;

public interface Repository<T> {
    T save(T entity);
}
""",
            "src/main/java/com/acme/repo/OrderRepository.java": """package com.acme.repo;

import com.acme.model.Order;

public class OrderRepository implements Repository<Order> {
    @Override
    public Order save(Order entity) {
        if (entity.isValid()) {
            return entity;
        }
        return null;
    }

    public Order find(String id) {
        return new Order(id, 1);
    }

    public Order find(String id, int defaultQty) {
        return new Order(id, defaultQty);
    }
}
""",
            "src/main/java/com/acme/service/OrderService.java": """package com.acme.service;

import com.acme.model.*;
import com.acme.repo.OrderRepository;
import static com.acme.util.Checks.ensurePositive;

public class OrderService {
    private final OrderRepository repo;

    public OrderService(OrderRepository repo) {
        this.repo = repo;
    }

    public Order createOrder(
        String id,
        int qty
    ) {
        ensurePositive(qty);
        Order item = new Order(id, qty);
        return this.repo.save(item);
    }
}
""",
            "src/test/java/com/acme/service/OrderServiceTest.java": """package com.acme.service;

import com.acme.model.Order;
import com.acme.repo.OrderRepository;

public class OrderServiceTest {
    @Test
    public void testCreateOrder() {
        OrderRepository repo = new OrderRepository();
        OrderService svc = new OrderService(repo);
        Order result = svc.createOrder("ord-1", 5);
    }
}
""",
        },
    )

    with GraphStore(":memory:") as store:
        stats = store.sync_workspace(root)
        assert stats["parsed_files"] == 6

        calls = {
            (r["source_id"], r["target_id"])
            for r in store.rows("SELECT source_id,target_id FROM edges WHERE kind='CALLS'")
        }
        # Cross-file constructor-injected field dispatch: this.repo.save(item) -> OrderRepository.save
        assert (
            "com.acme.service.OrderService:OrderService.createOrder",
            "com.acme.repo.OrderRepository:OrderRepository.save",
        ) in calls
        # Static import call: ensurePositive(qty) -> Checks.ensurePositive
        assert (
            "com.acme.service.OrderService:OrderService.createOrder",
            "com.acme.util.Checks:Checks.ensurePositive",
        ) in calls
        # Wildcard package import + instantiation: new Order(id, qty) -> Order
        assert (
            "com.acme.service.OrderService:OrderService.createOrder",
            "com.acme.model.Order:Order",
        ) in calls
        # Local variable typed dispatch inside OrderRepository.save: entity.isValid() -> Order.isValid
        assert (
            "com.acme.repo.OrderRepository:OrderRepository.save",
            "com.acme.model.Order:Order.isValid",
        ) in calls
        # Method overloads indexed as distinct symbols without synthetic self-call
        assert (
            "com.acme.repo.OrderRepository:OrderRepository.find#2",
            "com.acme.model.Order:Order",
        ) in calls

        # INHERITS and USES_TYPE on generic interface Repository<Order>
        inherits = {
            (r["source_id"], r["target_id"])
            for r in store.rows("SELECT source_id,target_id FROM edges WHERE kind='INHERITS'")
        }
        assert (
            "com.acme.repo.OrderRepository:OrderRepository",
            "com.acme.repo.Repository:Repository",
        ) in inherits
        uses_type = {
            (r["source_id"], r["target_id"])
            for r in store.rows("SELECT source_id,target_id FROM edges WHERE kind='USES_TYPE'")
        }
        assert (
            "com.acme.repo.OrderRepository:OrderRepository",
            "com.acme.model.Order:Order",
        ) in uses_type

        # TESTS edge from JUnit test method
        tests_edges = {
            (r["source_id"], r["target_id"])
            for r in store.rows("SELECT source_id,target_id FROM edges WHERE kind='TESTS'")
        }
        assert (
            "com.acme.service.OrderServiceTest:OrderServiceTest.testCreateOrder",
            "com.acme.service.OrderService:OrderService.createOrder",
        ) in tests_edges

        # Slice on OrderRepository.save: upstream_callers has OrderService.createOrder with // L<line> snippet
        sliced = slice_context(
            store, ["com.acme.repo.OrderRepository:OrderRepository.save"]
        )
        callers = sliced["sections"]["upstream_callers"]
        assert [c["symbol"] for c in callers] == [
            "com.acme.service.OrderService:OrderService.createOrder"
        ]
        assert "// L" in callers[0]["text"]
        assert "return this.repo.save(item);" in callers[0]["text"]

        # Flow refuses non-Python anchors cleanly with UnsupportedFlowError
        with pytest.raises(UnsupportedFlowError, match="flow supports Python functions only"):
            query_flow(store, "com.acme.service.OrderService:OrderService.createOrder")

    # CLI map uses ```java fence
    db = tmp_path / "java.sqlite"
    assert main(["sync", str(root), "--db", str(db)]) == 0
    capsys.readouterr()
    assert main(["map", "--db", str(db), "--format", "markdown", "--max-tokens", "4000"]) == 0
    md = capsys.readouterr().out
    assert "```java\n" in md
    assert "{ ... }" in md


def test_typescript_and_tsx_barrels_paths_di_and_tests(tmp_path, capsys):
    root = tmp_path / "ts_repo"
    _write(
        root,
        {
            "tsconfig.json": """{
  // JSONC comment in tsconfig
  "compilerOptions": {
    "baseUrl": ".",
    "paths": {
      "@models/*": ["src/models/*"],
      "@services/*": ["src/services/*"]
    }
  }
}
""",
            "src/models/user.ts": """export type UserId = string;

export interface User {
  id: UserId;
  name: string;
}

export class UserEntity implements User {
  constructor(public id: UserId, public name: string) {}

  normalize(): string {
    return this.name.trim() || "anon";
  }
}
""",
            "src/models/index.ts": """export * from './user';
""",
            "src/services/repo.ts": """import { UserEntity, type UserId } from '../models/index.js';

export class UserRepository {
  findById(id: UserId): UserEntity {
    return new UserEntity(id, "Alice");
  }
}
""",
            "src/services/user_service.ts": """import { UserEntity, type UserId } from '@models/index';
import { UserRepository } from '@services/repo';

export function formatName(name: string): string;
export function formatName(name: string, upper: boolean): string;
export function formatName(name: string, upper?: boolean): string {
  return upper ? name.toUpperCase() : name;
}

export class UserService {
  constructor(private readonly repo: UserRepository) {}

  getUserDisplay(
    id: UserId
  ): string {
    const entity = this.repo.findById(id);
    const norm = entity.normalize();
    return formatName(norm, true);
  }
}

export const createDefaultService = (): UserService => {
  return new UserService(new UserRepository());
};
""",
            "src/ui/UserCard.tsx": """import { UserService, createDefaultService } from '../services/user_service';

export const Badge = ({ text }: { text: string }) => {
  return <span>{text}</span>;
};

export function UserCard({ id }: { id: string }) {
  const svc: UserService = createDefaultService();
  const label = svc.getUserDisplay(id);
  return <Badge text={label} />;
}
""",
            "src/services/user_service.test.ts": """import { createDefaultService } from './user_service';

describe('UserService', () => {
  it('resolves user display', () => {
    const svc = createDefaultService();
    const out = svc.getUserDisplay('u1');
  });
});
""",
        },
    )

    with GraphStore(":memory:") as store:
        stats = store.sync_workspace(root)
        assert stats["parsed_files"] == 6

        calls = {
            (r["source_id"], r["target_id"])
            for r in store.rows("SELECT source_id,target_id FROM edges WHERE kind='CALLS'")
        }
        # Constructor-injected private readonly repo: this.repo.findById -> UserRepository.findById
        assert (
            "services.user_service:UserService.getUserDisplay",
            "services.repo:UserRepository.findById",
        ) in calls
        # Overloaded TS function resolves to single canonical implementation
        assert (
            "services.user_service:UserService.getUserDisplay",
            "services.user_service:formatName",
        ) in calls
        # Arrow function symbol creation and new_expression calls
        assert (
            "services.user_service:createDefaultService",
            "services.user_service:UserService",
        ) in calls
        assert (
            "services.user_service:createDefaultService",
            "services.repo:UserRepository",
        ) in calls
        # TSX component call <Badge text={label} /> and typed local svc.getUserDisplay
        assert ("ui.UserCard:UserCard", "ui.UserCard:Badge") in calls
        assert (
            "ui.UserCard:UserCard",
            "services.user_service:UserService.getUserDisplay",
        ) in calls

        # Barrel re-export `export * from './user'` resolves UserEntity across @models/index
        uses_type = {
            (r["source_id"], r["target_id"])
            for r in store.rows("SELECT source_id,target_id FROM edges WHERE kind='USES_TYPE'")
        }
        assert (
            "services.user_service:UserService.getUserDisplay",
            "models.user:UserId",
        ) in uses_type
        assert (
            "services.repo:UserRepository.findById",
            "models.user:UserEntity",
        ) in uses_type

        # Vitest/Jest describe/it in .test.ts emits TESTS edges
        tests_edges = {
            (r["source_id"], r["target_id"])
            for r in store.rows("SELECT source_id,target_id FROM edges WHERE kind='TESTS'")
        }
        assert (
            "services.user_service.test:",
            "services.user_service:createDefaultService",
        ) in tests_edges

        # Impact on UserRepository.findById reaches UserService, UserCard, and user_service.test.ts
        impact = query_call_impact(store, "services.repo:UserRepository.findById")
        caller_symbols = {row[0] for row in impact["callers"]["rows"]}
        assert "services.user_service:UserService.getUserDisplay" in caller_symbols
        assert "ui.UserCard:UserCard" in caller_symbols


def test_multilang_slice_m1_m4_and_delta_gates(tmp_path):
    root = tmp_path / "ts_delta"
    methods_code = "\n\n".join(
        f"""  step{i}(inputValue: number, multiplierFactor: number): number {{
    const intermediateResult = inputValue * multiplierFactor + {i};
    if (intermediateResult > 0) {{
      return intermediateResult + {i * 10};
    }}
    return 0;
  }}"""
        for i in range(1, 21)
    )
    _write(
        root,
        {
            "src/domain/order.ts": f"export class OrderProcessor {{\n{methods_code}\n}}\n",
            "src/infra/db.ts": """import { OrderProcessor } from '../domain/order';

export function saveOrder(proc: OrderProcessor, x: number): number {
  return proc.step1(x, 2);
}
""",
        },
    )

    with GraphStore(":memory:") as base_store:
        base_store.sync_workspace(root)

        # M1: slicing on saveOrder auto-summarizes large OrderProcessor in types_and_schemas with // ... (+N more methods)
        sliced_m1 = slice_context(
            base_store, ["infra.db:saveOrder"], max_tokens=4000
        )
        m1_text = sliced_m1["sections"]["types_and_schemas"][0]["text"]
        assert "step1(inputValue: number, multiplierFactor: number): number" in m1_text
        assert "// ... (+16 more methods)" in m1_text

        # M4: slicing on OrderProcessor + saveOrder under tight budget (>=1000) condenses with // ... (N lines condensed)
        # and does not duplicate OrderProcessor.step1 in downstream_dependencies
        sliced_m4 = slice_context(
            base_store,
            [
                "domain.order:OrderProcessor",
                "infra.db:saveOrder",
            ],
            max_tokens=1000,
        )
        assert sliced_m4["sections"]["downstream_dependencies"] == []
        m4_text = sliced_m4["sections"]["target_implementations"][0]["text"]
        assert "// ... (" in m4_text
        assert "lines condensed;" in m4_text

        # Modify workspace to introduce a cycle (domain -> infra -> domain), complexity drift, and boundary violation
        _write(
            root,
            {
                "src/domain/order.ts": """import { saveOrder } from '../infra/db';

export class OrderProcessor {
  stepOne(x: number): number {
    if (x > 0 && x < 100) {
      for (let i = 0; i < x; i++) {
        if (i % 2 === 0) {
          saveOrder(i);
        }
      }
      return x + 1;
    }
    return 0;
  }

  stepTwo(x: number): number {
    return x * 2;
  }
}
""",
            },
        )
        with GraphStore(":memory:") as head_store:
            head_store.sync_workspace(root)
            delta = compute_delta(
                base_store,
                head_store,
                boundary_rules=[("domain", "infra")],
                cx_threshold=2,
            )
        assert len(delta["new_cycles"]) >= 1
        assert any(
            d["symbol"] == "domain.order:OrderProcessor.stepOne" and d["threshold_exceeded"]
            for d in delta["complexity_drift"]
        )
        assert any(
            v["source"].startswith("domain") and v["target"].startswith("infra")
            for v in delta["boundary_violations"]
        )
        assert "domain.order:OrderProcessor.stepOne" in delta["untested_modified_symbols"]


def test_java_and_ts_parse_errors_raise_codegraph_parse_error():
    with pytest.raises(CodeGraphParseError, match="cannot parse Broken.java"):
        extract_file("Broken.java", b"public class Broken { void f( { } }")
    with pytest.raises(CodeGraphParseError, match="cannot parse broken.ts"):
        extract_file("broken.ts", b"export function f(a: number { return a; }")
    with pytest.raises(CodeGraphError, match="invalid Python path"):
        extract_file("ambient.d.ts", b"export declare function f(): void;")


def test_revision_2_slice_headers_sibling_bodies_and_builtin_filtering(tmp_path):
    from codegraph_slicer.delta import summarize_delta

    root = tmp_path / "rev2_ts"
    _write(
        root,
        {
            "src/models/types.ts": """export type EventKind = "START" | "END";

export interface EventRecord {
  kind: EventKind;
  name: string;
}

export interface JobInput {
  id: string;
}
""",
            "src/services/executor.ts": """import * as path from "node:path";
import type { EventRecord, JobInput } from "../models/types.ts";

export class Executor {
  public events: EventRecord[] = [];

  public run(job: JobInput): string[] {
    const items: string[] = [];
    const walk = (p: string) => {
      items.push(p.trim());
    };
    walk(this.normalize(job.id));
    return items.map((x) => String(x));
  }

  private normalize(raw: string): string {
    return path.posix.normalize(raw);
  }
}
""",
            "src/cli.ts": """import { Executor } from "./services/executor.ts";

export function main(): string[] {
  const ex = new Executor();
  return ex.run({ id: "a" });
}
""",
        },
    )

    with GraphStore(":memory:") as store:
        stats = store.sync_workspace(root)
        # R5: local closure `walk`, built-in `String`, and `.push`/`.trim`/`.map` do not emit unresolved diagnostics
        diag_targets = [
            r["target"]
            for r in store.rows(
                "SELECT target FROM diagnostics WHERE source_id='services.executor:Executor.run'"
            )
        ]
        assert not any(
            t in {"?walk", "?String", "?items.push", "?items.map", "?p.trim"}
            for t in diag_targets
        )

        # Slice on Executor.run and cli:main
        sliced = slice_context(
            store,
            ["services.executor:Executor.run", "cli:main"],
            max_tokens=4000,
        )
        sections = sliced["sections"]

        # R1: Executor.run target includes file imports + class header & fields
        run_entry = next(
            e
            for e in sections["target_implementations"]
            if e["symbol"] == "services.executor:Executor.run"
        )
        assert "// File & class header" in run_entry["text"]
        assert 'import * as path from "node:path";' in run_entry["text"]
        assert "export class Executor {" in run_entry["text"]
        assert "public events: EventRecord[] = [];" in run_entry["text"]

        # R2: compact sibling method `Executor.normalize` includes its full body in downstream_dependencies
        norm_entry = next(
            e
            for e in sections["downstream_dependencies"]
            if e["symbol"] == "services.executor:Executor.normalize"
        )
        assert "return path.posix.normalize(raw);" in norm_entry["text"]

        # R3: Enclosing class USES_TYPE closure pulls in EventRecord -> EventKind into types_and_schemas
        type_syms = {e["symbol"] for e in sections["types_and_schemas"]}
        assert "models.types:EventRecord" in type_syms
        assert "models.types:EventKind" in type_syms
        assert "models.types:JobInput" in type_syms

        # R4: Bare enclosing class `services.executor:Executor` is suppressed from downstream_dependencies
        downstream_syms = {e["symbol"] for e in sections["downstream_dependencies"]}
        assert "services.executor:Executor" not in downstream_syms

        # R6: summarize_delta includes `complexity_threshold_exceeded` count when complexity_drift > 0
        summary_payload = summarize_delta(
            {
                "added_edges": [],
                "removed_edges": [],
                "complexity_drift": [
                    {
                        "symbol": "services.executor:Executor.run",
                        "cx_delta": 12,
                        "fan_out_delta": 0,
                        "threshold_exceeded": True,
                    },
                    {
                        "symbol": "cli:main",
                        "cx_delta": 1,
                        "fan_out_delta": 0,
                        "threshold_exceeded": False,
                    },
                ],
                "boundary_violations": [],
                "modified_symbols": ["services.executor:Executor.run", "cli:main"],
                "untested_modified_symbols": [],
                "affected_entrypoints": ["cli:main"],
                "new_cycles": [],
                "test_coverage": [],
                "test_gap_note": "note",
            },
            max_tokens=2000,
        )
        assert summary_payload["summary"]["complexity_threshold_exceeded"] == 1


def test_revision_3_1_hidden_scratch_exclusion_and_bounded_delta_summary(
    tmp_path,
) -> None:
    """Verify R3.1 hidden scratch-dir exclusion, stale cleanup, and bounded delta summary."""
    from codegraph_slicer.delta import summarize_delta

    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / ".backup-check").mkdir(parents=True)
    (repo / "src" / "main.ts").write_text(
        "export function runApp(): number { return 1; }\n", encoding="utf-8"
    )
    (repo / ".backup-check" / "scratch.ts").write_text(
        "export function tempHelper(): number { return 99; }\n", encoding="utf-8"
    )

    db = tmp_path / "graph.sqlite"
    with GraphStore(db) as store:
        # 1. With include_hidden=True, scratch file is indexed
        res_hidden = store.sync_workspace(repo, include_hidden=True)
        assert res_hidden["parsed_files"] == 2
        paths_hidden = {r["path"] for r in store.rows("SELECT path FROM files")}
        assert ".backup-check/scratch.ts" in paths_hidden

        # 2. Default sync_workspace ignores hidden scratch dir and cleans up stale rows
        res_clean = store.sync_workspace(repo)
        assert res_clean["deleted_files"] == 1
        paths_clean = {r["path"] for r in store.rows("SELECT path FROM files")}
        assert paths_clean == {"src/main.ts"}

    # 3. Default delta summary bounds untested_modified_symbols and test_coverage to 5 entries
    syms = [f"pkg.mod:fn_{i:02d}" for i in range(20)]
    raw_delta = {
        "added_edges": [],
        "removed_edges": [],
        "complexity_drift": [
            {"symbol": s, "cx_delta": 14 if idx < 2 else 1, "fan_out_delta": 0, "threshold_exceeded": idx < 2}
            for idx, s in enumerate(syms)
        ],
        "boundary_violations": [],
        "modified_symbols": syms,
        "untested_modified_symbols": syms,
        "affected_entrypoints": ["pkg.mod:fn_00"],
        "new_cycles": [],
        "test_coverage": [
            {"symbol": s, "reachable_tests": ["tests.test_mod:"], "updated_or_selected_tests": []}
            for s in syms
        ],
        "test_gap_note": "Static TESTS reachability and changed/selected source only; no execution or behavioral coverage is claimed.",
    }
    summary = summarize_delta(raw_delta, max_tokens=8000, full=False)
    assert summary["summary"]["counts"]["untested_modified_symbols"] == 20
    assert len(summary["untested_modified_symbols"]) == 5
    assert summary["omitted"]["untested_modified_symbols"] == 15
    assert len(summary["test_coverage"]) == 5
    assert summary["omitted"]["test_coverage"] == 15
    assert len(summary["modified_symbols"]) == 5
    assert summary["omitted"]["modified_symbols"] == 15
    assert summary["summary"]["complexity_threshold_exceeded"] == 2
    assert summary["serialized_tokens"] < 650

    full_res = summarize_delta(raw_delta, max_tokens=8000, full=True)
    assert len(full_res["untested_modified_symbols"]) == 20
    assert full_res["omitted"]["untested_modified_symbols"] == 0


def test_v0_2_1_mixed_language_collision_and_incremental_rebind(tmp_path) -> None:
    """Verify mixed-language same-stem module disambiguation and incremental sync re-extraction."""
    repo = tmp_path / "poly"
    _write(
        repo,
        {
            "a.ts": "export function run(): number { return 1; }\n",
            "b.ts": "import { run } from './a';\nexport function callRun(): number { return run(); }\n",
        },
    )
    db = tmp_path / "poly.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(repo)
        # Initially only a.ts exists, so its module is 'a'
        edges_initial = {
            (r["source_id"], r["target_id"], r["kind"])
            for r in store.rows("SELECT source_id,target_id,kind FROM edges")
        }
        assert ("b:callRun", "a:run", "CALLS") in edges_initial

        # Now add a.py so a.py and a.ts collide on stem 'a'
        (repo / "a.py").write_text("def run():\n    return 2\n", encoding="utf-8")
        store.sync_workspace(repo)

        # Incremental sync must re-extract a.ts as 'a_ts' and rebind b.ts -> a_ts:run
        edges_collided = {
            (r["source_id"], r["target_id"], r["kind"])
            for r in store.rows("SELECT source_id,target_id,kind FROM edges")
        }
        assert ("b:callRun", "a_ts:run", "CALLS") in edges_collided
        assert ("b:callRun", "a:run", "CALLS") not in edges_collided

        # Remove a.py and re-sync: a.ts reverts to 'a' and b.ts rebinds to a:run
        (repo / "a.py").unlink()
        store.sync_workspace(repo)
        edges_restored = {
            (r["source_id"], r["target_id"], r["kind"])
            for r in store.rows("SELECT source_id,target_id,kind FROM edges")
        }
        assert ("b:callRun", "a:run", "CALLS") in edges_restored


def test_v0_2_1_ts_accessors_overloads_tsconfig_urls_and_relative_imports(
    tmp_path,
) -> None:
    """Verify TS getters/setters, interface/method overloads, URL strings in tsconfig, and ../ imports."""
    repo = tmp_path / "ts_edge_cases"
    _write(
        repo,
        {
            "tsconfig.json": """{
  // Comment before URL string
  "$schema": "https://json.schemastore.org/tsconfig",
  "compilerOptions": {
    "baseUrl": ".",
    "paths": {
      "@shared/*": ["shared/*"]
    }
  }
}
""",
            "shared/helper.ts": """function internalHelper(x: string): string {
  return x.trim();
}
export { internalHelper as formatValue };
export default (n: number) => n + 1;
""",
            "src/box.ts": """import bump, { formatValue } from '../shared/helper';

export interface Parser {
  parse(input: string): string;
  parse(input: number): string;
}

export class Box implements Parser {
  private _val: string = formatValue("init");

  get value(): string {
    return this._val;
  }

  set value(v: string) {
    this._val = formatValue(v);
  }

  parse(input: string): string;
  parse(input: number): string;
  parse(input: string | number): string {
    const fn = x => formatValue(String(x));
    function nestedIgnored() {
      return bump(10);
    }
    return String(bump(1));
  }
}
""",
        },
    )
    db = tmp_path / "ts.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(repo)
        store.assert_fresh()

        syms = {r["symbol_id"]: r for r in store.rows("SELECT * FROM symbols")}
        assert "box:Box.value" in syms
        assert "box:Box.parse" in syms
        assert "box:Parser.parse" in syms
        assert "shared.helper:default" in syms

        edges = {
            (r["source_id"], r["target_id"], r["kind"])
            for r in store.rows("SELECT source_id,target_id,kind FROM edges")
        }
        # Setter & field initializer call formatValue -> shared.helper:internalHelper
        assert ("box:Box.value", "shared.helper:internalHelper", "CALLS") in edges
        assert ("box:Box", "shared.helper:internalHelper", "CALLS") in edges
        # Box.parse calls bump (shared.helper:default) and arrow fn calls formatValue
        assert ("box:Box.parse", "shared.helper:default", "CALLS") in edges
        assert ("box:Box.parse", "shared.helper:internalHelper", "CALLS") in edges

        # Modify tsconfig.json -> assert_fresh() must detect staleness
        from codegraph_slicer.store import StaleSnapshotError

        (repo / "tsconfig.json").write_text(
            '{"compilerOptions": {"baseUrl": ".", "paths": {"@other/*": ["shared/*"]}}}\n',
            encoding="utf-8",
        )
        with pytest.raises(StaleSnapshotError, match="tsconfig.json"):
            store.assert_fresh()

        # Re-sync updates tsconfig_sha and restores freshness
        store.sync_workspace(repo)
        store.assert_fresh()


def test_v0_2_1_file_anchors_hidden_freshness_python_types_and_java_enums(
    tmp_path,
) -> None:
    """Verify file/line anchors for Java/TS, hidden dir assert_fresh parity, Python class types, and Java enums/default pkg."""
    from codegraph_slicer.delta import compute_delta

    repo = tmp_path / "workspace"
    _write(
        repo,
        {
            ".scratch/temp.py": "def ignored():\n    return 0\n",
            ".scratch/temp.ts": "export const ignored = () => 0;\n",
            "pkg/service.py": """class Config:
    timeout: int = 30

class Runner:
    def execute(self, cfg: Config) -> Runner:
        return self
""",
            "src/util.ts": """export function computeTotal(a: number, b: number): number {
  return a + b;
}
""",
            "Helper.java": """public class Helper {
    public static int doubleIt(int x) {
        return x * 2;
    }
}
""",
            "Mode.java": """import org.junit.jupiter.api.Test;

public enum Mode {
    FAST,
    SAFE;

    private static final int INIT = Helper.doubleIt(2);

    public int apply(int v) {
        return Helper.doubleIt(v);
    }

    @Test
    public void verifyApply() {
        apply(3);
    }
}
""",
        },
    )

    db = tmp_path / "graph.sqlite"
    with GraphStore(db) as store:
        store.sync_workspace(repo)
        # P1 #4: Hidden .scratch directory must not trip assert_fresh()
        store.assert_fresh()

        # P1 #3: File-path and line anchors work for .ts and .java
        ts_slice_path = slice_context(store, ["src/util.ts:computeTotal"], max_tokens=2000)
        assert ts_slice_path["sections"]["target_implementations"][0]["symbol"] == "util:computeTotal"

        ts_slice_line = slice_context(store, ["src/util.ts:2"], max_tokens=2000)
        assert ts_slice_line["sections"]["target_implementations"][0]["symbol"] == "util:computeTotal"

        java_slice_path = slice_context(store, ["Mode.java:Mode.apply"], max_tokens=2000)
        assert java_slice_path["sections"]["target_implementations"][0]["symbol"] == "Mode:Mode.apply"

        # P1 #6: Python method slice retains enclosing class in types_and_schemas
        py_slice = slice_context(store, ["pkg.service:Runner.execute"], max_tokens=4000)
        py_types = {e["symbol"] for e in py_slice["sections"]["types_and_schemas"]}
        assert "pkg.service:Runner" in py_types
        assert "pkg.service:Config" in py_types

        # P2 #12 & #16: Java enum methods + default-package Helper resolution + @Test in non-*Test.java
        edges = {
            (r["source_id"], r["target_id"], r["kind"])
            for r in store.rows("SELECT source_id,target_id,kind FROM edges")
        }
        assert ("Mode:Mode.apply", "Helper:Helper.doubleIt", "CALLS") in edges
        assert ("Mode:Mode", "Helper:Helper.doubleIt", "CALLS") in edges
        assert ("Mode:Mode.verifyApply", "Mode:Mode.apply", "TESTS") in edges

        # Verify delta recognizes @Test method Mode:Mode.verifyApply even though Mode.java is not *Test.java
        db_head = tmp_path / "head.sqlite"
        (repo / "Helper.java").write_text(
            "public class Helper {\n    public static int doubleIt(int x) {\n        return x * 3;\n    }\n}\n",
            encoding="utf-8",
        )
        with GraphStore(db_head) as head_store:
            head_store.sync_workspace(repo)
            delta_res = compute_delta(
                store, head_store, selected_tests=("Mode:Mode.verifyApply",)
            )
            cov_by_sym = {c["symbol"]: c for c in delta_res["test_coverage"]}
            assert "Mode:Mode.verifyApply" in cov_by_sym["Helper:Helper.doubleIt"]["reachable_tests"]
            assert "Mode:Mode.verifyApply" in cov_by_sym["Helper:Helper.doubleIt"]["updated_or_selected_tests"]


def test_v0_2_1_tsx_syntax_error_rejection_vs_jsx_ampersand() -> None:
    """Verify real TSX syntax errors are rejected while bare '&' in JSX text is allowed."""
    from codegraph_slicer.ast_extractor import CodeGraphParseError, extract_file

    # Valid TSX with bare '&' in JSX text
    valid_tsx = b"export const App = () => <div>Terms & Conditions</div>;\n"
    analysis = extract_file("src/App.tsx", valid_tsx)
    assert any(s.symbol_id == "App:App" for s in analysis.symbols)

    # Broken TSX inside JSX expression must raise CodeGraphParseError
    broken_tsx = b"export const App = () => <div>{foo(}</div>;\n"
    with pytest.raises(CodeGraphParseError):
        extract_file("src/App.tsx", broken_tsx)


def test_v0_2_2_python_typed_dispatch_protocol_tables_and_skip_unparseable(
    tmp_path,
) -> None:
    """Verify Python typed/Protocol/inherited dispatch, table lookups, upstream non-hub, and --skip-unparseable."""
    from dataclasses import dataclass
    from codegraph_slicer.cli import main

    repo = tmp_path / "py_workspace"
    _write(
        repo,
        {
            "pkg/__init__.py": "def broken_init(:\n",
            "pkg/scheduler.py": """from dataclasses import dataclass
from typing import Protocol, Union

class Session(Protocol):
    def start(self) -> list[str]: ...
    def add(self, item: str) -> None: ...

class FileSession:
    def start(self) -> list[str]:
        return ["file"]
    def add(self, item: str) -> None:
        pass

class PackSession:
    def start(self) -> list[str]:
        return ["pack"]
    def add(self, item: str) -> None:
        pass

class BaseStrategy:
    def open_session(self) -> FileSession:
        return FileSession()

class FullStrategy(BaseStrategy):
    pass

class PackStrategy:
    def open_session(self) -> PackSession:
        return PackSession()

Strategy = Union[FullStrategy, PackStrategy]

@dataclass
class Job:
    strategy: Strategy | None = None

@dataclass
class Schedule:
    jobs: tuple[Job, ...] = ()

def _parse_full() -> FullStrategy:
    return FullStrategy()

def _parse_pack() -> PackStrategy:
    return PackStrategy()

PARSERS = {"full": _parse_full, "pack": _parse_pack}

def parse_strategy(kind: str) -> Strategy:
    if kind in PARSERS:
        fn = PARSERS.get(kind)
        return fn()
    return PARSERS[kind]()

def run_schedule(sched: Schedule, session: Session | None) -> list[str]:
    if session is not None:
        session.add("init")
    out = [
        tag
        for job in sched.jobs
        if job.strategy is not None
        for tag in job.strategy.open_session().start()
    ]
    if sched.jobs and sched.jobs[0].strategy is not None:
        sess = sched.jobs[0].strategy.open_session()
        out.extend(sess.start())
    return out

def helper_only() -> int:
    return 42

def main_cli() -> int:
    parse_strategy("full")
    return 0

if __name__ == "__main__":
    main_cli()
""",
            "tests/test_helper.py": """from pkg.scheduler import helper_only

def test_helper() -> None:
    assert helper_only() == 42
""",
            "tests/test_sched.py": """from pkg.scheduler import parse_strategy, run_schedule, Schedule

def test_sched() -> None:
    parse_strategy("full")
    run_schedule(Schedule(), None)
""",
        },
    )

    db = tmp_path / "graph.sqlite"
    assert main(["sync", str(repo), "--db", str(db), "--skip-unparseable"]) == 0

    with GraphStore(db) as store:
        store.assert_fresh()
        edges = {
            (r["source_id"], r["target_id"], r["kind"])
            for r in store.rows("SELECT source_id,target_id,kind FROM edges")
        }
        # Dispatch table resolution inside conditional block
        assert ("pkg.scheduler:parse_strategy", "pkg.scheduler:_parse_full", "CALLS") in edges
        assert ("pkg.scheduler:parse_strategy", "pkg.scheduler:_parse_pack", "CALLS") in edges

        # Base-class inherited method & union attribute resolution
        assert ("pkg.scheduler:run_schedule", "pkg.scheduler:BaseStrategy.open_session", "CALLS") in edges
        assert ("pkg.scheduler:run_schedule", "pkg.scheduler:PackStrategy.open_session", "CALLS") in edges

        # Return-type and Protocol structural dispatch
        assert ("pkg.scheduler:run_schedule", "pkg.scheduler:Session.add", "CALLS") in edges
        assert ("pkg.scheduler:run_schedule", "pkg.scheduler:FileSession.add", "CALLS") in edges
        assert ("pkg.scheduler:run_schedule", "pkg.scheduler:PackSession.add", "CALLS") in edges
        assert ("pkg.scheduler:run_schedule", "pkg.scheduler:Session.start", "CALLS") in edges
        assert ("pkg.scheduler:run_schedule", "pkg.scheduler:FileSession.start", "CALLS") in edges
        assert ("pkg.scheduler:run_schedule", "pkg.scheduler:PackSession.start", "CALLS") in edges

        # Upstream reachability does not leak into test_helper via module root or __main__
        up_full = set(store.upstream("pkg.scheduler:_parse_full"))
        assert "tests.test_sched:test_sched" in up_full
        assert "tests.test_helper:test_helper" not in up_full
        assert "tests.test_helper:" not in up_full


def test_resilient_anchors_public_condensation_and_return_hints(tmp_path):
    repo = tmp_path / "resilient_js"
    internal_methods = "\n".join(
        f"  _helper{i}(v) {{\n    const x = v + {i};\n    return x * 2;\n  }}"
        for i in range(1, 45)
    )
    _write(
        repo,
        {
            "src/pkg/planner.js": """export class Cursor {
  constructor(id, nowLocal) {
    this.id = id;
    this.nowLocal = nowLocal;
  }
}

export class Planner {
  computeDue(items) {
    const out = [];
    for (const it of items) {
      if (!it) {
        continue;
      }
      if (!it.id) {
        continue;
      }
      if (it.skip) {
        continue;
      }
      if (it.disabled) {
        continue;
      }
      if (it.expired) {
        continue;
      }
      out.push(it);
    }
    return out.map((it) => new Cursor(it.id, it.now_local));
  }
}
""",
            "src/pkg/validator.js": f"""import {{ Planner }} from "./planner.js";

export class PolicyValidator {{
  constructor() {{
    this.planner = new Planner();
  }}
{internal_methods}
  validateStrategyAndDestination(items) {{
    return this.planner.computeDue(items);
  }}
}}
""",
            "src/pkg/other.js": f"""export class OtherCatalog {{
  constructor() {{
    this.ready = true;
  }}
{internal_methods}
  partitionIntoPacks(files, limit) {{
    return [files.slice(0, limit)];
  }}
}}
""",
        },
    )

    db = tmp_path / "graph.sqlite"
    assert main(["sync", str(repo), "--db", str(db)]) == 0

    with GraphStore(db) as store:
        # 1. Bare file path without colon resolves to module symbol
        xml_file = json.dumps(
            slice_context(
                store,
                ["src/pkg/planner.js"],
                max_tokens=4000,
            )
        )
        assert "pkg.planner:" in xml_file

        # 2. Unambiguous file:methodName without ClassName prefix resolves cleanly
        xml_method = json.dumps(
            slice_context(
                store,
                ["src/pkg/validator.js:validateStrategyAndDestination"],
                max_tokens=4000,
            )
        )
        assert "pkg.validator:PolicyValidator.validateStrategyAndDestination" in xml_method
        # 3. Callee return expression hint is preserved on stubbed downstream dependency
        assert "returns: out.map((it) => new Cursor(it.id, it.now_local));" in xml_method

        # 4. Multi-anchor whole-class condensation preserves public methods at bottom of class
        xml_multi = json.dumps(
            slice_context(
                store,
                ["src/pkg/validator.js:PolicyValidator", "src/pkg/other.js:OtherCatalog"],
                max_tokens=1000,
            )
        )
        assert "validateStrategyAndDestination(items)" in xml_multi
        assert "partitionIntoPacks(files, limit)" in xml_multi
        assert "condensed" in xml_multi


def test_python_file_imports_builtin_diag_filter_and_js_new_uses_type(tmp_path):
    repo = tmp_path / "mixed_repo"
    _write(
        repo,
        {
            "pkg/api.py": """def ensure_positive_int(v: int) -> int:
    return int(v)

def ensure_body_object(payload: dict) -> dict:
    return dict(payload)
""",
            "pkg/server.py": '''"""
Multi-line module docstring header.
"""
from pkg.api import ensure_positive_int

def handle_request(payload: dict) -> int:
    if isinstance(payload, dict) and len(payload) > 0:
        missing_helper(payload)
        return ensure_positive_int(1)
    return 0
''',
            "src/archive/tar_helper.js": """export class ExistingPackSnapshot {
  constructor(packName, checksum, members) {
    this.packName = packName;
    this.checksum = checksum;
    this.members = members;
  }
}

export class GnuTarPackHelper {
  inspectExistingPack(tarPath) {
    const members = [];
    return new ExistingPackSnapshot(tarPath, "sha256:abc", members);
  }
}
""",
        },
    )

    db = tmp_path / "graph.sqlite"
    assert main(["sync", str(repo), "--db", str(db)]) == 0

    with GraphStore(db) as store:
        # 1. Python built-ins (isinstance, len, int, dict) are filtered from CALLS diagnostics,
        # while genuine missing imports (?missing_helper) remain in diagnostics.
        py_diags = [
            r["target"]
            for r in store.rows(
                "SELECT target FROM diagnostics WHERE source_id='pkg.server:handle_request' AND kind='CALLS'"
            )
        ]
        assert py_diags == ["?missing_helper"]

        # 2. Slicing a Python function includes the file's top-level import header
        sliced_py = slice_context(
            store,
            ["pkg/server.py:handle_request"],
            max_tokens=4000,
        )
        target_text = sliced_py["sections"]["target_implementations"][0]["text"]
        assert "from pkg.api import ensure_positive_int" in target_text

        # 3. JS method returning `new ExistingPackSnapshot(...)` emits USES_TYPE and includes ExistingPackSnapshot in types_and_schemas
        sliced_js = slice_context(
            store,
            ["src/archive/tar_helper.js:GnuTarPackHelper.inspectExistingPack"],
            max_tokens=4000,
        )
        type_symbols = {e["symbol"] for e in sliced_js["sections"]["types_and_schemas"]}
        assert "archive.tar_helper:ExistingPackSnapshot" in type_symbols






