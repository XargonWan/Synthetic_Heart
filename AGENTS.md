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
- **Read logs if a bug fix is requested**": always read container logs or logs folder if some issue raises and the user asks your bug fixing in order to find the real cause of the issue, do not reply with guesses but with a real analisys of the logs and the code
- **No keyword-based implementations.**
  Never design or implement features whose behavior depends primarily on detecting specific words, phrases, trigger terms, or regular expression matches, as this won't work in a multi language environment.
  Avoid logic such as:
  - `if message contains X then do Y`
  - keyword lists
  - trigger-word routing
  - regex-based intent detection
  - hardcoded phrase matching for feature activation

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
After any substatnial code change or feature addition evaluate a documentation update.

- Location of Wiki/Documentation: `docs/` (Sphinx, ReadTheDocs format, English).
- README.md is located in the project root
- Evaluate whether your changes require a docs update. If they do, update docs as part of the task.
- At the same time update this file, AGENTS.md if something critical is missing for the future iterations or if the information need to be updated

---

## 11. Container & Infrastructure Notes

**Container restart:**
```bash
docker compose up -d --build
```

**Dev container restart:**
Some devs might want to have a developemnt deploy, usually called `docker-compose-dev.yml`
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

> Resolved issues (Status: fixed) have been moved to [`FIXED_ISSUES.md`](FIXED_ISSUES.md).

---

### Full test suite has 3 pre-existing order-dependent failures (`config_registry` singleton pollution)  <!-- 2026-07-01 -->
**Symptom:** Running `uv run pytest tests/ -k "not selenium"` (the full suite) fails `test_vox_defaults.py::test_active_vox_engine_default_is_kitten`, `test_vox_plugin.py::test_active_vox_engine_default_is_kitten`, and `test_grillo_prevent_duplicates.py::test_grillo_suppresses_when_last_is_synth`. All three pass when run in isolation or in small groups.
**Location:** `core/config_manager.py` (`config_registry` is a process-wide singleton); the failing assertions read `config_registry.get_value("ACTIVE_VOX_ENGINE", None)` and expect the hard-coded default `"kitten"`.
**Status:** known, pre-existing — confirmed via `git stash` that these 3 fail identically on unmodified `develop` (commit `e4558376`), so it is not caused by any specific feature change; whichever test runs first in the full ordering leaks the *real* DB-loaded value (`"disabled"`, per the live config table) into `config_registry._definitions["ACTIVE_VOX_ENGINE"]`, and it's never reset before the default-expecting test runs.
**Notes:** Don't waste a debugging session re-diagnosing this if the full suite shows exactly these 3 failures — verify first with `git stash` whether they reproduce on unmodified code before assuming a regression. Real fix would be giving `config_registry` (or at least the affected definitions) a per-test reset/fixture instead of relying on process-wide state; out of scope for whatever unrelated change surfaced this note.

---

### `grillo_impl.create_activity_log` writes to a table that doesn't exist on Postgres  <!-- 2026-07-01 -->
**Symptom:** `[grillo] create_activity_log failed: relation "grillo_activity_log" does not exist` on every grillo beat.
**Location:** `plugins/grillo/grillo_impl.py:525` (`create_activity_log`); the live Postgres "soul" DB has `radio_activity_log` but no `grillo_activity_log` (confirmed via `list_tables()` — `radio_activity_log` exists with both a stale `timestamptz` and a working `timestamp` column, see `FIXED_ISSUES.md` "Postgres DDL translator…"; `grillo_activity_log` is simply absent).
**Status:** known, not investigated further — found incidentally while debugging the unrelated `timestamp`/`timestamptz` column-rename bug; out of scope for that fix.
**Notes:** Likely candidates, not verified: (a) a rename from `grillo_activity_log` → `radio_activity_log` happened in the radio_host refactor and `grillo_impl.py` was missed, or (b) the two are meant to be genuinely separate tables and `grillo_activity_log`'s `CREATE TABLE IF NOT EXISTS` simply never runs on the Postgres path (it's defined in `tools/database_fix.py`'s `TABLE_DEFINITIONS`, which is a MariaDB-only `pymysql` script — not part of `core/db.py`'s Postgres `init_db()`/`ensure_plugin_tables()` preflight, so on the Postgres backend it would never get created at all). Next agent: check `git log -p` for `grillo_activity_log` vs `radio_activity_log` to see if it was a rename, and check whether `grillo_impl.py` has (or needs) its own Postgres-path table-init call analogous to `ai_diary`/`emotion_manager`.

