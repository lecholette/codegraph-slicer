"""Standalone commands; query commands open existing databases read-only."""

import argparse
from contextlib import ExitStack
import json
from collections import Counter
from pathlib import Path
import re
import sqlite3
import os
import stat
from .ast_extractor import CodeGraphError
from .store import GraphStore
from .pagerank import build_repo_map
from .slicer import (AnchorNotFoundError, slice_context,
                     rendered_tokens, tokens_for_length)
from .delta import compute_delta, summarize_delta
from .flow_query import query_flow
from .flow import FlowQueryError, UnsupportedFlowError


_DEPENDENCIES = (("DATA_DEP", "data ←"), ("CONTROL_DEP", "control ←"))
_ENDPOINTS = ("entry", "exit")


def _map_markdown_item(item):
    fence = "`" * max(
        3,
        1 + max((len(run) for run in re.findall(r"`+", item["text"])), default=0),
    )
    return f"### {item['symbol']}\n\n{fence}python\n{item['text']}\n{fence}"


class _MapMarkdownEstimator:
    """Additive exact character accounting for the CLI map Markdown shape."""

    def item_cost(self, item):
        return len(_map_markdown_item(item))

    def __call__(self, payload):
        return rendered_tokens(_markdown(payload))

    def estimate_map(self, item_cost, item_count, omitted):
        fixed = ["Estimated context tokens: 0"]
        if omitted:
            fixed.extend(
                [f"Omitted **files** records: {omitted}", "rerun with --max-tokens N"]
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
              f"Graph JSON ≈ {result['tokens']} tokens; this view ≈ 0 tokens · profile `{graph['profile']}` · Python {graph['python_version']}"]
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
    while True:
        rendered = text.replace("this view ≈ 0 tokens", f"this view ≈ {estimate} tokens", 1)
        updated = rendered_tokens(rendered)
        if updated == estimate:
            return rendered
        estimate = updated


def _markdown(result):
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
    if "flow" in result:
        return _flow_markdown(result)
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
    slicing = sub.add_parser(
        "slice",
        help="render implementations and related context for anchors",
        description=(
            "Read an existing snapshot and render required definitions plus\n"
            "related types, callers, and optional neighbors within the estimated\n"
            "context budget. Anchors may be symbol IDs, package.module:,\n"
            "path.py:qualname, or path.py:line."
        ),
        epilog="Example: codegraph-slicer slice --db /tmp/project.sqlite package.module:run --max-tokens 4000\nNext: inspect the JSON, or add --format markdown for readable context.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    slicing.add_argument("--db", required=True, help="existing indexed SQLite database")
    slicing.add_argument("--snapshot", action="store_true", help="explicitly query cached historical bytes without checking source freshness")
    slicing.add_argument("anchors", nargs="+", help="symbol, module, or path/line anchor")
    slicing.add_argument("--max-tokens", type=int, default=8000, help="estimated context budget (default: 8000)")
    slicing.add_argument("--include-callee-bodies", action="store_true", help="include optional callee implementations (default: disabled)")
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
    flow.add_argument("--max-tokens", type=int, default=8000, help="estimated complete flow JSON budget (default: 8000)")
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
    delta.add_argument("--max-tokens", type=int, default=8000, help="estimated compact JSON budget for the delta report (default: 8000)")
    delta.add_argument("--full", action="store_true", help="include the complete raw delta payload, refusing if it exceeds the budget")
    for command in (sync, mapping, slicing, flow, delta):
        command.add_argument("--format", choices=("json", "markdown"), default="json", help="output representation (default: json)")
    args = parser.parse_args(argv)
    opening_db = True
    opening_db_path = None
    try:
        with ExitStack() as stack:
            if args.cmd == "delta":
                opening_db_path = str(Path(args.base_db).resolve())
                base = stack.enter_context(GraphStore(args.base_db, readonly=True))
                opening_db_path = str(Path(args.head_db).resolve())
                head = stack.enter_context(GraphStore(args.head_db, readonly=True))
                opening_db = False
                result = compute_delta(
                    base, head, args.forbid_import, selected_tests=args.selected_test
                )
                result = summarize_delta(
                    result, args.max_tokens, full=args.full,
                    selected_tests=bool(args.selected_test),
                    render_estimator=(
                        lambda candidate: rendered_tokens(_markdown(candidate))
                    ) if args.format == "markdown" else None,
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
                    result = build_repo_map(
                        store, args.seed, args.max_tokens,
                        render_estimator=(
                            _MapMarkdownEstimator()
                        ) if args.format == "markdown" else None,
                    )
                elif args.cmd == "flow":
                    if not args.snapshot:
                        store.assert_fresh()
                    result = query_flow(
                        store, args.anchor, variable=args.variable, line=args.line,
                        direction=args.direction, include_control=not args.data_only,
                        max_tokens=args.max_tokens,
                    )
                else:
                    if not args.snapshot:
                        store.assert_fresh()
                    result = slice_context(
                        store, args.anchors, args.max_tokens, args.include_callee_bodies,
                        database=args.db,
                        render_estimator=(
                            lambda candidate: rendered_tokens(_markdown(candidate))
                        ) if args.format == "markdown" else None,
                    )
        rendered = _markdown(result) if args.format == "markdown" else json.dumps(
            result, ensure_ascii=False, separators=(",", ":")
        )
        if (args.cmd in ("map", "slice") and args.format == "markdown"
                and rendered_tokens(rendered) > args.max_tokens):
            from .slicer import InvalidBudgetError
            raise InvalidBudgetError(f"rendered Markdown exceeds budget {args.max_tokens}")
        if args.cmd == "delta":
            from .slicer import InvalidBudgetError
            if rendered_tokens(rendered) > args.max_tokens:
                label = "rendered Markdown" if args.format == "markdown" else "printed JSON"
                raise InvalidBudgetError(f"{label} exceeds budget {args.max_tokens}")
        print(rendered)
        return 0
    except (CodeGraphError, OSError, sqlite3.Error) as exc:
        message = f"codegraph-slicer: {type(exc).__name__}: {exc}\n"
        hints = []
        if isinstance(exc, AnchorNotFoundError):
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
