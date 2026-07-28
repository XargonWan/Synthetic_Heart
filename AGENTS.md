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

**Downstream char-budget clamp (external openai-protocol endpoints).** `ExternalCortexEngine` (`core/external_endpoints/bridges/cortex_bridge.py`) enforces a hard budget on the *fully-assembled* OpenAI-style messages before they leave for the endpoint, via `_clamp_messages_to_char_budget`. This exists because the **browser-driven `selenium-llm-engine` retains conversation state across requests**: when a prompt overruns the engine's per-model char limit (32000 for the active `2.5-flash`), the engine splits it with a "reply only OK / parte 1 / parte 2" chunking protocol that then contaminates *subsequent* turns (the model keeps replying `OK`). The clamp trims only non-system messages from the front (system prompt + injected action catalog are preserved), so heavy turns (e.g. the large vessel action catalog) stay under the threshold. `_DEFAULT_DOWNSTREAM_CHAR_BUDGET = 24000` leaves headroom for JSON serialization overhead (the clamp measures message *content* length, but the endpoint receives the larger role-separated JSON). Override per-endpoint with `extra_config["downstream_char_budget"]`; a non-positive value disables it. **Never modify the selenium engine itself** (explicit constraint) — the mitigation lives entirely on the SyntH side.

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
| Mineflayer bridge (Node.js) | `plugins/rift_vessel/minecraft/minecraft_bridge.js` |
| Bridge provisioner | `interface/minecraft_provisioner.py` (`BridgeProvisioner`, `get_bridge_provisioner`) |
| DB tables | `core/db.py::init_vessel_tables` + `init-db.sql` (`vessel_sessions`, `vessel_activity_log`) |
| WebUI Activities voice | `core/webui.py` (`/api/history/vessel`), `history.html`/`history.js` (🌀 Vessel sub-tab + 🎯 Goals sub-tab) |
| CLI | `core/command_registry.py` (`/vessel status`, `/vessel join [world]`, `/vessel logout`, `/minecraft provision …`) |

**Three hard constraints (all enforced):**

