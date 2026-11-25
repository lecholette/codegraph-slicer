---
name: codegraph-slicer
description: >-
  Proactively use whenever `codegraph-slicer` is installed and you are implementing features, fixing bugs,
  refactoring, debugging, or answering structural questions in any Python (.py), Java (.java), TypeScript (.ts, .tsx),
  or JavaScript (.js, .jsx, .mjs, .cjs) codebase—before reading large source files (>=300 lines) or grepping across
  packages. Static code graph (`v0.3.14`): sync, map (--recommend-workflow), route, slice (--db or zero-setup --root
  lazy mode, --contracts), impact, flow, and delta. Not for runtime behavior or running tests.
---

# codegraph-slicer

Requires `codegraph-slicer` on `PATH` and Python 3.12+. Do not run `pip install` to reinstall `codegraph-slicer`.
Indexes Python (`.py`), Java (`.java`), TypeScript (`.ts`, `.tsx`), and JavaScript (`.js`, `.jsx`, `.mjs`, `.cjs`) source trees.
Full cookbook: `codegraph-slicer --help` and `codegraph-slicer <command> --help`.

## Proactive Activation Rule (When Installed)

Whenever `codegraph-slicer` is on `PATH` (`command -v codegraph-slicer`) and your task involves implementing features, fixing bugs, refactoring, debugging, or answering structural questions in a Python (`.py`), Java (`.java`), TypeScript (`.ts`, `.tsx`), or JavaScript (`.js`, `.jsx`, `.mjs`, `.cjs`) repository, **proactively use `codegraph-slicer` at the start of the task** before reading large source files (`>= 300` lines) or running broad `grep`/`find` sweeps—even when the user does not explicitly mention `codegraph-slicer`.

## Quick Start

```bash
# 1. Index the local workspace into a temp SQLite DB (or existing .codegraph/graph.sqlite)
GRAPH="${CODEGRAPH_DB:-$(mktemp -d)/graph.sqlite}"
codegraph-slicer sync . --db "$GRAPH"

# 2. Orient & check scale/monolith routing before reading source files
codegraph-slicer map --db "$GRAPH" --recommend-workflow --format markdown

# 3. When bypass_slicer is false, front-load one batched XML slice for 3–8 target anchors
codegraph-slicer slice --snapshot --db "$GRAPH" --max-tokens 8000 --format xml \
  path/to/FileA.ext:ClassA.methodA \
  path/to/FileB.ext:ClassB

# Or on huge unindexed monorepos (no upfront sync): target-seeded lazy slice (~1-2s)
codegraph-slicer slice --root . --max-tokens 8000 --format xml \
  path/to/FileA.ext:ClassA.methodA

# Or remotely from GitHub via gh CLI (cached locally, no git clone needed)
codegraph-slicer slice --repo owner/name --ref main --no-callers --max-tokens 8000 --format xml \
  path/to/FileA.ext:ClassA.methodA
```

## Routing Precedence (When to Slice vs. Read Directly)

1. **Check Scale & Monoliths First (`map --recommend-workflow` or `route`):**
   - When `.codegraph/graph.sqlite` exists (or after `sync`), run `codegraph-slicer map --db "$GRAPH" --recommend-workflow --format markdown` (or `codegraph-slicer route --db "$GRAPH" --prompt "<task>"`) **before** running `find`, `wc -l`, or `cat`/`Read` on source files. Do not guess repository scale by counting files in a single subdirectory.
2. **Graph-Eligible / Monolith Scope (`bypass_slicer: false`, e.g. `mode: "slice_monoliths"` or `graph_optional`): use `codegraph-slicer slice` first** whenever `ScaleRecommendation` reports `bypass_slicer: false` (repositories `>= 30` files or `>= 5,000` LOC in Java/TS/JS, `>= 50` files or `>= 7,500` LOC in Python, or any repository with `slice_targets` monoliths `>= 300` lines or `>= 15` definitions) or when callers, callees, and interface implementations span multiple packages:
   - **Front-load one batched `slice` early:** Pass 3–8 target `path/file.ext:Class.method` or `path/file.ext:ClassName` anchors in a single batched `codegraph-slicer slice --snapshot --db "$GRAPH" --max-tokens 8000 --format xml <anchors...>` call so the deduplicated slice is locked into the early prompt cache prefix. Never run separate `slice` calls in a shell loop or across consecutive turns (if a follow-up `slice` is required later in the session, pass `--dedup-session` to elide already-cached types and callers).
   - **Prefer `Class.method` anchors when known, or `Class` anchors when method names are unknown:** When slicing multiple classes, pass specific methods from `map` (e.g. `path/file.ext:Service.handle`) when known; if a class's exact method names are not yet known, pass the class anchor directly (`path/file.ext:ClassName`) rather than guessing method names.
   - **Bounded `Read`-before-`Edit` (avoid the double-context tax):** Never `Read` or `cat` an entire large file (`>= 300` lines) whose methods are already in `<target_implementations>`. When `Edit` requires `Read` first (or when inspecting condensed lines), call `Read` with `offset=<start_line>` and `limit=<end_line - start_line + 1>` from `<code_slice lines="start-end">` (or pass `--target-mode skeleton` to `slice` if you plan to read the target files directly).
