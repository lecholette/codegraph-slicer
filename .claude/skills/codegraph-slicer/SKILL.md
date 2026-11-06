---
name: codegraph-slicer
description: >-
  Static Python, Java, TypeScript, and JavaScript code graph: map, slice, flow, delta. Use for structural questions
  about Python (.py), Java (.java), TypeScript (.ts, .tsx), or JavaScript (.js, .jsx, .mjs, .cjs) code: who calls X,
  where a value comes from, what a change touches, import cycles, forbidden imports, blast radius, or base-vs-head
  graph changes. Not for runtime behaviour or running tests.
---

# codegraph-slicer

Requires `codegraph-slicer` on `PATH` (`pip install git+https://github.com/lecholette/codegraph-slicer`) and Python 3.12+.
Indexes Python (`.py`), Java (`.java`), TypeScript (`.ts`, `.tsx`), and JavaScript (`.js`, `.jsx`, `.mjs`, `.cjs`) source trees.
Full cookbook: `codegraph-slicer --help` and `codegraph-slicer <command> --help`.

## Routing Precedence (When to Slice vs. Read Directly)

1. **Use direct `Read` (skipping `codegraph-slicer`)** when the repository is tiny (`< 500` total LOC), when the language is outside Python/Java/TypeScript/JavaScript, or when all target files are already known **and** every target file is compact (**`< 300` lines and `< 15` definitions each**). On compact one-class-per-file modules (`< 300` lines and `< 15` definitions) that you will immediately edit, reading the file directly avoids loading both the XML slice and the full file into context.
2. **Otherwise (repositories `>= 500` total LOC in Python/Java/TypeScript/JavaScript), use `codegraph-slicer` (`map`, `slice`, or `impact`)** whenever target symbols live inside large files (**`>= 300` lines** or **`>= 15` definitions**) **or** when callers, callees, and interface implementations are unknown across multiple modules/packages. Pass target `path/file.ext:Class.method` anchors in a single batched `codegraph-slicer slice --snapshot --db "$GRAPH" --max-tokens 8000 --format xml <anchors...>` call before reading large source files.
3. **Incremental head sync (`delta`):** When running `delta --base-db /tmp/base.sqlite --head-db /tmp/head.sqlite`, copy `cp "$GRAPH" /tmp/head.sqlite` before `codegraph-slicer sync . --db /tmp/head.sqlite` so only modified files are reparsed.

## Contract & Verification Discipline

- **Check helper & scaffold contracts before reuse:** Inspect whether existing helpers preserve required return shapes, formatting, side effects, observable event/output ordering, and state transitions. Preserve existing early-return guards and state fields in `<target>` methods required by earlier callers.
- **Expand stubbed or omitted context when needed:** When a signature-only callee stub in `<downstream_dependencies>` or an omitted symbol affects formatting, ordering, or state transitions, fetch its body (`slice --include-callee-bodies` or targeted `slice`/`Read`) rather than guessing its implementation.
- **Requirement-driven verification:** Run available tests. Where acceptance tests are external or legacy tests reflect superseded contracts, run focused checks derived independently from the specification (covering boundary transitions, exact output/event sequences, and error/rejection cases) before completing.

## Index first

Keep the index outside the repo (or in a git-ignored dot-directory such as `.codegraph/`). Hidden dot-directories are automatically ignored during workspace sync. One database holds one snapshot.

```bash
GRAPH="${CODEGRAPH_DB:-$(mktemp -d)/graph.sqlite}"
codegraph-slicer sync . --db "$GRAPH"
```

Sync is incremental: unchanged files are not reparsed, so re-run it whenever source changed.
`map`, `slice`, `impact` and `flow` exit 2 with `StaleSnapshotError` when files changed since the sync.
Re-sync for symbols you edited. For symbols you did not edit, `--snapshot` (a boolean flag taking no argument) reads the indexed bytes without a re-sync.
Errors go to stderr with exit 2. An empty result is exit 0.

## Commands

- **In repositories `>= 500` total LOC, do not browse or read entire large module files (`>= 300` lines or `>= 15` definitions)** when sliced anchors are known. Query anchors directly with `codegraph-slicer slice ... --format xml` for bounded, token-efficient method context (including enclosing class field/constructor preambles and same-file helper bodies `<= 40` lines).
- **Skip codegraph** when all target files are already known and `< 300` lines and `< 15` definitions each, for tiny repositories (`< 500` total LOC), or for languages outside Python/Java/TypeScript/JavaScript.
- Orient: `codegraph-slicer map --db "$GRAPH" --seed package.module --format markdown` (default budget 2000; a large seed file may need `--max-tokens 8000`)
- One function/method with its types, production callers and callees: `codegraph-slicer slice --db "$GRAPH" package.module:func --format xml` (use `--outline` or `impact` to include test callers)
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

  Default `delta` JSON bounds each summary list to at most 5 items (`omitted` reports any truncated counts) so output stays compact. Pass `--full` only when you need raw unfiltered delta arrays.
  Add `--forbid-import SRC_PREFIX TGT_PREFIX` for layer rules.

Anchors: `pkg.mod:qualname`, `pkg.mod:` (whole module), `path/file.py:qualname`, `path/File.java:Class.method`, `path/file.ts:Class.method`, `path/file.js:Class.method`, or `path/file:LINE`.
`flow` takes only Python (`.py`) function or method anchors.

## Limits (state them when you cite output)

- Output is static evidence, not a verdict. Unresolved references do not prove absence; confirm with `grep`.
- `impact` follows resolved `CALLS` edges backward with explicit depth and caller-node bounds. Its witnesses show call reachability, not value flow or runtime execution. It reports unresolved `CALLS` references across the indexed snapshot as potential unknown boundaries, not linked to the selected anchor; excess boundary records are counted and a truncated walk is not a complete impact set.
- `flow` is function-local. Calls are opaque. Aliases and implicit exceptions are not modelled.
- `flow` models `try`/`except`/`finally`, `with`, `async with` and comprehensions conservatively. It refuses `await`, `yield`, lambdas, nested definitions and imports with a source location. Use `slice` for those.
- `delta` never runs tests. Run them separately.
- Do not pick a budget below the default up front. If a command refuses for size, rerun once with `--max-tokens` at the N the error prints.
- Errors print `hint:` lines (nearest anchors, snapshot commit). Use them before retrying.
