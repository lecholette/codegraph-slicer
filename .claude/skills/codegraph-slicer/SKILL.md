---
name: codegraph-slicer
description: >-
  Static Python code graph: map, slice, flow, delta. Use for structural questions about Python code:
  who calls X, where a value comes from, what a change touches, import cycles, forbidden imports,
  blast radius, or base-vs-head graph changes. Not for runtime behaviour or running tests.
---

# codegraph-slicer

Requires `codegraph-slicer` on `PATH` (`pip install git+https://github.com/lecholette/codegraph-slicer`).
Full cookbook: `codegraph-slicer --help` and `codegraph-slicer <command> --help`.

## Index first

Keep the index outside the repo (or in a git-ignored path). One database holds one snapshot.

```bash
GRAPH="${CODEGRAPH_DB:-$HOME/.cache/codegraph-slicer/$(basename "$PWD").sqlite}"
mkdir -p "$(dirname "$GRAPH")"
codegraph-slicer sync . --db "$GRAPH"
```

Sync is incremental: unchanged files are not reparsed, so re-run it whenever source changed.
`map`, `slice` and `flow` exit 2 with `StaleSnapshotError` when files changed since the sync; re-sync then.
Errors go to stderr with exit 2. An empty result is exit 0.

## Commands

- Orient: `codegraph-slicer map --db "$GRAPH" --seed package.module --max-tokens 1500 --format markdown`
- One function with its types, callers and callees: `codegraph-slicer slice --db "$GRAPH" package.module:func --format markdown`
- Where does a value come from (backward line slice): `codegraph-slicer flow --db "$GRAPH" package.module:func --variable total --line 42 --format markdown`
- What a value feeds (forward data slice): `codegraph-slicer flow --db "$GRAPH" package.module:func --variable x --direction forward --data-only --format markdown`
- Branch vs base: sync each revision into its own database, then diff them:

  ```bash
  codegraph-slicer sync . --revision origin/main --db /tmp/base.sqlite
  codegraph-slicer sync . --db /tmp/head.sqlite
  codegraph-slicer delta --base-db /tmp/base.sqlite --head-db /tmp/head.sqlite --format json > /tmp/delta.json
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); print({k: len(v) for k, v in d.items() if isinstance(v, list)})' /tmp/delta.json
  ```

  Print the counts first. `delta` JSON can be large; inspect only the entries you need.
  Add `--forbid-import SRC_PREFIX TGT_PREFIX` for layer rules.

Anchors: `pkg.mod:qualname`, `pkg.mod:` (whole module), `path/file.py:qualname` or `path/file.py:LINE`.
`flow` takes only function or method anchors.

## Limits (state them when you cite output)

- Output is static evidence, not a verdict. Unresolved references do not prove absence; confirm with `grep`.
- `flow` is function-local. Calls are opaque. Aliases and implicit exceptions are not modelled.
- `flow` refuses `try`, `with`, async, generators, comprehensions, nested definitions and imports. Use `slice` for those.
- `delta` never runs tests. Run them separately.
- Raise `--max-tokens` if `map`, `slice` or `flow` refuses for size.
