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

---

### Local tool-calling models drop the chat reply → "missing reply" correction loop  <!-- 2026-06-20 -->
**Symptom:** With native tool-calling models on llama.cpp (Qwen3.5, Gemma) the chat works for a turn or two, then every turn gets caught by the corrector (`⚠️ LLM generated no outbound message action … triggering corrector for missing reply`, `message_chain.py:2298`) and sometimes echoes the user's own message / ends in the 😵 fallback. Cloud models and non-tool-calling local quants are unaffected.
**Location:** `core/prompt_renderers.py` (`OpenAIRenderer.parse_tool_call_response`), `core/external_endpoints/adapters/openai_compat.py` (`_extract_tool_call_actions`), `core/prompt_engine.py` (`_derive_default_prompt_action_types`).
**Status:** fixed (2026-06-20).
**Notes:** Root cause — `parse_tool_call_response` discarded the model's natural-language `content` whenever any `tool_calls` were present, keeping only the structured calls. Tool-trained local models write the reply in `content` and use tool_calls for side-effects (and `create_personal_diary_entry` is prompted as *"REQUIRED in every response"*, so they almost always emit at least one non-message tool call), so the reply was lost → no `message_*` action → corrector storm; the corrector embeds the original user message, which small quants then parrot back (the "echo"). Fix: when tool_calls are present, surface leftover `content` under the top-level `message` key (the message chain already maps that to `message_<interface>` and dedupes), unless a `message_*` tool call already carries the reply; `<think>` blocks are stripped first.

**Related — disabled plugins still injected their tools (token bloat for small LLMs):** `core_initializer` skips action registration for plugins reporting `is_enabled() == False`, but `radio_host` and `agent_plugin` stored their toggle in `self._enabled` (`RADIO_HOST_ENABLED` / `AGENT_ENABLED`) **without overriding `is_enabled()`** — and `RadioHostPlugin` isn't even an `AIPluginBase` subclass — so they always counted as enabled and dumped `radio_speak`/`radio_update_metadata` and `agent_execute`/`propose_action`/`approve_action` into *every* prompt. Fix: both now override `is_enabled()` to return `bool(self._enabled)`. General rule for new plugins: gate tool exposure with `is_enabled()`, not a private flag. (Note: actions are scoped at registration/startup, so a runtime toggle flip needs a component reload to take effect.)

**Related — corrector dropped the persona (model improvised likes/dislikes on corrected turns):** `run_corrector_middleware` (`core/transport_layer.py`) sends a fresh single-message correction prompt with no `system` role and no history, so the persona (identity + `persona_preferences` likes/dislikes) was absent and the model improvised off-character on every corrected turn. Because corrections were firing constantly (the tool-call bug above), it looked like likes/dislikes were never injected — but on *normal* turns they are (`_build_context_summary` → `[Persona background]`). Fix: the corrector now prepends the active persona (`get_static_identity_content()` + `get_static_preference_content()`) to `correction_message_text`. Note: likes/dislikes are read from the **Postgres `runtime` DB only** (`SYNTH_LIKES`/`SYNTH_DISLIKES`), never from `skins/*/persona.json`; the MariaDB `source` DB is not read for config and can diverge. Latent wipe risk: `save_persona`/`_update_persona_configs` write `persona.likes` back to config, so a persona that loads with empty likes (config DB not ready at startup) could overwrite the stored list with `[]`.

**Related — diary consolidation ran on a broken base cortex:** the diary-merge `Exhausted 4 attempts … chat_id=-1` errors happened because diary consolidation was resolving to `BASE_CORTEX` (which was set to a keyless `anthropic`) instead of the local grillo engine. `ai_diary` re-dispatches the merge as its own `diary_merge` interface with `diary_merge_beat` but **no `grillo_beat`**, and `derive_cortex_scope` (`core/config.py`) only mapped `is_trainer`/`grillo_beat` to a scope — so it fell through to base. Fix: `derive_cortex_scope` now routes `diary_merge_beat` to the `grillo` scope, so diary consolidation follows `GRILLO_CORTEX`. (If `GRILLO_CORTEX` is `Default` it still falls back to `BASE_CORTEX`, so a usable `BASE_CORTEX` is still required.) This is near-undebuggable from the UI — the only signal was the `ANTHROPIC_API_KEY not configured` warning line.

