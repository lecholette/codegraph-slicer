# codegraph-slicer help

`codegraph-slicer` indexes Python source into a SQLite snapshot, then builds a
ranked repository map, extracts bounded context around symbols, compares two
snapshots, or builds bounded local control/data-flow slices. It is static syntax
analysis: it does not import or execute indexed code, execute tests, or infer
runtime behavior.

After `pip install`, the executable can be called by name from any working
directory. Every query uses an existing database. The first
example creates one; the later commands reuse that same index:

```sh
codegraph-slicer sync /path/to/project --db /tmp/project.sqlite
codegraph-slicer map --db /tmp/project.sqlite
codegraph-slicer slice --db /tmp/project.sqlite package.module:run
codegraph-slicer flow package.module:run --db /tmp/project.sqlite --variable total
```

To try it on the bundled example from a checkout:

```sh
codegraph-slicer sync examples --db /tmp/example.sqlite
codegraph-slicer map --db /tmp/example.sqlite --max-tokens 500
codegraph-slicer slice --db /tmp/example.sqlite spend:admit
```

`--help` on the command or a subcommand gives a short form; `README.md`
describes the index model and known static-analysis limits.

## Workflow

1. Run `sync` once to create the snapshot database.
2. Run `map` to see ranked file skeletons, or `slice` for context around one or
   more anchors.
3. To review changes, sync base and head source into separate databases and
   run `delta --base-db BASE --head-db HEAD`.
4. Inspect the report, then run any tests separately. `delta` only reports
   static test-selection gaps; `--selected-test` does not execute or certify a
   test.

## Commands

### `sync`

```text
codegraph-slicer sync ROOT --db DATABASE [--revision REVISION]
```

`ROOT` is a workspace directory or Git repository. Without `--revision`, sync
indexes the workspace's current `.py` files. With `--revision`, it reads the
specified Git commit tree instead. `DATABASE` is created or updated. A database
holds one selected source root/snapshot: syncing another source replaces the
current snapshot in that database. Keep base and head in separate database
files when comparing them. Sync is the only command that writes a database.

The parser cache is keyed by the actual `ast_extractor.py` and `store.py` bytes
plus the Python major/minor version. Missing or stale identity triggers a full
parse even if source blobs are unchanged; another sync with the same identity
and unchanged source takes the normal cache path. Sync takes an immediate
SQLite transaction before reading old hashes, parsing, and writing. A competing
writer can receive `database is locked` and exit 2 rather than mix snapshots.

Map, slice, and flow check the recorded source root before ordinary queries.
They refuse when Python source has changed, the Git HEAD moved, metadata is
missing, or freshness cannot be verified. Re-run sync to refresh. To explicitly
inspect the historical cached snapshot, pass `--snapshot` to map, slice, or
flow; this mode remains read-only and does not change the database.
Parse or SQL errors roll back both graph data and cache identity.

Sync detects the project layout; there is nothing to configure. Packages are
the top-level directories holding an `__init__.py`. A pure `src/` layout (no
top-level package and no `src/__init__.py`) is indexed by import name, so
`src/acme/core.py` is module `acme.core`; otherwise `src/` stays part of the
module name. A string constant whose first dotted part is a detected package
(for example `"acme.plugins"` in a registry) becomes an `IMPORTS` edge. The
sync result reports `source_root` and `packages`, and a layout change triggers
a full parse.

```sh
codegraph-slicer sync /path/to/project --db /tmp/project.sqlite
codegraph-slicer sync /path/to/repo --revision HEAD --db /tmp/head.sqlite
```

Next, pass the created database to `map` or `slice`.

### `map`

```text
codegraph-slicer map --db DATABASE [--seed MODULE_OR_PATH ...]
                      [--max-tokens N] [--format json|markdown]
```

