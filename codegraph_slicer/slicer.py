"""Bounded contexts and output budget estimates."""

import ast
import contextlib
from dataclasses import dataclass
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import textwrap
from .ast_extractor import (
    TS_JS_EXTENSIONS,
    CodeGraphError,
    is_supported_source_path,
    is_test_path,
)
from .treesitter_support import node_text, parser_for, scoped_descendants
from .ts_extractor import _TS_FUNCTION_NODES, _TS_TYPE_NODES

_BRACE_LANG_EXTS = (".java", *TS_JS_EXTENSIONS)
_BARREL_INDEX_NAMES = (
    "index.ts",
    "index.tsx",
    "index.js",
    "index.jsx",
    "index.mjs",
    "index.cjs",
)


class AnchorNotFoundError(CodeGraphError):
    pass


@dataclass(frozen=True)
class IndexedSymbol:
    symbol_id: str
    file_path: str
    module: str
    qualname: str
    kind: str
    signature: str
    body: str
    start_line: int
    end_line: int

    @classmethod
    def from_row(cls, row) -> "IndexedSymbol":
        if isinstance(row, cls):
            return row
        data = dict(row)
        return cls(
            symbol_id=str(data.get("symbol_id", "")),
            file_path=str(data.get("file_path", "")),
            module=str(data.get("module", "")),
            qualname=str(data.get("qualname", "")),
            kind=str(data.get("kind", "")),
            signature=str(data.get("signature", "")),
            body=str(data.get("body", "")),
            start_line=int(data.get("start_line", 1)),
            end_line=int(data.get("end_line", 1)),
        )


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


_LEADING_PATH_PREFIXES = ("src/", "lib/", "app/")


def _strip_leading_path_prefix(path: str) -> str:
    clean = path.lstrip("/")
    for pfx in _LEADING_PATH_PREFIXES:
        if clean.startswith(pfx) and len(clean) > len(pfx):
            return clean[len(pfx) :]
    return clean


def _find_unique_suffix_file(store, path: str) -> str | None:
    clean = path.lstrip("/")
    cand_files = store.rows(
        "SELECT path FROM files WHERE path GLOB ? ORDER BY length(path), path",
        (f"*/{clean}",),
    )
    stripped = _strip_leading_path_prefix(clean)
    if not cand_files and stripped != clean:
        cand_files = store.rows(
            "SELECT path FROM files WHERE path=? OR path GLOB ? ORDER BY length(path), path",
            (stripped, f"*/{stripped}"),
        )
    return cand_files[0]["path"] if len(cand_files) == 1 else None


def _resolve_suffix_path_anchor(store, path: str, target: str):
    if not is_supported_source_path(path):
        return None
    fpath = _find_unique_suffix_file(store, path)
    if fpath is None:
        return None
    rows = store.rows(
        "SELECT * FROM symbols WHERE file_path=? AND qualname=?",
        (fpath, target),
    )
    if rows:
        return rows[0]
    if target and "." not in target:
        s_rows = store.rows(
            "SELECT * FROM symbols WHERE file_path=? AND qualname GLOB ? ORDER BY start_line, symbol_id",
            (fpath, f"*.{target}"),
        )
        if len(s_rows) == 1:
            return s_rows[0]
    return None


def _resolve_unique_qualname_anchor(store, anchor: str):
    q_rows = store.rows(
        "SELECT * FROM symbols WHERE qualname=? AND kind!='module' ORDER BY start_line, symbol_id",
        (anchor,),
    )
    non_test = [r for r in q_rows if not _is_test_symbol(r)]
    if len(non_test) == 1:
        return non_test[0]
    return q_rows[0] if len(q_rows) == 1 else None


def _candidate_rows_for_nearest_anchors(store, path: str, target: str) -> list[dict]:
    cnt_row = store.rows("SELECT COUNT(*) AS n FROM symbols")
    total_syms = int(cnt_row[0]["n"]) if cnt_row else 0
    if total_syms <= 1500:
        return store.rows("SELECT file_path,module,qualname FROM symbols")
    scoped: list[dict] = []
    if path:
        scoped.extend(
            store.rows(
                "SELECT file_path,module,qualname FROM symbols WHERE file_path=? OR module=? LIMIT 400",
                (path, path),
            )
        )
    leaf = (target or path).rsplit(".", 1)[-1]
    if leaf and not leaf.isdigit():
        scoped.extend(
            store.rows(
                "SELECT file_path,module,qualname FROM symbols WHERE qualname=? OR qualname GLOB ? LIMIT 100",
                (leaf, f"*.{leaf}"),
            )
        )
    tok = leaf[:12]
    if len(tok) >= 3:
        scoped.extend(
            store.rows(
                "SELECT file_path,module,qualname FROM symbols WHERE qualname GLOB ? OR qualname GLOB ? LIMIT 400",
                (f"{tok[:4]}*", f"*.{tok[:4]}*"),
            )
        )
    return scoped if scoped else store.rows("SELECT file_path,module,qualname FROM symbols LIMIT 1200")


def _anchor(store, anchor):
    exact = store.rows("SELECT * FROM symbols WHERE symbol_id=?", (anchor,))
    if exact:
        return exact[0]
    path, separator, target = anchor.rpartition(":")
    if separator and is_supported_source_path(path):
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
        if line == 0 and target:
            suffix_rows = store.rows(
                "SELECT * FROM symbols WHERE file_path=? AND qualname GLOB ? ORDER BY end_line-start_line DESC, start_line ASC",
                (path, f"*.{target}"),
            )
            if len(suffix_rows) == 1:
                return suffix_rows[0]
        if line == 0:
            suf_match = _resolve_suffix_path_anchor(store, path, target)
            if suf_match is not None:
                return suf_match
    if not separator and is_supported_source_path(anchor):
        mod_rows = store.rows(
            "SELECT * FROM symbols WHERE file_path=? AND qualname=''",
            (anchor,),
        )
        if mod_rows:
            return mod_rows[0]
        suf_mod = _resolve_suffix_path_anchor(store, anchor, "")
        if suf_mod is not None:
            return suf_mod
    if separator and "." in path and target:
        java_cls = path.rsplit(".", 1)[-1]
        java_cand = f"{path}:{java_cls}.{target}"
        java_rows = store.rows(
            "SELECT * FROM symbols WHERE symbol_id=? AND file_path LIKE '%.java'",
            (java_cand,),
        )
        if java_rows:
            return java_rows[0]
    elif not separator and "." in anchor:
        java_cls = anchor.rsplit(".", 1)[-1]
        java_cand = f"{anchor}:{java_cls}"
        java_rows = store.rows(
            "SELECT * FROM symbols WHERE symbol_id=? AND file_path LIKE '%.java'",
            (java_cand,),
        )
        if java_rows:
            return java_rows[0]
        cls_fqcn, _, member = anchor.rpartition(".")
        if "." in cls_fqcn and member:
            outer_cls = cls_fqcn.rsplit(".", 1)[-1]
            java_method_cand = f"{cls_fqcn}:{outer_cls}.{member}"
            java_m_rows = store.rows(
                "SELECT * FROM symbols WHERE symbol_id=? AND file_path LIKE '%.java'",
                (java_method_cand,),
            )
            if java_m_rows:
                return java_m_rows[0]
    if separator and target:
        mod_suf_rows = store.rows(
            "SELECT * FROM symbols WHERE module=? AND qualname GLOB ? ORDER BY end_line-start_line DESC, start_line ASC",
            (path, f"*.{target}"),
        )
        if len(mod_suf_rows) == 1:
            return mod_suf_rows[0]
    if not separator:
        uniq_match = _resolve_unique_qualname_anchor(store, anchor)
        if uniq_match is not None:
            return uniq_match
    error = AnchorNotFoundError(anchor)
    dedup_rows = {
        (row["file_path"], row["qualname"]): row
        for row in _candidate_rows_for_nearest_anchors(store, path, target)
    }
    rows = sorted(
        dedup_rows.values(),
        key=lambda row: (row["file_path"], row["qualname"]),
    )
    aliases = {}
    for row in rows:
        copyable = f"{row['file_path']}:{row['qualname']}"
        aliases[copyable] = copyable
        aliases[f"{row['module']}:{row['qualname']}"] = copyable
    matches = difflib.get_close_matches(anchor, sorted(aliases), n=6, cutoff=0.25)
    nearest = list(dict.fromkeys(aliases[item] for item in matches))[:3]

    raw_leaf = target if separator else ("" if is_supported_source_path(anchor) else anchor)
    leaf = raw_leaf.rsplit(".", 1)[-1] if raw_leaf and not raw_leaf.isdigit() else ""
    exact_leaf_match = ""
    missing_symbol_name = ""
    if leaf:
        exact_rows = [
            row
            for row in rows
            if row["qualname"] == leaf or row["qualname"].endswith("." + leaf)
        ]
        if exact_rows:
            norm_path = path.replace("\\", "/").lstrip("./") if path else ""
            exact_rows.sort(
                key=lambda row: (
                    0
                    if norm_path
                    and (
                        row["file_path"] == norm_path
                        or row["module"] == path
                        or row["file_path"].endswith("/" + norm_path)
                    )
                    else 1,
                    1 if _is_test_symbol(row) else 0,
                    len(row["qualname"]),
                    row["file_path"],
                    row["qualname"],
                )
            )
            exact_copyables = [
                f"{row['file_path']}:{row['qualname']}" for row in exact_rows
            ]
            exact_leaf_match = exact_copyables[0]
            nearest = list(dict.fromkeys([*exact_copyables, *nearest]))[:3]
        else:
            candidate_leaves = sorted(
                {
                    row["qualname"].rsplit(".", 1)[-1]
                    for row in rows
                    if row["qualname"]
                }
            )
            if not difflib.get_close_matches(leaf, candidate_leaves, n=1, cutoff=0.85):
                missing_symbol_name = leaf

    error.nearest_anchors = nearest
    error.exact_leaf_match = exact_leaf_match
    error.missing_symbol_name = missing_symbol_name
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
    with store.read_snapshot():
        return _query_call_impact_in_snapshot(
            store,
            anchor,
            max_depth=max_depth,
            max_nodes=max_nodes,
            unknown_boundary_limit=unknown_boundary_limit,
            max_tokens=max_tokens,
            database=database,
            render_estimator=render_estimator,
            output_format=output_format,
        )