**Related — small local cortex writes `meta.rationale` / diary fields in detached 3rd-person "the user" instead of persona-voice "Scarlet":** in Langfuse / `cortex_api.log` the `meta.rationale` (and the grillo action-checker `rationale` + sometimes `interaction_summary`) come out as *"The user gave permission to escalate…"* / *"User asked X"* — generic assistant register, 2nd/3rd person, no trainer name — while a stronger cortex on the same code writes them in first person using "Scarlet". Not a per-persona config bug: `TRAINER_NAME` is set (`Scarlet, Zahej`) and the trainer bio *is* injected, but it's a single buried line in the persona profile. Root cause is prompt framing + weak model: (1) `load_json_instructions()` (`core/prompt_engine.py:2078+`) and the whole instruction block refer to the human only as *"the user"*, never the trainer name; the `AUTONOMY GUIDELINES` ask for a *"rationale explaining why the action is taken"*, which reads as a system/meta justification and nudges the model into assistant register. (2) The `create_personal_diary_entry` examples/notes (`plugins/ai_diary.py:1535-1564`) literally model 3rd-person *"User asked about weather conditions and I provided…"*. Small quants (Qwen3.5-9B-Q4, Gemma-4B at `1070ti` = current `BASE_CORTEX`) parrot that literal framing; frontier models resolve "the user" → the named trainer and write in-voice, which is why another instance's output "looks fine". `IDENTITY INTEGRITY: Stay inside the active persona in first person` (`prompt_engine.py:2093`) governs the persona's self-reference, not how the trainer is named, so it doesn't catch this. **Not** caught by the corrector — `run_corrector_middleware` (`core/transport_layer.py`) is JSON-recovery only and never rewrites voice/perspective.
**Status:** fixed (2026-06-20) for the autonomy `rationale` + diary fields (options a+b). The grillo action-checker `rationale` (safe/confidence triad) was left as-is — it is an internal safety justification where system register is acceptable.
**Notes:** Fix — added `core/config.get_trainer_display_name()` (reads `TRAINER_NAME` live from the config registry, returns `""` when unset; **never hardcoded**, multi-trainer comma values preserved). `load_json_instructions()` (`core/prompt_engine.py`) now resolves it and the `AUTONOMY GUIDELINES` line asks for a *first-person `rationale` (your own voice)* plus a dynamic `Name people, not 'the user' (your trainer: <name>)` hint. `ai_diary.get_supported_actions()` resolves the same name and its `interaction_summary`/`personal_thought` descriptions + examples/notes are now first-person and named (e.g. *"{trainer} asked me about the weather, so I shared the forecast"*; placeholder `"my trainer"` when unset). Watch the `load_json_instructions()` size guard (`tests/test_prompt_minification.py::test_instructions_size_reasonable`, <5000 chars) — the trainer name is variable-length; current no-name build ≈4966, with `Scarlet, Zahej` ≈4997, so a very long multi-trainer value could trip it. Remaining option if small-model output still drifts: option (c) — point `GRILLO_CORTEX`/`BASE_CORTEX` at a stronger model for the meta/diary work.

---

### llama.cpp generation cancelled mid-stream — layered client timeouts (default was 120s)  <!-- 2026-06-20 -->
**Symptom:** On slow hardware a long local generation is cancelled partway through; the `llama.cpp` server log shows `slot ... n_decoded = N` then `next: stopping wait for next result due to should_stop condition (adjust the --timeout argument if needed)` and `stop: cancel task`. It is *not* the server's `--timeout` — the **client (synth) aborts first and closes the socket**, which llama.cpp detects as `should_stop`.
**Location:** `core/external_endpoints/bridges/cortex_bridge.py` `_get_request_timeout()` (was `return 120.0`), applied both as the OpenAI SDK per-request `timeout` (httpx socket) *and* `asyncio.wait_for` around `chat_completion` (~lines 462/467).
**Status:** fixed (2026-06-20).
**Notes:** The trap that makes this hard to fix once: **multiple independent timeouts wrap the generation and the smallest binds**, so fixing one just exposes the next. Order (innermost→outermost) and the new generous defaults: generation `LLM_GENERATION_TIMEOUT_SEC` **1800** (new `core/config.py` var, `.env`/WebUI tunable, default for the bridge; per-endpoint `extra_config["timeout"]` still overrides) < `RESPONSE_TIMEOUT` 300→**2100** (`core/message_chain.py`) ≤ `AWAIT_RESPONSE_TIMEOUT` 600→**2400** (`core/transport_layer.py`) ≤ `LLM_CHAIN_LEASE_TIMEOUT_SEC` 600→**2400** (`core/plugin_instance.py`, registration + getter; only force-releases the lock, never cancels the gen). The adapter `__init__` default (60s, `openai_compat.py`) is overridden per-request by the bridge, so it doesn't bind on the cortex path. `RESPONSE_TIMEOUT`/`LLM_CHAIN_LEASE_TIMEOUT_SEC` were absent from the runtime DB (code default applies); `AWAIT_RESPONSE_TIMEOUT` was persisted at 600 and was bumped to 2400 in the DB. **External, not in code:** llama.cpp's own `--timeout` server arg — raise it to match for very long gens or the server cancels first. Invariant to preserve if you touch these: keep generation < all outer guards.