---

### GBNF action grammar — hard constraint for local cortex output  <!-- 2026-06-21 -->
**What:** `force_action_grammar: true` in an openai_compat endpoint's `extra_config` makes `cortex_bridge` auto-build a GBNF grammar (`core/external_endpoints/action_grammar.py:build_actions_gbnf`) whose `type` enum is the exact set of actions offered for the request, and send it via `extra_body.grammar`. llama.cpp then constrains decoding so the model can only emit one well-formed `{"actions":[{"type":<known>,"payload":{...}}]}` object — no `<think>`/`<thought>` preamble (output must start with `{`), no malformed JSON, no invented/combined/duplicated types, and generation stops after the first object (kills the repetition cascade). This is the real fix for the whole class of local-model output failures; `force_json_object` is best-effort and silently ignored by many llama.cpp builds.
**Scope/safety:** opt-in per endpoint and only wired through the OPENAI-protocol path — other engines (gemini/anthropic/xai) are untouched. Implies the in-prompt protocol (`_disable_tools()` returns true for it, since a grammar constrains *content*, not tool_calls). A manual `extra_config.grammar` takes precedence; `response_format` is dropped when a grammar is present (redundant/conflicting). Payload schemas are intentionally NOT encoded (only `payload ::= object`) — encoding all 49 action schemas would be enormous/brittle.
**Caveat:** the grammar is generated, not validated against a live llama.cpp here. If a malformed grammar ever slips in, the server rejects the request and the turn fails — remove `force_action_grammar` to fall back. The builder fails safe (returns `None` → no grammar) on any internal error.

---

### Local model 20-min runaway + leaked `<thought>` (json_object not enforced)  <!-- 2026-06-21 -->
**Symptom:** A single chat turn took ~20 min and logged a malformed thinking tag plus cascading repeated `message_telegram_bot` outputs; only the first message was delivered. Trace: 1240s elapsed, `prompt 4887 + completion 27881 ≈ 32768` — the model generated until it **filled its entire 32k context window**.
**Location:** `core/external_endpoints/adapters/openai_compat.py` (`_strip_thinking`, `chat_completion`); `core/external_endpoints/bridges/cortex_bridge.py` (`_extra_api_kwargs`).
**Status:** mitigated (2026-06-21).
**Notes:** Two independent causes. (1) The model ignored `enable_thinking=False` and emitted reasoning terminated by `</thought>`; `_strip_thinking` only matched `<think>`/`<thinking>`, not `<thought>`, nor a dangling closing tag (open tag dropped), so it leaked into content (JSON was still extracted after it, so the first reply went out). Fixed: regex now covers `thought` and a leading `^.*?</…>` dangling close. (2) **`response_format: json_object` is NOT enforced by this llama.cpp/model** — the output contained reasoning + prose + repeated JSON objects, i.e. free-form, so `force_json_object` is effectively a no-op here. With **no `max_tokens`**, a repetition loop ran to the context limit. Fixed: a default `max_tokens` (4096) is applied by `cortex_bridge._extra_api_kwargs()` — **only for local-model endpoints** (`disable_tools` / `force_action_grammar`); an explicit `extra_config.max_tokens` always wins, and cloud openai endpoints (xai, openrouter) stay uncapped (scoping tightened 2026-06-21 — every endpoint here is `protocol: openai`, so the blanket adapter default was wrong). The only *hard* JSON constraint for this server remains a GBNF `grammar` (already forwardable via `extra_config.grammar`); `json_object` should be treated as best-effort on local backends.

---

