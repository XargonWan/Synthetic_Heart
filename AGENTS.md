# AGENTS.md — Synthetic Heart (SyntH)

> Canonical reference for any AI agent working on this codebase.
> Claude Code users: see `CLAUDE.md` for auto-loaded workflow rules.

---

## 1. Project Identity

**Synthetic Heart** (stylized **SyntH**) is a modular AI persona system.
"Synth" is the name of the digital person this project brings to life.

---

## 2. Architecture at a Glance

```
                  ┌──────────────────────────────────────┐
                  │              core/                    │
                  │  message chain · action parser · DB   │
                  │  validation · dispatcher · notifier   │
                  └──┬──────────┬──────────────┬─────────┘
                     │          │              │
              ┌──────┴───┐  ┌───┴────┐   ┌─────┴──────┐
              │ plugins/ │  │ cortex/│   │ interface/ │
              │          │  │ llm_   │   │            │
              │ actions  │  │ engines│   │ Telegram   │
              │ agents   │  │        │   │ Discord    │
              │          │  │ Gemini │   │ Matrix     │
              └──────────┘  │ GPT …  │   │ Ollama API │
                            └────────┘   └────────────┘
```

| Layer | Location | Purpose |
|-------|----------|---------|
| **Core** | `core/` | Message chain, validation, dispatcher, DB, notifier. Never hardcodes plugin/LLM/interface logic. |
| **Plugins** | `plugins/` | Provide actions via `get_supported_actions()`. Subclass `PluginBase` or `AIPluginBase`. |
| **LLM Engines** | `cortex/`, `llm_engines/` | Interchangeable reasoning backends. Subclass `AIPluginBase`. |
| **Interfaces** | `interface/` | I/O adapters (Telegram, Discord, Matrix, Ollama compat). Register actions via `get_supported_actions()`. |

**Golden rule:** removing any plugin, engine, or interface must not break the rest of the system.

---

## 3. Core Principles

- All messages flow through a **single chain** managed by the core.
- Actions must **attach to the existing chain**, never create parallel flows.
- The **action parser** dynamically discovers supported actions by querying plugins and interfaces.
- **Validation rules** are auto-registered from `get_supported_actions()`.
- Plugins are optional — if one is missing, its actions are silently ignored.

---

## 4. Plugin System

Every plugin must implement:

```python
def get_supported_actions(self) -> dict:
    """Return supported actions and their prompt instructions."""
```

Two flavours:
- **Standard** (`PluginBase`): logic without LLM.
- **AI plugins** (`AIPluginBase`): LLM-powered actions.

Optional lifecycle hooks: init, teardown, extended behaviour.

### Background Agents (Grillo)

Some plugins are long-running scheduled agents. The canonical example is **G.R.I.L.L.O.** (`plugins/grillo/`):

- Generates periodic "beats" (introspection prompts) enqueued via `core.message_queue.enqueue_low_priority`.
- DB tables: `grillo_activity_log`, `grillo_beats`, `grillo_action_execs` (see `init-db.sql`).
- Context keys on beats: `grillo_beat`, `beat_type`, `activity_log_id`.
- Configurable via `GRILLO_BEAT_INTERVAL`; includes duplicate suppression and rate-limiting.
- Extensible: discovers beat-specific plugins (tag compactor, memory compactor, curiosity) via the plugin registry.

The **Agent plugin** (`plugins/agent_plugin.py`) gives Synth a controlled hand for external tasks under policy-managed approval modes (`always_approve`, `whitelist`, `always_ask`, `disabled`). Uses `agent_activity_log` and `agent_action_execs` tables.

---

## 5. LLM Engines

- Subclass `AIPluginBase`.
- Handle reasoning, output JSON actions.
- Multiple engines can coexist; hot-swappable.
- Primary: `cortex/llm_engine/` (newer). Legacy: `llm_engines/`.

---

## 5a. Media Subsystems

SyntH has four named media subsystems, each with its own registry, base class, plugin, and WebUI selector.

| Name | Purpose | Registry | Plugin | Config key | Action |
|------|---------|---------|--------|-----------|--------|
| **Cortex** | Text generation / LLM | `core/cortex_registry.py` | — (AI engines) | `BASE_CORTEX` | — |
| **Vox** | Text-to-Speech | `core/vox_registry.py` | `plugins/vox_plugin.py` | `ACTIVE_VOX_ENGINE` | `tts_speak` |
| **Auris** | Speech-to-Text (file-based) | `core/auris_registry.py` | `plugins/auris_plugin.py` | `ACTIVE_AURIS_ENGINE` | `stt_transcribe` |
| **Iris** | Image / Video Understanding | `core/iris_registry.py` | `plugins/iris_plugin.py` | `ACTIVE_IRIS_ENGINE` | `vision_describe` |

### Iris — Vision

Iris handles file-based image and video analysis.

- **Base class**: `plugins/iris_base.py` — `IrisEngineBase(ABC)`, `IrisResult` dataclass.
- **Plugin**: `plugins/iris_plugin.py` — public API: `await iris_plugin.describe_media(file_path, mime_type, prompt, engine_name, model)`.
- **Bridge**: `core/external_endpoints/bridges/iris_bridge.py` — wraps any external endpoint adapter.
- **Adapter method**: `BaseProtocolAdapter.describe_image(image_bytes, mime_type, prompt, model)` — implemented in `openai_compat`, `gemini_adapter`, `anthropic_adapter`.
- **Media dispatcher**: `core/media_dispatcher.py` — Iris is called for `image/*` and `video/*` MIME types (step 2 in the escalation chain, between Auris and Live).
- **Default engine**: `selenium-llm-engine` (pre-set in `init-db.sql`). No local model is bundled.
- **WebUI**: Engine selector appears in the Engines tab (`core/webui_templates/sections/engines.html`), populated from `/api/components` → `iris` key.

Engine authors subclass `IrisEngineBase` and set `ENGINE_CLASS = MyEngine` at module level. Register at import time:

```python
from core.iris_registry import register_iris_engine
register_iris_engine("my_engine", __name__, capabilities={"vision": True}, label="My vision engine")
```

---

## 6. Interfaces

- Manage I/O with external systems.
- Must forward all input into the core message chain and dispatch outputs from it.
- Never bypass the chain.
- Register actions via `get_supported_actions()`.

---

## 7. Animation System

The `AnimationHandler` (`core/animation_handler.py`) manages VRM avatar animations with state-based triggering.

### States & Flow

```
Message received  →  THINK  →  LLM starts  →  WRITE  →  Response sent  →  IDLE
```

Always use logical state names, never raw file paths:

```python
# Correct
await persona_manager.set_animation_state("think", session_id=session_id)

# Wrong — never hardcode paths
await webui.send_animation_command(session_id, "/skins/Rei/animations/Think/Thinking.fbx")
```

### Resolution Order

`skins/<persona>/animations/<state>/` → `skins/Rei/animations/<state>/` (fallback).

### Descriptor Format (`.fbx.json`)

```json
{
  "intro":  { "start_frame": 0,  "end_frame": 15 },
  "loop":   { "start_frame": 16, "end_frame": 60 },
  "outro":  { "start_frame": 61, "end_frame": 90 },
  "fps": 30,
  "play_once": false,
  "lipsync": false,
  "expressions": [
    { "start_frame": 0, "end_frame": 30, "targets": { "eyes_closed": 0.1 }, "source": "descriptor", "priority": 10 }
  ],
  "blink": { "auto": true, "rate_s": 3.5, "intensity": 0.6, "close_ms": 60, "hold_ms": 120, "open_ms": 60 },
  "eye_movement": { "auto": true, "saccade_rate_s": 2.0 }
}
```

