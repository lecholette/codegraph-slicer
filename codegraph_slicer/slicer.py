"""Bounded contexts and output budget estimates."""

import ast
import json
import difflib
import shlex
import textwrap
from .ast_extractor import CodeGraphError


class AnchorNotFoundError(CodeGraphError):
    pass


class InvalidBudgetError(CodeGraphError):
    pass


class RequiredAnchorOverflow(CodeGraphError):
    pass


def serialized_tokens(value) -> int:
    return tokens_for_length(
        len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    )


def tokens_for_length(n: int) -> int:
    """Estimate tokens from a total printed character count."""
    return n // 4 + 1


def rendered_tokens(text: str) -> int:
    """Estimate the exact printed text, including its trailing newline."""
    return tokens_for_length(len(text) + 1)


def _with_output_tokens(result, render_estimator=None):
    result = dict(result)
    result["tokens"] = 0
    while True:
        if render_estimator is None:
            estimate = rendered_tokens(
                json.dumps(result, ensure_ascii=False, separators=(",", ":"))
            )
        else:
            estimate = render_estimator(result)
        if result["tokens"] == estimate:
            return result
        result["tokens"] = estimate


def _anchor(store, anchor):
    exact = store.rows("SELECT * FROM symbols WHERE symbol_id=?", (anchor,))
    if exact:
        return exact[0]
    path, separator, target = anchor.rpartition(":")
    if separator and path.endswith(".py"):
        if target.isdigit():
            try:
                line = int(target)
            except ValueError:
                raise AnchorNotFoundError(anchor) from None
            if line > 2**63 - 1:
                raise AnchorNotFoundError(anchor)
        else:
            line = 0
        if line > 0:
            rows = store.rows(
                """SELECT * FROM symbols WHERE file_path=?
                AND start_line<=? AND end_line>=?
                ORDER BY end_line-start_line, length(qualname) DESC, symbol_id""",
                (path, line, line),
            )
        else:
            rows = store.rows(
                "SELECT * FROM symbols WHERE file_path=? AND qualname=?", (path, target)
            )
        if len(rows) >= 1:
            return rows[0]
    error = AnchorNotFoundError(anchor)
    rows = sorted(
        store.rows("SELECT file_path,module,qualname FROM symbols"),
        key=lambda row: (row["file_path"], row["qualname"]),
    )
    aliases = {}
    for row in rows:
        copyable = f"{row['file_path']}:{row['qualname']}"
        aliases[copyable] = copyable
        aliases[f"{row['module']}:{row['qualname']}"] = copyable
    matches = difflib.get_close_matches(anchor, sorted(aliases), n=6, cutoff=0.25)
    error.nearest_anchors = list(dict.fromkeys(aliases[item] for item in matches))[:3]
    revision = store.rows("SELECT value FROM metadata WHERE key='snapshot_revision'")
    error.snapshot_revision = revision[0]["value"] if revision else ""
    raise error


