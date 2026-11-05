"""Standalone commands; query commands open existing databases read-only."""

import argparse
import json
import os
import re
import sqlite3
import stat
from collections import Counter
from contextlib import ExitStack
from pathlib import Path
import xml.sax.saxutils as _saxutils

from .ast_extractor import CodeGraphError
from .delta import compute_delta, project_delta_v2, summarize_delta
from .flow import FlowBudgetExceeded, FlowQueryError, UnsupportedFlowError, compact_flow_result
from .flow_query import query_flow
from .pagerank import build_repo_map, build_repo_outline
from .slicer import (
    AnchorNotFoundError,
    build_slice_outline,
    query_call_impact,
    rendered_tokens,
    serialized_tokens,
    slice_context,
    tokens_for_length,
)
from .store import GraphStore, StaleSnapshotError

_DEPENDENCIES = (("DATA_DEP", "data ←"), ("CONTROL_DEP", "control ←"))
_ENDPOINTS = ("entry", "exit")


def _fence_lang(path: str) -> str:
    if path.endswith(".java"):
        return "java"
    if path.endswith(".tsx"):
        return "tsx"
    if path.endswith(".ts"):
        return "typescript"
    return "python"


def _map_markdown_item(item):
    fence = "`" * max(
        3,
        1 + max((len(run) for run in re.findall(r"`+", item["text"])), default=0),
    )
    lang = _fence_lang(item.get("path", ""))
    return f"### {item['symbol']}\n\n{fence}{lang}\n{item['text']}\n{fence}"


class _MapMarkdownEstimator:
    """Additive exact character accounting for the CLI map Markdown shape."""

    def item_cost(self, item):
        return len(_map_markdown_item(item))

    def __call__(self, payload):
        return rendered_tokens(_markdown(payload))

    def estimate_map(self, item_cost, item_count, omitted, top_file_omitted=None):
        fixed = ["Estimated context tokens: 0"]
        if omitted:
            fixed.extend(
                [f"Omitted **files** records: {omitted}", "rerun with --max-tokens N"]
            )
        if top_file_omitted is not None:
            fixed.append(
                f"Top ranked file omitted: {top_file_omitted['path']} "
                f"(first item estimate: {top_file_omitted['max_tokens']} tokens)"
            )
        fixed.append("## repository_map")
        size = sum(map(len, fixed)) + 2 * (len(fixed) - 1)
        size += item_cost + 2 * item_count
        tokens = 0
        while True:
            estimate = tokens_for_length(size - len("0") + len(str(tokens)) + 1)
            if estimate == tokens:
                return estimate
            tokens = estimate


class _OutlineMarkdownEstimator:
    """Exact Markdown accounting from selected outline records."""

    def estimate_outline(self, files, edges, omitted, tokens=0):
        blocks = ["# Repository outline (schema v1)", f"Estimated context tokens: {tokens}"]
        for file_id, path, entities in files:
            blocks.append(f"## {path} · `{file_id}`")
            blocks.extend(
                f"- `{entity['id']}` {entity['kind']} {entity['name']} "
                f"(line {entity['line']}): `{entity['signature']}`"
                for entity in entities
            )
        if edges:
            blocks.append("## Edges")
            blocks.extend(
                f"- {edge['kind']}: `{edge['from']}` → `{edge['to']}`"
                for edge in edges
            )
        for name, records in omitted.items():
            blocks.append(f"## Omitted {name}")
            blocks.append(f"- {name}: {records}")
        return rendered_tokens("\n\n".join(blocks))

    def __call__(self, payload):
        files = [(item["id"], item["path"], item["entities"]) for item in payload["files"]]
        tokens = 0
        while True:
            estimate = self.estimate_outline(
                files, payload["edges"], payload.get("omitted", {}), tokens
            )
            if estimate == tokens:
                return estimate
            tokens = estimate


def _node_refs(nodes):
    per_line = Counter(n["line"] for n in nodes if n["kind"] not in _ENDPOINTS)
    return {n["id"]: n["kind"] if n["kind"] in _ENDPOINTS
            else f"L{n['line']}" if per_line[n["line"]] == 1 else f"L{n['line']}#{n['id']}"
            for n in nodes}