---

### Langfuse traces that "start with an error" are corrector retries, not a fault  <!-- 2026-06-20 -->
**Symptom:** In Langfuse the input of many generations begins with `{"system_message": {"type": "error", "message": "=== PERSONA … === CORRECTION === CRITICAL ERROR: Your previous response was not valid JSON or incomplete …"}}`. Looks alarming, as if the system errored before the model ran.
**Location:** `core/transport_layer.py` `run_corrector_middleware` (`correction_payload = {"system_message": {"type": "error", …}}`, ~line 2019); the 2026-06-20 fix also prepends the persona block. This object is sent as the **user-role content** of a fresh single-turn request.
**Status:** working as designed (recovery), but high-frequency on local quants — diagnosis only, not changed.
**Notes:** These traces are the corrector asking the model to repeat valid JSON after `extract_json_from_text` failed. Common trigger on the `1070ti` openai_compat endpoint is a **JSON syntax error in a long reply** (e.g. `Expecting ',' delimiter at line 1 column 8360` — an unescaped `"` mid-string), not the "missing message action" loop documented above. The retry usually recovers (valid `message_*` action in the output). Root enabler: `core/external_endpoints/adapters/openai_compat.py` sends **no `response_format`/grammar** (only `extra_body.enable_thinking=False`), so the model free-decodes and small Q4 models break JSON on 2–4 paragraph outputs. Why it's invisible in `cortex_api.log`/`synth-cortex`: the whole correction envelope is one big string sanitized to `<string: N chars>`, so `cortex_search("system_message")` returns nothing — only Langfuse shows the full text. Mitigation now shipped: set `force_json_object: true` (or an explicit `response_format` / `grammar`) in the cortex endpoint's `extra_config` — `cortex_bridge._extra_api_kwargs()` forwards it so llama.cpp constrains decoding to valid JSON (auto-dropped when native tool-calling is active). See `docs/external_endpoints.rst` "Constrained JSON output". Opt-in per endpoint (zero regression for others); enable it on the local `1070ti` endpoint via the WebUI. Other levers: shorter outputs; stronger cortex.

**Important follow-up (2026-06-20): `force_json_object` alone does NOT fix the small-model silence, and is silently dropped on chat turns.** Investigation of a "no reply" report showed two things: (1) chat turns always send 49 native `tools`, and the guard in `generate_response` strips `response_format` whenever tools are present — so `force_json_object` only ever applied to non-tool turns (e.g. diary merge), never to chats. (2) The actual silence is a *schema* failure, not a syntax one: the small quant returned valid JSON but emitted diary fields (`interaction_summary`/`personal_thought`/`emotions`/`content`) as top-level action types with **no `message_*` action** → `message_chain` "no outbound message action" → corrector loop → fallback skipped → user gets nothing. `json_object` guarantees syntax, not the action schema, so it can't fix this. New lever: `disable_tools: true` in `extra_config` (`cortex_bridge._disable_tools` / `_inject_actions_into_prompt`) — stops advertising native tools and folds the scoped action catalog into the system prompt (the legacy in-prompt protocol). **Critical implementation note:** in the PromptRequest path the action catalog is delivered *only* via native tools (`OpenAIRenderer.render()` emits system+history+current; `system_instruction` carries format rules + persona, NOT the actions list), so a naive "stop sending tools" would strip the catalog entirely — `disable_tools` MUST re-inject it (it does). With tools off, the `response_format` guard no longer fires, so `force_json_object` finally applies to chats. The guaranteed fix for "always include a message action" is still a json_schema/GBNF grammar (not yet built).

**Follow-up (2026-06-20): with `disable_tools` on, the model now emits a message action but mis-addresses it.** After `disable_tools`+`force_json_object`, a manual reply came out as valid JSON *with* a message action — but the small quant hallucinated `interface_path: "/channels/main"` (→ Telegram chat `channels` → `BadRequest('Chat not found')`, silent non-delivery) and mangled the type (`"message_plugin, telegram_bot"`). Grillo outreach was unaffected because its target chat is system-set, not echoed from an incoming message. Two fixes: (1) `_inject_actions_into_prompt` now renders the catalog as a flat `- name: brief (payload keys: …)` list instead of a nested `{name:{brief,schema}}` dict — the nested shape made the model emit sub-keys like `brief` as action types. (2) `message_plugin._handle_message_action` now mirrors a reply to the **originating chat** when `_should_mirror_origin_path` is true — scoped to active **openai_compat** cortex engines (instance is `ExternalCortexEngine` with `EndpointProtocol.OPENAI`), and **excluded** for grillo/outreach/internal turns (those legitimately target a system-chosen chat). Cloud/other engines are untouched. Routing detection is **per-turn scope-aware**: `_should_mirror_origin_path` resolves the engine via `derive_cortex_scope(context)` → `get_active_cortex_engine(scope)`, so a split setup (e.g. local 1070ti base + xai trainer for image recognition) mirrors only the local-engine turns and leaves xai trainer turns alone. `is_trainer` is present on the action-execution context (set in `message_queue`, passed straight through `run_action` → `execute_action`).

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

