---
name: codegraph-slicer
description: >-
  Static Python code graph: map, slice, flow, delta. Use for structural questions about Python code:
  who calls X, where a value comes from, what a change touches, import cycles, forbidden imports,
  blast radius, or base-vs-head graph changes. Not for runtime behaviour or running tests.
---

# codegraph-slicer

Requires `codegraph-slicer` on `PATH` (`pip install git+https://github.com/lecholette/codegraph-slicer`) and Python 3.12+.
The indexer parses with the running interpreter, so use a Python at least as new as the indexed code.
Full cookbook: `codegraph-slicer --help` and `codegraph-slicer <command> --help`.

## Index first

Keep the index outside the repo (or in a git-ignored path). One database holds one snapshot.

```bash
GRAPH="${CODEGRAPH_DB:-$HOME/.cache/codegraph-slicer/$(basename "$PWD").sqlite}"
mkdir -p "$(dirname "$GRAPH")"
codegraph-slicer sync . --db "$GRAPH"
```

Sync is incremental: unchanged files are not reparsed, so re-run it whenever source changed.
`map`, `slice`, `impact` and `flow` exit 2 with `StaleSnapshotError` when files changed since the sync.
Re-sync for symbols you edited. For symbols you did not edit, `--snapshot` reads the indexed bytes without a re-sync.
Errors go to stderr with exit 2. An empty result is exit 0.

## Commands

- Orient: `codegraph-slicer map --db "$GRAPH" --seed package.module --format markdown` (default budget 2000; a large seed file may need `--max-tokens 8000`)
- One function with its types, callers and callees: `codegraph-slicer slice --db "$GRAPH" package.module:func --format markdown`
- Several symbols: pass every anchor to one `slice` call (`slice --db "$GRAPH" pkg.mod:a pkg.mod:b`), not a shell loop. One call shares types and callers across anchors.
- Structure only (callers, callees, types, no bodies): `slice --outline`. Outlines are not always small; keep the default budget.
- Bounded transitive callers: `codegraph-slicer impact package.module:func --db "$GRAPH" --max-depth 3 --max-nodes 200 --format markdown`
- Where does a value come from (backward line slice): `codegraph-slicer flow --db "$GRAPH" package.module:func --variable total --line 42 --format markdown`
- What a value feeds (forward data slice): `codegraph-slicer flow --db "$GRAPH" package.module:func --variable x --direction forward --data-only --format markdown`
- Branch vs base: sync each revision into its own database, then diff them:

  ```bash
  codegraph-slicer sync . --revision origin/main --db /tmp/base.sqlite
  codegraph-slicer sync . --db /tmp/head.sqlite
  codegraph-slicer delta --base-db /tmp/base.sqlite --head-db /tmp/head.sqlite --full --format json > /tmp/delta.json
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print({k: len(v) for k, v in d.items() if isinstance(v, list)})' /tmp/delta.json
  ```

  `--full` requests raw delta details. Print counts first; JSON can be large, so inspect only the entries you need.
  Add `--forbid-import SRC_PREFIX TGT_PREFIX` for layer rules.

Anchors: `pkg.mod:qualname`, `pkg.mod:` (whole module), `path/file.py:qualname` or `path/file.py:LINE`.
`flow` takes only function or method anchors.

## Limits (state them when you cite output)

- Output is static evidence, not a verdict. Unresolved references do not prove absence; confirm with `grep`.
- `impact` follows resolved `CALLS` edges backward with explicit depth and caller-node bounds. Its witnesses show call reachability, not value flow or runtime execution. It reports unresolved `CALLS` references across the indexed snapshot as potential unknown boundaries, not linked to the selected anchor; excess boundary records are counted and a truncated walk is not a complete impact set.
- `flow` is function-local. Calls are opaque. Aliases and implicit exceptions are not modelled.
- `flow` models `try`/`except`/`finally`, `with`, `async with` and comprehensions conservatively. It refuses `await`, `yield`, lambdas, nested definitions and imports with a source location. Use `slice` for those.
- `delta` never runs tests. Run them separately.
- Do not pick a budget below the default up front. If a command refuses for size, rerun once with `--max-tokens` at the N the error prints.
- Errors print `hint:` lines (nearest anchors, snapshot commit). Use them before retrying.
