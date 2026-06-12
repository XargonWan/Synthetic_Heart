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
              │ plugins/ │  │engines/│   │ interface/ │
              │          │  │        │   │            │
              │ actions  │  │external│   │ Telegram   │
              │ agents   │  │ live   │   │ Discord    │
              │          │  │ agent  │   │ Matrix     │
              └──────────┘  │Gemini …│   │ Ollama API │
                            └────────┘   └────────────┘
```

| Layer | Location | Purpose |
|-------|----------|---------|
| **Core** | `core/` | Message chain, validation, dispatcher, DB, notifier. Never hardcodes plugin/LLM/interface logic. |
| **Plugins** | `plugins/` | Provide actions via `get_supported_actions()`. Subclass `PluginBase` or `AIPluginBase`. |
| **LLM Engines** | `engines/` | Interchangeable reasoning backends (`external_engines/`, `live/`, `agent/`). Subclass `AIPluginBase`. |
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
- Location: `engines/external_engines/` (API engines), `engines/live/` (live audio), `engines/agent/` (agentic). The `cortex/` and `llm_engines/` paths referenced in older docs no longer exist.

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

### Codebase audit completed — do not re-sweep  <!-- 2026-06-12 -->
**Symptom:** N/A — this is an audit record, not a bug.
**Location:** Whole repo; detailed ledger = the 24 commits ending at `d423162` (2026-06-11/12).
**Status:** done — 15 production bugs fixed, 28 vacuous tests resurrected, production dirs lint-clean.
**Notes:** The following checks were already performed and need not be repeated unless the code has changed since `d423162`:
- *Commit review*: all commits from the month before 2026-06-11 reviewed line-by-line; defects fixed.
- *Extended lint sweep* (`ruff --select B,PLE,ASYNC,RUF006,B023,B005,B039,B905`) across `core/`, `plugins/`, `engines/`, `interface/`: every hit triaged; real bugs fixed (loop-binding in `message_queue`/`notifier`, `lstrip` sample-URL bug, blocking subprocess in `gemini_cli`), the rest are accepted idiom (B904/ASYNC230/ASYNC109/B007/B009/B010/B027) or documented (RUF006, entry below).
- *Default `ruff check`*: production dirs (`core`, `plugins`, `engines`, `interface`, `mcp_servers`, `vendor`) pass with zero errors. Remaining failures live only in `tests/`, `plugins_dev/`, `interface_dev/` (dev sandboxes, left as-is, incl. known F821s in `telethon_userbot.py` and `gasmask.py`).
- *Semantic `ty` sweep* of `core/` (minus `webui.py`), `plugins/`, `engines/`, `interface/`: all call-level error classes (missing/unknown-argument, unresolved-reference/import, call-non-callable, not-iterable/subscriptable, invalid-argument-type) triaged. Real crashes fixed (`bio_manager.update_user_name`, `event_plugin` phantom `get_local_tz` + `run_action` signature, `message_send_utils` `TELEGRAM_TRAINER_ID`/missing `global`s, `telegram_bot.reset_chat`, `recent_chats` chat-path keys). Remaining `ty` diagnostics are annotation debt (`param: str = None` defaults, dict value-union noise, private `_queue` access) — verified non-bugs.
- *Targeted pattern hunts*: nested `asyncio.run` (all guarded), HTTP calls without timeout (none in prod code), `run_coroutine_threadsafe().result()` deadlocks (none), `unittest.TestCase` classes with `async def` tests (all four affected files fixed).
- *Known false positives* (don't re-investigate): `grillo_compactor` extract_json tuple overload, Iris/Auris TypedDict capability dicts, discord `disconnect(force)` stub mismatch, `ollama_compat_server` payload value-union subscript, `variables_engine` guarded casts, `models.py` hasattr-guarded isoformat.
- *Explicitly NOT audited*: `core/webui.py` logic (maintainer decision — "works well enough"), runtime/integration behaviour against a live DB, deep business logic of `radio_host`/`emotion_manager`/memory plugins beyond pattern level, `automation_tools/`, `scripts/`, `webtop/`.
- *Continuation pass (2026-06-12, HEAD `fd424ef`, code unchanged since `d423162`)*: deep business-logic review of `plugins/radio_host/` (all 5 files), `plugins/emotion_manager.py`, `plugins/memory_search.py`, `core/synth_core_memory.py`, `plugins/ai_diary.py`; review of `automation_tools/`, `webtop/` shell scripts; default + extended ruff sweep of `scripts/` (clean except two accepted-idiom hits: B007/B905 in `windows_setup.py`). All findings recorded as individual 2026-06-12 entries below; on maintainer request the same day, the fixes were applied (see each entry's Status). Still NOT audited: `core/webui.py` logic, live-DB integration behaviour beyond log sampling, `plugins_dev/`, `interface_dev/`.
- *Open decisions for the maintainer*: delete `core/presence_manager.py` (entry below); add a CI `ruff check` gate (two of the shipped bugs were plain F821s a lint gate would have caught).

---

### `core/presence_manager.py` is dead and partially broken  <!-- 2026-06-12 -->
**Symptom:** None at runtime — nothing imports this module anywhere.
**Location:** `core/presence_manager.py`
**Status:** known — candidate for deletion, pending a maintainer decision.
**Notes:** `presence_loop`/`evaluate_emotions` would work if wired in, but `reflect_on_recent_responses` imports `core.llm_logic` (a module that has never existed), calls `get_recent_responses(limit=10)` against a `(since_timestamp)` signature, and passes `insert_memory(emotion_state=...)` which may not match. Do not wire this module in without fixing those first.

---

### `emotion_manager` decay loop double-applies decay (timestamp never refreshed)  <!-- 2026-06-12 -->
**Symptom:** Emotions collapse to baseline much faster than `EMOTION_DECAY_TAU` implies; injected emotion intensities look "flat" shortly after being set.
**Location:** `plugins/emotion_manager.py`, `decay_emotions()`.
**Status:** fixed.
**Notes:** The 60 s `_decay_loop` persisted the *decayed* intensity but kept the original timestamp, so every read and every later decay cycle re-applied the full decay-since-original-timestamp to the already-decayed value, compounding each minute. The persist now refreshes `timestamp = NOW()` alongside the decayed intensity (same restart-the-curve convention `set_emotion()` uses). Also fixed in the same pass: `decay_emotions`/`sync_emotions_from_all_sources`/`update_emotion_state` now use timezone-aware `datetime.now(timezone.utc)` instead of naive now (which `_normalize_datetime` mislabelled as UTC), and the `EMOTION_SYNONYMS` typo `"adoaration"` was corrected to `"adoration"`.

---

### `ai_diary.get_recent_entries_async` crashes on Postgres and silently disables the diary  <!-- 2026-06-12 -->
**Symptom:** `[ai_diary] Failed to get recent entries async: 'asyncpg.protocol.record.Record' object does not support item assignment` in `synth.log` (observed live 2026-06-12), after which `PLUGIN_ENABLED` is set `False` and the diary stops injecting/persisting until a lazy re-init succeeds.
**Location:** `plugins/ai_diary.py`, `get_recent_entries_async()` (JSON-field mutation loop) and the broad `except` that sets `PLUGIN_ENABLED = False`.
**Status:** fixed.
**Notes:** On the Postgres compat path, `_fetchall(..., aiomysql.DictCursor)` returned immutable asyncpg `Record` objects, so `entry["context_tags"] = json.loads(...)` raised and the except handler disabled the plugin. `_fetchall` now copies every row into a plain dict, and all JSON-field parsing goes through the defensive `_parse_json_list()` / `_isoformat_timestamp()` helpers (handles `NULL`, malformed text, and already-decoded lists), so data-shaped rows can no longer trip the `PLUGIN_ENABLED = False` path. The disable-on-exception policy itself remains for genuine DB-connectivity errors.

---

### `ai_diary` sync `_run()` bridge blocks the event loop up to 10 s per call  <!-- 2026-06-12 -->
**Symptom:** Interaction processing can stall while a diary entry is written; under DB latency the whole loop freezes for up to the 10 s future timeout.
**Location:** `plugins/ai_diary.py`, `_run()` (ThreadPoolExecutor + `asyncio.run` + `future.result(timeout=10.0)`).
**Status:** partially fixed.
**Notes:** `DiaryPlugin.execute_action` is now `async`: the diary-write path awaits `add_diary_entry_async`/`_execute` directly, and the consolidation archive step runs the sync helper via `asyncio.to_thread`, so the action path no longer blocks the event loop. The sync wrappers (`add_diary_entry`, `get_entries_by_tags`, `archive_diary_entries`, ...) still use the `_run` bridge for their remaining sync callers (e.g. `core/webui.py` calls `archive_diary_entries` synchronously from async handlers) — convert those call sites when touching webui.

---

### `radio_host` track verification can store a track *title* as the last track id  <!-- 2026-06-12 -->
**Symptom:** Spurious duplicate track-change announcements; logs show a track change fire twice for the same song after a nowplaying fetch hiccup.
**Location:** `plugins/radio_host/track_monitor.py`, `_verify_track_stable()` (the two fallback paths: fetch-exception and incomplete-data).
**Status:** fixed.
**Notes:** The function's contract is "return the verified track **id** or None", but the fallback paths returned the *title*; the caller persisted it into `_last_track_id`, so the next poll fired a phantom second track change. `_verify_track_stable(detected_track_id)` now receives the originally detected id and returns it on both fallback paths.

---

### `radio_host` pre-generated banter: six concurrent writers race for one slot  <!-- 2026-06-12 -->
**Symptom:** Logs frequently show `Pre-generated banter stale (was 'X', now 'Y'); falling back to template` even though pre-generation ran; LLM/TTS work is generated and discarded every transition.
**Location:** `plugins/radio_host/radio_host_plugin.py` — `_pending_banter`, `_store_pending_banter`, `_pop_matching_banter`.
**Status:** fixed.
**Notes:** Up to 3 transitions × 2 generators = 6 async writers all overwrote a single `_pending_banter` slot; last writer won, so most transitions fell back to template text and pre-gen LLM/TTS cycles were wasted. Pending banter is now a dict keyed by `(prev_title, prev_artist)` (pruned to 8 entries, FIFO) via `_store_pending_banter(banter, source)`; a fast `"template"` result can no longer overwrite richer `"llm"` banter already stored for the same transition.

---

### `radio_host` WebDJ: hardcoded credentials and no `wss://` support  <!-- 2026-06-12 -->
**Symptom:** Banter broadcast fails (or silently can't connect) against HTTPS-only AzuraCast instances; streamer credentials cannot be configured.
**Location:** `plugins/radio_host/azuracast_client.py`, `plugins/radio_host/radio_host_plugin.py`, `jingle_injector.py`.
**Status:** fixed.
**Notes:** The WebDJ URL now maps `https://` base URLs to `wss://` via `AzuraCastClient._build_webdj_url()`. Streamer credentials are configurable through the new exposed vars `AZURACAST_STREAMER_USERNAME` / `AZURACAST_STREAMER_PASSWORD` (defaults unchanged: `SyntH`/`synthradio`), wired through `_read_config` into the injector and used by the winding-down injection path instead of literals. The non-f-string in `_enqueue_banter_generation` (literal `{curr_title}` sent to the model) was also fixed.

---

### `radio_host` `/api/radio/audio?path=` check allows sibling-directory bypass  <!-- 2026-06-12 -->
**Symptom:** None observed; latent path-validation weakness.
**Location:** `plugins/radio_host/radio_host_plugin.py`, `_serve_radio_audio` (direct-path branch).
**Status:** fixed.
**Notes:** The allow-check was `abs_path.startswith(prefix)`, which let sibling directories like `/app/tmp_tts/radio_host_evil/` through. It now uses `os.path.commonpath([abs_path, prefix]) == prefix`.

---

### `radio_host` `_find_active_schedule` likely never matches AzuraCast's schedule shape  <!-- 2026-06-12 -->
**Symptom:** `schedule_description` stays empty in `/api/radio/data` and the DJ prompt never includes "Current program: ...", even with active station schedules.
**Location:** `plugins/radio_host/radio_host_plugin.py`, `_find_active_schedule`.
**Status:** fixed (not yet verified against a live AzuraCast).
**Notes:** The code only understood entries with `days` (list) + seconds-in-day boundaries, but AzuraCast's `/api/station/{id}/schedule` returns calendar-style entries (`is_now`, `start_timestamp`/`end_timestamp`). The matcher now checks `is_now` first, then unix start/end timestamps, and keeps the legacy days/seconds shape as a final fallback. Cosmetic impact only (prompt enrichment); verify `schedule_description` populates on the next live run.

---

### `memory_search` plugin is deliberately dormant (`PLUGIN_CLASS = None`) with latent bugs  <!-- 2026-06-12 -->
**Symptom:** None at runtime — the `memory_search` action is not registered; the file gives no hint it is disabled.
**Location:** `plugins/memory_search.py` (last line), deactivated in commit `fee51dc` (2026-02-13) when the Recon plugins were introduced; live free-search now goes through `core/prompt_engine.free_memory_search`.
**Status:** known — dormant by maintainer action, not dead code by accident. Latent bugs fixed 2026-06-12.
**Notes:** Nothing in production instantiates `MemorySearchPlugin`, and the loader skips `PLUGIN_CLASS = None` modules. The latent bugs were fixed in place so reactivation is safe: empty OR-joins no longer produce invalid `WHERE ()` SQL for time-window-only free searches, and the chat-history sub-query is now restricted to mode='free' (tags mode with a time window no longer floods results with unrelated chat rows). To reactivate, restore `PLUGIN_CLASS = MemorySearchPlugin`.

---

### `automation_tools/container_synth.sh notify` imports symbols that no longer exist  <!-- 2026-06-12 -->
**Symptom:** `./container_synth.sh notify` dies with `ImportError: cannot import name 'BOT_TOKEN' from 'core.config'`.
**Location:** `automation_tools/container_synth.sh`, heredoc in the `notify)` case.
**Status:** fixed.
**Notes:** The heredoc imported `BOT_TOKEN` / `TELEGRAM_TRAINER_ID`, which no longer exist in `core/config.py`. It now reads the token from the `BOTFATHER_TOKEN` env var (the script sources `/app/.env` with `set -a`) and resolves the trainer via `core.config.get_trainer_id("telegram_bot")`, printing a clear message when either is unconfigured.

---

### pytest runs pollute the live `logs/` directory with test noise  <!-- 2026-06-12 -->
**Symptom:** `get_recent_errors` MCP output is dominated by bursts like `Recon plugin MagicMock parse failed ...` and synthetic `telegram_bot/1` correction warnings, all stamped within the same second.
**Location:** Test suite logging through the real `core/logging_utils.py` handlers into `logs/synth.log`.
**Status:** known, not fixed.
**Notes:** When triaging runtime errors from the synth-logs MCP, check whether the burst coincides with a `uv run pytest` invocation (MagicMock strings are the giveaway) before treating entries as production failures. Complements the existing "Interface tests can leak into the live `chat_history_cache`" entry.

---

### Unreferenced fire-and-forget asyncio tasks (RUF006)  <!-- 2026-06-11 -->
**Symptom:** Fire-and-forget work occasionally never completes, with no error logged. ~80 call sites use bare `asyncio.create_task(...)` / `ensure_future(...)` without keeping a reference (`ruff check --select RUF006` lists them).
**Location:** Spread across `core/` (webui, transport_layer, message_chain), `plugins/`, `interface/`.
**Status:** known, not fixed — the event loop holds only weak references, so an un-referenced task *can* be garbage-collected mid-flight. Most of these are short-lived sends and complete before GC, which is why it is rarely observed.
**Notes:** When touching one of these sites, keep a reference (module-level `set` + `task.add_done_callback(set.discard)`) rather than ignoring the return value. Do not mass-fix; convert opportunistically.

---

### Order-dependent test failures in full pytest runs  <!-- 2026-06-11 -->
**Symptom:** `test_db_cutover::test_cutover_runs_backup_and_migration`, `test_exposed_variables_static::test_no_direct_getenv_for_exposed_vars`, `test_exposed_variables_style::test_exposed_variable_label_and_description_style`, `test_vox_defaults::test_active_vox_engine_default_is_kitten`, and `test_vox_plugin::test_active_vox_engine_default_is_kitten` fail in a full `uv run pytest` run (e.g. `assert 'disabled' == 'kitten'`) but all pass when their files are run in isolation. The affected set shifts slightly between runs (`test_grillo_beat_system::test_grillo_beat_types_exist` has also tripped this way).
**Location:** `tests/` (config-registry / exposed-vars global state leaking between test modules)
**Status:** known — pre-existing, not tied to any single commit.
**Notes:** The `config_registry` and exposed-variable registry are process-global; earlier tests register or mutate vars (e.g. `ACTIVE_VOX_ENGINE` ends up `'disabled'`) that later default-assertion tests then see. When triaging a full-suite run, re-run the failing file alone before assuming a regression. Also note: local `.env` values (e.g. `SYNTH_PRIMARY_DB=soul`) leak into tests that don't pin them — `tests/test_db_preflight.py` now monkeypatches `core.db._get_db_type` for this reason. The reverse also happens: `tests/test_message_queue.py` `test_enqueue_*` tests hit the live DB configured in `.env` and time out when it is slow/unreachable (they pass standalone only with a responsive DB; verified unrelated to code changes via stash A/B).

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

### `mcp_synth-db_get_recent_diary` still queries a stale `created_at` column  <!-- 2026-05-04 -->
**Symptom:** The MCP diary helper returns `DB error: (1054, "Unknown column 'created_at' in 'SELECT'")` even though `ai_diary` uses `timestamp`.
**Location:** `synth-db` MCP diary helper / live `ai_diary` schema mismatch.
**Status:** fixed.
**Notes:** `get_recent_diary()` now queries the canonical `timestamp` column and dynamically limits the selected columns to those actually present in the current schema.

---

### `grillo_activity_log` inserts could return `None` ids on Postgres  <!-- 2026-05-04 -->
**Symptom:** Logs showed lines like `[grillo_outreach] Created activity log None`, followed by outreach rows that existed in `grillo_activity_log` but kept `response_text` / `diary_entry_id` as `NULL` because downstream code never learned the inserted row id.
**Location:** `plugins/grillo/grillo_impl.py`, `GrilloPlugin.create_activity_log`.
**Status:** fixed.
**Notes:** MariaDB callers relied on `cursor.lastrowid`, but the Postgres compat cursor only populates `lastrowid` when the statement returns rows. The insert path now uses `RETURNING id` on Postgres and Grillo synthetic messages also carry the activity id in `message_id` for fallback recovery.

---

### Automatic diary logging could create internal `diary_consolidation` noise rows  <!-- 2026-05-04 -->
**Symptom:** `ai_diary` could gain rows whose `content` looked like `Performed update_diary_entry action` and whose `user_message` started with `[DIARY CONSOLIDATION - INTERNAL SYSTEM TASK] ...`.
**Location:** `core/action_parser.py`, automatic diary hook in `_create_diary_entry_for_actions`.
**Status:** fixed.
**Notes:** Internal merge beats execute `update_diary_entry` as maintenance, not as a user-facing interaction. The diary hook now skips auto-entry creation for `beat_type == "diary_consolidation"` / `interface == "diary_merge"` so consolidation work no longer pollutes introspection rows.

---

### `diary_merge` upserts could overwrite a real `ai_diary.interface` origin  <!-- 2026-05-04 -->
**Symptom:** A same-day diary update triggered by internal merge flow could replace a real external interface such as `telegram_bot` with `diary_merge`, making the row look system-generated.
**Location:** `plugins/ai_diary.py`, `_merge_diary_interface` during same-day upsert merge.
**Status:** fixed.
**Notes:** `diary_merge` is an internal maintenance interface and should be treated like `grillo` / `unknown` for origin merge purposes. The merge helper now preserves meaningful external origins instead of promoting `diary_merge`.

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
**Notes:** Some deployments still use a legacy `emotion_diary` schema with `id varchar(100)` and `intensity int(11)` (no `timestamp`). The emotion manager writes floats (including baseline `0.1`), but DB coercion stores these as `0`, creating misleading analytics. **Root cause found and fixed 2026-06-12:** the "legacy" schema was not legacy — `plugins/ai_diary.py` `init_diary_table()` created `emotion_diary` with `id VARCHAR(100) PRIMARY KEY`, `intensity INT`, no `timestamp`, while `plugins/emotion_manager.py` created it with auto-increment id and `intensity FLOAT`; whichever plugin initialized first won the `CREATE TABLE IF NOT EXISTS` race (ai_diary runs at module import, so it usually won on fresh DBs). The ai_diary DDL is now identical to the emotion_manager schema, so new databases get the float-capable table. **Existing databases keep the old table** — `CREATE TABLE IF NOT EXISTS` does not migrate it; truncated-to-zero rows on already-deployed DBs need a manual `ALTER TABLE emotion_diary MODIFY intensity FLOAT` (plus id/timestamp migration) if accurate history matters.

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
**Status:** fixed.
**Notes:** Verified 2026-06-12: `on_debrief` now branches on `_get_db_type()` and uses `string_agg(...)` on the Postgres path.

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
**Notes:** The lexical fallback query used unindexed `atomic_facts::text` trigram checks plus a composite `to_tsvector(episodic_trace || atomic_facts::text)` expression, forcing a sequential scan over `mem_cells`. Candidate selection now stays on indexed `episodic_trace` trigram/tsvector expressions and computes richer episodic-trace-plus-atomic-facts lexical overlap in Python after fetch. Live probe on `2026-04-19` dropped warm repo recall from about `5.2s` to about `3.3s` end-to-end static injection. The `synth-db` MCP now supports explicit `target` selection (`runtime`, `source`, `soul`, `process_env`) so SOUL-state inspection no longer requires a separate ad hoc query path.

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

### External OpenAI-compatible adapters still do not use native tool calls end-to-end  <!-- 2026-05-07 -->
**Symptom:** External Gemini cortex turns now log native `tools` payloads and can return parsed function-call actions, but OpenAI-compatible external endpoints can still rely on freeform JSON-in-text responses instead of native tool calls. MCP traces for external OpenRouter-backed turns may still show `messages` only or text-only completions with malformed multi-action JSON.
**Location:** Remaining gap is primarily `core/external_endpoints/adapters/openai_compat.py` (`chat_completion` still returns `message.content` only, no tool-call parsing) plus any other non-Gemini external adapters that do not consume native tool declarations. External Gemini path is now handled by `core/external_endpoints/bridges/cortex_bridge.py` and `core/external_endpoints/adapters/gemini_adapter.py`.
**Status:** partially fixed.
**Notes:** The external bridge now preserves `PromptRequest` tool declarations for Gemini endpoints, forwards Gemini-native `tools`, and the SDK adapter normalizes Gemini `function_call` responses back into SyntH JSON actions. The remaining end-to-end native tool-calling gap is on external OpenAI-compatible and other non-Gemini adapters.

---

### Gemini tool manifests could lose normalized action parameters and yield empty payloads  <!-- 2026-05-07 -->
**Symptom:** `cortex_api.log` could show Gemini requests with native `tools` present, yet each `function_declaration.parameters.properties` object was empty. The resulting response then normalized into actions like `{"type":"message_telegram_bot","payload":{}}`, causing downstream validation errors such as missing `payload.text` and empty diary payloads.
**Location:** `core/live_tool_registry.py` (`build_manifests_from_actions`, plus shared manifest extraction for action definitions built from normalized `schema` blocks).
**Status:** fixed.
**Notes:** The prompt action registry largely stores normalized actions in `{"schema": {"properties": ...}, "brief": ...}` form, but the manifest builder only read a legacy `payload` dict. Gemini tool declarations now fall back to `schema.properties` and `schema.required`, so native tool calls retain required arguments like `text` and `interface_path`.

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

### `grillo_chat_observer` direct DB probe still uses MySQL `UNIX_TIMESTAMP(...)` syntax  <!-- 2026-05-05 -->
**Symptom:** Runtime logs can show `Error executing query: SELECT COUNT(*) as cnt, MAX(UNIX_TIMESTAMP(timestamp)) ...` followed by `[grillo_chat_observer] Direct DB check failed; falling back to checker: function unix_timestamp(timestamp with time zone) does not exist` on Postgres-backed runs.
**Location:** `plugins/grillo/grillo_chat_observer.py` (`GrilloChatObserverPlugin._run_observer`).
**Status:** fixed.
**Notes:** The observer fast-path now queries raw timestamps from `chat_history_cache`, passes a timezone-aware `datetime` cutoff into the DB layer, and normalizes the returned `MAX(timestamp)` value back to epoch seconds before advancing `GRILLO_OBSERVER_LAST_RUN_TS`. This removes the Postgres error log while preserving the observer's non-consuming update check.

---

### `memory_consolidation` beats were using a stale plugin prompt override  <!-- 2026-05-05 -->
**Symptom:** Runtime `memory_consolidation` beats could bypass the richer built-in prompt in `GrilloPlugin._create_memory_consolidation_prompt()` and instead use the older `plugins/grillo/grillo_memory.py` prompt, which lagged the current diary schema and omitted the newer first-person diary fields.
**Location:** `plugins/grillo/grillo_impl.py` (`GrilloPlugin._create_beat_prompt`) with the stale override in `plugins/grillo/grillo_memory.py`.
**Status:** fixed.
**Notes:** `_create_beat_prompt()` now routes `memory_consolidation` through the built-in prompt before consulting beat-plugin `build_prompt()` overrides. This makes the runtime path match the tested rewrite-safe prompt that includes history-derived context and the current `create_personal_diary_entry` payload shape.

---

### Tagged memory recall helpers still used MariaDB-only JSON predicates on Postgres  <!-- 2026-05-06 -->
**Symptom:** Internal prompt/memory recall paths could log warnings like `[search_memories] query failed: function json_contains(text, unknown) does not exist`, then fall back to zero tag-based memory hits on Postgres-backed runs. Related memory search/diary utilities also still relied on MariaDB-only `JSON_CONTAINS(...)`, `DATE_SUB(...)`, or `RAND()` forms.
**Location:** `core/synth_core_memory.py` (`search_memories`), `core/prompt_engine.py` (`search_memories`), `plugins/memory_search.py`, `plugins/ai_diary.py` tag/person lookups, and `plugins/grillo/grillo_compactor.py` marker filtering.
**Status:** fixed.
**Notes:** The active recall/search helpers now branch on the runtime DB backend: MariaDB keeps `JSON_CONTAINS(...)`, while Postgres uses `::jsonb ? %s` against text-backed JSON arrays. The prompt-layer helper now deduplicates in Python instead of relying on a MariaDB-tolerated `SELECT DISTINCT ... ORDER BY timestamp` shape that Postgres rejects. The memory search plugin also switches `RAND()` to `RANDOM()` on Postgres, and the Grillo compactor uses Postgres-safe timestamp cutoffs instead of `DATE_SUB(...)` when filtering old diary rows by marker. Live Postgres verification returned tag-based `environment` hits from both recall paths after the fix.

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

### Orphan `synth-soul-db` can block the Postgres-first runtime on port 5432  <!-- 2026-05-11 -->
**Symptom:** The browser can show `Unsafe attempt to load URL https://localhost:8000/ from frame with URL chrome-error://chromewebdata/`, `curl -kI https://localhost:8000` fails with TLS EOF / broken pipe, and `synth` logs loop on startup with `Legacy DB cutover failed: [Errno -2] Name or service not known`.
**Location:** Docker Compose runtime state after switching to the Postgres-first stack; stale orphan containers such as `synth-soul-db` and `synth-db-backup` can survive from the older topology.
**Status:** known / operational workaround.
**Notes:** In the observed failure, the current `synth-db` service could not bind host port `5432` because orphan `synth-soul-db` still owned it. `docker compose up -d --force-recreate synth-db synth` then left `synth` and `synth-legacy-db` on `synth_network` while `synth-db` never came up correctly, so `synth` could resolve `synth-legacy-db` but not `synth-db`. Safe recovery was: stop the orphan containers without deleting volumes, then rerun `docker compose up -d --force-recreate synth-db synth`. After that, `docker exec synth getent hosts synth-db synth-legacy-db` resolved both hosts and `https://localhost:8000` returned `200 OK` again.

---

### Stale `synth` image after branch switch can keep the old MySQL code path  <!-- 2026-05-11 -->
**Symptom:** On `feat/postgres-migration`, `synth-db` (Postgres) is healthy and `docker compose config` resolves `DB_HOST=synth-db` / `DB_PORT=5432`, but WebUI still fails with TLS EOF and `synth` logs show `aiomysql` errors such as `OperationalError(2013, 'Lost connection to MySQL server during query')` or `Can't connect to MySQL server on 'synth-db'`.
**Location:** Docker runtime / rebuilt state of the `synth` application container after changing branches.
**Status:** known / operational workaround.
**Notes:** The running `synth` container can still contain code from the previous branch even though the workspace and compose file are already on the Postgres migration branch. In the observed case, `/app/core/db.py` inside the live container still defaulted `_get_db_type()` to `mariadb`, while the workspace version defaulted to `postgres`. Safe recovery was: `docker compose up -d --build synth`, then verify the live container code and recheck `https://localhost:8000`.

---

### Daily diary WebUI used MySQL-only `group_concat_max_len` on Postgres  <!-- 2026-05-19 -->
**Symptom:** The WebUI diary history endpoint could fail with errors like `Failed to fetch daily diary: unrecognized configuration parameter "group_concat_max_len"` on Postgres-backed runs.
**Location:** `core/webui.py` (`history_diary`).
**Status:** fixed.
**Notes:** The query itself already relied on SQL translation for `GROUP_CONCAT(...)`, but the handler still executed `SET SESSION group_concat_max_len = 1048576` unconditionally. The fix keeps that session setting only on MariaDB/MySQL and skips it on Postgres, where `string_agg(...)` is used after translation.

---

### Repo-wide lint still has unrelated failures, but broad pytest is green  <!-- 2026-05-07 -->
**Symptom:** `uv run ruff check --fix .` can still fail on pre-existing files outside most feature slices (observed in `interface/message_send_utils.py`, `interface_dev/reddit_interface.py`, `interface_dev/telethon_userbot.py`, `interface_dev/x_interface.py`, `plugins/bio_manager.py`), but broad `uv run pytest --ignore=tests/plugins/test_selenium_ttsfree.py -q --disable-warnings` passed on `2026-05-07` with `1185 passed, 15 skipped`.
**Location:** Mixed pre-existing validation debt across interfaces, plugins, and broad regression suite.
**Status:** partially fixed.
**Notes:** The order-dependent pytest failures observed on `2026-05-06` in `tests/test_ai_diary_pool_behavior.py`, `tests/test_current_chat_history.py`, `tests/test_grillo_observer.py`, `tests/test_iris.py`, `tests/test_mobile_chat_behavior.py`, `tests/test_ollama_compat_server.py`, `tests/test_send_message_no_ws.py`, and `tests/test_vox_plugin.py` were fixed on `2026-05-07`. When working on a focused feature, still run the mandatory repo-wide commands for signal, but treat repo-wide Ruff failures as unrelated debt unless your slice touches those files. Use scoped lint/type checks on touched files plus targeted pytest around the modified area when the global lint pass is still dirty.

---

### `plugins/ai_diary.py` still has pre-existing scoped `ty` failures  <!-- 2026-05-07 -->
**Symptom:** Even focused validation can fail on `uv run ty check plugins/ai_diary.py` with existing Optional/default and loose-type diagnostics such as `invalid-parameter-default`, `invalid-return-type`, and `unsupported-operator`, even when the only new edit is a small schema dict change.
**Location:** `plugins/ai_diary.py`
**Status:** known, not fixed.
**Notes:** This is separate from the Gemini tool-schema regressions. If you only touch prompt/action metadata inside `DiaryPlugin.get_supported_actions()`, validate behavior with targeted pytest plus scoped type checks on the new shared schema-conversion files; do not assume fresh `ty` failures in `plugins/ai_diary.py` were introduced by the schema edit.

---

### PromptRequest history could replay autonomous outreach as assistant-only monologues  <!-- 2026-05-07 -->
**Symptom:** Manual Telegram prompts could reach Gemini with a `contents` history containing long runs of consecutive `model` turns such as multiple prior outreach messages, making the assembled conversation look malformed or oddly anchored even when the wire schema was valid.
**Location:** `core/prompt_engine.py` (`_history_to_turns`) fed by same-chat `history_current_chat` from `core/history_engine.py` / `chat_history_cache`.
**Status:** fixed.
**Notes:** Outreach messages are valid visible chat history and were not persisted with a special marker, so the safest fix was at turn normalization: drop unmatched leading assistant turns when a user turn appears later in the visible window, and coalesce consecutive same-role turns into one `Turn`. This keeps normal alternating conversations intact while preventing autonomous assistant streaks from poisoning the next manual prompt.

---

### External Gemini 503 overloads were not considered retryable  <!-- 2026-05-07 -->
**Symptom:** A single upstream error like `503 UNAVAILABLE ... high demand ... Please try again later` could immediately bubble out of `ExternalCortexEngine.generate_response()`, causing the queue fallback response instead of a retry even when the endpoint already allowed retries.
**Location:** `core/external_endpoints/bridges/cortex_bridge.py` (`ExternalCortexEngine._is_retryable_exception`).
**Status:** fixed.
**Notes:** The retry classifier only matched connection/timeouts and a narrow `temporarily unavailable` phrase, so Gemini overload errors with `status: UNAVAILABLE` were treated as fatal. The bridge now treats common transient API overload markers (`429`, `502/503/504`, `UNAVAILABLE`, `high demand`, `rate limit`, `try again later`, etc.) as retryable.

---

### WebUI phase logs could hide valid THINKING/WRITING transitions  <!-- 2026-04-22 -->
**Symptom:** During debugging it could look like WebUI never entered `THINKING` / `WRITING`, because the browser console only logged `vrm_animation` messages and the backend phase-promotion log could misleadingly print `WRITING -> WRITING` even when the real transition was `THINKING -> WRITING`.
**Location:** `core/action_state_manager.py`, `res/synth_webui/js/chat-window.mjs`
**Status:** fixed.
**Notes:** `ActionStateManager.update_phase()` now snapshots the old phase before mutation, and the chat window now logs incoming `action_state` WebSocket events so frontend and backend traces can be correlated directly.

---

### `SYNTH_PRIMARY_DB=memory` can inherit stale MariaDB cortex settings for Grillo  <!-- 2026-05-05 -->
**Symptom:** After switching to `SYNTH_PRIMARY_DB=memory`, fresh `grillo_activity_log` rows can appear in MariaDB with empty `response_text` / `diary_entry_id`, while logs show `[cortex_bridge:<engine>] generate_response failed: Connection error.` for internal `grillo/-1` beats.
**Location:** Selected primary DB config registry (`BASE_CORTEX`, `GRILLO_CORTEX`) plus runtime Grillo prompt execution.
**Status:** known / configuration-dependent.
**Notes:** The DB selector itself can work correctly while still exposing older config values from the chosen DB. In the observed MariaDB case, `GRILLO_CORTEX=Default` fell through to `BASE_CORTEX=gemma`, so Grillo inherited a dead engine after the switch. When changing primary DBs, verify or realign the selected DB's cortex config keys, not just the connection settings.

---

### `universal_send skip_history=True` was ignored and polluted `chat_history_cache`  <!-- 2026-05-05 -->
**Symptom:** Fallback/test sends could still appear in prompt history as rows like `fake / fallback / 😵`, even when the caller explicitly passed `skip_history=True`.
**Location:** `core/transport_layer.py` (`universal_send` history-save path), plus tests calling `send_llm_fallback_message()` / `universal_send()` with synthetic interface paths.
**Status:** fixed.
**Notes:** The transport layer now checks `skip_history` before calling `add_message_to_context()`. This prevents fallback deliveries and similar synthetic sends from polluting `history_recent`.

---

### Telegram send failures could mask the real error with undefined `correction_payload`  <!-- 2026-05-05 -->
**Symptom:** Logs could show a real Telegram delivery failure such as `Chat not found`, followed by `[message_plugin] Failed to send message via telegram_bot: cannot access local variable 'correction_payload' where it is not associated with a value`.
**Location:** `interface/telegram_bot.py`, `TelegramInterface.send_message` exception handling.
**Status:** fixed.
**Notes:** The send path now initializes a default correction payload before resolution/sending, and overrides it with more specific messaging when chat-name resolution fails. Delivery errors now surface consistently instead of raising a secondary `UnboundLocalError`.

---

### Telegram startup timeout could leave the interface permanently half-initialized  <!-- 2026-05-06 -->
**Symptom:** Logs could show `Error during Telegram bot startup: TimedOut('Timed out')`, followed by outbound attempts logging `Bot not initialized, cannot send message` while `message_plugin` still logged `Message successfully sent ...` for the same action.
**Location:** `interface/telegram_bot.py` (`start_bot`, `TelegramInterface.send_message`, `shutdown_interface`) and `plugins/message_plugin.py`.
**Status:** fixed.
**Notes:** `start_bot()` previously set `_bot_started = True` before initialization succeeded, so one transient timeout could brick Telegram for the rest of the process. Startup now tracks `starting` separately, retries transient timeout/network failures inline before disabling the interface, resets the state on failure, marks the interface disabled with the failure reason, and still schedules a delayed retry if all inline attempts fail. `TelegramInterface.send_message()` / `add_reaction()` now attempt recovery when the bot is missing, and `MessagePlugin` treats an explicit `False` return from `send_message()` as a real delivery failure instead of logging false success.

---

### Langfuse Gemini generations could keep token summaries empty  <!-- 2026-05-06 -->
**Symptom:** Langfuse trace and observation summaries could show zero/empty token usage for Gemini calls even though the stored observation still contained full `input` and `output` bodies.
**Location:** `core/cortex_api_logger.py` generation logging, `engines/external_engines/gemini_api.py` usageMetadata mapping, and `core/external_endpoints/adapters/gemini_adapter.py` SDK usage-metadata logging.
**Status:** fixed.
**Notes:** Langfuse SDK `2.60.10` accepts canonical generation `usage` in `{input, output, total}` form. The logger previously forwarded only provider-style `usage_details`, which preserved raw metadata but did not populate the summary token columns. `log_cortex_response()` now normalizes provider usage into both canonical `usage` and detailed `usage_details`, Gemini HTTP responses now forward `totalTokenCount` / cached-token counts from `usageMetadata`, and the Google SDK-backed external Gemini adapter now extracts `usage_metadata` so live `engine=gemini:...` calls in `cortex_api.log` include token tags too.

---

### External cortex bridge could misreport adapter timeouts as 300s and keep retrying them  <!-- 2026-05-06 -->
**Symptom:** Runtime logs could show warnings like `[cortex_bridge:gemini] generate_response timed out after 300.0s (attempt 1/3)` even when the underlying adapter request had already timed out much earlier (for example a `status=504` in `cortex_api.log`). Interactive chats and Grillo beats could then sit through repeated full-request timeout retries.
**Location:** `core/external_endpoints/bridges/cortex_bridge.py` (`_get_request_timeout`, `generate_response`).
**Status:** fixed.
**Notes:** The bridge timeout default (`300s`) had drifted from adapter-level defaults (for example Gemini's `120s`), so nested `asyncio.TimeoutError` exceptions were logged against the bridge timeout instead of the adapter timeout. The bridge now defaults to `120s`, forwards that timeout into `chat_completion(...)` so bridge and adapter stay aligned, and does not retry full request timeouts unless the endpoint explicitly opts in with `extra_config.retry_on_timeout=true`.

---

### Langfuse request traces could remain invisible until the response finished  <!-- 2026-05-06 -->
**Symptom:** Operators could see a request hit `cortex_api.log` or the console, but the matching Langfuse trace would only appear after the response finished because the request-side trace was created without an immediate flush.
**Location:** `core/cortex_api_logger.py` (`log_cortex_request`).
**Status:** fixed.
**Notes:** When `LANGFUSE_FLUSH_EACH_CALL=true`, `log_cortex_request()` now flushes the client right after pushing the request trace. This makes in-flight requests visible earlier in Langfuse. The response path still performs its own flush, and both request/response flush warnings are deduplicated.

---

### Langfuse API-error traces could look half-empty and mislabel Gemini provider failures  <!-- 2026-05-06 -->
**Symptom:** Langfuse could show a Gemini trace row with `metadata.error`, but the trace/generation output itself stayed empty and the status could appear as generic `500` even when the upstream provider error was really `429 RESOURCE_EXHAUSTED` or another API status.
**Location:** `core/cortex_api_logger.py` (`log_cortex_response` error-output handling) and `core/external_endpoints/adapters/gemini_adapter.py` exception logging.
**Status:** fixed.
**Notes:** `log_cortex_response()` now emits a structured Langfuse output payload for error paths instead of leaving `output=None`, so failed API calls carry visible error details in the trace. The Google SDK-backed Gemini adapter also preserves provider-side status/body details when exceptions expose them, so Langfuse metadata no longer collapses quota/API failures into a misleading local `500` by default.

---

### Same-chat user activity could cancel an in-flight Grillo outreach  <!-- 2026-05-06 -->
**Symptom:** Logs could show `Cancelled LOW_PRIORITY background task for telegram_bot/... (superseded by incoming user message)`, followed by Gemini `HTTP499` / `request cancelled`, and the expected outreach never reached Telegram even though the beat had already been enqueued and started prompt generation.
**Location:** `core/message_queue.py` low-priority background task tracking and cancellation.
**Status:** fixed.
**Notes:** The queue previously tracked low-priority work only by `interface_path` and always cancelled any same-chat background task when a user message arrived. That is fine for disposable background work, but it could abort an already-running outreach beat mid-generation. Background task tracking now carries per-task cancellation policy so `beat_type="outreach"` can finish while other low-priority tasks remain cancellable.

---

### Interface tests can leak into the live `chat_history_cache` if persistence is not stubbed  <!-- 2026-05-05 -->
**Symptom:** Running pytest can inject obvious test rows into runtime prompt context, for example `telegram_bot/123456789 -> "Private Rekku test"`, `synth_webui/session1 -> "Hello"`, or `discord_bot/888888 -> "hi"`.
**Location:** Tests that call real interface entry points such as `interface.telegram_bot.handle_message`, `SynthWebUIInterface.send_message`, or `DiscordInterface.send_message` without mocking `add_message_to_context`, `save_chat_message`, or `save_response_message`.
**Status:** workaround in place for the known offenders.
**Notes:** The affected tests now stub persistence explicitly. When adding new interface tests, mock chat-history persistence or use isolated DB fixtures, otherwise runtime prompt context can be contaminated by test data.

---

### `interface/telegram_bot.py` has ~60 pre-existing `ty check` errors  <!-- 2026-05-07 -->
**Symptom:** `uv run ty check interface/telegram_bot.py` emits ~60 errors: `unresolved-attribute` on `Message | None` and `User | None` unions, `invalid-return-type` on `get_trainer_id`, `invalid-argument-type` on coroutine-vs-Iterable, etc.
**Location:** `interface/telegram_bot.py` throughout.
**Status:** known, not fixed — all pre-existing before any session modifications.
**Notes:** These are python-telegram-bot optional-chaining patterns that `ty` doesn't resolve without stub annotations. Any agent editing this file will see the same errors and should confirm via `git diff` that their change is limited to a single line before concluding the errors are pre-existing.

---

### `core/webui.py` has broad pre-existing `ty check` noise  <!-- 2026-05-11 -->
**Symptom:** `uv run ty check core/webui.py` emits a long list of pre-existing diagnostics such as Starlette middleware callable mismatches, unresolved animation-handler attributes on stub unions, optional persona-manager attribute access, and deprecated `datetime.utcnow()` usage.
**Location:** `core/webui.py` throughout.
**Status:** known, not fixed.
**Notes:** During the manual-backup WebUI work, `get_errors` stayed clean for the touched backup route/button code, but scoped `ty` still reported many unrelated historical issues across the file. Validate local WebUI edits with focused tests plus `get_errors`, and do not assume fresh `ty` noise in this file came from a small endpoint/template change.

---

### WebUI startup history replay could show the oldest prompt-context window instead of the recent chat  <!-- 2026-05-11 -->
**Symptom:** After a restart, the WebUI could appear to have "lost" chat history even though `chat_history_cache` still contained the messages. Logs showed lines like `[context_manager] Loaded 10 messages for interface_path synth_webui/webui_default` and `[synth_webui] _replay_history: sent 10 messages ...`, but the replayed window reflected the oldest cached rows or only the smaller prompt-context deque.
**Location:** `core/chat_history_cache.py` (`load_chat_history` ordering/limit), `core/webui.py` (`_ensure_session_history_loaded`), and `core/chat_context_manager.py` (prompt-context deque size).
**Status:** fixed.
**Notes:** Two separate issues combined here: `load_chat_history()` fetched `ORDER BY timestamp ASC LIMIT N`, which returned the oldest N rows instead of the most recent N, and WebUI startup bound `self.message_history` to the context-manager deque whose default maxlen is 10 even though the WebUI can hold far more. The fix now fetches the most recent N rows then reorders them chronologically, and WebUI rehydrates its visible history directly from persisted cache with `self.max_history` while still loading the smaller prompt context separately.

---

### `grillo_activity_log` can show silent blank outreach rows after log rotation  <!-- 2026-05-07 -->
**Symptom:** A user reports "no outreach fired," but `logs/synth*` only start at the most recent restart time while `grillo_activity_log` still contains hourly `outreach` rows. Some of those rows have `response_text = ''` and `diary_entry_id = NULL`, so they look like missing beats with no obvious runtime trace left in the rotated logs.
**Location:** Runtime observability split across `logs/synth*`, `grillo_activity_log`, `chat_history_cache`, `core/external_endpoints/adapters/gemini_adapter.py`, and `core/plugin_instance.py` (`_update_grillo_response`).
**Status:** fixed.
**Notes:** After a restart, the current synth logs may no longer cover the user-reported window, so check `grillo_activity_log` plus `chat_history_cache` for the target `interface_path` before assuming the scheduler stalled. Successful outreach leaves a self-authored chat-history row near the activity timestamp. Blank outreach rows were caused by empty external-model replies being dropped twice: `handle_incoming_message()` skipped Grillo write-back for falsey results and `_update_grillo_response()` also returned early on empty text. The Grillo path now persists a diagnostic `[EMPTY LLM RESPONSE] ...` marker with engine / finish / block metadata when the visible reply is empty, so scheduler issues and safety-filtered model silences are distinguishable in the DB.

---

### Radio host KittenTTS volume — hard clipping distortion fixed with ffmpeg dynaudnorm  <!-- 2026-05-24 -->
**Symptom:** KittenTTS-generated TTS was too quiet. Adding makeup gain caused audible hard-clipping distortion.
**Location:** `plugins/vox_engines/kitten.py` (`generate_tts`), `plugins/radio_host/azuracast_client.py` (`_convert_to_webm`).
**Status:** fixed.
**Notes:** The engine now peak-normalizes cleanly to -1 dBFS (no makeup gain, no clipping). Transparent loudness is handled downstream by ffmpeg's `dynaudnorm` filter (frame=150 ms, max_gain=15×, target_peak=0.95) in `broadcast_banter`. This gives radio-presence volume without distortion.

---

### Radio host injection at track_change was too slow for timely announcements  <!-- 2026-05-24 -->
**Symptom:** Announcements injected at track_change time arrived ~8 s into the new song (TTS 3 s + ffmpeg 1 s + WebDJ 4 s pre-delay), making them feel "late" relative to the song start.
**Location:** `plugins/radio_host/radio_host_plugin.py` (`_on_track_change`, `_inject_banter_now`, `_on_winding_down`).
**Status:** fixed.
**Notes:** The design was changed to a **hybrid approach**: `_on_winding_down` is the primary injection point (banter plays during song outro, ~13 s remaining — safe from jingle overlap), and `_on_track_change` only acts as fallback when winding-down was skipped for a short/jingle track. A `_inject_at_track_change` boolean flag bridges the two paths.

---

## 13. Database Quick Reference

> Tables are created inline in `core/db.py` and each plugin — **`init-db.sql` only seeds a subset.** If you need a table's full column list, `grep -A20 "CREATE TABLE IF NOT EXISTS <name>"` in the relevant file.

| Table | Owner | Purpose |
|-------|-------|---------|
| `config` | `core/db.py` | All `config_registry` persistent values — key/value store for every runtime setting |
| `chat_history_cache` | `core/chat_history_cache.py` | Message history per `interface_path`; source of truth for prompt context |
| `chat_session_meta` | `core/session_meta.py` | Per-interface session metadata (JSON blob) |
| `chat_archives` | `core/chat_archives_db.py` | Long-term archived chat history |
| `ai_diary` | `plugins/ai_diary.py` | Synth's diary entries. Two competing CREATEs in the same file: `init_diary_table()` uses `content TEXT` + `user_message TEXT`; the lazy-init fallback uses `content LONGTEXT`. See §12 for the `user_message` overflow issue |
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

This project is indexed by GitNexus as **synthetic_heart** (10324 symbols, 33256 relationships, 300 execution flows). Use the GitNexus MCP tools to understand code, assess impact, and navigate safely.

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
