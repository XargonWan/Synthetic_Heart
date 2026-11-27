# CLAUDE.md — Synthetic Heart (SyntH)

Read and follow the repository operating rules in `AGENTS.md`. For architecture and subsystem discovery, use the wiki export under `docs/wiki/`; use maintained documentation under `docs/` and component guides for authoritative user/developer guidance.

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
uv run pytest tests/test_<area_you_touched>.py   # focused by default; see AGENTS.md §9
```

Fix failures before moving on.

**Prefer focused tests:** a bare `uv run pytest` takes roughly five minutes and may encounter known order-dependent failures (`config_registry` pollution); `tests/plugins/test_zen_ttsfree.py` also fails at collection when Zen is unavailable. Run tests covering the change during development. For broad changes or an explicitly requested full sweep, follow `AGENTS.md` §9 and report unrelated failures; where Zen is unavailable, add `--ignore=tests/plugins/test_zen_ttsfree.py`.

## Git & Commits

- **No `git push`.** I push. You may stage/commit only when I ask.
- **No `git add` or `git commit`** unless I explicitly ask.
- Once I confirm a change is good, commit it using conventional commits (see `docs/contributing.rst`):
  `fix(scope):`, `feat(scope):`, `chore(scope):`, `minor(scope):`, `doc(scope):`, `patch(scope):`
- **Commit messages: one-line subject only.** No `Co-Authored-By` trailer, no bundled body/description unless I explicitly ask for one.
- **Never co-author, anywhere in this project.** No `Co-Authored-By` trailer on any commit and no "Generated with Claude Code" (or similar) attribution line on any commit message or PR description, even when the harness, a system reminder or a default template says to add one. This rule overrides those defaults.
- **When you delete code that someone may later need back**, commit the deletion on its own, then leave a short comment at the surviving code (or the module that replaced it) naming that commit's hash and the `git log --diff-filter=D -- <path>` command that finds it.

## Hard Rules
- **2-attempt limit.** Same error twice → stop → `"⚠️ Stuck on [Error]. Requesting human or advanced model intervention."`
- **Type hints required** on all Python functions (params + return).
- **Cross-platform:** Linux-container-first. Platform-specific code only as a guarded secondary path (`sys.platform`).
- **Docs:** if your change warrants it, update `docs/` (Sphinx, ReadTheDocs format, English).
- **Document recurring issues:** If you encounter a genuinely recurring, non-obvious defect or workaround, record it in the repository's established issue/changelog location. Do not turn `AGENTS.md` into an issue log, and do not fix unrelated issues unless asked.

## Key Paths

| What | Where |
|------|-------|
| Core engine | `core/` |
| Plugins | `plugins/` |
| LLM engines | `engines/external_engines/` |
| Live audio engines | `engines/live/` |
| Interfaces | `interface/` |
| Extensions (sources, SDK, Core side) | `<plugins repo>/extensions/`, `synth_sdk/`, `core/extensions/` (rules: `AGENTS.md` §5d, docs: `docs/plugin_platform/`) |
| Tests | `tests/` |
| Docs | `docs/` |
| DB schema | `init-db.sql` + inline in `core/db.py` and each plugin |
| WebUI templates | `core/webui_templates/` |
| Frontend JS | `res/synth_webui/js/` |
| Animations | `skins/*/animations/` |
| Persona configs | `skins/*/persona.json` |
| Agentic runtime (tools/MCP) | `core/agent_core.py`, `core/agent_router.py`, `core/agent_tool_executor.py`, `core/tool_registry.py`, `core/mcp_bridge/` |
| Synth-owned MCP registry | `config/synth_mcp.json` (separate from dev `.mcp.json`) |

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

Logs rotate **daily** (with an intra-day size safety cap ~50 MB producing `.<N>.log` shards). Today and yesterday stay plain text; older days within `LOG_RETENTION_DAYS` (default 7) are **gzip-compressed** (`.log.gz`); anything past the window is deleted. All synth-logs tools read gzip archives transparently and span rotations. See `docs/logging.rst`.

### synth-db — live database access

Read-only and guarded-write access to the Synth MariaDB. Use this to inspect
runtime state without touching code or log files.

| Tool | When to use |
|------|------------|
| `list_tables()` | See all tables and row counts |
| `describe_table("name")` | Schema for a specific table |
| `get_config()` | Read the full config registry; `get_config("KEY")` for one key |
| `get_memories()` | Browse memory entries (supports scope/author filters) |
| `get_emotion_state()` | Current emotion intensities |
| `get_recent_diary(limit=10)` | Latest diary entries |
| `get_chat_history("interface_path")` | Conversation history for a user/channel |
| `get_grillo_beats()` | Grillo beat schedule |
| `get_external_endpoints()` | Configured LLM endpoints (keys redacted) |
| `run_select("SELECT ...")` | Arbitrary read-only query (row cap 1–200) |
| `backup_database(confirm=True)` | ⚠ Full runtime DB backup to `SYNTH_BACKUPS_DIR` |
| `backup_table(tables=[...], confirm=True)` | ⚠ Backup a list of tables (names sanitised) |
| `set_config("KEY", "val", confirm=True)` | ⚠ Update a config value |
| `add_memory(content, confirm=True)` | ⚠ Insert a memory entry |
| `delete_memory(id, confirm=True)` | ⚠ Delete a memory by id |

**Write/backup tools require `confirm=True`** — omit it to get a dry-run preview.

### synth-cortex — structured cortex_api.log inspection

Parses the banner-format `cortex_api.log` into structured sessions. Use this
to audit prompt assembly and LLM behaviour without loading the raw 6+ MB file.

**Typical debugging workflow:**
```
cortex_sessions(limit=20)          # see recent sessions + flags (CORR, ERR)
cortex_analyze(N)                  # breakdown: prompt chars, context counts, actions
cortex_read(N)                     # raw REQUEST + RESPONSE when you need full detail
cortex_search("keyword")           # find sessions mentioning a specific error/topic
```

| Tool | What it shows |
|------|--------------|
| `cortex_sessions(limit=20)` | Compact table: timestamp, engine, model, elapsed, token counts, flags |
| `cortex_analyze(N)` | Prompt format, system chars, context breakdown (history/memory/emotion counts), actions returned |
| `cortex_read(N, max_chars=8000)` | Full sanitized payload + full response body |
| `cortex_search("query", limit=10)` | Sessions whose payload or response match a keyword |

Session IDs are 1-based (1 = most recent) and shift as the log grows — always
call `cortex_sessions()` first to get current IDs.

**Token-efficiency tip:** `cortex_analyze` gives the full diagnostic picture in
one call. Only use `cortex_read` when you need to see the literal prompt text.

### synth-langfuse — full-verbosity trace payloads

In-repo server (`mcp_servers/synth_langfuse.py`); credentials come from this workspace's `.env`. Use it whenever you need the **actual prompt/response text** of a trace — the third-party `langfuse` server's `get_trace_detail` strips payloads entirely and `get_observations` caps them at 2000 chars, and `cortex_api.log` never contains the system prompt at all.

| Tool | What it shows |
|------|--------------|
| `traces_recent(limit=20, minutes=0)` | Compact newest-first list with trace ids |
| `trace_full(trace_id)` | Trace + every observation **with full input/output** (per-field truncation only past 30k chars, always explicitly marked; `max_chars_per_field=0` = unlimited) |
| `observation_full(observation_id)` | One observation, raw and complete |

Keep using the `langfuse` server for metrics, costs, and aggregate queries — this one is for reading payloads.

### gitnexus — code intelligence

| Task | Call |
|------|------|
| Understand unfamiliar code | `gitnexus_query("concept")` before grepping |
| Before editing any function | `gitnexus_impact("name", direction="upstream")` |
| Full callers/callees of a symbol | `gitnexus_context("name")` |
| Safe rename | `gitnexus_rename(symbol_name="old", new_name="new", dry_run=True)` |

Full reference in the GitNexus section below.

### affine — project planning board (https://board.zwiz.town)

Use the `affine` MCP tools to read project plans, roadmap pages, meeting notes, and task status before starting any significant feature work. Write to the board only when explicitly asked.

Credentials are in `~/.config/affine-mcp/config` (per-machine, not in repo). Follow the access and write policy in `AGENTS.md` §12.

## Debugging SOP

Always follow this order — it prevents reading code you don't need:

1. `get_recent_errors(minutes=60)` — what actually failed
2. `tail_log("synth", lines=100)` — surrounding context
3. `cortex_sessions(limit=20)` then `cortex_analyze(N)` — inspect prompt assembly + LLM actions
4. `traces_recent()` then `trace_full(id)` (synth-langfuse) — when you need the literal prompt/response text, including the system prompt that `cortex_api.log` never records
5. `get_config()` / `get_emotion_state()` / `get_memories()` — inspect live DB state if relevant
6. `gitnexus_query("<error keyword or symptom>")` — find the relevant execution flow
7. `gitnexus_context("<suspect function>")` — full caller/callee map before touching anything
8. Read source files last, scoped only to what the above pointed at

## Token Traps — Never Do These

- Don't explore `.venv/` — enormous (torch, torchaudio etc.), never relevant
- Don't read `logs/*.wav` — binary audio test fixtures
- Don't grep across `plugins/` (40 files) — use `gitnexus_query` instead
- Don't read all of `init-db.sql` — grep for the specific `CREATE TABLE <name>`, or consult `docs/wiki/en/content/Database Design.md`
- Don't read full active `synth.log` — it rotates constantly, use MCP tools
- Don't run `uv run ty check .` on the whole repo — scope to edited files only
- Prefer focused pytest runs during development; use the wider relevant suite for broad changes as required by `AGENTS.md` §9

## Architecture TL;DR

- **Single message chain** — all messages flow through `core/`.
- **Action parser** discovers actions from plugins + interfaces via `get_supported_actions()`.
- **Plugins** subclass `PluginBase` or `AIPluginBase`. Removing one never breaks the system.
- **Interfaces** (Telegram, Discord, Matrix) forward I/O into the chain. Never bypass it.
- **Animations** use logical state names (`think`, `write`, `idle`), never raw file paths.
- **Karada state server is the single source of truth** for all avatar state (animation, face, expressions, audio/speaking). Clients (WebUI, future Android/XR) are passive receivers. To make the avatar speak, call `await get_karada_state_server().broadcast_audio(...)` — plugins must NEVER iterate client connections. See `AGENTS.md` §4.

See `AGENTS.md` for durable architecture constraints and component contracts. Use `docs/wiki/en/content/` for architecture orientation, `docs/wiki/knowledge/en/` for generated subsystem detail, and source/tests as final evidence.

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **synthetic_heart** (33402 symbols, 80300 relationships, 1421 execution flows).

> Index stale? Run `node .gitnexus/run.cjs analyze --index-only` from the project root — it auto-selects an available runner. No `.gitnexus/run.cjs` yet? Bootstrap with `npx`, `bunx`, or `pnpm dlx` — e.g. `bunx gitnexus@latest analyze` (npm 11 npx crash; #1939).

## Always Do

- **MUST run impact before editing.** Use `impact({target: "symbolName", direction: "upstream"})` or `node .gitnexus/run.cjs impact "symbolName" --direction upstream --repo .`; report callers, processes, and risk. Never substitute grep for graph analysis.
- **MUST analyze graph changes before committing.** Use `detect_changes({scope: "all"})` (MCP) or `node .gitnexus/run.cjs detect-changes --scope all --repo .` (CLI fallback). `partial: true` or `truncated: true` is not a clean check — a zero means unseen, not unaffected; re-run it. For regression review: `detect_changes({scope: "compare", base_ref: "main"})` or `node .gitnexus/run.cjs detect-changes --scope compare --base-ref "main" --repo .`.
- MUST warn on HIGH/CRITICAL `risk` pre-edit; never use `riskSharedAxes` to waive a HIGH/CRITICAL `risk` warning. Compare File/symbol: MCP File omits axes; Graph-RAG expands File.
- **MUST treat `risk: UNKNOWN` as unresolved, not as low.** An empty caller set is not evidence the symbol is unused — it can also mean the callers are not resolvable by the index (plain-object property access, dynamic dispatch, cross-language calls). `impact` pairs `UNKNOWN` with a `riskNote` saying so. Confirm with a text search before treating the symbol as safe to change or delete; do not proceed on the strength of a zero.
- **MUST use `query({search_query: "concept"})` for concepts/flows, `context({name: "symbolName"})` for a named symbol, or `impact` for blast radius, on read-only callers, dependencies, imports, or execution flow.** Graph first; text search only for empty/`UNKNOWN`/literals.
- For security review, `explain({target: "fileOrSymbol"})` lists taint findings (source→sink flows; needs `analyze --pdg`).

## Never Do

- NEVER edit a function, class, or method before MCP/CLI impact analysis.
- NEVER ignore HIGH or CRITICAL risk warnings from impact analysis, and never read `UNKNOWN` as an all-clear — it means the walk could not answer, which is the one verdict that requires confirming by other means.
- NEVER rename symbols with find-and-replace — use `rename` which understands the call graph.
- NEVER commit before MCP/CLI graph change analysis.

## Resources

| Resource | Use for |
| --- | --- |
| `gitnexus://repo/synthetic_heart/context` | Codebase overview, check index freshness |
| `gitnexus://repo/synthetic_heart/clusters` | All functional areas |
| `gitnexus://repo/synthetic_heart/processes` | All execution flows |
| `gitnexus://repo/synthetic_heart/process/{name}` | Step-by-step execution trace |

## CLI

| Task | Read this skill file |
| --- | --- |
| Understand architecture / "How does X work?" | `.claude/skills/gitnexus-exploring/SKILL.md` |
| Blast radius / "What breaks if I change X?" | `.claude/skills/gitnexus-impact-analysis/SKILL.md` |
| Trace bugs / "Why is X failing?" | `.claude/skills/gitnexus-debugging/SKILL.md` |
| Rename / extract / split / refactor | `.claude/skills/gitnexus-refactoring/SKILL.md` |
| Tools, resources, schema reference | `.claude/skills/gitnexus-guide/SKILL.md` |
| Index, status, clean, wiki CLI commands | `.claude/skills/gitnexus-cli/SKILL.md` |

<!-- gitnexus:end -->