def _flow_markdown(result):
    graph = result["flow"]
    identity = graph["identity"]
    source_hash = graph["source_sha256"]
    ref = _node_refs(graph["nodes"])
    blocks = [f"# Python local flow: `{graph['function']}`",
              f"Graph JSON ≈ {serialized_tokens(graph)} tokens; this view ≈ 0 tokens · profile `{graph['profile']}` · Python {graph['python_version']}"]
    if identity["blob_sha"] == f"sha256:{source_hash}":
        blocks.append(f"Source: `{identity['path']}` · `{identity['symbol_id']}` · SHA-256 `{source_hash}`")
    else:
        blocks.append(f"Source: `{identity['path']}` · `{identity['symbol_id']}` · blob `{identity['blob_sha']}` · SHA-256 `{source_hash}`")
    blocks.append("## Dependencies\n\nStatements in source order, keyed by line (`L7#n0012` when a line holds several). "
                  "Each lists what it depends on as `source (label)`; `entry` is the function entry. "
                  "`x[*]` means some unresolved element or attribute of `x`. "
                  "CFG and AST edges are omitted; `--format json` has the full record.")
    incoming = {}
    for edge in graph["edges"]:
        incoming.setdefault((edge["target"], edge["kind"]), []).append(edge)
    for node in sorted((n for n in graph["nodes"] if n["kind"] not in _ENDPOINTS), key=lambda n: (n["line"], n["id"])):
        flags = "".join(f" · {flag}" for flag, on in (("unreachable", not node["reachable"]), ("synthetic", node["synthetic"])) if on)
        span = "" if node["line"] == node["end_line"] else f" (to L{node['end_line']})"
        statement = node["text"]
        if "\n" not in statement:
            backticks = max((len(run) for run in re.findall(r"`+", statement)), default=0)
            fence = "`" * (backticks + 1)
            padding = " " if statement.startswith("`") or statement.endswith("`") else ""
            code = f"{fence}{padding}{statement}{padding}{fence}"
            lines = [f"- {ref[node['id']]} {node['kind']}{span}{flags}: {code}"]
        else:
            runs = re.findall(r"(`+|~+)", statement)
            longest = max((len(run) for run in runs), default=0)
            fence = "`" * max(3, longest + 1)
            lines = [f"- {ref[node['id']]} {node['kind']}{span}{flags}:", "  " + fence]
            lines.extend("  " + line for line in statement.split("\n"))
            lines.append("  " + fence)
        facts = "; ".join(f"{name}: {', '.join(values)}" for name, values in
                          (("defines", node["defs"]), ("uses", node["uses"]), ("writes", node["mutation_targets"])) if values)
        if facts:
            lines.append(f"  - {facts}")
        for kind, arrow in _DEPENDENCIES:
            edges = incoming.get((node["id"], kind), [])
            if edges:
                lines.append(f"  - {arrow} " + ", ".join(f"{ref[e['source']]} ({e['label']})" for e in edges))
        blocks.append("\n".join(lines))
    blocks.append("## Diagnostics")
    grouped = {}
    for d in graph["diagnostics"]:
        grouped.setdefault((d["code"], d["message"]), []).append(ref[d["node"]])
    blocks.extend(f"- `{code}` {message}: {', '.join(where)}" for (code, message), where in grouped.items())
    blocks.append("## Limitations")
    blocks.extend(f"- {item}" for item in graph["limitations"])
    text = "\n\n".join(blocks)
    estimate = rendered_tokens(text)
    for _ in range(16):
        rendered = text.replace("this view ≈ 0 tokens", f"this view ≈ {estimate} tokens", 1)
        updated = rendered_tokens(rendered)
        if updated == estimate:
            return rendered
        estimate = updated
    raise FlowBudgetExceeded("flow Markdown token estimate did not reach a fixed point")


def _xml_attr(val):
    return _saxutils.quoteattr(str(val))


