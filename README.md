# codegraph-slicer

A standalone, stdlib-only Python structural graph index, bounded context slicer and snapshot delta analyzer. Requires Python 3.12+ (including PEP 695 type aliases). Run `bin/codegraph-slicer` directly, or put its absolute path on `PATH` as `codegraph-slicer`. The command works from any current directory. Start with `codegraph-slicer --help`; the [full command cookbook](HELP.md) documents workflows, anchor forms, budget estimates, output, and limitations.

```sh
codegraph-slicer sync /path/to/project --db /tmp/graph.sqlite
codegraph-slicer sync /path/to/git/repo --revision HEAD --db /tmp/head.sqlite
codegraph-slicer map --db /tmp/graph.sqlite --seed package.module --max-tokens 1800
codegraph-slicer slice --db /tmp/graph.sqlite package.module:thing --max-tokens 4000
codegraph-slicer slice --db /tmp/graph.sqlite path/file.py:thing --format markdown
codegraph-slicer slice --db /tmp/graph.sqlite path/file.py:42 --include-callee-bodies
codegraph-slicer flow package.module:function --db /tmp/graph.sqlite --variable total --line 15
codegraph-slicer delta --base-db /tmp/base.sqlite --head-db /tmp/head.sqlite \
  --forbid-import package.runtime package.execution --selected-test tests/test_runtime.py
```

Every command supports `--format json|markdown`. Query commands require an existing database and open it read-only. `sync` is the only writer. Python APIs are exported by `codegraph_slicer`; use `with GraphStore(path) as store:` to close the connection.

Workspace sync hashes `.py` files except `.git`, `.venv`, `venv` and `__pycache__` directories. Python source symlinks are refused. Git sync reads immutable commit trees with a sanitized Git environment, refuses nonregular Python entries, and uses native blob OIDs: a repeated commit sync neither reads blobs nor reparses files. Workspace and Git caches use distinct identity prefixes. Added/deleted definitions rebind retained references from unchanged files. The parse-cache identity hashes the actual `ast_extractor.py` and `store.py` bytes plus the Python major/minor version. Missing or stale identity forces a full parse even when source hashes match; after that, an unchanged snapshot under the same identity uses the normal cache. Sync begins an immediate SQLite transaction before reading old hashes, parsing, or writing. A competing writer may receive `database is locked` and CLI exit 2 rather than interleave snapshots. Parse or SQL errors roll back the graph and cache identity together. A database belongs to one selected source root/snapshot at a time; syncing another root replaces that snapshot.

The index stores classes, functions, async functions, methods, nested definitions and explicit `type` aliases. It preserves original signatures/source locations, computes syntactic branch complexity, and renders module skeletons with function bodies replaced by ellipses. Module/function docstrings remain. Calls use enclosing callers. Local definitions, imported aliases, relative imports, annotations and inheritance resolve against indexed symbols. Lexical name tracking handles lambda, `global`, `nonlocal`, and comprehension scopes, while remaining a static approximation. Imports also retain module dependencies. Test-file references generate static `TESTS` edges. Raw references survive target addition/deletion; unmatched, shadowed, external or dynamic references appear in `diagnostics`, never as invented symbols:

```python
with GraphStore('/tmp/graph.sqlite', readonly=True) as store:
    print(store.rows('SELECT * FROM diagnostics ORDER BY source_file,line,target'))
```

Slices contain full requested implementations, full class/type definitions up to two `USES_TYPE`/`INHERITS` hops, caller signatures, then callee signatures (optionally bodies). Required anchors/types must fit or the query refuses. Across multiple anchors, required type definitions are not duplicated as optional callees. Optional neighbors follow PageRank order. A `file.py:line` anchor picks the innermost observed definition containing that line. Module anchors use `package.module:`. Repository maps aggregate symbol PageRank by file and include each file skeleton once.

Budgets are deterministic estimates, not tokenizer counts: `len(compact_json) // 4 + 1`, with Unicode characters counted directly. A slice budgets the complete `sections` value; a map budgets its complete `items` value. Keys, symbol IDs, locations, escaped source and punctuation count. The enclosing result metadata and Markdown rendering are outside the budget. `tokens` reports the same estimate used for admission.