def query_call_impact(
    store, anchor, *, max_depth=3, max_nodes=200, unknown_boundary_limit=0,
    max_tokens=8000, database=None, render_estimator=None, output_format="json"
):
    """Return bounded transitive callers over resolved CALLS edges only."""
    if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth <= 0:
        raise InvalidBudgetError("max_depth must be a positive integer")
    if not isinstance(max_nodes, int) or isinstance(max_nodes, bool) or max_nodes <= 0:
        raise InvalidBudgetError("max_nodes must be a positive integer")
    if (
        not isinstance(unknown_boundary_limit, int)
        or isinstance(unknown_boundary_limit, bool)
        or unknown_boundary_limit < 0
    ):
        raise InvalidBudgetError("unknown boundary limit must be a non-negative integer")
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        raise InvalidBudgetError("max_tokens must be a positive integer")
    target = _anchor(store, anchor)
    if target["kind"] not in {"function", "async_function", "method"}:
        raise AnchorNotFoundError(f"impact requires an observed function or method anchor: {anchor}")

    anchor_id = target["symbol_id"]
    visited = {anchor_id}
    callers = []
    frontier = [(anchor_id, 0)]
    depth_boundary_nodes = set()
    while frontier:
        next_frontier = []
        for callee, depth in sorted(frontier):
            incoming = store.rows(
                """SELECT e.source_id,e.target_id,e.kind,e.line FROM edges e
                   JOIN symbols s ON s.symbol_id=e.source_id
                   WHERE e.target_id=? AND e.kind='CALLS'
                     AND s.kind IN ('function','async_function','method')
                   ORDER BY e.source_id,e.line""",
                (callee,),
            )
            for edge in incoming:
                caller = edge["source_id"]
                if caller in visited:
                    continue
                visited.add(caller)
                callers.append({
                    "symbol": caller,
                    "depth": depth + 1,
                    "witness": {
                        "from": caller,
                        "to": edge["target_id"],
                        "kind": edge["kind"],
                        "line": edge["line"],
                    },
                })
                if depth + 1 < max_depth:
                    next_frontier.append((caller, depth + 1))
                else:
                    depth_boundary_nodes.add(caller)
        frontier = next_frontier

    depth_limit_hit = False
    for caller in depth_boundary_nodes:
        possible_callers = store.rows(
            """SELECT e.source_id FROM edges e JOIN symbols s
                ON s.symbol_id=e.source_id WHERE e.target_id=?
                AND e.kind='CALLS' AND s.kind IN ('function','async_function','method')
                ORDER BY e.source_id""",
            (caller,),
        )
        if any(row["source_id"] not in visited for row in possible_callers):
            depth_limit_hit = True
            break
    boundary_count_rows = store.rows(
        "SELECT count(*) AS count FROM diagnostics WHERE kind='CALLS'"
    )
    boundary_count = boundary_count_rows[0]["count"]
    rows = store.rows(
        """SELECT source_id,target,kind,line,reason FROM diagnostics
            WHERE kind='CALLS' ORDER BY source_id,line,target,kind,reason LIMIT ?""",
        (unknown_boundary_limit,),
    )
    unknown_boundaries = [
        {
            "from": row["source_id"],
            "target": row["target"],
            "kind": row["kind"],
            "line": row["line"],
            "reason": row["reason"],
        }
        for row in rows
    ]
    unknown_boundaries_omitted = max(0, boundary_count - len(unknown_boundaries))
    callers.sort(key=lambda item: (item["depth"], item["symbol"]))
    compact = [
        {"symbol": item["symbol"], "depth": item["depth"],
         "to": None if item["depth"] == 1 else item["witness"]["to"],
         "line": item["witness"]["line"]}
        for item in callers
    ]
    columns = ["symbol", "depth", "to", "line"]

    def layout(rows):
        columnar = {"columns": columns, "rows": [[row[key] for key in columns] for row in rows]}
        def size(value):
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
        return columnar if size(columnar) < size(rows) else rows

    result = {
        "profile": "python-call-impact-v2",
        "anchor": anchor_id,
        "query": {"max_depth": max_depth, "max_nodes": max_nodes,
                  "unknown_boundaries": unknown_boundary_limit},
        "callers": layout([]),
        "omitted": {"max_nodes": max(0, len(compact) - max_nodes), "budget": 0},
        "unknown_boundaries": [],
        "unknown_boundary_count": boundary_count,
        "unknown_boundaries_omitted": boundary_count,
        "unknown_boundary_scope": "indexed_snapshot; unresolved calls are not linked to the anchor",
        "truncated": False,
        "retrieve_details": "",
    }

    def measured(rows, boundaries):
        candidate = dict(result)
        candidate["callers"] = layout(rows)
        candidate["omitted"] = {"max_nodes": max(0, len(compact) - max_nodes),
                                "budget": min(len(compact), max_nodes) - len(rows)}
        candidate["unknown_boundaries"] = boundaries
        candidate["unknown_boundaries_omitted"] = boundary_count - len(boundaries)
        candidate["truncated"] = bool(
            depth_limit_hit or any(candidate["omitted"].values())
        )
        return _with_output_tokens(candidate, render_estimator)

    # Recovery expands the node bound and admits every requested unknown sample.
    # Its own command and token field are included in the fixed-point budget.
    recovery = dict(result)
    recovery["query"] = dict(result["query"], max_nodes=max(max_nodes, len(compact)))
    recovery["callers"] = layout(compact)
    recovery["omitted"] = {"max_nodes": 0, "budget": 0}
    recovery["unknown_boundaries"] = unknown_boundaries
    recovery["unknown_boundaries_omitted"] = unknown_boundaries_omitted
    recovery["truncated"] = bool(depth_limit_hit)
    recovery_budget = 1
    while True:
        command = shlex.join([
            "codegraph-slicer", "impact", anchor_id, "--db", str(database or store.rows("PRAGMA database_list")[0]["file"]),
            "--snapshot", "--max-depth", str(max_depth), "--max-nodes",
            str(max(max_nodes, len(compact))), "--unknown-boundaries",
            str(unknown_boundary_limit), "--format", output_format,
            "--max-tokens", str(recovery_budget),
        ])
        recovery["retrieve_details"] = command
        estimate = _with_output_tokens(recovery, render_estimator)["tokens"]
        if estimate <= recovery_budget:
            break
        recovery_budget = estimate
    result["retrieve_details"] = command
    eligible = compact[:max_nodes]
    selected = eligible[:1]
    output = measured(selected, [])
    if output["tokens"] > max_tokens:
        raise InvalidBudgetError(
            f"impact header and first caller require {output['tokens']} tokens; budget is {max_tokens}"
        )
    # Keep a deterministic ordered prefix, then spend residual room on unknowns.
    for row in eligible[1:]:
        candidate = measured(selected + [row], [])
        if candidate["tokens"] > max_tokens:
            break
        selected.append(row)
        output = candidate
    boundaries = []
    for boundary in unknown_boundaries:
        candidate = measured(selected, boundaries + [boundary])
        if candidate["tokens"] > max_tokens:
            break
        boundaries.append(boundary)
        output = candidate
    return output



