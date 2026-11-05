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
        # Method overload link: OrderRepository.find -> OrderRepository.find#2
        assert (
            "com.acme.repo.OrderRepository:OrderRepository.find",
            "com.acme.repo.OrderRepository:OrderRepository.find#2",
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
        for i in range(1, 16)
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
        assert "// ... (+11 more methods)" in m1_text

        # M4: slicing on OrderProcessor + saveOrder under tight budget (>=1000) condenses with // ... (N lines condensed)
        sliced_m4 = slice_context(
            base_store,
            [
                "domain.order:OrderProcessor",
                "infra.db:saveOrder",
            ],
            max_tokens=1000,
        )
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