def _xml(result):
    if "schema_version" in result and "entities" in result and "edges" in result:
        blocks = [f'<slice_outline schema_version="{result["schema_version"]}" tokens="{result.get("tokens", 0)}">']
        blocks.append("  <entities>")
        for entity in result["entities"]:
            blocks.append(
                f'    <entity id={_xml_attr(entity.get("id", ""))} '
                f'kind={_xml_attr(entity.get("kind", ""))} '
                f'symbol={_xml_attr(entity.get("symbol", ""))} '
                f'path={_xml_attr(entity.get("path", ""))} '
                f'line={_xml_attr(entity.get("line", ""))} '
                f'signature={_xml_attr(entity.get("signature", ""))} />'
            )
        blocks.append("  </entities>")
        blocks.append("  <edges>")
        for edge in result["edges"]:
            blocks.append(
                f'    <edge kind={_xml_attr(edge.get("kind", ""))} '
                f'from={_xml_attr(edge.get("from", ""))} '
                f'to={_xml_attr(edge.get("to", ""))} '
                f'line={_xml_attr(edge.get("line", ""))} />'
            )
        blocks.append("  </edges>")
        if result.get("omitted"):
            blocks.append("  <omitted>")
            for k, v in result["omitted"].items():
                if v:
                    blocks.append(f'    <{k} count="{v}" />')
            blocks.append("  </omitted>")
        blocks.append("</slice_outline>")
        return "\n".join(blocks)

    if "schema_version" in result and "files" in result and "edges" in result:
        blocks = [f'<repository_outline schema_version="{result["schema_version"]}" tokens="{result.get("tokens", 0)}">']
        for file in result["files"]:
            blocks.append(f'  <file id={_xml_attr(file.get("id", ""))} path={_xml_attr(file.get("path", ""))}>')
            for entity in file.get("entities", []):
                blocks.append(
                    f'    <entity id={_xml_attr(entity.get("id", ""))} '
                    f'kind={_xml_attr(entity.get("kind", ""))} '
                    f'name={_xml_attr(entity.get("name", ""))} '
                    f'line={_xml_attr(entity.get("line", ""))} '
                    f'signature={_xml_attr(entity.get("signature", ""))} />'
                )
            blocks.append("  </file>")
        if result.get("edges"):
            blocks.append("  <edges>")
            for edge in result["edges"]:
                blocks.append(
                    f'    <edge kind={_xml_attr(edge.get("kind", ""))} '
                    f'from={_xml_attr(edge.get("from", ""))} '
                    f'to={_xml_attr(edge.get("to", ""))} />'
                )
            blocks.append("  </edges>")
        blocks.append("</repository_outline>")
        return "\n".join(blocks)

    if result.get("profile") == "python-call-impact-v2":
        blocks = [f'<call_impact anchor={_xml_attr(result.get("anchor", ""))} tokens="{result.get("tokens", 0)}">']
        for caller in result.get("callers", []):
            blocks.append(
                f'  <caller id={_xml_attr(caller.get("id", ""))} '
                f'symbol={_xml_attr(caller.get("symbol", ""))} '
                f'depth={_xml_attr(caller.get("depth", ""))} '
                f'file={_xml_attr(caller.get("file", ""))} '
                f'line={_xml_attr(caller.get("line", ""))} />'
            )
        blocks.append("</call_impact>")
        return "\n".join(blocks)

    if "sections" in result:
        blocks = [f'<context tokens="{result.get("tokens", 0)}">']
        if "omitted" in result:
            blocks.append("  <omitted>")
            for k, v in result["omitted"].items():
                if v:
                    blocks.append(f'    <{k} count="{v}" />')
            blocks.append("  </omitted>")
        names = list(result["sections"])
        if "target_implementations" in names:
            names = [n for n in names if n != "target_implementations"] + ["target_implementations"]
        for section_name in names:
            items = result["sections"][section_name]
            if not items:
                continue
            blocks.append(f"  <{section_name}>")
            for item in items:
                sym_attr = f' symbol={_xml_attr(item["symbol"])}' if "symbol" in item else ""
                raw_text = item.get("text", "").rstrip()
                first_nl = raw_text.find("\n")
                header = raw_text[:first_nl] if first_nl != -1 else ""
                file_path = item.get("file_path")
                lines_span = None
                if not file_path and ":" in header:
                    cand_path, _, cand_span = header.rpartition(":")
                    if "-" in cand_span and cand_span.replace("-", "").isdigit():
                        file_path = cand_path
                        lines_span = cand_span
                file_attr = f' file={_xml_attr(file_path)}' if file_path else ""
                start = item.get("start_line")
                end = item.get("end_line")
                lines_attr = f' lines="{start}-{end}"' if start and end else (f' lines="{lines_span}"' if lines_span else "")
                blocks.append(f'    <code_slice{sym_attr}{file_attr}{lines_attr}>')
                blocks.append(raw_text)
                blocks.append("    </code_slice>")
            blocks.append(f"  </{section_name}>")
        blocks.append("</context>")
        return "\n".join(blocks)

    if "items" in result:
        blocks = [f'<repository_map tokens="{result.get("tokens", 0)}">']
        if "omitted" in result:
            blocks.append("  <omitted>")
            for k, v in result["omitted"].items():
                if v:
                    blocks.append(f'    <{k} count="{v}" />')
            blocks.append("  </omitted>")
        for item in result["items"]:
            file_path = item.get("file_path") or item.get("path")
            file_attr = f' path={_xml_attr(file_path)}' if file_path else ""
            sym_attr = f' symbol={_xml_attr(item["symbol"])}' if "symbol" in item else ""
            blocks.append(f'  <file{file_attr}{sym_attr}>')
            blocks.append(item.get("text", "").rstrip())
            blocks.append("  </file>")
        blocks.append("</repository_map>")
        return "\n".join(blocks)

    return f"<codegraph_report>\n{json.dumps(result, indent=2)}\n</codegraph_report>"


def _render_budget_estimator(output_format, markdown_est=None):
    if output_format == "markdown":
        return markdown_est if markdown_est is not None else (lambda candidate: rendered_tokens(_markdown(candidate)))
    if output_format == "xml":
        return lambda candidate: rendered_tokens(_xml(candidate))
    return None