def build_slice_outline(
    store, anchors, max_tokens=8000, *, include_imports=False, render_estimator=None
):
    """Return a bounded, body-free entity graph for a slice's local neighborhood."""
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        raise InvalidBudgetError("max_tokens must be a positive integer")
    if not anchors:
        raise AnchorNotFoundError("at least one anchor is required")

    allowed_kinds = ["CALLS", "USES_TYPE", "INHERITS", "DEFINES"]
    if include_imports:
        allowed_kinds.append("IMPORTS")
    selected = {row["symbol_id"] for row in (_anchor(store, anchor) for anchor in anchors)}
    roots = set(selected)
    for symbol_id in sorted(roots):
        for row in store.rows(
            """WITH RECURSIVE closure(node,depth) AS (
                SELECT ?,0 UNION SELECT e.target_id,c.depth+1 FROM edges e
                JOIN closure c ON e.source_id=c.node
                WHERE e.kind IN ('USES_TYPE','INHERITS') AND c.depth<2
            ) SELECT DISTINCT node FROM closure WHERE depth>0""",
            (symbol_id,),
        ):
            selected.add(row["node"])
        for row in store.rows(
            "SELECT source_id AS node FROM edges WHERE target_id=? AND kind='CALLS' "
            "UNION SELECT target_id AS node FROM edges WHERE source_id=? AND kind='CALLS'",
            (symbol_id, symbol_id),
        ):
            selected.add(row["node"])

    # Include lexical definitions but leave module symbols out unless import
    # context was requested.
    for symbol_id in tuple(sorted(selected)):
        rows = store.rows("SELECT file_path,module,qualname FROM symbols WHERE symbol_id=?", (symbol_id,))
        if not rows:
            continue
        row = rows[0]
        parent = row["module"] + ":" + row["qualname"].rpartition(".")[0]
        while parent != row["module"] + ":" and parent not in selected:
            exists = store.rows("SELECT symbol_id FROM symbols WHERE symbol_id=?", (parent,))
            if not exists:
                break
            selected.add(parent)
            parent = parent.rpartition(".")[0]
            if ":" not in parent:
                parent = row["module"] + ":"
    if include_imports:
        module_ids = {
            row["module"] + ":"
            for row in store.rows(
                f"SELECT DISTINCT module FROM symbols WHERE symbol_id IN ({','.join('?' for _ in selected)})",
                tuple(sorted(selected)),
            )
        } if selected else set()
        selected.update(module_ids)
        if module_ids:
            placeholders = ",".join("?" for _ in module_ids)
            imported = store.rows(
                f"SELECT DISTINCT target_id FROM edges WHERE kind='IMPORTS' AND source_id IN ({placeholders})",
                tuple(sorted(module_ids)),
            )
            selected.update(row["target_id"] for row in imported)

    rows_by_id = {
        row["symbol_id"]: row
        for row in store.rows(
            "SELECT symbol_id,file_path,module,qualname,kind,start_line,signature "
            f"FROM symbols WHERE symbol_id IN ({','.join('?' for _ in selected)})",
            tuple(sorted(selected)),
        )
    } if selected else {}
    ordered = sorted(rows_by_id.values(), key=lambda row: (
        row["file_path"], row["start_line"], row["symbol_id"]
    ))
    local_id = {row["symbol_id"]: f"e{index:04d}" for index, row in enumerate(ordered, 1)}
    entities = [
        {
            "id": local_id[row["symbol_id"]],
            "symbol": row["symbol_id"],
            "path": row["file_path"],
            "name": row["qualname"].rsplit(".", 1)[-1],
            "kind": row["kind"],
            "line": row["start_line"],
            "signature": row["signature"],
        }
        for row in ordered
    ]
    placeholders = ",".join("?" for _ in local_id)
    edge_rows = store.rows(
        f"SELECT source_id,target_id,kind,line FROM edges WHERE kind IN ({','.join('?' for _ in allowed_kinds)}) "
        f"AND (source_id IN ({placeholders}) OR target_id IN ({placeholders})) "
        "ORDER BY source_id,target_id,kind,line",
        (*allowed_kinds, *local_id, *local_id),
    ) if local_id else []
    edges = []
    omitted_edges = 0
    for row in edge_rows:
        if row["source_id"] not in local_id or row["target_id"] not in local_id:
            omitted_edges += 1
            continue
        edges.append({
            "from": local_id[row["source_id"]],
            "to": local_id[row["target_id"]],
            "kind": row["kind"],
            "line": row["line"],
        })
    diagnostics = []
    diagnostic_counts = {}
    if local_id:
        diagnostic_rows = store.rows(
            f"SELECT source_id,target,kind,line,reason FROM diagnostics WHERE source_id IN ({placeholders}) "
            "ORDER BY source_id,line,target,kind,reason",
            tuple(local_id),
        )
        for row in diagnostic_rows:
            if row["source_id"] in roots:
                diagnostics.append({
                    "source": local_id[row["source_id"]],
                    "target": row["target"],
                    "kind": row["kind"],
                    "line": row["line"],
                    "reason": row["reason"],
                })
            else:
                source = local_id[row["source_id"]]
                diagnostic_counts[source] = diagnostic_counts.get(source, 0) + 1

    omitted_diagnostics = 0

    def result_payload():
        payload = {
            "schema_version": 1,
            "tokens": 0,
            "entities": entities,
            "edges": edges,
            "diagnostics": diagnostics,
            "diagnostic_counts": dict(diagnostic_counts),
        }
        omitted = {"edges": omitted_edges} if omitted_edges else {}
        if omitted_diagnostics:
            omitted["diagnostics"] = omitted_diagnostics
        if omitted:
            payload["omitted"] = omitted
        return _with_output_tokens(payload, render_estimator)

    payload = result_payload()
    while payload["tokens"] > max_tokens and diagnostics:
        diagnostics.pop()
        omitted_diagnostics += 1
        payload = result_payload()
    while payload["tokens"] > max_tokens and diagnostic_counts:
        source = next(reversed(diagnostic_counts))
        omitted_diagnostics += diagnostic_counts.pop(source)
        payload = result_payload()
    if payload["tokens"] > max_tokens:
        raise RequiredAnchorOverflow(
            f"required slice outline needs {payload['tokens']} tokens; budget is {max_tokens}"
        )
    return payload