### `emotion_manager` decay loop double-applies decay (timestamp never refreshed)  <!-- 2026-06-12 -->
**Location:** `plugins/emotion_manager.py`, `decay_emotions()`.
**Status:** fixed.

---

### `ai_diary.get_recent_entries_async` crashes on Postgres and silently disables the diary  <!-- 2026-06-12 -->
**Location:** `plugins/ai_diary.py`, `get_recent_entries_async()` (JSON-field mutation loop) and the broad `except` that sets `PLUGIN_ENABLED = False`.
**Status:** fixed.

---

### `ai_diary` sync `_run()` bridge blocks the event loop up to 10 s per call  <!-- 2026-06-12 -->
**Symptom:** Interaction processing can stall while a diary entry is written; under DB latency the whole loop freezes for up to the 10 s future timeout.
**Location:** `plugins/ai_diary.py`, `_run()` (ThreadPoolExecutor + `asyncio.run` + `future.result(timeout=10.0)`).
**Status:** partially fixed.
**Notes:** `DiaryPlugin.execute_action` is now `async`: the diary-write path awaits `add_diary_entry_async`/`_execute` directly, and the consolidation archive step runs the sync helper via `asyncio.to_thread`, so the action path no longer blocks the event loop. The sync wrappers (`add_diary_entry`, `get_entries_by_tags`, `archive_diary_entries`, ...) still use the `_run` bridge for their remaining sync callers (e.g. `core/webui.py` calls `archive_diary_entries` synchronously from async handlers) — convert those call sites when touching webui.

---

### `radio_host` track verification can store a track *title* as the last track id  <!-- 2026-06-12 -->
**Location:** `plugins/radio_host/track_monitor.py`, `_verify_track_stable()` (the two fallback paths: fetch-exception and incomplete-data).
**Status:** fixed.

---

### `radio_host` pre-generated banter: six concurrent writers race for one slot  <!-- 2026-06-12 -->
**Location:** `plugins/radio_host/radio_host_plugin.py` — `_pending_banter`, `_store_pending_banter`, `_pop_matching_banter`.
**Status:** fixed.

---

### `radio_host` WebDJ: hardcoded credentials and no `wss://` support  <!-- 2026-06-12 -->
**Location:** `plugins/radio_host/azuracast_client.py`, `plugins/radio_host/radio_host_plugin.py`, `jingle_injector.py`.
**Status:** fixed.

---

### `radio_host` `/api/radio/audio?path=` check allows sibling-directory bypass  <!-- 2026-06-12 -->
**Location:** `plugins/radio_host/radio_host_plugin.py`, `_serve_radio_audio` (direct-path branch).
**Status:** fixed.

---

### `radio_host` `_find_active_schedule` likely never matches AzuraCast's schedule shape  <!-- 2026-06-12 -->
**Location:** `plugins/radio_host/radio_host_plugin.py`, `_find_active_schedule`.
**Status:** fixed — pending live verification: confirm `schedule_description` populates on the next radio run against a real AzuraCast.

---

### `memory_search` plugin is deliberately dormant (`PLUGIN_CLASS = None`) with latent bugs  <!-- 2026-06-12 -->
**Symptom:** None at runtime — the `memory_search` action is not registered; the file gives no hint it is disabled.
**Location:** `plugins/memory_search.py` (last line), deactivated in commit `fee51dc` (2026-02-13) when the Recon plugins were introduced; live free-search now goes through `core/prompt_engine.free_memory_search`.
**Status:** known — dormant by maintainer action, not dead code by accident. Latent bugs fixed 2026-06-12.
**Notes:** Nothing in production instantiates `MemorySearchPlugin`, and the loader skips `PLUGIN_CLASS = None` modules. The latent bugs were fixed in place so reactivation is safe: empty OR-joins no longer produce invalid `WHERE ()` SQL for time-window-only free searches, and the chat-history sub-query is now restricted to mode='free' (tags mode with a time window no longer floods results with unrelated chat rows). To reactivate, restore `PLUGIN_CLASS = MemorySearchPlugin`.

---

