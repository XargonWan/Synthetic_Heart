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

The **Agent plugin** (`plugins/agent_plugin.py`) exposes Synth's agentic tools (`agent_list_files`, `agent_read_file`, `agent_write_file`, `agent_edit_file`, `agent_search_files`, `agent_run_shell`, `spawn_drone`) to the Agentic Runtime 2.0. Task state is persisted in the `agent_tasks` table. Enablement is gated by `AGENT_ENABLED` (user toggle, re-read on every `is_enabled()` call); the router 2.0 additionally requires `AGENTIC_ROUTING_ENABLED`.

`agent_write_file` (`required_fields: ["path", "content"]`, `optional_fields: ["mode"]`, `security_level: "medium"`, `external_effects: ["filesystem"]`) writes a text file inside the sandbox. It reuses `_resolve_safe_path()` / `_allowed_roots()` (same roots as `agent_read_file`: `AGENT_FS_ROOTS`, else `[AGENT_FS_ROOT|/app, SYNTH_LOG_DIR|/app/logs]`), creates parent dirs, supports `mode` = `"overwrite"` (default) or `"append"`, and caps content at 2 MB. `external_effects` makes `core/agent_router.py` route it to the Agent Lane automatically. This is a **native Python** action — chosen over the standard filesystem MCP (`@modelcontextprotocol/server-filesystem`, pre-registered but `"enabled": false` in `config/synth_mcp.json`) because the runtime image (`python:3.12-slim`) has no node/npx (node lives only in the Dockerfile `stage_builder` build stage), so an `npx`-based MCP server cannot start in-container.

`agent_edit_file` (`required_fields: ["path", "old_string", "new_string"]`, `optional_fields: ["expected_replacements"]`, `security_level: "medium"`, `external_effects: ["filesystem"]`) does a literal find-and-replace inside a sandboxed text file. It reads via `_resolve_safe_path()`, counts occurrences of `old_string`, and requires the count to exactly equal `expected_replacements` (default 1, clamped 1–10000 via `_safe_int()`) — an ambiguous or missing match errors out rather than editing the wrong spot. `old_string`/`new_string` must be non-empty strings and must differ; the resulting content is capped at 2 MB. Returns `{"status": "ok", "path", "replacements", "bytes_written"}`. `external_effects: ["filesystem"]` routes it to the Agent Lane automatically. Native Python for the same no-node reason as `agent_write_file`.

`agent_search_files` (`required_fields: ["pattern"]`, `optional_fields: ["path", "regex", "case_sensitive", "glob", "max_results", "max_file_bytes"]`) is a **read-only** in-sandbox grep — it has **no** `security_level`/`external_effects` and stays on the Fast Lane. `pattern` is a plain substring by default, or a Python `re` pattern when `regex` is true; `case_sensitive` (default False) toggles `re.IGNORECASE`. `path` (default the first allowed root) is confined via `_resolve_safe_path()`; when it's a directory the search recurses with `rglob(glob)` (`glob` default `"*"`), skipping files larger than `max_file_bytes` (`_safe_int` default 2 MB, 1000–20 000 000). Results cap at `max_results` (`_safe_int` default 200, 1–2000) and each line is truncated to 1000 chars. Returns `{"status": "ok", "path", "files_scanned", "count", "truncated", "matches": [{"path", "line", "text"}]}`. Native Python for the same no-node reason as `agent_write_file`.

`agent_run_shell` (`required_fields: ["command"]`, `optional_fields: ["cwd", "timeout"]`, `security_level: "high"`, `external_effects: ["shell"]`) runs a shell command and returns `{status, exit_code, cwd, stdout, stderr, truncated}`. **Its security is gated by container detection.** The module-level helper `_is_in_container()` decides the environment via, in order: the explicit `SYNTH_IN_CONTAINER` env override → presence of `/.dockerenv` (Docker) or `/run/.containerenv` (Podman) → a `docker`/`kubepods`/`containerd`/`libpod` marker in `/proc/1/cgroup`; it defaults to `False` (host) when unsure. `_run_shell()` **only executes inside a container** (the disposable runtime image); on a bare host it refuses unless the `AGENT_SHELL_ALLOW_HOST` config var (default `False`) is explicitly enabled — because a shell on the host is a real machine-compromise risk for a public persona. The working directory (`cwd`, default the first allowed root) is confined to `_allowed_roots()` via `_resolve_safe_path()`; the command runs through `bash -c`/`sh -c` under `asyncio.create_subprocess_exec`, with a `timeout` clamped to 1–600 s (default 60) and stdout/stderr each capped at 40 000 chars. `external_effects: ["shell"]` routes it to the Agent Lane automatically. Native Python for the same no-node reason as `agent_write_file`.

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

**Vox per-language routing:** `VOX_LANGUAGE_OVERRIDES` (JSON map `iso639-1 → {engine, model, voice}`, registered hidden in `plugins/vox_plugin.py`) lets TTS use a different engine/model/voice per detected language. Resolution lives in `core/config.py::get_vox_language_override(language)` (normalises region codes, returns `None` for unknown/`"disabled"` engines). `VoxPlugin.speak()` (in `plugins/vox_plugin.py`) calls it after `lingua` language detection and, when present, loads that engine and forwards its `model`/`voice` as explicit per-call kwargs (the bridge `vox_bridge.generate_tts` already prioritises explicit values). An explicit per-call `engine_name` always wins over the override. UI: classic WebUI (`res/synth_webui/js/main.js` + `engines.html`, "Add language override" editor) and Vue (`frontend/src/components/settings/VoiceSettings.vue`); both read the catalogue from `GET /api/languages` (`core/languages.py::SUPPORTED_LANGUAGES`).
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

## 5b. Agentic Runtime (Tools & MCP)

SyntH can act as an **agent**: it calls *tools* — native actions and remote MCP
tools — inside a bounded reasoning loop. Implemented in the `feat/agentv2` work.

**Golden rule — dev MCP stays separate.** Synth's *own* MCP support lives
**only** in `config/synth_mcp.json` + `core/mcp_bridge/`. The developer MCP
servers (`.mcp.json`, `mcp_servers/*.py`) are never touched by this runtime.

| Concern | Location |
|---------|----------|
| Synth-owned MCP registry | `config/synth_mcp.json` (top-level key `synthMcpServers`) |
| Registry loader (fail-safe) | `core/mcp_bridge/config.py` |
| Unified tool registry | `core/tool_registry.py` (`ToolRegistry`, `UnifiedToolManifest`) |
| MCP client bridge | `core/mcp_bridge/client.py` (`McpClientBridge`, `mcp_client_bridge`) |
| Tool executor (single gate) | `core/agent_tool_executor.py` (`AgentToolExecutor`, `agent_tool_executor`) |
| Bounded agent loop | `core/agent_core.py::AgentLoopManager.run_agentic_turn` |
| Fast/Agent router | `core/agent_router.py` (`classify`, `route`) |
| Expose Synth actions as MCP | `core/mcp_bridge/server.py` (`build_server`, FastMCP) |

**Every action is a tool — automatically.** By design, any action registered
through a plugin/interface `get_supported_actions()` is *automatically* exposed
as an MCP tool named `synth_<action_name>` (e.g. `message_telegram_bot` →
`synth_message_telegram_bot`). There is **no whitelist** and **no developer
opt-in**: `core/mcp_bridge/server.py::_get_exposed_action_names()` enumerates the
full internal action set from `core/tool_registry.py::tool_registry.internal_tools()`.
MCP tool names have no hard `tool_` prefix requirement — the `synth_` prefix is a
namespacing choice. Safety is unchanged: every call still funnels through
`core.action_safety.is_action_allowed_for_execution`, so each action's security
level is enforced regardless of how it is invoked.

**Two lanes, one chain.** `core/agent_router.classify` is a pure deterministic
function: multiple actions, a tool call (`mcp_*` or an internal action with
external effects), or a multi-step intent → **Agent Lane**; a single pure
message → **Fast Lane** (unchanged path). Gated by `AGENTIC_ROUTING_ENABLED`
(default `False`).

**Tools are actions.** Internal actions and remote MCP tools are unified in
`ToolRegistry`. Every tool — internal or external — funnels through
`core.action_safety.is_action_allowed_for_execution`. Internal tools dispatch
via `run_action`; external MCP tools via `mcp_client_bridge.call_tool`. Tool
names are namespaced `mcp_<server>_<tool>`.

**Config keys:** `AGENTIC_ROUTING_ENABLED`, `AGENT_MAX_ITERATIONS` (30),
`AGENT_TURN_TIMEOUT_SEC` (120), `SYNTH_MCP_CONFIG`.
See `docs/agentic_tools.rst` for the full reference.

**Drones — ephemeral sub-agents.** The Agent can delegate a focused, self-contained
sub-task to a **Drone**: a short-lived sub-agent that runs its own bounded
`run_agentic_turn` loop with the full tool set and returns a concise result. Drones
keep the parent task clean (research, scoped lookups, multi-step file inspection).

- **Spawn:** only via the `spawn_drone` action (`required_fields: ["goal"]`,
  `optional_fields: ["engine", "max_iterations"]`, `security_level: "medium"`),
  handled in `plugins/agent_plugin.py`. There is **no** direct user/interface spawn.
- **Single-level delegation — Drones cannot spawn Drones.** Enforced twice:
  (1) `AgentLoopManager._build_agent_prompt` hides `spawn_drone` from a Drone's tool
  list when `context["drone"]["is_drone"]` is set; (2) the `spawn_drone` handler
  returns `{"ok": False, "error": "drones_cannot_spawn_drones"}` if invoked from
  within a Drone.
