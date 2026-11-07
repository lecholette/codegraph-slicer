"""Static graph differences; test reachability never asserts tests were executed."""

from dataclasses import dataclass
from .ast_extractor import CodeGraphError, is_test_path, plain_target
from .slicer import rendered_tokens


@dataclass(frozen=True)
class DeltaReport:
    boundary_violations: list
    new_cycles: list
    complexity_drift: list
    added_edges: list
    removed_edges: list
    modified_symbols: list
    affected_entrypoints: list
    untested_modified_symbols: list
    test_coverage: list
    test_gap_note: str
    raw: dict

    @classmethod
    def from_dict(cls, result: dict) -> "DeltaReport":
        if isinstance(result, cls):
            return result
        return cls(
            boundary_violations=list(result["boundary_violations"]),
            new_cycles=list(result["new_cycles"]),
            complexity_drift=list(result["complexity_drift"]),
            added_edges=list(result["added_edges"]),
            removed_edges=list(result["removed_edges"]),
            modified_symbols=list(result["modified_symbols"]),
            affected_entrypoints=list(result["affected_entrypoints"]),
            untested_modified_symbols=list(result["untested_modified_symbols"]),
            test_coverage=list(result["test_coverage"]),
            test_gap_note=str(result["test_gap_note"]),
            raw=dict(result),
        )


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
        elif edge["kind"] == "CALLS" and source != target:
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
    _earliest_edge_lines=False,
):
    with base_store.read_snapshot(), head_store.read_snapshot():
        return _compute_delta_in_snapshot(
            base_store,
            head_store,
            boundary_rules,
            selected_tests=selected_tests,
            cx_threshold=cx_threshold,
            fan_out_threshold=fan_out_threshold,
            _earliest_edge_lines=_earliest_edge_lines,
        )