### `automation_tools/container_synth.sh notify` imports symbols that no longer exist  <!-- 2026-06-12 -->
**Location:** `automation_tools/container_synth.sh`, heredoc in the `notify)` case.
**Status:** fixed.

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
**Location:** `synth-db` MCP diary helper / live `ai_diary` schema mismatch.
**Status:** fixed.

---

### `grillo_activity_log` inserts could return `None` ids on Postgres  <!-- 2026-05-04 -->
**Location:** `plugins/grillo/grillo_impl.py`, `GrilloPlugin.create_activity_log`.
**Status:** fixed.

---

### Automatic diary logging could create internal `diary_consolidation` noise rows  <!-- 2026-05-04 -->
**Location:** `core/action_parser.py`, automatic diary hook in `_create_diary_entry_for_actions`.
**Status:** fixed.

---

### `diary_merge` upserts could overwrite a real `ai_diary.interface` origin  <!-- 2026-05-04 -->
**Location:** `plugins/ai_diary.py`, `_merge_diary_interface` during same-day upsert merge.
**Status:** fixed.

---

### Corrector returns empty when `successful_actions = []`  <!-- 2026-04-13 -->
**Location:** `core/transport_layer.py` → `run_corrector_middleware`, the `if correction_context:` block that builds `correction_message_text`.
**Status:** fixed.

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
**Location:** `tests/test_openrouter_engine.py` patch targets; current engine module lives under `engines/external_engines/openrouter.py`.
**Status:** fixed.

---

### `grillo_outreach` may route to invalid chat id `-1`  <!-- 2026-04-17 -->
**Location:** `plugins/grillo/grillo_outreach.py` target resolution in `_get_target_interface_and_chat`; fallback query over `chat_history_cache` can recover stale interface paths.
**Status:** fixed.

---

### google-genai async close can raise `_async_httpx_client` AttributeError  <!-- 2026-04-17 -->
**Location:** google-genai SDK cleanup path (`google/genai/_api_client.py`) triggered from project client instances in `engines/external_engines/gemini_api.py`, `core/live_session_manager.py`, `plugins/live_engines/gemini.py`, and `core/external_endpoints/adapters/gemini_adapter.py`.
**Status:** fixed.

---

### Langfuse response may attach to wrong request when model label drifts  <!-- 2026-04-18 -->
**Location:** `core/cortex_api_logger.py`, `_pop_langfuse_request` fallback behavior.
**Status:** fixed.

---

### `emotion_diary` legacy schema truncates low intensities to zero  <!-- 2026-04-18 -->
**Location:** MariaDB table `emotion_diary`, `plugins/ai_diary.py` `init_diary_table()`, `plugins/emotion_manager.py`.
**Status:** partially fixed — code resolved 2026-06-12, existing databases still need a manual migration.
**Notes:** Root cause was two competing `CREATE TABLE IF NOT EXISTS` definitions (ai_diary's `intensity INT` variant vs emotion_manager's `intensity FLOAT`); the DDLs are now identical, so fresh databases are correct. **Open maintainer action:** already-deployed databases keep the old table — run `ALTER TABLE emotion_diary MODIFY intensity FLOAT` (plus id/timestamp alignment) on the live DB if accurate emotion history matters.

---

### `grillo_activity_log.diary_entry_id` may reference missing `ai_diary` rows  <!-- 2026-04-18 -->
**Location:** MariaDB source data in `grillo_activity_log` vs `ai_diary`; migration handling in `core/main_db_migration.py`.
**Status:** fixed.

---

### Postgres compat release path can emit unawaited `Pool.release` warnings  <!-- 2026-04-18 -->
**Location:** `core/db_backends.py` (`PostgresCompatConnection.close`), `core/db.py` (`release_conn`, `_ConnProxy.close`).
**Status:** fixed.

---

### Proxy cursors can break `async with` via delegated `__aenter__` lookup  <!-- 2026-04-18 -->
**Location:** `core/db.py` (`ensure_plugin_tables` local `_cursor_ctx` helper, `_ConnProxy.cursor` proxy wrappers).
**Status:** fixed.

---

### `ai_diary` merge query still uses MySQL `GROUP_CONCAT ... SEPARATOR` syntax  <!-- 2026-04-18 -->
**Location:** `plugins/ai_diary.py` (`DiaryPlugin.on_debrief`, query around `_get_unmerged_entries`).
**Status:** fixed.

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
**Location:** `core/message_chain.py`, LLM-originated action normalization before validation.
**Status:** fixed.

---

### OpenAI-compatible external endpoint probe ignored configured adapter timeout  <!-- 2026-04-19 -->
**Location:** `core/external_endpoints/adapters/openai_compat.py` (`_list_models_via_http`, `ping_test`), `core/external_endpoints/probe.py` timeout plumbing.
**Status:** fixed.