`--db` must name an existing index and is opened read-only. Repeat `--seed` to
rank from one or more indexed module names or source paths; with no seeds,
ranking starts across the whole indexed repository. `--max-tokens` defaults to
2000 and is a deterministic estimate, not a model tokenizer count. The map
includes whole file skeletons in rank order while they fit. When files are
omitted, JSON and Markdown report the exact omitted count; emitted and omitted
counts reconcile to the complete map. Increase `--max-tokens` to retrieve more.
The budget includes complete
printed output and its trailing newline in either format. A budget too small
for required map metadata is an error. `--format` defaults to JSON.

```sh
codegraph-slicer map --db /tmp/project.sqlite --seed package.module --max-tokens 1800
```

Next, choose a symbol from the indexed repository and use `slice` to retrieve
its implementation and related context.

### `slice`

```text
codegraph-slicer slice --db DATABASE ANCHOR [ANCHOR ...]
                       [--max-tokens N] [--include-callee-bodies]
                       [--format json|markdown]
```

`--db` must be an existing index and is opened read-only. Supply one or more
anchors in any of these forms:

| Form | Meaning |
| --- | --- |
| `package.module:qualname` | A module-qualified symbol, such as `package.module:run` |
| `path/to/file.py:qualname` | A symbol's source path and qualified name |
| `path/to/file.py:line` | The innermost indexed definition containing that line |
| `symbol_id` | An exact symbol ID returned in index/query results |
| `package.module:` | The full source text of the module |

For example, `package.module:Outer.run` names a nested qualified symbol, and
`src/worker.py:42` selects the indexed definition containing line 42. Paths
are relative to the indexed root and use `/` separators. The selected root
controls module names: indexing a project root containing `acme/run/core.py`
gives `acme.run.core`, while indexing `acme/run` itself gives `core`. Beyond
the pure `src/` layout (see `sync`), the tool does not infer package roots.

`--max-tokens` defaults to 8000. It estimates the complete printed output as
`len(printed_output) // 4 + 1`, counting Unicode characters and the trailing
newline. JSON and Markdown each include their complete output, including
metadata. Required anchors and required type definitions must fit, as must
exact omission counts when optional records are dropped, or the command
refuses without printing a partial report. Increase `--max-tokens` to retrieve
more optional records. `--include-callee-bodies` adds optional callee
implementations instead of only their signatures; if a body does not fit, its
signature may still be returned and is counted as an emitted record.
Related class bodies whose combined size is larger than half the token budget
are summarized to keep their APIs visible; the half-budget threshold reserves
room for the selected target and other context when a set of class bodies
dominates a slice.
`--format` defaults to JSON.

When several anchors share required type definitions, those required types are
kept in the required section and are not repeated as optional callee entries.

```sh
codegraph-slicer slice --db /tmp/project.sqlite package.module:run --max-tokens 4000
codegraph-slicer slice --db /tmp/project.sqlite src/worker.py:42 --include-callee-bodies --format markdown
```

Next, inspect the returned sections; if the anchor is unknown, choose an
indexed symbol or module from the map/database.

### `delta`

```text
codegraph-slicer delta --base-db DATABASE --head-db DATABASE
                       [--forbid-import SOURCE TARGET ...]
                       [--selected-test PATH ...] [--max-tokens N] [--full]
                       [--format json|markdown]
```

Both databases must already exist and are opened read-only. They should contain
the base and head snapshots respectively. Repeat `--forbid-import SOURCE
TARGET` for each forbidden module-prefix pair. Repeat `--selected-test PATH`
to record selected test paths when analyzing static test gaps; selection is not
evidence that a test ran or passed. `--format` defaults to JSON. `--max-tokens`
defaults to 8000 and bounds the complete printed output. The default
output is a priority-ranked summary with counts for every change kind, static
test reachability and gap status, plus exact omitted-record counts. Boundaries,
new cycles, complexity drift, import changes, affected entrypoints and test gaps
rank before routine symbol details. Call/import edge lists are always omitted
from summary mode; their counts and import-change samples are retained.
`serialized_tokens` estimates the complete printed JSON, including that field
and its trailing newline. In Markdown it still reports the JSON estimate, while
the budget applies to the complete rendered Markdown including its trailing
newline. Complexity drift over its configured threshold appears before lower-risk
drift. Each emitted test-coverage sample carries its own static status.

