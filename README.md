# codegraph-slicer (`v0.3.3`)

A fast static code graph index, bounded context slicer, call-impact analyzer, local control/data-flow slicer, and snapshot delta analyzer for **Python (`.py`)**, **Java (`.java`)**, **TypeScript (`.ts`, `.tsx`)**, and **JavaScript (`.js`, `.jsx`, `.mjs`, `.cjs`)**.

Requires Python 3.12+. Start with `codegraph-slicer --help`; see [HELP.md](HELP.md) for detailed command schemas and static-analysis limits.

```sh
# 1. Index a workspace or Git revision into SQLite
codegraph-slicer sync /path/to/project --db /tmp/graph.sqlite
codegraph-slicer sync /path/to/repo --revision HEAD --db /tmp/head.sqlite

# 2. Ranked repository map (PageRank over call/type/import graph)
codegraph-slicer map --db /tmp/graph.sqlite --seed package.module --max-tokens 2000 --format markdown

# 3. Bounded XML context slice (target implementation + file imports + types + callers + callees)
codegraph-slicer slice --db /tmp/graph.sqlite --max-tokens 8000 --format xml \
  src/service/handler.py:RequestHandler.handle \
  src/enterprise/strategy/pack_handler.js:PackBackupStrategyHandler.executeJob

# 4. Transitive call impact & Python intraprocedural control/data flow
codegraph-slicer impact package.module:func --db /tmp/graph.sqlite --max-depth 3 --max-nodes 200
codegraph-slicer flow package.module:func --db /tmp/graph.sqlite --variable total --line 42

# 5. Base-vs-head structural delta & architecture boundary checks
codegraph-slicer delta --base-db /tmp/base.sqlite --head-db /tmp/head.sqlite \
  --forbid-import package.runtime package.execution --max-tokens 8000
```

---

## Why `codegraph-slicer`?

Coding agents (`Claude Code`, `Codex`, `Cursor`, `OpenHands`) degrade on multi-file repositories when they rely solely on raw file reads and `grep`:
1. **Context Window & Cache Bloat:** Reading 500–2,000 line source files saturates the prompt cache with unrelated helper methods and boilerplate.
2. **Cross-Package Contract Bugs:** Key DTO schemas, constructor field wiring, file-level imports, and barrel re-exports get missed across packages.
3. **Brute-Force Exploration Loops:** Mid-tier and open-weight models spend dozens of turns grepping across decoy modules.

`codegraph-slicer` indexes the repository into SQLite (typically ~1s on ~300-file repositories; no fixed speed guarantee is claimed) and delivers a single **Bounded XML Slice** (`codegraph-slicer slice <anchors...> --format xml`):
- **`<types_and_schemas>`**: Enclosing class fields/constructors, inherited types, `@property` getters, JSDoc types, and instantiated DTO classes (`USES_TYPE` / `INHERITS` up to 2 hops) with non-called methods folded to 1-line signatures.
- **`<upstream_callers>`**: Exact 1-hop caller snippets and route dispatch windows.
- **`<downstream_dependencies>`**: Callee signatures (plus 1-line return hints or compact helper bodies `<= 40` lines).
- **`<target_implementations>`**: Placed at the tail of the XML envelope with top-level file imports and class header preambles preserved.

---

## Multi-Language Benchmark Results (`v0.3.3`, Zero Scenario Overfitting)

Across Python, Java, TypeScript, and JavaScript benchmarks, **`codegraph-slicer` cuts agent exploration turns and tool calls by `17.1%–50.0%` across all evaluated runs, reduces token or cache-read usage on 11 of 13 displayed model-language pairs (`-11.8%` to `-80.4%`, with `+10.0%` to `+28.7%` on the 2 JS runs where Terra and DeepSeek traded extra slice context for `+16.8%` and `+2.2%` higher test accuracy), and matches within 1 test or lifts task accuracy (`-1.1%` on Sonnet JS to `+16.8%` on Terra JS, `+42.3%` on Sonnet Python CP3, and `+69.2%` on DeepSeek Click)**:

### 1. Cross-Model Summary: Enterprise JavaScript Monorepo (`289` `.js` Files, `44,213` LOC, `89` Tests + `6` Cross-Package Contracts)

