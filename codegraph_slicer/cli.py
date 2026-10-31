"""Standalone commands; query commands open existing databases read-only."""

import argparse
from contextlib import ExitStack
import json
from collections import Counter
from pathlib import Path
import re
import sqlite3
from .ast_extractor import CodeGraphError
from .store import GraphStore
from .pagerank import build_repo_map
from .slicer import slice_context, serialized_tokens
from .delta import compute_delta, summarize_delta
from .flow_query import query_flow


_DEPENDENCIES = (("DATA_DEP", "data ←"), ("CONTROL_DEP", "control ←"))
_ENDPOINTS = ("entry", "exit")


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
        text = node["text"].splitlines() or [""]
        lines = [f"- {ref[node['id']]} {node['kind']}{span}{flags}: `{text[0].strip()}`" if len(text) == 1
                 else f"- {ref[node['id']]} {node['kind']}{span}{flags}:\n" + "\n".join(f"      {t}" for t in text)]
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
    return text.replace("this view ≈ 0 tokens", f"this view ≈ {len(text) // 4 + 1} tokens", 1)


def _markdown(result):
    if "summary" in result or "modified_symbols" in result and "serialized_tokens" in result:
        blocks = ["# Static delta report"]
        if "summary" in result:
            blocks.append("## Summary\n\n" + "\n".join(
                f"- **{name.replace('_', ' ')}:** {count}"
                for name, count in result["summary"]["counts"].items()
            ))
            blocks.append(
                f"Test selection requested: `{result['summary']['test_selection']}`. "
                f"Static test gap statuses: {result['summary']['test_gap_status_counts']}.\n\n"
                + result["summary"]["test_selection_note"] + "\n\n"
                + result["summary"]["test_gap_note"] + "\n\n"
                + result["summary"]["retrieve_details"]
            )
        for kind, count in result.get("omitted", {}).items():
            blocks.append(f"Omitted **{kind.replace('_', ' ')}** records: {count}")
        for kind in ("boundary_violations", "new_cycles", "complexity_drift", "import_changes", "modified_symbols", "affected_entrypoints", "untested_modified_symbols", "test_coverage", "added_edges", "removed_edges"):
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
                blocks.append("## " + kind.replace("_", " ").title() + "\n\n" + "\n".join(bullets))
        blocks.append(f"Estimated compact JSON tokens: {result['serialized_tokens']}")
        return "\n\n".join(blocks)
    if "flow" in result:
        return _flow_markdown(result)
    if "sections" in result:
        blocks = [f"Estimated context tokens: {result['tokens']}"]
        for name, items in result["sections"].items():
            blocks.append("## " + name.replace("_", " "))
            for item in items:
                fence = "`" * max(
                    3,
                    1
                    + max(
                        (len(run) for run in re.findall(r"`+", item["text"])), default=0
                    ),
                )
                blocks.append(
                    f"### {item['symbol']}\n\n{fence}python\n{item['text']}\n{fence}"
                )
        return "\n\n".join(blocks)
    if "items" in result:
        return _markdown(
            {
                "tokens": result["tokens"],
                "sections": {"repository_map": result["items"]},
            }
        )
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
    try:
        with ExitStack() as stack:
            if args.cmd == "delta":
                base = stack.enter_context(GraphStore(args.base_db, readonly=True))
                head = stack.enter_context(GraphStore(args.head_db, readonly=True))
                result = compute_delta(
                    base, head, args.forbid_import, selected_tests=args.selected_test
                )
                result = summarize_delta(
                    result, args.max_tokens, full=args.full,
                    selected_tests=bool(args.selected_test),
                    render_estimator=(
                        lambda candidate: serialized_tokens(_markdown(candidate))
                    ) if args.format == "markdown" else None,
                )
            else:
                store = stack.enter_context(GraphStore(args.db, readonly=args.cmd != "sync"))
                if args.cmd == "sync":
                    result = (
                        store.sync_git_tree(args.root, args.revision)
                        if args.revision is not None
                        else store.sync_workspace(args.root)
                    )
                elif args.cmd == "map":
                    if not args.snapshot:
                        store.assert_fresh()
                    result = build_repo_map(store, args.seed, args.max_tokens)
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
                        store, args.anchors, args.max_tokens, args.include_callee_bodies
                    )
        rendered = _markdown(result) if args.format == "markdown" else json.dumps(
            result, ensure_ascii=False, separators=(",", ":")
        )
        if args.cmd == "delta" and args.format == "markdown":
            from .slicer import InvalidBudgetError
            if serialized_tokens(rendered) > args.max_tokens:
                raise InvalidBudgetError(f"rendered Markdown exceeds budget {args.max_tokens}")
        print(rendered)
        return 0
    except (CodeGraphError, OSError, sqlite3.Error) as exc:
        parser.exit(2, f"codegraph-slicer: {type(exc).__name__}: {exc}\n")