3. **Compact Scope & Python Segregation Gate (`bypass_slicer: true` / `mode: "map_then_read"`; skip `slice` and `delta`)** when:
   - `ScaleRecommendation` reports `bypass_slicer: true` (`< 30` files AND `< 5,000` LOC across Java/TS/JS, or `< 50` files AND `< 7,500` LOC for Python-only repositories, with no monoliths `>= 300` lines / `>= 15` definitions and `< 40` cross-file edges),
   - the task modifies **`> 30%` of the existing production source files** in the service,
   - all target files are already known and compact (**`< 300` lines and `< 15` definitions each**), or
   - the language is outside Python/Java/TypeScript/JavaScript.
   In this compact lane, read the target files directly and **do not run `slice` or post-edit `delta`**, avoiding the double-context tax of loading both XML slices and full files into context.
4. **Incremental head sync (`delta`):** Only when explicitly requested or needed for multi-package boundary checks on graph-eligible repositories, copy `cp "$GRAPH" /tmp/head.sqlite` before `codegraph-slicer sync . --db /tmp/head.sqlite` so only modified files are reparsed.

## Index first

Keep the index outside the repo (or in a git-ignored dot-directory such as `.codegraph/`). Hidden dot-directories are automatically ignored during workspace sync. One database holds one snapshot.

```bash
GRAPH="${CODEGRAPH_DB:-$(mktemp -d)/graph.sqlite}"
codegraph-slicer sync . --db "$GRAPH"
```

Sync is incremental: unchanged files are not reparsed, so re-run it whenever source changed.
For large monorepos, pass repeatable `--scope <dir>` (`codegraph-slicer sync . --scope mod-a --scope shared --db "$GRAPH"`) to index only the relevant modules while keeping repo-root paths.
If a polyglot or legacy repo fails on an unparseable file (e.g., an old Python 2 script), pass `--skip-unparseable` (`codegraph-slicer sync . --skip-unparseable --db "$GRAPH"`).
`map`, `route`, `slice --db`, `impact` and `flow` exit 2 with `StaleSnapshotError` when files changed since the sync.
Re-sync for symbols you edited. For symbols you did not edit, `--snapshot` (a boolean flag taking no argument) reads the indexed bytes without a re-sync.
Errors go to stderr with exit 2. An empty result is exit 0.

## Commands

