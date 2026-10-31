"""Static graph differences; test reachability never asserts tests were executed."""

from .ast_extractor import CodeGraphError, plain_target


def _cycles(graph):
    """Iterative Tarjan SCC, avoiding Python recursion limits on large repositories."""
    index = {}
    low = {}
    active = []
    on_stack = set()
    components = set()
    for root in sorted(graph):
        if root in index:
            continue
        index[root] = low[root] = len(index)
        active.append(root)
        on_stack.add(root)
        todo = [(root, iter(sorted(graph[root])))]
        while todo:
            node, children = todo[-1]
            child = next(children, None)
            if child is not None:
                if child not in index:
                    index[child] = low[child] = len(index)
                    active.append(child)
                    on_stack.add(child)
                    todo.append((child, iter(sorted(graph[child]))))
                elif child in on_stack:
                    low[node] = min(low[node], index[child])
                continue
            todo.pop()
            if todo:
                parent = todo[-1][0]
                low[parent] = min(low[parent], low[node])
            if low[node] == index[node]:
                component = []
                while True:
                    member = active.pop()
                    on_stack.remove(member)
                    component.append(member)
                    if member == node:
                        break
                if len(component) > 1 or node in graph[node]:
                    components.add(tuple(sorted(component)))
    return components


def _graph_cycles(store):
    modules = {r["module"]: set() for r in store.rows("SELECT module FROM files")}
    calls = {r["symbol_id"]: set() for r in store.rows("SELECT symbol_id FROM symbols")}
    for edge in store.rows("SELECT source_id,target_id,kind FROM edges"):
        source, target = edge["source_id"], edge["target_id"]
        if edge["kind"] == "IMPORTS":
            src, dst = source.split(":")[0], target.split(":")[0]
            if src != dst:
                modules[src].add(dst)
        elif edge["kind"] == "CALLS":
            calls[source].add(target)
    return _cycles(modules) | _cycles(calls)


def _prefix(value, prefix):
    return value == prefix or value.startswith(prefix + ".")


def compute_delta(
    base_store,
    head_store,
    boundary_rules=(),
    *,
    selected_tests=(),
    cx_threshold=10,
    fan_out_threshold=10,
):
    def edges(store):
        return {
            (r["source_id"], r["target_id"], r["kind"]): r["line"]
            for r in store.rows("SELECT source_id,target_id,kind,line FROM edges")
        }

    def fanout(store):
        return {
            r["source_id"]: r["n"]
            for r in store.rows("""SELECT source_id,count(DISTINCT target_id) n
            FROM edges WHERE kind IN ('CALLS','IMPORTS') GROUP BY source_id""")
        }

    base_edges, head_edges = edges(base_store), edges(head_store)
    base = {r["symbol_id"]: r for r in base_store.rows("SELECT * FROM symbols")}
    head = {r["symbol_id"]: r for r in head_store.rows("SELECT * FROM symbols")}
    base_fanout, head_fanout = fanout(base_store), fanout(head_store)
    source_changed = set(base.keys() ^ head.keys())
    modified = set(source_changed)
    drift = []
    for sid in sorted(base.keys() | head.keys()):
        before, after = base.get(sid), head.get(sid)
        if (
            before
            and after
            and any(before[k] != after[k] for k in ("body", "signature", "kind", "cx"))
        ):
            source_changed.add(sid)
            modified.add(sid)
        cx_delta = (after["cx"] if after else 0) - (before["cx"] if before else 0)
        fan_delta = head_fanout.get(sid, 0) - base_fanout.get(sid, 0)
        if cx_delta or fan_delta:
            drift.append(
                {
                    "symbol": sid,
                    "cx_delta": cx_delta,
                    "fan_out_delta": fan_delta,
                    "threshold_exceeded": cx_delta >= cx_threshold
                    or fan_delta >= fan_out_threshold,
                }
            )
    # A reference can rebind after another file changes even when its source bytes are cached.
    modified.update(
        e[0]
        for e in base_edges.keys() ^ head_edges.keys()
        if e[2] != "DEFINES"
    )
    violations = set()
    for edge in head_store.rows(
        "SELECT source_id,target_id FROM raw_edges WHERE kind='IMPORTS'"
    ):
        source = edge["source_id"].split(":", 1)[0]
        target = plain_target(edge["target_id"]).replace(":", ".")
        for prefix, forbidden in boundary_rules:
            if _prefix(source, prefix) and _prefix(target, forbidden):
                violations.add((source, target))
    tests = {
        sid
        for sid, row in head.items()
        if any(part == "tests" for part in row["file_path"].split("/"))
        or row["file_path"].split("/")[-1].startswith("test_")
    }
    selected = set()
    for name in selected_tests:
        matches = {
            sid
            for sid in tests
            if sid == name
            or head[sid]["file_path"] == name
            or head[sid]["module"] == name
        }
        if not matches:
            raise CodeGraphError(f"unobserved test selection: {name}")
        selected.update(matches)
    updated_tests = tests & source_changed
    affected = set()
    gaps = set()
    coverage = []
    for sid in sorted(modified):
        upstream = set(head_store.upstream(sid)) | set(base_store.upstream(sid))
        reachable_tests = upstream & tests
        evidence = reachable_tests & (updated_tests | selected)
        current = head.get(sid, base.get(sid))
        if current is None:
            continue
        is_test = any(part == "tests" for part in current["file_path"].split("/")) or (
            current["file_path"].split("/")[-1].startswith("test_")
        )
        for node in upstream:
            row = head.get(node, base.get(node))
            if row and (
                row["module"] == "__main__"
                or row["module"].endswith(".__main__")
                or any(
                    part in {"cli", "scripts", "mcp"}
                    for part in row["module"].split(".")
                )
                or row["qualname"] == "main"
            ):
                affected.add(node)
        if not is_test:
            coverage.append(
                {
                    "symbol": sid,
                    "reachable_tests": sorted(reachable_tests),
                    "updated_or_selected_tests": sorted(evidence),
                }
            )
            if not evidence:
                gaps.add(sid)
    return {
        "added_edges": [
            [*e, head_edges[e]] for e in sorted(head_edges.keys() - base_edges.keys())
        ],
        "removed_edges": [
            [*e, base_edges[e]] for e in sorted(base_edges.keys() - head_edges.keys())
        ],
        "complexity_drift": drift,
        "boundary_violations": [
            {"source": s, "target": t} for s, t in sorted(violations)
        ],
        "modified_symbols": sorted(modified),
        "untested_modified_symbols": sorted(gaps),
        "affected_entrypoints": sorted(affected),
        "new_cycles": [
            list(c)
            for c in sorted(_graph_cycles(head_store) - _graph_cycles(base_store))
        ],
        "test_coverage": coverage,
        "test_gap_note": "Static TESTS reachability and changed/selected source only; no execution or behavioral coverage is claimed.",
    }