Use `--full` to include all raw delta details. It obeys the same budget and
refuses if the complete payload cannot fit. The command also refuses if summary
counts and metadata do not fit. Markdown reports use readable headings and
lists under the same budget. Static selected or reachable tests describe graph
selection only; this command never claims tests ran or passed. It distinguishes
whether tests were requested, updated-or-selected test evidence,
reachable-but-unselected tests, and no reachable tests. A requested path that
cannot be resolved is reported as unknown and refused. Summary output includes `--full --max-tokens N`
guidance for retrieving full details.

```sh
codegraph-slicer sync /path/to/repo --revision BASE_REV --db /tmp/base.sqlite
codegraph-slicer sync /path/to/repo --revision HEAD --db /tmp/head.sqlite
codegraph-slicer delta --base-db /tmp/base.sqlite --head-db /tmp/head.sqlite \
  --forbid-import package.runtime package.execution \
  --selected-test tests/test_runtime.py --max-tokens 2400 --format markdown
codegraph-slicer delta --base-db /tmp/base.sqlite --head-db /tmp/head.sqlite \
  --full --max-tokens 8000
```

Next, review the static change report and run relevant tests separately. There
are no project-specific import boundaries unless supplied with
`--forbid-import`.

### `flow`

```text
codegraph-slicer flow ANCHOR --db DATABASE [--variable NAME_OR_ACCESS_PATH]
                      [--line LINE] [--direction backward|forward]
                      [--data-only] [--max-tokens N]
                      [--format json|markdown]
```

`flow` requires one observed function or method; module/class and ambiguous
definitions are refused. It reads full module source from the cached snapshot,
analyzes one function on demand, and includes the observed symbol ID, file path,
database blob identity, and analyzed source SHA-256. Analysis uses cached source; ordinary freshness checks still inspect the
recorded source root. Sync the database to observe later source
edits; both cold indexing and incremental updates retain ordinary snapshot
semantics. The database is opened read-only.

With neither `--variable` nor `--line`, the report contains the complete
function graph. A variable selects its observed definitions, or uses if no
definition is modeled. A line selects modeled nodes at that line; combining
both requires a node to match both. Unknown selections refuse. Traversal is
backward by default; `--direction forward` follows dependencies outward from
the selected values. Control dependencies are included by default;
`--data-only` excludes them. The report keeps selected nodes, edge endpoints,
relevant diagnostics, and graph-wide limitations. It records `query.variable`,
`query.line`, `query.direction`, and `query.include_control`. With no selection
criteria the full graph is returned with `seeds: []`, including its CFG and AST
edges.

```sh
codegraph-slicer flow spend:admit --db /tmp/example.sqlite \
  --variable total --line 15
codegraph-slicer flow spend:admit --db /tmp/example.sqlite \
  --variable new_estimate_cents --direction forward --data-only --format markdown
```

On `examples/spend.py`, the first is a backward slice at the `total` guardrail predicate, showing
observed dependencies such as billed and new estimates, loop-carried liability,
and relevant guard control. The second follows only data dependencies forward
from `new_estimate_cents`, avoiding unrelated liability flows. These examples
depend on the indexed source revision: use the actual path/qualname and line in
your snapshot.

