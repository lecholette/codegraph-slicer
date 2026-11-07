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


def _resolve_suffix_path_anchor(store, path: str, target: str):
    if not is_supported_source_path(path):
        return None
    cand_files = store.rows(
        "SELECT path FROM files WHERE path GLOB ? ORDER BY length(path), path",
        (f"*/{path.lstrip('/')}",),
    )
    if len(cand_files) != 1:
        return None
    fpath = cand_files[0]["path"]
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


def _slice_item(row, full):
    sym = IndexedSymbol.from_row(row)
    text = sym.body if full else sym.signature
    return {
        "symbol": sym.symbol_id,
        "text": f"{sym.file_path}:{sym.start_line}-{sym.end_line}\n{text}",
    }


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
    if sym.kind == "method" and "." in sym.qualname and sym.file_path.endswith(_BRACE_LANG_EXTS):
        class_qual = sym.qualname.rpartition(".")[0]
        class_sid = f"{sym.module}:{class_qual}"
        if class_sid in chosen:
            return base
        class_key = f"class:{class_sid}"
        if emitted_headers is None or class_key not in emitted_headers:
            class_rows = store.rows(
                "SELECT start_line, end_line FROM symbols WHERE symbol_id=?",
                (class_sid,),
            )
            if class_rows:
                c_start = class_rows[0]["start_line"]
                class_short = class_qual.rsplit(".", 1)[-1]
                first_m_rows = store.rows(
                    "SELECT MIN(start_line) AS first_m FROM symbols WHERE file_path=? AND kind='method' AND start_line > ? AND qualname NOT GLOB ? AND qualname NOT GLOB '*.constructor'",
                    (sym.file_path, c_start, f"*.{class_short}"),
                )
                first_m = (
                    first_m_rows[0]["first_m"]
                    if first_m_rows and first_m_rows[0]["first_m"] is not None
                    else sym.start_line
                )
                c_end = min(first_m - 1, sym.start_line - 1, c_start + 20)
                added_class = False
                for ln in range(c_start, c_end + 1):
                    if 1 <= ln <= len(file_lines):
                        line_txt = file_lines[ln - 1]
                        if line_txt.strip():
                            header_lines.append(line_txt)
                            header_end_line = max(header_end_line, ln)
                            added_class = True
                if emitted_headers is not None and added_class:
                    emitted_headers.add(class_key)
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


def _caller_boundary_lines(body_lines: list[str], code_start: int, window_start: int) -> list[tuple[int, str]]:
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
            for k, cl in _caller_boundary_lines(body_lines, code_start, window_start):
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


def _first_docstring_line(body: str, preceding_lines: list[str] | None = None):
    if body:
        try:
            parsed = ast.parse(textwrap.dedent(body))
            doc = ast.get_docstring(parsed.body[0]) if parsed.body else None
        except (SyntaxError, IndexError):
            doc = None
        if doc:
            return doc.splitlines()[0]
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


def _format_condensed_method_body(body: str, comment_pfx: str) -> str:
    m_lines = body.splitlines()
    if len(m_lines) <= 45:
        return body
    return (
        "\n".join(m_lines[:20])
        + f"\n        {comment_pfx} ... ({len(m_lines) - 28} lines condensed)\n"
        + "\n".join(m_lines[-8:])
    )


def _condense_class_public_methods(
    store, sid: str, row: dict, db_arg: str, max_tokens: int, min_internal: int = 1
) -> str | None:
    sym = IndexedSymbol.from_row(row)
    if sym.kind not in {"class", "module"}:
        return None
    methods = store.rows(
        """SELECT * FROM symbols WHERE file_path=? AND kind='method'
           AND start_line>=? AND end_line<=? ORDER BY start_line, symbol_id""",
        (sym.file_path, sym.start_line, sym.end_line),
    )
    ctor_names = {"__init__", "__new__", "constructor", sym.qualname.rsplit(".", 1)[-1]}
    pub_methods = [m for m in methods if not _is_internal_method(m, ctor_names)]
    int_methods = [m for m in methods if _is_internal_method(m, ctor_names)]
    if not pub_methods or len(int_methods) < min_internal:
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
    for m in pub_methods:
        kept_parts.append(_format_condensed_method_body(m["body"], comment_pfx))
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