- **Engine inheritance:** when `engine` is omitted, a Drone resolves the same
  agent-scope cortex as its parent (`get_active_cortex_engine(scope="agent")` →
  `AGENT_CORTEX` → `BASE_CORTEX`). An explicit `engine` in the payload wins.
- **Budget:** tighter than the parent — `DRONE_MAX_ITERATIONS` (3),
  `DRONE_TURN_TIMEOUT_SEC` (90).
- **Persistence:** Drone turns are recorded in `agent_tasks` with
  `metadata.source = "drone"` and `metadata.drone.parent_task_id` linking them to
  the spawning Agent task. No new DB table.
- **Entry point:** `AgentLoopManager.run_drone(...)` in `core/agent_core.py` —
  additive over `run_agentic_turn` (no signature change to the existing loop).

**Drone config keys:** `DRONE_MAX_ITERATIONS` (3), `DRONE_TURN_TIMEOUT_SEC` (90).

---

## 5c. Rift Vessel — Multi-World Embodiment

SyntH is a persistent cognitive entity; a **Vessel** is a layer of embodiment into an external world. The Rift Vessel subsystem lets SyntH inhabit game/virtual worlds (Minecraft shipped as PoC; Skyrim/VRChat/Hytale are registry-ready) through pluggable **connectors**, while identity/memory/personality persist across worlds and chat interfaces. Full reference: `docs/rift_vessel.rst`.

| Concern | Location |
|---------|----------|
| Connector registry (Iris pattern) | `core/vessel_registry.py` (`VESSEL_REGISTRY`, `register_vessel_connector`) |
| Connector base + schema | `plugins/vessel_base.py` (`VesselConnectorBase` ABC, `WorldState`, `PerceptionEvent`, `VesselActionResult`) |
| Actions facade | `plugins/vessel_plugin.py` (`VesselPlugin`, `PLUGIN_CLASS`) |
| Session lifecycle + experience buffer | `core/vessel_session_manager.py` (`vessel_session_manager`) |
| I/O interface (duck-typed) | `interface/vessel_interface.py` (`INTERFACE_NAME = "vessel"`) |
| Minecraft PoC connector | `plugins/vessels/minecraft_connector.py` (`CONNECTOR_CLASS`, self-registers) |
| Mineflayer bridge (Node.js) | `interface_dev/minecraft_bridge_minimal.js` |
| Bridge provisioner | `interface/minecraft_provisioner.py` (`BridgeProvisioner`, `get_bridge_provisioner`) |
| DB tables | `core/db.py::init_vessel_tables` + `init-db.sql` (`vessel_sessions`, `vessel_activity_log`) |
| WebUI Activities voice | `core/webui.py` (`/api/history/vessel`), `history.html`/`history.js` (🌀 sub-tab) |
| CLI | `core/command_registry.py` (`/vessel status`, `/minecraft provision …`) |

**Three hard constraints (all enforced):**

1. **Vessel actions never create agentic tasks.** `vessel_say`/`vessel_move`/`vessel_look`/`vessel_use`/`vessel_status` declare **no** `external_effects` → they stay on the Fast Lane (`run_actions`), never the Agent Lane / Drones. (They are still passively auto-exposed as MCP tools `synth_vessel_*`.) A connector talks to its world directly; no reasoning loop is needed.
2. **No diary during a session.** Events accumulate in an in-DB `experience_buffer` on `vessel_sessions`; a **single** autobiographical "lived experience" diary entry is written **only at end-of-session** — explicit logout OR `VESSEL_SESSION_COOLDOWN_SEC` (default 3600 s) of inactivity, detected by the interface scheduler calling `close_expired_sessions`.
3. **Own Activities voice.** Like Radio/Grillo: `vessel_activity_log` + `/api/history/vessel` (GET history, DELETE per-item) + a dedicated History sub-tab.

**Perception & salience:** the PoC filter is LLM-free — dedup (30 s) + rate-limit (2 s) in `interface/vessel_interface.py`. A richer LLM salience/attention worker (Grillo *RAW cognition* style) is a documented future phase and must also respect constraint 1. Never stream raw telemetry into cognition.

**Adding a connector:** subclass `VesselConnectorBase`, set module-level `CONNECTOR_CLASS`, and call `register_vessel_connector(name, __name__, capabilities=..., label=...)` at import time. Removing any connector/plugin/interface must not break the rest of the system.

**Minecraft PoC deployment:** single-container, **opt-in** via `MINECRAFT_BRIDGE_ENABLED` (default False). Node is **not** in the default image (`python:3.12-slim`) — build with `docker build --build-arg INSTALL_NODE=true …` (Dockerfile `ARG INSTALL_NODE=false` + conditional NodeSource install). The provisioner runs the bridge as a **non-root** subprocess and returns a clear error if `node`/`npm` are missing. Uses offline auth; real Microsoft/XBL auth is out of scope.

**Vessel config keys:** `ACTIVE_VESSEL` (`"disabled"`), `VESSEL_SETTINGS`, `VESSEL_SESSION_COOLDOWN_SEC` (3600), `MINECRAFT_BRIDGE_ENABLED` (False), `MINECRAFT_BRIDGE_RUN_AT_START`, `MINECRAFT_BRIDGE_HOST` (127.0.0.1), `MINECRAFT_BRIDGE_PORT` (8137), `MINECRAFT_SERVER_HOST` (127.0.0.1), `MINECRAFT_SERVER_PORT` (25565), `MINECRAFT_BOT_USERNAME` (Synth).

---

## 6. Interfaces

- Manage I/O with external systems.
- Must forward all input into the core message chain and dispatch outputs from it.
- Never bypass the chain.
- Register actions via `get_supported_actions()`.

### Outbound file sending

Telegram, Discord, and Matrix each expose a single generic **send-file** action so
Synth can push a local file to a user/channel/room. All three share the sandbox
path-safety and MIME-detection helper `core/outbound_file_utils.py`:

- `resolve_safe_outbound_path(raw_path)` confines the source to the same sandbox
  roots as the Agent tools (`AGENT_FS_ROOTS`, else `[AGENT_FS_ROOT|/app, SYNTH_LOG_DIR|/app/logs]`);
  relative paths resolve against the first root. Only existing regular files inside a
  root pass — traversal, missing files, and directories are rejected.
- `classify_media(path)` returns `image` / `video` / `audio` / `document` (MIME first,
  extension fallback); `guess_mime_type(path)` backs it with an
  `application/octet-stream` fallback.

| Action | Interface | Required | Optional |
|--------|-----------|----------|----------|
| `send_file_telegram_bot` | `interface/telegram_bot.py` | `path`, `interface_path` | `chat_name`, `caption` |
| `send_file_discord_bot` | `interface/discord_interface.py` | `path` | `interface_path`, `target`, `channel_id`, `caption` |
| `send_file_matrix_chat` | `interface/matrix_interface.py` | `path`, `target` | `caption`, `thread_event_id` |

All three are `security_level: "medium"`, `external_effects: ["filesystem"]` — so the
router 2.0 auto-routes them to the Agent Lane, and (like every action) each is
auto-exposed as an MCP tool (`synth_send_file_*`). Captions longer than the
interface limit are split into a follow-up text message.

**Audio stays playable.** A file classified as audio is delivered as a *playable*
message, not an inert attachment: Telegram uses `send_audio`, Matrix uses the
`m.audio` msgtype. The pre-existing dedicated `audio_*` actions are untouched — use
`send_file_*` only when a caller explicitly wants a generic file.

---

## 7. Animation System

The `AnimationHandler` (`core/animation_handler.py`) manages VRM avatar animations with state-based triggering.

### The Karada state server is the single source of truth

**`KaradaStateServer` (`core/animation_handler.py`, accessed via `get_karada_state_server()`) is the single source of truth for everything the avatar does — animation, face, expressions, and audio/"speaking" state.** It owns that state and *distributes* it to every connected client through a transport abstraction (`KaradaTransport` in `core/karada_transport.py`; the WebSocket implementation is `core/karada_ws_transport.py`).

Clients are passive receivers. The WebUI is **just a client** — and any future client (Android app, XR headset, etc.) is another one. When a new client connects it must be able to read the current server state and catch up (late-join replay via `get_current_audio()` / current animation).

**Rules:**

- Plugins and interfaces must **NEVER** iterate individual client connections to push avatar state. Always drive the server.
- To make the avatar speak, call `await get_karada_state_server().broadcast_audio(audio_path=..., lipsync_data=..., audio_duration_s=..., text=...)`. The server fans a `tts-play` command out to **all** transports and records the audio so late-joiners catch up. This is the *only* place that broadcasts speaking state.
- This holds regardless of which interface originated the turn (e.g. an audio received via Telegram while the WebUI is open) and whether the turn was automatic or explicitly triggered.
- Interface-specific *native* delivery (Telegram `audio_telegram_bot`, Discord audio, the WebUI chat caption bubble) is a separate concern handled by each interface — it must not re-broadcast the shared avatar audio.
- To add a new client type, implement a new `KaradaTransport` and register it with the server. Do **not** add per-client logic in plugins.

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
- **Never use SQL reserved words as bare column names.**
  `timestamp` is a PostgreSQL reserved word. A bare `timestamp` column on a fresh Postgres install is auto-translated by the ORM to `timestamptz`, producing an invalid schema that leaves SyntH broken (T-pose, unable to do anything). Always name time columns explicitly, e.g. `created_at`, `event_timestamp`, `updated_at`. This applies to every DDL in `init-db.sql`, `scripts/sql/*.sql`, inline plugin DDL, and `core/migrations.py`. Public API dict keys returned to the WebUI/JS may still be named `"timestamp"` — only the DB-column SQL references are forbidden from using the reserved word.

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