def _query_call_impact_in_snapshot(
    store, anchor, *, max_depth=3, max_nodes=200, unknown_boundary_limit=0,
    max_tokens=8000, database=None, render_estimator=None, output_format="json"
):
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
    with store.read_snapshot():
        return _build_slice_outline_in_snapshot(
            store,
            anchors,
            max_tokens=max_tokens,
            include_imports=include_imports,
            render_estimator=render_estimator,
        )


def _build_slice_outline_in_snapshot(
    store, anchors, max_tokens=8000, *, include_imports=False, render_estimator=None
):
    allowed_kinds = ["CALLS", "USES_TYPE", "INHERITS", "DEFINES"]
    if include_imports:
        allowed_kinds.append("IMPORTS")
    selected = {
        row["symbol_id"]
        for row in (_anchor(store, anchor) for anchor in _expand_comma_anchors(anchors))
    }
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


def _slice_item(row, full):
    sym = IndexedSymbol.from_row(row)
    text = sym.body if full else sym.signature
    return {
        "symbol": sym.symbol_id,
        "text": f"{sym.file_path}:{sym.start_line}-{sym.end_line}\n{text}",
    }


def _preceding_doc_comment_start(file_lines: list[str], decl_line: int) -> int:
    idx = min(decl_line - 2, len(file_lines) - 1)
    while idx >= 0 and not file_lines[idx].strip():
        idx -= 1
    start_ln = decl_line
    count = 0
    while idx >= 0 and count < 22:
        s = file_lines[idx].strip()
        if s.startswith(("//", "/*", "*", "*/", "@")):
            start_ln = idx + 1
            count += 1
            if s.startswith("/*"):
                break
            idx -= 1
        else:
            break
    return start_ln


def _append_line_span(
    file_lines: list[str], start_ln: int, end_ln: int, header_lines: list[str], header_end_line: int
) -> tuple[bool, int]:
    added = False
    for ln in range(start_ln, end_ln + 1):
        if 1 <= ln <= len(file_lines):
            line_txt = file_lines[ln - 1]
            if line_txt.strip():
                header_lines.append(line_txt)
                header_end_line = max(header_end_line, ln)
                added = True
    return added, header_end_line


def _append_enclosing_brace_class_header(
    store,
    sym: IndexedSymbol,
    chosen,
    file_lines: list[str],
    header_lines: list[str],
    header_end_line: int,
    emitted_headers: set[str] | None,
) -> int:
    class_qual = sym.qualname.rpartition(".")[0]
    class_sid = f"{sym.module}:{class_qual}"
    class_key = f"class:{class_sid}"
    if class_sid in chosen or (emitted_headers is not None and class_key in emitted_headers):
        return header_end_line
    class_rows = store.rows(
        "SELECT start_line, end_line FROM symbols WHERE symbol_id=?",
        (class_sid,),
    )
    if not class_rows:
        return header_end_line
    raw_c_start = class_rows[0]["start_line"]
    c_start = max(header_end_line + 1, _preceding_doc_comment_start(file_lines, raw_c_start))
    class_short = class_qual.rsplit(".", 1)[-1]
    first_m_rows = store.rows(
        """SELECT MIN(start_line) AS first_m FROM symbols
           WHERE file_path=? AND kind='method' AND start_line > ?
             AND qualname NOT GLOB ? AND qualname NOT GLOB '*.constructor'""",
        (sym.file_path, raw_c_start, f"*.{class_short}"),
    )
    first_m = (
        first_m_rows[0]["first_m"]
        if first_m_rows and first_m_rows[0]["first_m"] is not None
        else sym.start_line
    )
    c_end = min(first_m - 1, sym.start_line - 1, raw_c_start + 20)
    added_class, header_end_line = _append_line_span(
        file_lines, c_start, c_end, header_lines, header_end_line
    )
    if emitted_headers is not None and added_class:
        emitted_headers.add(class_key)
    return header_end_line


def _append_brace_class_or_method_doc_preamble(
    store,
    sym: IndexedSymbol,
    chosen,
    file_lines: list[str],
    header_lines: list[str],
    header_end_line: int,
    emitted_headers: set[str] | None,
) -> int:
    if not sym.file_path.endswith(_BRACE_LANG_EXTS):
        return header_end_line
    if sym.kind == "class":
        doc_start = _preceding_doc_comment_start(file_lines, sym.start_line)
        if doc_start < sym.start_line:
            _, header_end_line = _append_line_span(
                file_lines, max(header_end_line + 1, doc_start), sym.start_line - 1, header_lines, header_end_line
            )
        return header_end_line
    if sym.kind != "method" or "." not in sym.qualname:
        return header_end_line
    header_end_line = _append_enclosing_brace_class_header(
        store, sym, chosen, file_lines, header_lines, header_end_line, emitted_headers
    )
    m_doc_start = _preceding_doc_comment_start(file_lines, sym.start_line)
    if m_doc_start < sym.start_line and m_doc_start > header_end_line:
        _, header_end_line = _append_line_span(
            file_lines, m_doc_start, sym.start_line - 1, header_lines, header_end_line
        )
    return header_end_line


def _target_item_with_preamble(
    store,
    row,
    chosen,
    max_tokens: int,
    emitted_headers: set[str] | None = None,
):
    sym = IndexedSymbol.from_row(row)
    base = _slice_item(sym, True)
    if (
        max_tokens < 2000
        or sym.kind not in {"method", "function", "async_function", "class", "type_alias"}
    ):
        return base
    module_sid = f"{sym.module}:"
    if module_sid in chosen:
        return base
    mod_rows = store.rows("SELECT body FROM symbols WHERE symbol_id=?", (module_sid,))
    if not mod_rows:
        return base
    file_lines = mod_rows[0]["body"].splitlines()
    limit_idx = min(len(file_lines), max(0, sym.start_line - 1))
    header_lines = []
    header_end_line = 0
    file_key = f"file:{sym.file_path}"
    emit_file_imports = emitted_headers is None or file_key not in emitted_headers
    if emit_file_imports:
        if sym.file_path.endswith(".py"):
            try:
                tree = ast.parse(mod_rows[0]["body"])
            except SyntaxError:
                tree = None
            if tree is not None:
                for idx_stmt, stmt in enumerate(tree.body):
                    if getattr(stmt, "lineno", 0) >= sym.start_line:
                        break
                    if (
                        idx_stmt == 0
                        and isinstance(stmt, ast.Expr)
                        and isinstance(stmt.value, ast.Constant)
                        and isinstance(stmt.value.value, str)
                    ):
                        continue
                    if isinstance(stmt, (ast.Import, ast.ImportFrom)):
                        s_ln = stmt.lineno
                        e_ln = getattr(stmt, "end_lineno", s_ln) or s_ln
                        header_lines.extend(file_lines[s_ln - 1 : min(limit_idx, e_ln)])
                        header_end_line = min(limit_idx, e_ln)
                        continue
                    break
        else:
            in_multiline_import = False
            for idx in range(limit_idx):
                raw = file_lines[idx]
                stripped = raw.strip()
                if in_multiline_import:
                    header_lines.append(raw)
                    header_end_line = idx + 1
                    if not stripped.endswith("\\") and (
                        ";" in stripped or ")" in stripped or stripped.endswith(('"', "'")) or "(" not in "".join(header_lines)
                    ):
                        in_multiline_import = False
                    continue
                if stripped.startswith(("import ", "from ", "package ")) or (
                    stripped.startswith(("const ", "let ", "var ")) and "require(" in stripped
                ):
                    header_lines.append(raw)
                    header_end_line = idx + 1
                    if (
                        stripped.endswith("\\")
                        or ("{" in stripped and "}" not in stripped)
                        or ("(" in stripped and ")" not in stripped)
                    ):
                        in_multiline_import = True
                    continue
                if stripped and not stripped.startswith(("#", "//", "/*", "*", "*/")):
                    break
        if emitted_headers is not None and header_lines:
            emitted_headers.add(file_key)
    header_end_line = _append_brace_class_or_method_doc_preamble(
        store, sym, chosen, file_lines, header_lines, header_end_line, emitted_headers
    )
    comment_pfx = _comment_prefix(sym.file_path)
    if emit_file_imports and sym.file_path.endswith(TS_JS_EXTENSIONS) and "." in sym.module:
        parent_mod_sid = f"{sym.module.rpartition('.')[0]}:"
        barrel_rows = store.rows(
            "SELECT file_path, body FROM symbols WHERE symbol_id=?",
            (parent_mod_sid,),
        )
        if barrel_rows and barrel_rows[0]["file_path"].endswith(_BARREL_INDEX_NAMES):
            b_lines = [ln.strip() for ln in barrel_rows[0]["body"].splitlines() if ln.strip()]
            if 1 <= len(b_lines) <= 8:
                header_lines.append(
                    f"{comment_pfx} Barrel ({barrel_rows[0]['file_path']}): {' '.join(b_lines)}"
                )
    if not header_lines:
        return base
    label = f"lines 1-{header_end_line}" if header_end_line > 0 else "module metadata"
    preamble = (
        f"{comment_pfx} File & class header ({label}):\n"
        + "\n".join(header_lines)
        + "\n"
    )
    return {
        "symbol": sym.symbol_id,
        "text": f"{sym.file_path}:{sym.start_line}-{sym.end_line}\n{preamble}{sym.body}",
    }