| Model | Baseline (No Slicer) | With `codegraph-slicer v0.3.3` | Delta |
| :--- | :--- | :--- | :--- |
| **`deepseek-v4.1-flash-1017`** | `84/89 (94.4%)`, `5/6` Contracts, `84` turns, `4.53M` cache read (`95.6k` in / `91.1k` out) | **`86/89 (96.6%)`**, **`6/6` Contracts (`100%`)**, **`67` turns**, `5.83M` cache read (`137.9k` in / `130.5k` out) | **`+2.2%` accuracy (`#1` overall), `+16.7%` contracts, `-20.2%` turns** |
| **`gpt-6.1-terra`** | `70/89 (78.7%)`, `5/6` Contracts, `20` execs, `67,390` tokens | **`85/89 (95.5%)`**, `5/6` Contracts, **`15` execs**, `74,163` tokens | **`+16.8%` accuracy (`+15` tests), `-25.0%` exec calls** |
| **`claude-sonnet-5-5`** | `85/89 (95.5%)`, `6/6` Contracts, `14` turns, `1,045,238` cache read, `$0.68` | **`84/89 (94.4%)`**, **`6/6` Contracts (`100%`)**, **`11` turns**, **`581,835` cache read**, **`$0.40`** | **`-21.4%` turns, `-44.3%` cache read tokens, `-40.9%` cost** |
| **`gpt-6-luna`** | `60/89 (67.4%)`, `5/6` Contracts, `23` execs, `116,324` tokens | **`68/89 (76.4%)`**, **`6/6` Contracts (`100%`)**, **`19` execs**, **`91,080` tokens** | **`+9.0%` accuracy (`+8` tests), `+16.7%` contracts, `-17.4%` execs, `-21.7%` tokens** |

### 2. Cross-Language Highlights (`Python`, `Java`, `TypeScript`, `JavaScript`)

- **`claude-sonnet-5-5`:**
  - **JavaScript (`289` files, `44k` LOC):** `84/89 (94.4%)`, `6/6` contracts, **`-21.4%` turns** (`14 -> 11`), **`-44.3%` cache read tokens** (`1.05M -> 582k`), **`-40.9%` cost** (`$0.68 -> $0.40`).
  - **Python `dynamic_config_service_api` CP3 (`26` executable tests):** **`13/26 (50.0%) -> 24/26 (92.3%, +42.3%)`**, **`-47.8%` turns** (`23 -> 12`), **`-48.7%` cache read tokens** (`972k -> 499k`), **`-34.8%` cost**.
  - **Java (`273` files, `58k` LOC):** **`89/89 (100.0%)`**, **`-16.3%` wall time**, **`-30.9%` cache creation tokens**, **`-18.3%` cost**.
  - **TypeScript (`238` files, `36k` LOC):** **`89/89 (100.0%)`**, **`-50.0%` Bash calls** (`10 -> 5`), **`-11.8%` cache read tokens**.
- **`gpt-6.1-terra`:**
  - **JavaScript (`289` files, `44k` LOC):** **`70/89 (78.7%) -> 85/89 (95.5%, +16.8%)`**, **`-25.0%` exec calls** (`20 -> 15`).
  - **Python `dynamic_config_service_api` CP3 (`26` executable tests):** **`24/26 (92.3%) -> 26/26 (100.0%)`**, **`-27.8%` exec calls** (`18 -> 13`), **`-19.2%` tokens** (`91.6k -> 74.1k`).
  - **TypeScript (`238` files, `36k` LOC):** **`89/89 (100.0%)`**, **`-27.3%` exec calls** (`11 -> 8`), **`-80.4%` tokens** (`57.9k -> 11.3k`).
  - **Java (`273` files, `58k` LOC):** **`89/89 (100.0%)`**, **`-33.3%` exec calls** (`12 -> 8`), **`-17.8%` tokens** (`721k -> 593k`).