Animations without a descriptor get **implicit defaults**: IDLE loops, non-IDLE plays once.

### Plugin-Friendly APIs

| Method | Purpose |
|--------|---------|
| `register_state_animations(state, animations, sequential)` | Override/register state animations |
| `register_state_aliases(aliases)` | Declare alias names for canonical states |
| `set_animation_search_paths(paths)` | Add custom search paths |
| `get_animation_variants(state)` | Returns `{'loop': [...], 'post': [...], 'other': [...]}` |
| `play_animation(state, session_id, ...)` | Play animation for a state |
| `stop_animation(context_id, session_id)` | Stop and return to idle (respects outro) |

### Smart Eye Behaviour

When `eyes_closed > 0.5`, blink and saccade loops are automatically suspended until eyes reopen. No configuration needed.

### Troubleshooting

| Symptom | Likely cause |
|---------|-------------|
| T-pose on start | Missing descriptor or FBX file |
| Wrong animation | Check `get_animation_variants()` discovery |
| Abrupt transition | Descriptor missing `outro` section |
| Unwanted looping | Set `play_once: true` in descriptor |

---

## 8. Development Workflow

### First-time setup after cloning

```bash
uv sync                   # install all dependencies including MCP server deps
npx gitnexus analyze      # build the code intelligence index (one-time, ~1-2 min)
```

MCP servers (`synth-logs`, `synth-db`, `synth-cortex`, `gitnexus`, `affine`) are pre-configured in `.mcp.json` and `.vscode/mcp.json` — no manual setup needed after the above two commands.

> **Affine MCP one-time credential setup:** credentials are stored in `~/.config/affine-mcp/config`, not in the repo. If running on a new machine, write the file (see §8a below).

### §8a. Affine MCP — Project Planning Board

The project planning board lives at **https://board.zwiz.town** (self-hosted AFFiNE instance).
The `affine` MCP server (v1.13.0+) is pre-configured in `.mcp.json` and exposes pages, blocks, and search from that board.

**Agent user:** `agent@synth.io`

**One-time credential setup (per machine):**

```bash
npm install -g affine-mcp-server
```

Then write `~/.config/affine-mcp/config` (mode 600):

```
AFFINE_BASE_URL=https://board.zwiz.town
AFFINE_EMAIL=agent@synth.io
AFFINE_PASSWORD=meme12345
```

Verify with:

```bash
affine-mcp doctor
# Expected: ✓ graphql-auth: agent@synth.io (1 workspace(s))
```

**When to use the Affine MCP:**

| Task | Use it when… |
|------|-------------|
| Check project plans / roadmap | User asks about what's planned or in-progress |
| Read meeting notes / decisions | Background context on a design decision |
| Look up task status | Before starting a feature to see if it's already tracked |
| Create/update pages | User explicitly asks to update the planning board |

**Never** write to the board without explicit user instruction — it's a shared planning space.

---

### Toolchain: Astral (`uv` + `ruff` + `ty`)

| Task | Command |
|------|---------|
| Sync/install deps | `uv sync` |
| Add a package | `uv add <package>` |
| Add a dev tool | `uv add --dev <tool>` |
| Format | `uv run ruff format .` |
| Lint + autofix | `uv run ruff check --fix .` |
| Type check (scoped) | `uv run ty check path/to/file.py` |
| Run tests | `uv run pytest` |

**Never use `pip install` or `python -m venv`.** They break the lockfile.

### Validation Sequence (mandatory before marking any task done)

1. `uv run ruff format .`
2. `uv run ruff check --fix .`
3. `uv run ty check <files_you_edited>` — scoped only, never the whole repo.
4. `uv run pytest`

If any step fails, fix it before proceeding.

### Hard Rules

- **No `git push`.** Stage and commit locally if asked. The human pushes.
- **No `git add` or `git commit`** unless the developer explicitly asks.
- **2-attempt limit.** If the same error persists after 2 fix attempts, stop and output:
  `"⚠️ Stuck on [Error]. Requesting human or advanced model intervention."`
- **Type hints required.** All Python functions need complete annotations (params + return).
- **Cross-platform policy.** Default runtime is Linux containers. No Windows/macOS-specific primary code paths. Platform-specific logic only as a secondary, guarded case (`sys.platform`).

---

## 9. Testing

- All persistent tests go in `tests/`. Throwaway tests may live at the repo root but must be deleted when done.
- Config: `pytest.ini` — `asyncio_mode = auto`, markers: `asyncio`, `slow`, `integration`.
- Run: `uv run pytest`

### Testing via Ollama API

The Ollama-compatible API (port 11435) can be used for quick testing without Telegram/Discord:

```bash
curl -X POST http://localhost:11435/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "default",
    "messages": [
      { "role": "system", "content": "Respond with ONLY valid JSON: {\"actions\": [...]}" },
      { "role": "user", "content": "Your test message" }
    ],
    "stream": false
  }'
```

Monitor: `docker exec synth-dev tail -f /app/logs/synth.log | grep -E "run_action|execute_action"`

---

## 10. Documentation

- Location: `docs/` (Sphinx, ReadTheDocs format, English).
- Evaluate whether your changes require a docs update. If they do, update docs as part of the task.

---

## 11. Container & Infrastructure Notes

**Dev container restart:**
```bash
docker compose -f docker-compose-dev.yml --env-file .env-dev up -d --build && rm -rf logs/dev/*
```

**Selkies TLS:** HTTPS on container port 3001, HTTP on 3000. Self-signed certs at `/config/ssl/`.

**Grillo monitoring:**
```bash
docker exec synth-dev tail -f /app/logs/synth.log | grep -E "\[grillo\]|grillo"
```

## 12. Known Issues & Recurring Errors

> **Agent instruction:** When you encounter a bug, error pattern, or non-obvious workaround that isn't already listed here, append a new entry before finishing your session. Use the format below. Do **not** fix it unless asked — the point is to stop future agents from wasting tokens rediscovering it.
>
> ```
> ### Short title  <!-- YYYY-MM-DD -->
> **Symptom:** what shows up in logs or at runtime
> **Location:** file(s) involved
> **Status:** known / in progress / workaround in place
> **Notes:** anything that helps the next agent understand it fast
> ```

---

### `ai_diary` — user_message column overflow  <!-- 2026-04-13 -->
**Symptom:** `(1406, "Data too long for column 'user_message' at row 1")` appearing repeatedly in `synth.log`, originating from `ai_diary.py` `_upsert_diary_impl`.
**Location:** `plugins/ai_diary.py`, `init-db.sql` (`ai_diary` table, `user_message` column)
**Status:** known, not fixed — seen multiple times per hour during active sessions.
**Notes:** Diary entries can exceed the column's declared length. The insert fails silently (error is logged, execution continues). No data loss to the user but diary entries are dropped.

---

### `synth.log` rotates extremely fast in DEBUG mode  <!-- 2026-04-13 -->
**Symptom:** Active `synth.log` has only a handful of lines; most content is in timestamped rotation files (`synth.2026-04-12_HH-MM-SS.log`).
**Location:** `core/logging_utils.py` (`maxLines=2000` in `TimestampedRotatingFileHandler`)
**Status:** by design — 2000 lines fills in 1–2 interactions at DEBUG level.
**Notes:** Always use `lookback_files` parameter in the `synth-logs` MCP tools. `tail_log` and `search_logs` default to `lookback_files=2` and `lookback_files=3` respectively. `get_recent_errors` uses 5. Increase if you need more history.

---

