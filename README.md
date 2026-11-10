# codegraph-slicer

**Give your coding agent the relevant code before it edits.**

Ask a coding agent to change a method in a large repository and it can spend its first dozen turns finding the file, reading hundreds of lines of unrelated helpers, and chasing callers across packages. By the time it starts editing, critical DTO contracts, constructor state wiring, or caller assumptions may still be missing.

`codegraph-slicer` gives your agent a different starting point: the target implementation, enclosing class contracts, upstream callers, and downstream dependencies in one bounded XML context slice.

Install the CLI and bundled skill, and your agent automatically checks repository structure before drowning in raw file reads. Small, straightforward tasks bypass slicing and go directly to the files; large monoliths and cross-package changes get structural context first.

**`v0.3.8`** · Python 3.12+ · **Python, Java, TypeScript, and JavaScript** · Apache 2.0

---

## Table of Contents

- [60-Second Quick Start](#60-second-quick-start)
- [How It Works](#how-it-works)
- [Installation & Agent Skill Setup](#installation--agent-skill-setup)
- [How Your Agent Uses the Skill](#how-your-agent-uses-the-skill)
- [The Basic CLI Workflow](#the-basic-cli-workflow)
- [Benchmark Results (`v0.3.8`)](#benchmark-results-v038)
- [When Something Goes Wrong](#when-something-goes-wrong)
- [What's Inside](#whats-inside)
- [Philosophy](#philosophy)
- [License](#license)

---

## 60-Second Quick Start

### Step 1: Install the CLI (Python 3.12+)

```sh
uv tool install "git+https://github.com/lecholette/codegraph-slicer.git@v0.3.8"
```

### Step 2: Install the Agent Skill (Pick Your Harness)

```sh
# Claude Code:
mkdir -p "$HOME/.claude/skills/codegraph-slicer" && \
  curl -fsSL "https://raw.githubusercontent.com/lecholette/codegraph-slicer/v0.3.8/.claude/skills/codegraph-slicer/SKILL.md" \
  -o "$HOME/.claude/skills/codegraph-slicer/SKILL.md"

# Cursor (Global):
mkdir -p "$HOME/.cursor/skills/codegraph-slicer" && \
  curl -fsSL "https://raw.githubusercontent.com/lecholette/codegraph-slicer/v0.3.8/.claude/skills/codegraph-slicer/SKILL.md" \
  -o "$HOME/.cursor/skills/codegraph-slicer/SKILL.md"

# Codex (User):
mkdir -p "$HOME/.codex/skills/codegraph-slicer" && \
  curl -fsSL "https://raw.githubusercontent.com/lecholette/codegraph-slicer/v0.3.8/.claude/skills/codegraph-slicer/SKILL.md" \
  -o "$HOME/.codex/skills/codegraph-slicer/SKILL.md"
```

*(See [Installation & Agent Skill Setup](#installation--agent-skill-setup) below for Gemini CLI, Antigravity, OpenCode, and repo-scoped paths.)*

### Step 3: Index a Local Repo & Slice Your First Target

```sh
cd /path/to/your/repo

# 1. Index the local workspace into a temporary SQLite graph (~1s for ~300 files; keeps git status clean)
GRAPH="${CODEGRAPH_DB:-$(mktemp -d)/graph.sqlite}"
codegraph-slicer sync . --db "$GRAPH"

# 2. Check repo map + scale recommendation (tells you whether to slice or read directly)
codegraph-slicer map --db "$GRAPH" --recommend-workflow --format markdown

# 3. Route a task prompt to exact symbols and monolith slice targets
codegraph-slicer route --db "$GRAPH" \
  --prompt "Fix validation in CheckoutHandler and update callers" --format json
```

Once the skill is installed, **you don't need to run these commands manually**—just prompt your coding agent as usual, and it will automatically sync, route, and slice when working in multi-package or monolithic repositories.

---

## How It Works

From the moment you install the CLI and skill, your agent has a structural workflow before reading large source files:

1. **Index (`sync`)**: Indexes Python (`.py`), Java (`.java`), TypeScript (`.ts`, `.tsx`), and JavaScript (`.js`, `.jsx`, `.mjs`, `.cjs`) source trees into a local SQLite graph (`~1s` on ~300-file repositories).
2. **Orient & Route (`map --recommend-workflow` or `route`)**: Evaluates repository scale, coupling, and monoliths (`>= 300` lines, `>= 15` definitions, or `CC >= 25`). Compact scopes automatically bypass slicing (`map_then_read`); monolithic or coupled scopes surface exact `slice_targets`.
3. **Front-Load Bounded Context (`slice --format xml`)**: Extracts 3–8 target symbols in a single deduplicated, token-budgeted XML envelope (`<types_and_schemas>`, `<upstream_callers>`, `<downstream_dependencies>`, `<target_implementations>`), including class/method Javadoc/JSDoc preambles and field declarations.
4. **Read Only What Remains**: Uses line-bounded reads only where condensed blocks or signature stubs need inspection, avoiding the double-context tax of loading both full files and slices.
5. **Verify Impact & Boundaries (`impact`, `flow`, `delta`)**: Traces caller blast radius, intraprocedural Python control/data flow, and base-vs-head import or cycle regressions alongside your test suite.

---

## Installation & Agent Skill Setup

### 1. Install the CLI

Requires **Python 3.12+** (`3.12`, `3.13`, or `3.14`) and **Git** (to install from GitHub). Only two Python packages are installed alongside standard library modules (`sqlite3`, `ast`): `tree-sitter>=0.24` and `tree-sitter-language-pack>=0.7`.

```sh
# With uv (recommended):
uv tool install "git+https://github.com/lecholette/codegraph-slicer.git@v0.3.8"

# Or with pip:
python3 -m pip install "git+https://github.com/lecholette/codegraph-slicer.git@v0.3.8"
```

Verify installation:

```sh
codegraph-slicer --version
codegraph-slicer --help
```

#### Local Code & Git Behavior
- **Operates on local source trees (`sync .`)**: `codegraph-slicer` indexes local files on disk (`Python`, `Java`, `TypeScript`, `JavaScript`). No code ever leaves your machine, and the target directory **does not even need to be a Git repository** for normal workspace indexing (`sync`), `map`, `route`, `slice`, `impact`, or `flow`.
- **When Git is used at runtime**: `git` is only invoked when you explicitly pass `--revision <git-ref>` (for example, `codegraph-slicer sync . --revision origin/main --db /tmp/base.sqlite` before running `delta`). In `--revision` mode, blobs are read directly from local `.git` objects (`git ls-tree` / `git cat-file`) without modifying or checking out your working tree.
- **Keeping `git status` clean**: Writing `--db` to a temp path (`$(mktemp -d)/graph.sqlite` or `/tmp/graph.sqlite`) keeps your repository working tree 100% clean without needing `.gitignore` changes. If you prefer a repo-local database (such as `.codegraph/graph.sqlite`), run `mkdir -p .codegraph` first and add `.codegraph/` to `.gitignore` (hidden dot-directories are automatically skipped during indexing).

### 2. Add the Skill to Your Coding Harness

The bundled [`SKILL.md`](.claude/skills/codegraph-slicer/SKILL.md) teaches your coding agent **when to slice** (multi-package repos and `>= 300`-line monoliths) and **when to bypass slicing** (compact repos where direct file reads are faster). Each snippet below is self-contained:

#### Claude Code

```sh
mkdir -p "$HOME/.claude/skills/codegraph-slicer" && \
  curl -fsSL "https://raw.githubusercontent.com/lecholette/codegraph-slicer/v0.3.8/.claude/skills/codegraph-slicer/SKILL.md" \
  -o "$HOME/.claude/skills/codegraph-slicer/SKILL.md"
```

#### Cursor

```sh
# Global user skill:
mkdir -p "$HOME/.cursor/skills/codegraph-slicer" && \
  curl -fsSL "https://raw.githubusercontent.com/lecholette/codegraph-slicer/v0.3.8/.claude/skills/codegraph-slicer/SKILL.md" \
  -o "$HOME/.cursor/skills/codegraph-slicer/SKILL.md"

# Or repository-scoped skill:
mkdir -p .cursor/skills/codegraph-slicer && \
  curl -fsSL "https://raw.githubusercontent.com/lecholette/codegraph-slicer/v0.3.8/.claude/skills/codegraph-slicer/SKILL.md" \
  -o .cursor/skills/codegraph-slicer/SKILL.md
```

#### Codex

```sh
# User-scoped skill:
mkdir -p "$HOME/.codex/skills/codegraph-slicer" && \
  curl -fsSL "https://raw.githubusercontent.com/lecholette/codegraph-slicer/v0.3.8/.claude/skills/codegraph-slicer/SKILL.md" \
  -o "$HOME/.codex/skills/codegraph-slicer/SKILL.md"

# Or repository-scoped skill:
mkdir -p .agents/skills/codegraph-slicer && \
  curl -fsSL "https://raw.githubusercontent.com/lecholette/codegraph-slicer/v0.3.8/.claude/skills/codegraph-slicer/SKILL.md" \
  -o .agents/skills/codegraph-slicer/SKILL.md
```

#### Gemini CLI

```sh
mkdir -p "$HOME/.gemini/skills/codegraph-slicer" && \
  curl -fsSL "https://raw.githubusercontent.com/lecholette/codegraph-slicer/v0.3.8/.claude/skills/codegraph-slicer/SKILL.md" \
  -o "$HOME/.gemini/skills/codegraph-slicer/SKILL.md"
```

#### Antigravity

```sh
mkdir -p "$HOME/.gemini/config/skills/codegraph-slicer" && \
  curl -fsSL "https://raw.githubusercontent.com/lecholette/codegraph-slicer/v0.3.8/.claude/skills/codegraph-slicer/SKILL.md" \
  -o "$HOME/.gemini/config/skills/codegraph-slicer/SKILL.md"
```

#### OpenCode

```sh
mkdir -p "$HOME/.config/opencode/skills/codegraph-slicer" && \
  curl -fsSL "https://raw.githubusercontent.com/lecholette/codegraph-slicer/v0.3.8/.claude/skills/codegraph-slicer/SKILL.md" \
  -o "$HOME/.config/opencode/skills/codegraph-slicer/SKILL.md"
```

---

## How Your Agent Uses the Skill

Once `codegraph-slicer` is on your `PATH` and `SKILL.md` is placed in your harness's skill directory:

1. **Zero-Config Automatic Triggering**:
   - Your agent reads the `SKILL.md` description at session start and automatically invokes `codegraph-slicer` when you ask structural questions (*"who calls X"*, *"what breaks if I change Y"*) or assign multi-file engineering tasks in Python, Java, TypeScript, or JavaScript repositories.
2. **Explicit Invocation (Optional)**:
   - In **Claude Code / Cursor / Antigravity / Gemini CLI**, you can also invoke the skill explicitly via `/codegraph-slicer` or mention it in your prompt (e.g., *"Use codegraph-slicer to slice the checkout handlers before editing"*).
3. **Optional Team Repo Rule (`AGENTS.md` or `CLAUDE.md`)**:
   - If you want every agent session in a repository to maintain a persistent index in `.codegraph/graph.sqlite` (git-ignored), add these 3 lines to your project's `AGENTS.md` or `CLAUDE.md`:
     ```markdown
     ## Code Graph Indexing (`codegraph-slicer`)
     - Before reading large source files (`>= 300` lines) or grepping across packages, run `mkdir -p .codegraph && codegraph-slicer sync . --db .codegraph/graph.sqlite` and `codegraph-slicer map --db .codegraph/graph.sqlite --recommend-workflow --format markdown`.
     - When `bypass_slicer: false`, front-load one batched `codegraph-slicer slice --snapshot --db .codegraph/graph.sqlite --max-tokens 8000 --format xml <anchors...>` call for your target symbols before editing.
     ```

---

## The Basic CLI Workflow

### 1. Sync the Repository (`sync`)

Keep the index outside the repository or in a git-ignored dot-directory (such as `.codegraph/`). Hidden dot-directories are automatically ignored during workspace indexing.

```sh
GRAPH="${CODEGRAPH_DB:-$(mktemp -d)/graph.sqlite}"
codegraph-slicer sync . --db "$GRAPH"

# Or index a specific Git revision:
codegraph-slicer sync . --revision HEAD --db /tmp/head.sqlite
```

`sync` is incremental: unchanged files are not reparsed when re-syncing after edits.

### 2. Orient & Route (`map --recommend-workflow` and `route`)

Check repository structure and scale before reading source files:

```sh
# Ranked PageRank repository map + scale recommendation at the top of the report:
codegraph-slicer map --db "$GRAPH" --recommend-workflow --format markdown

# Or route a specific task prompt directly to resolved anchors and monolith slice targets:
codegraph-slicer route --db "$GRAPH" \
  --prompt "Update CheckoutHandler.processOrder and inspect its callers" \
  --format json
```

`ScaleRecommendation` classifies the repository into one of four modes:

| Mode | When It Triggers | Recommended Action |
| :--- | :--- | :--- |
| **`map_then_read`** (`bypass_slicer: true`) | Compact scope (`< 30` files & `< 5,000` LOC in Java/TS/JS, or `< 50` files & `< 7,500` LOC in Python) with no monoliths (`>= 300` lines, `>= 15` definitions, or `CC >= 25`) and `< 40` cross-file edges. | Read the target files directly; skip `slice` and `delta`. |
| **`slice_monoliths`** (`bypass_slicer: false`) | One or more source files are monoliths (`>= 300` lines, `>= 15` definitions with `>= 200` lines, or `CC >= 25`), even in small-file-count repositories. | Slice symbols inside the monolithic files listed in `slice_targets` instead of reading them end-to-end. |
| **`graph_optional`** (`bypass_slicer: false`) | Multi-package or strongly coupled repository (`>= 30` files, `>= 5,000` LOC, or `>= 40` cross-file edges). | Front-load a batched `slice` for target symbols across packages. |
| **`direct_read`** | Empty or unparsed index. | Inspect source files directly. |

### 3. Extract One Bounded XML Context Slice (`slice`)

Batch 3–8 target anchors into a single call so shared types and callers are deduplicated:

```sh
codegraph-slicer slice --db "$GRAPH" --max-tokens 8000 --format xml \
  src/main/java/com/acme/orders/OrderPipeline.java:OrderPipeline.dispatchDueOrders \
  src/main/java/com/acme/checkout/CheckoutHandler.java:CheckoutHandler \
  src/main/java/com/acme/validation/OrderPolicyValidator.java:OrderPolicyValidator
```

The XML slice organizes structural context into four deterministic sections:

| XML Section | Contents |
| :--- | :--- |
| **`<types_and_schemas>`** | Enclosing class fields/constructors, inherited types, `@property` getters, JSDoc types, and instantiated DTOs (`USES_TYPE` / `INHERITS` up to 2 hops) with non-called methods folded to 1-line signatures. |
| **`<upstream_callers>`** | Direct 1-hop caller snippets and dispatch windows showing how targets are invoked. |
| **`<downstream_dependencies>`** | Callee signatures, 1-line return hints, and compact same-file helper bodies (`<= 40` lines). |
| **`<target_implementations>`** | Placed at the tail of the XML envelope with top-level file imports and class header preambles preserved. |

### 4. Check Blast Radius, Data Flow, and Structural Deltas (`impact`, `flow`, `delta`)

```sh
# Transitive caller blast radius before changing a signature or return contract:
codegraph-slicer impact src/service/handler.py:RequestHandler.handle \
  --db "$GRAPH" --max-depth 3 --max-nodes 200 --format markdown

# Intraprocedural Python control and data-flow slice for a specific variable:
codegraph-slicer flow src/service/handler.py:RequestHandler.handle \
  --db "$GRAPH" --variable total --line 42 --direction backward --format markdown

# Base-vs-head structural delta, cycle detection, and import-boundary enforcement:
cp "$GRAPH" /tmp/head.sqlite
codegraph-slicer sync . --db /tmp/head.sqlite
codegraph-slicer delta --base-db "$GRAPH" --head-db /tmp/head.sqlite \
  --forbid-import com.enterprise.runtime com.enterprise.execution --max-tokens 8000
```

---

## Benchmark Results (`v0.3.8`)

### 1. Enterprise JavaScript Hard Monorepo (`289` `.js` Files, `44,219` LOC, `89` Tests + `6` Cross-Package Contracts)

Evaluated across five coding models on a 12-package enterprise CommonJS JavaScript repository containing `400–520`-line monolithic strategy/orchestration classes, barrel `index.js` re-exports, and `273` decoy modules (zero post-hoc fixups):

| Model | Baseline (No Slicer) | With `codegraph-slicer v0.3.8` | Delta |
| :--- | :--- | :--- | :--- |
| **`gpt-6-luna`** | `69/89 (77.5%)`, `6/6` Contracts (`CP2: 14/18`, `CP4: 7/21`), `20` exec calls, `72,751` tokens | **`85/89 (95.5%)`**, **`6/6` Contracts (`100%`, `CP1–CP2: 100%`, `CP4: 19/21`)**, `29` exec calls, `106,666` tokens | **`+18.0%` accuracy (`+16` tests), `100%` on CP1 & CP2, `+57.1%` on CP4** |
| **Cursor Auto (`auto`)** | `76/89 (85.4%)`, `4/6` Contracts (`CP2: 13/18`, `CP3: 11/18`), `18` turns, `1.24M` cache read (`205.5k` in / `21.4k` out), `1.47M` total tokens | **`87/89 (97.8%)`**, **`6/6` Contracts (`100%`, `CP2: 18/18`, `CP3: 17/18`)**, **`18` turns**, **`969k` cache read** (**`86.1k` in** / `22.5k` out), **`1.08M` total tokens** | **`+12.4%` accuracy (`+11` tests), `+33.3%` contracts (`4/6 -> 6/6`), `-58.1%` input tokens, `-22.0%` cache read tokens, `-26.6%` total tokens** |
| **`claude-haiku-5-5`** | `70/89 (78.7%)`, `2/6` Contracts (`CP2: 10/18`, `CP3: 12/18`), `53` turns, `7.03M` cache read (`86.3k` out), `399.1s`, `$0.54` | **`85/89 (95.5%)`**, **`6/6` Contracts (`100%`, `CP1–CP2: 100%`, `CP3: 17/18`)**, **`44` turns**, **`4.18M` cache read** (**`76.1k` out**), **`346.8s`**, **`$0.42`** | **`+16.8%` accuracy (`+15` tests), `+66.7%` contracts (`2/6 -> 6/6`), `-17.0%` turns, `-40.6%` cache read tokens, `-13.1%` wall time, `-21.7%` cost** |
| **Cursor Composer (`composer-2.5`)** | `87/89 (97.8%)`, `6/6` Contracts (`CP3: 16/18`), `21` turns, `1.30M` cache read (`73.4k` in / `13.3k` out), `87.6s` | **`88/89 (98.9%)`**, **`6/6` Contracts (`100%`, `CP1–CP3: 100%`)**, `26` turns, `1.54M` cache read (`126.3k` in / `18.9k` out), `116.2s` | **`+1.1%` accuracy (`88/89 = 98.9%`), `100%` on CP1–CP3 & `6/6` contracts** |
| **`deepseek-v4.1-flash-1017`** | `86/89 (96.6%)`, `6/6` Contracts, `97` turns, `7.68M` cache read (`114.2k` in / `96.3k` out), `828.3s` | **`86/89 (96.6%)`**, **`6/6` Contracts (`100%`, `CP1: 32/32`, `CP2: 18/18`)**, `111` turns, `8.11M` cache read (`113.7k` in / `131.4k` out), `1172.0s` | **`96.6%` accuracy (`86/89`) & `6/6` cross-package contracts (`-0.5%` input tokens)** |

### 2. Enterprise Java Hard Monorepo (`295` `.java` Files, `64,080` LOC, `89` Tests + `6` Cross-Package Contracts)

Evaluated across the same five coding models on a 12-package enterprise Java repository containing `500–640`-line monolithic handlers and `279` decoy classes (zero post-hoc fixups):

| Model | Baseline (No Slicer) | With `codegraph-slicer v0.3.8` | Delta |
| :--- | :--- | :--- | :--- |
| **Cursor Composer (`composer-2.5`)** | `64/89 (71.9%)`, `6/6` Contracts, `24` turns, `1.45M` cache read (`231.8k` in / `16.8k` out), `154.9s` | **`87/89 (97.8%)`**, **`6/6` Contracts (`100%`, `CP1–CP3: 100%`)**, `35` turns, `2.18M` cache read (**`208.2k` in** / `24.1k` out), **`135.0s`** | **`+25.9%` accuracy (`+23` tests), `-10.2%` input tokens, `-12.8%` wall time** |
| **`deepseek-v4.1-flash-1017`** | `30/89 (33.7%)`, `6/6` Contracts (`CP1: 4/32`), `46` turns, `2.48M` cache read (`76.7k` in / `37.2k` out) | **`75/89 (84.3%)`**, **`6/6` Contracts (`100%`, `CP1: 32/32`, `CP4: 21/21`)**, `61` turns, **`2.44M` cache read** (`88.1k` in / `42.4k` out), `346.9s` | **`+50.6%` accuracy (`+45` tests), `+87.5%` on CP1 & `100%` on CP4, `-1.6%` cache read tokens** |
| **`gpt-6-luna`** | `75/89 (84.3%)`, `6/6` Contracts (`CP2: 14/18`, `CP3: 8/18`), `20` exec calls, `84,340` tokens | **`86/89 (96.6%)`**, **`6/6` Contracts (`100%`, `CP1–CP2: 100%`, `CP3: 17/18`)**, **`15` exec calls**, **`80,138` tokens** | **`+12.3%` accuracy (`+11` tests), `-25.0%` exec calls (`20 -> 15`), `-5.0%` tokens** |
| **`claude-haiku-5-5`** | `89/89 (100.0%)`, `6/6` Contracts, `58` turns, `6.52M` cache read (`89.0k` out), `408.1s`, `$0.64` | **`89/89 (100.0%)`**, **`6/6` Contracts (`100%`)**, **`49` turns**, **`4.81M` cache read** (**`75.0k` out**), **`337.6s`**, **`$0.42`** | **`-15.5%` turns, `-26.2%` cache read tokens, `-17.3%` wall time, `-34.4%` cost** |
| **Cursor Auto (`auto`)** | `86/89 (96.6%)`, `6/6` Contracts, `22` turns, `1.70M` cache read (`110.1k` in / `25.0k` out), `1.83M` total tokens, `403.1s` | `75/89 (84.3%)`, **`6/6` Contracts (`100%`, `CP1: 32/32`)**, **`17` turns**, **`914k` cache read** (**`81.2k` in** / **`21.1k` out**), **`1.02M` total tokens**, **`242.3s`** | **`-22.7%` turns (`22 -> 17`), `-26.3%` input tokens, `-46.2%` cache read tokens, `-44.6%` total tokens, `-39.9%` wall time** |

### 3. Additional Cross-Language Highlights (`Python`, `TypeScript`, `JavaScript`)

- **Enterprise JavaScript Monorepo (`289` `.js` files, `44,213` LOC, `89` tests + `6` contracts):**
  - **`deepseek-v4.1-flash-1017`:** **`84/89 (94.4%) -> 86/89 (96.6%, +2.2%)`**, **`5/6 -> 6/6` contracts (`100%`)**, **`-20.2%` turns** (`84 -> 67`).
  - **`gpt-6.1-terra`:** **`70/89 (78.7%) -> 85/89 (95.5%, +16.8%)`**, **`-25.0%` exec calls** (`20 -> 15`).
  - **`claude-sonnet-5-5`:** `84/89 (94.4%)`, **`6/6` contracts (`100%`)**, **`-21.4%` turns** (`14 -> 11`), **`-44.3%` cache read tokens** (`1.05M -> 582k`), **`-40.9%` cost** (`$0.68 -> $0.40`).
  - **`gpt-6-luna`:** **`69/89 (77.5%) -> 85/89 (95.5%, +18.0%)`**, **`6/6` contracts (`100%`)**, **`-23.7%` exec calls** (`38 -> 29`), **`-13.8%` tokens** (`123.7k -> 106.7k`).
- **Python Multi-File Services & Libraries:**
  - **`claude-sonnet-5-5` (`dynamic_config_service_api`, `76` tests / `26` CP3 tests):** **`72/76 (94.7%)`** overall (**`13/26 -> 24/26 (+42.3%)`** on CP3 subset), **`-47.8%` turns** (`23 -> 12`), **`-48.7%` cache read tokens** (`972k -> 499k`).
  - **`gpt-6.1-terra` (`dynamic_config_service_api`, `76` tests / `26` CP3 tests):** **`24/26 -> 26/26 (100.0%)`** on CP3 subset, **`-27.8%` exec calls** (`18 -> 13`), **`-19.2%` tokens** (`91.6k -> 74.1k`).
  - **`deepseek-v4.1-flash-1017` (Pallets Click, `15` files):** **`4/13 (30.8%) -> 13/13 (100.0%, +69.2%)`**, **`-47.0%` cache read tokens** (`32.4M -> 17.2M`), **`-42.9%` cost**.
- **TypeScript (`238` `.ts` files, `36k` LOC, `89` tests):**
  - **`claude-sonnet-5-5`:** **`89/89 (100.0%)`**, **`-50.0%` Bash calls** (`10 -> 5`), **`-11.8%` cache read tokens**.
  - **`gpt-6.1-terra`:** **`89/89 (100.0%)`**, **`-27.3%` exec calls** (`11 -> 8`), **`-80.4%` tokens** (`57.9k -> 11.3k`).
  - **`deepseek-v4.1-flash-1017`:** **`89/89 (100.0%)`**, **`-17.1%` turns** (`35 -> 29`), **`-29.3%` cache read tokens** (`2.09M -> 1.48M`).

---

## When Something Goes Wrong

### `StaleSnapshotError` (Exit Code `2`)
`map`, `route`, `slice`, `impact`, and `flow` open the SQLite database read-only and verify that indexed source files have not changed on disk.
- **If you want current workspace code:** Re-run `codegraph-slicer sync . --db "$GRAPH"`.
- **If you want to query the pre-edit snapshot without re-syncing:** Pass `--snapshot` (`codegraph-slicer slice --snapshot --db "$GRAPH" ...`).

### `AnchorNotFoundError` (Exit Code `2`)
When a requested symbol or file anchor cannot be resolved, `codegraph-slicer` returns nearest-match `candidates` in copy-pasteable `path/file.ext:Class.method` format (for example, `src/main/java/com/acme/checkout/CheckoutHandler.java:CheckoutHandler.processOrder`) in `< seconds` even on million-line indices.
- Copy the exact candidate anchor from the error diagnostic instead of guessing method names.
- If a class's method names are unknown, pass the class anchor directly (`path/file.ext:ClassName`).

### Output Exceeds `--max-tokens`
`--max-tokens` bounds the complete printed JSON, Markdown, or XML output (`len(output) // 4 + 1`). When records are omitted to fit the budget, exact omission counts (`<omitted>`) and line ranges (`lines="start-end"`) are reported so you can inspect specific lines or raise `--max-tokens`.

### Small Repositories & Double-Context Tax
On small repositories with no monoliths (`ScaleRecommendation` reports `bypass_slicer: true` / `mode: "map_then_read"`), reading 2–3 small files directly is faster and cheaper than loading both an XML slice and full files. Follow `bypass_slicer: true` and skip `slice` and `delta`.

---

## What's Inside

### CLI Commands

| Command | Purpose |
| :--- | :--- |
| **`sync`** | Index a workspace directory or Git `--revision` into SQLite (incremental on re-sync). |
| **`map`** | Ranked PageRank repository map (`--seed` to focus on a package/path; `--recommend-workflow` for scale/monolith advisory). |
| **`route`** | Fast (`< 25 ms` warm on 1M-LOC graphs) task prompt router returning `workflow_mode`, `resolved_anchors`, `target_files`, and ranked `slice_targets`. |
| **`slice`** | Bounded XML/Markdown/JSON context slice combining `<types_and_schemas>`, `<upstream_callers>`, `<downstream_dependencies>`, and `<target_implementations>`. |
| **`impact`** | Bounded transitive caller reachability (`--max-depth`, `--max-nodes`, `--include-ambiguous`). |
| **`flow`** | Intraprocedural Python control/data-flow slice (`--variable`, `--line`, `--direction backward|forward`, `--data-only`, `--compact`). |
| **`delta`** | Base-vs-head snapshot diff reporting modified symbols, added/removed edges, new cycles, complexity drift, and `--forbid-import` violations. |

### Language Support Matrix

| Language | Extensions | Indexed Constructs & Static Resolution | Local `flow` |
| :--- | :--- | :--- | :--- |
| **Python** | `.py` | Classes, functions, `async def`, methods, `@property`, PEP 695 `type` aliases, C3 MRO inheritance, Protocol/ABC subtypes, constructor `self.<attr>` state coupling, and top-level file import preambles. | Yes (CFG + def-use). |
| **Java** | `.java` | Classes, interfaces, enums, records (including compact constructors), nested classes (`Outer.Inner`), constructors, methods, fields, generic type unwrapping (`List<T> -> T`), `this.field.method()` chains, and arity-aware overload resolution. | No (use `slice`). |
| **TypeScript** | `.ts`, `.tsx` | Classes, interfaces, type aliases, enums, functions, arrow callbacks, methods, `tsconfig.json` `baseUrl`/`paths` aliases, barrel `index.ts` re-exports, and JSX component tags. | No (use `slice`). |
| **JavaScript** | `.js`, `.jsx`, `.mjs`, `.cjs` | ES modules & CommonJS (`require()`, `module.exports`, `exports.x`, barrel `index.js` spreads), classes, functions, prototype assignments (`Cls.prototype.m`), constructor `this.field` inference, `new ClassName()` `USES_TYPE` edges, and JSDoc `@param`/`@returns`/`@type` annotations. | No (use `slice`). |

See [HELP.md](HELP.md) for complete command schemas and static-analysis limits.

---

## Philosophy

- **Deterministic static evidence over brute-force `grep`:** Give the agent exact type schemas, constructor fields, caller snippets, and file import preambles before it edits.
- **Scale-aware routing from 1k to 1M+ LOC:** Automatically bypass slicing on compact tasks while detecting and slicing monolithic files (`>= 300` lines, `>= 15` definitions, or `CC >= 25`) even in repositories with few files.
- **Strict token budgets with explicit omission accounting:** Every command respects `--max-tokens` and reports exact counts of omitted records so nothing is silently dropped.
- **Read-only queries & freshness guarantees:** Only `sync` writes to SQLite. All query commands open the database read-only and verify source freshness unless `--snapshot` is explicitly requested.
- **Static evidence complements runtime tests:** Call graphs and slices accelerate localization and prevent contract mismatches; test suites verify runtime behavior.

```sh
uv run pytest -q tests
```

## License

Copyright 2026 Chris Cholette. Licensed under the Apache License, Version 2.0; see [LICENSE](LICENSE) and [NOTICE](NOTICE).