---

### `external_endpoints.updated_at` string writes can fail on Postgres-backed endpoint registry paths  <!-- 2026-04-19 -->
**Location:** `core/external_endpoints/registry.py` (`update_endpoint`, `set_subsystem_map`, `_auto_set_default_model`, `set_default_model`).
**Status:** fixed.

---

### Queued trainer notifications could lose `skip_history` and pollute prompt context  <!-- 2026-04-19 -->
**Location:** `core/notifier.py` (`flush_pending_for_interface`), `core/history_engine.py`.
**Status:** fixed.

---

### `ai_diary` consolidation could recursively re-merge whole days and bloat Gemini prompts  <!-- 2026-04-19 -->
**Location:** `plugins/ai_diary.py` (`DiaryPlugin.on_debrief`, `DiaryPlugin.execute_action` for `update_diary_entry`).
**Status:** fixed.

---

### OpenAI-compatible image turns could be silently downgraded to text after a stale probe  <!-- 2026-04-19 -->
**Location:** `core/external_endpoints/bridges/cortex_bridge.py`, `core/external_endpoints/adapters/openai_compat.py`, `core/external_endpoints/probe.py`.
**Status:** fixed.

---

### OpenAI-compatible image-only turns could hallucinate non-visible details  <!-- 2026-04-19 -->
**Location:** `core/prompt_renderers.py` (`OpenAIRenderer.render_with_multimodal`).
**Status:** fixed.

---

### SOUL `async_consolidate` ran on every idle compile  <!-- 2026-04-19 -->
**Location:** `plugins/soul_plugin.py` (`SoulPlugin._compile_interface`).
**Status:** fixed.

---

### SOUL Postgres recall could full-scan memcells and trip static injection timeouts  <!-- 2026-04-19 -->
**Location:** `core/soul/repository.py` (`PostgresSoulRepository.recall_memories`), SOUL Postgres tables `mem_cells` / `mem_cell_vectors`.
**Status:** fixed.

---

### SOUL recall could inject diary-merge housekeeping into live prompts  <!-- 2026-04-19 -->
**Location:** `plugins/soul_plugin.py` (`SoulPlugin._recall_memories`).
**Status:** fixed.

---

### Selective correction context could store counts instead of action lists  <!-- 2026-04-19 -->
**Location:** `core/action_parser.py` (`_request_selective_correction`), `core/transport_layer.py` (`run_corrector_middleware`).
**Status:** fixed.

---

### Telegram multimodal extraction assumed every optional media attribute exists  <!-- 2026-04-19 -->
**Location:** `core/multimodal_attachment.py` (`extract_multimodal_from_telegram`).
**Status:** fixed.

---

### Async diary commands called sync diary retrieval on the event-loop thread  <!-- 2026-04-19 -->
**Location:** `core/command_registry.py` (`diary_command`, `context_command`), `core/generic_commands.py` (`generic_diary_command`, import of `last_chats_command_generic`).
**Status:** fixed.

---

### Recovered JSON with extra trailing content could drop later actions silently  <!-- 2026-04-20 -->
**Location:** `core/message_chain.py` (`handle_incoming_message` recovery/correction branch), `core/transport_layer.py` (`extract_json_from_text`).
**Status:** fixed.

---

### External cortex bridge dropped PromptRequest and fell back to legacy JSON flattening  <!-- 2026-04-20 -->
**Location:** `core/plugin_instance.py` (`prompt.pop("__prompt_request", None)` handoff), `core/external_endpoints/bridges/cortex_bridge.py` (`ExternalCortexEngine`).
**Status:** fixed.

---

### External OpenAI-compatible PDF attachments were serialized as image parts  <!-- 2026-04-20 -->
**Location:** `core/external_endpoints/bridges/cortex_bridge.py` (`ExternalCortexEngine._format_mm_part`, `_build_mm_parts_from_prompt_request`), `core/prompt_renderers.py` (`_build_multimodal_turn_text`, `OpenAIRenderer.render_with_multimodal`).
**Status:** fixed.

---

### External OpenAI-compatible adapters still do not use native tool calls end-to-end  <!-- 2026-05-07 -->
**Symptom:** External Gemini cortex turns now log native `tools` payloads and can return parsed function-call actions, but OpenAI-compatible external endpoints can still rely on freeform JSON-in-text responses instead of native tool calls. MCP traces for external OpenRouter-backed turns may still show `messages` only or text-only completions with malformed multi-action JSON.
**Location:** Remaining gap is primarily `core/external_endpoints/adapters/openai_compat.py` (`chat_completion` still returns `message.content` only, no tool-call parsing) plus any other non-Gemini external adapters that do not consume native tool declarations. External Gemini path is now handled by `core/external_endpoints/bridges/cortex_bridge.py` and `core/external_endpoints/adapters/gemini_adapter.py`.
**Status:** partially fixed.
**Notes:** The external bridge now preserves `PromptRequest` tool declarations for Gemini endpoints, forwards Gemini-native `tools`, and the SDK adapter normalizes Gemini `function_call` responses back into SyntH JSON actions. The remaining end-to-end native tool-calling gap is on external OpenAI-compatible and other non-Gemini adapters.