### `cortex_api.log` is section-format, not standard  <!-- 2026-04-13 -->
**Symptom:** Searching `cortex_api` via MCP returns truncated banner lines only; full LLM payloads are cut at 400 chars.
**Location:** `logs/cortex_api.log`, `mcp_servers/synth_logs.py` (`_LARGE_PAYLOAD_FILES`)
**Status:** known limitation — no structured parser tool exists yet for this format.
**Notes:** The file uses `==` and `--` banner sections (`REQUEST`, `RESPONSE`, `SEND`, `RECV`). Level/time filters don't work on it. For LLM debugging, search for the banner headers (e.g. `search_logs("REQUEST", log_files=["cortex_api"])`) to find timestamps, then correlate with `synth.log` by time.

---

### `check_logs.py` plugin is stale  <!-- 2026-04-13 -->
**Symptom:** Synth's own `get_logs`/`search_logs` chat actions use hardcoded `/app/logs`, old filenames (`selkies.log`, `prompt_cycle.log`), and only know about 3 numbered rotations.
**Location:** `plugins/check_logs.py`
**Status:** known, not fixed.
**Notes:** For agents, use the `synth-logs` MCP server instead — it handles all rotation schemes. The plugin only matters for Synth herself using log commands during operation.

---

### Corrector returns empty when `successful_actions = []`  <!-- 2026-04-13 -->
**Symptom:** LLM outputs actions without a `type` field (e.g. `{"arousal": 5}`). The unsupported-action check fires, sets `correction_context` with `successful_actions=[]`, and calls the corrector. The corrector told the LLM "PARTIAL SUCCESS — 0 actions succeeded, do NOT repeat successful ones", which was self-contradictory and caused the LLM to return an empty string all 4 attempts → fallback '😵'.
**Location:** `core/transport_layer.py` → `run_corrector_middleware`, the `if correction_context:` block that builds `correction_message_text`.
**Status:** fixed — when `successful_actions` is empty the corrector now uses a `CORRECTION NEEDED` prompt asking the LLM to resend the full response, not the "PARTIAL SUCCESS" / "do not repeat" wording that misled it.
**Notes:** Also added `"Every action object inside 'actions' MUST have a 'type' field"` to `strict_requirements`. The root trigger is the LLM emitting bare dict actions like `{"arousal": 5}` or `{"feelings": {...}}` without a `type` key; the strict requirement now explicitly prohibits this.

---

### `test_corrector_on_top_level_message.py` — fake corrector never called  <!-- 2026-04-13 -->
**Symptom:** `test_corrector_invoked_when_top_level_message_without_message_action` fails with `AssertionError: assert 'context' in {}` — the `called` dict is empty because the fake was never invoked.
**Location:** `tests/test_corrector_on_top_level_message.py`
**Status:** pre-existing, not fixed.
**Notes:** The test patches `core.transport_layer.run_corrector_middleware` but `core.action_parser` imports the function at module level (`from core.transport_layer import run_corrector_middleware`), so the patch doesn't intercept calls made from inside `action_parser`. The test also needs to patch `core.action_parser.run_corrector_middleware`. Separately, in some test environment configurations `use_animation` is resolved as a registered action (PersonaManager is loaded), causing `corrector_orchestrator` to exit early with "Actions executed successfully" before selective correction even fires.

---

### GitNexus MCP server fails to start in some VS Code sessions  <!-- 2026-04-17 -->
**Symptom:** Calls to GitNexus MCP tools return `MCP server could not be started: Process exited with code 1`.
**Location:** VS Code MCP runtime / `gitnexus` server startup (not tied to a single repo file).
**Status:** known, intermittent.
**Notes:** When this occurs, agents cannot run `gitnexus_query` / `gitnexus_impact` / `gitnexus_context`. Use fallback discovery (`grep_search`, `file_search`, symbol/reference tools) and keep edits conservative until MCP health is restored.

---

### `test_openrouter_engine.py` uses stale import patch targets  <!-- 2026-04-17 -->
**Symptom:** Multiple tests fail with `ModuleNotFoundError: No module named 'cortex'` when patching paths like `cortex.llm_provider.openrouter.*`.
**Location:** `tests/test_openrouter_engine.py` patch targets; current engine module lives under `engines/external_engines/openrouter.py`.
**Status:** fixed.
**Notes:** Patch targets now point to `engines.external_engines.openrouter.*`. Suite also reflects current document-attachment handling (`application/pdf`) in multimodal extraction.

---

### `grillo_outreach` may route to invalid chat id `-1`  <!-- 2026-04-17 -->
**Symptom:** Outreach scheduler starts and enqueues beats, but messages are sent to `interface_path` values like `synth_webui/-1` and do not appear in active WebUI sessions (e.g. `webui_default`).
**Location:** `plugins/grillo/grillo_outreach.py` target resolution in `_get_target_interface_and_chat`; fallback query over `chat_history_cache` can recover stale interface paths.
**Status:** fixed.
**Notes:** Resolution now rejects sentinel chat IDs (`-1`, empty, `none`, `null`) and prefers explicitly configured `GRILLO_OUTREACH_CHAT_IDS` before DB fallback. If outreach appears silent, check for warnings like `no active websocket for session -1` in `synth.log`.

---

### google-genai async close can raise `_async_httpx_client` AttributeError  <!-- 2026-04-17 -->
**Symptom:** Console shows `Task exception was never retrieved` with `BaseApiClient.aclose()` failing: `AttributeError: 'BaseApiClient' object has no attribute '_async_httpx_client'`.
**Location:** google-genai SDK cleanup path (`google/genai/_api_client.py`) triggered from project client instances in `engines/external_engines/gemini_api.py`, `core/live_session_manager.py`, `plugins/live_engines/gemini.py`, and `core/external_endpoints/adapters/gemini_adapter.py`.
**Status:** fixed in-project workaround.
**Notes:** Added `core/genai_client_utils.py` and apply `harden_genai_client_for_async_close(...)` immediately after each `genai.Client(...)` construction. It injects a no-op async close target when missing, preventing unhandled shutdown tasks on affected SDK builds.

---

### Langfuse response may attach to wrong request when model label drifts  <!-- 2026-04-18 -->
**Symptom:** Some traces show request metadata only (missing output/error/status/elapsed), while nearby traces can look mismatched during concurrent calls.
**Location:** `core/cortex_api_logger.py`, `_pop_langfuse_request` fallback behavior.
**Status:** fixed.
**Notes:** Previous fallback popped the newest stack item even when `engine/model` did not match, which could orphan the correct request. Matching now uses: (1) exact `engine+model`, (2) same `engine`, and otherwise returns `None` without popping unrelated requests.

---

### `emotion_diary` legacy schema truncates low intensities to zero  <!-- 2026-04-18 -->
**Symptom:** `emotion_diary` appears dominated by zero-intensity rows even while `emotion_state` has non-zero baseline values (e.g. `0.1` for low emotions).
**Location:** MariaDB table `emotion_diary` (legacy schema), `plugins/emotion_manager.py` (`_log_emotion_diary_entry` / `set_emotion`).
**Status:** known, not fixed.
**Notes:** Some deployments still use a legacy `emotion_diary` schema with `id varchar(100)` and `intensity int(11)` (no `timestamp`). The emotion manager writes floats (including baseline `0.1`), but DB coercion stores these as `0`, creating misleading analytics. The plugin's schema-adaptive insert avoids crashes but does not prevent numeric truncation.

---

### `grillo_activity_log.diary_entry_id` may reference missing `ai_diary` rows  <!-- 2026-04-18 -->
**Symptom:** PostgreSQL staging migration can fail on `grillo_activity_log_diary_entry_id_fkey` with errors like `Key (diary_entry_id)=(1566) is not present in table "ai_diary"`.
**Location:** MariaDB source data in `grillo_activity_log` vs `ai_diary`; migration handling in `core/main_db_migration.py`.
**Status:** fixed in-project workaround.
**Notes:** The real `3306` source contained 24 orphaned `grillo_activity_log.diary_entry_id` values. The migration now preserves those activity rows but writes the broken `diary_entry_id` references as `NULL` in PostgreSQL, matching the target FK policy (`ON DELETE SET NULL`) and allowing the rest of the dataset to load.