def _markdown(result):
    if "schema_version" in result and "entities" in result and "edges" in result:
        blocks = [f"# Slice outline (schema v{result['schema_version']})"]
        blocks.append(f"Estimated context tokens: {result['tokens']}")
        blocks.append("## Entities")
        blocks.extend(
            f"- `{entity['id']}` {entity['kind']} `{entity['symbol']}` "
            f"({entity['path']}:{entity['line']}): `{entity['signature']}`"
            for entity in result["entities"]
        )
        blocks.append("## Edges")
        blocks.extend(
            f"- {edge['kind']} `{edge['from']}` → `{edge['to']}` (line {edge['line']})"
            for edge in result["edges"]
        )
        blocks.append("## Diagnostics")
        blocks.extend(
            f"- {item['kind']} from `{item['source']}` to `{item['target']}` "
            f"(line {item['line']}): {item['reason']}"
            for item in result["diagnostics"]
        )
        if result.get("diagnostic_counts"):
            blocks.append("## Other entity diagnostic counts")
            blocks.extend(
                f"- `{source}`: {count} unresolved observations"
                for source, count in result["diagnostic_counts"].items()
            )
        if result.get("omitted"):
            blocks.append("## Omitted records")
            blocks.extend(
                f"- {name}: {count}"
                for name, count in result["omitted"].items()
            )
        return "\n\n".join(blocks)
    if "schema_version" in result and "files" in result and "edges" in result:
        blocks = [f"# Repository outline (schema v{result['schema_version']})"]
        blocks.append(f"Estimated context tokens: {result['tokens']}")
        for file in result["files"]:
            blocks.append(f"## {file['path']} · `{file['id']}`")
            blocks.extend(
                f"- `{entity['id']}` {entity['kind']} {entity['name']} "
                f"(line {entity['line']}): `{entity['signature']}`"
                for entity in file["entities"]
            )
        if result["edges"]:
            blocks.append("## Edges")
            blocks.extend(
                f"- {edge['kind']}: `{edge['from']}` → `{edge['to']}`"
                for edge in result["edges"]
            )
        for name, records in result.get("omitted", {}).items():
            blocks.append(f"## Omitted {name}")
            blocks.extend(
                f"- `{item['path']}`{(' ' + item['name']) if 'name' in item else ''}: "
                f"rerun with --max-tokens {item['max_tokens']}"
                for item in records
            )
        return "\n\n".join(blocks)
    if result.get("schema") == "python-delta-v2":
        blocks = ["# Static delta report (python-delta-v2)"]
        blocks.append("Filters: " + json.dumps(result["filters"], ensure_ascii=False, sort_keys=True))
        blocks.append(f"Test selection requested: `{result['test_selection']}`.")
        blocks.append(result["test_gap_note"])
        blocks.append("## Totals\n\n" + "\n".join(
            f"- **{name.replace('_', ' ')}:** {count}"
            for name, count in result["totals"].items()
        ))
        for name, records in result["samples"].items():
            if records:
                rendered = []
                for row in records:
                    if isinstance(row, dict):
                        detail = "; ".join(
                            f"{key.replace('_', ' ')}: {value}"
                            for key, value in row.items()
                        )
                    elif isinstance(row, list):
                        detail = " · ".join(map(str, row))
                    else:
                        detail = str(row)
                    rendered.append("- " + detail)
                blocks.append(
                    "## " + name.replace("_", " ").title() + "\n\n" + "\n".join(rendered)
                )
        for name, count in result["omitted"].items():
            if count:
                blocks.append(f"Omitted **{name.replace('_', ' ')}** records: {count}")
        blocks.append(f"Estimated compact JSON tokens: {result['serialized_tokens']}")
        return "\n\n".join(blocks)
    if "summary" in result or "modified_symbols" in result and "serialized_tokens" in result:
        blocks = ["# Static delta report"]
        if "summary" in result:
            blocks.append("## Summary\n\n" + "\n".join(
                f"- **{name.replace('_', ' ')}"
                f"{' (none selected)' if name == 'untested_modified_symbols' and result['summary']['test_selection'] == 'none_selected' else ''}:** {count}"
                for name, count in result["summary"]["counts"].items()
            ))
            blocks.append(
                f"Test selection requested: `{result['summary']['test_selection']}`. "
                f"Static test gap statuses: {result['summary']['test_gap_status_counts']}.\n\n"
                + result["summary"]["test_selection_note"] + "\n\n"
                + result["summary"]["test_gap_note"] + "\n\n"
                + result["summary"]["retrieve_details"]
            )
        elif "test_selection" in result:
            blocks.append(
                f"Test selection requested: `{result['test_selection']}`. "
                + result["test_selection_note"]
            )
        for kind, count in result.get("omitted", {}).items():
            if count:
                blocks.append(f"Omitted **{kind.replace('_', ' ')}** records: {count}")
        kinds = ("boundary_violations", "new_cycles", "untested_modified_symbols", "test_coverage",
                 "complexity_drift", "import_changes", "affected_entrypoints", "modified_symbols",
                 "added_edges", "removed_edges")
        for kind in kinds:
            values = result.get(kind, [])
            if values:
                bullets = []
                for item in values:
                    if isinstance(item, dict):
                        bullets.append("- " + "; ".join(f"{key.replace('_', ' ')}: {value}" for key, value in item.items()))
                    elif isinstance(item, list):
                        bullets.append("- " + " · ".join(str(value) for value in item))
                    else:
                        bullets.append("- " + str(item))
                title = kind.replace("_", " ").title()
                selection = result.get("summary", {}).get(
                    "test_selection", result.get("test_selection")
                )
                if kind == "untested_modified_symbols" and selection == "none_selected":
                    title += " (none selected)"
                blocks.append("## " + title + "\n\n" + "\n".join(bullets))
        blocks.append(f"Estimated compact JSON tokens: {result['serialized_tokens']}")
        return "\n\n".join(blocks)
    if result.get("flow", {}).get("schema") == "python-local-cpg-flow-compact-v1":
        return (
            "# Compact Python local flow\n\n"
            f"Estimated context tokens: {result['tokens']}\n\n"
            "```json\n"
            + json.dumps(result["flow"], ensure_ascii=False)
            + "\n```"
        )
    if "flow" in result:
        return _flow_markdown(result)
    if result.get("profile") == "python-call-impact-v2":
        return (
            f"# Call impact: `{result['anchor']}`\n\n"
            "Reachability only; not value flow. Caller `to: null` means the anchor.\n\n"
            f"Estimated context tokens: {result['tokens']}\n\n"
            "```json\n" + json.dumps(result, ensure_ascii=False, separators=(",", ":")) + "\n```"
        )
    if "sections" in result:
        blocks = [f"Estimated context tokens: {result['tokens']}"]
        omission_blocks = []
        if "omitted" in result:
            omission_blocks.extend(
                f"Omitted **{name.replace('_', ' ')}** records: {count}"
                for name, count in result["omitted"].items()
            )
            omission_blocks.append(result["retrieve_details"])
        names = list(result["sections"])
        if "target_implementations" in names:
            names = ["target_implementations"] + [name for name in names if name != "target_implementations"]
        else:
            blocks.extend(omission_blocks)
        for name in names:
            items = result["sections"][name]
            blocks.append("## " + name.replace("_", " "))
            for item in items:
                blocks.append(_map_markdown_item(item))
            if name == "target_implementations":
                blocks.extend(omission_blocks)
        return "\n\n".join(blocks)
    if "items" in result:
        mapped = {
            "tokens": result["tokens"],
            "sections": {"repository_map": result["items"]},
        }
        if "omitted" in result:
            mapped["omitted"] = result["omitted"]
            mapped["retrieve_details"] = result["retrieve_details"]
        return _markdown(mapped)
    return (
        "# Code graph report\n\n```json\n"
        + json.dumps(result, ensure_ascii=False, indent=2)
        + "\n```"
    )


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="codegraph-slicer",
        description=(
            "Build a static Python code graph, query bounded repository context,\n"
            "or compare two indexed snapshots. Sync once, then run map or slice;\n"
            "queries refuse stale or unverifiable source. Use --snapshot only to\n"
            "intentionally query historical cached bytes.\n"
            "Sync ROOT indexes its workspace unless --revision selects a Git\n"
            "commit; each database holds one selected snapshot. This is static\n"
            "syntax analysis, not runtime behavior or test execution. The flow\n"
            "command adds bounded intraprocedural Python data/control-flow views."
        ),
        epilog=f"Full cookbook and limitations: {Path(__file__).resolve().parent.parent / 'HELP.md'}",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="cmd", required=True)
    sync = sub.add_parser(
        "sync",
        help="index a workspace or Git commit snapshot",
        description=(
            "Index Python files from a workspace, or an immutable Git revision.\n"
            "One database stores one source snapshot; syncing another replaces it."
        ),
        epilog="Example: codegraph-slicer sync ./project --db /tmp/project.sqlite\nNext: codegraph-slicer map --db /tmp/project.sqlite",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sync.add_argument("root", help="workspace directory or Git repository")
    sync.add_argument("--db", required=True, help="SQLite database to create or replace")
    sync.add_argument("--revision", help="Git revision to index instead of workspace files (default: workspace)")
    mapping = sub.add_parser(
        "map",
        help="rank files and render a bounded repository map",
        description=(
            "Read an existing snapshot and include ranked file skeletons up to\n"
            "the estimated context budget. With no seeds, rank from the whole\n"
            "indexed repository."
        ),
        epilog="Example: codegraph-slicer map --db /tmp/project.sqlite --seed package.module\nNext: codegraph-slicer slice --db /tmp/project.sqlite package.module:run",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    mapping.add_argument("--db", required=True, help="existing indexed SQLite database")
    mapping.add_argument("--snapshot", action="store_true", help="explicitly query cached historical bytes without checking source freshness")
    mapping.add_argument("--seed", action="append", default=[], help="module or source path to rank from; repeatable (default: whole repository)")
    mapping.add_argument("--max-tokens", type=int, default=2000, help="estimated context budget (default: 2000)")
    mapping.add_argument("--outline", action="store_true", help="emit versioned per-entity JSON outline")
    slicing = sub.add_parser(
        "slice",
        help="render implementations and related context for anchors",
        description=(
            "Read an existing snapshot and render required definitions plus\n"
            "related types, callers, and optional neighbors within the estimated\n"
            "context budget. Anchors may be symbol IDs, package.module:,\n"
            "path.py:qualname, or path.py:line."
        ),
        epilog="Example: codegraph-slicer slice --db /tmp/project.sqlite package.module:run package.module:helper\nNext: inspect the JSON, or add --format markdown or --format xml for readable context.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    slicing.add_argument("--db", required=True, help="existing indexed SQLite database")
    slicing.add_argument("--snapshot", action="store_true", help="explicitly query cached historical bytes without checking source freshness")
    slicing.add_argument("anchors", nargs="+", help="symbol, module, or path/line anchor")
    slicing.add_argument("--max-tokens", type=int, default=8000, help="estimated context budget (default: 8000)")
    slicing.add_argument("--outline", action="store_true", help="emit a versioned, body-free entity graph")
    slicing.add_argument("--include-imports", action="store_true", help="include module and import hops in an outline")
    slicing.add_argument("--include-callee-bodies", action="store_true", help="include optional callee implementations (default: disabled)")
    impact = sub.add_parser(
        "impact",
        help="find bounded transitive callers over resolved call edges",
        description=(
            "Walk resolved CALLS edges backward from one function or method. This reports static "
            "reachability, not value flow or proof of runtime execution. Unresolved call references "
            "across the indexed snapshot are returned as potential unknown boundaries, not linked "
            "to the selected anchor."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    impact.add_argument("anchor", help="observed function or method anchor")
    impact.add_argument("--db", required=True, help="existing indexed SQLite snapshot, opened read-only")
    impact.add_argument("--snapshot", action="store_true", help="query cached historical bytes without checking source freshness")
    impact.add_argument("--max-depth", type=int, default=3, help="maximum number of caller hops (default: 3)")
    impact.add_argument("--max-nodes", type=int, default=200, help="maximum returned caller nodes (default: 200)")
    impact.add_argument(
        "--unknown-boundaries", type=int, default=0,
        help="include up to N unresolved CALLS records (default: count only)",
    )
    impact.add_argument("--max-tokens", type=int, default=8000, help="maximum printed output estimate (default: 8000)")
    flow = sub.add_parser(
        "flow", help="query bounded local Python control/data flow",
        description=("Build a function-local flow graph on demand from cached module source.\n"
                     "No indexed code is imported or executed. Variable and line selections\n"
                     "slice dependencies; omitted criteria return the complete function graph.\n"
                     "Supports assignments, expressions, branches, loops and exits. Calls\n"
                     "have opaque effects; try/with, async, generators, comprehensions,\n"
                     "nested definitions and imports refuse with a source location."),
        epilog=("Example: codegraph-slicer flow spend:admit --db graph.sqlite --variable total --line 15\n"
                "Next: inspect the source identity and diagnostics; sync the index after source changes."),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    flow.add_argument("anchor", help="observed function/method anchor (module/class anchors are refused)")
    flow.add_argument("--db", required=True, help="existing indexed SQLite snapshot, opened read-only")
    flow.add_argument("--snapshot", action="store_true", help="explicitly query cached historical bytes without checking source freshness")
    flow.add_argument("--variable", help="local name or access path to seed")
    flow.add_argument("--line", type=int, help="source line to seed")
    flow.add_argument("--direction", choices=("backward", "forward"), default="backward", help="dependency traversal direction (default: backward)")
    flow.add_argument("--data-only", action="store_true", help="exclude control-dependence edges")
    flow.add_argument("--compact", action="store_true", help="emit versioned columnar flow JSON; full JSON is the recovery path")
    flow.add_argument("--max-tokens", type=int, default=8000, help="estimated printed flow output budget (default: 8000)")
    delta = sub.add_parser(
        "delta",
        help="compare indexed base and head snapshots",
        description=(
            "Compare independently indexed snapshots for static graph, import,\n"
            "call, complexity, and test-selection changes. Selected tests are\n"
            "static evidence only; this command never executes tests. By default,\n"
            "emit a priority-ranked summary within --max-tokens. Use --full to\n"
            "request all details under the same hard budget."
        ),
        epilog=(
            "Example: codegraph-slicer delta --base-db /tmp/base.sqlite --head-db /tmp/head.sqlite --max-tokens 2400\n"
            "Retrieve all detail with --full --max-tokens N. Next: review reported changes and run selected tests separately."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    delta.add_argument("--base-db", required=True, help="existing base snapshot database")
    delta.add_argument("--head-db", required=True, help="existing head snapshot database")
    delta.add_argument(
        "--forbid-import",
        nargs=2,
        action="append",
        default=[],
        metavar=("SOURCE", "TARGET"),
        help="report imports from SOURCE to TARGET prefixes; repeatable (default: none)",
    )
    delta.add_argument("--selected-test", action="append", default=[], help="select a test path for static gap analysis; repeatable, does not run it (default: none)")
    delta.add_argument("--projection", choices=("legacy", "v2"), help="output schema; v2 adds filters and bounded edge samples while preserving filtered totals (default: legacy)")
    delta.add_argument("--anchor", action="append", default=[], help="v2 projection: retain records mentioning this exact symbol or its qualified descendants; repeatable")
    delta.add_argument("--kind", action="append", default=[], help="v2 projection: retain this report category or edge kind (for example CALLS or IMPORTS); repeatable")
    delta.add_argument("--edge-sample-limit", type=int, help="v2 projection: maximum added/removed edge samples per direction (minimum: 1; default: 20)")
    delta.add_argument("--max-tokens", type=int, default=8000, help="estimated compact JSON budget for the delta report (default: 8000)")
    delta.add_argument("--full", action="store_true", help="include the complete raw delta payload, refusing if it exceeds the budget")
    for command in (sync, mapping, slicing, impact, flow, delta):
        command.add_argument("--format", choices=("json", "markdown", "xml"), default="json", help="output representation (default: json)")
    args = parser.parse_args(argv)
    if args.cmd == "delta" and args.edge_sample_limit is not None and args.edge_sample_limit < 1:
        parser.error("--edge-sample-limit must be at least 1")
    if args.cmd == "slice" and args.include_imports and not args.outline:
        parser.error("--include-imports requires --outline")
    opening_db = True
    opening_db_path = None
    try:
        with ExitStack() as stack:
            if args.cmd == "delta":
                has_v2_options = bool(args.anchor) or bool(args.kind) or args.edge_sample_limit is not None
                if args.projection == "legacy" and has_v2_options:
                    parser.error("v2 filters require --projection v2")
                use_v2 = args.projection == "v2" or (
                    args.projection is None and has_v2_options
                )
                if args.full and use_v2:
                    parser.error("--full cannot be combined with the v2 projection or its filters")
                opening_db_path = str(Path(args.base_db).resolve())
                base = stack.enter_context(GraphStore(args.base_db, readonly=True))
                opening_db_path = str(Path(args.head_db).resolve())
                head = stack.enter_context(GraphStore(args.head_db, readonly=True))
                opening_db = False
                result = compute_delta(
                    base, head, args.forbid_import, selected_tests=args.selected_test,
                    _earliest_edge_lines=use_v2,
                )
                render_budget = _render_budget_estimator(args.format)
                if use_v2:
                    result = project_delta_v2(
                        result, args.max_tokens, anchors=args.anchor, kinds=args.kind,
                        edge_sample_limit=(
                            20 if args.edge_sample_limit is None else args.edge_sample_limit
                        ),
                        selected_tests=bool(args.selected_test),
                        render_estimator=render_budget,
                    )
                else:
                    result = summarize_delta(
                        result, args.max_tokens, full=args.full,
                        selected_tests=bool(args.selected_test),
                        render_estimator=render_budget,
                    )
            else:
                opening_db_path = str(Path(args.db).resolve())
                store = stack.enter_context(GraphStore(args.db, readonly=args.cmd != "sync"))
                opening_db = False
                if args.cmd == "sync":
                    result = (
                        store.sync_git_tree(args.root, args.revision)
                        if args.revision is not None
                        else store.sync_workspace(args.root)
                    )
                elif args.cmd == "map":
                    if not args.snapshot:
                        store.assert_fresh()
                    if args.outline:
                        result = build_repo_outline(
                            store, args.seed, args.max_tokens,
                            render_estimator=_render_budget_estimator(args.format, _OutlineMarkdownEstimator()),
                        )
                    else:
                        result = build_repo_map(
                            store, args.seed, args.max_tokens,
                            render_estimator=_render_budget_estimator(args.format, _MapMarkdownEstimator()),
                        )
                elif args.cmd == "flow":
                    if not args.snapshot:
                        store.assert_fresh()
                    result = query_flow(
                        store, args.anchor, variable=args.variable, line=args.line,
                        direction=args.direction, include_control=not args.data_only,
                        max_tokens=(2**63 - 1) if args.compact else args.max_tokens,
                        render_estimator=(
                            (lambda candidate: rendered_tokens(_markdown(candidate)))
                            if args.format == "markdown"
                            else (lambda candidate: rendered_tokens(_xml(candidate)))
                            if args.format == "xml"
                            else (lambda candidate: rendered_tokens(json.dumps(
                                candidate, ensure_ascii=False, separators=(",", ":")
                            )))
                        ),
                    )
                    if args.compact:
                        result = compact_flow_result(
                            result,
                            (
                                (lambda candidate: rendered_tokens(_markdown(candidate)))
                                if args.format == "markdown"
                                else (lambda candidate: rendered_tokens(_xml(candidate)))
                                if args.format == "xml"
                                else (lambda candidate: rendered_tokens(json.dumps(
                                    candidate, ensure_ascii=False, separators=(",", ":")
                                )))
                            ),
                        )
                elif args.cmd == "impact":
                    if not args.snapshot:
                        store.assert_fresh()
                    result = query_call_impact(
                        store, args.anchor,
                        max_depth=args.max_depth,
                        max_nodes=args.max_nodes,
                        unknown_boundary_limit=args.unknown_boundaries,
                        max_tokens=args.max_tokens, database=args.db, output_format=args.format,
                        render_estimator=_render_budget_estimator(args.format),
                    )
                else:
                    if not args.snapshot:
                        store.assert_fresh()
                    if args.outline:
                        result = build_slice_outline(
                            store, args.anchors, args.max_tokens,
                            include_imports=args.include_imports,
                            render_estimator=_render_budget_estimator(args.format),
                        )
                    else:
                        result = slice_context(
                            store, args.anchors, args.max_tokens, args.include_callee_bodies,
                            database=args.db,
                            render_estimator=_render_budget_estimator(args.format),
                        )
        if args.format == "markdown":
            rendered = _markdown(result)
        elif args.format == "xml":
            rendered = _xml(result)
        else:
            rendered = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        if args.cmd in ("map", "slice", "impact", "flow", "delta") and rendered_tokens(rendered) > args.max_tokens:
            from .slicer import InvalidBudgetError
            if args.cmd == "flow":
                label = "printed flow output"
            elif args.format == "markdown":
                label = "rendered Markdown"
            elif args.format == "xml":
                label = "rendered XML"
            else:
                label = "printed JSON"
            raise InvalidBudgetError(f"{label} exceeds budget {args.max_tokens}")
        print(rendered)
        return 0
    except (CodeGraphError, OSError, sqlite3.Error) as exc:
        message = f"codegraph-slicer: {type(exc).__name__}: {exc}\n"
        hints = []
        if (isinstance(exc, StaleSnapshotError)
                and str(exc).startswith("snapshot is stale: Python source differs at ")):
            hints.append(
                "hint: for symbols you did not edit, add --snapshot to read the indexed bytes; "
                "re-run sync for symbols you edited"
            )
        elif isinstance(exc, AnchorNotFoundError):
            hints.extend(f"hint: nearest anchor: {anchor}" for anchor in getattr(exc, "nearest_anchors", []))
            revision = getattr(exc, "snapshot_revision", "")
            if revision:
                hints.append(f"hint: a symbol added on a branch is absent from a base-commit DB (recorded commit: {revision}); query a snapshot containing that symbol")
        elif isinstance(exc, FlowQueryError) and getattr(exc, "observed_lines", []):
            hints.append("hint: observed def/use lines for this variable in the selected function: " + ", ".join(map(str, exc.observed_lines)))
        elif isinstance(exc, UnsupportedFlowError) and args.cmd == "flow":
            hints.append("hint: use slice for this function")
        elif (isinstance(exc, sqlite3.OperationalError) and opening_db
              and str(exc) == "unable to open database file"):
            db_path = Path(opening_db_path)
            try:
                try:
                    db_path.stat()
                    exists = True
                except (FileNotFoundError, NotADirectoryError):
                    exists = False
                try:
                    parent_mode = db_path.parent.stat().st_mode
                except (FileNotFoundError, NotADirectoryError):
                    parent_writable = False
                else:
                    parent_writable = stat.S_ISDIR(parent_mode) and os.access(db_path.parent, os.W_OK)
            except OSError:
                pass
            else:
                hints.append(f"hint: database {opening_db_path}; exists={exists}, parent_writable={parent_writable}")
        parser.exit(2, message + "".join(line + "\n" for line in hints))