def _condense_multi_anchor_targets(store, chosen, sections, result_fn, database, max_tokens):
    required = result_fn()
    if required["tokens"] <= max_tokens or max_tokens < 1000:
        return required
    db_arg = shlex.quote(str(database)) if database is not None else "GRAPH"
    sorted_targets = sorted(chosen.items(), key=lambda kv: -len(kv[1]["body"]))
    for sid, row in sorted_targets:
        if required["tokens"] <= max_tokens:
            break
        pub_condensed = _condense_class_public_methods(store, sid, row, db_arg, max_tokens)
        if pub_condensed is not None and len(pub_condensed) < len(row["body"]):
            condensed_item = {
                "symbol": sid,
                "text": f"{row['file_path']}:{row['start_line']}-{row['end_line']}\n{pub_condensed}",
            }
            sections["target_implementations"] = [
                condensed_item if entry["symbol"] == sid else entry
                for entry in sections["target_implementations"]
            ]
            required = result_fn()
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
        condensed_item = {
            "symbol": sid,
            "text": f"{row['file_path']}:{row['start_line']}-{row['end_line']}\n{condensed_body}",
        }
        sections["target_implementations"] = [
            condensed_item if entry["symbol"] == sid else entry
            for entry in sections["target_implementations"]
        ]
        required = result_fn()
    return required


def _resolve_batch_method_fallback(store, anchor_str: str) -> dict | None:
    mod_part, sep, qual_part = anchor_str.rpartition(":")
    if not sep or not qual_part:
        return None
    if "." not in qual_part:
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
    _, _, m_short = qual_part.rpartition(".")
    m_rows = store.rows(
        "SELECT * FROM symbols WHERE (module=? OR file_path=?) AND kind='method' AND qualname GLOB ? ORDER BY start_line, symbol_id",
        (mod_part, mod_part, f"*.{m_short}"),
    )
    return m_rows[0] if len(m_rows) == 1 else None


def _resolve_anchor_batch_with_status(store, anchors) -> tuple[dict, list[str]]:
    chosen = {}
    unresolved: list[str] = []
    first_err = None
    for a in anchors:
        try:
            row = _anchor(store, a)
            chosen[row["symbol_id"]] = row
        except AnchorNotFoundError as exc:
            if first_err is None:
                first_err = exc
            fb = len(anchors) > 1 and _resolve_batch_method_fallback(store, a)
            if fb:
                chosen[fb["symbol_id"]] = fb
            else:
                unresolved.append(a)
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
        for related in store.rows(
            """SELECT s.*, e.line AS call_line FROM symbols s JOIN edges e
                ON s.symbol_id=e.source_id WHERE e.target_id=? AND e.kind='CALLS'
                ORDER BY e.line""",
            (sid,),
        ):
            csid = related["symbol_id"]
            if csid not in chosen and (all_target_in_test or not _is_test_symbol(related)):
                callers[csid] = related
                caller_lines.setdefault(csid, set()).add(related["call_line"])
        _recover_chained_callers(store, row, chosen, all_target_in_test, callers, caller_lines)
        for related in store.rows(
            """SELECT DISTINCT s.* FROM symbols s JOIN edges e
                ON s.symbol_id=e.target_id WHERE e.source_id=? AND e.kind='CALLS'
                ORDER BY s.symbol_id""",
            (sid,),
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
    required = _condense_multi_anchor_targets(store, chosen, sections, result, database, max_tokens)
    if required["tokens"] > max_tokens and target_mode != "skeleton":
        sections["target_implementations"] = [
            item(row, True) for _, row in sorted(chosen.items())
        ]
        required = _condense_multi_anchor_targets(store, chosen, sections, result, database, max_tokens)
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