1. **Vessel actions never create agentic tasks.** The embodiment verbs — the world-agnostic **core set** `connect`/`disconnect`/`say`/`move`/`look`/`observe`/`use`/`attack`/`follow`/`unfollow`/`respawn`/`status` (`say` accepts an optional `audio` flag mirroring the interface audio-message flag — it falls back to plain text in worlds with no voice channel; `observe` is a universal self-awareness verb that reads the current `WorldState` and reports nearby affordances/entities/blocks in character — it powers autonomous play; `attack` is a dedicated hostile verb kept out of `use`; `follow`/`unfollow` are universal entity-following verbs that fail cleanly when there is nothing to follow; `respawn` is a universal come-back-to-life verb that no-ops in worlds with no death/respawn concept or when already alive) — declare **no** `external_effects` → they stay on the Fast Lane (`run_actions`), never the Agent Lane / Drones. (They are still passively auto-exposed as MCP tools.) **Connection-driven action exposure.** The Vessel action set is *not* static — it mirrors the live connection state because `get_supported_actions()` is a pure read the core calls on every prompt build/dispatch/validation, so the exposed verbs change automatically on the next prompt. **Disconnected:** a single `vessel_connect` is exposed; its required `game` field is an enum of every *enabled* world (`_enabled_worlds()`, i.e. worlds whose `<world>_vessel` sub-plugin is on), plus optional `host`/`port` overriding the server address for that connect only — no gameplay verbs are visible. **Connected to world W:** the core set (minus `connect`) plus W's `get_world_actions()` extras appear namespaced `vessel_<W>_<verb>` (e.g. `vessel_minecraft_say`) alongside `vessel_disconnect`, and `vessel_connect` disappears. **Logout / inactivity cooldown** (`VESSEL_SESSION_COOLDOWN_SEC`) → the session closes, `has_active_session()` goes false, and on the next prompt the gameplay verbs vanish and `vessel_connect` returns — free, driven by `close_expired_sessions`. **No enabled worlds** → empty set. Detection is entirely structural (no keyword logic): the connected world is `_connected_world()` = `vessel_session_manager.has_active_session()` + the cached connector's `is_connected`; the `game` field is an enum *value*, not text matched. `vessel_connect` accepts the plain form or a legacy `vessel_<world>_connect`, taking the world from `game` first. **Hybrid action ownership.** The core set above is owned by the Vessel and shared by every world (guaranteeing world-agnostic portability); each connector may *additionally* declare its **own** world-specific verbs (e.g. a future Minecraft `craft`/`mine`, Skyrim `cast_spell`/`sneak`) via the optional `VesselConnectorBase.get_world_actions()` hook — returned keyed by bare verb, same schema shape, must NOT declare `external_effects`; the core plugin namespaces them under the same `vessel_<world>_` prefix and dispatches them via `connector.act(verb, payload)`. `plugins/rift_vessel/vessel_plugin.py` implements this in `_ACTION_VERBS` (core set), `_connected_world()` (structural connection probe — `has_active_session()` + cached connector `is_connected`), `_enabled_worlds()`/`_world_enabled()` (worlds whose `<world>_vessel` sub-plugin is on), `_action_world()` (active world resolution), `_world_extra_verbs_for()` (fail-safe pull of the connector's extra verbs, skipping core collisions), `get_supported_actions()` (disconnected → single `vessel_connect{game}`; connected → per-world prefix + both tiers), and `_parse_action_verb()` (accepts both legacy and namespaced forms). The Minecraft connector currently adds no extra verbs (core-set-only). A connector talks to its world directly; no reasoning loop is needed. **Entering/leaving a world is an action, not just an interface hook:** the `connect` verb (exposed as `vessel_<world>_connect`) is the LLM-invokable entry point — it loads the active connector (`ACTIVE_VESSEL`), opens a session on `interface/vessel_interface.py`, and calls `connector.connect(settings, on_event)` wiring the perception callback that forwards world events into the message chain; `vessel_disconnect` calls `connector.disconnect()` and flushes the session(s) to the single end-of-session diary entry via `end_sessions_for_environment`. Both live in `plugins/rift_vessel/vessel_plugin.py` (`connect_world`/`disconnect_world`). **`vessel_connect` accepts an optional server-address override:** the action declares `optional_fields: ["host", "port"]`, and `connect_world(overrides=...)` merges any non-empty values onto a *copy* of the saved settings before calling the connector (the saved plugin config stays the default; the override applies to that connect only, persistent config is never mutated). The Minecraft connector resolves the target via `_resolve_server_target(settings)` (override else `MINECRAFT_SERVER_HOST`/`MINECRAFT_SERVER_PORT`) and sends it in the bridge `/connect` body; the Node bridge `connectBot(overrides)` / `POST /connect` read `host`/`port` from the body, falling back to their `CFG` defaults.
2. **No diary during a session.** Events accumulate in an in-DB `experience_buffer` on `vessel_sessions`; a **single** autobiographical "lived experience" diary entry is written **only at end-of-session**. End-of-session is reached via explicit logout (`/vessel logout` CLI or the `vessel_disconnect` action), OR `VESSEL_SESSION_COOLDOWN_SEC` (default 3600 s) of inactivity (`close_expired_sessions`), OR the **connection-driven 3-state lifecycle** below. **Connection-driven 3-state session lifecycle.** A session's liveness is tied to the *real* connector, not just a DB row. The interface registers a **liveness probe** into the session manager at `start()` (`get_vessel_session_manager().set_liveness_probe(self._any_connector_live)`) — this keeps `core/vessel_session_manager.py` free of any interface/connector import while letting `has_active_session()` reflect the connector's actual `is_connected` state. The three states: **CONNECTED** — a session id exists *and* the probe returns True → `has_active_session()` True → beats/perceptions run. **RECONNECTING** — the session id still exists but the connector dropped (probe False) → `has_active_session()` reads **False**, so new vessel elements are *frozen* (no beats/perceptions enqueued) while priorities are left untouched; the disconnect sweep retries the connection each tick for up to `VESSEL_DISCONNECT_GRACE_SEC` (default 30 s, clamped 5–3600). A successful reconnect flips the probe True → back to CONNECTED. **ENDED** — the grace window elapsed without recovery → the session is force-closed (`end_sessions_for_environment(reason="disconnected")` → `end_session`), the experience buffer is flushed to the single diary entry, and **all queued vessel traffic for that world is purged** via `drop_vessel_queue_for_world`, so nothing stale is dispatched into a dead world. The sweep runs **every scheduler tick** (`interface/vessel_interface.py::_close_disconnected_sessions`): it records `_disconnected_since[world]` on first-seen disconnection, calls `_attempt_reconnect(world)` on entry and each in-grace tick, and only ends the session once `now - first_seen >= grace`. Without this, a dropped client would leave the session `active` for the full 3600 s cooldown, generating beats that consume cognition on a dead world. The Minecraft connector detects the drop structurally: `MinecraftConnector._poll_loop` counts consecutive `/events` poll failures and, after `_MAX_POLL_FAILURES` (5, ≈5 s), sets `self._connected = False` so `is_connected` reports the loss.
3. **Own Activities voice.** Like Radio/Grillo: `vessel_activity_log` + `/api/history/vessel` (GET history, DELETE per-item) + a dedicated History sub-tab. The History section actually carries **two** Vessel sub-tabs: the 🌀 **Vessel** tab (actions + session history) and a 🎯 **Goals** tab that, *per game/world*, renders cards showing the goals Synth authored for itself and their steps. The Goals tab is backed by `GET /api/history/vessel/goals` (`core/webui.py::history_vessel_goals`), which iterates the enabled worlds (`_enabled_vessel_worlds()` → `vessel_plugin._enabled_worlds()`), resolves a per-world goal reader (`_resolve_world_goal_reader()`; Minecraft → `plugins/rift_vessel/minecraft/goals.py::list_all_goals`), and returns `{"success": True, "worlds": [{"world", "goals": [...]}]}`. It renders **only** what the model stored (free-text self-authored goals + steps) — no catalogue, no fixed step schema — honouring the spontaneity rule. Frontend: `renderGoalsWorld`/`renderGoalCard` in `res/synth_webui/js/history.js`, styled in `core/webui_templates/sections/history.html`.

**Real-time gaming focus (two enforced behaviours).** Because the Vessel is used mainly in interactive game worlds, embodiment behaves like a person *concentrating on the game*, not multitasking every chat. Both are decided **only from routing metadata, never from message text** (project rule: no keyword logic), and both are lazily imported + fully guarded so removing the Vessel plugin can't break queueing or context assembly.

- **(1) Pure 0–10 numeric priority, no de-prioritisation.** `core/message_queue.py` ranks messages on an absolute urgency scale where **higher = more urgent** (a min-heap keyed by `_heap_key(p) = -int(p)`): `PRIORITY_EMERGENCY = 10`, `PRIORITY_URGENT = 9` (`priority=True`), `PRIORITY_HIGH = 8` (direct human input, e.g. an in-world **player** chat), `PRIORITY_TRAINER = 7`, `PRIORITY_GENERAL = 6` (ordinary chat), `PRIORITY_AMBIENT = 4` (Synth's **own** autonomous vessel perceptions/beats — below every human), `PRIORITY_LOW = 3` (Grillo/radio/background). `enqueue` assigns each message its rung from **structural origin only** (never message text, never conditional on session state): a real player chat → `PRIORITY_HIGH`, an autonomous vessel perception/beat → `PRIORITY_AMBIENT`, the trainer → `PRIORITY_TRAINER`, ordinary chat → `PRIORITY_GENERAL`, urgent → `PRIORITY_URGENT`. There is **no de-prioritisation**: ordinary chat is never demoted because a session is active, so a person addressing Synth is always answered promptly while the game's own perceptions simply sit at a lower rung. This replaced the earlier scheme that raised perceptions to `HIGH` and demoted chat to `AGENT_PRIORITY` (which caused chat starvation).
- **(2) Vessel-focus turns get world-scoped context.** `core/history_engine.py::build_context` detects an embodiment turn from routing metadata (`interface_path` starting with `vessel`, a `chat.type == "vessel"` message, or an explicit `vessel_focus` context flag) and forces `unified_mode = False` and disables the global diary/memory injections, keeping only persona/profile + local vessel history. SyntH is not omniscient while playing — it doesn't read other chats in real time or notice unrelated global events mid-session (that catch-up happens in quiet moments and at end-of-session).

Action speed: `vessel_plugin.act()` logs the connector round-trip at `INFO` (`act('...') dispatched via '...' in N ms`). The *decision* to act still costs a full cognition turn; a reflex/attention layer that reacts without a full LLM turn is a documented future phase (must also respect constraint 1).

**Perception & salience:** the filter is LLM-free — dedup (30 s) + rate-limit (2 s) in `interface/vessel_interface.py`. A richer LLM salience/attention worker (Grillo *RAW cognition* style) is a documented future phase and must also respect constraint 1. Never stream raw telemetry into cognition.

**Perceptions never evict player chat (separate context buffer).** Autonomous perceptions (sightings/movement/damage/will-beats) and real conversation are stored in **two separate in-memory windows** in `core/chat_context_manager.py`, keyed purely by a structural metadata flag — never by content. The conversational window is a bounded `deque(maxlen=CHAT_HISTORY_LIMIT)`; a **separate** `_perception_memory` ring (`deque(maxlen=_PERCEPTION_MEMORY_MAXLEN=32)`) holds perceptions. `add_message_to_context` routes a message to `_perception_memory` when `metadata["vessel_perception"]` is set (tagged at ingestion by `interface/vessel_interface.py::on_world_event` as `{"vessel_perception": True, "vessel_event_type": <type>}`, `None` for a player chat), so a rapid ambient burst (e.g. repeated **drowning** damage, which bypasses the 30 s dedup by design) can **never** evict a player's chat from the bounded conversational deque. `load_chat_history` rehydrates the split from the DB `chat_history_cache.metadata JSON` column; `clear_chat_context` clears both. The vessel-focus prompt (`core/history_engine.py`) merges them: it drops autonomous perceptions from the conversational `window`, then appends the most recent `VESSEL_PERCEPTION_CONTEXT_CAP` (default 3) perceptions from `get_perception_memory()`, sorted chronologically — so ambient grounding is still present without starving the conversation. This is the fix for the "Rekku replied with one stock line to every player" bug (§12): the earlier `history_engine` prompt-cap was necessary but insufficient because the `maxlen` deque discards player chat *upstream* of the prompt. Keyword-free; removing the vessel subsystem leaves `_perception_memory` empty.

**En-route element collection (CORE, world-agnostic).** While the body travels A→B it keeps a per-session, world-scoped registry (`VesselInterface._seen_elements: dict["vessel/<world>", set["kind:target"]]`) of everything it has already perceived, so Synth *knows* what it passed and can divert toward something rare/interesting (a quest item), revisit it later, or mention it to other players. After each fast motor tick, `_collect_en_route_sightings(world, world_state)` reads the connector's structural `WorldState.extra["affordances"]` (`{kind, target, verb, distance}`, distance-sorted), keys novelty via `_element_signature` (`kind:target`, `kind` defaults `"thing"`), and surfaces each *first* sighting **once** as a new `sighting` perception through `on_world_event(event_type="sighting", …, data={kind,target,distance})`. The registry records **all** seen elements (recallable via `observe`) while the dedup/rate-limit salience filter paces what reaches cognition (the slow will beat) — so a burst of new blocks never floods the chain; the nearest new element wins each rate-limit window. `end_sessions_for_environment` pops the world's registry so the next session starts fresh. Keyword-free, no LLM, fully guarded (a failure never disrupts the motor tick). Whether a sighting is "rare" or "the item I wanted" is Synth's own cognition-turn judgement, never decided here.

**Autonomous play — three speeds: volition (slow, LLM) + action (middle, LLM) + motorics (fast, reflex).** By default a session is reactive; when `VESSEL_AUTONOMY_ENABLED` is on, Synth **plays on its own** — wanders, looks around, sets and pursues its own goals, gathers, builds, interacts — while still obeying all three constraints (Fast Lane only, no Agent Lane/Drones, single end-of-session diary). Autonomy is deliberately **split into three independently-paced layers** so that *deciding what to want* (slow, personality-driven) never bottlenecks *deciding the next concrete step* (middle), which never bottlenecks *moving the body* (fast, reactive). The middle **action beat** closes the "walks around but accomplishes nothing" gap: the will beat authors a free-text goal but is forbidden to move/act, and the motor tick moves but never reads goal text — so nothing translated *"gather wood"* into the concrete verb `vessel_minecraft_collect_block`/`mine`/`craft`. The action beat is that translator (mapping is cognition's, no keyword logic).

- **Will beat — volition (slow, LLM).** Mirrors G.R.I.L.L.O.: the interface scheduler (`interface/vessel_interface.py`, 10 s tick) fires a **will beat** every `VESSEL_WILL_INTERVAL_SEC` s (default 45, falls back to the legacy `VESSEL_BEAT_INTERVAL_SEC`, clamped `[10, 3600]`) via `_maybe_run_will_beat` while a session is active. The beat reads the live connector's `WorldState`, builds a **structural, keyword-free** volition prompt with `core/vessel_beat.py::build_will_prompt` (surfacing position/health/time/entities/blocks/inventory/affordances/current+recent goals straight from the `WorldState` contract, and framing the turn as *will, not motion* — "your body will move toward it on its own"), and enqueues it as a **normal** `vessel` message (`chat.type == "vessel"`, `interface_path` `vessel/<world>`) so `build_context` applies world-scoped context and the core runs **one ordinary Fast-Lane cognition turn** in which Synth writes/keeps/updates a free-text goal via `vessel_<world>_set_goal`/`vessel_<world>_update_goal`. This is where Synth's **will and memories** live — the goal is authored from personality, not a script. The will beat is FORBIDDEN to move or act. `build_decision_prompt` remains a backward-compat alias of `build_will_prompt`.
- **Action beat — "idea → concrete step" (middle, LLM).** A second, faster LLM beat fires every `VESSEL_ACTION_INTERVAL_SEC` s (default 20, clamped `[3, 300]`, gated by `VESSEL_ACTION_BEAT_ENABLED`, default True) via `_maybe_run_action_beat`. Built by `core/vessel_beat.py::build_action_prompt` (returns `""` — no beat — when there is no active goal), it frames the turn as *"a moment to actually do something toward your goal"* and asks for exactly **one** concrete step: Synth picks a world verb (`vessel_<world>_collect_block`/`mine`/`craft`/`smelt`/`place`/`goto`/`say`) and may record progress via `vessel_<world>_update_goal` with `advance=true`. Enqueued as an ordinary Fast-Lane `vessel` message like the will beat; the player-quiet deferral (`VESSEL_WILL_QUIET_SEC`) applies so a player addressing Synth in-world is answered reactively, not overridden. Respects all three constraints (Fast Lane, no Agent Lane/Drones, no mid-session diary).
- **Motor tick — motorics (fast, no LLM).** A separate, much faster loop moves the body toward the current goal with **no prompt, no cognition turn, no diary**. The scheduler calls `_maybe_run_motor_tick` every `VESSEL_MOTOR_INTERVAL_SEC` s (default 3, clamped `[1, 60]`, gated by `VESSEL_MOTOR_ENABLED`, default True) while a session is active; it fetches the active connector and current goal and calls `await connector.motor_step(goal)` **directly** — never enqueuing a message. `motor_step` is a pure reflex over the **structural affordance contract only** (`{kind, target, verb, distance}`, distance-sorted): it picks the nearest benign affordance (verb `use`/`mine`, hostile `attack` skipped), then `mine`s a block or `use`s an entity within `_MOTOR_REACH` (3.0 m), else `goto`s it, else `wander`s. The goal's **already-validated structural fields** (`target_kind`/`target_name`, populated by cognition — never free text) may steer *where* the body walks: when the goal names a **block** target and that exact block is a live affordance within reach, the reflex `mine`s it (returning `{"action": "mine", "target": …, "target_kind": "block"}`) instead of standing next to it re-issuing `goto` — the "walks up but never picks anything up" gap; entities are never mined. It still **never reads the goal's free text**. The base `VesselConnectorBase.motor_step` is a no-op returning `{"acted": False, "reason": "no_motorics"}`, so a world without motorics degrades gracefully. This fulfils the "reflex/attention layer that reacts without a full LLM turn" anticipated above, and still respects constraint 1 (no agentic task, no mid-session diary). **Structured inventory:** `get_world_state` also aggregates the raw stack list into an id→total map exposed as `WorldState.extra["inventory_counts"]` (via `MinecraftConnector._inventory_counts`, fail-safe, keyword-free) so cognition can judge how many of a thing it still needs without rescanning.

- **Self-preservation guard — survival reflex (fast, no LLM).** Evaluated **first** on every motor tick, before the no-goal early return, so Synth reacts to danger even with no active goal. `MinecraftConnector._survival_threat(state)` classifies threats from **numeric telemetry + game enum ids only** (never user text) in strict priority: **dead** → `respawn`; **drowning** (head submerged — liquid block id at head or `is_in_water` — AND `oxygen <= _sp_low_oxygen`) → `goto_surface` (reuses `goto` toward `y + _SURFACE_CLIMB_BLOCKS`=8, no new bridge verb); **burning** (feet/head on a hot block id: `lava`/`flowing_lava`/`fire`/`soul_fire`/`magma_block`) → `flee`; **hostile** near (`kind=="mob"`/`hostile` flag within `_sp_hostile_dist`) → **defend** (verb `attack`) while `VESSEL_SP_FIGHT_BACK` on AND health ≥ flee-threshold AND fail_count < `VESSEL_SP_FIGHT_MAX_FAILS`, else **escalate to flee**. `get_world_state().extra` is enriched with `oxygen`/`is_in_water`/`is_alive`/`block_feet`/`block_head`/`health`/`threat`/`threat_reason`; the Node bridge `worldSnapshot` supplies the raw fields and tags nearby entities with a structural `hostile` flag. **Hunger** is handled OUTSIDE the guard by the `mineflayer-auto-eat` bridge plugin (auto `require`+`loadPlugin` in `minecraft_bridge.js`) — no manual verb, no motor branch, no config key. `core/vessel_beat.py::build_will_prompt` appends a structural threat cue so the slow will beat knows a reflex just fired. Config (component `vessel_plugin`): `VESSEL_SELF_PRESERVATION_ENABLED` (True), `VESSEL_SP_LOW_OXYGEN` (**6 — 0..20 bubble scale, see gotcha**), `VESSEL_SP_LOW_HEALTH` (6), `VESSEL_SP_HOSTILE_DIST` (8), `VESSEL_SP_FIGHT_BACK` (True), `VESSEL_SP_FIGHT_MAX_FAILS` (3). **GOTCHA: at RUNTIME mineflayer `bot.oxygenLevel` reports the vanilla 0..20 air-bubble scale (20 = full lungs, 0 = out of air), NOT air ticks** — validated live: a healthy submerged bot reads ~20, so the drowning threshold MUST be on the 0..20 scale (default 6 ≈ two bubbles left). An air-ticks threshold (e.g. 200) would fire the drowning reflex constantly (false positive) because the runtime value never approaches it. This respects constraint 1 (pure motor reflex, no agentic task, no mid-session diary). Reference studied: mindcraft-bots/mindcraft (reimplemented natively; mindcraft never touched). Unit-tested in `tests/test_vessel_survival.py`.

`core/vessel_beat.py` is pure/side-effect-free (dataclass **or** dict input, fail-safe autonomy gating, interval clamp/failsafe on both `resolve_will_interval` and `resolve_motor_interval`, `is_motor_enabled`) and fully unit-tested (`tests/test_vessel_beat.py`) without DB/bridge/LLM; `MinecraftConnector.motor_step`'s structural rules are unit-tested in `tests/test_vessel_minecraft_motor.py`. **Generic self-awareness** is the `observe` core verb (reads `WorldState`, reports affordances/entities/blocks in character). **Affordances** follow a generic structural contract `{kind, target, verb, distance}` built by the connector from the raw snapshot — never keyword matching — so both the volition prompt and the motor reflex stay world-agnostic. **What to play is world-specific**: `plugins/rift_vessel/minecraft/goals.py` is a Minecraft **goal store** — it does **not** ship a catalogue, templates, prerequisites, or inventory-count progression (a fixed quest menu would make every Synth play identically, like a scripted bot). It only *persists* and *recalls* the free-text goals Synth writes for itself; progress is judged by Synth from what it perceives, never by an item counter. Goals are kept in the `minecraft_goals` table so a goal survives across beats within a session. The connector exposes extra verbs via `get_world_actions()` — bridge-backed `goto`/`scan`/`mine`/`place`/`inventory`/`wander` plus the goal-store verbs `set_goal`/`goals`/`update_goal` — and enriches `WorldState.extra` with `current_goal`/`recent_goals`. Synth authors its own goals (`vessel_minecraft_set_goal`, required free-text `description` + optional `note`) during a beat — *"diamonds now or build a chest first?"* is its call, driven by its personality and wants, not a script. All autonomy wiring is lazily imported and guarded: removing the beat module, the goal store, or disabling the flag never breaks the reactive Vessel.

**Core + attachable world sub-plugins (Grillo-style).** The Rift Vessel mirrors G.R.I.L.L.O.'s shape: a **core** plugin plus **attachable** per-world sub-plugins. `vessel_plugin` is the core — it owns the generic `vessel_*` actions and the **global** config (`ACTIVE_VESSEL`, `VESSEL_SETTINGS`, `VESSEL_SESSION_COOLDOWN_SEC`, all under component `vessel_plugin`). Each world is its own attachable sub-plugin with a **separate** WebUI banner and config namespace, so world-specific options are never conflated with the global entity. **WebUI coherence LED (orange).** A world sub-plugin can be enabled while the core `vessel_plugin` is disabled — a state in which that world can never actually connect. To flag this incoherence, the classic WebUI Plugins tab (`core/webui.py`) shows an **orange** status dot on any `Vessels`-category world sub-plugin whose LED would otherwise be green when `vessel_plugin` is not loaded (with a tooltip explaining the world can't connect until the Rift Vessel plugin is enabled). Enabling the core plugin restores the normal green/grey LED.

**Layout (folder-per-plugin, see §4).** The Rift Vessel core lives in `plugins/rift_vessel/` (`vessel_plugin.py` + `vessel_base.py` + `icon.svg` + `guide.md` + an empty `__init__.py`; the module `vessel_plugin` differs from the folder name so no `sys.modules` shim is needed). Each world gets its own sub-folder `plugins/rift_vessel/<world>/` (`<world>.py` + `icon.svg` + `guide.md` + empty `__init__.py`). `derive_plugin_category` maps the `rift_vessel` path token to the **Vessels** category; both `vessel_plugin` and each world sub-plugin also declare `category: "Vessels"` explicitly.

**A world module ships BOTH a connector and an attachable sub-plugin.** `plugins/rift_vessel/minecraft/minecraft.py` exposes both module-level classes: `CONNECTOR_CLASS = MinecraftConnector` (self-registers on `VESSEL_REGISTRY` at import — the actual world driver) **and** `PLUGIN_CLASS = MinecraftVesselPlugin` (a thin, action-less `PluginBase` that gives Minecraft its own WebUI banner and owns the Minecraft-specific config under component `minecraft_vessel`: `MINECRAFT_BRIDGE_RUN_AT_START`/`MINECRAFT_BRIDGE_HOST`/`MINECRAFT_BRIDGE_PORT`, `MINECRAFT_SERVER_*`, `MINECRAFT_BOT_USERNAME_OVERRIDE`, `MINECRAFT_SKIN_*` — the bridge enable state is the plugin toggle itself, `PLUGIN_ENABLED__minecraft_vessel`). The world sub-plugin's `get_supported_actions()` returns `{}` — the generic `vessel_*` actions stay in the core. This is the Grillo model applied to worlds; a world without a `PLUGIN_CLASS` would still register as a connector but would have no separate banner/config.

**Adding a world:** create `plugins/rift_vessel/<world>/<world>.py` with (1) a `VesselConnectorBase` subclass + module-level `CONNECTOR_CLASS` calling `register_vessel_connector(name, __name__, capabilities=..., label=...)` at import, and (2) a thin `PluginBase` subclass + module-level `PLUGIN_CLASS` that calls `register_plugin("<world>_vessel", self)` in `__init__`, declares its config under component `<world>_vessel`, returns `category: "Vessels"` from `get_metadata()`, and returns `{}` from `get_supported_actions()`. The world automatically gets the Vessel's core action set exposed as `vessel_<world>_<verb>`; to add **world-specific** verbs, override `get_world_actions()` on the connector (return a `{verb: schema}` mapping keyed by bare verb — same schema shape as `get_supported_actions`, **no** `external_effects` — the core plugin namespaces and dispatches them via `connector.act`). Removing any connector/sub-plugin/core/interface must not break the rest of the system.

**Scope rule (where a feature belongs).** When deciding whether a capability lives in the Rift Vessel **core** or in a **world adapter/plugin**, apply this rule: *if the feature is common to the great majority of games/worlds, its scope is the Rift Vessel core (a generic `vessel_*` verb on `vessel_base`/`vessel_plugin`); if it is specific to one game, it belongs to that game's adapter/plugin* (a world-specific verb via `get_world_actions()` on the connector, namespaced `vessel_<world>_<verb>`). E.g. move/look/observe/goals are core; **crafting is a Minecraft-specific verb** and therefore lives on the Minecraft connector, not the core.

**Spontaneity rule (autonomous play is not hard-coded).** Autonomous play must be **spontaneous and human-like, never a hard-coded/scripted quest list**. Synth chooses *for itself* — out of its personality and mood — what to do in a world; the code owns only lifecycle/persistence, never the *content* of goals or a fixed catalogue of objectives. `plugins/rift_vessel/minecraft/goals.py` embodies this: it persists free-text, self-authored goals in `minecraft_goals` and judges nothing — there is no `gather_wood`/`find_diamonds` template and no auto-progress counter. Affordances are structural (`{kind, target, verb, distance}`, distance-sorted) and are **never** matched by name keywords. Two different Synths, or the same Synth on two different days, should set completely different goals.

**Minecraft world-specific verbs.** Beyond the core set, the Minecraft connector adds world-specific verbs via `get_world_actions()`, including **`craft`** (`vessel_minecraft_craft`): required `item` (lowercase Minecraft item id, e.g. `oak_planks`, `stick`, `crafting_table`, `wooden_pickaxe`), optional `count` (clamped `[1, 64]`), `search_radius`, `timeout_ms`; `security_level: "low"`, **no** `external_effects`. The bridge resolves the recipe via `bot.recipesFor()`/`bot.craft()`, auto-locates a nearby `crafting_table` and pathfinds to it when a 3×3 recipe requires one, and returns a structural fail-safe result (`ok:false` with a clear reason) when materials or a reachable table are missing — no keyword logic. The `status`/`scan` world snapshot also exposes `game_mode` (e.g. `creative`/`survival`) so cognition can reason about world limitations (e.g. in `creative` mined blocks yield no drops, a vanilla behaviour, not a bug).

**Game knowledge base (reference, not a script).** A Synth that doesn't know a world's *rules* plays badly — e.g. it mines iron ore bare-handed and gets nothing, never having learned iron needs at least a stone pickaxe. Each world may ship a small **knowledge base (KB)**: the *mechanism* is world-agnostic (the Vessel core renders whatever facts a world supplies) and the *content* is world-specific (the Minecraft adapter owns its facts). The KB is strictly **reference** — it states how the world works, never what to do — so the spontaneity rule (self-authored goals, no catalogue) is preserved. **Source:** the Minecraft adapter consults the **live [minecraft.wiki](https://minecraft.wiki)** (its MediaWiki API is open to bots, no auth) via `plugins/rift_vessel/minecraft/wiki_client.py` — there is **no** curated fact file. `wiki_client.lookup(query, limit, *, cache_only=False)` searches for matching pages, then for each page serves a **one-time LLM summary** cached incrementally on disk as `plugins/rift_vessel/minecraft/wiki/cache/<slug>.json` (`{title, url, raw_extract, summary, fetched_at}`); the summary is a short EN factual note (*"how the game works"*, never *what to do*). Subsequent lookups of the same page are served from cache — no re-fetch, no re-summarise. Matching is keyword-free/structural: the `query` is whitespace-joined game tokens (a goal `target_name`, block/item ids), matched against page-title slugs. **Verb:** the connector exposes a Fast-Lane, `external_effects`-free `lookup_knowledge` (`vessel_minecraft_lookup_knowledge`, `required_fields: ["query"]`, `optional_fields: ["limit"]`, `security_level: "low"`); `MinecraftConnector.lookup_knowledge(query, limit=5, *, cache_only=False)` delegates to `wiki_client.lookup` and returns notes as `{title, text, url}`. **Beat vs verb split (important):** the automatic will/motor-beat path (`_resolve_knowledge`) calls the lookup with **`cache_only=True`** so a `WorldState` build never blocks on the network or the LLM — it serves only already-cached pages; the **explicit** `lookup_knowledge` verb and the goal-expansion Drone use the default live path (`cache_only=False`), which is allowed to fetch + summarise. Everything is best-effort/fail-safe: offline or on any error the client returns whatever it has cached (possibly empty) and never raises, so a Fast-Lane beat can't break. Config: `VESSEL_KNOWLEDGE_LIVE_FETCH` (bool, default True — disables all network, cache-only everywhere), `VESSEL_KNOWLEDGE_FETCH_TIMEOUT_SEC` (int, default 4, clamp 1–30), `VESSEL_KNOWLEDGE_SUMMARY_MAX_CHARS` (int, default 600, clamp 120–4000). Fully offline-testable with the live API + LLM mocked (`tests/test_vessel_knowledge.py`). **Prompt injection:** when a beat's `WorldState.extra["knowledge"]` is populated, `core/vessel_beat.py::_fmt_knowledge` renders it into both the will and action prompts as a bulleted **"Game knowledge"** block headed by an explicit *reference, not a script* framing (purely structural — never inspects fact text for keywords — and drops the block when nothing renderable survives). **Drone goal expansion:** when Synth authors a *new* goal, a Drone (single-level ephemeral sub-agent, §5b) can expand it into ordered sub-steps by consulting the KB via `lookup_knowledge` — turning *"get some iron"* into *"craft a wooden pickaxe → mine stone → craft a stone pickaxe → mine iron ore"* (the mapping is the Drone's reasoning, no fixed table, no keyword routing). **After the goal is updated with its sub-steps it is re-notified to Synth via a will beat**, so the next volition turn acts on the freshly-expanded plan. The WebUI Goals sub-tab renders the sub-steps **collapsed by default** (a `<details>` disclosure labelled `Plan · done/total steps`, styled in `core/webui_templates/sections/history.html`, built in `res/synth_webui/js/history.js::renderGoalCard`).

**Minecraft deployment:** single-container, gated by the Minecraft Vessel plugin's own enable toggle (`PLUGIN_ENABLED__minecraft_vessel`) — there is no separate `MINECRAFT_BRIDGE_ENABLED` key; enable/disable the connector from its WebUI plugin card. Node.js is **baked into the Docker image by default** (Dockerfile `ARG INSTALL_NODE=true` + conditional NodeSource install), so the Minecraft Vessel works out of the box in Docker with no extra build flags — only non-Docker / bare-metal deployments need to install Node themselves, or opt out of the baked-in Node with `docker build --build-arg INSTALL_NODE=false …`. The provisioner runs the bridge as a **non-root** subprocess and returns a clear error if `node`/`npm` are missing. Uses offline auth; real Microsoft/XBL auth is out of scope. **The bridge's Node runtime is self-contained inside the plugin package.** `interface/minecraft_provisioner.py::BridgeProvisioner` keeps the whole runtime under `plugins/rift_vessel/minecraft/mineflayer/` (constant `_BRIDGE_RUNTIME_SUBDIR = "mineflayer"`): a **committed** `package.json` pins the deps (`mineflayer`, `mineflayer-pathfinder`, `minecraft-data`), and `node_modules/`/`bridge.json`/`bridge.log` are per-run artefacts (gitignored, installed at first run via `npm install` into that folder, or pre-bundled in the shipped zip for a fully offline package). The bridge *script* `minecraft_bridge.js` stays in the plugin folder one level up and is executed with `NODE_PATH` prepended with `mineflayer/node_modules`, so `require('mineflayer')` resolves against the package's own modules regardless of the script's location. The whole `plugins/rift_vessel/minecraft/` tree is therefore self-sufficient and zip-shippable — nothing lives under a shared `/opt` path that a container recreate would lose. Tests override the location with an explicit constructor `bridge_root` (or env `MINECRAFT_BRIDGE_ROOT`). **Bridge memory footprint — pin the render distance or Node OOM-crashes in ~2 min.** mineflayer caches every chunk the server streams, so on a normal render distance the Node old-space grows unbounded toward its ~4 GB default heap limit; observed live: RSS climbs to ~3.9 GB then the process dies with `FATAL ERROR: Ineffective mark-compacts near heap limit — JavaScript heap out of memory` (via `node::OOMErrorHandler`) at ~137 s, killing the session shortly after a successful connect. Two guards prevent this: (1) `minecraft_bridge.js` passes `viewDistance: 'tiny'` in the `createBot` options (the bot does not need to see far to play, so the smallest view keeps the chunk cache tiny — root fix); (2) `interface/minecraft_provisioner.py::_bridge_env()` appends `--max-old-space-size=512` to `NODE_OPTIONS` on the bridge subprocess (safety belt — forces aggressive GC well before the 4 GB limit). Validated: with both guards the bridge holds ~195 MB RSS and stays alive indefinitely while playing autonomously. A successful `/connect` is NOT proof of a durable session — poll `/health` over several minutes to confirm the bridge survives.

**Vessel config keys:** `ACTIVE_VESSEL` (`"disabled"`), `VESSEL_SETTINGS`, `VESSEL_SESSION_COOLDOWN_SEC` (3600), `VESSEL_AUTONOMY_ENABLED` (False — enable autonomous play, both layers), `VESSEL_WILL_INTERVAL_SEC` (45, clamped `[10, 3600]`, falls back to the legacy `VESSEL_BEAT_INTERVAL_SEC` — seconds between slow volition/will beats), `VESSEL_WILL_QUIET_SEC` (60, clamped `[0, 3600]`, `0` disables — quiet window a player interaction must have elapsed before the will beat may fire, so a directly-addressing player is answered reactively rather than ignored by the "on your own" volition prompt), `VESSEL_MOTOR_ENABLED` (True — enable the fast motorics reflex), `VESSEL_MOTOR_INTERVAL_SEC` (3, clamped `[1, 60]` — seconds between fast motor ticks that move the body with no LLM). The Minecraft connector is enabled/disabled via its plugin toggle `PLUGIN_ENABLED__minecraft_vessel` (no `MINECRAFT_BRIDGE_ENABLED` key). Minecraft keys: `MINECRAFT_BRIDGE_RUN_AT_START` (False — optional boot pre-warm; the bridge otherwise starts **on demand** when the connector connects, i.e. when Synth enters the world), `MINECRAFT_BRIDGE_HOST` (127.0.0.1, advanced), `MINECRAFT_BRIDGE_PORT` (8137, advanced), `MINECRAFT_SERVER_HOST` (127.0.0.1), `MINECRAFT_SERVER_PORT` (44383), `MINECRAFT_BOT_USERNAME_OVERRIDE` (empty, advanced — falls back to `SYNTH_NAME`), and the skin keys `MINECRAFT_SKIN_FILE` (empty — file upload in the plugin card, served over HTTP)/`MINECRAFT_SKIN_MODEL` (classic — `select` dropdown, `classic`/`slim`)/`MINECRAFT_SKIN_PUBLIC_BASE_URL` (empty, advanced — public base URL the MC server uses to fetch the skin; when empty it is auto-derived from the WebUI host, substituting the machine's primary LAN IP for a loopback host via `_detect_lan_ip()` so a same-LAN server can reach it out of the box; set explicitly for a VPN/public/reverse-proxy address)/`MINECRAFT_SKIN_COMMAND_TEMPLATES` (empty, advanced — newline-separated list of chat commands tried in order at spawn; empty tries both built-in provider syntaxes)/`MINECRAFT_SKIN_COMMAND_TEMPLATE` (empty, advanced — legacy single-command override).

**Minecraft skin (offline-mode caveat).** A real client-side skin *upload* is impossible for an offline-mode Mineflayer bot — the skin is decided server-side (username/UUID or a skin-management plugin/mod), and Mineflayer only exposes read-only skin data + cape/sleeve visibility toggles, never the texture. The supported path is a **server-side skin provider**; two are supported out of the box: the classic **SkinsRestorer** Bukkit/Spigot plugin (`/skin url <url>`) and the **SkinRestorer** Fabric/Forge/NeoForge/Quilt mod by Lionarius (`/skin set web <model> "<url>"` — the URL **must** be double-quoted). The skin PNG is **uploaded directly** from the plugin card (`MINECRAFT_SKIN_FILE`, a `register_exposed_var(..., ui_type="file")` upload) and served by SyntH at `<base>/api/config/MINECRAFT_SKIN_FILE/file` — where `<base>` is `MINECRAFT_SKIN_PUBLIC_BASE_URL` if set, else auto-derived from the WebUI host/port (the MC server must be able to reach it). Because providers use different syntaxes, the connector's `_apply_skin()` **tries every configured command in turn** at spawn — the server accepts the one it understands and ignores the rest, so both providers work with **no keyword logic**. Resolution order (first non-empty wins): `MINECRAFT_SKIN_COMMAND_TEMPLATES` (newline-separated list) → the legacy `MINECRAFT_SKIN_COMMAND_TEMPLATE` (single) → the built-in defaults covering both providers (`/skin set web {model} "{url}"` then `/skin url {url}`); each substitutes `{url}` and `{model}`. Commands are forwarded to the bridge's `skin` action (`bot.chat`). Fast-Lane connector action (no `external_effects`), best-effort — if `MINECRAFT_SKIN_FILE` is empty no command is sent, and a failed/ignored command never breaks the session.

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

### Vessel: Synth ignores a player who addresses her in-world  <!-- 2026-07-26 -->
**Symptom:** A player writes to Synth in the Minecraft world (e.g. `XargonWan: Rekku, mi leggi?`); the chat is correctly polled by the connector and enqueued (`skip_mention_check=True`, mention/salience bypass passes) and `vessel_minecraft_say` is in the scoped allowlist — yet Synth returns `{"actions": []}` and never replies.
**Location:** `interface/vessel_interface.py` (`_maybe_run_will_beat`, `on_world_event`), `core/vessel_beat.py` (`build_will_prompt`).
**Status:** fixed.
**Notes:** The turn that actually ran was the autonomous **will beat**, whose prompt is framed as *"a quiet moment to reflect while you play… on your own"* (`build_will_prompt`). Because the will beat fires on its own timer with no awareness that a player just addressed Synth, its solitary "you are alone" framing made the LLM keep its goal and reply with no actions — the player's chat sat only in scrollback history, never as the turn's primary input. Fix: track the monotonic time of the last salient **player-originated** chat (structural: `event_type == "chat"` with an `entity`/actor, never keyword matching) in `on_world_event`, and defer the will beat in `_maybe_run_will_beat` while a player has been active within `VESSEL_WILL_QUIET_SEC` (new config, default 60 s, `0` disables). The still-enqueued player chat is then processed as an ordinary reactive turn instead of being coincident with the volition beat. New pure helper `core.vessel_beat.resolve_will_quiet_sec` (unit-tested).

### Vessel: player chat still ignored — coalesced with autonomous perceptions  <!-- 2026-07-27 -->
**Symptom:** Even after the `VESSEL_WILL_QUIET_SEC` fix above (will beat correctly deferred — logs show `Will beat deferred: player active Ns ago`), Synth *still* returned `{"actions": []}` to an in-world chat mentioning her (`XargonWan: Rekku, ci sei?`). The chat was polled, mention-matched, persisted to `vessel/minecraft` history, and enqueued at `HIGH_PRIORITY` — yet never replied to.
**Location:** `core/message_queue.py` (`compact_similar_messages`, `enqueue` item dict), `interface/vessel_interface.py` (`_enqueue_perception`).
**Status:** fixed.
**Notes:** Deeper, distinct root cause from the will-beat entry above. `compact_similar_messages` coalesces every queued item sharing the same `chat_id` (here `vessel/minecraft`, 600 s window, limit 5) into one turn, using the *earliest* item as the merge base and its text as `original_user_message`. The player chat was continuously merged with autonomous **sightings** ("You notice a block nearby: cyan_bed …") and/or a **will-beat** prompt, so the reflective/solitary framing dominated the turn and the buried player chat never became the primary user message — proven by log counts: `original_user_message` containing the will-beat phrase = 252, containing the player's chat text = 0. Fix: a structural `no_compact` opt-out. `enqueue` reads `getattr(message, "_no_compact", False)` onto `item["no_compact"]`; `compact_similar_messages` returns `[first]` immediately when the base is `no_compact` and skips absorbing any `no_compact` item into another base. `interface/vessel_interface.py::_enqueue_perception` sets `wrapped._no_compact = True` for a salient player chat (structural: `event_type == "chat"` and `actor` present — never keyword matching). A human directly addressing Synth in-world therefore always runs as its own reactive turn with the chat as `original_user_message`, earning a `vessel_minecraft_say` reply. Autonomous perceptions (sightings, will beats) still coalesce normally.

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
| `minecraft_goals` | `plugins/rift_vessel/minecraft/goals.py` / `init-db.sql` | Minecraft goal store — Synth's own free-text goals per session (`session_id`, `description`, `note`, `status`, `created_at`, `updated_at`); no catalogue, persists self-authored goals across beats |
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
| `VESSEL_CORTEX` | LLM engine used for Rift Vessel will beats (the slow volition turn where Synth authors its in-world goals). `Default` means Base Cortex. Only the Will beat uses the LLM — the Motor tick is reflex-only and never routed here. |
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
| `VESSEL_AUTONOMY_ENABLED` | Enable autonomous Rift Vessel play — the slow will beat, the middle action beat, and the fast motor tick (default `False`) |
| `VESSEL_WILL_INTERVAL_SEC` | Seconds between slow volition/will beats — the LLM turn that authors/updates Synth's goal (default 45, clamped `[10, 3600]`, falls back to legacy `VESSEL_BEAT_INTERVAL_SEC`) |
| `VESSEL_WILL_QUIET_SEC` | Quiet window (s) required before a will beat may fire after a *player* interacts with Synth in-world (default 60, clamped `[0, 3600]`, `0` disables). Defers the "reflect on your own" volition turn while a player is present so a direct address is answered reactively instead of being ignored |
| `VESSEL_ACTION_BEAT_ENABLED` | Enable the middle "idea → concrete step" action beat — the LLM Fast-Lane turn that maps the free-text goal to one concrete world verb (default `True`) |
| `VESSEL_ACTION_INTERVAL_SEC` | Seconds between action beats (default 20, clamped `[3, 300]`) |
| `VESSEL_MOTOR_ENABLED` | Enable the fast motorics reflex that moves the body toward the goal with no LLM (default `True`) |
| `VESSEL_MOTOR_INTERVAL_SEC` | Seconds between fast motor ticks (default 3, clamped `[1, 60]`) |
| `VESSEL_SELF_PRESERVATION_ENABLED` | Enable the fast self-preservation survival reflex on the motor tick (default `True`) |
| `VESSEL_SP_LOW_OXYGEN` | Oxygen threshold at/below which the drowning reflex surfaces the body (default 6). **On the 0..20 air-bubble scale** (mineflayer `oxygenLevel` at runtime: 20 = full lungs, 0 = out of air), NOT air ticks — a healthy submerged bot reads ~20, so an air-ticks value would false-fire constantly |
| `VESSEL_SP_LOW_HEALTH` | Health at/below which a hostile encounter escalates from defend to flee (default 6) |
| `VESSEL_SP_HOSTILE_DIST` | Distance (blocks) within which a hostile mob triggers the defend/flee reflex (default 8) |
| `VESSEL_SP_FIGHT_BACK` | Whether Synth fights a nearby hostile (`attack`) before escalating to flee (default `True`) |
| `VESSEL_SP_FIGHT_MAX_FAILS` | Consecutive failed fight attempts before escalating from defend to flee (default 3) |
| `VESSEL_PERCEPTION_CONTEXT_CAP` | Max autonomous vessel perceptions merged into a vessel-focus prompt (default 3). Perceptions live in a SEPARATE in-memory ring buffer (`_perception_memory`, maxlen 32) so a rapid ambient burst (e.g. drowning damage) can never evict player chat from the bounded conversational deque; the prompt merges conversation + the last N perceptions chronologically |
| `VESSEL_KNOWLEDGE_LIVE_FETCH` | Allow the Minecraft KB to fetch + LLM-summarise pages from the live [minecraft.wiki](https://minecraft.wiki) (default `True`). When `False` the client is cache-only everywhere (no network, no LLM) — it serves only already-cached pages |
| `VESSEL_KNOWLEDGE_FETCH_TIMEOUT_SEC` | HTTP timeout (s) for a live minecraft.wiki search/fetch (default 4, clamp 1–30). Only the explicit `lookup_knowledge` verb / goal-expansion Drone fetch live; the automatic will/motor beat path is `cache_only` and never hits the network |
| `VESSEL_KNOWLEDGE_SUMMARY_MAX_CHARS` | Max length of the one-time EN factual summary the LLM writes per wiki page before it is cached to `wiki/cache/<slug>.json` (default 600, clamp 120–4000) |
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
