---
name: codegraph-slicer
description: >-
  Static Python, Java, TypeScript, and JavaScript maps, slices, impact, local
  Python flow, and graph deltas. Use for explicit structural questions or
  selective navigation across larger or dense source trees. Compact feature
  work normally uses direct reads or one map, without slicing or delta.
---

# codegraph-slicer

Requires Python 3.12+ and `codegraph-slicer` on PATH. This workflow requires
`map --recommend-workflow`; check `codegraph-slicer map --help` if the installed
version is uncertain. Never retry an unsupported flag repeatedly.

Supported sources: `.py`, `.java`, `.ts`, `.tsx`, `.js`, `.jsx`, `.mjs`, `.cjs`.
Use `codegraph-slicer <command> --help` for the command contract.

## Choose a workflow before indexing

These rules take precedence over every command example below.

1. If the user explicitly requests a structural map, caller/impact analysis,
   local flow, import rule check, or base-vs-head graph delta, perform that
   analysis regardless of language or repository size. State its limits.
2. For ordinary implementation work, inspect a cheap source inventory first
   (`rg --files`, then line counts for a small source set when needed). If the
   target is a known local file/region, read it directly. Do not index merely
   to confirm that a known small task is small.
3. A scope is compact when its existing non-test supported source has BOTH
   fewer than 30 files AND fewer than 5,000 LOC. A Python-only scope is also
   compact with BOTH fewer than 50 files AND fewer than 7,500 LOC. Exclude
   tests and known generated/vendor/build output from the manual task scope;
   the CLI advisory counts indexed non-test source and may include such files.
4. For compact implementation work, read known target files directly. If
   targets are unknown and a map would help, use at most one ordinary map,
   then source reads and targeted search. Do not automatically run `slice`,
   `impact`, `flow`, `delta`, or a completion-time sync in this lane. Do not
   request `map --outline` as a cheap substitute: it includes graph entities
   and edges and can be much larger than the ordinary map.
5. If more than 30% of existing production files in the established task scope
   will change, prefer reading that edit surface directly. Count distinct
   existing files against the same scope; count new files separately. This
   does not forbid a specific cross-package structural query needed by the task.
6. Larger/dense scopes are graph-eligible, not graph-mandatory. Use the graph
   when relationships or active implementations are unknown and it can avoid
   substantial unrelated reading. A 300-line file or 15 definitions alone
   does not override compactness or require slicing.
7. In mixed monorepos, use a known service/package as the task scope when
   appropriate. A global map advisory describes indexed source, not the task's
   edit surface. A map seed changes ranking, not the advisory's scope. Do not
   create a new index per package just to force a compact classification.

## Optional orientation and index lifecycle

Keep databases outside the repository or in a git-ignored `.codegraph/`.
Reuse a current database when available. Only when a graph query is needed:

```bash
GRAPH="${CODEGRAPH_DB:-$(mktemp -d)/graph.sqlite}"
codegraph-slicer sync . --db "$GRAPH"
codegraph-slicer map --db "$GRAPH" --recommend-workflow --format markdown
```

The default map budget is 2,000 estimated tokens. Read `ScaleRecommendation`
and omission metadata. `bypass_slicer: true` means use the compact read workflow;
`graph_optional` does not instruct you to run every graph command. Empty or
incomplete indexes require direct inspection and do not prove the repo is small.
A complete map is not guaranteed to fit in 3,500 tokens, even for ten source files.

For compact work, a partial map is orientation, not a reason for repeated map
expansion. Read/search omitted relevant paths directly. If the opted-in map
cannot fit even its metadata, fall back to direct reads instead of retry loops.
With an older CLI lacking the flag, apply the scale rules manually and use at
most one ordinary map if needed; do not reinstall tools during a coding task.

Source changed? Re-sync only before a further graph query that needs current
bytes. Fresh queries reject stale snapshots. `--snapshot` reads indexed bytes
without freshness verification; use it only deliberately for historical or
known-unchanged context, never as evidence of current edited behavior.

## Selective graph queries for eligible work

- Orient with a map, optionally `--seed package.module` to focus ranking.
- Batch the anchors you actually need in one call:

  ```bash
  codegraph-slicer slice --db "$GRAPH" --format xml \
    path/file.py:Class.method path/other.py:helper
  ```

- Prefer method/function anchors to giant class anchors. Default slice budget
  is 8,000 estimated tokens. Do not manufacture 3–8 anchors just to fill a batch.
- If direct target reads are already planned but relationships are needed,
  consider `--target-mode skeleton`. Avoid loading a full body in both slice
  and Read. If Edit requires Read, use a bounded fresh native read where safe;
  do not assume a slice satisfies that tool's read requirement.
- Condensed or omitted bodies are not complete source. Read the relevant source
  before changing behavior whose implementation was omitted. Expand context
  when surrounding invariants require it; do not obey a blanket ban on full reads.
- Use `slice --outline` for structure-only questions; it is still budgeted.
- Use `impact ANCHOR --db "$GRAPH" --max-depth 3 --max-nodes 200 --format markdown`
  for bounded transitive callers when that relationship is the question.
- Use `flow --db "$GRAPH" ANCHOR --variable NAME --line LINE --format markdown`
  for backward Python function-local flow; add `--direction forward --data-only`
  for forward data dependence. It is a separate analysis, not routine completion.
- Batch related retrieval, but do not assume tool output is permanently retained
  in a model cache. Session deduplication is optional, not a correctness guarantee.

## Contracts and completion

Check helper return shapes, side effects, output ordering, early-return guards,
imports, and state transitions before reuse. Preserve existing contracts except
where the specification deliberately changes them. Run applicable tests and
independent checks for new boundaries, error cases, and state transitions.
Report passed, failed, skipped, and blocked cases separately. Static graph
success never substitutes for executing the relevant behavior.

For ordinary implementation work, review source changes and `git diff --check`.
Do not run delta as a ritual final step. Run it only for an explicit or necessary
base-vs-head dependency, cycle, or layer-policy question. When it is needed:

```bash
codegraph-slicer sync . --revision origin/main --db /tmp/base.sqlite
cp /tmp/base.sqlite /tmp/head.sqlite
codegraph-slicer sync . --db /tmp/head.sqlite
codegraph-slicer delta --base-db /tmp/base.sqlite --head-db /tmp/head.sqlite
```

Use task-unique paths in real sessions to avoid collisions. Reuse a valid base
snapshot instead of rebuilding it. Delta is static and never runs tests.

## Limits

- Unresolved references do not prove absence. Confirm relevant unknown boundaries
  with source search; aggregate diagnostics are not a language confidence score.
- `impact` follows resolved CALLS edges and has depth/node limits. Its snapshot-wide
  unknown boundaries are not automatically linked to the selected anchor.
- `flow` is Python function-local, calls are opaque, and unsupported constructs
  may be refused. It does not establish runtime execution or cross-call value flow.
- A bounded map/slice can omit files, entities, edges, or bodies. Inspect omission
  metadata and never label a partial view complete.
- Anchors include `pkg.mod:qualname`, `path/file.py:Class.method`, analogous
  Java/TS/JS anchors, and `path/file:LINE`. Use hints from errors before retrying.
- For an explicitly needed graph query that exceeds budget, retry once at the
  suggested budget if useful. Compact implementation work falls back to reads.