def slice_context(
    store, anchors, max_tokens=8000, include_callee_bodies=False, *,
    render_estimator=None, database=None,
):
    if (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or max_tokens <= 0
    ):
        raise InvalidBudgetError("max_tokens must be a positive integer")
    if not anchors:
        raise AnchorNotFoundError("at least one anchor is required")
    chosen = {row["symbol_id"]: row for row in (_anchor(store, a) for a in anchors)}
    sections = {
        name: []
        for name in (
            "types_and_schemas",
            "upstream_callers",
            "target_implementations",
            "downstream_dependencies",
        )
    }

    def item(row, full):
        text = row["body"] if full else row["signature"]
        return {
            "symbol": row["symbol_id"],
            "text": f"{row['file_path']}:{row['start_line']}-{row['end_line']}\n{text}",
        }

    types = {}
    callers = {}
    callees = {}
    for sid, row in sorted(chosen.items()):
        sections["target_implementations"].append(item(row, True))
        for related in store.rows(
            """WITH RECURSIVE closure(node,depth) AS (
                SELECT ?,0 UNION SELECT e.target_id,c.depth+1 FROM edges e
                JOIN closure c ON e.source_id=c.node
                WHERE e.kind IN ('USES_TYPE','INHERITS') AND c.depth<2
            ) SELECT DISTINCT s.* FROM symbols s JOIN closure c ON s.symbol_id=c.node
                WHERE c.depth>0 AND s.kind IN ('class','type_alias') ORDER BY s.symbol_id""",
            (sid,),
        ):
            if related["symbol_id"] not in chosen:
                types[related["symbol_id"]] = related
        for related in store.rows(
            """SELECT DISTINCT s.* FROM symbols s JOIN edges e
                ON s.symbol_id=e.source_id WHERE e.target_id=? AND e.kind='CALLS' """,
            (sid,),
        ):
            if related["symbol_id"] not in chosen:
                callers[related["symbol_id"]] = related
        for related in store.rows(
            """SELECT DISTINCT s.* FROM symbols s JOIN edges e
                ON s.symbol_id=e.target_id WHERE e.source_id=? AND e.kind='CALLS' """,
            (sid,),
        ):
            if related["symbol_id"] not in chosen and related["symbol_id"] not in types:
                callees[related["symbol_id"]] = related
    callees = {sid: row for sid, row in callees.items() if sid not in types}
    sections["types_and_schemas"] = [
        item(row, True) for _, row in sorted(types.items())
    ]
    class_types = [(sid, row) for sid, row in sorted(types.items()) if row["kind"] == "class"]
    summaries = {}
    for sid, row in class_types:
        methods = store.rows(
            "SELECT * FROM symbols WHERE file_path=? AND kind='method' AND qualname LIKE ? ORDER BY start_line, symbol_id",
            (row["file_path"], row["qualname"] + ".%"),
        )
        methods = [m for m in methods if m["qualname"].rpartition(".")[0] == row["qualname"]]
        lines = [row["signature"]]
        try:
            parsed = ast.parse(textwrap.dedent(row["body"]))
            doc = ast.get_docstring(parsed.body[0]) if parsed.body else None
        except SyntaxError:
            doc = None
        if doc:
            lines.append(doc.splitlines()[0])
        for method in methods:
            lines.append(f"{method['file_path']}:{method['start_line']}-{method['end_line']} {method['signature']}")
            try:
                node = ast.parse(textwrap.dedent(method["body"])).body[0]
                method_doc = ast.get_docstring(node)
            except (SyntaxError, IndexError):
                method_doc = None
            if method_doc:
                lines.append(method_doc.splitlines()[0])
        db_arg = shlex.quote(str(database)) if database is not None else "GRAPH"
        item_record = item(row, False)
        full_size = tokens_for_length(len(row["body"]))
        item_record["text"] = f"{row['file_path']}:{row['start_line']}-{row['end_line']}\n" + "\n".join(lines) + f"\nExpansion {sid} ({full_size} tokens): codegraph-slicer slice --db {db_arg} {shlex.quote(sid)} --snapshot --max-tokens {max(max_tokens, full_size * 2)}"
        summaries[sid] = item_record
    totals = {name: len(items) for name, items in sections.items()}
    totals["upstream_callers"] = len(callers)
    totals["downstream_dependencies"] = len(callees)
    body_fallbacks = {}
    omitted_detail_limit = None

    def result():
        payload = {"tokens": 0, "sections": sections}
        dropped = {
            name: totals[name] - len(items)
            for name, items in sections.items()
        }
        if any(dropped.values()) or body_fallbacks:
            payload["omitted"] = dropped
            notes = []
            emitted = {item["symbol"] for group in sections.values() for item in group}
            omitted_details = []
            omitted_ids = []
            omitted_budget = 0
            for name, candidates in (("upstream_callers", callers), ("downstream_dependencies", callees)):
                for sid, candidate in candidates.items():
                    if sid not in emitted:
                        size = tokens_for_length(len(candidate["body"]))
                        omitted_details.append(f"{sid} ({size} tokens)")
                        omitted_ids.append(shlex.quote(sid))
                        omitted_budget += size * 2
            if omitted_ids:
                db_arg = shlex.quote(str(database)) if database is not None else "GRAPH"
                if omitted_detail_limit == 0:
                    notes.append("Omitted records; rerun with --max-tokens N")
                else:
                    if omitted_detail_limit is not None:
                        omitted_details = omitted_details[:omitted_detail_limit]
                        omitted_ids = omitted_ids[:omitted_detail_limit]
                    notes.append(f"Omitted: {', '.join(omitted_details)}; fetch with codegraph-slicer slice --db {db_arg} {' '.join(omitted_ids)} --snapshot --max-tokens {omitted_budget}")
            for sid, candidate in body_fallbacks.items():
                size = tokens_for_length(len(candidate["body"]))
                db_arg = shlex.quote(str(database)) if database is not None else "GRAPH"
                notes.append(f"Body summarized: {sid} ({size} tokens); fetch with codegraph-slicer slice --db {db_arg} {shlex.quote(sid)} --include-callee-bodies --snapshot --max-tokens {size * 2}")
            payload["retrieve_details"] = "; ".join(notes) or "rerun with --max-tokens N"
        return _with_output_tokens(payload, render_estimator)

    from .pagerank import rank

    scores = rank(store)
    for name, candidates in (
        ("upstream_callers", callers),
        ("downstream_dependencies", callees),
    ):
        for sid in sorted(candidates, key=lambda s: (-scores.get(s, 0), s)):
            full = name == "downstream_dependencies" and include_callee_bodies
            sections[name].append(item(candidates[sid], full))
    complete = result()
    if complete["tokens"] <= max_tokens:
        return complete
    for sid, summary in summaries.items():
        original = next(entry for entry in sections["types_and_schemas"] if entry["symbol"] == sid)
        sections["types_and_schemas"] = [
            summary if entry["symbol"] == sid else entry
            for entry in sections["types_and_schemas"]
        ]
        candidate = result()
        if candidate["tokens"] < complete["tokens"]:
            complete = candidate
        else:
            sections["types_and_schemas"] = [
                original if entry["symbol"] == sid else entry
                for entry in sections["types_and_schemas"]
            ]
    if complete["tokens"] <= max_tokens:
        return complete
    for name in ("upstream_callers", "downstream_dependencies"):
        sections[name].clear()
    required = result()
    if required["tokens"] > max_tokens and any(
        totals[name] > len(sections[name])
        for name in ("upstream_callers", "downstream_dependencies")
    ):
        omitted_detail_limit = 2
        while required["tokens"] > max_tokens and omitted_detail_limit >= 0:
            required = result()
            if required["tokens"] > max_tokens:
                omitted_detail_limit -= 1
    if required["tokens"] > max_tokens:
        raise RequiredAnchorOverflow(
            f"required definitions need {required['tokens']} tokens including omission metadata, budget is {max_tokens}"
        )
    for name, candidates in (
        ("upstream_callers", callers),
        ("downstream_dependencies", callees),
    ):
        for sid in sorted(candidates, key=lambda s: (-scores.get(s, 0), s)):
            full = name == "downstream_dependencies" and include_callee_bodies
            candidate = item(candidates[sid], full)
            sections[name].append(candidate)
            if result()["tokens"] > max_tokens:
                sections[name].pop()
                if full:
                    sections[name].append(item(candidates[sid], False))
                    body_fallbacks[sid] = candidates[sid]
                    if result()["tokens"] > max_tokens:
                        sections[name].pop()
                        body_fallbacks.pop(sid)
    final = result()
    if final["tokens"] > max_tokens:
        raise RequiredAnchorOverflow(
            f"required definitions need {final['tokens']} tokens including omission metadata, budget is {max_tokens}"
        )
    return final
