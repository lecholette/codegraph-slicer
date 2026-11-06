"""Comprehensive tests for JavaScript (.js, .jsx, .mjs, .cjs) extraction, resolution, and slicing."""

import json
from pathlib import Path

from codegraph_slicer.ast_extractor import is_supported_source_path, is_test_path
from codegraph_slicer.cli import main
from codegraph_slicer.slicer import slice_context
from codegraph_slicer.store import GraphStore


def test_js_extension_and_test_path_classification():
    for ext in (".js", ".jsx", ".mjs", ".cjs"):
        assert is_supported_source_path(f"src/app{ext}")
        assert is_test_path(f"src/app.test{ext}")
        assert is_test_path(f"src/app.spec{ext}")
        assert is_test_path(f"__tests__/app{ext}")
    assert not is_supported_source_path("dist/app.min.js")
    assert not is_supported_source_path("dist/vendor.bundle.js")
    assert not is_supported_source_path("types/index.d.ts")


def test_commonjs_require_exports_barrels_and_local_require_isolation(tmp_path: Path):
    ws = tmp_path / "cjs_repo"
    (ws / "src" / "repos").mkdir(parents=True)
    (ws / "src" / "services").mkdir(parents=True)
    (ws / "src" / "utils").mkdir(parents=True)

    (ws / "src" / "repos" / "order_repo.cjs").write_text(
        """class OrderRepository {
  save(order) {
    return order;
  }
  static findById(id) {
    return { id };
  }
}
module.exports = OrderRepository;
""",
        encoding="utf-8",
    )

    (ws / "src" / "utils" / "formatters.js").write_text(
        """function normalizeAmount(val) {
  return Number(val);
}

exports.formatCurrency = function(amount) {
  return "$" + normalizeAmount(amount);
};

module.exports.validateOrder = (order) => {
  return Boolean(order && order.id);
};
""",
        encoding="utf-8",
    )

    (ws / "src" / "utils" / "metrics.js").write_text(
        """function recordLatency(ms) {
  return ms;
}

module.exports = {
  recordLatency,
  aliasRecord: recordLatency,
  incrementCounter(name) {
    return recordLatency(1);
  },
   flushAll: (tag) => recordLatency(0),
};
""",
        encoding="utf-8",
    )

    (ws / "src" / "utils" / "index.js").write_text(
        """module.exports = {
  ...require("./formatters"),
  ...require("./metrics"),
};
""",
        encoding="utf-8",
    )

    (ws / "src" / "services" / "checkout.js").write_text(
        """const OrderRepository = require("../repos/order_repo");
const { formatCurrency, validateOrder: checkOrder, incrementCounter } = require("../utils");

function runCheckout(order) {
  if (!checkOrder(order)) {
    return null;
  }
  const repo = new OrderRepository();
  const existing = OrderRepository.findById(order.id);
  incrementCounter("checkout");
  const label = formatCurrency(order.total);
  return repo.save({ ...existing, label });
}

function dynamicHelperA() {
  const mod = require("../utils/formatters");
  return mod.formatCurrency(10);
}

function dynamicHelperB() {
  const mod = require("../utils/metrics");
  return mod.incrementCounter("b");
}

module.exports = { runCheckout, dynamicHelperA, dynamicHelperB };
""",
        encoding="utf-8",
    )

    db_path = tmp_path / "graph.db"
    with GraphStore(db_path) as store:
        store.sync_workspace(ws)
        edges = {
            (r["source_id"], r["target_id"], r["kind"])
            for r in store.rows("SELECT source_id, target_id, kind FROM edges")
        }

        assert ("services.checkout:runCheckout", "utils.formatters:validateOrder", "CALLS") in edges
        assert ("services.checkout:runCheckout", "utils.formatters:formatCurrency", "CALLS") in edges
        assert ("services.checkout:runCheckout", "utils.metrics:incrementCounter", "CALLS") in edges
        assert ("services.checkout:runCheckout", "repos.order_repo:OrderRepository", "CALLS") in edges
        assert ("services.checkout:runCheckout", "repos.order_repo:OrderRepository.save", "CALLS") in edges
        assert ("services.checkout:runCheckout", "repos.order_repo:OrderRepository.findById", "CALLS") in edges

        # Function-scoped require() calls must not collide across functions
        assert ("services.checkout:dynamicHelperA", "utils.formatters:formatCurrency", "CALLS") in edges
        assert ("services.checkout:dynamicHelperB", "utils.metrics:incrementCounter", "CALLS") in edges
        assert ("services.checkout:dynamicHelperA", "utils.metrics:incrementCounter", "CALLS") not in edges