---

### Gemini tool manifests could lose normalized action parameters and yield empty payloads  <!-- 2026-05-07 -->
**Location:** `core/live_tool_registry.py` (`build_manifests_from_actions`, plus shared manifest extraction for action definitions built from normalized `schema` blocks).
**Status:** fixed.

---

### Selective correction retried safety-blocked actions and wasted an extra LLM call  <!-- 2026-04-20 -->
**Location:** `core/action_parser.py` (`_request_selective_correction`).
**Status:** fixed.

---

### Literal newlines inside JSON strings could trigger a spurious corrector round-trip  <!-- 2026-04-20 -->
**Location:** `core/transport_layer.py` (`extract_json_from_text`).
**Status:** fixed.

---

### Legacy `diary_entry` action alias could trigger an avoidable correction hop  <!-- 2026-04-20 -->
**Location:** `core/message_chain.py` (`handle_incoming_message` normalization path before unsupported-action validation).
**Status:** fixed.

---

### Legacy `diary` action and diary payload aliases could still force correction or drop diary metadata  <!-- 2026-04-20 -->
**Location:** `core/message_chain.py` normalization helpers before unsupported-action validation and action execution.
**Status:** fixed.

---

### Standalone `thought` action could still force correction instead of populating diary metadata  <!-- 2026-04-20 -->
**Location:** `core/message_chain.py` diary normalization helpers before unsupported-action validation.
**Status:** fixed.

---

### `chat_history_cache` deduplication query still used MySQL `DATE_SUB(... INTERVAL ...)` syntax  <!-- 2026-04-20 -->
**Location:** `core/chat_history_cache.py` (`save_chat_message`, duplicate-message guard query).
**Status:** fixed.

---

### `chat_update_checker` DB polling still used MySQL `UNIX_TIMESTAMP(...)` syntax  <!-- 2026-04-20 -->
**Location:** `core/chat_update_checker.py` (`ChatUpdateChecker._check_once`).
**Status:** fixed.

---

### `grillo_chat_observer` direct DB probe still uses MySQL `UNIX_TIMESTAMP(...)` syntax  <!-- 2026-05-05 -->
**Location:** `plugins/grillo/grillo_chat_observer.py` (`GrilloChatObserverPlugin._run_observer`).
**Status:** fixed.

---

### `memory_consolidation` beats were using a stale plugin prompt override  <!-- 2026-05-05 -->
**Location:** `plugins/grillo/grillo_impl.py` (`GrilloPlugin._create_beat_prompt`) with the stale override in `plugins/grillo/grillo_memory.py`.
**Status:** fixed.

---

### Tagged memory recall helpers still used MariaDB-only JSON predicates on Postgres  <!-- 2026-05-06 -->
**Location:** `core/synth_core_memory.py` (`search_memories`), `core/prompt_engine.py` (`search_memories`), `plugins/memory_search.py`, `plugins/ai_diary.py` tag/person lookups, and `plugins/grillo/grillo_compactor.py` marker filtering.
**Status:** fixed.

---

### SOUL static injection could time out on internal `grillo/-1` turns  <!-- 2026-04-20 -->
**Location:** `plugins/soul_plugin.py` (`SoulPlugin.get_static_injection`).
**Status:** fixed.

---

### Grillo outreach synthetic message ids could skip PromptRequest assembly  <!-- 2026-04-20 -->
**Location:** `core/prompt_engine.py` (`_assemble_prompt_request`, `RuntimeContext.message_id` assignment).
**Status:** fixed.

---

### Exact runtime timestamps could make trainer replies over-mention time and location  <!-- 2026-04-20 -->
**Location:** `core/prompt_renderers.py` (`_build_runtime_prefix`), `core/prompt_engine.py` (`load_unminified_chat_instruction`).
**Status:** fixed.

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
**Location:** `core/webui.py` (`history_diary`).
**Status:** fixed.

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
**Location:** `core/prompt_engine.py` (`_history_to_turns`) fed by same-chat `history_current_chat` from `core/history_engine.py` / `chat_history_cache`.
**Status:** fixed.

---

### External Gemini 503 overloads were not considered retryable  <!-- 2026-05-07 -->
**Location:** `core/external_endpoints/bridges/cortex_bridge.py` (`ExternalCortexEngine._is_retryable_exception`).
**Status:** fixed.