def _is_test_symbol(row):
    path = row["file_path"].replace("\\", "/")
    if is_test_path(path):
        return True
    parts = path.split("/")
    name = parts[-1]
    return (
        any(p in {"tests", "test", "t"} for p in parts[:-1])
        or name.startswith("test_")
        or name.endswith("_test.py")
        or name in {"conftest.py", "tests.py"}
    )


def _comment_prefix(path: str) -> str:
    return "//" if path.endswith(_BRACE_LANG_EXTS) else "#"


def _first_code_line_offset(body: str) -> int:
    try:
        mod = ast.parse(textwrap.dedent(body))
        if mod.body and isinstance(mod.body[0], (ast.FunctionDef, ast.AsyncFunctionDef)):
            stmts = mod.body[0].body
            if (
                stmts
                and isinstance(stmts[0], ast.Expr)
                and isinstance(getattr(stmts[0], "value", None), ast.Constant)
                and isinstance(stmts[0].value.value, str)
            ):
                stmts = stmts[1:]
            if stmts:
                return max(1, stmts[0].lineno - 1)
    except SyntaxError:
        lines = body.splitlines()
        for idx, line in enumerate(lines[:15]):
            if "{" in line:
                return min(idx + 1, max(1, len(lines) - 1))
    return 1


_BOUNDARY_TOKENS = (".split(", ".rsplit(", ".partition(", ".rpartition(", ".strip(")
_CALLER_SLICE_IGNORE = frozenset({
    "if", "else", "elif", "for", "while", "switch", "case", "try", "catch", "finally",
    "return", "new", "null", "None", "true", "false", "True", "False", "self", "this",
    "let", "const", "var", "final", "int", "long", "double", "float", "boolean", "bool",
    "String", "List", "Map", "Set", "Path", "Optional", "Object", "void", "async", "await",
})
_CALL_SITE_IDENT_RE = re.compile(r"\b([A-Za-z_]\w*)\b")
_CALLEE_NAME_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")
_MUTATION_CALL_PATS = ("add", "append", "extend", "put", "push", "set", "update", "remove", "clear", "pop", "insert")
_CTRL_HEADER_PREFIXES = ("for ", "for(", "while ", "while(", "if ", "if(", "} else", "else:", "else ", "elif ", "switch ", "switch(")


def _extract_non_callee_vars(line: str) -> set[str]:
    callees = set(_CALLEE_NAME_RE.findall(line))
    return {
        v for v in _CALL_SITE_IDENT_RE.findall(line)
        if v not in _CALLER_SLICE_IGNORE and v not in callees and len(v) > 1
    }


def _extract_call_window_vars(body_lines: list[str], window_start: int, window_end: int) -> set[str]:
    found: set[str] = set()
    for k in range(max(0, window_start), min(len(body_lines), window_end + 1)):
        line = body_lines[k].strip()
        if line and not line.startswith(("#", "//", "/*", "*")):
            found.update(_extract_non_callee_vars(line))
    return found


def _has_mutation_call(line: str) -> bool:
    return any(f".{m}(" in line for m in _MUTATION_CALL_PATS)


def _line_defines_var(line: str, tracked_vars: set[str]) -> bool:
    return any(re.search(rf"\b{re.escape(v)}\b[^=;()]*=(?!=)", line) for v in tracked_vars)


def _line_mutates_var(line: str, tracked_vars: set[str]) -> bool:
    return any(
        re.search(rf"\b{re.escape(v)}\.{m}\s*\(", line)
        for v in tracked_vars
        for m in _MUTATION_CALL_PATS
    )


def _is_ctrl_header(stripped: str) -> bool:
    return stripped.startswith(_CTRL_HEADER_PREFIXES)


def _is_skippable_caller_line(s: str) -> bool:
    return not s or s == "}" or s.startswith(("#", '"""', "'''", "//", "/*", "*", "*/"))


_EMPTY_COLLECTION_INIT_RE = re.compile(
    r"=\s*(?:new\s+[A-Za-z_]\w*(?:<[^>]*>)?\s*\(\s*\)|\[\s*\]|\{\s*\})\s*;?$"
)


def _should_expand_caller_hop(is_def: bool, hops: int, s: str) -> bool:
    if not is_def or hops >= 2:
        return False
    return not bool(_EMPTY_COLLECTION_INIT_RE.search(s))


def _is_enclosing_ctrl_line(min_indent: int | None, indent: int, s: str) -> bool:
    return min_indent is not None and indent <= min_indent and _is_ctrl_header(s)


def _walk_backward_caller_slice(
    body_lines: list[str],
    code_start: int,
    window_start: int,
    tracked: set[str],
) -> list[tuple[int, str]]:
    selected: list[tuple[int, str]] = []
    min_indent: int | None = None
    hops = 0
    for k in range(window_start - 1, code_start - 1, -1):
        raw = body_lines[k]
        s = raw.strip()
        if _is_skippable_caller_line(s):
            continue
        indent = len(raw) - len(raw.lstrip())
        is_def = _line_defines_var(s, tracked)
        if is_def or _line_mutates_var(s, tracked):
            selected.append((k, s))
            min_indent = indent if min_indent is None else min(min_indent, indent)
            if _should_expand_caller_hop(is_def, hops, s):
                tracked.update(_extract_non_callee_vars(s))
                hops += 1
        elif _is_enclosing_ctrl_line(min_indent, indent, s):
            selected.append((k, s))
            min_indent = indent
    selected.reverse()
    return selected[-8:]


def _caller_boundary_lines(
    body_lines: list[str],
    code_start: int,
    window_start: int,
    call_idx: int | None = None,
) -> list[tuple[int, str]]:
    if window_start <= code_start:
        return []
    win_end = min(len(body_lines) - 1, (call_idx if call_idx is not None else window_start) + 1)
    tracked = _extract_call_window_vars(body_lines, window_start, win_end)
    if tracked:
        sliced = _walk_backward_caller_slice(body_lines, code_start, window_start, tracked)
        if sliced:
            return sliced
    return [
        (k, body_lines[k].strip())
        for k in range(code_start, window_start)
        if any(tok in body_lines[k] for tok in _BOUNDARY_TOKENS)
    ][:3]


def _caller_item(row, call_lines):
    sym = IndexedSymbol.from_row(row)
    base = _slice_item(sym, False)
    if sym.end_line - sym.start_line < 3 or not call_lines:
        return base
    body_lines = sym.body.splitlines()
    code_start = _first_code_line_offset(sym.body)
    comment_pfx = _comment_prefix(sym.file_path)
    snippets = []
    seen_idxs = set()
    for ln in sorted(call_lines)[:2]:
        idx = ln - sym.start_line
        if 0 < idx < len(body_lines):
            window_start = max(code_start, idx - 5)
            window_end = min(len(body_lines) - 1, idx + 1)
            for k, cl in _caller_boundary_lines(body_lines, code_start, window_start, idx):
                if k not in seen_idxs:
                    seen_idxs.add(k)
                    snippets.append(f"    {comment_pfx} L{sym.start_line + k}: {cl}")
            cand = []
            for k in range(window_start, window_end + 1):
                if k in seen_idxs:
                    continue
                cl = body_lines[k].strip()
                if (
                    cl
                    and cl != "}"
                    and not cl.startswith(("#", '"""', "'''", "//", "/*", "*", "*/"))
                ):
                    cand.append((k, cl))
            for k, cl in cand[-5:]:
                seen_idxs.add(k)
                snippets.append(f"    {comment_pfx} L{sym.start_line + k}: {cl}")
    if snippets:
        base["text"] = base["text"] + "\n" + "\n".join(snippets)
    return base


def _recover_chained_callers(store, row, chosen, all_target_in_test, callers, caller_lines):
    if row["kind"] != "method":
        return
    mname = row["qualname"].rsplit(".", 1)[-1]
    if len(mname) < 4 or mname.startswith("__") or not mname.isidentifier():
        return
    mcount = store.rows(
        "SELECT count(*) AS c FROM symbols WHERE kind='method' AND qualname GLOB ?",
        (f"*.{mname}",),
    )[0]["c"]
    if mcount > 3:
        return
    target_class = row["qualname"].rpartition(".")[0]
    target_class_sid = f"{row['module']}:{target_class}" if target_class else ""
    for related in store.rows(
        """SELECT s.*, d.line AS call_line, d.target AS diag_target FROM diagnostics d
           JOIN symbols s ON s.symbol_id=d.source_id
           WHERE d.kind='CALLS' AND d.target GLOB ?
           ORDER BY d.line""",
        (f"*.{mname}",),
    ):
        csid = related["symbol_id"]
        if csid in chosen or related["kind"] not in {"function", "async_function", "method"}:
            continue
        if not all_target_in_test and _is_test_symbol(related):
            continue
        diag_target = related["diag_target"]
        if not (
            diag_target.startswith(("?self.", "?this."))
            or "." in diag_target.lstrip("?") and diag_target.lstrip("?").count(".") >= 2
        ):
            continue
        has_type_link = bool(
            target_class_sid
            and store.rows(
                "SELECT 1 FROM edges WHERE source_id IN (?, ?) AND target_id=? AND kind IN ('USES_TYPE', 'CALLS', 'INHERITS')",
                (csid, f"{related['module']}:", target_class_sid),
            )
        )
        if not has_type_link and related["module"] == row["module"]:
            enclosing_qual = related["qualname"].rpartition(".")[0]
            if target_class and (
                enclosing_qual == target_class or target_class in related["body"]
            ):
                has_type_link = True
        if not has_type_link:
            continue
        caller_body = related["body"].splitlines()
        idx = related["call_line"] - related["start_line"]
        if 0 <= idx < len(caller_body):
            span = "".join(
                line.strip()
                for line in caller_body[max(0, idx - 1) : min(len(caller_body), idx + 2)]
            )
            if f".{mname}(" in span:
                callers[csid] = related
                caller_lines.setdefault(csid, set()).add(related["call_line"])