The `python-local-cpg-v2` profile is statement-granularity and
intraprocedural. It replaces `python-local-cpg-v1` because ordered target reads
change graphs for previously accepted functions, including annotated memory
targets. Engine/Python identity remains explicit.
It supports synchronous and async functions (without explicit
await), assignments, annotated/augmented assignments, expressions, if/elif/else,
for/while and loop else, break/continue, return/raise, assert/pass, and eagerly
evaluated named expressions. Walrus expressions inside short-circuit or
conditional expressions are refused because the definition may not execute.
Potentially overlapping chained attribute/subscript targets, assignment writes
that also rebind their receiver, and memory targets whose own evaluation
walrus-rebinds the receiver are refused with a source location. Rebinding a
receiver or subpath invalidates older direct-path facts where modeled; aliases
remain unresolved.

`try`/`except`/`else`/`finally`, `except*`, `with`, and `async with` have
conservative statement-level exceptional transfers. Every protected statement,
including nested statements, may raise before or after its writes. This retains
both pre-store and post-store reaching definitions, including partial bindings;
it does not infer exception types or expression-level failure points. Handler
type expressions are evaluated in source order: walrus bindings in a type are
available to that handler and to later handler type expressions and bodies, but
not to earlier handlers. Only normal try-body completion enters `else`. Finalizers
run on normal, exceptional, return, break, and continue paths, with separate
copies for pending completions; a finalizer's abrupt completion replaces the
pending completion. Handler aliases
are bound only on the matched handler path and deleted on every exit, including
implicit errors. Type lookup or matching may itself raise after any walrus
bindings; those failures propagate outward with the bindings already made.
`except*` models possible matched-handler subsets in source order, preserving
effects of earlier handlers even when they raise; unmatched or pending failures
may propagate. Exception matching is an overapproximation.

Context expressions evaluate left to right before their `as` bindings and body;
walrus bindings in a manager expression are visible to its binding and later
context expressions. Assignment evaluates its value before targets from left to
right; annotated assignment does not locally evaluate its annotation, and
augmented assignment evaluates its target once before its value. Loop iterable
expressions run before target binding, whose target reads and walrus bindings are
modeled on both ordinary and structured-control paths. A `raise` evaluates its
exception expression before its cause.
Acquired managers exit in reverse order on all completions; failed acquisition
does not exit that manager, but still unwinds earlier acquisitions. Each
`__exit__`/`__aexit__` may raise. On exceptional completion it may also suppress
the error and continue after the context statement, skipping the remaining suite.
Suppression never directly converts return, break, or continue into normal flow.
These are conservative possibilities, not inferred manager behavior or runtime
execution; implicit async protocol operations are modeled without executing them.
When nested `try` blocks with nonempty `finally` suites, `with`, and `async with`
exceed depth 4 inside the selected function body, the report adds a limitation
with the observed depth and deepest block location because graph size grows exponentially.

List/set/dict comprehensions and generator expressions collapse their reads into
the enclosing statement. The first iterable reads the outer scope. All target
names are local throughout the implicit scope after that first iterable, and
neither targets nor access paths rooted in them leak into outer definitions or
reads. Generator expressions are accepted only when deferred expressions have
no outer-name captures (for example, `(item for item in items)`); deferred outer
captures are refused because later assignments could affect them. Async
comprehensions, comprehension walrus expressions, and targets other than names
or name destructuring are refused.

Boolean and other statement-level expression reads are conservative, not
expression-path precise. Calls have opaque effects: arguments and receivers
are visible, but purity, return values, and alias side effects are not inferred.
Direct attribute/subscript writes are recorded; dynamic indexes, wildcard paths,
and aliasing do not prove memory paths disjoint. Explicit await, yield, lambdas,
nested definitions, imports, global/nonlocal mutation, and deletion refuse with a
specific source location. Functions without structured exception/context syntax
retain the original graph behavior, which has no implicit exceptional transfers.

`--max-tokens` defaults to 8000. It estimates the complete compact-JSON flow
report as `len(compact_json) // 4 + 1`; this includes identity and graph data,
not the outer `tokens` field or Markdown. If the selected dependencies do not
fit, the command errors without trimming them. This is a deterministic size
estimate, not a tokenizer count.