Delta reports edge additions/removals, new module-import and symbol-call SCCs, forbidden import-prefix pairs (including unindexed targets), body/signature changes, complexity/fan-out drift, upstream entrypoints and static test gaps. Relationship identity is `(source, target, kind)`; a reported edge line is a representative occurrence, not a list of every call site. Threshold defaults are 10 for positive complexity/fan-out drift. Reachability uses both base and head graphs, while test candidates and explicit selections come from observed HEAD test symbols. A test counts as updated only when its source symbol changed; edge rebinding alone does not count. Selection does not assert that the test ran. Entrypoint discovery is heuristic: it recognizes `__main__` modules, modules with `cli`, `scripts`, or `mcp` path components, and symbols named `main`; arbitrary framework-decorated entrypoints are not discovered automatically. No test execution or behavioral coverage is inferred. Boundary rules are forbidden source/target prefix pairs; there are no project-specific default boundaries.

This is static syntax analysis, not runtime execution. Bounded local CFG and def-use/state-mutation slices are available through `flow`; their scope and limits are described below. The broader index still does not provide whole-program CPG/CFG, runtime dispatch, arbitrary alias flow, package-root inference beyond the `src/` layout or runtime-import evaluation. Qualified `self`/`cls` calls identify the lexical class method, not the eventual runtime implementation. Branch complexity counts each supported branch AST node once and excludes nested definitions. Module-level re-exports resolve exactly when the defining module's own reference to the name would (see HELP.md "Analysis limits"). Conditional bindings and external dependencies can remain unresolved; star imports, dynamic namespace writes and cross-module assignment are not modelled. Repeated ordinary/overload definitions retain the final binding and a diagnostic; property accessor source spans retain getter/setter definitions. No speed or fixed compression ratio is claimed. Unsupported syntax and invalid source fail explicitly.

## Bounded local flow

`flow` builds an intraprocedural statement graph on demand for one indexed function or method. It provides local CFG, reaching-definition and conservative mutation information (profile `python-local-cpg-v1`); it does not claim whole-program CPG coverage, runtime dispatch, alias precision, or execution behavior. Supported statements and explicit refusal cases are listed in [HELP.md](HELP.md).

```sh
codegraph-slicer sync examples --db /tmp/example.sqlite
codegraph-slicer flow spend:admit --db /tmp/example.sqlite --variable total --line 15
codegraph-slicer flow spend:admit --db /tmp/example.sqlite --variable new_estimate_cents --direction forward --data-only --format markdown
```

Using [`examples/spend.py`](examples/spend.py), the first flow command selects `total` at its guardrail line and walks backward through data and (by default) control dependencies, including the billed and new estimate inputs and loop-carried liability where observed. The second walks forward from `new_estimate_cents` using data edges only. With no `--variable` or `--line`, `flow` returns the complete function graph with no selection seeds. `--line` and `--variable` together restrict the seed set to nodes satisfying both. Forward/backward select edge direction; `--data-only` removes control edges. An unknown variable or line refuses instead of selecting a nearby value. The report records the query criteria and whether control dependence was enabled.

The source is the full module text cached in the SQLite snapshot. The report includes the observed symbol ID, indexed path and blob identity plus the SHA-256 of analyzed module text. Map, slice and flow verify source freshness before reading the index and refuse stale or unverifiable source; run `sync` to observe updates. Use `--snapshot` to intentionally query historical cached bytes. Querying opens SQLite read-only and never imports or executes indexed code. Default JSON includes the complete selected graph and diagnostics. Markdown lists nodes, source locations, definitions/uses, writes and limitations. The deterministic token estimate budgets the complete compact JSON flow value, including identity; if it does not fit, the query errors instead of trimming dependencies.

The local profile supports assignments, annotated and augmented assignments, expressions, if/elif/else, for/while and their `else` blocks, break/continue, return/raise, assert/pass, and eagerly evaluated named expressions. Walrus expressions under short-circuit or conditional expressions are refused because their execution is conditional. Potentially overlapping chained attribute/subscript targets, writes whose receiver is rebound in the same assignment, and augmented memory targets that depend on a receiver walrus are refused rather than treated as independent writes. Rebinding a receiver or subpath invalidates older direct-path facts where modeled; aliases remain unresolved, so access paths are not proof of disjoint memory.