### Langfuse traces that "start with an error" are corrector retries, not a fault  <!-- 2026-06-20 -->
**Symptom:** In Langfuse the input of many generations begins with `{"system_message": {"type": "error", "message": "=== PERSONA … === CORRECTION === CRITICAL ERROR: Your previous response was not valid JSON or incomplete …"}}`. Looks alarming, as if the system errored before the model ran.
**Location:** `core/transport_layer.py` `run_corrector_middleware` (`correction_payload = {"system_message": {"type": "error", …}}`, ~line 2019); the 2026-06-20 fix also prepends the persona block. This object is sent as the **user-role content** of a fresh single-turn request.
**Status:** working as designed (recovery), but high-frequency on local quants — diagnosis only, not changed.
**Notes:** These traces are the corrector asking the model to repeat valid JSON after `extract_json_from_text` failed. Common trigger on the `1070ti` openai_compat endpoint is a **JSON syntax error in a long reply** (e.g. `Expecting ',' delimiter at line 1 column 8360` — an unescaped `"` mid-string), not the "missing message action" loop documented above. The retry usually recovers (valid `message_*` action in the output). Root enabler: `core/external_endpoints/adapters/openai_compat.py` sends **no `response_format`/grammar** (only `extra_body.enable_thinking=False`), so the model free-decodes and small Q4 models break JSON on 2–4 paragraph outputs. Why it's invisible in `cortex_api.log`/`synth-cortex`: the whole correction envelope is one big string sanitized to `<string: N chars>`, so `cortex_search("system_message")` returns nothing — only Langfuse shows the full text. Mitigation now shipped: set `force_json_object: true` (or an explicit `response_format` / `grammar`) in the cortex endpoint's `extra_config` — `cortex_bridge._extra_api_kwargs()` forwards it so llama.cpp constrains decoding to valid JSON (auto-dropped when native tool-calling is active). See `docs/external_endpoints.rst` "Constrained JSON output". Opt-in per endpoint (zero regression for others); enable it on the local `1070ti` endpoint via the WebUI. Other levers: shorter outputs; stronger cortex.

**Important follow-up (2026-06-20): `force_json_object` alone does NOT fix the small-model silence, and is silently dropped on chat turns.** Investigation of a "no reply" report showed two things: (1) chat turns always send 49 native `tools`, and the guard in `generate_response` strips `response_format` whenever tools are present — so `force_json_object` only ever applied to non-tool turns (e.g. diary merge), never to chats. (2) The actual silence is a *schema* failure, not a syntax one: the small quant returned valid JSON but emitted diary fields (`interaction_summary`/`personal_thought`/`emotions`/`content`) as top-level action types with **no `message_*` action** → `message_chain` "no outbound message action" → corrector loop → fallback skipped → user gets nothing. `json_object` guarantees syntax, not the action schema, so it can't fix this. New lever: `disable_tools: true` in `extra_config` (`cortex_bridge._disable_tools` / `_inject_actions_into_prompt`) — stops advertising native tools and folds the scoped action catalog into the system prompt (the legacy in-prompt protocol). **Critical implementation note:** in the PromptRequest path the action catalog is delivered *only* via native tools (`OpenAIRenderer.render()` emits system+history+current; `system_instruction` carries format rules + persona, NOT the actions list), so a naive "stop sending tools" would strip the catalog entirely — `disable_tools` MUST re-inject it (it does). With tools off, the `response_format` guard no longer fires, so `force_json_object` finally applies to chats. The guaranteed fix for "always include a message action" is still a json_schema/GBNF grammar (not yet built).

**Follow-up (2026-06-20): with `disable_tools` on, the model now emits a message action but mis-addresses it.** After `disable_tools`+`force_json_object`, a manual reply came out as valid JSON *with* a message action — but the small quant hallucinated `interface_path: "/channels/main"` (→ Telegram chat `channels` → `BadRequest('Chat not found')`, silent non-delivery) and mangled the type (`"message_plugin, telegram_bot"`). Grillo outreach was unaffected because its target chat is system-set, not echoed from an incoming message. Two fixes: (1) `_inject_actions_into_prompt` now renders the catalog as a flat `- name: brief (payload keys: …)` list instead of a nested `{name:{brief,schema}}` dict — the nested shape made the model emit sub-keys like `brief` as action types. (2) `message_plugin._handle_message_action` now mirrors a reply to the **originating chat** when `_should_mirror_origin_path` is true, and is **excluded** for grillo/outreach/internal turns (those legitimately target a system-chosen chat). Routing detection is **per-turn scope-aware**: `_should_mirror_origin_path` resolves the engine via `derive_cortex_scope(context)` → `get_active_cortex_engine(scope)`. **Scope gate (2026-06-21):** in this deployment *every* endpoint is `protocol: openai` (1070ti, openrouter, xai-grok, xtx), so "OPENAI protocol" alone is not a useful discriminator — the mirror is therefore gated on the **local-model marker** (`extra_config.disable_tools` or `force_action_grammar`, the same flags `_disable_tools()` reads). So only the flagged local endpoint (1070ti) is mirrored; cloud openai endpoints (xai, openrouter) are left alone. `is_trainer` is present on the action-execution context (set in `message_queue`, passed straight through `run_action` → `execute_action`).

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
- *Open decisions for the maintainer*: delete `core/presence_manager.py` (entry below); add a CI `ruff check` gate (two of the shipped bugs were plain F821s a lint gate would have caught); run the `emotion_diary` schema migration on existing databases (entry below); verify `schedule_description` populates on the next live radio run (entry below).