---

### Postgres compat release path can emit unawaited `Pool.release` warnings  <!-- 2026-04-18 -->
**Symptom:** Runtime smoke against Postgres logs `RuntimeWarning: coroutine 'Pool.release' was never awaited` during connection cleanup.
**Location:** `core/db_backends.py` (`PostgresCompatConnection.close`), `core/db.py` (`release_conn`, `_ConnProxy.close`).
**Status:** fixed.
**Notes:** `asyncpg.Pool.release(...)` is awaitable. The compat close path now returns the release result instead of consuming it synchronously, and `release_conn()` awaits any awaitable close/release result so Postgres cleanup stays warning-free.

---

### Proxy cursors can break `async with` via delegated `__aenter__` lookup  <!-- 2026-04-18 -->
**Symptom:** Startup preflight logs `[db] ensure_plugin_tables failed: '_ProxyCursor' object does not support the asynchronous context manager protocol` even though the inner cursor supports async context methods.
**Location:** `core/db.py` (`ensure_plugin_tables` local `_cursor_ctx` helper, `_ConnProxy.cursor` proxy wrappers).
**Status:** fixed.
**Notes:** Special-method lookup for `async with` bypasses `__getattr__`, so proxy cursors that only delegate `__aenter__`/`__aexit__` cannot be used directly in `async with`. The preflight helper now calls the delegated enter/exit methods explicitly when present.

---

### `ai_diary` merge query still uses MySQL `GROUP_CONCAT ... SEPARATOR` syntax  <!-- 2026-04-18 -->
**Symptom:** Runtime logs show `syntax error at or near "SEPARATOR"` during diary merge/debrief paths.
**Location:** `plugins/ai_diary.py` (`DiaryPlugin.on_debrief`, query around `_get_unmerged_entries`).
**Status:** known, not fixed.
**Notes:** PostgreSQL rejects MySQL's `GROUP_CONCAT(... SEPARATOR ...)` form. The current query needs a Postgres equivalent such as `string_agg(...)` on the Postgres path.

---

### `scheduled_events.delivered = 0` breaks on Postgres boolean columns  <!-- 2026-04-18 -->
**Symptom:** Event scheduler logs `UndefinedFunctionError('operator does not exist: boolean = integer')` while polling due events.
**Location:** `core/db.py` (`get_due_events`, query `WHERE delivered = 0 AND next_run <= %s`).
**Status:** known, not fixed.
**Notes:** The migrated Postgres schema uses a boolean for `delivered`, but the query still compares it to integer `0`. The Postgres path should query with `delivered = FALSE` (or equivalent boolean-safe SQL).

---

### `emotion_manager` can mix offset-aware DB timestamps with naive `datetime.now()`  <!-- 2026-04-18 -->
**Symptom:** Runtime logs show `Error getting emotion state: can't subtract offset-naive and offset-aware datetimes`.
**Location:** `plugins/emotion_manager.py` (`get_emotion_state`, `get_all_emotion_states`, and related decay logic using `datetime.now()` against DB timestamps).
**Status:** known, not fixed.
**Notes:** On Postgres, fetched timestamps may be timezone-aware while local comparisons still use naive `datetime.now()`. The emotion state path needs a consistent timezone policy before subtracting timestamps.

---

### `schedule_message send_at` path imports missing `get_local_tz` helper  <!-- 2026-04-18 -->
**Symptom:** Absolute-time reminders can fail before scheduling with an import error when `schedule_message.payload.send_at` is used.
**Location:** `plugins/event_plugin.py` (`_handle_schedule_message_payload`, import `from core.time_zone_utils import get_local_tz`).
**Status:** known, not fixed.
**Notes:** There is no `get_local_tz` symbol in `core.time_zone_utils`. Relative-delay scheduling (`send_in`) is unaffected, but `send_at` parsing needs to use an existing timezone helper or inline timezone resolution.

---

### `event_plugin` interface-path reminder delivery still calls stale `run_action` signature  <!-- 2026-04-18 -->
**Symptom:** Reminder delivery via `interface_path` can log a `run_action()` argument error instead of sending the message.
**Location:** `plugins/event_plugin.py` (`_send_via_interface_path`) vs `core/action_parser.py` (`run_action(action, context, bot, original_message)`).
**Status:** known, not fixed.
**Notes:** The call site still uses the old two-argument form (`run_action(action, message)`). This path needs the same context/bot/original-message signature update that other callers already received.

---

### `test_selenium_ttsfree.py` blocks broad pytest without optional Selenium dependency  <!-- 2026-04-18 -->
**Symptom:** `uv run pytest` can fail during collection with `ModuleNotFoundError: No module named 'selenium'` from `tests/plugins/test_selenium_ttsfree.py` after it falls back to `plugins_dev.selenium_ttsfree`.
**Location:** `tests/plugins/test_selenium_ttsfree.py`, `plugins_dev/selenium_ttsfree.py`
**Status:** known, not fixed.
**Notes:** Environments without the optional Selenium package cannot collect this test module. For broad regression sweeps, either install `selenium` or ignore this file explicitly (for example `uv run pytest --ignore=tests/plugins/test_selenium_ttsfree.py`).

---

### `send_message` alias rewrite could trigger avoidable correction on `body` payloads  <!-- 2026-04-18 -->
**Symptom:** Langfuse traces can show an initial LLM reply like `{"type":"send_message","payload":{"body":"..."}}` that is semantically fine, but validation fails after internal normalization rewrites the type to `message_telegram_bot` without a canonical `payload.text` field. The corrector retry then succeeds, making the first correction look unnecessarily "stupid".
**Location:** `core/message_chain.py`, LLM-originated action normalization before validation.
**Status:** fixed.
**Notes:** The message chain now promotes legacy text aliases (`body`, `content`, `message`, `value`) into `payload.text` before validation and reruns `interface_path` injection after rewriting generic `message` / `send_message` actions to concrete `message_*` types.

---

### OpenAI-compatible external endpoint probe ignored configured adapter timeout  <!-- 2026-04-19 -->
**Symptom:** Fresh OpenAI-compatible endpoints (for example OpenRouter) can log `ping_test exception ... Connection timeout` and `list_models HTTP fallback failed ... TimeoutError()` during auto-probe even when the endpoint-level probe timeout is configured much higher, leaving `available_models` empty in the UI.
**Location:** `core/external_endpoints/adapters/openai_compat.py` (`_list_models_via_http`, `ping_test`), `core/external_endpoints/probe.py` timeout plumbing.
**Status:** fixed.
**Notes:** The adapter was built with `endpoint.extra_config.timeout`, but `list_models()` still hardcoded a 40s HTTP timeout and `ping_test()` defaulted to 30s with a 10s connect timeout unless a caller overrode it manually. The adapter now uses its configured timeout for both model discovery and ping probes by default.

---

### `external_endpoints.updated_at` string writes can fail on Postgres-backed endpoint registry paths  <!-- 2026-04-19 -->
**Symptom:** Runtime warnings like `ext endpoint model DB persist failed: invalid input for query argument $2: '2026-04-18 22:08:45' (expected a datetime.date or datetime.datetime instance, got 'str')` can appear when saving endpoint state such as default model selection.
**Location:** `core/external_endpoints/registry.py` (`update_endpoint`, `set_subsystem_map`, `_auto_set_default_model`, `set_default_model`).
**Status:** fixed.
**Notes:** Several registry writes formatted `updated_at` as a string while other paths already passed real timezone-aware `datetime` objects. The registry now binds real UTC datetimes consistently, matching the Postgres-compatible probe result path.