Edge identity is `(source, target, kind)`. Each reported line is one
representative occurrence, not a list of all call sites. Reachability uses
both snapshots, but test candidates and explicit test selections come from
observed HEAD test symbols. A test counts as updated only when its source
symbol changed; an edge rebind by itself does not count. Selection says only
that the test was selected, not that it ran. `affected_entrypoints` uses a
heuristic: `__main__` modules, module paths containing `cli`, `scripts`, or
`mcp`, and symbols named `main`. Arbitrary framework-decorated entrypoints are
not discovered automatically.

## Results and errors

Commands print JSON by default; `--format markdown` produces a readable report
where supported. Successful commands exit `0`. Invalid input, unreadable
source/database, unsupported syntax, an unknown anchor/seed, or a budget that
cannot hold required content reports an error on stderr and exits `2`.
Successful empty results are still JSON/Markdown output with exit `0` (for
example, an empty repository map when no files are indexed). The program does
not turn an unknown anchor or invalid database into an empty result. A delta
with no detected changes returns its report with empty change collections and
exit `0`.

Errors keep their original first diagnostic line and may add `hint:` lines
with indexed anchor suggestions, observed flow-variable lines, or database
open-state facts. Unsupported flow syntax suggests using `slice` for the
resolved function. A missing flow variable or an already informative stale
or budget error does not add an inferred suggestion.

To inspect unresolved references from Python, using the stdlib SQLite driver:

```sh
python3 - <<'PY'
import sqlite3

db = sqlite3.connect("file:/tmp/project.sqlite?mode=ro", uri=True)
for row in db.execute(
    "SELECT source_file,line,target,reason FROM diagnostics "
    "ORDER BY source_file,line,target"
):
    print(*row, sep="\t")
PY
```

This does not require the `sqlite3` shell executable. Diagnostics include
unmatched, shadowed, external, or dynamic references; they are observations,
not inferred definitions.

## Analysis limits

The repository index remains a static syntax model. `flow` adds only the
bounded intraprocedural CFG, reaching-definition, and mutation profile described
above; it does not provide whole-program CPG/CFG, runtime dispatch, arbitrary
alias-flow, package-root inference beyond the `src/` layout, or runtime-import evaluation. The broader
index still resolves lexical lambda, `global`, `nonlocal`, and comprehension
scopes.

Module-level re-exports resolve by one rule: `pkg.Name` resolves exactly when
`pkg`'s own module-scope reference to `Name` would. That holds when the final
module binding of `Name` is an unconditional import. A binding inside
`if`/`try`/loops/`match`, a later assignment, `del`, `except ... as Name`, a
match capture, or a function that declares `global Name` and binds it leaves the name
unknown, both inside the module and through the re-export, even when
another branch restores the import. The final binding
wins over an earlier definition of the same name. A `global Name` declaration
counts in function and class bodies alike, and so does a walrus in a decorator,
default, base class or annotation (annotations count even where Python 3.14
or `from __future__ import annotations` would not evaluate them). Import statements find their module
path through the import system: `import pkg.sub` and `from pkg.sub import x`
reach the submodule whatever `pkg` binds as `sub`, and `from pkg import sub`
reads `pkg`'s binding first, then the submodule. Attribute access `pkg.sub`
follows `pkg`'s binding, which wins over a same-named submodule, both inside
the module and through the re-export. Not modelled, for
in-module and re-exported names alike: `import pkg.sub` rebinding the
package attribute `sub`, star imports, writes through
`globals()`/`vars()`/`exec`/`eval`, `setattr` or assignment from another
module (`pkg.Name = x`), module `__getattr__`, and `sys.modules` replacement.
`CALLS` edges are static; runtime rebinding can falsify them. Budgets are deterministic size estimates,
not exact model tokenizer counts. No speed or fixed compression-ratio guarantee
is made.
