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
              └──────────┘  │Gemini …│   │ OpenAI API │
                            └────────┘   └────────────┘
```

| Layer | Location | Purpose |
|-------|----------|---------|
| **Core** | `core/` | Message chain, validation, dispatcher, DB, notifier. Never hardcodes plugin/LLM/interface logic. |
| **Plugins** | `plugins/` | Provide actions via `get_supported_actions()`. Subclass `PluginBase` or `AIPluginBase`. |
| **LLM Engines** | `engines/` | Interchangeable reasoning backends (`external_engines/`, `live/`, `agent/`). Subclass `AIPluginBase`. |
| **Interfaces** | `interface/` | I/O adapters (Telegram, Discord, Matrix, OpenAI-compatible API). Register actions via `get_supported_actions()`. |

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

**WebUI metadata / assets contract.** A plugin declares how it appears in the
classic WebUI *Plugins* tab via `get_metadata()` (all keys optional; the loader
derives defaults reflectively): `name`, `display_name`, `description`,
`category` (one of `Core`/`Interfaces`/`Grillo`/`Vessels`/`Agent`/`Various` —
explicit value wins, otherwise auto-derived from location), `icon` (relative
`icon.<ext>`), `guide` (relative `guide.md`), `disable_allowed`, and the
`runnable`/`run_action`/`run_label`/`run_title` "Run Now" quartet. Ship an
`icon.<ext>` (recommended 256×256) and a `guide.md` in the plugin's folder — the
WebUI serves the icon from `/api/plugins/<name>/icon` (falls back to the SyntH
logo) and renders the guide in the detail pane. **The plugin/interface manager,
not the component itself, discovers the icon.** It looks for an `icon.<ext>`
file sitting next to the component and accepts any of `png`, `svg`, `webp`,
`jpg`, `jpeg`, `gif` (resolved in that priority order); the MIME type is derived
automatically. A component never declares its own icon path. **Multi-file
plugins live in a
sub-folder.** As soon as a plugin ships more than one file (e.g. its `.py`
module *and* a `.guide.md`, or an `icon.<ext>`), those files must live together in
a dedicated `plugins/<name>/` folder — never as loose sibling files directly
under `plugins/`. The canonical layout is `plugins/<name>/<name>.py` (the
module, kept discoverable because the loader's `rglob("*.py")` skips only
`__init__.py`) plus `plugins/<name>/<name>.guide.md` (or a folder-owned
`plugins/<name>/guide.md`). To keep the historical `import plugins.<name>` path
working after the move, add a thin `plugins/<name>/__init__.py` shim that
re-exports the submodule and rebinds the package in `sys.modules`:

```python
from plugins.<name>.<name> import *  # noqa: F401,F403
import sys as _sys
from plugins.<name> import <name> as _mod
_sys.modules[__name__] = _mod
```

This preserves every symbol — public *and* private — under `plugins.<name>`
while the loader independently imports `plugins.<name>.<name>` to find
`PLUGIN_CLASS`. A genuine **single-file plugin** (a bare `plugins/<name>.py`
with *no* companion files) may still ship a sibling `plugins/<name>.guide.md`;
the WebUI's `_read_plugin_guide` falls back to that path (using the plugin's
short name) and the Sphinx collector globs it too — but the moment a second
file appears, promote it to a sub-folder.
**`guide.md` is the single source of
truth for plugin docs**: the Sphinx build collects every `plugins/*/guide.md`
*and* every `plugins/*.guide.md` into `docs/plugins/generated/` via
`_collect_plugin_guides` in `docs/conf.py`
(listed under `docs/plugins/generated_index.rst`) — never duplicate it as a
separate `.rst`. Third-party brand/trademark logos may be committed **only** if
the owner's licence, trademark policy, or press kit explicitly permits using the
mark to refer to that software — when allowed, the asset must be used
unmodified, only to refer to the original project, and attributed in
`LICENSE_EXTERNAL.md`; otherwise ship an original non-branded glyph or rely on
the SyntH logo fallback (see `AzuraCast` in `LICENSE_EXTERNAL.md` for a worked
example). Plugins can
be enabled/disabled at runtime from the WebUI (`POST /api/components/toggle`,
true unload + grey ghost record, no restart); set `disable_allowed: False` for
message-chain-critical plugins. Full reference: `docs/plugins.rst` → "Plugin
Layout, Metadata and WebUI Presentation". Reference implementation:
`plugins/radio_host/` (folder + `icon.png` + `guide.md` + explicit
`get_metadata()`).

### Background Agents (Grillo)

Some plugins are long-running scheduled agents. The canonical example is **G.R.I.L.L.O.** (`plugins/grillo/`):

- Generates periodic "beats" (introspection prompts) enqueued via `core.message_queue.enqueue_low_priority`.
- DB tables: `grillo_activity_log`, `grillo_beats`, `grillo_action_execs` (see `init-db.sql`).
- Context keys on beats: `grillo_beat`, `beat_type`, `activity_log_id`.
- Configurable via `GRILLO_BEAT_INTERVAL`; includes duplicate suppression and rate-limiting.
- Extensible: discovers beat-specific plugins (tag compactor, memory compactor, curiosity) via the plugin registry.
- **Each beat sub-plugin lives in its own sub-folder** under `plugins/grillo/<beat>/` (`<beat>.py` module + `__init__.py` `sys.modules` shim + a dedicated `guide.md`), following the standard multi-file plugin layout from §4. The shim keeps the historical `plugins.grillo.<beat>` import path working (heavily used by tests/mock patches). The core (`grillo_impl.py`, `grillo_plugin.py`) and the non-plugin helpers (`common_instructions.py`, `grillo_action_checker.py`, `grillo_response_recorder.py`) stay flat in `plugins/grillo/`. The docs collector (`docs/conf.py::_collect_plugin_guides`) globs both `plugins/*/guide.md` and `plugins/*/*/guide.md`, so these nested guides are published automatically.

The **Agent plugin** (`plugins/agent_plugin.py`) exposes Synth's agentic tools (`agent_list_files`, `agent_read_file`, `agent_write_file`, `agent_edit_file`, `agent_search_files`, `agent_run_shell`, `spawn_drone`) to the Agentic Runtime 2.0. Task state is persisted in the `agent_tasks` table. Enablement is gated by `AGENT_ENABLED` (user toggle, re-read on every `is_enabled()` call); the router 2.0 additionally requires `AGENTIC_ROUTING_ENABLED`.

`agent_write_file` (`required_fields: ["path", "content"]`, `optional_fields: ["mode"]`, `security_level: "medium"`, `external_effects: ["filesystem"]`) writes a text file inside the sandbox. It reuses `_resolve_safe_path()` / `_allowed_roots()` (same roots as `agent_read_file`: `AGENT_FS_ROOTS`, else `[AGENT_FS_ROOT|/app, SYNTH_LOG_DIR|/app/logs]`), creates parent dirs, supports `mode` = `"overwrite"` (default) or `"append"`, and caps content at 2 MB. `external_effects` makes `core/agent_router.py` route it to the Agent Lane automatically. This is a **native Python** action — chosen over the standard filesystem MCP (`@modelcontextprotocol/server-filesystem`, pre-registered but `"enabled": false` in `config/synth_mcp.json`) to avoid depending on Node at runtime: while Docker builds now bake Node in by default (`ARG INSTALL_NODE=true`, for the Minecraft Vessel bridge), a slim/non-Docker build may not have `node`/`npx`, so the filesystem tool stays pure-Python and works regardless of the deployment.

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

SyntH is a persistent cognitive entity; a **Vessel** is a layer of embodiment into an external world. The Rift Vessel subsystem lets SyntH inhabit game/virtual worlds (Minecraft shipped; Skyrim/VRChat/Hytale are registry-ready) through pluggable **connectors**, while identity/memory/personality persist across worlds and chat interfaces. Full reference: `docs/rift_vessel.rst`.

| Concern | Location |
|---------|----------|
| Connector registry (Iris pattern) | `core/vessel_registry.py` (`VESSEL_REGISTRY`, `register_vessel_connector`) |
| Connector base + schema | `plugins/rift_vessel/vessel_base.py` (`VesselConnectorBase` ABC, `WorldState`, `PerceptionEvent`, `VesselActionResult`) |
| Actions facade | `plugins/rift_vessel/vessel_plugin.py` (`VesselPlugin`, `PLUGIN_CLASS`) |
| Session lifecycle + experience buffer | `core/vessel_session_manager.py` (`vessel_session_manager`) |
| I/O interface (duck-typed) | `interface/vessel_interface.py` (`INTERFACE_NAME = "vessel"`) |
| Minecraft connector | `plugins/rift_vessel/minecraft/minecraft.py` (`CONNECTOR_CLASS`, self-registers) |
| Mineflayer bridge (Node.js) | `interface_dev/minecraft_bridge_minimal.js` |
| Bridge provisioner | `interface/minecraft_provisioner.py` (`BridgeProvisioner`, `get_bridge_provisioner`) |
| DB tables | `core/db.py::init_vessel_tables` + `init-db.sql` (`vessel_sessions`, `vessel_activity_log`) |
| WebUI Activities voice | `core/webui.py` (`/api/history/vessel`), `history.html`/`history.js` (🌀 sub-tab) |
| CLI | `core/command_registry.py` (`/vessel status`, `/minecraft provision …`) |

**Three hard constraints (all enforced):**

1. **Vessel actions never create agentic tasks.** The embodiment verbs — the world-agnostic **core set** `connect`/`disconnect`/`say`/`move`/`look`/`use`/`attack`/`follow`/`unfollow`/`respawn`/`status` (`say` accepts an optional `audio` flag mirroring the interface audio-message flag — it falls back to plain text in worlds with no voice channel; `attack` is a dedicated hostile verb kept out of `use`; `follow`/`unfollow` are universal entity-following verbs that fail cleanly when there is nothing to follow; `respawn` is a universal come-back-to-life verb that no-ops in worlds with no death/respawn concept or when already alive) — declare **no** `external_effects` → they stay on the Fast Lane (`run_actions`), never the Agent Lane / Drones. (They are still passively auto-exposed as MCP tools.) **Connection-driven action exposure.** The Vessel action set is *not* static — it mirrors the live connection state because `get_supported_actions()` is a pure read the core calls on every prompt build/dispatch/validation, so the exposed verbs change automatically on the next prompt. **Disconnected:** a single `vessel_connect` is exposed; its required `game` field is an enum of every *enabled* world (`_enabled_worlds()`, i.e. worlds whose `<world>_vessel` sub-plugin is on), plus optional `host`/`port` overriding the server address for that connect only — no gameplay verbs are visible. **Connected to world W:** the core set (minus `connect`) plus W's `get_world_actions()` extras appear namespaced `vessel_<W>_<verb>` (e.g. `vessel_minecraft_say`) alongside `vessel_disconnect`, and `vessel_connect` disappears. **Logout / inactivity cooldown** (`VESSEL_SESSION_COOLDOWN_SEC`) → the session closes, `has_active_session()` goes false, and on the next prompt the gameplay verbs vanish and `vessel_connect` returns — free, driven by `close_expired_sessions`. **No enabled worlds** → empty set. Detection is entirely structural (no keyword logic): the connected world is `_connected_world()` = `vessel_session_manager.has_active_session()` + the cached connector's `is_connected`; the `game` field is an enum *value*, not text matched. `vessel_connect` accepts the plain form or a legacy `vessel_<world>_connect`, taking the world from `game` first. **Hybrid action ownership.** The core set above is owned by the Vessel and shared by every world (guaranteeing world-agnostic portability); each connector may *additionally* declare its **own** world-specific verbs (e.g. a future Minecraft `craft`/`mine`, Skyrim `cast_spell`/`sneak`) via the optional `VesselConnectorBase.get_world_actions()` hook — returned keyed by bare verb, same schema shape, must NOT declare `external_effects`; the core plugin namespaces them under the same `vessel_<world>_` prefix and dispatches them via `connector.act(verb, payload)`. `plugins/rift_vessel/vessel_plugin.py` implements this in `_ACTION_VERBS` (core set), `_connected_world()` (structural connection probe — `has_active_session()` + cached connector `is_connected`), `_enabled_worlds()`/`_world_enabled()` (worlds whose `<world>_vessel` sub-plugin is on), `_action_world()` (active world resolution), `_world_extra_verbs_for()` (fail-safe pull of the connector's extra verbs, skipping core collisions), `get_supported_actions()` (disconnected → single `vessel_connect{game}`; connected → per-world prefix + both tiers), and `_parse_action_verb()` (accepts both legacy and namespaced forms). The Minecraft connector currently adds no extra verbs (core-set-only). A connector talks to its world directly; no reasoning loop is needed. **Entering/leaving a world is an action, not just an interface hook:** the `connect` verb (exposed as `vessel_<world>_connect`) is the LLM-invokable entry point — it loads the active connector (`ACTIVE_VESSEL`), opens a session on `interface/vessel_interface.py`, and calls `connector.connect(settings, on_event)` wiring the perception callback that forwards world events into the message chain; `vessel_disconnect` calls `connector.disconnect()` and flushes the session(s) to the single end-of-session diary entry via `end_sessions_for_environment`. Both live in `plugins/rift_vessel/vessel_plugin.py` (`connect_world`/`disconnect_world`). **`vessel_connect` accepts an optional server-address override:** the action declares `optional_fields: ["host", "port"]`, and `connect_world(overrides=...)` merges any non-empty values onto a *copy* of the saved settings before calling the connector (the saved plugin config stays the default; the override applies to that connect only, persistent config is never mutated). The Minecraft connector resolves the target via `_resolve_server_target(settings)` (override else `MINECRAFT_SERVER_HOST`/`MINECRAFT_SERVER_PORT`) and sends it in the bridge `/connect` body; the Node bridge `connectBot(overrides)` / `POST /connect` read `host`/`port` from the body, falling back to their `CFG` defaults.
2. **No diary during a session.** Events accumulate in an in-DB `experience_buffer` on `vessel_sessions`; a **single** autobiographical "lived experience" diary entry is written **only at end-of-session** — explicit logout OR `VESSEL_SESSION_COOLDOWN_SEC` (default 3600 s) of inactivity, detected by the interface scheduler calling `close_expired_sessions`.
3. **Own Activities voice.** Like Radio/Grillo: `vessel_activity_log` + `/api/history/vessel` (GET history, DELETE per-item) + a dedicated History sub-tab.

**Real-time gaming focus (two enforced behaviours).** Because the Vessel is used mainly in interactive game worlds, embodiment behaves like a person *concentrating on the game*, not multitasking every chat. Both are decided **only from routing metadata, never from message text** (project rule: no keyword logic), and both are lazily imported + fully guarded so removing the Vessel plugin can't break queueing or context assembly.

- **(1) The game takes top priority while a session is active.** When `vessel_session_manager.has_active_session()` is true (a cheap, DB-free, in-memory flag maintained by `start_session`/`end_session`), `core/message_queue.py::enqueue` **raises the Vessel's own in-world perceptions** (`interface == "vessel"`) to `HIGH_PRIORITY` and **lowers ordinary chat** from other interfaces from `NORMAL_PRIORITY` to `AGENT_PRIORITY`, so in-world perceptions drain first. Exemptions: urgent/HIGH (`priority=True`) messages always pass untouched, and the trainer (`TRAINER_CHAT_ID`) is never deprioritised.
- **(2) Vessel-focus turns get world-scoped context.** `core/history_engine.py::build_context` detects an embodiment turn from routing metadata (`interface_path` starting with `vessel`, a `chat.type == "vessel"` message, or an explicit `vessel_focus` context flag) and forces `unified_mode = False` and disables the global diary/memory injections, keeping only persona/profile + local vessel history. SyntH is not omniscient while playing — it doesn't read other chats in real time or notice unrelated global events mid-session (that catch-up happens in quiet moments and at end-of-session).

Action speed: `vessel_plugin.act()` logs the connector round-trip at `INFO` (`act('...') dispatched via '...' in N ms`). The *decision* to act still costs a full cognition turn; a reflex/attention layer that reacts without a full LLM turn is a documented future phase (must also respect constraint 1).

**Perception & salience:** the filter is LLM-free — dedup (30 s) + rate-limit (2 s) in `interface/vessel_interface.py`. A richer LLM salience/attention worker (Grillo *RAW cognition* style) is a documented future phase and must also respect constraint 1. Never stream raw telemetry into cognition.

**Core + attachable world sub-plugins (Grillo-style).** The Rift Vessel mirrors G.R.I.L.L.O.'s shape: a **core** plugin plus **attachable** per-world sub-plugins. `vessel_plugin` is the core — it owns the generic `vessel_*` actions and the **global** config (`ACTIVE_VESSEL`, `VESSEL_SETTINGS`, `VESSEL_SESSION_COOLDOWN_SEC`, all under component `vessel_plugin`). Each world is its own attachable sub-plugin with a **separate** WebUI banner and config namespace, so world-specific options are never conflated with the global entity. **WebUI coherence LED (orange).** A world sub-plugin can be enabled while the core `vessel_plugin` is disabled — a state in which that world can never actually connect. To flag this incoherence, the classic WebUI Plugins tab (`core/webui.py`) shows an **orange** status dot on any `Vessels`-category world sub-plugin whose LED would otherwise be green when `vessel_plugin` is not loaded (with a tooltip explaining the world can't connect until the Rift Vessel plugin is enabled). Enabling the core plugin restores the normal green/grey LED.

**Layout (folder-per-plugin, see §4).** The Rift Vessel core lives in `plugins/rift_vessel/` (`vessel_plugin.py` + `vessel_base.py` + `icon.svg` + `guide.md` + an empty `__init__.py`; the module `vessel_plugin` differs from the folder name so no `sys.modules` shim is needed). Each world gets its own sub-folder `plugins/rift_vessel/<world>/` (`<world>.py` + `icon.svg` + `guide.md` + empty `__init__.py`). `derive_plugin_category` maps the `rift_vessel` path token to the **Vessels** category; both `vessel_plugin` and each world sub-plugin also declare `category: "Vessels"` explicitly.

**A world module ships BOTH a connector and an attachable sub-plugin.** `plugins/rift_vessel/minecraft/minecraft.py` exposes both module-level classes: `CONNECTOR_CLASS = MinecraftConnector` (self-registers on `VESSEL_REGISTRY` at import — the actual world driver) **and** `PLUGIN_CLASS = MinecraftVesselPlugin` (a thin, action-less `PluginBase` that gives Minecraft its own WebUI banner and owns the Minecraft-specific config under component `minecraft_vessel`: `MINECRAFT_BRIDGE_RUN_AT_START`/`MINECRAFT_BRIDGE_HOST`/`MINECRAFT_BRIDGE_PORT`, `MINECRAFT_SERVER_*`, `MINECRAFT_BOT_USERNAME_OVERRIDE`, `MINECRAFT_SKIN_*` — the bridge enable state is the plugin toggle itself, `PLUGIN_ENABLED__minecraft_vessel`). The world sub-plugin's `get_supported_actions()` returns `{}` — the generic `vessel_*` actions stay in the core. This is the Grillo model applied to worlds; a world without a `PLUGIN_CLASS` would still register as a connector but would have no separate banner/config.

**Adding a world:** create `plugins/rift_vessel/<world>/<world>.py` with (1) a `VesselConnectorBase` subclass + module-level `CONNECTOR_CLASS` calling `register_vessel_connector(name, __name__, capabilities=..., label=...)` at import, and (2) a thin `PluginBase` subclass + module-level `PLUGIN_CLASS` that calls `register_plugin("<world>_vessel", self)` in `__init__`, declares its config under component `<world>_vessel`, returns `category: "Vessels"` from `get_metadata()`, and returns `{}` from `get_supported_actions()`. The world automatically gets the Vessel's core action set exposed as `vessel_<world>_<verb>`; to add **world-specific** verbs, override `get_world_actions()` on the connector (return a `{verb: schema}` mapping keyed by bare verb — same schema shape as `get_supported_actions`, **no** `external_effects` — the core plugin namespaces and dispatches them via `connector.act`). Removing any connector/sub-plugin/core/interface must not break the rest of the system.

**Minecraft deployment:** single-container, gated by the Minecraft Vessel plugin's own enable toggle (`PLUGIN_ENABLED__minecraft_vessel`) — there is no separate `MINECRAFT_BRIDGE_ENABLED` key; enable/disable the connector from its WebUI plugin card. Node.js is **baked into the Docker image by default** (Dockerfile `ARG INSTALL_NODE=true` + conditional NodeSource install), so the Minecraft Vessel works out of the box in Docker with no extra build flags — only non-Docker / bare-metal deployments need to install Node themselves, or opt out of the baked-in Node with `docker build --build-arg INSTALL_NODE=false …`. The provisioner runs the bridge as a **non-root** subprocess and returns a clear error if `node`/`npm` are missing. Uses offline auth; real Microsoft/XBL auth is out of scope.

**Vessel config keys:** `ACTIVE_VESSEL` (`"disabled"`), `VESSEL_SETTINGS`, `VESSEL_SESSION_COOLDOWN_SEC` (3600). The Minecraft connector is enabled/disabled via its plugin toggle `PLUGIN_ENABLED__minecraft_vessel` (no `MINECRAFT_BRIDGE_ENABLED` key). Minecraft keys: `MINECRAFT_BRIDGE_RUN_AT_START` (False — optional boot pre-warm; the bridge otherwise starts **on demand** when the connector connects, i.e. when Synth enters the world), `MINECRAFT_BRIDGE_HOST` (127.0.0.1, advanced), `MINECRAFT_BRIDGE_PORT` (8137, advanced), `MINECRAFT_SERVER_HOST` (127.0.0.1), `MINECRAFT_SERVER_PORT` (44383), `MINECRAFT_BOT_USERNAME_OVERRIDE` (empty, advanced — falls back to `SYNTH_NAME`), and the skin keys `MINECRAFT_SKIN_FILE` (empty — file upload in the plugin card, served over HTTP)/`MINECRAFT_SKIN_MODEL` (classic — `select` dropdown, `classic`/`slim`)/`MINECRAFT_SKIN_PUBLIC_BASE_URL` (empty, advanced — public base URL the MC server uses to fetch the skin; when empty it is auto-derived from the WebUI host, substituting the machine's primary LAN IP for a loopback host via `_detect_lan_ip()` so a same-LAN server can reach it out of the box; set explicitly for a VPN/public/reverse-proxy address)/`MINECRAFT_SKIN_COMMAND_TEMPLATE` (`/skin url {url}`, advanced).

**Minecraft skin (offline-mode caveat).** A real client-side skin *upload* is impossible for an offline-mode Mineflayer bot — the skin is decided server-side (username/UUID or a skin-management plugin), and Mineflayer only exposes read-only skin data + cape/sleeve visibility toggles, never the texture. The supported path is a **server-side skin plugin** (e.g. SkinsRestorer): the skin PNG is **uploaded directly** from the plugin card (`MINECRAFT_SKIN_FILE`, a `register_exposed_var(..., ui_type="file")` upload) and served by SyntH at `<base>/api/config/MINECRAFT_SKIN_FILE/file` — where `<base>` is `MINECRAFT_SKIN_PUBLIC_BASE_URL` if set, else auto-derived from the WebUI host/port (the MC server must be able to reach it). The connector's `_apply_skin()` runs a **config-driven, keyword-free** chat command at spawn (`MINECRAFT_SKIN_COMMAND_TEMPLATE`, default `/skin url {url}`, substituting `{url}` and `{model}`), forwarded to the bridge's `skin` action (`bot.chat`). It is a Fast-Lane connector action (no `external_effects`) and best-effort — if `MINECRAFT_SKIN_FILE` is empty no command is sent, and if no skin plugin is present the command is ignored.

---

## 6. Interfaces

- Manage I/O with external systems.
- Must forward all input into the core message chain and dispatch outputs from it.
- Never bypass the chain.
- Register actions via `get_supported_actions()`.

### Interface layout, icons and guides

Interfaces have **no base class** (they are duck-typed) and register themselves
at import time by calling the module-level `register_interface(name, self)`.

**Multi-file interfaces live in a sub-folder**, mirroring the multi-file plugin
convention from §4. The four bot interfaces (Telegram, Discord, Matrix, Fluxer)
each own a self-contained folder that ships their WebUI assets alongside the
code:

```
interface/<module>/
    <module>.py        # the interface module (loader recurses via pkgutil into packages)
    __init__.py        # package shim — re-exports the submodule under interface.<module>
    icon.<ext>         # png/svg/webp/jpg/jpeg/gif, served by the WebUI Interfaces tab
    guide.md           # setup guide rendered in the WebUI detail pane