Calls have opaque effects: argument and receiver reads are visible, but purity, return values, implicit exceptions, and alias side effects are not inferred. Statement-level expression reads are conservative. Attribute/subscript writes record the explicit path; dynamic indexes and aliases cannot establish disjoint memory. Unsupported syntax (including exception handlers, context managers, async/await, generators, comprehensions, lambdas, nested definitions, imports and global/nonlocal mutation) is refused with a location. Implicit exception edges are not modeled. This flow profile is bounded local evidence, not proof that unobserved effects are absent.

## Install

```sh
pip install git+https://github.com/lecholette/codegraph-slicer
```

Or run `bin/codegraph-slicer` from a checkout. There are no runtime dependencies. Python 3.12+.

## Use it locally

Index once, then query. Keep the database outside your repo or git-ignored (`*.sqlite`).

```sh
cd /path/to/your/project
codegraph-slicer sync . --db ~/.cache/codegraph-slicer/myproject.sqlite
export GRAPH=~/.cache/codegraph-slicer/myproject.sqlite

codegraph-slicer map --db $GRAPH --max-tokens 1500 --format markdown           # ranked overview
codegraph-slicer slice --db $GRAPH mypkg.module:func --format markdown         # one symbol in context
codegraph-slicer flow --db $GRAPH mypkg.module:func --variable total --line 42 # where does `total` come from
```

Re-run `sync` after edits; it only reparses changed files. Queries refuse a stale index (exit 2).
To review a branch, sync base and head into separate databases and compare them:

```sh
codegraph-slicer sync . --revision origin/main --db /tmp/base.sqlite
codegraph-slicer sync . --db /tmp/head.sqlite
codegraph-slicer delta --base-db /tmp/base.sqlite --head-db /tmp/head.sqlite --format markdown
```

## Use it with Claude Code

This repo ships a Claude Code skill at [`.claude/skills/codegraph-slicer/SKILL.md`](.claude/skills/codegraph-slicer/SKILL.md).
It tells Claude when to reach for the graph (callers, value provenance, blast radius, import cycles), how to index, and which limits to state.

1. Install the CLI (above) so `codegraph-slicer` is on `PATH`.
2. Install the skill for all your projects:

   ```sh
   mkdir -p ~/.claude/skills/codegraph-slicer
   curl -fsSL -o ~/.claude/skills/codegraph-slicer/SKILL.md \
     https://raw.githubusercontent.com/lecholette/codegraph-slicer/main/.claude/skills/codegraph-slicer/SKILL.md
   ```

   Or copy it into one project's `.claude/skills/codegraph-slicer/` to scope it to that repo.
3. Optionally allow the read-only commands without prompts in `.claude/settings.json`:

   ```json
   {
     "permissions": {
       "allow": [
         "Bash(codegraph-slicer sync:*)",
         "Bash(codegraph-slicer map:*)",
         "Bash(codegraph-slicer slice:*)",
         "Bash(codegraph-slicer flow:*)",
         "Bash(codegraph-slicer delta:*)"
       ]
     }
   }
   ```

4. Optionally keep the index fresh at session start with a `SessionStart` hook in the same file:

   ```json
   {
     "hooks": {
       "SessionStart": [
         { "hooks": [ { "type": "command",
           "command": "mkdir -p ~/.cache/codegraph-slicer && codegraph-slicer sync \"$CLAUDE_PROJECT_DIR\" --db ~/.cache/codegraph-slicer/$(basename \"$CLAUDE_PROJECT_DIR\").sqlite >/dev/null 2>&1 || true" } ] }
       ]
     }
   }
   ```

Then ask Claude things like "who calls `billing.charge`?", "where does `total` in `spend:admit` come from?" or "what does this branch break versus main?". Invoke it directly with `/codegraph-slicer`.

## Tests

```sh
python3 -m pytest -q tests
```

## License

Apache-2.0. See [LICENSE](LICENSE).