> **Agent instruction:** When you encounter a bug, error pattern, or non-obvious workaround that isn't already listed here, append a new entry before finishing your session in the CHANGELOG.md. Use the format below. Do **not** fix it unless asked — the point is to stop future agents from wasting tokens rediscovering it.
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

### Grillo observer feeds days-old chat snippets as "recent" — no freshness gate, no age markers, so outreach continues stale threads  <!-- 2026-07-12 -->
**Symptom:** After outreach started firing again, the first beat after an idle period reached into a group chat grabbing very stale context, and the subsequent DM outreaches were topically relevant but to things that happened days ago. Langfuse trace `7f4dbeda-a3fc-49db-8a45-4a2042f32e06` (observer beat) shows the injected snippet block containing messages dated `2026-07-10`, `2026-07-06`, `2026-07-03`, `2026-07-02` — 1.7 to 10 days old — under a header that called them "recent chat snippets," and the model's own rationale was "reacting to how needy I was in **that last snippet**," i.e. it treated a 2-day-old group line as the current moment and continued it.
**Location:** `plugins/grillo/grillo_chat_observer.py::_collect_recent_snippets` (took the last 2 messages of each recent interface_path with NO age filter) and `_build_observer_prompt` (header asserted "recent," snippets carried a raw ISO timestamp but no relative-age framing). This is the concrete recurrence of the open 2026-07-05 "conversation-history turns carry no timestamps — staleness is invisible to the model" note, on the observer/outreach path specifically.
**Status:** fixed 2026-07-12.
**Root cause:** The observer's snippets are the only fresh-context signal outreach reacts to, but nothing conveyed their age. The single existing time gate (`self_skip_window`) only skips a chat when the *synth* spoke last recently — it does nothing to stop ancient *human* lines being presented as live. Small models can't infer staleness from a bare ISO timestamp, so a days-old line reads as "now" and gets continued.
**Fix:** `_collect_recent_snippets` still loads the last X entries (config `GRILLO_OBSERVER_SAMPLES`, default 10) but now annotates each snippet with a relative-age marker via a new `_humanize_age` helper (`age:just now` / `age:Nm ago` / `age:Nh ago` / `age:Nd ago`), and `_build_observer_prompt`'s header tells the model to treat older snippets as historical context and not continue a stale line as if it just happened. The model judges staleness itself from the markers — there is deliberately **no hard age gate**, so outreach always has context behind it. (An earlier draft added a `GRILLO_OBSERVER_SNIPPET_MAX_AGE_HOURS` cutoff; it was removed as wrong — with `samples=10` a normal day of use burns through any fixed window, and an over-tight gate can filter every snippet to empty and force context-less outreach. Temporal tagging is the correct mechanism, not filtering.) Validated: scoped `ruff`/`ty` clean; `tests/test_grillo_observer.py` = 6 passed, 2 failed — the 2 failures are the pre-existing DB-refused (`WinError 1225`) tests documented below (confirmed identical via `git stash` A/B on `develop`).
**Notes:** The eligible-targets `idle=` value and the `age:` snippet marker are independent (target idle = whole-chat recency; snippet age = per-message). The turn-by-turn `conversation_history` block (separate from observer snippets) still lacks per-turn age markers — see the 2026-07-05 note; this fix only covers the observer snippet path.

---

### New Vue stage VRM freezes on `skin_change` — intro+outro descriptor with no `loop` section dead-ends the client state machine  <!-- 2026-07-12 -->
**Symptom:** On loading the new VRM stage (`type=stage`), the model renders, plays a short motion (looks at its left hand), then freezes mid-pose with mangled fingers and a fixed "creepy" smile while the render loop keeps running. Reproduces on every VRM-set / stage connect. `synth.log` shows the (cosmetic) backend line `[KaradaStateServer] Animation 'Look Around.fbx' has both 'play_once' flag and structured sections (intro/outro). 'play_once' will be ignored ... Structure: intro=True, loop=False, outro=True` (animation_handler.py:1494) immediately after `SET ACTIVE VRM END`.
**Location:** `frontend/src/composables/vrm/animation.ts::updateDescriptorStateMachine` (new Vue-stage Karada engine). Trigger descriptor: `skins/Rei/animations/skin_change/Look Around.fbx.json` (`intro` 0–60, `outro` 61–120, `play_once: true`, **no `loop`**). Same latent gap exists in the legacy `res/synth_webui/js/vrm-animation-engine.mjs`.
**Status:** fixed 2026-07-12 (new stage only).
**Root cause:** `skin_change` is the stage bootstrap animation. Its descriptor has an intro and an outro but NO `loop` section. `playAnimation` starts the intro as `LoopOnce` with `clampWhenFinished = true`. `updateDescriptorStateMachine` only advanced intro→loop `&& loop` — with no loop it did nothing, and the outro is normally only fired externally by `stopAnimation()`, which never runs for skin_change (no follow-up state arrives). So the intro action clamped permanently on its last frame (frame 60 = "looking at left hand"); the mangled fingers/fixed smile are that frozen retargeted last frame with no facial descriptor to reset expressions. The state machine had no path for "intro finished, no loop".
**Fix:** when the intro completes and there is no `loop`, the state machine now drives **intro → outro → idle** (via `playSection('outro')` + a `setTimeout` sized to the outro duration that calls `transitionToIdle`), or **intro → idle** directly when there is also no outro. The `currentSection === 'intro'` guard prevents the per-frame ticker from re-triggering once outro starts. Verified: `npm run typecheck` (vue-tsc) clean. **Deploy reminder:** the Docker-served stage bundle is built from the image — a frontend rebuild (or dev server) is needed for this to reach the browser; host-side `.ts` edits alone do not hot-reach the container-served UI (see the "Docker-served WebUI can keep stale static JS" entry).

---

### New Vue stage VRM intermittently snaps to (partial) T-pose while idling — base-idle floor drop lowers the SAME action serving as foreground for skinless-idle skins  <!-- 2026-07-12 -->
**Symptom:** On the new VRM stage, after idling normally for a while the model snaps to a T-pose (or a mangled near-T-pose, "tweaking out"), then recovers to normal idle a long time later. Correlated by the reporter with a `304 Not Modified` for a **Rei** idle asset in the console — while the active skin was **2B**. Recovery lines up with the next Grillo `write`→`idle` beat (~30 min cadence in `synth.log`: 01:38, 02:08, 02:37).
**Location:** `frontend/src/composables/vrm/animation.ts::scheduleBaseIdleFloorDrop` (+ its callers `playAnimation`/`playSection`/`transitionToIdle`); interacts with `frontend/src/composables/vrm/avatar-driver.ts::createAvatarDriver` (idle fallback = `idles[0]`) and `animation-cache.ts` (returns one shared clip object per URL). Only surfaced after the 2026-07-12 skin_change fix let the model settle cleanly into a plain `idle`.
**Status:** fixed 2026-07-12 (new stage only).
**Root cause:** Skins with no idle animation of their own (e.g. 2B) fall back to `idles[0]` = **Rei's idle** (the "304 for Rei while on 2B"). The persistent `baseIdleAction` is built from that fallback idle clip. When a plain `idle` state then arrives, `playAnimation` does `this.currentAction = this.mixer.clipAction(clip)` with that same idle clip — and three.js `AnimationMixer.clipAction()` is **cached by clip object**, so `currentAction` becomes the *exact same action instance* as `baseIdleAction`. `playAnimation`'s idle path then calls `scheduleBaseIdleFloorDrop()`, whose 420ms timer sets the base idle's weight to the `0.12` floor — but that base idle IS the only full-weight foreground driver, so the skeleton drops to ~12% animation / ~88% bind pose = the (partial) T-pose. It self-recovers on the next `write` beat because `write` uses a *different* clip (Texting.fbx) → a distinct foreground action at full weight is restored.
**Fix:** the floor-drop timer now checks `this.currentAction !== this.baseIdleAction` at **fire time** before lowering the weight — so when the base idle is itself the current foreground action the drop is a no-op and it stays at full weight. Fire-time (not schedule-time) checking also closes the 420ms race and covers all three schedulers (`playAnimation` idle `else` branch, `playSection`, `transitionToIdle`). `transitionToIdle`'s own base-idle-promotion path already set weight 1.0, so it was unaffected. Verified `npm run typecheck` clean. Same deploy reminder as the entry above (stage bundle is image-built; needs a frontend rebuild / dev server).

---