---

### Queued trainer notifications could lose `skip_history` and pollute prompt context  <!-- 2026-04-19 -->
**Symptom:** Engine-switch notifications like `✅ Cortex engine dynamically updated to ...` could appear in `chat_history_cache` / `history_recent`, especially during startup or interface registration races.
**Location:** `core/notifier.py` (`flush_pending_for_interface`), `core/history_engine.py`.
**Status:** fixed.
**Notes:** The direct `notify_trainer()` path already sent `skip_history=True`, but queued trainer notifications were flushed later without that flag. The flush path now preserves `skip_history`, and the history builder ignores these legacy self-notification rows so old DB pollution stops affecting prompt context.

---

### `ai_diary` consolidation could recursively re-merge whole days and bloat Gemini prompts  <!-- 2026-04-19 -->
**Symptom:** Langfuse shows very large `@diary_merge` prompts with repeated diary fragments, and Gemini generations can stretch into ~100s while prompt reduction still fails to get under the size cap.
**Location:** `plugins/ai_diary.py` (`DiaryPlugin.on_debrief`, `DiaryPlugin.execute_action` for `update_diary_entry`).
**Status:** fixed.
**Notes:** The consolidation beat grouped all rows for a day, updated only one row, and left the source rows in place; subsequent merges could concatenate the already-merged blob plus the originals again. The merge beat now carries exact source row IDs and the original merge timestamp through context, and `update_diary_entry` archives the merged source fragments after writing the consolidated row.

---

### OpenAI-compatible image turns could be silently downgraded to text after a stale probe  <!-- 2026-04-19 -->
**Symptom:** Image-only or image-plus-text turns reach prompt construction with attachments present, but the external endpoint request contains only text parts, so OpenRouter-compatible models appear to ignore the image.
**Location:** `core/external_endpoints/bridges/cortex_bridge.py`, `core/external_endpoints/adapters/openai_compat.py`, `core/external_endpoints/probe.py`.
**Status:** fixed.
**Notes:** Fresh probes could persist `capabilities["vision"] = false` and `capabilities["cortex"] = false` when `ping_test()` / `_probe_vision_support()` fell back to the invalid model name `"default"`. The Cortex bridge then trusted the stale endpoint-level `vision` flag and silently stripped `image_url` parts even after the user selected a real model. Probes now resolve a concrete model before sending test requests, and the bridge forwards image parts when a vision mapping or explicit model selection exists so multimodal turns are not silently flattened.

---

### OpenAI-compatible image-only turns could hallucinate non-visible details  <!-- 2026-04-19 -->
**Symptom:** Requests with a real `image_url` attachment can still produce invented details (for example a nonexistent blindfold) when the user sends only an image and no caption.
**Location:** `core/prompt_renderers.py` (`OpenAIRenderer.render_with_multimodal`).
**Status:** fixed.
**Notes:** The multipart current-turn text companion previously contained only the runtime prefix when `current_text` was empty, leaving the model free to fill gaps from prior chat context. The OpenAI renderer now adds an explicit grounding instruction for image attachments, telling the model to describe only clearly visible details and to admit uncertainty for ambiguous content.

---

### SOUL `async_consolidate` ran on every idle compile  <!-- 2026-04-19 -->
**Symptom:** Langfuse shows frequent `async_consolidate` traces that look like diary-consolidation churn, often on every idle transcript flush.
**Location:** `plugins/soul_plugin.py` (`SoulPlugin._compile_interface`).
**Status:** fixed.
**Notes:** The SOUL plugin called `self._compiler.async_consolidate()` after every `post_session_compile()`, so routine idle compiles emitted consolidation work and traces far too often. The plugin now throttles background consolidation with a cooldown while `soul_force_compile` still bypasses the cooldown for explicit manual compiles.

---

### SOUL Postgres recall could full-scan memcells and trip static injection timeouts  <!-- 2026-04-19 -->
**Symptom:** Runtime logs show `get_static_injection() on SoulPlugin timed out after 5s` even though memcells and embeddings are present in Postgres; direct profiling shows `PostgresSoulRepository.recall_memories()` taking about 5.2 seconds on warm calls.
**Location:** `core/soul/repository.py` (`PostgresSoulRepository.recall_memories`), SOUL Postgres tables `mem_cells` / `mem_cell_vectors`.
**Status:** fixed.
**Notes:** The lexical fallback query used unindexed `atomic_facts::text` trigram checks plus a composite `to_tsvector(episodic_trace || atomic_facts::text)` expression, forcing a sequential scan over `mem_cells`. Candidate selection now stays on indexed `episodic_trace` trigram/tsvector expressions and computes richer episodic-trace-plus-atomic-facts lexical overlap in Python after fetch. Live probe on `2026-04-19` dropped warm repo recall from about `5.2s` to about `3.3s` end-to-end static injection. When verifying SOUL state, query the separate `SOUL_POSTGRES_DSN` store directly; the `synth-db` MCP points at the legacy `synth` schema and cannot see `mem_cells`.

---

### SOUL recall could inject diary-merge housekeeping into live prompts  <!-- 2026-04-19 -->
**Symptom:** User-facing prompt context could include `soul_recalled_memories` entries such as `[DIARY CONSOLIDATION - INTERNAL SYSTEM TASK] ...` or `Performed update_diary_entry action`, leaking maintenance-only traces into normal chat turns.
**Location:** `plugins/soul_plugin.py` (`SoulPlugin._recall_memories`).
**Status:** fixed.
**Notes:** SOUL memcells do not carry an explicit internal-task flag, so the live prompt path now filters diary-merge and nightly housekeeping traces before formatting recalled memories. This keeps normal same-chat recall intact while excluding consolidation-only prompt noise.

---

### Selective correction context could store counts instead of action lists  <!-- 2026-04-19 -->
**Symptom:** `corrector_middleware` could log `object of type 'int' has no len()` or `'int' object is not iterable` immediately after `Using payload_thread_id=...`, then exhaust retries without returning corrected JSON.
**Location:** `core/action_parser.py` (`_request_selective_correction`), `core/transport_layer.py` (`run_corrector_middleware`).
**Status:** fixed.
**Notes:** `_request_selective_correction` stored integer counts in `correction_context.successful_actions` / `failed_actions`, but the transport-layer corrector expects iterable action/error records when building the selective-correction prompt. The producer now stores the real action lists plus explicit `successful_count` / `failed_count` fields, and the consumer defensively normalizes legacy malformed contexts.

---

### Telegram multimodal extraction assumed every optional media attribute exists  <!-- 2026-04-19 -->
**Symptom:** Runtime logs could show `Error extracting Telegram attachments: 'types.SimpleNamespace' object has no attribute 'photo'`, and attachment extraction aborted before reaching later media fields.
**Location:** `core/multimodal_attachment.py` (`extract_multimodal_from_telegram`).
**Status:** fixed.
**Notes:** The Telegram extractor read `message.photo`, `message.document`, `message.audio`, `message.voice`, `message.video`, `message.video_note`, and `message.sticker` directly. Partial PTB message objects and test doubles do not guarantee those attributes exist. The extractor now uses `getattr(..., None)` for every optional field and treats missing sticker flags as false.

---