---

### `core/presence_manager.py` is dead and partially broken  <!-- 2026-06-12 -->
**Symptom:** None at runtime — nothing imports this module anywhere.
**Location:** `core/presence_manager.py`
**Status:** known — candidate for deletion, pending a maintainer decision.
**Notes:** `presence_loop`/`evaluate_emotions` would work if wired in, but `reflect_on_recent_responses` imports `core.llm_logic` (a module that has never existed), calls `get_recent_responses(limit=10)` against a `(since_timestamp)` signature, and passes `insert_memory(emotion_state=...)` which may not match. Do not wire this module in without fixing those first.

---

### `ai_diary` sync `_run()` bridge blocks the event loop up to 10 s per call  <!-- 2026-06-12 -->
**Symptom:** Interaction processing can stall while a diary entry is written; under DB latency the whole loop freezes for up to the 10 s future timeout.
**Location:** `plugins/ai_diary.py`, `_run()` (ThreadPoolExecutor + `asyncio.run` + `future.result(timeout=10.0)`).
**Status:** partially fixed.
**Notes:** `DiaryPlugin.execute_action` is now `async`: the diary-write path awaits `add_diary_entry_async`/`_execute` directly, and the consolidation archive step runs the sync helper via `asyncio.to_thread`, so the action path no longer blocks the event loop. The sync wrappers (`add_diary_entry`, `get_entries_by_tags`, `archive_diary_entries`, ...) still use the `_run` bridge for their remaining sync callers (e.g. `core/webui.py` calls `archive_diary_entries` synchronously from async handlers) — convert those call sites when touching webui.

---

### `memory_search` plugin is deliberately dormant (`PLUGIN_CLASS = None`) with latent bugs  <!-- 2026-06-12 -->
**Symptom:** None at runtime — the `memory_search` action is not registered; the file gives no hint it is disabled.
**Location:** `plugins/memory_search.py` (last line), deactivated in commit `fee51dc` (2026-02-13) when the Recon plugins were introduced; live free-search now goes through `core/prompt_engine.free_memory_search`.
**Status:** known — dormant by maintainer action, not dead code by accident. Latent bugs fixed 2026-06-12.
**Notes:** Nothing in production instantiates `MemorySearchPlugin`, and the loader skips `PLUGIN_CLASS = None` modules. The latent bugs were fixed in place so reactivation is safe: empty OR-joins no longer produce invalid `WHERE ()` SQL for time-window-only free searches, and the chat-history sub-query is now restricted to mode='free' (tags mode with a time window no longer floods results with unrelated chat rows). To reactivate, restore `PLUGIN_CLASS = MemorySearchPlugin`.

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

### `emotion_diary` legacy schema truncates low intensities to zero  <!-- 2026-04-18 -->
**Location:** MariaDB table `emotion_diary`, `plugins/ai_diary.py` `init_diary_table()`, `plugins/emotion_manager.py`.
**Status:** partially fixed — code resolved 2026-06-12, existing databases still need a manual migration.
**Notes:** Root cause was two competing `CREATE TABLE IF NOT EXISTS` definitions (ai_diary's `intensity INT` variant vs emotion_manager's `intensity FLOAT`); the DDLs are now identical, so fresh databases are correct. **Open maintainer action:** already-deployed databases keep the old table — run `ALTER TABLE emotion_diary MODIFY intensity FLOAT` (plus id/timestamp alignment) on the live DB if accurate emotion history matters.

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

### External OpenAI-compatible adapters still do not use native tool calls end-to-end  <!-- 2026-05-07 -->
**Symptom:** External Gemini cortex turns now log native `tools` payloads and can return parsed function-call actions, but OpenAI-compatible external endpoints can still rely on freeform JSON-in-text responses instead of native tool calls. MCP traces for external OpenRouter-backed turns may still show `messages` only or text-only completions with malformed multi-action JSON.
**Location:** Remaining gap is primarily `core/external_endpoints/adapters/openai_compat.py` (`chat_completion` still returns `message.content` only, no tool-call parsing) plus any other non-Gemini external adapters that do not consume native tool declarations. External Gemini path is now handled by `core/external_endpoints/bridges/cortex_bridge.py` and `core/external_endpoints/adapters/gemini_adapter.py`.
**Status:** partially fixed.
**Notes:** The external bridge now preserves `PromptRequest` tool declarations for Gemini endpoints, forwards Gemini-native `tools`, and the SDK adapter normalizes Gemini `function_call` responses back into SyntH JSON actions. The remaining end-to-end native tool-calling gap is on external OpenAI-compatible and other non-Gemini adapters.

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