def _format_comment_line(s: str) -> str | None:
    if s.startswith("//"):
        return f"    {s}"
    if s.startswith(("/*", "*")) and not s.startswith("*/"):
        cleaned = s.lstrip("/* ").rstrip("*/ ").strip()
        if cleaned:
            return f"    // {cleaned}"
    return None


def _extract_preceding_comments(preceding_lines: list[str]) -> list[str]:
    idx = len(preceding_lines) - 1
    while idx >= 0 and not preceding_lines[idx].strip():
        idx -= 1
    block: list[str] = []
    while idx >= 0 and preceding_lines[idx].strip().startswith(("//", "/*", "*", "*/")):
        s = preceding_lines[idx].strip()
        block.append(s)
        idx -= 1
        if s.startswith("/*"):
            break
    return [fmt for s in reversed(block) if (fmt := _format_comment_line(s))]


def _parse_python_first_stmt_docstring(body: str) -> str | None:
    if not body:
        return None
    with contextlib.suppress(SyntaxError, IndexError):
        parsed = ast.parse(textwrap.dedent(body))
        if parsed.body:
            return ast.get_docstring(parsed.body[0])
    return None


def _extract_python_docstring_summary(body: str) -> str | None:
    doc = _parse_python_first_stmt_docstring(body)
    if not doc:
        return None
    d_lines = [ln.strip() for ln in doc.splitlines() if ln.strip()]
    is_contract = "CONTRACT" in doc or any(ln.startswith("-") for ln in d_lines[1:])
    if len(d_lines) > 1 and is_contract:
        return "\n".join(
            [d_lines[0], *(f"    # {ln}" for ln in d_lines[1:8])]
        )
    return d_lines[0]


def _first_docstring_line(body: str, preceding_lines: list[str] | None = None):
    py_doc = _extract_python_docstring_summary(body)
    if py_doc is not None:
        return py_doc
    comment_lines = _extract_preceding_comments(preceding_lines) if preceding_lines else []
    for line in body.splitlines()[1:10]:
        s = line.strip()
        fmt = _format_comment_line(s)
        if fmt:
            comment_lines.append(fmt)
        elif comment_lines or (s and not s.startswith("*/")):
            break
    return "\n".join(comment_lines) if comment_lines else None


_SELF_ATTR_RE = re.compile(r"\b(?:self|this)\.([A-Za-z_]\w*)\b")


def _extract_self_attrs(body: str) -> set[str]:
    return set(_SELF_ATTR_RE.findall(body))


def _extract_py_init_fields(methods: list[dict]) -> list[str]:
    init_rows = [m for m in methods if m["qualname"].endswith(".__init__")]
    if not init_rows:
        return []
    return [
        f"        {raw.strip()}"
        for raw in init_rows[0]["body"].splitlines()
        if raw.strip().startswith("self.") and "=" in raw and not raw.strip().startswith("self._")
    ][:6]


def _is_public_class_field_stmt(stmt: ast.stmt) -> bool:
    if isinstance(stmt, ast.AnnAssign):
        return isinstance(stmt.target, ast.Name) and not stmt.target.id.startswith("_")
    if isinstance(stmt, ast.Assign) and stmt.targets:
        return any(
            isinstance(t, ast.Name) and not t.id.startswith("_") for t in stmt.targets
        )
    return False


def _extract_py_class_fields(row: dict) -> list[str]:
    try:
        mod = ast.parse(textwrap.dedent(row["body"]))
    except SyntaxError:
        return []
    if not mod.body or not isinstance(mod.body[0], ast.ClassDef):
        return []
    fields = [
        f"    {ast.unparse(stmt)}"
        for stmt in mod.body[0].body
        if _is_public_class_field_stmt(stmt)
    ]
    if len(fields) > 12:
        return [*fields[:12], f"    # ... (+{len(fields) - 12} more fields)"]
    return fields


def _is_priority_compact_method(
    m: dict,
    ctor_names: set[str],
    referenced_names: set[str],
    target_self_attrs: set[str],
) -> bool:
    m_short = m["qualname"].rpartition(".")[2]
    return bool(
        m_short in (ctor_names | referenced_names)
        or "@property" in m["body"]
        or (_extract_self_attrs(m["body"]) & target_self_attrs)
    )


def _is_internal_method(m: dict, ctor_names: set[str]) -> bool:
    m_short = m["qualname"].rpartition(".")[2]
    if m_short in ctor_names:
        return False
    return m["signature"].lstrip().startswith("private ") or m_short.startswith("_")


def _select_compact_methods(
    row: dict,
    methods: list[dict],
    ctor_names: set[str],
    referenced_names: set[str],
    target_self_attrs: set[str],
) -> list[dict]:
    public_methods = [m for m in methods if not _is_internal_method(m, ctor_names)]
    candidate_methods = public_methods or methods
    kept = [
        m for m in candidate_methods
        if _is_priority_compact_method(m, ctor_names, referenced_names, target_self_attrs)
    ]
    is_java_all = (
        row["file_path"].endswith(".java")
        and not referenced_names
        and len(candidate_methods) <= 18
    )
    fill_limit = 18 if is_java_all else 4
    extra = [m for m in candidate_methods if m not in kept][: max(0, fill_limit - len(kept))]
    return (kept + extra)[: (18 if is_java_all else 10)]


def _extract_brace_class_fields(
    row: dict, methods: list[dict], nested_classes: list[dict]
) -> list[str]:
    body_lines = row["body"].splitlines()
    bound_lines = [m["start_line"] for m in (*methods, *nested_classes)]
    first_offset = (
        min(bound_lines) - row["start_line"] if bound_lines else len(body_lines)
    )
    fields: list[str] = []
    for fline in body_lines[1 : max(1, first_offset)]:
        sf = fline.strip()
        if (
            sf.endswith(";")
            and "(" not in sf
            and not sf.startswith(("this.", "return ", "//", "/*", "*"))
        ):
            fields.append(f"    {sf}")
    return fields


def _build_type_summaries(
    store,
    class_types,
    referenced_names,
    database,
    max_tokens,
    target_self_attrs: set[str] | None = None,
    enclosing_py_classes: set[str] | None = None,
):
    summaries = {}
    compact_summaries = {}
    auto_summarize_ids = set()
    self_attrs = target_self_attrs or set()
    py_enclosing = enclosing_py_classes or set()
    db_arg = shlex.quote(str(database)) if database is not None else "GRAPH"
    for sid, row in class_types:
        methods = store.rows(
            """SELECT s.* FROM symbols s JOIN edges e ON s.symbol_id=e.target_id
               WHERE e.source_id=? AND e.kind='DEFINES' AND s.kind='method'
               ORDER BY s.start_line, s.symbol_id""",
            (sid,),
        )
        if not methods:
            methods = store.rows(
                "SELECT * FROM symbols WHERE file_path=? AND kind='method' AND qualname GLOB ? ORDER BY start_line, symbol_id",
                (row["file_path"], f"{row['qualname']}.*"),
            )
            methods = [m for m in methods if m["qualname"].rpartition(".")[0] == row["qualname"]]
        mod_rows = store.rows(
            "SELECT body FROM symbols WHERE symbol_id=?", (f"{row['module']}:",)
        )
        preceding = (
            mod_rows[0]["body"].splitlines()[: max(0, row["start_line"] - 1)]
            if mod_rows
            else None
        )
        doc_line = _first_docstring_line(row["body"], preceding)
        py_class_fields = (
            _extract_py_class_fields(row) if row["file_path"].endswith(".py") else []
        )
        lines = [row["signature"]]
        if doc_line:
            lines.append(doc_line)
        lines.extend(py_class_fields)
        for method in methods:
            lines.append(f"{method['file_path']}:{method['start_line']}-{method['end_line']} {method['signature']}")
            method_doc = _first_docstring_line(method["body"])
            if method_doc:
                lines.append(method_doc)
        item_record = _slice_item(row, False)
        full_size = tokens_for_length(len(row["body"]))
        item_record["text"] = (
            f"{row['file_path']}:{row['start_line']}-{row['end_line']}\n"
            + "\n".join(lines)
            + f"\nExpansion {sid} ({full_size} tokens): codegraph-slicer slice --db {db_arg} {shlex.quote(sid)} --snapshot --max-tokens {max(max_tokens, full_size * 2)}"
        )
        summaries[sid] = item_record

        compact_lines = [row["signature"]]
        if doc_line:
            compact_lines.append(doc_line)
        if len(py_class_fields) <= 4:
            compact_lines.extend(py_class_fields)
        nested_classes = (
            store.rows(
                """SELECT * FROM symbols WHERE file_path=? AND kind='class'
                   AND qualname GLOB ? ORDER BY start_line, symbol_id""",
                (row["file_path"], f"{row['qualname']}.*"),
            )
            if row["file_path"].endswith(".java")
            else []
        )
        if row["file_path"].endswith(_BRACE_LANG_EXTS):
            compact_lines.extend(_extract_brace_class_fields(row, methods, nested_classes))
        for nc in nested_classes:
            if nc["qualname"].rpartition(".")[0] == row["qualname"] and (
                nc["end_line"] - nc["start_line"] <= 30
            ):
                for nline in nc["body"].splitlines():
                    sline = nline.strip()
                    if sline.startswith(("public final ", "public static class ", "public " + nc["qualname"].rsplit(".", 1)[-1] + "(")):
                        compact_lines.append(f"    {sline}")
        class_short_name = row["qualname"].rsplit(".", 1)[-1]
        ctor_names = {"__init__", "__new__", "constructor", class_short_name}
        kept_methods = _select_compact_methods(row, methods, ctor_names, referenced_names, self_attrs)
        py_init_fields = _extract_py_init_fields(methods) if row["file_path"].endswith(".py") else []
        for method in kept_methods:
            compact_lines.append(f"    {method['signature']}")
            if method["qualname"].rsplit(".", 1)[-1] == "__init__" and py_init_fields:
                compact_lines.extend(py_init_fields)
        omitted_m = len(methods) - len(kept_methods)
        if omitted_m > 0:
            comment_pfx = _comment_prefix(row["file_path"])
            compact_lines.append(f"    {comment_pfx} ... (+{omitted_m} more methods)")
        compact_rec = _slice_item(row, False)
        compact_rec["text"] = f"{row['file_path']}:{row['start_line']}-{row['end_line']}\n" + "\n".join(compact_lines)
        if len(methods) >= 2 or (
            row["file_path"].endswith(_BRACE_LANG_EXTS)
            and len(methods) >= 1
        ):
            compact_summaries[sid] = compact_rec
        if (
            (len(methods) >= 3 and full_size >= 250)
            or (sid in py_enclosing and len(methods) >= 5)
            or (
                row["file_path"].endswith(_BRACE_LANG_EXTS)
                and len(methods) >= 1
                and full_size >= 120
            )
        ):
            auto_summarize_ids.add(sid)
    return summaries, compact_summaries, auto_summarize_ids