---

### WebUI phase logs could hide valid THINKING/WRITING transitions  <!-- 2026-04-22 -->
**Location:** `core/action_state_manager.py`, `res/synth_webui/js/chat-window.mjs`
**Status:** fixed.

---

### `SYNTH_PRIMARY_DB=memory` can inherit stale MariaDB cortex settings for Grillo  <!-- 2026-05-05 -->
**Symptom:** After switching to `SYNTH_PRIMARY_DB=memory`, fresh `grillo_activity_log` rows can appear in MariaDB with empty `response_text` / `diary_entry_id`, while logs show `[cortex_bridge:<engine>] generate_response failed: Connection error.` for internal `grillo/-1` beats.
**Location:** Selected primary DB config registry (`BASE_CORTEX`, `GRILLO_CORTEX`) plus runtime Grillo prompt execution.
**Status:** known / configuration-dependent.
**Notes:** The DB selector itself can work correctly while still exposing older config values from the chosen DB. In the observed MariaDB case, `GRILLO_CORTEX=Default` fell through to `BASE_CORTEX=gemma`, so Grillo inherited a dead engine after the switch. When changing primary DBs, verify or realign the selected DB's cortex config keys, not just the connection settings.

---

### `universal_send skip_history=True` was ignored and polluted `chat_history_cache`  <!-- 2026-05-05 -->
**Location:** `core/transport_layer.py` (`universal_send` history-save path), plus tests calling `send_llm_fallback_message()` / `universal_send()` with synthetic interface paths.
**Status:** fixed.

---

### Telegram send failures could mask the real error with undefined `correction_payload`  <!-- 2026-05-05 -->
**Location:** `interface/telegram_bot.py`, `TelegramInterface.send_message` exception handling.
**Status:** fixed.

---

### Telegram startup timeout could leave the interface permanently half-initialized  <!-- 2026-05-06 -->
**Location:** `interface/telegram_bot.py` (`start_bot`, `TelegramInterface.send_message`, `shutdown_interface`) and `plugins/message_plugin.py`.
**Status:** fixed.

---

### Langfuse Gemini generations could keep token summaries empty  <!-- 2026-05-06 -->
**Location:** `core/cortex_api_logger.py` generation logging, `engines/external_engines/gemini_api.py` usageMetadata mapping, and `core/external_endpoints/adapters/gemini_adapter.py` SDK usage-metadata logging.
**Status:** fixed.

---

### External cortex bridge could misreport adapter timeouts as 300s and keep retrying them  <!-- 2026-05-06 -->
**Location:** `core/external_endpoints/bridges/cortex_bridge.py` (`_get_request_timeout`, `generate_response`).
**Status:** fixed.

---

### Langfuse request traces could remain invisible until the response finished  <!-- 2026-05-06 -->
**Location:** `core/cortex_api_logger.py` (`log_cortex_request`).
**Status:** fixed.

---

### Langfuse API-error traces could look half-empty and mislabel Gemini provider failures  <!-- 2026-05-06 -->
**Location:** `core/cortex_api_logger.py` (`log_cortex_response` error-output handling) and `core/external_endpoints/adapters/gemini_adapter.py` exception logging.
**Status:** fixed.

---

### Same-chat user activity could cancel an in-flight Grillo outreach  <!-- 2026-05-06 -->
**Location:** `core/message_queue.py` low-priority background task tracking and cancellation.
**Status:** fixed.

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
**Location:** `core/chat_history_cache.py` (`load_chat_history` ordering/limit), `core/webui.py` (`_ensure_session_history_loaded`), and `core/chat_context_manager.py` (prompt-context deque size).
**Status:** fixed.

---

### `grillo_activity_log` can show silent blank outreach rows after log rotation  <!-- 2026-05-07 -->
**Location:** Runtime observability split across `logs/synth*`, `grillo_activity_log`, `chat_history_cache`, `core/external_endpoints/adapters/gemini_adapter.py`, and `core/plugin_instance.py` (`_update_grillo_response`).
**Status:** fixed.

---

### Radio host KittenTTS volume — hard clipping distortion fixed with ffmpeg dynaudnorm  <!-- 2026-05-24 -->
**Location:** `plugins/vox_engines/kitten.py` (`generate_tts`), `plugins/radio_host/azuracast_client.py` (`_convert_to_webm`).
**Status:** fixed.

---

### Radio host injection at track_change was too slow for timely announcements  <!-- 2026-05-24 -->
**Location:** `plugins/radio_host/radio_host_plugin.py` (`_on_track_change`, `_inject_banter_now`, `_on_winding_down`).
**Status:** fixed.

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