### Grillo outreach silently stops firing when the `interface_paths` registry is empty (missed backfill after table (re)creation)  <!-- 2026-07-12 -->
**Symptom:** Grillo outreach (the folded-in `grillo_chat_observer` beat) stops attempting outreach entirely — Langfuse shows zero outreach attempts, and `synth.log` logs `[grillo_chat_observer] No fragments and no eligible targets; skipping` (grillo_chat_observer.py:503) on every hourly run. The scheduler itself is healthy (other beats fire on schedule).
**Location:** `plugins/grillo/grillo_chat_observer.py` (`_collect_recent_snippets`, `_collect_eligible_targets` — both source candidate chats ONLY from `core.interface_paths.get_recent_interface_paths`), `core/interface_paths.py` (`touch_interface_path`, `init_interface_paths_table`, `get_recent_interface_paths`).
**Status:** fixed 2026-07-12.
**Root cause:** The observer's candidate chats come exclusively from the `interface_paths` table via `get_recent_interface_paths()`. That table is populated ONLY by `touch_interface_path()` on live message flow (`core/chat_context_manager.py::add_message_to_context`). On this Postgres-first deployment the table was created *after* a window where touches were failing (`relation "interface_paths" does not exist`, seen in the 2026-07-10 logs), so it was created empty and **never backfilled** — `touch_interface_path` only writes on NEW inbound traffic, and no new non-self messages arrived for ~2 days. Empty table → `get_recent_interface_paths()` returns `[]` → both `fragments` and `eligible_targets` are empty → the guard at grillo_chat_observer.py:502 returns before outreach is ever attempted, every hour, indefinitely. `chat_history_cache` had 40+ active chats the whole time, but the outreach path never consults it directly — that coupling is the fragile point (registry out of sync with the chat cache = silent outreach death, no error).
**Fix:** Two layers in `core/interface_paths.py`. (1) `init_interface_paths_table()` now calls a new idempotent `backfill_interface_paths_from_history()` on startup, which seeds one row per distinct `chat_history_cache.interface_path` NOT already present, using that chat's `MAX(timestamp)` as `last_used` (float epoch). Backend-aware: Postgres `EXTRACT(EPOCH FROM MAX(timestamp))`, MariaDB `UNIX_TIMESTAMP(MAX(timestamp))`. Existing rows are never touched (`touch_interface_path`'s `last_used` stays authoritative); `segment_labels` left NULL (refreshed on next touch/resolve). (2) `get_recent_interface_paths()` now falls back to a new `_recent_paths_from_history()` (derives recent paths straight from `chat_history_cache`) whenever the registry query returns empty — so a missed backfill can never again silently starve the observer OR the other 5 consumers (`grillo_dream`, `grillo_impl`, `beat_utils`, telegram `/chats`, `command_registry`). A one-time run of `backfill_interface_paths_from_history()` seeded 28 rows into the live DB to unblock immediately.
**Notes:** Pre-existing test debt surfaced during validation (NOT caused by this fix, confirmed via `git stash` A/B on `develop`): `tests/test_grillo_observer.py::test_collect_recent_snippets_includes_sender_and_timestamp` and `::test_collect_recent_snippets_skips_recent_bot_messages` both FAIL on unmodified `develop`. They monkeypatch `core.recent_chats.get_last_active_chats_verbose` / `get_chat_path`, but `_collect_recent_snippets` actually calls `core.interface_paths.get_recent_interface_paths` — the mocks target functions the code no longer uses, so the tests only "pass" when the real function reaches a live DB (and fail with `WinError 1225` when the DB refuses the test's separate-event-loop connection). Fix direction if reactivated: monkeypatch `plugins.grillo.grillo_chat_observer.get_recent_interface_paths` (or the import site) instead of the retired `recent_chats` helpers. Left as-is to keep this change scoped.

---

### External Vox engines lose the WebUI voice selection — dynamic `<ENGINE>_VOICE` config key was never registered  <!-- 2026-07-10 -->
**Symptom:** With a Fish Audio (or any external) Vox endpoint active, the WebUI voice picker (`VoiceSettings.vue`) shows the fetched voices but clicking one only *previews* it ("(preview only)" label) — the choice is never persisted, so every turn uses a random/default voice. Built-in KittenTTS is unaffected.
**Location:** `frontend/src/components/settings/VoiceSettings.vue` (`voicePersistable` gate), `core/webui.py::config_summary` (`GET /api/config` exports only *registered* exposed-var definitions), `core/external_endpoints/registry.py::_sync_registries` (Vox registration), `core/external_endpoints/bridges/vox_bridge.py::_runtime_selected_voice`.
**Status:** fixed 2026-07-10.
**Notes:** The WebUI persists the chosen speaker code into config key `<ACTIVE_VOX>_VOICE` (e.g. `FISHAUDIO_VOICE`), and treats a voice as persistable only when that key is present in `/api/config`. `/api/config` returns only *registered* exposed-var definitions. KittenTTS registers `KITTEN_VOICE` statically at import (`plugins/vox_engines/kitten.py`), but external endpoints have a **dynamic** name (chosen by the user) so no static registration exists → the key never appears → `voicePersistable` stays `false` → the selection is never saved → the bridge's `_runtime_selected_voice()` (which reads `<ENGINE>_VOICE`) always finds nothing → Fish falls back to `extra_config`/random. Fix: `_sync_registries` now calls a new `_register_voice_config_key(engine_name, label)` when the endpoint's adapter exposes `list_speakers`, registering `<engine_name.upper()>_VOICE` as an exposed var (`ui_type="select"`, empty `options` — the visible list is populated separately by `/api/vox/speakers`, this only needs to exist so `/api/config` returns the key). Registration is idempotent (`exposed_vars.get_definition` guard + `ExposedVariableRegistry.register` ignores dupes). **Reminder:** media-subsystem registration is NOT hot-reloaded — enabling/repairing an external Vox endpoint on a running instance needs a `docker restart synth` before the key appears (AGENTS.md §12 "Media-subsystem registration is NOT hot-reloaded").

---

### Classic WebUI Vox voice picker + preview for external engines (Fish Audio) — two deadlocks + an empty speaker list  <!-- 2026-07-10 -->
**Symptom:** In the CLASSIC WebUI (`res/synth_webui/js/main.js` + `core/webui_templates/sections/engines.html`, NOT the Vue frontend), selecting `fish-audio` in the Vox engine dropdown never showed a voice picker or preview button like Kitten does. `GET /api/vox/speakers?engine=fish-audio` returned an empty list; the picker auto-hides on an empty list, so nothing appeared.
**Location:** `core/webui.py` (`vox_speakers` GET `/api/vox/speakers`, `vox_sample` GET `/api/vox/sample`), `core/external_endpoints/bridges/vox_bridge.py` (`ExternalVoxEngine.get_speakers`/`sample`), `core/external_endpoints/adapters/fish_audio_adapter.py` (`list_speakers`), `core/webui_templates/sections/engines.html` (`#vox-voice-select`, `#vox-voice-play-btn`), `res/synth_webui/js/main.js` (generic picker + `loadVoxVoices`/`updateVoxVoiceVisibility`).
**Status:** fixed 2026-07-10.
**Notes:** THREE stacked causes, all engine-agnostic in the fix. (1) **Event-loop deadlock:** the async webui handlers called the *synchronous* bridge methods `get_speakers()`/`sample()` (which drive an async adapter) directly on the event-loop thread → deadlock, logged `[vox_bridge:fish-audio] get_speakers failed: TimeoutError`. Fix: call them via `await asyncio.to_thread(engine.get_speakers)` / `asyncio.to_thread(engine.sample, speaker)` in `webui.py`. **Rule:** any sync bridge method that drives an async adapter, invoked from the loop thread, must go through `asyncio.to_thread`. (2) **Empty speaker list:** `fish_audio_adapter.list_speakers` requested `GET /model` with `params={"self":"true", ...}`, which returns ONLY the account's own cloned/custom voices — this account has none → 0 items → picker stays hidden. Direct probe confirmed `self=true`→200/n=0 while dropping it →200/n=5. Fix: `list_speakers` now tries `self=true` first (keeps the list small+relevant when the account has own voices), and if that yields zero it falls back to the popular public library (`{"sort_by":"task_count","page_size":"100"}`), parsing via a new `_fetch_model_page` helper. Result: 100 selectable voices. (3) **Picker was Kitten-only:** the classic WebUI hardcoded Kitten's controls. Added a *generic* `#vox-voice-select` + `#vox-voice-play-btn` shown for ANY engine whose `/api/vox/speakers` returns a non-empty list; selection persists to `<ENGINE.toUpperCase()>_VOICE` (for fish-audio that is literally `FISH-AUDIO_VOICE`, hyphen included) via `POST /api/config`, and the ▶ button plays `GET /api/vox/sample?engine=<e>&speaker=<code>`. Verified end-to-end in the browser: picker populates (100 voices), selection persists (`FISH-AUDIO_VOICE` saved + toast), preview returns HTTP 200 `audio/wav` (~365 KB). **Deploy reminder:** classic WebUI JS/HTML edits need `docker cp` to reach the container-served UI; adapter/webui `.py` edits need `docker cp` + `docker restart synth`. The classic-WebUI picker's visibility is driven by the DROPDOWN selection, not the *active* engine, so you can configure/preview a non-active external Vox engine's voice.

---

### Grillo observer "outreach" goes silent after one proactive DM — cooldown gates skip runs invisibly at INFO level  <!-- 2026-07-09 -->
**Symptom:** The observer beat (`grillo_chat_observer`, the folded-in "outreach") fires once, sends a proactive DM, then apparently never fires again — no ERROR, no log line at all at the hourly mark. The scheduler is actually fine: the hourly `_run_observer()` runs, but every skip path (`No fragments and no eligible targets`, `Decay-driven run but no eligible targets`) logs only at DEBUG, so with `LOGGING_LEVEL=INFO` the runs are invisible.
**Location:** `plugins/grillo/grillo_chat_observer.py` (`_run_observer`, `_collect_eligible_targets`).
**Status:** fixed 2026-07-09 — cooldown made minutes-granular + skip logs promoted to INFO.
**Notes:** After the synth sends an outreach it is the chat's `last_sender`, which (a) hides the chat's snippets for `GRILLO_OBSERVER_SELF_WINDOW` (12 h) and (b) puts the chat on self-cooldown. Historically the cooldown was `GRILLO_OBSERVER_SELF_COOLDOWN_DAYS` (default 3 days) only — one outreach muted the whole network for days. Now: when the days key is **0**, `GRILLO_OBSERVER_SELF_COOLDOWN_MINUTES` (default 45; keep it below `GRILLO_OBSERVER_INTERVAL` or every other run lands inside the cooldown) applies instead, and a new active-conversation guard skips any chat whose last **human** message is younger than `GRILLO_OUTREACH_QUIET_MINUTES` (default 15 — key existed in DB but was previously read by nothing). Days > 0 preserves the strict legacy behaviour. This instance runs days=0. The formerly-DEBUG skip lines ("no eligible targets") now log at INFO, so hourly runs are visible. Remaining gotchas: `recent_chats` is repopulated per-process (right after a restart only chats touched since startup are candidate targets); external `set_config` writes need a process restart (`config_registry` caches in-process, no DB poll); and the prompt still tells the LLM to reach out only with a genuine internal reason, so an hourly run does not guarantee an hourly DM.

### Iris Vision fails on large photos — Harmony `/v1/chat/completions` rejects bodies over ~1 MB, fallback 404 masks it  <!-- 2026-07-09 -->
**Symptom:** Iris Vision "goes completely into error" on real photo uploads via the `harmonyai` endpoint. Logs show `[openai_compat.py] describe_image failed: HTTP 404: 404 page not found` — misleading. Real failing image was a 933380-byte JPEG.
**Location:** `core/external_endpoints/adapters/openai_compat.py` (`describe_image`, `_shrink_image_for_request`, `_http_chat_urls`).
**Status:** fixed 2026-07-09 (deployed via `docker cp` + `docker restart synth`).
**Root cause:** Harmony's `POST /v1/chat/completions` returns **HTTP 400 "request body too large"** when the base64-encoded JSON body exceeds ~1 MB (1,048,576 bytes). A 933 KB JPEG → ~1.24 MB base64 body → 400. Confirmed by live probes: raw ~730 KB img → 200; ~1.46 MB+ → 400. The adapter then tried the fallback `/api/v1/chat/completions` URL (from `_http_chat_urls()`), which always 404s, and that 404 **overwrote** the meaningful 400 in `last_error` — so the log only showed the 404. This is DETERMINISTIC for large photos, not transient (tiny 1x1 PNG probes returned 200 and misled earlier diagnosis).
**Fix:** (1) `_shrink_image_for_request()` (new classmethod) downscales/recompresses any image over a raw budget of `_VISION_MAX_RAW_IMAGE_BYTES = 700_000` before base64-encoding — iterates longest-side sizes (1568→512) × JPEG quality (85→55) with Pillow LANCZOS, returns the first candidate under budget. `describe_image` calls it before building the data URL. Verified live: 761 KB img → 380 KB → Harmony returned a full description. (2) The URL loop now collects per-URL errors into a list and joins them, so a trailing fallback 404 no longer hides the real first-URL error. Pillow (`pillow>=10.0.0`) added to `pyproject.toml` dependencies (was installed transitively, now explicit). Applies to ALL OpenAI-compatible vision endpoints, not just Harmony. Note: modern Pillow moved `LANCZOS` to `Image.Resampling.LANCZOS`; the helper resolves it via `getattr(getattr(Image,"Resampling",Image),"LANCZOS",None)` for compatibility. **Distinct** from the 2026-07-08 `iris_model` entry (that fixed a wrong non-vision model; this fixes payload size).

### Inline comments in `.env` poison values when launching from the IDE — and `.env` itself can't fix it  <!-- 2026-07-09 -->
**Symptom:** A numeric env var silently misbehaves even though `.env` looks correct and `dotenv_values()` parses it fine. Observed as: `SYNTH_WEBUI_HTTPS_PORT=8088   # comment` → the running process's env literally contains `'8088     # comment'` → `int()` fails in `core/webui.py` → silent fallback served HTTPS on the HTTP port and nothing on 8088 (an evening of "empty response"/"connection refused" debugging).
**Location:** Any `.env` value with an inline `#` comment. Root cause is two-layer: (1) VS Code/Antigravity's Python integration injects the workspace `.env` into terminals/processes with a parser that does **not** strip inline comments; (2) `core/logging_utils.py`/`core/config.py` call `load_dotenv(override=False)`, so the properly-parsed value never overrides the poisoned one already in the environment.
**Status:** fixed for the webui config path (2026-07-09): `core/webui.py::_clean_env` strips inline `#` suffixes from host/TLS/port vars and logs a WARNING naming the variable; unparsable ports also warn instead of failing silently. Other subsystems reading env vars directly remain exposed.
**Notes:** Debug this class of problem by reading the *live process* env, not the file: `uv run --with psutil python -c "import psutil; print(psutil.Process(<pid>).environ())"`. Rule of thumb for this repo: comments in `.env` go on their own lines, always.

### `blocklist` rejects webui users: UUID session ids bound against an integer `user_id` column  <!-- 2026-07-08 -->
**Symptom:** Every webui-originated message logs `[blocklist] Failed to check if user <uuid> is blocked: invalid input for query argument $1: '<uuid>' ('str' object cannot be interpreted as an integer)`. Fail-open (`is_user_blocked` returns False on error), so nothing user-visible breaks — but it's one ERROR log line per webui message, and webui users can never actually be blocked.
**Location:** `plugins/blocklist.py::is_user_blocked` (typed `user_id: int`), callers pass webui session UUIDs (strings); `blocklist.user_id` is an integer column sized for Telegram ids.
**Status:** known, not fixed — diagnosis only (2026-07-08).
**Notes:** Fix direction: either widen the column + type to string (user ids are interface-scoped strings elsewhere in the codebase), or skip the blocklist check for non-numeric ids. Watch for the same assumption in anything else keyed on Telegram-style numeric user ids.

### Windows host runs spam `--- Logging error ---` / `UnicodeEncodeError: 'charmap' codec` for any log line with non-ASCII  <!-- 2026-07-08 -->
**Symptom:** When SyntH runs directly on the Windows host (e.g. `scripts/run_webui.py` from a terminal), every log message containing `✓`, emoji, etc. produces a multi-line `--- Logging error ---` traceback (`cp1252.py ... charmap_encode`) on the console handler; the file handlers are fine. Also surfaces as `[QUEUE] Error adding reaction: 'charmap' codec can't encode character '\U0001f440'` in `synth.log`.
**Location:** `core/logging_utils.py` console `StreamHandler` (inherits the terminal's cp1252 encoding); the Linux container is unaffected (UTF-8).
**Status:** known, not fixed — cosmetic, host-only.
**Notes:** If it ever needs fixing: set `PYTHONIOENCODING=utf-8` for host runs (or wire the console handler with `errors="replace"`). Don't strip the emoji from log messages — they're load-bearing grep anchors in several debug flows.

### Frontend builds from IDE agent shells silently bake the theme hue (chromatic preset env sniffing)  <!-- 2026-07-08 -->
**Symptom:** The `/stage` theme-hue slider does nothing: `--chromatic-hue` updates on `<html>` but every `primary-*` color keeps the default hue. The built CSS contains literal `oklch(... 220.44 ...)` values instead of `var(--chromatic-hue)` references. `pnpm build` output looks identical otherwise — screenshots pass casual review.
**Location:** `@proj-airi/unocss-preset-chromatic` `dist/index.node.mjs` (bakes colors when `VSCODE_ESM_ENTRYPOINT` contains `"extensionHostProcess"`, a heuristic for the UnoCSS VSCode extension); guard: `frontend/chromatic-env-guard.ts`, imported first in `frontend/uno.config.ts`.
**Status:** workaround in place (2026-07-08) — the guard strips the env var before the preset module is evaluated, so var-based colors are always emitted.
**Notes:** VSCode/Antigravity extension-host shells (i.e. every in-IDE Claude agent session) export `VSCODE_ESM_ENTRYPOINT=vs/workbench/api/node/extensionHostProcess`, which the preset misreads as "I'm the IDE preview". Any Node tool that changes behavior on VSCode env vars can misfire the same way in agent shells. If the guard import is ever removed from `uno.config.ts`, hue theming breaks again with zero build errors. Verify a build is var-based with: `grep -c "var(--chromatic-hue)" frontend/dist/assets/*.css` (must be ≥1).

### VueUse `useLocalStorage` skips JSON encoding for string defaults — seeding a value from outside the app must NOT `JSON.stringify` it  <!-- 2026-07-09 -->
**Symptom:** A Playwright (or manual) script does `localStorage.setItem(key, JSON.stringify(value))` to pre-seed a `useLocalStorage(key, '')`-backed store field before page load, then the app reads back the literal string `"value"` (quotes included) instead of `value`. Looked exactly like the app-side wiring was broken (e.g. an auth token silently "not matching") when the real bug was in the test setup.
**Location:** Any `useLocalStorage('...', '')` field, e.g. `frontend/src/stores/settings.ts::apiToken`. Root cause: VueUse's `useStorage`/`useLocalStorage` guesses the serializer from the *initial value's type* — a `''` default guesses the `'string'` serializer, which stores/reads the raw string with no `JSON.stringify`/`JSON.parse` round-trip (unlike object/number/boolean defaults, which do get JSON-encoded).
**Status:** not a code bug — documenting so it isn't rediscovered. `frontend/scripts/auth-ux-ui-check.mjs` seeds the token by driving the real Settings-drawer `<input>` instead of touching `localStorage` directly, which sidesteps this entirely and is the safer pattern for future smoke scripts.
**Notes:** If you must seed a `useLocalStorage` value directly from a script, check the field's *default value's type* in the store first — string defaults want the raw string in `localStorage`, non-string defaults want `JSON.stringify(value)`.

### Aborting a playback-manager item races its cleanup against the next item's synchronous start — shared state gets clobbered  <!-- 2026-07-09 -->
**Symptom:** Interrupting one `tts-play` clip with another (e.g. a new conversational turn arriving while the previous one is still queued/playing) made `useAudioStore().speaking` immediately flip back to `false` right after the new clip started, even though audio was still actively playing. No console error — the bug is silent. Caught by a Playwright script that measured `speaking`'s timeline with fine-grained polling around an interrupt; a fixed `setTimeout` check 80ms after the interrupt looked like "new item never started" when the real problem was "it started, then got immediately un-started."
**Location:** `frontend/src/stores/audio.ts::playItem`. Root cause: `AbortController.abort()` dispatches its `abort` event **synchronously**, but the awaiting `playItem`'s `onAbort` handler only *resolves* a pending Promise — the code after that `await` (the `finally` block, which wrote `lipsync = null; speaking.value = false` to store-level shared variables) only runs as a **microtask**. Meanwhile `lib/pipelines-audio/playback-manager.ts`'s `stopActive()` synchronously removes the aborted item from `active` before returning, so a caller that calls `stopAll()` immediately followed by `schedule()` in the same synchronous tick (exactly what `scheduleTts`'s turn-interrupt logic does) causes the **new** item's `playItem` to run its synchronous prefix (through `speaking.value = true`) *before* the **old**, aborted item's deferred `finally` block runs — so the old item's cleanup executes last and clobbers the new item's state.
**Status:** fixed (2026-07-09). `playItem` now tracks `activeItemId` (the id of whichever item last legitimately claimed `speaking`/`lipsync`) and each call's `finally` block only writes `speaking.value = false` / `lipsync = null` if `activeItemId` still equals its own item's id — a superseded item's belated cleanup becomes a no-op for shared state (it still tears down its own `AudioNode`/lipsync-driver resources unconditionally, just doesn't touch the store's "who's currently speaking" state). Also fixed as a side effect: the lipsync driver was a single shared variable reused across items with no per-item scoping (`const itemLipsync = new AnalyserLipSyncDriver()`, a local now, instead of reassigning the shared `lipsync` directly) — the old code would `.detach()` whichever driver happened to be in the shared slot at cleanup time, which after the race could be the *new* item's driver.
**Notes:** This exact race pre-dates sentence-chunked TTS streaming (`_speak_chunked` in `plugins/vox_plugin.py`) and the turn-grouping logic (`turn_id` in `protocol.ts`/`scheduleTts`) — it was already latent in the original `overflowPolicy: 'steal-oldest'` design, since `handleOverflow`'s steal path does the identical abort-then-immediately-start sequence. It just never got exercised by a test until sentence chunking made "new turn interrupts an in-flight one" an explicit, testable code path. If you add another `PlaybackManager`-backed store, check whether its `play()` callback writes to store-level shared state in a `finally` block — if so, it needs the same ownership guard.

### Mic on `/stage`/webui requires a secure context — plain-http LAN access has no `navigator.mediaDevices`  <!-- 2026-07-08 -->
**Symptom:** Mic button fails with `Cannot read properties of undefined (reading 'getUserMedia')` when the stage is opened via `http://<lan-ip>:<port>/stage/`. Works on `http://127.0.0.1`/`localhost` (browsers treat loopback as secure) and on any `https://` origin.
**Location:** Browser security model, not our code. Stage-side guard with a clear message: `frontend/src/stores/mic.ts::start()`. The legacy webui (`res/synth_webui/js/chat-window.mjs`) has no such guard and fails the same way.
**Status:** workaround in place (stage shows "Microphone needs a secure context — open the stage over HTTPS or via localhost"). Real fix for LAN use: serve over TLS (`SYNTH_WEBUI_TLS=1`).
**Notes:** Reproduced 2026-07-08 with Playwright against `http://192.168.1.69:8088/stage/` (`isSecureContext:false`, `mediaDevices:false`).

### Conversation-history turns carry no timestamps — staleness is invisible to the model, and outreach's "long silence" rule is un-satisfiable  <!-- 2026-07-05 -->
**Symptom:** Grillo outreach (and any beat/turn) grounds confidently in a day-old conversation thread as if it were live. Verified via langfuse trace `e5555717-275f-46b7-af4f-981588265da5` (2026-07-05 03:07Z, first group-targeted outreach): the `[Recent context from other conversations]` block had correct per-line timestamps (`[05/07/26:0132] ...`, all DM — the cross-chat `UNIFIED_HISTORY` merge works as designed), but the turn-by-turn `conversation_history` (from `history_current_chat`) was raw untimestamped text of a 24h-stale group exchange. The outreach template says "never imply they've been distant unless the conversation history itself actually shows a long silence" — which it never can, since turns have no time markers. Compounded by the observer beat posting into the group 60s earlier (see `propose_only` note in the outreach self-poisoning entry), which made the stale thread's last assistant turn look brand new.
**Location:** `core/history_engine.py` (`history_current_chat` lines keep timestamps only in the `history_recent` formatting path), `core/prompt_engine.py::_history_to_turns` (turns built without time markers), `plugins/grillo/grillo_outreach.py::_build_outreach_prompt` (the un-satisfiable ground rule).
**Status:** known, not fixed — diagnosis only (2026-07-05). The worst symptom (outreach anchoring on the stale group thread) is already mitigated by the 2026-07-05 targeting fix, since `history_current_chat` now follows the last real user interaction's chat.
**Notes:** Fix direction: add a relative-time marker to conversation turns (e.g. prefix turns older than N minutes with "[x hours earlier]", or annotate the last turn's age) so temporal distance is model-visible. Keep it lightweight — per-message absolute timestamps on every turn would fight the RUNTIME STYLE rule about not mirroring exact times.
### Grillo outreach self-poisons its target: one autonomous group post permanently redirects hourly outreach to the group  <!-- 2026-07-05 -->
**Symptom:** `grillo_outreach` beats stop firing in the DM of the last real user interaction and fire in a group instead, every hour, until the user happens to message the DM again. Every beat logs `Recovered target from chat_history_cache: telegram_bot/<id>` (grillo_outreach.py:408) — the fallback path, never the primary one.
**Location:** `plugins/grillo/grillo_outreach.py::_get_target_interface_and_chat` (Fallback A), `core/recent_chats.py::set_chat_path`, `plugins/grillo/grillo_chat_observer.py`, `core/chat_context_manager.py::save_response_message`.
**Status:** causes (1) and (2) fixed on develop 2026-07-05 (Fallback A now excludes `sender_name IN ('self','grillo')`; `add_message_to_context` now calls `set_chat_path`, and `core/chat_paths.json` is gitignored). Cause (3) — observer `propose_only` proposals executed as real sends — is still open.
**Notes:** Three stacked causes. (1) The primary target path (`recent_chats.get_last_active_chats` → `get_chat_path`) is dead code in practice: `set_chat_path` has **zero callers**, so `chat_paths.json` never gets written and `get_chat_path` always returns None — even though the `recent_chats` table itself correctly has the DM as most recent. (2) Fallback A takes the newest `chat_history_cache` row for the interface **regardless of sender**, so the bot's own `sender='self'` rows count as "recent activity". (3) The `grillo_chat_observer` beat (runs at :06) can autonomously send a `message_telegram_bot` into a group (observed replying to a day-old group thread from its cross-chat snippets, despite its activity being logged `propose_only=True`); that sent message is saved under the group path by `save_response_message`, becomes the newest cache row, and the next outreach beat (:07) targets the group — whose own outreach message re-poisons the cache, locking outreach onto the group indefinitely. Fix direction: exclude `sender_name='self'` (and grillo-origin rows) in Fallback A, and/or wire `set_chat_path` back up. The observer `propose_only` question was audited 2026-07-05: `GRILLO_OBSERVER_PROPOSE_ONLY` is prompt-only — it appends one "proposals only" sentence to the observer prompt and stores `propose_only` in the beat context, which **nothing in core/ reads**; observer responses run through the normal message_chain/action_parser pipeline and message actions are executed as real sends. The trainer runs the observer intentionally autonomous, so this is accepted behaviour, not a bug to fix — but the config flag is misleading (it does not gate execution). If enforcement is ever wanted: intercept `message_*` actions when context has `propose_only=True` and record them in `grillo_action_execs` (status 'pending') instead of dispatching. **Debugging trap that hid this:** `synth.log` timestamps are container-local time (UTC+2 in July), `chat_history_cache.timestamp` is UTC — a log send at 05:06 local *is* the cache row stamped 03:06 UTC; align timezones before concluding rows and sends don't match.

### `test_chat_attention_triggers.py` start_bot tests fail: FakeBuilder missing `get_updates_connection_pool_size`  <!-- 2026-07-03 -->
**Symptom:** `test_start_bot_failure_resets_state_and_schedules_retry` and `test_start_bot_retries_transient_timeout_inline` fail with `AttributeError("'FakeBuilder' object has no attribute 'get_updates_connection_pool_size'")` — the interface's `disabled_reason` becomes `Startup failed: AttributeError(...)` instead of the expected timeout/retry text.
**Location:** `tests/test_chat_attention_triggers.py` (`FakeBuilder` stub), `interface/telegram_bot.py` (`start_bot` builder chain).
**Status:** known, not fixed — pre-existing, found (and verified unrelated) during the 2026-07-03 upstream merge: the merge touched neither file; the builder call was added by commit `83415cef` ("widen get_updates connection pool") without updating the test stub.
**Notes:** Mechanical fix: give `FakeBuilder` a `get_updates_connection_pool_size(...)` chainable passthrough like its other builder methods. Left undone to keep the upstream merge free of unrelated changes.

### `cortex_api.log` never logs the actual system prompt content — can't verify context injection from logs alone  <!-- 2026-07-01 -->
**Symptom:** Every request entry in `cortex_api.log` (and the `cortex_read`/`cortex_analyze` MCP tools built on it) shows the system message as a placeholder, e.g. `"content": "<string: 16146 chars>"` — the real text is never written to the log, only its length. `cortex_search`'s payload/response text search therefore also cannot match anything inside the system prompt (it only sees the placeholder).
**Location:** whatever call in `core/cortex_api_logger.py` serializes the outbound request before writing it (the truncation happens before the write, not in a display layer — confirmed by reading the raw log file directly, not just the MCP tool output).
**Status:** known, not investigated further — found incidentally while trying to verify whether `UNIFIED_HISTORY` cross-chat merging (`core/history_engine.py`) actually injected another interface_path's history into a given session's system prompt (`core/prompt_engine.py::_build_context_summary`, `"[Recent context from other conversations]"` block). Could not confirm either way from the trace/log alone.
**Notes:** If you need to debug what's actually inside a system prompt (history_recent injection, peer instruction block, persona content, etc.), don't rely on `cortex_read`/`cortex_search`/raw log grep for the system message — it's opaque. Either add temporary debug logging in `core/prompt_engine.py::_build_context_summary` / `build_prompt_request`, or inspect `history_engine.build_context()`'s return value directly. The history-merging code itself (`load_global_chat_history`, `unified_candidates` split into `local_lines`/`other_lines` in `history_engine.py`) looked correctly wired on read-through: it queries the synth's own `chat_history_cache` table across all interface_paths with no cross-chat filtering, so DM and group history for the *same* synth should merge by default (`UNIFIED_HISTORY` defaults to `1`/on). Two separate SyntH instances each have their own DB/table, so this only merges within one instance; cross-instance awareness depends on Telegram actually delivering a peer's messages here at all — see the entry below.

### Telegram bots can't see each other's messages until "Bot-to-Bot Communication Mode" is enabled in BotFather  <!-- 2026-07-01, corrected 2026-07-01 -->
**Symptom:** In a two-SyntH Telegram group (SynthA + SynthB), one instance's own `chat_history_cache` had **zero rows ever** with `sender_id` equal to the other instance's bot ID — not just in one conversation window, across the entire table's history — despite the peer bot visibly replying in the group and `SYNTH_PEERS` being configured correctly. `synth.log` had zero mentions of that bot ID anywhere either, meaning the update never reached the application layer at all. Symptoms this caused: the receiving instance's LLM prompt never contained the peer's replies, and any wait/poll logic checking for a peer response (turn floor, mention-order relay) always burned its full timeout before giving up, since the awaited row could never appear.
**Location:** platform-level (Telegram Bot API itself), not a bug in this repo. `interface/telegram_bot.py::handle_message` has no `is_bot` filter anywhere, and `core/chat_context_manager.py::add_message_to_context` is called unconditionally near the top of `handle_message` before any mention/peer-policy logic runs — so once an update from the peer bot actually arrives, it's saved like any other message. The fix is entirely a Telegram-side setting, not a code change.
**Status:** root cause identified and documented (2026-07-01). **Correction (same day):** an earlier version of this entry claimed this needed a custom HTTP relay between instances (`core/peer_relay.py` + a WebUI endpoint) — that was reverted as unnecessary over-engineering after checking https://core.telegram.org/bots/features#bot-to-bot-communication. Telegram has a native opt-in for exactly this: enable **Bot-to-Bot Communication Mode** for each bot via BotFather, AND make sure each bot has Group Privacy Mode disabled (or admin rights) in the shared group (the communication mode alone only unlocks explicit `/command@OtherBot` mentions and direct replies; full plain-message visibility needs privacy-disabled-or-admin on top). See `docs/peer_synths.rst` "Bot-to-Bot Communication Mode". No code in this repo needed to change for delivery itself — `peer_already_responded`'s column-name fix and the mention-order relay (`get_relay_wait_peer`/`wait_for_peer_reply` in `core/peer_policy.py`) from the same debugging session are still valid and still needed; only the "how does the peer's message physically arrive" part was wrong.
**Notes:** If a future agent is asked to debug "peer SyntH doesn't see the other's replies" again: first check whether Bot-to-Bot Communication Mode is enabled in BotFather for both bots, and whether each has Group Privacy Mode disabled or admin rights — don't assume a code fix is needed, and don't reach for a custom relay/webhook without checking this Telegram-native setting first. Group privacy mode being disabled (confirmed via *human* messages arriving fine in the same chat) does **not** by itself extend to other bots' messages — the separate Bot-to-Bot Communication Mode toggle is required in addition.

### Order-dependent test failures in full pytest runs (`config_registry` / global-state pollution)  <!-- 2026-06-11, updated 2026-07-01 -->
**Symptom:** A full `uv run pytest` run fails a small, shifting set of tests that all pass when their file is run in isolation. Recurring offenders: `test_vox_defaults.py::test_active_vox_engine_default_is_kitten` and `test_vox_plugin.py::test_active_vox_engine_default_is_kitten` (e.g. `assert 'disabled' == 'kitten'`), plus a third slot that varies by run/ordering — observed as `test_db_cutover::test_cutover_runs_backup_and_migration`, `test_grillo_prevent_duplicates::test_grillo_suppresses_when_last_is_synth`, `test_grillo_beat_system::test_grillo_beat_types_exist`, `test_exposed_variables_static::test_no_direct_getenv_for_exposed_vars`, and `test_exposed_variables_style::test_exposed_variable_label_and_description_style` at various times.
**Location:** `core/config_manager.py` (`config_registry` is a process-wide singleton) and the exposed-variable registry; global state leaking between test modules.
**Status:** known, pre-existing — confirmed via `git stash` A/B (on commits `e4558376` and `afdaea0`) that the vox pair fails identically on unmodified `develop`, so it is not caused by any specific feature change.
**Notes:** Whichever test runs first in the full ordering leaks the *real* DB-loaded value (`ACTIVE_VOX_ENGINE = "disabled"`, per the live config table) into `config_registry._definitions`, and it's never reset before the default-expecting test runs. When triaging a full-suite run, re-run the failing file alone (and/or `git stash` A/B) before assuming a regression. Related leaks: local `.env` values (e.g. `SYNTH_PRIMARY_DB=soul`) bleed into tests that don't pin them — `tests/test_db_preflight.py` monkeypatches `core.db._get_db_type` for this reason — and `tests/test_message_queue.py` `test_enqueue_*` tests hit the live DB configured in `.env` and time out when it is slow/unreachable (verified unrelated to code changes via stash A/B); `tests/test_exposed_variables_audit.py::test_exposed_variables_have_label_description_and_component` fails the same way when the DB host doesn't resolve (`getaddrinfo failed`, re-verified 2026-07-04 via stash A/B). Real fix would be a per-test reset/fixture for `config_registry` (or at least the affected definitions) instead of process-wide state; out of scope so far.

---

### Unit tests can write real rows into the live `ai_diary` table  <!-- 2026-07-01 -->
**Symptom:** Grillo diary-consolidation prompts contain literal test fixtures interleaved with real diary content — e.g. langfuse trace `312a3ac5-03c2-40e6-bf91-25ce25e127b9` (2026-07-01 diary_consolidation beat) shows "Ciao Xargon!", "Hello", "Performed multiple actions: 1 use_animation, 1 create_personal_diary_entry", "Performed message_telegram_bot action" — each duplicated (two test runs same day) — mixed into the day's real fragments.
**Location:** `tests/test_message_chain.py:97` (hardcoded LLM response `"Ciao Xargon!"`), `tests/test_chat_attention_triggers.py` / `tests/test_telegram_sleep_bypass.py` (fixture username `Xargon`) all exercise `core/message_chain.py::handle_incoming_message` → `core/action_parser.py::_create_diary_entry_for_actions` (auto-diary hook, runs unconditionally after every processed action list, line 1974) → `plugins/ai_diary.py::create_personal_diary_entry` → `_upsert_diary_impl` (line 662). `tests/conftest.py` has no DB isolation fixture — only an animation-handler singleton reset and a temp backups dir.
**Status:** known, not fixed.
**Notes:** Tests mock `run_action`/`run_corrector_middleware` but never mock `plugins.ai_diary` or `core.db.get_conn_ctx`, and `add_diary_entry` lazy-inits/auto-creates the `ai_diary` table if missing (plugins/ai_diary.py:836-877) — so any test run against a reachable MariaDB (no dedicated test DB/config found) contaminates the live diary. Confirmed via DB: `ai_diary` id 4322 (2026-07-01) is already a single consolidated row and `ai_diary_archive` has zero rows for that date — consistent with `_upsert_diary_impl`'s same-day upsert-with-`---`-append design (one row per day, not separate fragment rows), so the test noise was appended straight into *today's* live content blob rather than creating archivable rows. The Grillo consolidator LLM happened to filter all the garbage out of the merged prose this time (verified the persisted `update_diary_entry` output has no test artifacts), so no permanent contamination landed — but that's not guaranteed, and it burns ~800 extra prompt tokens per beat. This is a **different, still-open** issue from `FIXED_ISSUES.md`'s "Automatic diary logging could create internal `diary_consolidation` noise rows" (2026-05-04 fix only skips the consolidation beat's own self-generated noise via `context.get("beat_type") == "diary_consolidation"`, `core/action_parser.py:1986-1993` — it does not address tests hitting the real DB). Real fix would be an autouse fixture in `tests/conftest.py` mocking `plugins.ai_diary.create_personal_diary_entry`/`add_diary_entry` (or pointing tests at an isolated DB).

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
**Status:** fixed (2026-07-04).
**Notes:** The migrated Postgres schema uses a boolean for `delivered`, but the query compared it to integer `0`. Fixed: `get_due_events` / `get_due_events_by_created_by` now query `WHERE delivered = FALSE AND next_run <= %s`, and `mark_event_delivered`'s one-time branch sets `delivered = TRUE` on Postgres (`delivered = 1` on MySQL). See the daily-weather-spam entry below for the related datetime bug.

---

### Daily weather report spam — `mark_event_delivered` passes a string to a Postgres `timestamp` column  <!-- 2026-07-04 -->
**Symptom:** The daily weather report was delivered repeatedly (~every 15s). Logs showed the plugin claiming success (`Delivered weather event 121; rescheduled for next day`) but NO `[db] Event 121 rescheduled to ...`, plus repeated `[mark_event_delivered] Error: invalid input for query argument $1: '2026-07-05 00:50:00' (expected a datetime.date or datetime.datetime instance, got 'str')`.
**Location:** `core/db.py` `mark_event_delivered` (recurring `daily`/`weekly`/`monthly` reschedule branch); amplified by two independent dispatchers (generic `EventPlugin` scheduler ~30s + `plugins/weather_plugin.py` `_weather_loop` 60s).
**Status:** fixed (2026-07-04).
**Notes:** The recurring branch computed the next run as a `strftime` string and passed it to the `next_run` `timestamp` column. The asyncpg driver rejects string literals for timestamp columns, so the UPDATE failed silently inside the try/except → `next_run` stayed today + `delivered` stayed FALSE → both dispatchers kept re-delivering the same event forever. Fix: pass a real `datetime` (UTC) on Postgres, keep the `strftime` string only on MySQL — same backend-aware pattern already used in `insert_scheduled_event`. **Lesson:** every write to `scheduled_events.next_run` (timestamp) must pass a `datetime`/`date` object on Postgres; every filter on `.delivered` (boolean) must use `TRUE`/`FALSE`. Two dispatchers sharing one event means any unmarked event turns into rapid spam.

---

### Weather report delivered twice + on the wrong day — two dispatchers both deliver weather events  <!-- 2026-07-05 -->
**Symptom:** After the 2026-07-04 spam fix (which stopped the every-15s loop), the daily weather report still arrived **twice** in quick succession, delivered to `telegram_bot` instead of the configured `synth_webui` interface, and the **next day's report was silently skipped**. Logs showed the same event rescheduled twice ~14s apart: `Event 134 rescheduled to 2026-07-06` (generic `EventPlugin` scheduler) then `Event 134 rescheduled to 2026-07-07` (weather plugin) + a `Delivered weather event 134`.
**Location:** `plugins/event_plugin.py` `_check_and_execute_events` (the generic scheduler); `plugins/weather_plugin.py` `_dispatch_due_weather_events`.
**Status:** fixed (2026-07-05).
**Notes:** Root cause is the residual double-dispatch the 2026-07-04 note warned about. `EventPlugin._check_and_execute_events()` called `get_due_events()` (ALL due events, no owner filter) and delivered weather events via `_deliver_event_to_llm`, which **hard-codes the `telegram_bot` interface** — while `weather_plugin._dispatch_due_weather_events()` ALSO fetched and delivered the same event via `get_due_events_by_created_by("weather_plugin")` on its configured interface. Both marked it delivered and rescheduled it, so the event jumped forward TWO days (today's second delivery advanced `next_run` past tomorrow). Fix: `event_plugin.py` now defines `_SELF_MANAGED_EVENT_OWNERS = frozenset({"weather_plugin"})` and `_check_and_execute_events` filters out any due event whose structured `created_by` field is in that set BEFORE dispatching — so only the owning plugin delivers its own events. Filtering is on the `created_by` column (available because `get_due_events` does `SELECT *` → `dict(row)`), **not** on message text, respecting the no-keyword-matching rule. To register a new self-dispatching plugin, add its `created_by` value to `_SELF_MANAGED_EVENT_OWNERS`.

---

### Weather report "wrong time" is a timezone/config issue, not a bug  <!-- 2026-07-05 -->
**Symptom:** User expected the report at 06:50 but it arrived ~09:48. Config `WEATHER_DAILY_REPORT_TIME` was `09:50` and the `synth` container runs with `TZ=Asia/Tokyo` (JST).
**Location:** config registry key `WEATHER_DAILY_REPORT_TIME`; `core/time_zone_utils.py` `get_local_timezone`/`utc_to_local` (resolves the local tz from the `TZ` env / timezone config); `core/db.py` `get_due_events` `advance_minutes=3` look-ahead.
**Status:** working as designed — no code change.
**Notes:** `WEATHER_DAILY_REPORT_TIME` is interpreted in the **project/local timezone** (`TZ`, here `Asia/Tokyo`), so `09:50` means 09:50 JST (= 02:50 CEST), not 06:50. The report also fires ~2-3 min early because `get_due_events` uses a `advance_minutes=3` look-ahead window to absorb LLM latency (09:50 − 3 min = 09:47/09:48). To change the delivery time, set `WEATHER_DAILY_REPORT_TIME` to the desired **local (JST) time**, or change `TZ`/the timezone config to the user's own timezone and set the value accordingly. Do NOT try to "fix" the conversion in code — it correctly follows the configured `TZ`.

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

### `vrm-viewer.mjs` fails standalone `node --check` near idle-finish handler  <!-- 2026-05-12 -->
**Symptom:** Running `node --check res/synth_webui/js/vrm-viewer.mjs` reports `SyntaxError: Unexpected token '{'` at `stopAction(actionName)`, after source text around `Finished idle animation -> starting next:` appears structurally corrupted.
**Location:** `res/synth_webui/js/vrm-viewer.mjs`, idle-finish / `stopAction` boundary around the `Finished idle animation -> starting next` block.
>>>>>>> b35813e2 (fix(grillo): backfill interface_paths registry so outreach never silently dies)
**Status:** fixed.
**Notes:** Two gaps in the Agent Lane. (1) `_build_agent_prompt` never surfaced the originating `interface_path` in the prompt text, so the model had no value to put in `message_telegram_bot`'s required `interface_path` field → validation rejected the action. Fixed by adding a "SOURCE CONVERSATION" block to the prompt that states the exact `interface_path` (and interface) and instructs delivery/message actions to reuse it verbatim. (2) The router (`core/agent_router.py`) only sets `context["interface_path"]`, never `context["interface"]`; internal tools run by the executor read `context["interface"]`, so the diary saved as "unknown". Fixed by deriving `interface` from `interface_path` once at the top of `run_agentic_turn` (via `core.interface_path_utils.get_interface_from_path`) and enriching the shared `context` — this covers both the prompt text and every executed tool (Drones inherit it, since `run_drone` delegates to `run_agentic_turn`).

### Bare `timestamp` column breaks fresh Postgres installs  <!-- 2025-01-01 -->
**Symptom:** On a fresh PostgreSQL install, SyntH comes up in a broken state — avatar stuck in T-pose, unable to do anything. Root cause: a bare `timestamp` column is a PostgreSQL reserved word; the ORM auto-translates it to `timestamptz`, producing an invalid schema.
**Location:** Any DDL using a bare `timestamp` column (`init-db.sql`, `scripts/sql/*.sql`, inline plugin DDL, `core/migrations.py`). Historically affected `chat_history_cache`, `ai_diary`, `ai_diary_archive`, `memories`, `emotion_state`, `emotion_diary`, `message_map`, `radio_activity_log`, and `mem_cells`.
**Status:** fixed (renamed to `created_at` / `event_timestamp`; startup auto-migration added in `core/migrations.py::_rename_timestamp_columns`).
**Notes:** A startup migration (`_rename_timestamp_columns`, registered in `_STARTUP_MIGRATIONS`) renames any lingering `timestamp` columns to `created_at` (and `mem_cells.timestamp` → `event_timestamp`) and renames stale indexes (`idx_timestamp` → `idx_created_at`, etc.) on both Postgres and MariaDB. See the Hard Rules entry: never use `timestamp` as a bare DB column name. Public API dict keys named `"timestamp"` returned to the WebUI/JS are intentionally kept — only DB-column SQL references are forbidden.

> Resolved issues (Status: fixed) and general changelog have been moved to [`CHANGELOG.md`](FIXED_ISSUES.md).

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
| `agent_tasks` | `init-db.sql` | Agentic Runtime 2.0 task records with I/O JSON (`engine`, `status`, `input`, `output`, `iterations_meta`) |
| `vessel_sessions` | `core/db.py` / `init-db.sql` | Rift Vessel embodiment sessions (`environment`, `status`, `experience_buffer` JSON, `started_at`/`last_event_at`/`ended_at`, `diary_entry_id`) |
| `vessel_activity_log` | `core/db.py` / `init-db.sql` | Rift Vessel Activities-tab log (`session_id`, `environment`, `event_type`, `summary`, `metadata` JSON, `created_at`) |
| `external_endpoints` | `init-db.sql` | LLM/API endpoint registry (name, protocol, URL, key, capabilities, model list) |
| `scheduled_events` | `plugins/event_plugin.py` | Date/time triggered events Synth should act on |
| `blocklist` | `plugins/blocklist.py` | Blocked users/entities |
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
| `DRONE_MAX_ITERATIONS` | Hard cap on Drone sub-agent loop iterations (default 3) |
| `DRONE_TURN_TIMEOUT_SEC` | Wall-clock budget per Drone turn in seconds (default 90) |
| `AGENT_SHELL_ALLOW_HOST` | Allow `agent_run_shell` to run when NOT in a container (default `False`; a host shell is a real compromise risk) |
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