def _is_loop_or_mutation_line(s: str) -> bool:
    return not _is_skippable_caller_line(s) and (
        s.startswith(("for ", "for(", "while ", "while(")) or _has_mutation_call(s)
    )


def _extract_mid_spine_lines(mid_lines: list[str], comment_pfx: str) -> list[str]:
    cleaned = [raw.strip() for raw in mid_lines if not _is_skippable_caller_line(raw.strip())]
    if not any(_is_loop_or_mutation_line(s) for s in cleaned):
        return []
    return [
        f"        {comment_pfx} spine: {s}"
        for s in cleaned
        if _is_ctrl_header(s) or _has_mutation_call(s)
    ][:6]


def _format_condensed_method_body(
    body: str, comment_pfx: str, max_method_lines: int = 45, signature: str = ""
) -> str:
    m_lines = body.splitlines()
    if len(m_lines) <= max_method_lines:
        return body
    if max_method_lines <= 0 and signature:
        return f"    {signature}"
    head_n, tail_n = (12, 5) if max_method_lines < 28 else (20, 8)
    mid_lines = m_lines[head_n:-tail_n]
    spine = _extract_mid_spine_lines(mid_lines, comment_pfx)
    spine_block = ("\n" + "\n".join(spine)) if spine else ""
    return (
        "\n".join(m_lines[:head_n])
        + f"\n        {comment_pfx} ... ({len(m_lines) - head_n - tail_n} lines condensed){spine_block}\n"
        + "\n".join(m_lines[-tail_n:])
    )


def _select_public_method_line_cap(
    max_tokens: int, pub_count: int, max_method_lines: int | None
) -> int:
    if max_method_lines is not None:
        return max_method_lines
    budget_per_method = (max_tokens * 2) // max(60, pub_count * 15)
    return min(65, max(45, budget_per_method))


def _can_condense_class_methods(
    pub_methods: list[dict],
    int_methods: list[dict],
    min_internal: int,
    max_method_lines: int | None,
) -> bool:
    if not pub_methods or len(int_methods) < min_internal:
        return False
    return not (max_method_lines is not None and max_method_lines <= 10 and len(pub_methods) < 5)


def _condense_class_public_methods(
    store,
    sid: str,
    row: dict,
    db_arg: str,
    max_tokens: int,
    min_internal: int = 1,
    max_method_lines: int | None = None,
) -> str | None:
    sym = IndexedSymbol.from_row(row)
    if sym.kind not in {"class", "module"}:
        return None
    raw_methods = store.rows(
        """SELECT * FROM symbols WHERE file_path=? AND kind='method'
           AND start_line>=? AND end_line<=? ORDER BY start_line, symbol_id""",
        (sym.file_path, sym.start_line, sym.end_line),
    )
    direct = [m for m in raw_methods if m["qualname"].rpartition(".")[0] == sym.qualname]
    methods = direct or raw_methods
    ctor_names = {"__init__", "__new__", "constructor", sym.qualname.rsplit(".", 1)[-1]}
    pub_methods = [m for m in methods if not _is_internal_method(m, ctor_names)]
    int_methods = [m for m in methods if _is_internal_method(m, ctor_names)]
    if not _can_condense_class_methods(pub_methods, int_methods, min_internal, max_method_lines):
        return None
    body_lines = sym.body.splitlines()
    first_m_offset = max(1, methods[0]["start_line"] - sym.start_line)
    kept_parts = ["\n".join(body_lines[: min(first_m_offset, 15)])]
    comment_pfx = _comment_prefix(sym.file_path)
    full_size = tokens_for_length(len(sym.body))
    expand_budget = max(max_tokens, full_size * 2)
    span_len = sym.end_line - sym.start_line + 1
    kept_parts.append(
        f"    {comment_pfx} ... ({len(int_methods)} internal helper methods condensed in "
        f"{sym.file_path}:{sym.start_line}-{sym.end_line}; "
        f"use Read offset={sym.start_line} limit={span_len} or --max-tokens {expand_budget})"
    )
    max_m_lines = _select_public_method_line_cap(
        max_tokens, len(pub_methods), max_method_lines
    )
    for m in pub_methods:
        kept_parts.append(
            _format_condensed_method_body(
                m["body"], comment_pfx, max_m_lines, m["signature"]
            )
        )
    if sym.file_path.endswith(_BRACE_LANG_EXTS):
        kept_parts.append("}")
    return "\n".join(kept_parts)


def _target_skeleton_item(row: dict) -> dict:
    sym = IndexedSymbol.from_row(row)
    doc = _first_docstring_line(sym.body)
    sig = f"{sym.signature}\n    {doc}" if doc else sym.signature
    return {
        "symbol": sym.symbol_id,
        "text": f"{sym.file_path}:{sym.start_line}-{sym.end_line}\n{sig}",
    }


def _build_target_entry(
    store,
    row: dict,
    chosen: dict,
    max_tokens: int,
    emitted_headers: set[str],
    target_mode: str,
) -> dict:
    if target_mode == "skeleton":
        return _target_skeleton_item(row)
    entry = _target_item_with_preamble(store, row, chosen, max_tokens, emitted_headers)
    if max_tokens >= 2000 and len(row["body"].splitlines()) >= 100:
        condensed = _condense_class_public_methods(
            store, row["symbol_id"], row, "GRAPH", max_tokens, min_internal=5
        )
        if condensed is not None and len(condensed) < len(row["body"]):
            prefix = entry["text"][: len(entry["text"]) - len(row["body"])]
            return {"symbol": entry["symbol"], "text": f"{prefix}{condensed}"}
    return entry


def _compact_cached_entry(entry: dict) -> dict:
    lines = entry["text"].splitlines()
    head = "\n".join(lines[:2]) if len(lines) >= 2 else entry["text"]
    return {"symbol": entry["symbol"], "text": f"{head} // [cached in earlier slice]"}


def _session_scope_key(dedup_session) -> str:
    if isinstance(dedup_session, str) and dedup_session.strip():
        return dedup_session.strip()
    return os.environ.get("CODEGRAPH_DEDUP_SESSION", "").strip() or "default"