- **`deepseek-v4.1-flash-1017`:**
  - **JavaScript (`289` files, `44k` LOC):** **`84/89 (94.4%) -> 86/89 (96.6%)`**, `6/6` contracts (`100%`), **`-20.2%` turns** (`84 -> 67`), `5.83M` cache read (`137.9k` in / `130.5k` out).
  - **Java (`273` files, `58k` LOC):** **`89/89 (100.0%)`**, **`-23.6%` wall time**, **`-27.4%` cache read tokens** (`1.21M -> 878k`, `963k` total tokens vs `1.32M` base), **`-25.3%` open-weight cost**.
  - **TypeScript (`238` files, `36k` LOC):** **`89/89 (100.0%)`**, **`-17.1%` turns** (`35 -> 29`), **`-29.3%` cache read tokens** (`2.09M -> 1.48M`), **`-38.3%` output tokens** (`36.4k -> 22.5k`).
  - **Python Pallets Click (`15` files, Major Release):** **`4/13 (30.8%) -> 13/13 (100.0%)`**, **`-47.0%` cache read tokens** (`32.4M -> 17.2M`), **`-42.9%` cost** (`$22.38 -> $12.78`).

---

## Language Support

| Language | Extensions | Indexed Constructs & Static Resolution |
| :--- | :--- | :--- |
| **Python** | `.py` | Classes, functions, `async def`, methods, `@property`, PEP 695 `type` aliases, C3 MRO inheritance, Protocol/ABC subtypes, constructor `self.<attr>` state coupling, top-level file import preambles, and intraprocedural CFG/def-use (`flow`). |
| **Java** | `.java` | Classes, interfaces, enums, records (including compact constructors), nested classes (`Outer.Inner`), constructors, methods, fields, generic type unwrapping (`List<T> -> T`), `this.field.method()` chains, and arity-aware overload resolution. |
| **TypeScript** | `.ts`, `.tsx` | Classes, interfaces, type aliases, enums, functions, arrow callbacks, methods, `tsconfig.json` `baseUrl`/`paths` aliases, barrel `index.ts` re-exports, and JSX component tags. |
| **JavaScript** | `.js`, `.jsx`, `.mjs`, `.cjs` | ES modules & CommonJS (`require()`, `module.exports`, `exports.x`, barrel `index.js` spreads), classes, functions, prototype assignments (`Cls.prototype.m`), constructor `this.field` inference, `new ClassName()` `USES_TYPE` edges, and JSDoc `@param`/`@returns`/`@type` annotations. |

---

## Install

```sh
pip install git+https://github.com/lecholette/codegraph-slicer
# or with uv:
uv tool install git+https://github.com/lecholette/codegraph-slicer
```

## Use with Claude Code

Install the bundled skill at [`.claude/skills/codegraph-slicer/SKILL.md`](.claude/skills/codegraph-slicer/SKILL.md):

```sh
mkdir -p ~/.claude/skills/codegraph-slicer
curl -fsSL -o ~/.claude/skills/codegraph-slicer/SKILL.md \
  https://raw.githubusercontent.com/lecholette/codegraph-slicer/main/.claude/skills/codegraph-slicer/SKILL.md
```

## Core Commands & Guarantees

- **Read-only queries & freshness checks:** `sync` is the only command that writes to the SQLite database. `map`, `slice`, `impact`, and `flow` open the database read-only and verify source freshness (exiting `2` with `StaleSnapshotError` if indexed files changed; pass `--snapshot` to query cached bytes without re-syncing).
- **Resilient anchor resolution:** `slice` and `impact` accept `pkg.mod:qualname`, `path/file.ext:Class.method`, `path/file.ext:method` (unambiguous suffix match), `path/file.ext:LINE`, or `path/file.ext` (module anchor).
- **Deterministic token budgeting:** `--max-tokens` bounds the complete printed JSON, Markdown, or XML output (`len(output) // 4 + 1`). When records are omitted to fit the budget, exact omission counts are reported.
- **Static analysis boundary:** All outputs are static syntax and call/type-graph evidence, never proof of runtime execution or test passage. See [HELP.md](HELP.md) for full schema details and analysis limits.

## Tests

```sh
python3 -m pytest -q tests
```

## License

Copyright 2026 Chris Cholette. Licensed under the Apache License, Version 2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE).