### Async diary commands called sync diary retrieval on the event-loop thread  <!-- 2026-04-19 -->
**Symptom:** Trainer diary commands could trip sync/async bridge errors while fetching diary entries from an async context, and `core/generic_commands.py` also imported a stale helper name (`last_chats_command_generic`) that no longer existed in `core.recent_chats`.
**Location:** `core/command_registry.py` (`diary_command`, `context_command`), `core/generic_commands.py` (`generic_diary_command`, import of `last_chats_command_generic`).
**Status:** fixed.
**Notes:** The async command handlers now offload `get_recent_entries(...)` via `asyncio.to_thread(...)` instead of calling the sync diary bridge directly on the active loop thread. `generic_commands` now imports `last_chats_command` under the expected alias, and `command_registry.context_command` was aligned with the actual `core.context` API (`set_context_state` / `get_context_state`).

---

### Recovered JSON with extra trailing content could drop later actions silently  <!-- 2026-04-20 -->
**Symptom:** Runtime logs could show `JSON recovered after ... parsing errors`, followed by a successful `message_*` action execution and `All actions executed successfully despite JSON recovery`, even though the raw LLM response still contained additional malformed actions later in the payload. The recovered first action would run, but later diary/emotion/animation actions could be lost without a correction pass.
**Location:** `core/message_chain.py` (`handle_incoming_message` recovery/correction branch), `core/transport_layer.py` (`extract_json_from_text`).
**Status:** fixed.
**Notes:** The message chain now treats `recovered=True` plus retained extra text as a selective-correction case, even when the salvaged actions themselves executed successfully. This preserves already-run actions while asking the corrector for the dropped remainder instead of silently terminating the loop.

---

### External cortex bridge dropped PromptRequest and fell back to legacy JSON flattening  <!-- 2026-04-20 -->
**Symptom:** External endpoint-backed cortex engines (for example OpenRouter via `ExternalCortexEngine`) could log classic `system + giant user blob` requests even when `build_prompt_request()` had attached a `__prompt_request`. MCP traces showed large serialized prompt dicts in the last user turn instead of the renderer's structured messages.
**Location:** `core/plugin_instance.py` (`prompt.pop("__prompt_request", None)` handoff), `core/external_endpoints/bridges/cortex_bridge.py` (`ExternalCortexEngine`).
**Status:** fixed.
**Notes:** `plugin_instance` only forwards the typed prompt object when the resolved engine advertises `supports_prompt_request`. The bridge already knew how to render `PromptRequest`, but did not set the flag, so the typed object was stripped and the bridge always fell back to the legacy dict path. `ExternalCortexEngine.supports_prompt_request = True` now keeps the typed prompt alive end-to-end.

---

### External OpenAI-compatible PDF attachments were serialized as image parts  <!-- 2026-04-20 -->
**Symptom:** Uploading a PDF manual through an external OpenAI-compatible cortex endpoint (for example OpenRouter → xAI Grok) could fail with `Invalid request content: Invalid base64-encoded image.` MCP traces showed the last user turn containing `{"type":"image_url","image_url":{"url":"data:application/pdf;base64,..."}}`.
**Location:** `core/external_endpoints/bridges/cortex_bridge.py` (`ExternalCortexEngine._format_mm_part`, `_build_mm_parts_from_prompt_request`), `core/prompt_renderers.py` (`_build_multimodal_turn_text`, `OpenAIRenderer.render_with_multimodal`).
**Status:** fixed.
**Notes:** The external bridge treated every non-Gemini binary attachment as an OpenAI `image_url` data URI, so PDFs were mislabeled as images and rejected by providers that validate image content. OpenAI-compatible document attachments are now converted into document-aware prompt context: extracted document text is injected into the final user turn when available, and image-only/scanned PDFs fall back to attached page images plus explicit prompt guidance so vision-capable models can read visible text from the document pages. Gemini endpoints still receive native inline document data.

---

### External endpoint adapters still do not use native tool calls end-to-end  <!-- 2026-04-20 -->
**Symptom:** Even after PromptRequest rendering, external endpoint-backed cortex engines can still rely on freeform JSON-in-text responses instead of native tool calls. MCP traces for external OpenRouter-backed turns may show `messages` only, with no observable `tools` payload, and malformed multi-action JSON can still occur.
**Location:** `core/external_endpoints/bridges/cortex_bridge.py` (`generate_response` only forwards `messages`), `core/external_endpoints/adapters/openai_compat.py` (`chat_completion` returns `message.content` only, no tool-call parsing), plus other external adapters.
**Status:** known, not fixed.
**Notes:** The PromptRequest handoff bug is fixed, so external bridges now render structured messages, but the external adapter stack still lacks a full tool-declaration and tool-call-response path comparable to the built-in OpenRouter engine. Until that lands, external endpoints remain vulnerable to malformed text JSON in multi-action replies.

---

### Selective correction retried safety-blocked actions and wasted an extra LLM call  <!-- 2026-04-20 -->
**Symptom:** After a user-visible `message_*` action succeeded, logs could still show a second OpenRouter call from `corrector_middleware` that returned `{"actions":[]}` because the only remaining failed action was already marked `unfixable` (for example `update_emotion_state` blocked by safety policy / whitelist).
**Location:** `core/action_parser.py` (`_request_selective_correction`).
**Status:** fixed.
**Notes:** `_request_selective_correction()` now filters out failed actions marked `unfixable` before building the correction prompt. If every failed action is unfixable, the helper skips the corrector entirely instead of burning an extra round-trip that can only return an empty action list.

---

### Literal newlines inside JSON strings could trigger a spurious corrector round-trip  <!-- 2026-04-20 -->
**Symptom:** Runtime logs could show a raw LLM reply that already looks like `{"actions":[...]}`, followed by repeated `Invalid control character` parse errors and `LLM returned non-JSON output; activating corrector to request JSON format`. A second LLM call then re-emits the same message with properly escaped newlines.
**Location:** `core/transport_layer.py` (`extract_json_from_text`).
**Status:** fixed.
**Notes:** Some external OpenAI-compatible models can emit literal newline, carriage-return, or tab characters inside quoted `payload.message` / `payload.text` strings instead of escaped JSON sequences. `extract_json_from_text()` now tries an additional variant that escapes control characters only while inside JSON string literals, so otherwise-valid action payloads recover locally without falling into the corrector loop.

---

### Legacy `diary_entry` action alias could trigger an avoidable correction hop  <!-- 2026-04-20 -->
**Symptom:** OpenRouter-backed manual turns could return a mostly valid action list such as `send_message` + `update_emotion_state` + `diary_entry`, then log `Detected unsupported action types from LLM: ['diary_entry']` and spend one extra corrector request only to rename the diary action to `create_personal_diary_entry`.
**Location:** `core/message_chain.py` (`handle_incoming_message` normalization path before unsupported-action validation).
**Status:** fixed.
**Notes:** The message chain already normalized generic message aliases, but it did not rewrite the legacy diary action name before checking supported action types. `diary_entry` is now normalized in-place to `create_personal_diary_entry`, so mixed manual replies can execute directly without a correction round-trip.

---

### Legacy `diary` action and diary payload aliases could still force correction or drop diary metadata  <!-- 2026-04-20 -->
**Symptom:** OpenRouter-backed manual turns could recover into a valid action list such as `send_message` + `update_emotion_state` + `diary`, then still log `Detected unsupported action types from LLM: ['diary']` and make one avoidable correction call. Even when the corrector renamed the action to `create_personal_diary_entry`, payload keys like `entry`, `summary`, and `thought` could bypass the diary plugin's canonical `interaction_summary` / `personal_thought` fields.
**Location:** `core/message_chain.py` normalization helpers before unsupported-action validation and action execution.
**Status:** fixed.
**Notes:** The message chain now normalizes both legacy diary action names (`diary`, `diary_entry`) and legacy diary payload keys (`entry`, `summary`, `thought`) into the canonical diary action schema before validation. This lets recovered manual replies execute without a corrector hop and preserves diary metadata for downstream diary creation.