def _parse_session_cache_file(cache_path: Path) -> dict[str, dict[str, str]]:
    if not cache_path.is_file():
        return {}
    try:
        loaded = json.loads(cache_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    return {
        str(k): {str(sk): str(sv) for sk, sv in v.items()}
        for k, v in loaded.items()
        if isinstance(v, dict)
    }


def _read_session_dedup(
    database, dedup_session
) -> tuple[Path | None, str, dict[str, dict[str, str]]]:
    if isinstance(dedup_session, str) and not dedup_session.strip():
        return None, "", {}
    if not dedup_session or not database or str(database) == ":memory:":
        return None, "", {}
    cache_path = Path(f"{database}.session.json")
    scope = _session_scope_key(dedup_session)
    return cache_path, scope, _parse_session_cache_file(cache_path)


def _stage_session_dedup(
    sections: dict, base_seen: dict[str, str]
) -> tuple[set[str], dict[str, str]]:
    seen = dict(base_seen)
    deduped: set[str] = set()
    for sec in ("types_and_schemas", "upstream_callers", "downstream_dependencies"):
        updated = []
        for entry in sections[sec]:
            digest = hashlib.sha256(entry["text"].encode("utf-8")).hexdigest()
            if seen.get(entry["symbol"]) == digest:
                updated.append(_compact_cached_entry(entry))
                deduped.add(entry["symbol"])
            else:
                seen[entry["symbol"]] = digest
                updated.append(entry)
        sections[sec] = updated
    return deduped, seen


def _commit_session_dedup(
    cache_path: Path | None,
    scope: str,
    all_sessions: dict[str, dict[str, str]],
    updated_seen: dict[str, str],
) -> None:
    if cache_path is None:
        return
    merged = dict(all_sessions)
    merged.update(_parse_session_cache_file(cache_path))
    merged[scope] = updated_seen
    tmp_path = cache_path.with_suffix(f".json.tmp.{os.getpid()}")
    try:
        tmp_path.write_text(json.dumps(merged), encoding="utf-8")
        os.replace(tmp_path, cache_path)
    except OSError:
        with contextlib.suppress(OSError):
            tmp_path.unlink(missing_ok=True)


def _apply_session_dedup(sections: dict, database, dedup_session=True) -> set[str]:
    cache_path, scope, all_sessions = _read_session_dedup(database, dedup_session)
    if cache_path is None:
        return set()
    deduped, updated_seen = _stage_session_dedup(
        sections, all_sessions.get(scope, {})
    )
    _commit_session_dedup(cache_path, scope, all_sessions, updated_seen)
    return deduped


def _replace_target_section_entry(sections: dict, sid: str, row: dict, body_text: str) -> None:
    condensed_item = {
        "symbol": sid,
        "text": f"{row['file_path']}:{row['start_line']}-{row['end_line']}\n{body_text}",
    }
    sections["target_implementations"] = [
        condensed_item if entry["symbol"] == sid else entry
        for entry in sections["target_implementations"]
    ]


def _condense_targets_by_head_tail(sorted_targets, sections, result_fn, required, max_tokens):
    for sid, row in sorted_targets:
        if required["tokens"] <= max_tokens:
            break
        body_lines = row["body"].splitlines()
        if len(body_lines) <= 30:
            continue
        comment_pfx = _comment_prefix(row["file_path"])
        omitted_count = len(body_lines) - 26
        omitted_start = row["start_line"] + 18
        omitted_end = row["end_line"] - 8
        condensed_body = (
            "\n".join(body_lines[:18])
            + f"\n    {comment_pfx} ... ({omitted_count} lines condensed; lines {omitted_start}-{omitted_end} of {row['file_path']}; "
            + f"use Read offset={omitted_start} limit={omitted_count})\n"
            + "\n".join(body_lines[-8:])
        )
        _replace_target_section_entry(sections, sid, row, condensed_body)
        required = result_fn()
    return required


def _condense_targets_at_cap(
    store,
    sorted_targets: list[tuple[str, dict]],
    sections: dict,
    result_fn,
    required: dict,
    db_arg: str,
    max_tokens: int,
    budget_cap: int,
    cap_lines: int | None,
) -> dict:
    for sid, row in sorted_targets:
        if required["tokens"] <= budget_cap:
            break
        pub_condensed = _condense_class_public_methods(
            store, sid, row, db_arg, max_tokens, max_method_lines=cap_lines
        )
        if pub_condensed is not None and len(pub_condensed) < len(row["body"]):
            _replace_target_section_entry(sections, sid, row, pub_condensed)
            required = result_fn()
    return required


def _condense_multi_anchor_targets(
    store, chosen, sections, result_fn, database, max_tokens, soft_target_cap: int | None = None
):
    required = result_fn()
    pub_cap = soft_target_cap if soft_target_cap is not None else max_tokens
    if required["tokens"] <= pub_cap or max_tokens < 1000:
        return required
    db_arg = shlex.quote(str(database)) if database is not None else "GRAPH"
    sorted_targets = sorted(chosen.items(), key=lambda kv: -len(kv[1]["body"]))
    for budget_cap, cap_lines in ((pub_cap, None), (max_tokens, 0), (max_tokens, 22)):
        required = _condense_targets_at_cap(
            store, sorted_targets, sections, result_fn, required, db_arg, max_tokens, budget_cap, cap_lines
        )
    return _condense_targets_by_head_tail(sorted_targets, sections, result_fn, required, max_tokens)


_BOILERPLATE_METHOD_RE = re.compile(r"(?:_[0-9]{2}$|Step[0-9]{2}$|^_helper[0-9]+$)")


def _is_non_boilerplate_public_method(
    m: dict, ctor_names: set[str], has_edges: bool = False
) -> bool:
    if _is_internal_method(m, ctor_names):
        return False
    m_short = m["qualname"].rpartition(".")[2]
    if m_short in ctor_names or m_short in ("__enter__", "__exit__"):
        return False
    return has_edges or not bool(_BOILERPLATE_METHOD_RE.search(m_short))


def _connected_method_sids(store, file_path: str, cls_prefix: str) -> set[str]:
    rows = store.rows(
        """SELECT DISTINCT source_id AS sid FROM edges
           WHERE source_file=? AND kind IN ('CALLS', 'USES_TYPE')
           UNION
           SELECT DISTINCT target_id AS sid FROM edges
           WHERE target_id GLOB ? AND kind='CALLS'""",
        (file_path, f"{cls_prefix}.*"),
    )
    return {r["sid"] for r in rows}


def _find_class_for_fallback(store, mod_part: str, cls_part: str) -> dict | None:
    cls_rows = store.rows(
        "SELECT * FROM symbols WHERE (module=? OR file_path=?) AND kind='class' AND qualname=? ORDER BY start_line, symbol_id",
        (mod_part, mod_part, cls_part),
    )
    if not cls_rows and is_supported_source_path(mod_part):
        suf_cls = _resolve_suffix_path_anchor(store, mod_part, cls_part)
        if suf_cls is not None and suf_cls["kind"] == "class":
            cls_rows = [suf_cls]
    if not cls_rows:
        raw_stem = mod_part[:-3] if mod_part.endswith(".py") else mod_part
        pkg_stem = _strip_leading_path_prefix(raw_stem)
        cls_rows = store.rows(
            "SELECT * FROM symbols WHERE file_path GLOB ? AND kind='class' AND qualname=? ORDER BY start_line, symbol_id",
            (f"*{pkg_stem}*/*", cls_part),
        )
    return cls_rows[0] if len(cls_rows) == 1 else None


def _non_boilerplate_class_methods(store, cls_row: dict, cls_part: str) -> list[dict]:
    sym = IndexedSymbol.from_row(cls_row)
    all_methods = store.rows(
        """SELECT * FROM symbols WHERE file_path=? AND kind='method'
           AND start_line>=? AND end_line<=? ORDER BY start_line, symbol_id""",
        (sym.file_path, sym.start_line, sym.end_line),
    )
    connected = _connected_method_sids(store, sym.file_path, sym.symbol_id)
    ctor_names = {"__init__", "__new__", "constructor", cls_part.rsplit(".", 1)[-1]}
    return [
        m
        for m in all_methods
        if _is_non_boilerplate_public_method(m, ctor_names, m["symbol_id"] in connected)
    ]


def _collect_interface_method_seeds(store, sid: str, pub_short_names: set[str]) -> list[str]:
    if not pub_short_names:
        return []
    iface_methods: list[str] = []
    for irow in store.rows(
        "SELECT target_id FROM edges WHERE source_id=? AND kind='INHERITS'", (sid,)
    ):
        parent_id = irow["target_id"]
        fanin = store.rows(
            "SELECT COUNT(*) AS cnt FROM edges WHERE target_id=? AND kind='INHERITS'",
            (parent_id,),
        )
        if fanin and int(fanin[0]["cnt"] or 0) <= 8:
            for drow in store.rows(
                "SELECT target_id FROM edges WHERE source_id=? AND kind='DEFINES'",
                (parent_id,),
            ):
                m_sid = drow["target_id"]
                if m_sid.rpartition(".")[2].split("#", 1)[0] in pub_short_names:
                    iface_methods.append(m_sid)
    return iface_methods


def _collect_class_call_seeds(store, sid: str, row: dict) -> tuple[list[str], list[str]]:
    if row["kind"] != "class":
        return [sid], [sid]
    pub_methods = _non_boilerplate_class_methods(store, row, row["qualname"])
    pub_sids = [m["symbol_id"] for m in pub_methods]
    pub_short = {m["qualname"].rpartition(".")[2].split("#", 1)[0] for m in pub_methods}
    iface_sids = _collect_interface_method_seeds(store, sid, pub_short)
    return [sid, *pub_sids, *iface_sids], [sid, *pub_sids]


def _identifier_word_tokens(name: str) -> set[str]:
    raw = (t.lower() for t in re.findall(r"[A-Z]?[a-z0-9]+", name) if len(t) >= 3)
    return {t[:-1] if len(t) >= 4 and t.endswith("s") else t for t in raw}


def _method_similarity_score(m_norm: str, m_tokens: set[str], c_short: str) -> float:
    c_norm = c_short.lower().replace("_", "")
    if m_norm == c_norm:
        return 1.0
    c_tokens = _identifier_word_tokens(c_short)
    shared = len(m_tokens & c_tokens)
    tok_overlap = 0.0
    if shared:
        min_n = min(len(m_tokens), len(c_tokens))
        max_n = max(len(m_tokens), len(c_tokens))
        tok_overlap = 0.9 * (shared / min_n) + 0.1 * (shared / max_n)
    seq_ratio = difflib.SequenceMatcher(None, m_norm, c_norm).ratio()
    return max(tok_overlap, seq_ratio if seq_ratio >= 0.65 else 0.0)


def _match_fuzzy_class_method(core_pub: list[dict], m_short: str) -> dict | None:
    m_norm = m_short.lower().replace("_", "")
    m_tokens = _identifier_word_tokens(m_short)
    scored = [(_method_similarity_score(m_norm, m_tokens, c["qualname"].rpartition(".")[2]), i, c) for i, c in enumerate(core_pub)]
    scored.sort(key=lambda item: -item[0])
    if not scored or scored[0][0] < 0.45:
        return None
    if len(scored) > 1 and scored[0][0] == scored[1][0]:
        return None
    return scored[0][2]


def _resolve_fuzzy_single_class_in_module(store, mod_part: str, qual_part: str) -> dict | None:
    if not qual_part[0].isupper():
        return None
    cls_rows = store.rows(
        "SELECT * FROM symbols WHERE (module=? OR file_path=?) AND kind='class' ORDER BY start_line, symbol_id",
        (mod_part, mod_part),
    )
    if len(cls_rows) != 1:
        return None
    cls_short = cls_rows[0]["qualname"].rsplit(".", 1)[-1]
    ratio = difflib.SequenceMatcher(None, qual_part.lower(), cls_short.lower()).ratio()
    return cls_rows[0] if ratio >= 0.6 else None


def _resolve_batch_method_fallback(store, anchor_str: str) -> dict | None:
    mod_part, sep, qual_part = anchor_str.rpartition(":")
    if not sep or not qual_part:
        return None
    if "." not in qual_part:
        return _resolve_fuzzy_single_class_in_module(store, mod_part, qual_part)
    cls_part, _, m_short = qual_part.rpartition(".")
    m_rows = store.rows(
        "SELECT * FROM symbols WHERE (module=? OR file_path=?) AND kind='method' AND qualname GLOB ? ORDER BY start_line, symbol_id",
        (mod_part, mod_part, f"*.{m_short}"),
    )
    if len(m_rows) == 1:
        return m_rows[0]
    cls_row = _find_class_for_fallback(store, mod_part, cls_part)
    if cls_row is None:
        return None
    core_pub = _non_boilerplate_class_methods(store, cls_row, cls_part)
    if not core_pub:
        return cls_row
    return _match_fuzzy_class_method(core_pub, m_short)


def _format_unresolved_anchor_with_hint(store, anchor_str: str) -> str:
    mod_part, sep, qual_part = anchor_str.rpartition(":")
    if not sep or "." not in qual_part:
        return anchor_str
    cls_part, _, _ = qual_part.rpartition(".")
    cls_row = _find_class_for_fallback(store, mod_part, cls_part)
    if cls_row is None:
        return anchor_str
    core_pub = [
        f"{cls_row['file_path']}:{m['qualname']}"
        for m in _non_boilerplate_class_methods(store, cls_row, cls_part)
    ][:6]
    return f"{anchor_str} (available: {', '.join(core_pub)})" if core_pub else anchor_str


def _expand_overload_siblings(store, chosen: dict, row: dict) -> None:
    if row["kind"] not in ("method", "function"):
        return
    sid = row["symbol_id"]
    for ov in store.rows(
        "SELECT * FROM symbols WHERE module=? AND kind=? AND symbol_id GLOB ? ORDER BY start_line, symbol_id",
        (row["module"], row["kind"], f"{sid}#*"),
    ):
        chosen[ov["symbol_id"]] = ov


def _record_resolved_anchor(store, chosen: dict, row: dict, raw_anchor: str) -> None:
    chosen[row["symbol_id"]] = row
    if "#" not in raw_anchor:
        _expand_overload_siblings(store, chosen, row)


def _expand_comma_anchors(anchors) -> list[str]:
    return [p.strip() for raw in anchors for p in str(raw).split(",") if p.strip()]


def _resolve_anchor_batch_with_status(store, anchors) -> tuple[dict, list[str]]:
    norm_anchors = _expand_comma_anchors(anchors)
    chosen = {}
    unresolved: list[str] = []
    first_err = None
    for a in norm_anchors:
        try:
            _record_resolved_anchor(store, chosen, _anchor(store, a), a)
        except AnchorNotFoundError as exc:
            first_err = first_err or exc
            fb = len(norm_anchors) > 1 and _resolve_batch_method_fallback(store, a)
            if fb:
                _record_resolved_anchor(store, chosen, fb, a)
            else:
                unresolved.append(_format_unresolved_anchor_with_hint(store, a))
    if not chosen and first_err is not None:
        raise first_err
    return chosen, unresolved


def _resolve_anchor_batch(store, anchors) -> dict:
    chosen, _ = _resolve_anchor_batch_with_status(store, anchors)
    return chosen


def slice_context(
    store, anchors, max_tokens=8000, include_callee_bodies=False, *,
    render_estimator=None, database=None, target_mode="full", dedup_session=False,
):
    if (
        not isinstance(max_tokens, int)
        or isinstance(max_tokens, bool)
        or max_tokens <= 0
    ):
        raise InvalidBudgetError("max_tokens must be a positive integer")
    if target_mode not in ("full", "skeleton"):
        raise ValueError(f"Unsupported target_mode: {target_mode}")
    if not anchors:
        raise AnchorNotFoundError("at least one anchor is required")
    with store.read_snapshot():
        return _slice_context_in_snapshot(
            store,
            anchors,
            max_tokens=max_tokens,
            include_callee_bodies=include_callee_bodies,
            render_estimator=render_estimator,
            database=database,
            target_mode=target_mode,
            dedup_session=dedup_session,
        )


def _slice_context_in_snapshot(
    store,
    anchors,
    *,
    max_tokens=8000,
    include_callee_bodies=False,
    render_estimator=None,
    database=None,
    target_mode="full",
    dedup_session=False,
):
    chosen, unresolved_anchors = _resolve_anchor_batch_with_status(store, anchors)
    sections = {
        name: []
        for name in (
            "types_and_schemas",
            "upstream_callers",
            "target_implementations",
            "downstream_dependencies",
        )
    }
    item = _slice_item
    all_target_in_test = all(_is_test_symbol(r) for r in chosen.values())
    types = {}
    callers = {}
    caller_lines = {}
    callees = {}
    referenced_names = set()
    chosen_enclosing_classes = {
        f"{r['module']}:{r['qualname'].rpartition('.')[0]}"
        for r in chosen.values()
        if max_tokens >= 2000
        and r["file_path"].endswith(_BRACE_LANG_EXTS)
        and r["kind"] == "method"
        and "." in r["qualname"]
    }
    emitted_headers: set[str] = set()
    target_self_attrs: set[str] = set()
    enclosing_py_classes: set[str] = set()
    max_type_depth = 2 if len(chosen) > 1 else 3
    for sid, row in sorted(chosen.items()):
        sections["target_implementations"].append(
            _build_target_entry(store, row, chosen, max_tokens, emitted_headers, target_mode)
        )
        target_self_attrs.update(_extract_self_attrs(row["body"]))
        for token in row["body"].replace("(", " ").replace(".", " ").split():
            if token.isidentifier():
                referenced_names.add(token)
        type_seeds = [sid]
        enclosing_class_sid = None
        if row["kind"] == "method" and "." in row["qualname"]:
            enclosing_class_sid = f"{row['module']}:{row['qualname'].rpartition('.')[0]}"
            type_seeds.append(enclosing_class_sid)
            if (
                row["file_path"].endswith(".py")
                and max_tokens >= 2000
                and "self." in row["body"]
                and enclosing_class_sid not in chosen
            ):
                enc_rows = store.rows(
                    "SELECT * FROM symbols WHERE symbol_id=? AND kind='class'",
                    (enclosing_class_sid,),
                )
                if enc_rows:
                    types[enclosing_class_sid] = enc_rows[0]
                    enclosing_py_classes.add(enclosing_class_sid)
        elif row["kind"] == "class":
            for mrow in store.rows(
                "SELECT target_id FROM edges WHERE source_id=? AND kind='DEFINES'",
                (sid,),
            ):
                type_seeds.append(mrow["target_id"])
        for seed_id in type_seeds:
            for related in store.rows(
                """WITH RECURSIVE closure(node,depth) AS (
                    SELECT ?,0 UNION SELECT e.target_id,c.depth+1 FROM edges e
                    JOIN closure c ON e.source_id=c.node
                    WHERE e.kind IN ('USES_TYPE','INHERITS') AND c.depth<?
                ) SELECT DISTINCT s.* FROM symbols s JOIN closure c ON s.symbol_id=c.node
                    WHERE c.depth>0 AND s.kind IN ('class','type_alias') ORDER BY s.symbol_id""",
                (seed_id, max_type_depth),
            ):
                if (
                    related["symbol_id"] not in chosen
                    and related["symbol_id"] not in chosen_enclosing_classes
                ):
                    types[related["symbol_id"]] = related
        caller_seeds, callee_seeds = _collect_class_call_seeds(store, sid, row)
        for c_seed in caller_seeds:
            for related in store.rows(
                """SELECT s.*, e.line AS call_line FROM symbols s JOIN edges e
                    ON s.symbol_id=e.source_id WHERE e.target_id=? AND e.kind='CALLS'
                    ORDER BY e.line""",
                (c_seed,),
            ):
                csid = related["symbol_id"]
                if csid not in chosen and (all_target_in_test or not _is_test_symbol(related)):
                    callers[csid] = related
                    caller_lines.setdefault(csid, set()).add(related["call_line"])
        _recover_chained_callers(store, row, chosen, all_target_in_test, callers, caller_lines)
        for d_seed in callee_seeds:
            for related in store.rows(
                """SELECT DISTINCT s.* FROM symbols s JOIN edges e
                    ON s.symbol_id=e.target_id WHERE e.source_id=? AND e.kind='CALLS'
                    ORDER BY s.symbol_id""",
                (d_seed,),
            ):
                if related["symbol_id"] not in chosen and related["symbol_id"] not in types:
                    callees[related["symbol_id"]] = related

    def _is_enclosed_by_chosen(cand_row) -> bool:
        if f"{cand_row['module']}:" in chosen:
            return True
        if cand_row["kind"] == "method" and "." in cand_row["qualname"]:
            parent_sid = f"{cand_row['module']}:{cand_row['qualname'].rpartition('.')[0]}"
            if parent_sid in chosen:
                return True
        return False

    callers = {
        sid: row
        for sid, row in callers.items()
        if not _is_enclosed_by_chosen(row)
    }
    callees = {
        sid: row
        for sid, row in callees.items()
        if sid not in types
        and sid not in chosen_enclosing_classes
        and not _is_enclosed_by_chosen(row)
        and not (
            not include_callee_bodies
            and row["file_path"].endswith(".py")
            and row["kind"] == "method"
            and "." in row["qualname"]
            and f"{row['module']}:{row['qualname'].rpartition('.')[0]}" in (set(chosen) | set(types))
        )
    }
    def _type_item_with_doc(row):
        entry = item(row, True)
        if row["file_path"].endswith(_BRACE_LANG_EXTS):
            mod_rows = store.rows(
                "SELECT body FROM symbols WHERE symbol_id=?", (f"{row['module']}:",)
            )
            preceding = (
                mod_rows[0]["body"].splitlines()[: max(0, row["start_line"] - 1)]
                if mod_rows
                else None
            )
            doc_line = _first_docstring_line("", preceding)
            if doc_line and doc_line.strip() not in entry["text"]:
                header, sep, rest = entry["text"].partition("\n")
                entry["text"] = f"{header}{sep}{doc_line}\n{rest}"
        return entry

    sections["types_and_schemas"] = [
        _type_item_with_doc(row) for _, row in sorted(types.items())
    ]
    class_types = [(sid, row) for sid, row in sorted(types.items()) if row["kind"] == "class"]
    summaries, compact_summaries, auto_summarize_ids = _build_type_summaries(
        store,
        class_types,
        referenced_names,
        database,
        max_tokens,
        target_self_attrs=target_self_attrs,
        enclosing_py_classes=enclosing_py_classes,
    )
    from .pagerank import rank

    scores = rank(store, seed_symbols=tuple(chosen))
    totals = {name: len(items) for name, items in sections.items()}
    totals["upstream_callers"] = len(callers)
    totals["downstream_dependencies"] = len(callees)
    if len(chosen) > 1 and callees and len(callers) > 4:
        top_caller_ids = sorted(callers, key=lambda s: (-scores.get(s, 0), s))[:4]
        callers = {sid: callers[sid] for sid in top_caller_ids}
    body_fallbacks = {}
    omitted_detail_limit = None

    def result():
        payload = {"tokens": 0, "sections": sections}
        if unresolved_anchors:
            payload["unresolved_anchors"] = list(unresolved_anchors)
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
                for sid in sorted(candidates, key=lambda s: (-scores.get(s, 0), s)):
                    if sid not in emitted:
                        candidate = candidates[sid]
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

    for sid in auto_summarize_ids:
        sections["types_and_schemas"] = [
            compact_summaries[sid] if entry["symbol"] == sid else entry
            for entry in sections["types_and_schemas"]
        ]
    def _should_include_callee_body(cand_row) -> bool:
        sym = IndexedSymbol.from_row(cand_row)
        if include_callee_bodies:
            return True
        if (
            max_tokens >= 2000
            and sym.file_path.endswith(_BRACE_LANG_EXTS)
            and sym.kind in {"method", "function", "async_function", "class"}
            and (sym.end_line - sym.start_line + 1) <= 20
        ):
            return True
        return False

    def _extract_outer_return_hint(body: str) -> str | None:
        p = parser_for("tsx")
        data = body.encode("utf-8")
        root = p.parse(data).root_node
        if root.has_error:
            data = f"class _ {{\n{body}\n}}".encode("utf-8")
            root = p.parse(data).root_node
        fbody = next((d for d in scoped_descendants(root, set()) if d.type == "statement_block"), None)
        if fbody is None:
            return None
        last_ret = None
        for d in scoped_descendants(fbody, {*_TS_TYPE_NODES, *_TS_FUNCTION_NODES}):
            if d.type == "return_statement":
                s_ln = node_text(data, d).splitlines()[0][6:].strip()
                if s_ln not in {"", ";", "null;", "true;", "false;", "0;"}:
                    last_ret = s_ln
        return last_ret

    def _callee_item(cand_row, full: bool):
        base = item(cand_row, full)
        if not full:
            ret_expr = _extract_outer_return_hint(cand_row["body"])
            if ret_expr:
                comment_pfx = _comment_prefix(cand_row["file_path"])
                base["text"] = f"{base['text']}\n    {comment_pfx} returns: {ret_expr}"
        return base

    for name, candidates in (
        ("upstream_callers", callers),
        ("downstream_dependencies", callees),
    ):
        for sid in sorted(candidates, key=lambda s: (-scores.get(s, 0), s)):
            if name == "upstream_callers":
                sections[name].append(_caller_item(candidates[sid], caller_lines.get(sid)))
            else:
                full = name == "downstream_dependencies" and _should_include_callee_body(
                    candidates[sid]
                )
                sections[name].append(_callee_item(candidates[sid], full))
    pre_dedup_map = {entry["symbol"]: entry for entry in sections["types_and_schemas"]}
    cache_path, sess_scope, all_sessions = _read_session_dedup(database, dedup_session)
    base_seen = all_sessions.get(sess_scope, {})
    deduped_ids, staged_seen = (
        _stage_session_dedup(sections, base_seen) if cache_path is not None else (set(), {})
    )
    auto_summarize_ids.update(deduped_ids)
    complete = result()
    if complete["tokens"] <= max_tokens:
        _commit_session_dedup(cache_path, sess_scope, all_sessions, staged_seen)
        return complete
    for sid, summary in summaries.items():
        if sid in auto_summarize_ids:
            continue
        original = next(entry for entry in sections["types_and_schemas"] if entry["symbol"] == sid)
        sections["types_and_schemas"] = [
            summary if entry["symbol"] == sid else entry
            for entry in sections["types_and_schemas"]
        ]
        candidate = result()
        if candidate["tokens"] < complete["tokens"]:
            complete = candidate
            staged_seen[sid] = hashlib.sha256(summary["text"].encode("utf-8")).hexdigest()
        else:
            sections["types_and_schemas"] = [
                original if entry["symbol"] == sid else entry
                for entry in sections["types_and_schemas"]
            ]
    if complete["tokens"] <= max_tokens:
        _commit_session_dedup(cache_path, sess_scope, all_sessions, staged_seen)
        return complete
    sections["types_and_schemas"] = [
        pre_dedup_map[entry["symbol"]] if entry["symbol"] in deduped_ids else entry
        for entry in sections["types_and_schemas"]
    ]
    for name in ("upstream_callers", "downstream_dependencies"):
        sections[name].clear()
    if len(chosen) > 1 and any(
        totals[name] > 0 for name in ("upstream_callers", "downstream_dependencies")
    ):
        omitted_detail_limit = 2
    required = result()
    if required["tokens"] > max_tokens and compact_summaries:
        for sid, c_summary in compact_summaries.items():
            if sid in deduped_ids:
                continue
            sections["types_and_schemas"] = [
                c_summary if entry["symbol"] == sid else entry
                for entry in sections["types_and_schemas"]
            ]
        required = result()
    soft_cap = (
        int(max_tokens * 0.72)
        if len(chosen) > 1 and (callers or callees) and max_tokens >= 2000
        else max_tokens
    )
    required = _condense_multi_anchor_targets(
        store, chosen, sections, result, database, max_tokens, soft_target_cap=soft_cap
    )
    if required["tokens"] > max_tokens and target_mode != "skeleton":
        sections["target_implementations"] = [
            item(row, True) for _, row in sorted(chosen.items())
        ]
        required = _condense_multi_anchor_targets(
            store, chosen, sections, result, database, max_tokens, soft_target_cap=soft_cap
        )
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
        ("downstream_dependencies", callees),
        ("upstream_callers", callers),
    ):
        for sid in sorted(candidates, key=lambda s: (-scores.get(s, 0), s)):
            if name == "upstream_callers":
                candidate = _caller_item(candidates[sid], caller_lines.get(sid))
                full = False
            else:
                full = name == "downstream_dependencies" and _should_include_callee_body(
                    candidates[sid]
                )
                candidate = _callee_item(candidates[sid], full)
            sections[name].append(candidate)
            if result()["tokens"] > max_tokens:
                sections[name].pop()
                if full:
                    sections[name].append(_callee_item(candidates[sid], False))
                    body_fallbacks[sid] = candidates[sid]
                    if result()["tokens"] > max_tokens:
                        sections[name].pop()
                        body_fallbacks.pop(sid)
    _, final_seen = (
        _stage_session_dedup(sections, base_seen) if cache_path is not None else (set(), {})
    )
    final = result()
    if final["tokens"] > max_tokens:
        raise RequiredAnchorOverflow(
            f"required definitions need {final['tokens']} tokens including omission metadata, budget is {max_tokens}"
        )
    _commit_session_dedup(cache_path, sess_scope, all_sessions, final_seen)
    return final