def _compute_delta_in_snapshot(
    base_store,
    head_store,
    boundary_rules=(),
    *,
    selected_tests=(),
    cx_threshold=10,
    fan_out_threshold=10,
    _earliest_edge_lines=False,
):
    def edges(store):
        rows = store.rows(
            "SELECT source_id,target_id,kind,line FROM edges ORDER BY source_id,target_id,kind,line"
        )
        if not _earliest_edge_lines:
            return {(r["source_id"], r["target_id"], r["kind"]): r["line"] for r in rows}
        found = {}
        for r in rows:
            key = (r["source_id"], r["target_id"], r["kind"])
            found[key] = min(found.get(key, r["line"]), r["line"])
        return found

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
    test_sources = {
        src for (src, _tgt, kind) in head_edges if kind == "TESTS"
    } | {
        src for (src, _tgt, kind) in base_edges if kind == "TESTS"
    }
    tests = {
        sid
        for sid, row in head.items()
        if is_test_path(row["file_path"]) or sid in test_sources
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
        if sid.endswith(":"):
            evidence = reachable_tests & (updated_tests | selected)
        else:
            call_upstream = set(head_store.test_upstream(sid)) | set(base_store.test_upstream(sid))
            call_tests = call_upstream & tests
            call_test_mods = {mod + sep for mod, sep, _ in (t.partition(":") for t in call_tests)}
            evidence = (call_tests | (reachable_tests & call_test_mods)) & (updated_tests | selected)
        current = head.get(sid, base.get(sid))
        if current is None:
            continue
        is_test = is_test_path(current["file_path"]) or sid in test_sources
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


def summarize_delta(
    result, max_tokens=8000, *, full=False, selected_tests=False, render_estimator=None
):
    """Render a stable, priority-ranked delta within its selected output budget."""
    from .slicer import InvalidBudgetError

    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        raise InvalidBudgetError("max_tokens must be a positive integer")
    report = DeltaReport.from_dict(result)
    kinds = (
        "boundary_violations", "new_cycles", "complexity_drift", "added_edges",
        "removed_edges", "modified_symbols", "affected_entrypoints",
        "untested_modified_symbols", "test_coverage",
    )
    counts = {kind: len(getattr(report, kind)) for kind in kinds}
    gaps = report.test_coverage
    statuses = []
    for item in gaps:
        statuses.append(
            "updated_or_selected_evidence" if item["updated_or_selected_tests"]
            else "reachable_but_unselected" if item["reachable_tests"]
            else "no_static_test_reaches"
        )
    imports = [
        {"change": change, "source": edge[0], "target": edge[1], "line": edge[3]}
        for change, edges in (("added", report.added_edges), ("removed", report.removed_edges))
        for edge in edges if edge[2] == "IMPORTS"
    ]
    counts["import_changes"] = len(imports)
    summary = {
        "counts": counts,
        "test_selection": "selected" if selected_tests else "none_selected",
        "test_selection_note": (
            "No test was selected; an unobserved requested path is refused as unknown."
            if not selected_tests else "Requested test paths were resolved in the head snapshot."
        ),
        "test_gap_status_counts": {name: statuses.count(name) for name in (
            "updated_or_selected_evidence", "reachable_but_unselected", "no_static_test_reaches"
        )},
        "test_gap_note": report.test_gap_note,
        "retrieve_details": "rerun with --full --max-tokens N",
    }
    if counts["complexity_drift"] > 0 and render_estimator is None:
        summary["complexity_threshold_exceeded"] = sum(
            1 for item in report.complexity_drift if item.get("threshold_exceeded")
        )
    if full:
        payload = dict(report.raw)
        payload["import_changes"] = imports
        payload["omitted"] = {**{kind: 0 for kind in kinds}, "import_changes": 0}
        payload["test_selection"] = summary["test_selection"]
        payload["test_selection_note"] = summary["test_selection_note"]
    else:
        payload = {"summary": summary, "omitted": dict(counts)}
        payload["omitted"]["added_edges"] = counts["added_edges"]
        payload["omitted"]["removed_edges"] = counts["removed_edges"]
        priority = (
            ("boundary_violations", report.boundary_violations, None),
            ("new_cycles", report.new_cycles, None),
            ("untested_modified_symbols", report.untested_modified_symbols, 5 if render_estimator is None and max_tokens == 8000 else None),
            ("test_coverage", [
                {**item, "status": status}
                for item, status in zip(report.test_coverage, statuses)
            ], 5 if render_estimator is None and max_tokens == 8000 else None),
            ("complexity_drift", sorted(
                (item for item in report.complexity_drift if item["threshold_exceeded"]),
                key=lambda item: (-item["cx_delta"], -item["fan_out_delta"], item["symbol"]),
            ), None),
            ("import_changes", imports, 8 if render_estimator is None else None),
            ("affected_entrypoints", report.affected_entrypoints, 5 if render_estimator is None else None),
            ("complexity_drift", sorted(
                (item for item in report.complexity_drift if not item["threshold_exceeded"]),
                key=lambda item: (-item["cx_delta"], -item["fan_out_delta"], item["symbol"]),
            ), 5 if render_estimator is None else None),
            ("modified_symbols", report.modified_symbols, 5 if render_estimator is None else None),
        )
        # Edge lists are intentionally not emitted in summary mode. Their exact
        # counts remain in summary/omitted and are retrievable with --full.
        payload["omitted"]["import_changes"] = len(imports)
        if render_estimator is not None and render_estimator(
            _with_serialized_tokens(payload)
        ) > max_tokens:
            raise InvalidBudgetError("delta summary metadata exceeds rendered Markdown budget")
        for kind, values, cap in priority:
            key = kind
            emitted_in_pass = 0
            for value in values:
                if cap is not None and emitted_in_pass >= cap:
                    break
                payload.setdefault(key, []).append(value)
                payload["omitted"][kind] -= 1
                candidate = _with_serialized_tokens(payload)
                if candidate["serialized_tokens"] > max_tokens or (
                    render_estimator is not None and render_estimator(candidate) > max_tokens
                ):
                    payload[key].pop()
                    if not payload[key]:
                        del payload[key]
                    payload["omitted"][kind] += 1
                    continue
                emitted_in_pass += 1
        payload["omitted"]["import_changes"] = len(imports) - len(payload.get("import_changes", []))
    payload = _with_serialized_tokens(payload)
    if payload["serialized_tokens"] > max_tokens:
        raise InvalidBudgetError(
            f"delta summary metadata requires {payload['serialized_tokens']} tokens; budget is {max_tokens}"
        )
    if render_estimator is not None and render_estimator(payload) > max_tokens:
        message = "complete delta payload exceeds rendered Markdown budget" if full else "delta summary metadata exceeds rendered Markdown budget"
        raise InvalidBudgetError(message)
    return payload


def project_delta_v2(
    result,
    max_tokens=8000,
    *,
    anchors=(),
    kinds=(),
    edge_sample_limit=20,
    selected_tests=False,
    render_estimator=None,
):
    """Return a filtered, sampled delta with one authoritative total per record kind."""
    from .slicer import InvalidBudgetError

    if not isinstance(edge_sample_limit, int) or isinstance(edge_sample_limit, bool) or edge_sample_limit < 1:
        raise InvalidBudgetError("edge sample limit must be a positive integer")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        raise InvalidBudgetError("max_tokens must be a positive integer")
    categories = (
        "boundary_violations", "new_cycles", "complexity_drift", "added_edges",
        "removed_edges", "modified_symbols", "affected_entrypoints",
        "untested_modified_symbols", "test_coverage", "import_changes",
    )
    requested_anchors = tuple(anchors)
    requested_kinds = tuple(k.upper() for k in kinds)
    rows = {name: list(result[name]) for name in categories if name != "import_changes"}
    rows["import_changes"] = [
        {"change": change, "source": edge[0], "target": edge[1], "line": edge[3]}
        for change, edges in (("added", result["added_edges"]), ("removed", result["removed_edges"]))
        for edge in edges if edge[2] == "IMPORTS"
    ]
    # In v2 each changed import has one home: the richer import_changes bucket.
    rows["added_edges"] = [edge for edge in rows["added_edges"] if edge[2] != "IMPORTS"]
    rows["removed_edges"] = [edge for edge in rows["removed_edges"] if edge[2] != "IMPORTS"]

    def has_anchor(value):
        if not requested_anchors:
            return True
        if isinstance(value, str):
            return any(
                value == anchor
                or (anchor.endswith(":") and value.startswith(anchor))
                or value.startswith((anchor + ".", anchor + ":"))
                for anchor in requested_anchors
            )
        if isinstance(value, dict):
            return any(has_anchor(item) for item in value.values())
        if isinstance(value, (list, tuple)):
            return any(has_anchor(item) for item in value)
        return False

    edge_categories = {"added_edges", "removed_edges", "import_changes"}
    for name in categories:
        filtered = []
        for item in rows[name]:
            if not has_anchor(item):
                continue
            if requested_kinds:
                if name in edge_categories:
                    if name.upper() not in requested_kinds:
                        edge_kind = item[2] if name != "import_changes" else "IMPORTS"
                        if edge_kind not in requested_kinds:
                            continue
                elif name.upper() not in requested_kinds:
                    continue
            filtered.append(item)
        rows[name] = filtered

    # Collapse identical import projection rows; legacy output retains its historical rows.
    rows["import_changes"] = list({
        (item["change"], item["source"], item["target"], item["line"]): item
        for item in rows["import_changes"]
    }.values())
    totals = {name: len(values) for name, values in rows.items()}
    samples = {name: list(values) for name, values in rows.items()}
    for name in ("added_edges", "removed_edges", "import_changes"):
        samples[name] = samples[name][:edge_sample_limit]
    omitted = {name: totals[name] - len(samples[name]) for name in categories}
    payload = {
        "schema": "python-delta-v2",
        "filters": {"anchors": list(requested_anchors), "kinds": list(requested_kinds)},
        "totals": totals,
        "samples": samples,
        "omitted": omitted,
        "test_selection": "selected" if selected_tests else "none_selected",
        "test_gap_note": result["test_gap_note"],
    }
    payload = _with_serialized_tokens(payload)
    while (
        payload["serialized_tokens"] > max_tokens
        or (render_estimator is not None and render_estimator(payload) > max_tokens)
    ) and any(samples[name] for name in categories):
        # Preserve all required totals and trim optional detail in stable category order.
        name = next(
            (
                n for n in (
                    "added_edges", "removed_edges", "import_changes", "complexity_drift",
                    "modified_symbols", "affected_entrypoints", "test_coverage",
                    "untested_modified_symbols", "new_cycles", "boundary_violations",
                ) if samples[n]
            ),
            None,
        )
        if name is None:
            break
        samples[name].pop()
        omitted[name] += 1
        payload["samples"] = samples
        payload["omitted"] = omitted
        payload = _with_serialized_tokens(payload)
    if payload["serialized_tokens"] > max_tokens:
        raise InvalidBudgetError(
            f"delta v2 required metadata requires {payload['serialized_tokens']} tokens; budget is {max_tokens}"
        )
    if render_estimator is not None and render_estimator(payload) > max_tokens:
        raise InvalidBudgetError("delta v2 required metadata exceeds rendered Markdown budget")
    return payload


def _with_serialized_tokens(payload):
    """Return a copy with the printed compact-JSON estimate including its field."""
    import json

    result = dict(payload)
    result["serialized_tokens"] = 0
    while True:
        estimate = rendered_tokens(
            json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        )
        if result["serialized_tokens"] == estimate:
            return result
        result["serialized_tokens"] = estimate