### `SYNTH_PRIMARY_DB=memory` can inherit stale MariaDB cortex settings for Grillo  <!-- 2026-05-05 -->
**Symptom:** After switching to `SYNTH_PRIMARY_DB=memory`, fresh `grillo_activity_log` rows can appear in MariaDB with empty `response_text` / `diary_entry_id`, while logs show `[cortex_bridge:<engine>] generate_response failed: Connection error.` for internal `grillo/-1` beats.
**Location:** Selected primary DB config registry (`BASE_CORTEX`, `GRILLO_CORTEX`) plus runtime Grillo prompt execution.
**Status:** known / configuration-dependent.
**Notes:** The DB selector itself can work correctly while still exposing older config values from the chosen DB. In the observed MariaDB case, `GRILLO_CORTEX=Default` fell through to `BASE_CORTEX=gemma`, so Grillo inherited a dead engine after the switch. When changing primary DBs, verify or realign the selected DB's cortex config keys, not just the connection settings.

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

### Correction system re-sends `message_*` on non-message action failures, causing duplicate Telegram messages  <!-- 2026-06-26 -->
**Symptom:** User receives two identical (or near-identical) replies on Telegram for a single message. Langfuse shows 3 traces in rapid succession: a broken primary generation, a first correction that sends the message, then a second correction that sends a second message.
**Location:** `core/transport_layer.py` `run_corrector_middleware`; correction prompt assembly.
**Root cause:** When the primary generation (Venice/gemma-4-uncensored) produces broken output (prose + malformed JSON), the corrector issues a retry that includes a full `message_*` action and delivers it. If any non-message action in that first correction fails (e.g. `use_animation` with an invalid field), a *second* correction is triggered. The second correction prompt says "0 actions failed" but still hands the model the original user message — the model re-generates a complete response including a new `message_*`, causing a duplicate delivery.
**Status:** known, not fixed.
**Notes:** The corrector prompt needs to track which actions already succeeded (specifically: whether a `message_*` action has already been delivered) and suppress re-generation of message actions in follow-up correction passes. Until fixed, the workaround is to ensure `use_animation` (and other minor supplementary actions) don't trigger a correction pass at all, or to make the corrector only ask the model to emit the *failed* action types. Observed in Langfuse session at 06:49 CEST on 2026-06-26: traces dd3f1636 (first correction, message delivered) and e69bce37 (second correction, duplicate delivered).

---