---

### Standalone `thought` action could still force correction instead of populating diary metadata  <!-- 2026-04-20 -->
**Symptom:** OpenRouter-backed manual turns could return `send_message` + `diary` + `thought`, log `Detected unsupported action types from LLM: ['thought']`, and spend a correction round-trip only to drop the reflective thought from the final action list.
**Location:** `core/message_chain.py` diary normalization helpers before unsupported-action validation.
**Status:** fixed.
**Notes:** Some replies emitted `thought` as a separate legacy action object instead of a `create_personal_diary_entry.payload.personal_thought` field. The message chain now folds a standalone `thought` action into the paired diary action before validation, preserving `personal_thought` metadata and avoiding the correction hop.

---

### `chat_history_cache` deduplication query still used MySQL `DATE_SUB(... INTERVAL ...)` syntax  <!-- 2026-04-20 -->
**Symptom:** Live manual turns could log `Deduplication check failed: syntax error at or near "5"` from `chat_history_cache`, then continue after skipping the duplicate check.
**Location:** `core/chat_history_cache.py` (`save_chat_message`, duplicate-message guard query).
**Status:** fixed.
**Notes:** The Postgres SQL translator already rewrote `UTC_TIMESTAMP()` but not MySQL's `DATE_SUB(..., INTERVAL 5 SECOND)` form. The deduplication check now computes the 5-second cutoff in Python and passes it as a normal query parameter, keeping the save path dialect-neutral.

---

### `chat_update_checker` DB polling still used MySQL `UNIX_TIMESTAMP(...)` syntax  <!-- 2026-04-20 -->
**Symptom:** Runtime logs could show `DB query failed, falling back to in-memory check: function unix_timestamp(timestamp with time zone) does not exist` while polling for new non-self chat activity.
**Location:** `core/chat_update_checker.py` (`ChatUpdateChecker._check_once`).
**Status:** fixed.
**Notes:** The checker used `MAX(UNIX_TIMESTAMP(timestamp))` and `WHERE UNIX_TIMESTAMP(timestamp) > %s`, which works on MySQL but fails on Postgres. The polling path now queries raw timestamps, converts DB values to epoch seconds in Python, and passes a timezone-aware `datetime` cutoff back into the follow-up query so both backends stay compatible.

---

### SOUL static injection could time out on internal `grillo/-1` turns  <!-- 2026-04-20 -->
**Symptom:** Runtime logs could show `get_static_injection() on SoulPlugin timed out after 5s` during internal Grillo memory-consolidation prompt assembly, even though the prompt later completed without any `soul_*` injections.
**Location:** `plugins/soul_plugin.py` (`SoulPlugin.get_static_injection`).
**Status:** fixed.
**Notes:** SOUL runtime recall is useful for real user/session interfaces, but not for internal Grillo control turns like `grillo/-1`. The plugin now short-circuits static injection for internal Grillo interfaces before session bookkeeping or repository recall, avoiding wasted recall work and eliminating this fresh timeout path.

---

### Grillo outreach synthetic message ids could skip PromptRequest assembly  <!-- 2026-04-20 -->
**Symptom:** Grillo outreach turns could log `PromptRequest assembly skipped: invalid literal for int() with base 10: 'grillo_outreach_0'`, then fall back to the legacy flattened prompt path even though normal manual turns were already using `__prompt_request`.
**Location:** `core/prompt_engine.py` (`_assemble_prompt_request`, `RuntimeContext.message_id` assignment).
**Status:** fixed.
**Notes:** Outreach beats synthesize string message ids like `grillo_outreach_0`. `_assemble_prompt_request()` previously coerced every `message_id` through `int(...)`, so typed prompt assembly aborted for those turns. The runtime now tolerates non-numeric message ids by leaving `RuntimeContext.message_id` unset instead of crashing, which keeps structured prompt rendering enabled for outreach beats.

---

### Exact runtime timestamps could make trainer replies over-mention time and location  <!-- 2026-04-20 -->
**Symptom:** Ordinary trainer replies could keep volunteering lines like `at 17:43 CEST right here in ...` even when the user did not ask for the time or location, making every response feel overly anchored to prompt metadata.
**Location:** `core/prompt_renderers.py` (`_build_runtime_prefix`), `core/prompt_engine.py` (`load_unminified_chat_instruction`).
**Status:** fixed.
**Notes:** The current-turn runtime prefix injected the exact timestamp into every rendered user message, while the shared chat instruction only said to treat time fields as authoritative and did not tell the model to keep them in the background. The prompt stack now omits the exact timestamp from the per-turn prefix and explicitly tells the model to use time and location as ambient context unless precise details are actually needed.

---

### Root-owned `.venv` can break `uv run` and configured interpreter launch  <!-- 2026-04-22 -->
**Symptom:** Validation commands can fail before running tests with messages like `failed to remove directory ... .venv/lib64: Permission denied` or `.../.venv/bin/python: File o directory non esistente`.
**Location:** Workspace environment / local `.venv` in repo root (for example `.venv/bin/python -> /usr/local/bin/python3.12` with a missing target, plus root ownership preventing `uv` from rebuilding it).
**Status:** known, not fixed.
**Notes:** In this state `configure_python_environment` may still report `.venv/bin/python`, but the symlink target is broken and `uv run` tries to replace the root-owned environment, then fails on permissions. Workaround: use a temporary user-owned environment, for example `UV_PROJECT_ENVIRONMENT=/tmp/synth-heart-venv uv sync --frozen`, then run validation with the same `UV_PROJECT_ENVIRONMENT` prefix.

---

### Broad validation is currently blocked by unrelated repo issues  <!-- 2026-04-22 -->
**Symptom:** `uv run ruff check --fix .` fails on pre-existing files outside most feature slices (observed in `interface/message_send_utils.py`, `interface_dev/reddit_interface.py`, `interface_dev/telethon_userbot.py`, `interface_dev/x_interface.py`, `plugins/bio_manager.py`), and broad `uv run pytest --ignore=tests/plugins/test_selenium_ttsfree.py` can still surface unrelated failing tests (observed during one run: `tests/test_exposed_variables_static.py`, `tests/test_iris.py`).
**Location:** Mixed pre-existing validation debt across interfaces, plugins, and broad regression suite.
**Status:** known, not fixed.
**Notes:** When working on a focused feature, still run the mandatory repo-wide commands for signal, but expect unrelated failures. Use scoped lint/type checks on touched files plus targeted pytest around the modified area to verify the feature itself until the broader repo debt is cleaned up. Additional unrelated pytest failures observed on `2026-04-22`: `tests/test_mobile_chat_behavior.py`, `tests/test_multimodal_attachment.py`, `tests/test_ollama_compat_server.py`, `tests/test_selkies_api.py`, `tests/test_vox_plugin.py`.

---

### WebUI phase logs could hide valid THINKING/WRITING transitions  <!-- 2026-04-22 -->
**Symptom:** During debugging it could look like WebUI never entered `THINKING` / `WRITING`, because the browser console only logged `vrm_animation` messages and the backend phase-promotion log could misleadingly print `WRITING -> WRITING` even when the real transition was `THINKING -> WRITING`.
**Location:** `core/action_state_manager.py`, `res/synth_webui/js/chat-window.mjs`
**Status:** fixed.
**Notes:** `ActionStateManager.update_phase()` now snapshots the old phase before mutation, and the chat window now logs incoming `action_state` WebSocket events so frontend and backend traces can be correlated directly.

---

## 13. Database Quick Reference