def test_jsdoc_types_ctor_this_fields_jsx_and_jsconfig_aliases(tmp_path: Path, capsys):
    ws = tmp_path / "esm_js_repo"
    (ws / "src" / "infra").mkdir(parents=True)
    (ws / "src" / "domain").mkdir(parents=True)
    (ws / "src" / "ui").mkdir(parents=True)
    (ws / "src" / "__tests__").mkdir(parents=True)

    (ws / "jsconfig.json").write_text(
        json.dumps(
            {
                "compilerOptions": {
                    "baseUrl": ".",
                    "paths": {"@/*": ["src/*"]},
                }
            }
        ),
        encoding="utf-8",
    )

    (ws / "src" / "infra" / "audit_logger.mjs").write_text(
        """export class AuditLogger {
  recordEvent(msg) {
    return msg;
  }
}

/**
 * @returns {AuditLogger}
 */
export function createAuditLogger() {
  return new AuditLogger();
}
""",
        encoding="utf-8",
    )

    (ws / "src" / "infra" / "user_store.js").write_text(
        """export class UserStore {
  persistUser(user) {
    return user;
  }
}
""",
        encoding="utf-8",
    )

    (ws / "src" / "domain" / "account_manager.js").write_text(
        """import { AuditLogger, createAuditLogger } from "@/infra/audit_logger";

export class AccountManager {
  /**
   * @param {import("../infra/user_store").UserStore} store
   */
  constructor(store) {
    this.store = store;
    this.logger = new AuditLogger();
    this.backupLogger = createAuditLogger();
  }

  /**
   * @param {import("../infra/user_store").UserStore} externalStore
   */
  register(user, externalStore) {
    this.logger.recordEvent("register");
    this.backupLogger.recordEvent("backup");
    externalStore.persistUser(user);
    return this.store.persistUser(user);
  }
}
""",
        encoding="utf-8",
    )

    (ws / "src" / "ui" / "AccountBadge.jsx").write_text(
        """import { createAuditLogger } from "@/infra/audit_logger";

export function AccountBadge({ username }) {
  const logger = createAuditLogger();
  logger.recordEvent(username);
  return <section className="badge">{username} &amp; Friends</section>;
}

export function DashboardView({ user }) {
  return (
    <div>
      <AccountBadge username={user} />
    </div>
  );
}
""",
        encoding="utf-8",
    )

    (ws / "src" / "__tests__" / "account_manager.test.js").write_text(
        """import { AccountManager } from "@/domain/account_manager";
import { UserStore } from "@/infra/user_store";

test("registers user", () => {
  const mgr = new AccountManager(new UserStore());
  mgr.register({ id: 1 }, new UserStore());
});
""",
        encoding="utf-8",
    )

    # Minified bundle should be ignored automatically
    (ws / "src" / "vendor.min.js").write_text("function broken({{{{", encoding="utf-8")

    db_path = tmp_path / "js_graph.db"
    with GraphStore(db_path) as store:
        store.sync_workspace(ws)
        edges = {
            (r["source_id"], r["target_id"], r["kind"])
            for r in store.rows("SELECT source_id, target_id, kind FROM edges")
        }

        assert ("domain.account_manager:AccountManager.register", "infra.audit_logger:AuditLogger.recordEvent", "CALLS") in edges
        assert ("domain.account_manager:AccountManager.register", "infra.user_store:UserStore.persistUser", "CALLS") in edges
        assert ("ui.AccountBadge:AccountBadge", "infra.audit_logger:AuditLogger.recordEvent", "CALLS") in edges
        assert ("ui.AccountBadge:DashboardView", "ui.AccountBadge:AccountBadge", "CALLS") in edges
        assert ("__tests__.account_manager.test:", "domain.account_manager:AccountManager.register", "TESTS") in edges

        # Slice on UserStore.persistUser should find AccountManager.register as upstream caller (and exclude test caller)
        sliced = slice_context(
            store, ["infra.user_store:UserStore.persistUser"], max_tokens=4000, database=db_path
        )
        caller_sids = {item["symbol"] for item in sliced["sections"]["upstream_callers"]}
        assert "domain.account_manager:AccountManager.register" in caller_sids
        assert "__tests__.account_manager.test:" not in caller_sids

    # Check CLI map markdown fences for .js and .jsx
    rc = main(["map", "--db", str(db_path), "--format", "markdown", "--max-tokens", "4000"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "```javascript" in out
    assert "```jsx" in out


def test_hybrid_ts_and_js_cross_file_interop(tmp_path: Path):
    ws = tmp_path / "hybrid_repo"
    (ws / "src").mkdir(parents=True)

    (ws / "src" / "legacy_helper.js").write_text(
        """export class LegacyFormatter {
  formatName(raw) {
    return String(raw).trim();
  }
}
""",
        encoding="utf-8",
    )

    (ws / "src" / "modern_service.ts").write_text(
        """import { LegacyFormatter } from "./legacy_helper.js";

export class ModernService {
  private fmt: LegacyFormatter = new LegacyFormatter();

  run(input: string): string {
    return this.fmt.formatName(input);
  }
}
""",
        encoding="utf-8",
    )

    (ws / "src" / "cli_entry.mjs").write_text(
        """import { ModernService } from "./modern_service.ts";

export function startCli() {
  const svc = new ModernService();
  return svc.run("hello");
}
""",
        encoding="utf-8",
    )

    db_path = tmp_path / "hybrid.db"
    with GraphStore(db_path) as store:
        store.sync_workspace(ws)
        edges = {
            (r["source_id"], r["target_id"], r["kind"])
            for r in store.rows("SELECT source_id, target_id, kind FROM edges")
        }
        assert ("modern_service:ModernService.run", "legacy_helper:LegacyFormatter.formatName", "CALLS") in edges
        assert ("cli_entry:startCli", "modern_service:ModernService.run", "CALLS") in edges