```

The folder is named after the **module** (not the component), so the historical
`interface.<module>` import path keeps working: `interface/telegram_bot/telegram_bot.py`
(component `telegram_bot`), `interface/discord_interface/discord_interface.py`
(component `discord_bot`), `interface/matrix_interface/matrix_interface.py`
(component `matrix_chat`), `interface/fluxer_interface/fluxer_interface.py`
(component `fluxer_bot`).

The `__init__.py` shim imports the submodule as a **module** (never `from pkg
import <mod>`, which would resolve a same-named module-level global — e.g.
`discord_interface = DiscordInterface(...)` — instead of the module) and rebinds
the package to it so both `import interface.<module>` and
`from interface.<module> import Symbol` keep working:

```python
import sys as _sys
import interface.<module>.<module> as _mod
from interface.<module>.<module> import *  # noqa: E402,F401,F403

_sys.modules[__name__] = _mod
```

Interface **discovery** (`core/core_initializer.py::_discover_interfaces`)
imports both flat modules *and* packages (sub-folders); the instance-init loop
dedupes by `id(mod)` so the shim's two `sys.modules` entries don't double-init.
The WebUI resolves each interface's `icon.<ext>`/`guide.md` from its on-disk
directory: `register_interface` derives `dir_path` from the instance's
`__module__` file's parent, which now points at the sub-folder. The
plugin/interface manager (`core/webui.py`) discovers the icon by scanning that
directory for `icon.<ext>` (png/svg/webp/jpg/jpeg/gif, in that priority order)
and derives the MIME type automatically — Fluxer, for instance, ships an
`icon.svg`. The OpenAI API Server (component `ollama_serve`) follows the same
sub-folder layout: `interface/openai_api_server/openai_api_server.py` +
`icon.png` + `guide.md`. Any remaining legacy single-file interface resolves to
`interface/` and falls back to a bundled
`res/synth_webui/static/component_icons/<name>.png` and a sibling
`<name>.guide.md`.

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
| `send_file_fluxer_bot` | `interface/fluxer_interface.py` | `path`, (`channel_id` or `interface_path`) | `caption` |
| `send_file_matrix_chat` | `interface/matrix_interface.py` | `path`, `target` | `caption`, `thread_event_id` |

All four are `security_level: "medium"`, `external_effects: ["filesystem"]` — so the
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

### Testing via the OpenAI-compatible API

The OpenAI-compatible API (port 11435, also speaks the legacy Ollama protocol) can be used for quick testing without Telegram/Discord:

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

### Agent Lane: message/diary actions fail because the source interface is missing  <!-- 2025-02-14 -->
**Symptom:** An agentic turn (Agent Lane / Drone) writes files fine but the final delivery steps fail: `message_telegram_bot` is rejected by validation ("payload.interface_path or payload.chat_name is required") and `create_personal_diary_entry` persists with `interface="unknown"` / `chat_id=None`.
**Location:** `core/agent_core.py` (`AgentLoopManager.run_agentic_turn`, `_build_agent_prompt`); consumers `interface/telegram_bot.py` (`message_telegram_bot` requires `interface_path`) and `plugins/ai_diary.py` (`create_personal_diary_entry` reads `context.get("interface", "unknown")`).
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