> Tables are created inline in `core/db.py` and each plugin — **`init-db.sql` only seeds a subset.** If you need a table's full column list, `grep -A20 "CREATE TABLE IF NOT EXISTS <name>"` in the relevant file.

| Table | Owner | Purpose |
|-------|-------|---------|
| `config` | `core/db.py` | All `config_registry` persistent values — key/value store for every runtime setting |
| `chat_history_cache` | `core/chat_history_cache.py` | Message history per `interface_path`; source of truth for prompt context |
| `chat_session_meta` | `core/session_meta.py` | Per-interface session metadata (JSON blob) |
| `chat_archives` | `core/chat_archives_db.py` | Long-term archived chat history |
| `ai_diary` | `plugins/ai_diary.py` | Synth's diary entries (`content LONGTEXT`, no `user_message` column in the canonical schema — see §12 for the recurring overflow issue) |
| `ai_diary_archive` | `plugins/ai_diary.py` | Archived diary entries |
| `memories` | `plugins/ai_diary.py` | Long-term memory entries (`content`, `author`, `tags`, `scope`, `emotion`) |
| `emotion_state` | `plugins/emotion_manager.py` | Current emotion intensities with timestamps for decay |
| `emotion_diary` | `plugins/emotion_manager.py` | Historical emotion snapshots |
| `bio` | `plugins/bio_manager.py` | Synth's self-knowledge: likes, contacts, past events, feelings (JSON arrays stored as TEXT) |
| `recent_chats` | `plugins/recent_chats.py` / `core/db.py` | Rolling recent conversation summaries |
| `grillo_beats` | `init-db.sql` | Scheduled autonomous beat timers (`beat_type`, `next_beat`, `enabled`) |
| `grillo_activity_log` | `init-db.sql` | Log of executed Grillo beats with prompt/response text |
| `grillo_action_execs` | `init-db.sql` | Individual action executions within a Grillo beat |
| `agent_activity_log` | `init-db.sql` | Agent plugin task log (`command`, `proposer`, `trainer_id`, `result`) |
| `agent_action_execs` | `init-db.sql` | Individual action steps within an agent task |
| `agent_tasks` | `init-db.sql` | Structured agent task records with I/O JSON |
| `external_endpoints` | `init-db.sql` | LLM/API endpoint registry (name, protocol, URL, key, capabilities, model list) |
| `scheduled_events` | `plugins/event_plugin.py` | Date/time triggered events Synth should act on |
| `blocklist` | `plugins/blocklist.py` | Blocked users/entities |
| `chatlink` | `plugins/chat_link.py` | Cross-interface chat bridging config |
| `message_map` | `plugins/message_map.py` | Message ID mapping across interfaces |

**Key facts:**
- All tables use `utf8mb4` / `utf8mb4_unicode_ci`. Emoji and multi-byte content is safe.
- `interface_path` is the canonical user identifier across the codebase: `telegram_bot/12345`, `discord_bot/guild/channel`, `synth_webui/<uuid>`.
- The `config` table is the single source of truth for runtime settings. Env vars override it at startup; DB values are used for defaults.

---

## 14. Config Registry Keys

All keys stored in the `config` table and accessible via `config_registry.get_value(key)`. Env vars with the same name take precedence.

| Key | Purpose |
|-----|---------|
| `BASE_CORTEX` | Default LLM engine for all interactions |
| `GRILLO_CORTEX` | LLM engine used by Grillo autonomous beats |
| `TRAINER_CORTEX` | LLM engine used for trainer-facing tasks |
| `LIVE_CORTEX` | LLM engine used for live audio sessions |
| `ACTIVE_VOX_ENGINE` | Active TTS engine |
| `ACTIVE_AURIS_ENGINE` | Active STT engine |
| `ACTIVE_IRIS_ENGINE` | Active vision/image engine |
| `SYNTH_NAME` | Synth's display name |
| `SYNTH_PROFILE` | Synth's persona profile text (injected into every prompt) |
| `SYNTH_ALIASES` | Comma-separated name aliases Synth responds to |
| `SYNTH_AUTONOMY_MODE` | Autonomy level: `disabled`, `always_ask`, `whitelist`, `always_approve` |
| `TRAINER_CHAT_ID` | `interface_path` of the trainer (Scarlet) — used for direct notifications |
| `LOG_CHAT_ID` | `interface_path` to send ERROR/WARNING log notifications to |
| `LOG_CHAT_INTERFACE` | Interface name for LogChat delivery |
| `LOG_CHAT_THREAD_ID` | Thread ID for LogChat (Discord threads etc.) |
| `PROJECT_DEFAULT_LANGUAGE` | Default language for responses |
| `PROJECT_DEFAULT_TONE` | Default response tone |
| `INTERFACE_LANGUAGE_OVERRIDES` | JSON: per-interface language overrides |
| `INTERFACE_TONE_OVERRIDES` | JSON: per-interface tone overrides |
| `DIARY_HISTORY_DAYS` | How many days of diary to inject into context |
| `EMOTION_DECAY_TAU` | Emotion decay time constant (seconds) |
| `EMOTION_MAX_DISPLAY` | Max emotions to display in UI |
| `SOUL_PLUGIN_ENABLED` | Enable/disable SOUL runtime orchestration plugin |
| `SOUL_COMPILE_IDLE_SECONDS` | Idle seconds before SOUL compiles buffered transcript |
| `SOUL_SCHEDULER_INTERVAL_SECONDS` | Scheduler tick interval for SOUL compile/rollup checks |
| `SOUL_REPOSITORY_BACKEND` | SOUL persistence backend selector (`memory` or `postgres`) |
| `SOUL_POSTGRES_DSN` | PostgreSQL DSN used when SOUL backend is `postgres` |
| `ENABLE_MEMORY_SEARCH` | Enable/disable semantic memory retrieval |
| `MEMORY_SEARCH_MAX_RESULTS` | Max memories returned per query |
| `GRILLO_ALLOWED_ACTIONS` | Actions Grillo is permitted to execute |
| `GRILLO_ALLOWED_SECURITY_LEVEL` | Max security level for Grillo actions |
| `AUTONOMY_ALLOWED_ACTIONS` | Actions allowed in autonomy mode |
| `AUTONOMY_ALLOWED_SECURITY_LEVEL` | Max security level for autonomous actions |
| `LLM_AUTO_EXECUTE_UNSAFE_ACTIONS` | Whether to auto-execute unsafe LLM actions |
| `AWAIT_RESPONSE_TIMEOUT` | Seconds to wait for LLM response before timeout |
| `LIVE_VOICE_NAME` | Voice name for live audio TTS |
| `LIVE_VOICE_STYLE` | Voice style for live audio |
| `LIVE_HISTORY_SYNC_INTERVAL` | How often to sync chat history in live sessions |
| `LIVE_SYNC_CHAT_HISTORY` | Whether to sync chat history in live sessions |
| `WEBUI_ACCENT_COLOR` | WebUI theme accent color |
| `GEMINI_API_KEY` | Gemini API key (also settable via env) |
| `RECON_MAX_RESULTS` | Max results for recon/search operations |
| `RECON_TIMEOUT` | Timeout for recon operations |
| `RECON_LOG_READER_LINES` | Lines to read for log recon actions |
| `VOSK_MODEL_PATH` | Path to VOSK STT model |
| `CHAT_SLEEP_COMMANDS` | Commands that put Synth into sleep/quiet mode |
| `CHAT_WAKE_COMMANDS` | Commands that wake Synth from sleep mode |

---

<!-- gitnexus:start -->
# GitNexus — Code Intelligence

This project is indexed by GitNexus as **synthetic_heart** (9044 symbols, 29174 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

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