- **In graph-eligible repositories, do not browse or read entire large module files (`>= 300` lines or `>= 15` definitions)** when sliced anchors are known. Query anchors directly with `codegraph-slicer slice ... --format xml` for bounded, token-efficient method context (including enclosing class field/constructor preambles and same-file helper bodies `<= 40` lines).
- **Skip `slice` and `delta`** for compact repositories (`< 30` files and `< 5,000` LOC, or Python `< 50` files and `< 7,500` LOC), when modifying `> 30%` of existing source files, when all target files are already known and `< 300` lines and `< 15` definitions each, or for languages outside Python/Java/TypeScript/JavaScript.
- Orient: `codegraph-slicer map --db "$GRAPH" --recommend-workflow --seed package.module --format markdown` (default budget 2000; a large seed file may need `--max-tokens 8000`)
- Plan workflow & localize anchors (`<25` ms): `codegraph-slicer route --db "$GRAPH" --prompt "<task>" --format json`
- One function/method with its types, production callers and callees: `codegraph-slicer slice --db "$GRAPH" package.module:func --format xml` (use `--outline` or `impact` to include test callers)
- Lazy target-seeded slice without upfront `sync` (ideal for 10k+ file monorepos or remote GitHub repos when you already have a path/symbol anchor): `codegraph-slicer slice --root . path/to/File.java:Class.method --format xml` (or `--root . --ref <sha>` to read exact Git commit objects, or `--repo owner/name --ref main --no-callers` via `gh` CLI; controlled by `--source-mode {auto,local,remote}`, `--cache-dir`, `--no-cache`, `--refresh-cache`, and bounded by `--max-files 40`, `--max-hops 2`, `--no-callers`).
- Several symbols: pass every anchor to one `slice` call (`slice --db "$GRAPH" pkg.mod:a pkg.mod:b --format xml`), not a shell loop. One call deduplicates enclosing classes, shared types, and callers across anchors.
- Structure only (callers, callees, types, no bodies): `slice --outline`. Outlines are not always small; keep the default budget.
- Bounded transitive callers: `codegraph-slicer impact package.module:func --db "$GRAPH" --max-depth 3 --max-nodes 200 --format markdown` (or `--format xml`)
- Where does a value come from (backward line slice): `codegraph-slicer flow --db "$GRAPH" package.module:func --variable total --line 42 --format markdown`
- What a value feeds (forward data slice): `codegraph-slicer flow --db "$GRAPH" package.module:func --variable x --direction forward --data-only --format markdown`
- Branch vs base: sync each revision into its own database, then diff them:

  ```bash
  codegraph-slicer sync . --revision origin/main --db /tmp/base.sqlite
  cp /tmp/base.sqlite /tmp/head.sqlite
  codegraph-slicer sync . --db /tmp/head.sqlite
  codegraph-slicer delta --base-db /tmp/base.sqlite --head-db /tmp/head.sqlite
  ```

  Default `delta` JSON bounds non-critical summary lists (up to 5 items per symbol list and 8 for `import_changes`, while keeping `boundary_violations` and `new_cycles` uncapped within budget; `omitted` reports any truncated counts) so output stays compact. Pass `--full` only when you need raw unfiltered delta arrays.
  Add `--forbid-import SRC_PREFIX TGT_PREFIX` for layer rules.

Anchors: `pkg.mod:qualname`, `pkg.mod:` (whole module), `path/file.py:qualname`, `path/File.java:Class.method`, `path/file.ts:Class.method`, `path/file.js:Class.method`, or `path/file:LINE`.
`flow` takes only Python (`.py`) function or method anchors.

## Limits (state them when you cite output)

- Output is static evidence, not a verdict. Unresolved references do not prove absence; confirm with `grep`.
- In `slice --root` / `slice --repo` (lazy mode), only seed files, resolved imports/types, and grep-matched candidate callers within `--max-files` / `--max-hops` are indexed; check `coverage` (`caller_discovery`, `tree_truncated`, `grep_note`) in the output and never treat unexplored callers as absent. Remote `--repo` mode uses `gh` against `github.com` with SHA-1-verified `BlobCache` and skips `gh search code` on pinned non-default refs (since GitHub code search only indexes the default branch); prefer explicit `file:symbol` or `module:symbol` anchors with `--no-callers`.
- `impact` follows resolved `CALLS` edges backward with explicit depth and caller-node bounds. Its witnesses show call reachability, not value flow or runtime execution. It reports unresolved `CALLS` references across the indexed snapshot as potential unknown boundaries, not linked to the selected anchor; excess boundary records are counted and a truncated walk is not a complete impact set.
- `flow` is function-local. Calls are opaque. Aliases and implicit exceptions are not modelled.
- `flow` models `try`/`except`/`finally`, `with`, `async with` and comprehensions conservatively. It refuses `await`, `yield`, lambdas, nested definitions and imports with a source location. Use `slice` for those.
- `delta` never runs tests. Run them separately.
- When `slice` or `impact` shows a multi-layer filter (e.g., DB/ES prefilter + Java/Python post-filter), verify edge-case parity (null/missing fields, timezone offsets, caps/truncation) between both layers—static graphs show call edges, not filter-predicate equivalence.
- Do not pick a budget below the default up front. If a command refuses for size (`RequiredAnchorOverflow`), copy the `rerun:` command it prints on stderr (which sets `--max-tokens` to the needed budget) and run it once; a multi-anchor `--outline` (3+ anchors) or a single-anchor `--outline` on a very large function may exceed 8000 tokens.
- Errors print `hint:` lines (`did you mean`, `nearest anchor`, `no symbol named ... in any module`, `rerun`, snapshot commit, DB path state). Use them before retrying: copy `hint: did you mean:` or the first matching `hint: nearest anchor:` verbatim; if `no symbol named ... in any module` appears, run `map --seed <module>` to list the module's real symbols rather than guessing names.
- `sync` takes the repository root as a positional (`codegraph-slicer sync . --db "$GRAPH"`), not a subdirectory (syncing a subdirectory fails with `relative import escapes package`). Run `sync` in the foreground (`codegraph-slicer sync ... && codegraph-slicer slice ...`) rather than backgrounding with `&` and querying before `sync` exits `0`.