### Gemma-4 missing closing `}` for action dict → diary payload selected as `parsed`  <!-- 2026-06-26 -->
**Symptom:** message_chain logs `Normalizing action-key dictionary format` + `Added 6 synthetic action(s) for unregistered top-level key(s): interaction_summary, content, personal_thought, emotions, context_tags, involved_users` (the diary payload's own fields). Immediately followed by 12 unsupported action types, a correction, and eventual delivery. Trace: `ab6930e3-1689-48c6-a284-ea927c31695a`.
**Location:** `core/transport_layer.py` `extract_json_from_text`; triggered by gemma-4-uncensored (Venice) output.
**Root cause:** Gemma-4 sometimes emits the diary action dict without its closing `}`, so the outer `{"actions": [...], "type": "message_telegram_bot", ...}` is also malformed. The raw_decode scan falls back to the diary *payload* dict (minimum extra-chars parseable candidate). That dict has no `actions` key → message_chain normalizes its 6 fields as fake action types, then the unregistered-top-level-keys block doubles them to 12 → correction fires.
**Status:** fixed 2026-06-26 — `json_repair` now also runs when `found_json` is a dict without `"actions"` (not just when nothing is found). It fixes the missing brace, json_repair returns a list `[outer_with_actions, use_animation]`, the list is detected and merged back into a single dict with `"actions"` containing all recovered actions. `syntax_repaired=True` in metadata; `had_errors=False`; no correction needed.
**Notes:** The fix is in `extract_json_from_text` — the `_json_repair_needed` condition block at the bottom of the outer scan loop (outside all `if not found_json:` guards). Also: `json_repair` was NOT the cause of this trace (confirmed — no `json_repair` log entry exists; the bug predates the json_repair integration).

---

### Admin/maintenance actions leak into non-lite in-prompt catalogs for local endpoints  <!-- 2026-06-27 -->
**Symptom:** When `PROMPT_LITE_MODE=false` and a local endpoint uses `disable_tools: true` or `force_action_grammar: true`, the `=== AVAILABLE ACTIONS ===` block injected by `_inject_actions_into_prompt` contains ~44 actions including admin/maintenance ones that should never appear in a normal chat prompt: `soul_force_compile`, `soul_force_rollup`, `soul_get_status`, `soul_run_curator`, `bio_full_request`, `bio_update`, `block_user`, `unblock_user`, `cleanup_old_chats`, `cleanup_old_mappings`, `compact_now`, `ensure_chat`, `resolve_chat`, `get_recent_chats`, `list_chats`, `decay_emotions`, `set_emotion`, `sync_emotions_from_all_sources`, `update_emotion_from_tags`, `static_inject`, `send_mate_message`, `trigger_weather_report`, `schedule_message`, and others.
**Location:** `core/external_endpoints/bridges/cortex_bridge.py` `_inject_actions_into_prompt`; `core/prompt_engine.py` `_derive_default_prompt_action_types`, `_is_non_user_facing_action`.
**Status:** known — cross-interface `message_*` leak and `PROMPT_LITE_MODE` bypass both fixed 2026-06-27; admin action leak in non-lite mode is open.
**Notes:** The pre-filter (`_derive_default_prompt_action_types`) only excludes actions whose source matches a *registered* interface name. Plugin-provided actions (sources: `soul`, `bio`, `blocklist`, etc.) have no matching interface, so they pass through for every interface. `_is_non_user_facing_action` can exclude them if their brief/description contains "admin only", "deprecated", or "internal" — but most admin actions don't use those keywords. Two fix approaches: **(A) Tag at the plugin level** — add `"admin only"` to the brief of admin actions in each plugin's `get_supported_actions()`, so the existing guard in `_is_non_user_facing_action` catches them with zero new plumbing. **(B) Add an `admin_only` flag** to the action schema contract and update `_is_non_user_facing_action` to check it — cleaner but requires a schema change. Approach (A) is simpler. `PROMPT_LITE_MODE=true` already filters all these actions — the issue only affects non-lite local endpoints.

---

### Recon `parse_recon_response` missing `_raw_llm_text` on 4 plugins  <!-- 2026-06-27 -->
**Symptom:** `Recon plugin ReconMemoryRecollectorPlugin parse failed: parse_recon_response() got an unexpected keyword argument '_raw_llm_text'` — and same for log_reader, tone_evaluator, language_evaluator. Crashes the entire recon dispatch for that plugin group, producing zero recon contributions.
**Location:** `plugins/recon_memory_recollector.py`, `plugins/recon_log_reader.py`, `plugins/recon_tone_evaluator.py`, `plugins/recon_language_evaluator.py` — all `parse_recon_response()` signatures.
**Status:** fixed.
**Notes:** `core/recon.py:747` passes `_raw_llm_text=llm_text` as a keyword arg to all recon plugins. Four plugins didn't accept it. `recon_web_search.py` already had it (was fixed earlier). The fix adds `_raw_llm_text: str | None = None` as the last keyword parameter.

### Server-side LLM errors skip correction loop  <!-- 2026-06-27 -->
**Symptom:** When the LLM engine returns a non-recoverable error (e.g. `Logprobs not supported` from selenium-llm-engine proxying Gemini), the correction loop in `message_chain.py` would call the corrector 2+ times, each hitting the same dead engine and waiting for timeout (~120s each), before finally sending a fallback message.
**Location:** `core/message_chain.py` (correction loop, line ~2365).
**Status:** fixed.
**Notes:** The fix adds a pre-check in the correction loop: if the LLM return text contains a known server-error marker (`logprobs not supported`, `internal server error`, `service unavailable`, `5xx` gateway errors), skip directly to the fallback message. The `Logprobs not supported` error itself comes from the selenium-llm-engine (not SyntH) — the OpenAI SDK it uses internally sends `logprobs` to Gemini, which rejects it. Fix the selenium-llm-engine to strip/not-set `logprobs` in its OpenAI-compatible adapter.

---

### `timestamptz`-as-column-name "fix" (ff9c4d7) was based on a false premise — reverted  <!-- 2026-06-30 -->
**Symptom:** Commit `ff9c4d7` (2026-06-29) rewrote all SQL in `emotion_manager.py`, `ai_diary.py`, `grillo_outreach.py`, `chat_update_checker.py`, `chat_history_cache.py` to reference a column literally named `timestamptz`, on the claim that "the live DB uses `timestamptz` as the column identifier." This was wrong: every affected table (`emotion_state`, `ai_diary`, `ai_diary_archive`, `chat_history_cache`, `emotion_diary`) actually has a column named `timestamp` of type `timestamp with time zone` (i.e. the Postgres *type* is TIMESTAMPTZ, the *column* is `timestamp`) — confirmed live via `describe_table`. The commit conflated the type name with the column name. Result: constant `column "timestamptz" does not exist` errors across emotion state, diary, and chat history reads/writes — including history injection silently failing.
**Location:** same files as above.
**Status:** fixed (2026-06-30) — reverted `ff9c4d7`'s column renames back to `timestamp`. The MariaDB→Postgres syntax conversions bundled into that commit (`ON DUPLICATE KEY UPDATE`→`ON CONFLICT`, `UTC_TIMESTAMP()`→`NOW()`, `CURDATE()`→`CURRENT_DATE`) were also reverted because they're redundant: `core/db_backends.py` `translate_postgres_sql()` already auto-translates this MySQL syntax for every query that goes through `PostgresCompatCursor`, and `DATE(col)` is valid Postgres syntax natively.
**Notes:** Before assuming a live schema differs from the code, verify with `describe_table` (synth-db MCP) rather than trusting an error-hint string or a guess — and check whether the existing DB compat layer already handles the apparent mismatch before hand-editing SQL strings across multiple files.

---

### `grillo_activity_log` table — previously reported missing, now exists  <!-- 2026-06-30 -->
**Status:** resolved/stale — `describe_table`/`list_tables` confirm `grillo_activity_log` exists on the live DB (7474+ rows as of 2026-06-30). The 2026-06-29 entry claiming it was missing no longer applies; table was presumably created by a subsequent migration.

---

### `BOTFATHER_TOKEN` silently disabled when `load_all_from_db` runs before ConfigVar is evaluated  <!-- 2026-06-30 -->
**Symptom:** `[telegram_interface] Interface loaded in disabled state: BOTFATHER_TOKEN not configured` at startup even though the token is present in `.env`. Bot never starts; recovery path also reports "BOTFATHER_TOKEN not configured".
**Location:** `interface/telegram_bot.py` module-level autostart block (~line 2893); `core/config_manager.py` `load_all_from_db` / `_load_definition_sync`.
**Status:** fixed (2026-06-30) — see [interface/telegram_bot.py](interface/telegram_bot.py) legacy autostart block.
**Root cause:** `ConfigVar` is lazy — it only reads `os.getenv` the FIRST time `bool()` is called on it (`_load_definition_sync`). If something blocks that first call until AFTER `load_all_from_db` has run, `load_all_from_db` marks the definition `loaded=True, value=None` (not in DB → default=None). Subsequent `_load_definition_sync` calls then return early and never read env. Concretely: the legacy autostart condition previously evaluated `BOTFATHER_TOKEN` as part of its `if` clause, which forced env-loading before `load_all_from_db`. Adding any short-circuit before that `BOTFATHER_TOKEN` check (e.g. `_under_pytest` guard) can prevent the early eval, letting `load_all_from_db` eat the definition first.
**Fix:** Evaluate `_botfather_configured = bool(BOTFATHER_TOKEN)` unconditionally at module level before the autostart `if` block. This forces `env_override=True` onto the definition, causing `load_all_from_db` to skip it. The `_under_pytest` guard (to prevent `initialize_interface()` from touching a live token during tests) must come AFTER this early eval — not before it. Also: only check `"pytest" in sys.modules` for the pytest guard, NOT `"unittest"` — the latter can appear in production if any dep imports `unittest.mock`.

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
| `RADIO_HOST_ANNOUNCE_IF_NO_LISTENERS` | When enabled (default True), Synth only speaks on air if listeners are present. Set to False for always-on announcements. |
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
