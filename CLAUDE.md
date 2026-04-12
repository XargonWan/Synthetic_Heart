# CLAUDE.md — Synthetic Heart (SyntH)

Read and follow the full project reference in `AGENTS.md`. Consult `docs/` as needed for deep-dives on specific subsystems.

## Role

You are a Senior Python Architect working on SyntH, a modular AI persona system.

## Toolchain — Astral only

- `uv sync` / `uv add <pkg>` / `uv add --dev <tool>`
- **Never** use `pip`, `pip install`, or `python -m venv`.

## Mandatory Validation (before marking any task done)

```bash
uv run ruff format .
uv run ruff check --fix .
uv run ty check path/to/files_you_edited.py   # scoped — never the whole repo
uv run pytest # Ignore all selenium tests
```

Fix failures before moving on.

## Git & Commits

- **No `git push`.** I push. You may stage/commit only when I ask.
- **No `git add` or `git commit`** unless I explicitly ask.
- Once I confirm a change is good, commit it using conventional commits (see `docs/contributing.rst`):
  `fix(scope):`, `feat(scope):`, `chore(scope):`, `minor(scope):`, `doc(scope):`, `patch(scope):`

## Hard Rules
- **2-attempt limit.** Same error twice → stop → `"⚠️ Stuck on [Error]. Requesting human or advanced model intervention."`
- **Type hints required** on all Python functions (params + return).
- **Cross-platform:** Linux-container-first. Platform-specific code only as a guarded secondary path (`sys.platform`).
- **Docs:** if your change warrants it, update `docs/` (Sphinx, ReadTheDocs format, English).
- **Document unknown issues:** If you encounter a bug, recurring error, or non-obvious workaround that isn't already in `AGENTS.md` §12, append an entry before ending your session. Do not fix it unless asked — just document it so future agents don't waste tokens rediscovering it.

## Key Paths

| What | Where |
|------|-------|
| Core engine | `core/` |
| Plugins | `plugins/` |
| LLM engines (new) | `cortex/` |
| LLM engines (legacy) | `llm_engines/` |
| Interfaces | `interface/` |
| Tests | `tests/` |
| Docs | `docs/` |
| DB schema | `init-db.sql` + inline in `core/db.py` and each plugin |
| WebUI templates | `core/webui_templates/` |
| Frontend JS | `res/synth_webui/js/` |
| Animations | `skins/*/animations/` |
| Persona configs | `skins/*/persona.json` |

## MCP Tools

Use these before reaching for file reads. They are faster and cheaper.

### synth-logs — log access across rotations

**Call this at the start of every debugging session:**
```
get_recent_errors(minutes=60)
```

| Tool | When to use |
|------|------------|
| `get_recent_errors(minutes=60)` | First step of any debug — all ERROR/WARNING across all logs |
| `tail_log("synth", lines=100)` | Recent runtime context |
| `search_logs("keyword", log_files=["synth"], level="ERROR")` | Targeted search with filters |
| `list_log_files()` | See what's available and how many rotations exist |

**Log files:**
- `synth` — main runtime (everything)
- `cortex_api` — full LLM request/response payloads, section/banner format, level filter does NOT apply
- `live_api` — live audio session events, same section format
- `webui` — web interface events
- `memoria` — memory subsystem
- `gemini_extract` — Gemini extraction pipeline

Logs rotate at 2000 lines — extremely fast in DEBUG mode (1–2 interactions). All MCP tools span rotations via `lookback_files` (default 3). Increase it if the window is too narrow.

### gitnexus — code intelligence

| Task | Call |
|------|------|
| Understand unfamiliar code | `gitnexus_query("concept")` before grepping |
| Before editing any function | `gitnexus_impact("name", direction="upstream")` |
| Full callers/callees of a symbol | `gitnexus_context("name")` |
| Safe rename | `gitnexus_rename(symbol_name="old", new_name="new", dry_run=True)` |

Full reference in the GitNexus section below.

## Debugging SOP

Always follow this order — it prevents reading code you don't need:

1. `get_recent_errors(minutes=60)` — what actually failed
2. `tail_log("synth", lines=100)` — surrounding context
3. `gitnexus_query("<error keyword or symptom>")` — find the relevant execution flow
4. `gitnexus_context("<suspect function>")` — full caller/callee map before touching anything
5. Read source files last, scoped only to what the above pointed at

## Token Traps — Never Do These

- Don't explore `.venv/` — enormous (torch, torchaudio etc.), never relevant
- Don't read `logs/*.wav` — binary audio test fixtures
- Don't grep across `plugins/` (40 files) — use `gitnexus_query` instead
- Don't read all of `init-db.sql` — grep for the specific `CREATE TABLE <name>`, or see AGENTS.md §13
- Don't read full active `synth.log` — it rotates constantly, use MCP tools
- Don't run `uv run ty check .` on the whole repo — scope to edited files only

## Architecture TL;DR

- **Single message chain** — all messages flow through `core/`.
- **Action parser** discovers actions from plugins + interfaces via `get_supported_actions()`.
- **Plugins** subclass `PluginBase` or `AIPluginBase`. Removing one never breaks the system.
- **Interfaces** (Telegram, Discord, Matrix) forward I/O into the chain. Never bypass it.
- **Animations** use logical state names (`think`, `write`, `idle`), never raw file paths.

See `AGENTS.md` for the full architecture reference, animation system details, plugin contracts, and container/infra notes.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **synthetic_heart** (7429 symbols, 24756 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

> If any GitNexus tool warns the index is stale, run `npx gitnexus analyze` in terminal first.

## Always Do

- **MUST run impact analysis before editing any symbol.** Before modifying a function, class, or method, run `gitnexus_impact({target: "symbolName", direction: "upstream"})` and report the blast radius (direct callers, affected processes, risk level) to the user.
- **MUST run `gitnexus_detect_changes()` before committing** to verify your changes only affect expected symbols and execution flows.
- **MUST warn the user** if impact analysis returns HIGH or CRITICAL risk before proceeding with edits.
- When exploring unfamiliar code, use `gitnexus_query({query: "concept"})` to find execution flows instead of grepping. It returns process-grouped results ranked by relevance.
- When you need full context on a specific symbol — callers, callees, which execution flows it participates in — use `gitnexus_context({name: "symbolName"})`.

## When Debugging

1. `gitnexus_query({query: "<error or symptom>"})` — find execution flows related to the issue
2. `gitnexus_context({name: "<suspect function>"})` — see all callers, callees, and process participation
3. `READ gitnexus://repo/synthetic_heart/process/{processName}` — trace the full execution flow step by step
4. For regressions: `gitnexus_detect_changes({scope: "compare", base_ref: "main"})` — see what your branch changed

## When Refactoring

- **Renaming**: MUST use `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` first. Review the preview — graph edits are safe, text_search edits need manual review. Then run with `dry_run: false`.
- **Extracting/Splitting**: MUST run `gitnexus_context({name: "target"})` to see all incoming/outgoing refs, then `gitnexus_impact({target: "target", direction: "upstream"})` to find all external callers before moving code.
- After any refactor: run `gitnexus_detect_changes({scope: "all"})` to verify only expected files changed.

## Never Do

- NEVER edit a function, class, or method without first running `gitnexus_impact` on it.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis.
- NEVER rename symbols with find-and-replace — use `gitnexus_rename` which understands the call graph.
- NEVER commit changes without running `gitnexus_detect_changes()` to check affected scope.

## Tools Quick Reference

| Tool | When to use | Command |
|------|-------------|---------|
| `query` | Find code by concept | `gitnexus_query({query: "auth validation"})` |
| `context` | 360-degree view of one symbol | `gitnexus_context({name: "validateUser"})` |
| `impact` | Blast radius before editing | `gitnexus_impact({target: "X", direction: "upstream"})` |
| `detect_changes` | Pre-commit scope check | `gitnexus_detect_changes({scope: "staged"})` |
| `rename` | Safe multi-file rename | `gitnexus_rename({symbol_name: "old", new_name: "new", dry_run: true})` |
| `cypher` | Custom graph queries | `gitnexus_cypher({query: "MATCH ..."})` |

## Impact Risk Levels

| Depth | Meaning | Action |
|-------|---------|--------|
| d=1 | WILL BREAK — direct callers/importers | MUST update these |
| d=2 | LIKELY AFFECTED — indirect deps | Should test |
| d=3 | MAY NEED TESTING — transitive | Test if critical path |

## Resources

| Resource | Use for |
|----------|---------|
| `gitnexus://repo/synthetic_heart/context` | Codebase overview, check index freshness |
| `gitnexus://repo/synthetic_heart/clusters` | All functional areas |
| `gitnexus://repo/synthetic_heart/processes` | All execution flows |
| `gitnexus://repo/synthetic_heart/process/{name}` | Step-by-step execution trace |

## Self-Check Before Finishing

Before completing any code modification task, verify:
1. `gitnexus_impact` was run for all modified symbols
2. No HIGH/CRITICAL risk warnings were ignored
3. `gitnexus_detect_changes()` confirms changes match expected scope
4. All d=1 (WILL BREAK) dependents were updated

## Keeping the Index Fresh

After committing code changes, the GitNexus index becomes stale. Re-run analyze to update it:

```bash
npx gitnexus analyze
```

If the index previously included embeddings, preserve them by adding `--embeddings`:

```bash
npx gitnexus analyze --embeddings
```

To check whether embeddings exist, inspect `.gitnexus/meta.json` — the `stats.embeddings` field shows the count (0 means no embeddings). **Running analyze without `--embeddings` will delete any previously generated embeddings.**

> Claude Code users: A PostToolUse hook handles this automatically after `git commit` and `git merge`.

## CLI

| Task | Read this skill file |
|------|---------------------|
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
