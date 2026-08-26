# AGENTS.md — Synthetic Heart (SyntH)

> Repository-wide operating rules for coding agents.
> Detailed architecture and subsystem documentation lives in `docs/wiki/` and `docs/`.
> Claude Code may also load `CLAUDE.md`; repository rules in this file still apply.

---

## 1. Project and Primary Invariant

**Synthetic Heart** (**SyntH**) is a modular AI persona system. “Synth” is the digital person implemented by this repository.

The architecture is intentionally detachable:

- `core/` owns the message chain, validation, dispatch, persistence, routing, and shared services.
- `plugins/` add actions and optional behavior.
- `engines/` provide interchangeable AI/media backends.
- `interface/` connects external systems to the core chain.

**Primary invariant:** removing an optional plugin, engine, connector, or interface must not break the remaining system.

---

## 2. Sources and Reading Strategy

Before making non-trivial changes, read only the material relevant to the task in this order:

1. `AGENTS.md`
2. `AGENT_WORK.md`, when present
3. Relevant pages under `docs/wiki/` and maintained documentation under `docs/`
4. Source code and tests
5. `CHANGELOG.md` and established known-issue records when debugging regressions

The Qoder wiki export has two complementary trees:

- `docs/wiki/en/content/` contains reader-facing architecture, development, API, and operations pages. Start here for orientation.
- `docs/wiki/knowledge/en/_index.yaml` maps source paths to generated subsystem modules under `docs/wiki/knowledge/en/`. Use those modules for focused implementation detail.
- `docs/` contains the maintained Sphinx documentation and remains authoritative for published user/developer guidance.

Search by subsystem or source path and read the smallest useful set of pages. Do not recursively ingest the export. Treat generated wiki content as a navigation aid: it may lag the implementation or contain exporter-specific links and structure.

Documentation is a map, not proof. When documentation and implementation disagree:

1. inspect the current code and tests;
2. establish intended behavior from evidence;
3. report the discrepancy;
4. update stale documentation as part of the change when appropriate.

Never preserve an incorrect implementation solely because an old document describes it.

---

## 3. Non-Negotiable Architecture Rules

### One message chain

- All incoming messages enter the core-managed message chain.
- Actions attach to the existing chain; do not create parallel message flows.
- Interfaces must not bypass core validation, dispatch, history, or safety.
- Shared behavior belongs in the core only when it is broadly applicable.
- World-, engine-, plugin-, and interface-specific behavior stays in its adapter.

### Dynamic optional components

- Actions are discovered through `get_supported_actions()`.
- Validation derives from each action schema.
- Missing or disabled optional components must fail closed and degrade gracefully.
- Avoid eager imports that make optional components mandatory.
- Guard optional integrations so import, startup, and shutdown remain safe.

### No keyword-driven product logic

Do not implement routing, intent detection, salience, autonomy, or feature activation primarily through words, phrases, regex triggers, or language-specific keyword lists.

Prefer structural signals such as:

- action schemas;
- typed metadata;
- interface and session state;
- registry membership;
- enums and capability declarations;
- numeric telemetry;
- explicit configuration;
- model reasoning where semantic interpretation is required.

Small syntax parsers and user-declared commands are exceptions only when the feature is explicitly command-oriented.

### Cross-platform baseline

Linux containers are the primary runtime.

Platform-specific behavior must be:

- secondary rather than the main path;
- isolated;
- guarded with capability or platform checks;
- covered by a safe fallback.

---

## 4. Component Contracts

### Plugins

Plugins subclass `PluginBase` or `AIPluginBase` and expose actions through:

```python
def get_supported_actions(self) -> dict:
    """Return supported actions and their prompt/validation schema."""
```

Rules:

- Multi-file plugins live in `plugins/<name>/`.
- Keep implementation, `guide.md`, and `icon.<ext>` together.
- `guide.md` is the documentation source of truth for that component.
- Preserve historical import paths with the repository’s established package shim when moving a flat module into a package.
- Critical message-chain plugins must declare that runtime disabling is not allowed.
- Third-party logos require explicit permission and attribution in `LICENSE_EXTERNAL.md`; otherwise use an original glyph or the SyntH fallback.

Use `plugins/radio_host/` and the relevant wiki pages as reference implementations.

### Interfaces

Interfaces are duck-typed and register at import time.

Rules:

- Every inbound message enters the core chain.
- Shared avatar/audio state is driven through the Karada state server, never by iterating individual WebUI clients.
- Interface-native delivery and shared avatar state are separate concerns.
- Multi-file interfaces use `interface/<module>/<module>.py`, `__init__.py`, `guide.md`, and optional `icon.<ext>`.
- Preserve existing import paths with the established module-rebinding shim.
- Outbound local files must use the shared sandbox path checks in `core/outbound_file_utils.py`.

### Engines and media subsystems

- Text reasoning engines subclass `AIPluginBase`.
- Media engines follow their registry/base-class contracts.
- Current named subsystems are Cortex, Vox, Auris, and Iris.
- Do not revive obsolete paths such as `cortex/` or `llm_engines/`.
- Keep endpoint-specific workarounds on the SyntH side unless the task explicitly authorizes changes to the external engine.

### Agentic Runtime and MCP

Synth runtime MCP and developer MCP are separate systems:

- Synth runtime: `config/synth_mcp.json` and `core/mcp_bridge/`
- Developer tooling: `.mcp.json`, `.vscode/mcp.json`, and `mcp_servers/`

Never merge their configuration or lifecycle.

Registered actions share the tool/action abstraction and must pass through the same safety gate. External effects determine Agent-Lane routing. Drones are single-level sub-agents and must never spawn other Drones.

### Rift Vessel

The Rift Vessel has strict boundaries:

1. Vessel actions do not create Agent-Lane tasks or Drones.
2. A session writes one autobiographical diary entry at session end, not continuously.
3. Vessel activity has its own history voice and persistence.
4. Core Vessel verbs are world-agnostic; game-specific verbs belong in that world connector.
5. Autonomous goals are free-text and personality-driven, never a fixed quest catalogue.
6. Fast motor and survival reflexes use structural world state, not free-text goal parsing or keywords.
7. Player conversation and autonomous perceptions remain separate context buffers.

Before changing Vessel routing, autonomy, motorics, goals, session lifecycle, or message compaction, read the generated `Rift Vessel Embodiment Core` module under `docs/wiki/knowledge/en/` and any relevant source-facing documentation.

### Karada avatar state

`KaradaStateServer` is the source of truth for animation, expressions, face state, and shared speaking/audio state.

- Drive the server, not individual clients.
- Use logical animation states, never hard-coded animation paths.
- New clients implement and register a `KaradaTransport`.
- Interface-specific audio delivery must not duplicate shared avatar broadcasts.

---

## 5. Security and Data Rules

- Never commit credentials, API keys, session cookies, access tokens, private certificates, or passwords.
- Repository documentation may describe where credentials belong, but must use placeholders.
- User secrets belong in environment variables or user-owned config files outside the repository.
- Never print secrets from environment files, databases, logs, or connector configuration.
- Do not weaken action safety or sandbox path checks to make a test pass.
- Shell execution on a bare host remains disabled unless the user explicitly enables the existing guarded override.
- Do not access paths outside declared sandbox roots.
- Treat logs, model prompts, chat history, diary content, and uploaded files as potentially sensitive.

### Database naming

Never use SQL reserved words as bare column names.

In particular, do not create a column named `timestamp`. Use names such as:

- `created_at`
- `updated_at`
- `event_timestamp`
- `started_at`
- `ended_at`

Public API keys may still be named `"timestamp"` when required for compatibility; the restriction applies to SQL identifiers.

---

## 6. Development Workflow

### Initial workspace setup
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
message → **Fast Lane** (unchanged path). Gated by the single authoritative
agent toggle `AGENT_ENABLED` (the user-facing on/off switch). When the agent is
enabled the router is active; when disabled, every turn stays on the Fast Lane.

**Tools are actions.** Internal actions and remote MCP tools are unified in
`ToolRegistry`. Every tool — internal or external — funnels through
`core.action_safety.is_action_allowed_for_execution`. Internal tools dispatch
via `run_action`; external MCP tools via `mcp_client_bridge.call_tool`. Tool
names are namespaced `mcp_<server>_<tool>`.

**Config keys:** `AGENT_ENABLED`, `AGENT_MAX_ITERATIONS` (30),
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

1. **Vessel actions never create agentic tasks.** The embodiment verbs — the world-agnostic **core set** `connect`/`disconnect`/`say`/`move`/`look`/`observe`/`use`/`attack`/`follow`/`unfollow`/`respawn`/`status` (`say` accepts an optional `audio` flag mirroring the interface audio-message flag — it falls back to plain text in worlds with no voice channel; `observe` is a universal self-awareness verb that reads the current `WorldState` and reports nearby affordances/entities/blocks in character — it powers autonomous play; `attack` is a dedicated hostile verb kept out of `use`; `follow`/`unfollow` are universal entity-following verbs that fail cleanly when there is nothing to follow; `respawn` is a universal come-back-to-life verb that no-ops in worlds with no death/respawn concept or when already alive) — declare **no** `external_effects` → they stay on the Fast Lane (`run_actions`), never the Agent Lane / Drones. (They are still passively auto-exposed as MCP tools.) **Connection-driven action exposure.** The Vessel action set is *not* static — it mirrors the live connection state because `get_supported_actions()` is a pure read the core calls on every prompt build/dispatch/validation, so the exposed verbs change automatically on the next prompt. **Disconnected:** a single `vessel_connect` is exposed; its required `game` field is an enum of every *enabled* world (`_enabled_worlds()`, i.e. worlds whose `<world>_vessel` sub-plugin is on), plus optional `host`/`port` overriding the server address for that connect only — no gameplay verbs are visible. **Connected to world W:** the core set (minus `connect`) plus W's `get_world_actions()` extras appear namespaced `vessel_<W>_<verb>` (e.g. `vessel_minecraft_say`) alongside `vessel_disconnect`, and `vessel_connect` disappears. **Logout / inactivity cooldown** (`VESSEL_SESSION_COOLDOWN_SEC`) → the session closes, `has_active_session()` goes false, and on the next prompt the gameplay verbs vanish and `vessel_connect` returns — free, driven by `close_expired_sessions`. **No enabled worlds** → empty set. Detection is entirely structural (no keyword logic): the connected world is `_connected_world()` = `vessel_session_manager.has_active_session()` + the cached connector's `is_connected`; the `game` field is an enum *value*, not text matched. `vessel_connect` accepts the plain form or a legacy `vessel_<world>_connect`, taking the world from `game` first. **Hybrid action ownership.** The core set above is owned by the Vessel and shared by every world (guaranteeing world-agnostic portability); each connector may *additionally* declare its **own** world-specific verbs (e.g. a future Minecraft `craft`/`mine`, Skyrim `cast_spell`/`sneak`) via the optional `VesselConnectorBase.get_world_actions()` hook — returned keyed by bare verb, same schema shape, must NOT declare `external_effects`; the core plugin namespaces them under the same `vessel_<world>_` prefix and dispatches them via `connector.act(verb, payload)`. `plugins/rift_vessel/vessel_plugin.py` implements this in `_ACTION_VERBS` (core set), `_connected_world()` (structural connection probe — `has_active_session()` + cached connector `is_connected`), `_enabled_worlds()`/`_world_enabled()` (worlds whose `<world>_vessel` sub-plugin is on), `_action_world()` (active world resolution), `_world_extra_verbs_for()` (fail-safe pull of the connector's extra verbs, skipping core collisions), `get_supported_actions()` (disconnected → single `vessel_connect{game}`; connected → per-world prefix + both tiers), and `_parse_action_verb()` (accepts both legacy and namespaced forms). The Minecraft connector currently adds no extra verbs (core-set-only). A connector talks to its world directly; no reasoning loop is needed. **Entering/leaving a world is an action, not just an interface hook:** the `connect` verb (exposed as `vessel_<world>_connect`) is the LLM-invokable entry point — it loads the active connector (`ACTIVE_VESSEL`), opens a session on `interface/vessel_interface.py`, and calls `connector.connect(settings, on_event)` wiring the perception callback that forwards world events into the message chain; `vessel_disconnect` calls `connector.disconnect()` and flushes the session(s) to the single end-of-session `vessel_diary` entry (chunked compaction, background) via `end_sessions_for_environment`. Both live in `plugins/rift_vessel/vessel_plugin.py` (`connect_world`/`disconnect_world`). **`vessel_connect` accepts an optional server-address override:** the action declares `optional_fields: ["host", "port"]`, and `connect_world(overrides=...)` merges any non-empty values onto a *copy* of the saved settings before calling the connector (the saved plugin config stays the default; the override applies to that connect only, persistent config is never mutated). The Minecraft connector resolves the target via `_resolve_server_target(settings)` (override else `MINECRAFT_SERVER_HOST`/`MINECRAFT_SERVER_PORT`) and sends it in the bridge `/connect` body; the Node bridge `connectBot(overrides)` / `POST /connect` read `host`/`port` from the body, falling back to their `CFG` defaults.
2. **No diary during a session; end-of-session produces one FACTUAL operational recap in `vessel_diary`, NOT the real `ai_diary`.** Events accumulate in an in-DB `experience_buffer` on `vessel_sessions`, and each is also audited as a row in `vessel_activity_log`. At **end-of-session** a **single** compacted entry is written to the **dedicated `vessel_diary` table** — **never** the shared `ai_diary`. This is deliberate: `ai_diary` keeps **one shared daily row across all interfaces** (the upsert concatenates, with no interface filter), which `get_static_injection` injects into **every** non-vessel Fast-Lane prompt — so writing in-world content there polluted every ordinary chat turn with an ever-growing wall of telemetry until the prompt overran and the LLM failed (SyntH went inactive/T-pose). **Decision A — the recap is operational, not autobiographical.** The old first-person "lived experience" narrative is **removed**: the only compaction product now is a **factual, third-person operational recap** (`reason = "activity_recap"`) built from the session's `vessel_activity_log` rows — concrete/resumable, with coordinates/quantities/state (`[mine] mined stone | block=stone x=12 y=8 z=2`), never *"I explored and felt curious"*. Rationale: this is Synth's working memory of where it was and what it was mid-doing at the next login. **Ownership — a dedicated plugin, not an inline task.** The recap is produced by the **Rift Vessel Compactor** plugin (`plugins/rift_vessel/vessel_compactor/`, a *separate* scope from the Grillo Compactor — it shares only the runnable shape). The plugin registers a compaction handler on the session manager at `start()` (`vessel_session_manager.set_compaction_handler`, mirroring `set_liveness_probe` — core never imports the plugin); on end-of-session the manager calls the handler, which **enqueues** the session id onto the plugin's own **internal, off-chain, low-priority asyncio worker queue** and returns immediately (teardown never blocks). This is **not** the message chain — no in-world turn, no Agent Lane, no Drone. It can also be run manually from the WebUI Plugins tab (runnable quartet → `run_action("compact_now")`). The recap is built by `core/vessel_diary_compactor.py::compact_activity_recap` (source `vessel_activity_log`, chunked by `VESSEL_DIARY_CHUNK_ITEMS`/`VESSEL_DIARY_CHUNK_CHARS`, folded recursively when oversized) on the **vessel-scope Cortex** (`get_active_cortex_engine(scope="vessel")` → `VESSEL_CORTEX`), fully **fail-safe** (any LLM error → deterministic plain-text join; empty log → no entry; never raises). The legacy inline `VesselSessionManager._launch_compaction`/`_compact_and_store` path (autobiographical `compact_session`) survives **only as a fail-safe fallback** when no compaction handler is registered (plugin absent/disabled), still gated by `VESSEL_DIARY_COMPACTION_ENABLED`; the plugin owns its own `VESSEL_COMPACTOR_ENABLED`. `vessel_sessions.diary_entry_id` is always `NULL`. **Deferred (explicit TODO): whether/how to later import a well-formed `vessel_diary` recap into the real `ai_diary`.** End-of-session is reached via explicit logout (`/vessel logout` CLI or the `vessel_disconnect` action), OR `VESSEL_SESSION_COOLDOWN_SEC` (default 3600 s) of inactivity (`close_expired_sessions`), OR the **connection-driven 3-state lifecycle** below. **Connection-driven 3-state session lifecycle.** A session's liveness is tied to the *real* connector, not just a DB row. The interface registers a **liveness probe** into the session manager at `start()` (`get_vessel_session_manager().set_liveness_probe(self._any_connector_live)`) — this keeps `core/vessel_session_manager.py` free of any interface/connector import while letting `has_active_session()` reflect the connector's actual `is_connected` state. The three states: **CONNECTED** — a session id exists *and* the probe returns True → `has_active_session()` True → beats/perceptions run. **RECONNECTING** — the session id still exists but the connector dropped (probe False) → `has_active_session()` reads **False**, so new vessel elements are *frozen* (no beats/perceptions enqueued) while priorities are left untouched; the disconnect sweep retries the connection each tick for up to `VESSEL_DISCONNECT_GRACE_SEC` (default 30 s, clamped 5–3600). A successful reconnect flips the probe True → back to CONNECTED. **ENDED** — the grace window elapsed without recovery → the session is force-closed (`end_sessions_for_environment(reason="disconnected")` → `end_session`), the experience buffer is compacted (chunked, background) into the single `vessel_diary` entry, and **all queued vessel traffic for that world is purged** via `drop_vessel_queue_for_world`, so nothing stale is dispatched into a dead world. The sweep runs **every scheduler tick** (`interface/vessel_interface.py::_close_disconnected_sessions`): it records `_disconnected_since[world]` on first-seen disconnection, calls `_attempt_reconnect(world)` on entry and each in-grace tick, and only ends the session once `now - first_seen >= grace`. Without this, a dropped client would leave the session `active` for the full 3600 s cooldown, generating beats that consume cognition on a dead world. The Minecraft connector detects the drop structurally: `MinecraftConnector._poll_loop` counts consecutive `/events` poll failures and, after `_MAX_POLL_FAILURES` (5, ≈5 s), sets `self._connected = False` so `is_connected` reports the loss.
3. **Own Activities voice.** Like Radio/Grillo: `vessel_activity_log` + `/api/history/vessel` (GET history, DELETE per-item) + a dedicated History sub-tab. The History section actually carries **two** Vessel sub-tabs: the 🌀 **Vessel** tab (actions + session history) and a 🎯 **Goals** tab that, *per game/world*, renders cards showing the goals Synth authored for itself and their steps. The Goals tab is backed by `GET /api/history/vessel/goals` (`core/webui.py::history_vessel_goals`), which iterates the enabled worlds (`_enabled_vessel_worlds()` → `vessel_plugin._enabled_worlds()`), resolves a per-world goal reader (`_resolve_world_goal_reader()`; Minecraft → `plugins/rift_vessel/minecraft/goals.py::list_all_goals`), and returns `{"success": True, "worlds": [{"world", "goals": [...]}]}`. It renders **only** what the model stored (free-text self-authored goals + steps) — no catalogue, no fixed step schema — honouring the spontaneity rule. Frontend: `renderGoalsWorld`/`renderGoalCard` in `res/synth_webui/js/history.js`, styled in `core/webui_templates/sections/history.html`.

**Vessel action whitelist — keep the will/reflection prompt from being erased.** During a vessel turn the full ~60-action global catalog (`base_system≈9.9k` + `catalog_block≈17.6k`) is folded into the **system** prompt, pushing it past the downstream char-budget clamp in `core/external_endpoints/bridges/cortex_bridge.py` (§5). Because that clamp trims only the **user body**, never the system message, the small will/reflection prompt (which lives in the body) gets erased — so SyntH authors goals "blind" and produces trivial material-gathering objectives instead of strategic ones. The fix is a **whitelist** applied *only on vessel turns* that trims the catalog to the actions embodiment actually needs, keeping the system prompt lean enough that the body survives. It is a **3-tier** model implemented in `plugins/rift_vessel/vessel_whitelist.py` (plugin-owned, self-contained — if the Rift Vessel plugin is absent the core falls back to its normal scope-based derive) and wired into `core/prompt_engine.py::build_json_prompt` via `_derive_vessel_whitelist_action_types` (only applied when the derived set is strictly smaller than the full catalog):
   - **Tier 1 — hardcoded, non-editable:** `vessel_*` (the Vessel's own world-agnostic verbs, already namespaced `vessel_<world>_<verb>`). The user can never remove these — they are imperative to embodiment.
   - **Tier 2 — hardcoded, interchangeable per connected world:** `*_<world>_*` (e.g. `*_minecraft_*`), derived **structurally** from the connected world token via `vessel_plugin._action_world()` (never a hardcoded game name), so it swaps automatically when the connected world changes.
   - **Tier 3 — editable:** the `VESSEL_ACTION_WHITELIST` config var (advanced; default `send_message, event, schedule_message, blocklist, spawn_drone`) holds only the optional *core-extra* actions the user may tune (message/event/schedule/blocklist/drone).

   The final allowlist = `hardcoded_vessel_patterns(world)` ∪ `matches_whitelist(available_action, parse_patterns(VESSEL_ACTION_WHITELIST))`, with `_SYSTEM_ONLY_ACTION_NAMES` and non-user-facing actions dropped. Matching is **structural** (`fnmatch.fnmatchcase` on the action *name*), never keyword/regex intent detection — safe in a multi-language deployment. **Global audio/tts trim (companion change).** As part of the same lean-prompt effort, the standalone audio/tts actions `tts_speak` (vox), `audio_telegram_bot`, and `audio_discord_bot` are removed from the exposed catalog **globally** (commented out with `TODO(vessel-whitelist)` markers referencing this section) — audio delivery still works via the `say audio` flag and the interface-native audio paths; restore them by uncommenting the marked blocks. Unit-tested in `tests/test_vessel_whitelist.py`.

**Real-time gaming focus (two enforced behaviours).** Because the Vessel is used mainly in interactive game worlds, embodiment behaves like a person *concentrating on the game*, not multitasking every chat. Both are decided **only from routing metadata, never from message text** (project rule: no keyword logic), and both are lazily imported + fully guarded so removing the Vessel plugin can't break queueing or context assembly.

- **(1) Pure 0–11 numeric priority, no de-prioritisation.** `core/message_queue.py` ranks messages on an absolute urgency scale where **higher = more urgent** (a min-heap keyed by `_heap_key(p) = -int(p)`): `PRIORITY_EMERGENCY = 11`, `PRIORITY_URGENT = 10` (`priority=True`), `PRIORITY_REFLECTION = 9` (Synth's own "pause & reflect on my goal" turn — above player chat so it is consumed before ordinary in-world traffic, yet below urgent/emergency), `PRIORITY_HIGH = 8` (direct human input, e.g. an in-world **player** chat), `PRIORITY_TRAINER = 7`, `PRIORITY_RADIO = 6` (radio-host DJ banter — above ordinary chat so its on-the-fly LLM generation window is never starved), `PRIORITY_GENERAL = 5` (ordinary chat), `PRIORITY_AMBIENT = 4` (Synth's **own** autonomous vessel perceptions/beats — below every human), `PRIORITY_LOW = 3` (background-adjacent — web-search 2nd pass, misc fallback), `PRIORITY_BACKGROUND = 2` (G.R.I.L.L.O. beats — absolute bottom, never starves anything). `enqueue` assigns each message its rung from **structural origin only** (never message text, never conditional on session state): a reflection-pause turn (`_vessel_reflection`) → `PRIORITY_REFLECTION`, a real player chat → `PRIORITY_HIGH`, an autonomous vessel perception/beat → `PRIORITY_AMBIENT`, the trainer → `PRIORITY_TRAINER`, ordinary chat → `PRIORITY_GENERAL`, urgent → `PRIORITY_URGENT`. There is **no de-prioritisation**: ordinary chat is never demoted because a session is active, so a person addressing Synth is always answered promptly while the game's own perceptions simply sit at a lower rung. `EMERGENCY`/`URGENT` were shifted up by one to make room for `REFLECTION`; the low bands were later split by the radio-starvation fix — `RADIO` at 6, `GENERAL` at 5, G.R.I.L.L.O. demoted to the absolute bottom `BACKGROUND` at 2. This replaced the earlier scheme that raised perceptions to `HIGH` and demoted chat to `AGENT_PRIORITY` (which caused chat starvation).
- **(2) Vessel-focus turns get world-scoped context.** `core/history_engine.py::build_context` detects an embodiment turn from routing metadata (`interface_path` starting with `vessel`, a `chat.type == "vessel"` message, or an explicit `vessel_focus` context flag) and forces `unified_mode = False` and disables the global diary/memory injections, keeping only persona/profile + local vessel history. SyntH is not omniscient while playing — it doesn't read other chats in real time or notice unrelated global events mid-session (that catch-up happens in quiet moments and at end-of-session).

Action speed: `vessel_plugin.act()` logs the connector round-trip at `INFO` (`act('...') dispatched via '...' in N ms`). The *decision* to act still costs a full cognition turn; a reflex/attention layer that reacts without a full LLM turn is a documented future phase (must also respect constraint 1).

**Perception & salience:** the filter is LLM-free — dedup (30 s) + rate-limit (2 s) in `interface/vessel_interface.py`. A richer LLM salience/attention worker (Grillo *RAW cognition* style) is a documented future phase and must also respect constraint 1. Never stream raw telemetry into cognition.

**Perceptions never evict player chat (separate context buffer).** Autonomous perceptions (sightings/movement/damage/will-beats) and real conversation are stored in **two separate in-memory windows** in `core/chat_context_manager.py`, keyed purely by a structural metadata flag — never by content. The conversational window is a bounded `deque(maxlen=CHAT_HISTORY_LIMIT)`; a **separate** `_perception_memory` ring (`deque(maxlen=_PERCEPTION_MEMORY_MAXLEN=32)`) holds perceptions. `add_message_to_context` routes a message to `_perception_memory` when `metadata["vessel_perception"]` is set (tagged at ingestion by `interface/vessel_interface.py::on_world_event` as `{"vessel_perception": True, "vessel_event_type": <type>}`, `None` for a player chat), so a rapid ambient burst (e.g. repeated **drowning** damage, which bypasses the 30 s dedup by design) can **never** evict a player's chat from the bounded conversational deque. `load_chat_history` rehydrates the split from the DB `chat_history_cache.metadata JSON` column; `clear_chat_context` clears both. The vessel-focus prompt (`core/history_engine.py`) merges them: it drops autonomous perceptions from the conversational `window`, then appends the most recent `VESSEL_PERCEPTION_CONTEXT_CAP` (default 3) perceptions from `get_perception_memory()`, sorted chronologically — so ambient grounding is still present without starving the conversation. This is the fix for the "Rekku replied with one stock line to every player" bug (§12): the earlier `history_engine` prompt-cap was necessary but insufficient because the `maxlen` deque discards player chat *upstream* of the prompt. Keyword-free; removing the vessel subsystem leaves `_perception_memory` empty.

**En-route element collection (CORE, world-agnostic).** While the body travels A→B it keeps a per-session, world-scoped registry (`VesselInterface._seen_elements: dict["vessel/<world>", set["kind:target"]]`) of everything it has already perceived, so Synth *knows* what it passed and can divert toward something rare/interesting (a quest item), revisit it later, or mention it to other players. After each fast motor tick, `_collect_en_route_sightings(world, world_state)` reads the connector's structural `WorldState.extra["affordances"]` (`{kind, target, verb, distance}`, distance-sorted), keys novelty via `_element_signature` (`kind:target`, `kind` defaults `"thing"`), and surfaces each *first* sighting **once** as a new `sighting` perception through `on_world_event(event_type="sighting", …, data={kind,target,distance})`. The registry records **all** seen elements (recallable via `observe`) while the dedup/rate-limit salience filter paces what reaches cognition (the slow will beat) — so a burst of new blocks never floods the chain; the nearest new element wins each rate-limit window. `end_sessions_for_environment` pops the world's registry so the next session starts fresh. Keyword-free, no LLM, fully guarded (a failure never disrupts the motor tick). Whether a sighting is "rare" or "the item I wanted" is Synth's own cognition-turn judgement, never decided here.

**Autonomous play — three speeds + a reflection pause: volition (slow, LLM) + action (middle, LLM) + motorics (fast, reflex), with a reflection pause (LLM, elevated priority) on top.** By default a session is reactive; when `VESSEL_AUTONOMY_ENABLED` is on, Synth **plays on its own** — wanders, looks around, sets and pursues its own goals, gathers, builds, interacts — while still obeying all three constraints (Fast Lane only, no Agent Lane/Drones, single end-of-session diary). Autonomy is deliberately **split into three independently-paced layers** so that *deciding what to want* (slow, personality-driven) never bottlenecks *deciding the next concrete step* (middle), which never bottlenecks *moving the body* (fast, reactive). The middle **action beat** closes the "walks around but accomplishes nothing" gap: the will beat authors a free-text goal but is forbidden to move/act, and the motor tick moves but never reads goal text — so nothing translated *"gather wood"* into the concrete verb `vessel_minecraft_collect_block`/`mine`/`craft`. The action beat is that translator (mapping is cognition's, no keyword logic). On top of these three sits the **reflection pause**: a deliberate stop-and-think turn that fires when Synth is playing *without a real objective* (no goal, or a goal with no step plan), prunes its own pending autonomous beats, and dedicates one elevated-priority cognition turn to authoring/refining the goal before ordinary autonomy resumes.

- **Will beat — volition (slow, LLM).** Mirrors G.R.I.L.L.O.: the interface scheduler (`interface/vessel_interface.py`, 10 s tick) fires a **will beat** every `VESSEL_WILL_INTERVAL_SEC` s (default 45, falls back to the legacy `VESSEL_BEAT_INTERVAL_SEC`, clamped `[10, 3600]`) via `_maybe_run_will_beat` while a session is active. The beat reads the live connector's `WorldState`, builds a **structural, keyword-free** volition prompt with `core/vessel_beat.py::build_will_prompt` (surfacing position/health/time/entities/blocks/inventory/affordances/current+recent goals straight from the `WorldState` contract, and framing the turn as *will, not motion* — "your body will move toward it on its own"), and enqueues it as a **normal** `vessel` message (`chat.type == "vessel"`, `interface_path` `vessel/<world>`) so `build_context` applies world-scoped context and the core runs **one ordinary Fast-Lane cognition turn** in which Synth writes/keeps/updates a free-text goal via `vessel_<world>_set_goal`/`vessel_<world>_update_goal`. This is where Synth's **will and memories** live — the goal is authored from personality, not a script. The will beat is FORBIDDEN to move or act. `build_decision_prompt` remains a backward-compat alias of `build_will_prompt`.
- **Action beat — "idea → concrete step" (middle, LLM).** A second, faster LLM beat fires every `VESSEL_ACTION_INTERVAL_SEC` s (default 20, clamped `[3, 300]`, gated by `VESSEL_ACTION_BEAT_ENABLED`, default True) via `_maybe_run_action_beat`. Built by `core/vessel_beat.py::build_action_prompt` (returns `""` — no beat — when there is no active goal), it frames the turn as *"a moment to actually do something toward your goal"* and asks for exactly **one** concrete step: Synth picks a world verb (`vessel_<world>_collect_block`/`mine`/`craft`/`smelt`/`place`/`goto`/`say`) and may record progress via `vessel_<world>_update_goal` with `advance=true`. Enqueued as an ordinary Fast-Lane `vessel` message like the will beat; the player-quiet deferral (`VESSEL_WILL_QUIET_SEC`) applies so a player addressing Synth in-world is answered reactively, not overridden. Respects all three constraints (Fast Lane, no Agent Lane/Drones, no mid-session diary).
- **Motor tick — motorics (fast, no LLM).** A separate, much faster loop moves the body toward the current goal with **no prompt, no cognition turn, no diary**. The scheduler calls `_maybe_run_motor_tick` every `VESSEL_MOTOR_INTERVAL_SEC` s (default 3, clamped `[1, 60]`, gated by `VESSEL_MOTOR_ENABLED`, default True) while a session is active; it fetches the active connector and current goal and calls `await connector.motor_step(goal)` **directly** — never enqueuing a message. `motor_step` is a pure reflex over the **structural affordance contract only** (`{kind, target, verb, distance}`, distance-sorted): it picks the nearest benign affordance (verb `use`/`mine`, hostile `attack` skipped), then `mine`s a block or `use`s an entity within `_MOTOR_REACH` (3.0 m), else `goto`s it, else `wander`s. The goal's **already-validated structural fields** (`target_kind`/`target_name`, populated by cognition — never free text) may steer *where* the body walks: when the goal names a **block** target and that exact block is a live affordance within reach, the reflex `mine`s it (returning `{"action": "mine", "target": …, "target_kind": "block"}`) instead of standing next to it re-issuing `goto` — the "walks up but never picks anything up" gap; entities are never mined. It still **never reads the goal's free text**. **Deliberate-action deferral:** cognition-driven actions are dispatched through `VesselConnectorBase.act_deliberate` (wired in `vessel_plugin.act`; duck-typed connectors fall back to plain `act`), which marks the body busy for the whole dispatch — so `motor_step` yields (returns `{"acted": False, "reason": "deliberate_action_in_flight"}`) instead of re-issuing its own `goto`/`mine` mid-action and stomping the pathfinder goal of a long-running verb like `collect_block` (the observed live "The goal was changed before it could be completed!" abort). The survival guard still runs **before** this check, so danger pre-empts deliberation, and the staticity ward stays frozen while the body is deliberately working. The base `VesselConnectorBase.motor_step` is a no-op returning `{"acted": False, "reason": "no_motorics"}`, so a world without motorics degrades gracefully. This fulfils the "reflex/attention layer that reacts without a full LLM turn" anticipated above, and still respects constraint 1 (no agentic task, no mid-session diary). **Structured inventory:** `get_world_state` also aggregates the raw stack list into an id→total map exposed as `WorldState.extra["inventory_counts"]` (via `MinecraftConnector._inventory_counts`, fail-safe, keyword-free) so cognition can judge how many of a thing it still needs without rescanning. **Staticity ward — always-on relocate-when-parked guard (Minecraft adapter scope).** The tick-to-tick physical-motion watchdog (`_stuck_position_ticks`/`_STUCK_MOVE_EPS`/`_STUCK_POSITION_TICKS`) only runs *after* the `if not goal` early-return and only while the motor is actively driving, so a **goalless** body — or one endlessly `mine`/`use`-ing an in-reach block without displacing — can stay pinned forever. The ward closes that gap: `MinecraftConnector._update_staticity_ward(position)` (gated by `VESSEL_STATICITY_WARD_ENABLED`, default True) runs **first** each motor tick, right after the survival guard and *before* the `no goal` early-return. It tracks a moving anchor (`_static_anchor` x/z) + an idle counter (`_static_ward_ticks`): while the body stays within `_static_ward_radius` (`VESSEL_STATICITY_RADIUS`, default 2.0, clamp `[0.5, 32.0]`) of the anchor it accrues ticks; stepping outside re-anchors and zeroes the counter (so genuine travel never trips it). After `_static_ward_limit` (`VESSEL_STATICITY_TICKS`, default 8 ≈ 24 s at the ~3 s tick, clamp `[2, 1000]`) consecutive idle ticks it fires: `motor_step` rotates `_explore_heading` by `_EXPLORE_TURN_RAD` and `goto`s a fresh reprojected waypoint (fallback `wander` when reprojection is unavailable), returning `{"acted": True, "action": "goto"|"wander", "reason": "staticity_ward"}`, then re-arms the anchor on the current spot. Purely positional/numeric (never reads goal text or keywords), fully fail-safe (a bad/missing position resets tracking and never fires), Fast Lane only (no LLM, no diary). Unit-tested in `tests/test_vessel_minecraft_motor.py`.

- **Reflection pause — deliberate stop-and-think (LLM, elevated priority).** Sits *above* the three speeds. Because the single message consumer can be blocked for a long time by a slow uncancellable Base-Cortex turn, will beats may pile up unconsumed and Synth can end up aimlessly wandering with no goal. The reflection pause addresses the queue *ordering* of that situation: the scheduler calls `_maybe_run_reflection` (in `interface/vessel_interface.py`) **before** the will/action beats each tick; when a session is active, autonomy + reflection are enabled (`VESSEL_AUTONOMY_ENABLED`, `VESSEL_REFLECTION_ENABLED`, default True), a player has been quiet for `VESSEL_WILL_QUIET_SEC`, the anti-thrash floor `VESSEL_REFLECTION_MIN_INTERVAL_SEC` (default 60, clamped `[10, 3600]`) has elapsed, and Synth has **no active goal or a goal with no step plan** (structural check via `_goal_from_world_state`/`_goal_needs_expansion` — never message text), it: (1) builds a structural prompt with `core/vessel_beat.py::build_reflection_prompt` framed as an intentional private pause that must NOT speak (no `say`), only author/refine the goal via `vessel_<world>_set_goal`/`vessel_<world>_update_goal`; (2) enqueues it as a `vessel` message tagged `_vessel_reflection` → `PRIORITY_REFLECTION` (9, above player chat), whose enqueue path **prunes older pending autonomous vessel beats** for that world (`core/message_queue.py::_supersede_pending_vessel_beats` — it keeps player chat and `no_compact` items; it never uses `drop_vessel_queue_for_world`, which would also drop player chat); (3) sets a `_reflecting`/`_reflecting_until` window of `VESSEL_REFLECTION_DURATION_SEC` (default 15, clamped `[3, 300]`) during which the will and action **beats** are held off (but the **motor tick and survival reflex keep running — the body still moves**); (4) on expiry, resets `_last_will_beat_at = 0.0` so the will beat re-fires immediately and resumes normal autonomy on the freshly-committed goal. **Known tension (by design):** clearing the queue removes only *pending* items — a reflection turn still cannot run until the in-flight (possibly slow selenium) turn drains, so this fixes queue *order*, not consumer *starvation*. It **complements** the goal-expander Drone (both kept). Fully guarded/keyword-free; the pure prompt + config helpers (`build_reflection_prompt`, `is_reflection_enabled`, `resolve_reflection_duration`, `resolve_reflection_min_interval`) are unit-tested in `tests/test_vessel_beat.py`, and the `PRIORITY_REFLECTION` band ordering in `tests/test_vessel_realtime.py`.

- **Goal beat — dedicated goal-setting turn (LLM, restricted allowlist).** Complements the reflection pause. The reflection turn runs with the **full** vessel action catalog, so a weak model (e.g. `harmonyai/qwen-35-9b`) can — and in practice does — fall back to a passive `observe`/`status` instead of authoring a goal, leaving Synth aimless (histogram proof: an entire session of only `observe`/`status`, zero `set_goal`). The goal beat closes that gap **structurally**, not by prompt pressure: the scheduler calls `_maybe_run_goal_beat` (in `interface/vessel_interface.py`) each tick; when a session is active, autonomy + the goal beat are enabled (`VESSEL_AUTONOMY_ENABLED`, `VESSEL_GOAL_BEAT_ENABLED`, default True), a player has been quiet for `VESSEL_WILL_QUIET_SEC`, no reflection window is open, the `VESSEL_GOAL_BEAT_INTERVAL_SEC` floor (default 45, clamped `[10, 3600]`) has elapsed, and Synth has **no active goal** (structural check via `_goal_from_world_state` — never message text), it: (1) builds a structural prompt with `core/vessel_beat.py::build_goal_prompt` — same persona/system prompt and in-character *private planning* framing as the will/reflection beats (must NOT speak, no `say`), asking Synth to author a single concrete goal; (2) enqueues it as a `vessel` message reusing the `_vessel_reflection` band (`PRIORITY_REFLECTION`, `no_compact`, same pruning of older pending autonomous beats) but with `event_type="goal"` and — critically — a **per-turn restricted action allowlist** passed via `context_memory["allowed_action_types"] = {vessel_<world>_set_goal, vessel_<world>_update_goal}` (the exact mechanism Grillo uses in `plugins/grillo/grillo_impl.py`; `core/prompt_engine.py::build_json_prompt` reads it and it takes precedence over the vessel whitelist for that turn). Because `set_goal`/`update_goal` are the **only** exposed actions, even a weak tool-caller has nothing passive to fall back to — it *must* emit a goal. Fast Lane only (no `external_effects` → never Agent Lane/Drones, no mid-session diary), keyword-free, world-agnostic (uses the `world` arg for the verb namespace). Runtime-verified: after deploy, a goal beat at 16:56:41 was immediately followed by an executed `vessel_minecraft_set_goal` at 16:57:13 and a new `status=active` row in the `goals` table. The pure prompt + config helpers (`build_goal_prompt`, `is_goal_beat_enabled`, `resolve_goal_beat_interval`) are unit-tested in `tests/test_vessel_beat.py`.

- **Self-preservation guard — survival reflex (fast, no LLM).** Evaluated **first** on every motor tick, before the no-goal early return, so Synth reacts to danger even with no active goal. `MinecraftConnector._survival_threat(state)` classifies threats from **numeric telemetry + game enum ids only** (never user text) in strict priority: **dead** → `respawn`; **drowning** (head submerged — liquid block id at head or `is_in_water` — AND `oxygen <= _sp_low_oxygen`) → `goto_surface` (reuses `goto` toward `y + _SURFACE_CLIMB_BLOCKS`=8, no new bridge verb); **burning** (feet/head on a hot block id: `lava`/`flowing_lava`/`fire`/`soul_fire`/`magma_block`) → `flee`; **aggressive combat** (see below) → **defend** or **escalate to flee**. `get_world_state().extra` is enriched with `oxygen`/`is_in_water`/`is_alive`/`block_feet`/`block_head`/`health`/`threat`/`threat_reason` plus the combat fields `has_ranged_weapon`/`ranged_ammo`/`best_melee_damage`/`damage_taken`/`damage_from_player`; the Node bridge `worldSnapshot` supplies the raw fields and tags nearby entities with a structural `hostile` flag and a per-entity `is_targeting_me` aggro flag. **Fight all aggressors, not just the nearest, and pre-emptively.** The combat branch calls `MinecraftConnector._aggressive_targets(state, near_dist)` — every aggressive mob, nearest-first, that either has the `hostile` flag within `_sp_hostile_dist` **or** is `is_targeting_me` at **any** distance (so a skeleton shooting from far, or a mob that has already locked aggro before closing in, is engaged pre-emptively). **Players are never a reflex target** (`kind == "player"` is skipped — a human hit is a social matter, handled by the appraisal beat below). The nearest aggressor is latched as `_fight_target`; on target change `_fight_fail_count` resets. **Weapon selection is structural.** While `VESSEL_SP_FIGHT_BACK` is on AND `health > _sp_low_health` (health is the *primary* escalation driver) AND `_fight_fail_count < _sp_fight_max_fails`: if `VESSEL_SP_USE_RANGED` AND `has_ranged_weapon` (bow/crossbow with ammo) AND the target distance `>= VESSEL_SP_RANGED_MIN_DIST` (5.0) → **ranged** (verb `shoot`); otherwise → **melee** (verb `attack`), which equips the highest-damage weapon carried via the bridge's `bestMeleeWeapon()` and swings a short burst. Below the health floor, or once the fail cap is hit, → **escalate to flee**. **Hunger** is handled OUTSIDE the guard by the `mineflayer-auto-eat` bridge plugin (auto `require`+`loadPlugin` in `minecraft_bridge.js`) — no manual verb, no motor branch, no config key. `core/vessel_beat.py::build_will_prompt` appends a structural threat cue so the slow will beat knows a reflex just fired. Config (component `vessel_plugin`): `VESSEL_SELF_PRESERVATION_ENABLED` (True), `VESSEL_SP_LOW_OXYGEN` (**6 — 0..20 bubble scale, see gotcha**), `VESSEL_SP_LOW_HEALTH` (6), `VESSEL_SP_HOSTILE_DIST` (8), `VESSEL_SP_FIGHT_BACK` (True), `VESSEL_SP_FIGHT_MAX_FAILS` (**8** — health-primary escalation, so the body keeps fighting while healthy), `VESSEL_SP_USE_RANGED` (True), `VESSEL_SP_RANGED_MIN_DIST` (5.0), `VESSEL_SP_APPRAISAL_ENABLED` (True), `VESSEL_SP_ENGAGE_RATIO` (1.0, clamp 0.2–5.0), `VESSEL_SP_WEAK_MOB_POWER` (6.0). **GOTCHA: at RUNTIME mineflayer `bot.oxygenLevel` reports the vanilla 0..20 air-bubble scale (20 = full lungs, 0 = out of air), NOT air ticks** — validated live: a healthy submerged bot reads ~20, so the drowning threshold MUST be on the 0..20 scale (default 6 ≈ two bubbles left). An air-ticks threshold (e.g. 200) would fire the drowning reflex constantly (false positive) because the runtime value never approaches it. This respects constraint 1 (pure motor reflex, no agentic task, no mid-session diary). Reference studied: mindcraft-bots/mindcraft (reimplemented natively; mindcraft never touched). Unit-tested in `tests/test_vessel_survival.py`. **Night shelter (Minecraft adapter scope).** A new priority-6 threat (below drowning/burning/combat, above "no threat"): when it is **not day** (`is_day` False — structural, from the world time telemetry) AND there are aggressive mobs within `_sp_shelter_dist` (`VESSEL_SP_SHELTER_DIST`, default 16.0 — deliberately wider than the melee `_HOSTILE_NEAR_DIST`=8.0 via the class const `_SHELTER_HOSTILE_DIST`=16.0, so the body walls itself in *before* the mob closes to melee), `_survival_threat` returns `{"threat": "night_shelter", "verb": "shelter", …}` and latches `_sheltered_last_day`. **A torch is not enough — Synth must fully enclose.** The bridge `shelter` verb (`minecraft_bridge.js`) tries, in order: (1) find a nearby **bed with a roof above it** (`findBlock` `_bed`, maxDistance 12, roof check), pathfind (`GoalNear`) and `bot.sleep` → `method: "bed"`; else (2) **seal** the ~10 open cells around the body by placing blocks → `method: "seal"`; else (3) **dig-in** a 1×2 niche as a last resort → `method: "dig_in"`; returns `{ok: enclosed, data: {method, sealed, dug_in, open_cells}}`. `_run_survival_guard` dispatches `verb == "shelter"` via `await self.act("shelter", payload)` and sets a 2-tick cooldown so it doesn't thrash. Gated by `VESSEL_SP_NIGHT_SHELTER` (default True). **Re-strategy on death (bridge = Minecraft adapter scope; will cue = Rift Vessel core scope).** A respawning Synth used to resume the exact same fatal goal and walk straight back into what killed it → infinite death loop (confirmed live: spawned at night, disarmed, surrounded, died, respawned, repeated). Fix (mindcraft parallel — save last death position + inject a "reconsider" message): the bridge `death` handler now increments `deathCount`, records the **numeric** `lastDeath = {x, y, z, count, at}` (rounded coords; never expires until overwritten), and `worldSnapshot` exposes it as `last_death`; `MinecraftConnector.get_world_state` copies it into `WorldState.extra["last_death"]` (null on an older bridge). `core/vessel_beat.py::build_will_prompt` (Rift Vessel **core**) then emits a strong, **purely structural** in-character cue — only when `last_death` is a dict with numeric `x`/`z` — telling Synth it *died at (x, y, z)* (with a `— this is death #N in this world` suffix when `count` is present) and pressing it to **reconsider**: pick a safer goal, move away first by setting `destination_x`/`destination_z` well clear of the death spot, or make survival itself the goal. It never parses any text. Unit-tested in `tests/test_vessel_beat.py` (death cue) and `tests/test_vessel_survival.py` (shelter reflex). **Power-aware fight-vs-flee (Minecraft adapter scope).** The decision to engage a mob is no longer a flat "always defend while healthy" — it is a **structural power comparison** so a disarmed Synth flees a mob it cannot win, while an armed/armored one engages. Two numeric helpers on the connector (keyword-free, telemetry-only) drive it: `_own_power(extra)` = `offense * survivability`, where `offense` = `best_melee_damage` (bare-hand floor `1.0` when carrying no weapon) and `survivability` = `1.0 + armor_points/20 + health/40`; `_mob_power(entity)` = `max_health * (1 + attack_damage/8)`, using the per-entity `max_health`/`attack_damage` the Node bridge attaches via `mobCombatStats()` (falling back to `_DEFAULT_MOB_POWER`=12.0 when a mob's stats are unknown). `armor_points` comes from the bridge's `armorPoints()` (summed `_ARMOR_DEFENSE` per equipped piece) and is forwarded into `WorldState.extra`. The gate: `ratio = own_power / mob_power`; if the body is **disarmed** (`_is_disarmed` — no melee weapon *and* no ranged weapon) it only engages a **weak** mob (`_mob_power < VESSEL_SP_WEAK_MOB_POWER`, default 6.0), otherwise `power_ok = ratio >= VESSEL_SP_ENGAGE_RATIO` (default 1.0). `power_ok` (plus `fight_back`, `health > low_health`, and the fail-cap) decides defend/shoot vs flee; every combat reason dict carries `own_power`/`mob_power`/`ratio` for debugging. **Per-mob strategy override (§17, Rift Vessel *core* mechanism + Minecraft *content*).** Before the power gate runs, `_survival_threat` calls `apply_combat_strategy(ENVIRONMENT, target, extra)`; a non-`None` result short-circuits the reflex with a mob-specific tactic. The **mechanism** is the world-agnostic core module `plugins/rift_vessel/vessel_combat_strategy.py` (mirrors `core/vessel_registry.py`: a `CombatStrategyRegistry` keyed `{world: {entity_id: strategy}}`, module-level singleton `combat_strategy_registry`, and wrappers `register_combat_strategy`/`resolve_combat_strategy`/`apply_combat_strategy` — fail-safe, resolves by the entity's structural `name` id, never display text). The **content** lives in the Minecraft adapter: `_mc_strategy_creeper` and `_mc_strategy_enderman` both return a `keep_distance` plan (a creeper must never be chased into its explosion; an enderman is disengaged rather than meleed), registered at import via `register_combat_strategy("minecraft", "creeper"/"enderman", …)`. A generic mob has no registered strategy → `apply_combat_strategy` returns `None` → the power gate decides. Unit-tested in `tests/test_vessel_survival.py`. **Morning surface-exit (Minecraft adapter scope).** A new lowest-priority threat (#7, below night-shelter): if Synth dug in / walled itself in overnight (a bunker with no real base) it must climb back to daylight in the morning instead of staying buried. `_survival_threat` reads the structural telemetry `is_day` (bool) and `sky_access` (bool — the Node bridge `hasOpenSkyAbove(maxUp)` scans `dy=2..24` for open sky, exposed as `sky_access` in `worldSnapshot`). At **day** with **no** open sky above (`is_day is True and sky_access is False`), and only when a `_surfaced_last_day` day-latch has not already fired, it returns `{"threat": "morning_exit", "verb": "climb_staircase"}`. The bridge verb `climb_staircase` (`minecraft_bridge.js`) digs a **jumpable ascending staircase** — one block forward + one block up per step, placing the tread — via direct dig/place (no pathfinder), stopping early once `skyClear()` reports open sky; returns `{ok, data:{steps, climbed, reached_sky, used, start, end}}`. The async `_run_survival_guard` gates the actual climb on `_has_reachable_base(state)`: if a registered base is within `_base_retreat_radius`, it just sets `_surfaced_last_day=True` and skips (a real base already has an exit); otherwise it dispatches `climb_staircase` (`reason="survival:morning_exit"`). The day-latch (`_surfaced_last_day`) prevents refiring the same day and re-arms at night. Gated by `VESSEL_MORNING_EXIT_ENABLED` (default True). Structural only (numeric time + sky-access bool, never text), Fast Lane, no diary. Unit-tested in `tests/test_vessel_survival.py`. **Prefab closed-house build (Minecraft adapter scope).** The `build_base` verb builds a small, fully-enclosed hollow-cube shelter (walls + roof + floor + one door gap + interior torch + crafting table, optional bed) from a deterministic, inventory-aware layout — a *model/reference*, not a scripted quest (spontaneity rule preserved). The layout recipe is `plugins/rift_vessel/minecraft/base_spec.py::derive_base_layout(origin, inventory_counts)` (pure, bounded, structural id-only, fail-open): it emits the shell **bottom-up — floor → walls → roof** so every cell has a solid neighbour already placed to click against (the roof, placed last, anchors onto the finished wall tops). The bridge `build_base` case then runs the shell placement plus a bounded **seal pass** (max 3 idempotent re-attempt rounds over the cells that failed with `no-solid-face` on the first pass, no-progress early-bail) so edge/corner/roof cells that were floating in air the first time get closed once the rest of the shell exists — the fix for the earlier "house was not closed" bug. A material shortfall surfaces structurally as `ok=False` + `missing=["<item>:need N (have M)"]` rather than dispatching an unbuildable plan. Unit-tested in `tests/test_base_spec.py`.

- **Damage-appraisal will beat — deliberate reaction to being hurt (LLM, elevated priority).** The survival reflex handles the *fast* motor response to a hit; on top of it a high-priority **appraisal will beat** lets Synth *think about* the hit in character. The structural trigger is **taking damage this tick** (not merely "a hostile is near"): `MinecraftConnector.get_world_state` computes `damage_taken` as the drop in `health` since the previous snapshot (numeric-only, no keyword logic) and surfaces it in `WorldState.extra["damage_taken"]` — so an unseen/ranged attacker or a trap also fires the beat, not just a visible mob. **The delta is single-read:** the baseline advances on *every* `get_world_state` call, so only the first reader per tick sees a given hit — therefore `interface/vessel_interface.py::_maybe_run_damage_appraisal` runs **first** in the scheduler autonomy checks (before reflection/will/action beats). When `damage_taken > 0` and `VESSEL_SP_APPRAISAL_ENABLED` (default True) it builds `core/vessel_beat.py::build_damage_appraisal_prompt(world_state, world)` and enqueues it as an ordinary Fast-Lane `vessel` message tagged `_vessel_appraisal` → **`PRIORITY_URGENT`** (via `core/message_queue.py`, which also supersedes older pending autonomous vessel beats for that world) and `no_compact` (so it never coalesces with sightings/will beats). Anti-thrash: at most one appraisal per `resolve_will_interval` window. **Player vs mob framing (structural).** The Node bridge attributes each hit with a time-boxed `lastDamage` (`DAMAGE_ATTRIBUTION_WINDOW_MS=2500`): `worldSnapshot` exposes `damage_from_player` = true only when the last hit's source was a **player** entity, null when stale/environmental. `build_damage_appraisal_prompt` branches on `extra["damage_from_player"]`: a **player** hit gets a *social* framing (do NOT reflexively swing back; consider `vessel_<world>_say`, ask/back off/remember), a **mob** hit gets a *combat* framing that offers `vessel_<world>_attack` and — when `has_ranged_weapon` and `ranged_ammo > 0` — `vessel_<world>_shoot`. It renders health, damage magnitude, nearby entities/affordances/inventory, `best_melee_damage` (or "bare hands"), and the ranged-ready state, plus the KB `knowledge` block. Fast-Lane only (no `external_effects` → never Agent Lane/Drones, no mid-session diary). Unit-tested in `tests/test_vessel_beat.py` (prompt) and `tests/test_vessel_survival.py` (combat reflex).

`core/vessel_beat.py` is pure/side-effect-free (dataclass **or** dict input, fail-safe autonomy gating, interval clamp/failsafe on both `resolve_will_interval` and `resolve_motor_interval`, `is_motor_enabled`) and fully unit-tested (`tests/test_vessel_beat.py`) without DB/bridge/LLM; `MinecraftConnector.motor_step`'s structural rules are unit-tested in `tests/test_vessel_minecraft_motor.py`. **Generic self-awareness** is the `observe` core verb (reads `WorldState`, reports affordances/entities/blocks in character). **Affordances** follow a generic structural contract `{kind, target, verb, distance}` built by the connector from the raw snapshot — never keyword matching — so both the volition prompt and the motor reflex stay world-agnostic. **What to play is world-specific, but the goal STORE is generic.** Goals now live in a standalone **generic Goals plugin** (`plugins/goals/goals.py`) — a *scope-aware* store usable by any game world, a general planner, or the Synth pursuing a personal life goal, **not** just Minecraft. Every goal set is isolated by a three-part **scope tuple** (`scope`/`game`/`world`): Minecraft goals are pinned `scope="vessel"`/`game="minecraft"`/`world="none"`; a personal goal uses `scope="none"`. The store does **not** ship a catalogue, templates, prerequisites, or inventory-count progression (a fixed quest menu would make every Synth play identically, like a scripted bot); it only *persists* and *recalls* the free-text goals Synth writes for itself, and progress is judged by Synth from what it perceives, never by an item counter. A **stepped goal auto-completes** when `current_step` advances past the last step (the fix for goals never being marked `done` once all their steps were finished); a stepless goal is only completed explicitly. Goals are kept in the `goals` table (legacy `minecraft_goals` renamed + scope-backfilled by `core/migrations.py::_migrate_goals_table`) so a goal survives across beats within a session. The Minecraft connector reaches the store through a thin compatibility shim (`plugins/rift_vessel/minecraft/goals.py`) that forwards every call with the Minecraft scope tuple pinned, keeping the historical `mc_goals.<fn>(...)` surface intact. The connector exposes extra verbs via `get_world_actions()` — bridge-backed `goto`/`scan`/`mine`/`place`/`inventory`/`wander` plus the goal-store verbs `set_goal`/`goals`/`update_goal` (namespaced `vessel_minecraft_*`) — and enriches `WorldState.extra` with `current_goal`/`recent_goals`. For non-vessel use the generic plugin additionally exposes `goal_set`/`goal_update`/`goal_list` (security `low`, no `external_effects`). Synth authors its own goals (`vessel_minecraft_set_goal`, required free-text `description` + optional `note`) during a beat — *"diamonds now or build a chest first?"* is its call, driven by its personality and wants, not a script. All autonomy wiring is lazily imported and guarded: removing the beat module, the Goals plugin, or disabling the flag never breaks the reactive Vessel (the shim degrades to a no-op "no goal").

**Core + attachable world sub-plugins (Grillo-style).** The Rift Vessel mirrors G.R.I.L.L.O.'s shape: a **core** plugin plus **attachable** per-world sub-plugins. `vessel_plugin` is the core — it owns the generic `vessel_*` actions and the **global** config (`ACTIVE_VESSEL`, `VESSEL_SETTINGS`, `VESSEL_SESSION_COOLDOWN_SEC`, all under component `vessel_plugin`). Each world is its own attachable sub-plugin with a **separate** WebUI banner and config namespace, so world-specific options are never conflated with the global entity. **WebUI coherence LED (orange).** A world sub-plugin can be enabled while the core `vessel_plugin` is disabled — a state in which that world can never actually connect. To flag this incoherence, the classic WebUI Plugins tab (`core/webui.py`) shows an **orange** status dot on any `Vessels`-category world sub-plugin whose LED would otherwise be green when `vessel_plugin` is not loaded (with a tooltip explaining the world can't connect until the Rift Vessel plugin is enabled). Enabling the core plugin restores the normal green/grey LED.

**Layout (folder-per-plugin, see §4).** The Rift Vessel core lives in `plugins/rift_vessel/` (`vessel_plugin.py` + `vessel_base.py` + `icon.svg` + `guide.md` + an empty `__init__.py`; the module `vessel_plugin` differs from the folder name so no `sys.modules` shim is needed). Each world gets its own sub-folder `plugins/rift_vessel/<world>/` (`<world>.py` + `icon.svg` + `guide.md` + empty `__init__.py`). `derive_plugin_category` maps the `rift_vessel` path token to the **Vessels** category; both `vessel_plugin` and each world sub-plugin also declare `category: "Vessels"` explicitly.

**A world module ships BOTH a connector and an attachable sub-plugin.** `plugins/rift_vessel/minecraft/minecraft.py` exposes both module-level classes: `CONNECTOR_CLASS = MinecraftConnector` (self-registers on `VESSEL_REGISTRY` at import — the actual world driver) **and** `PLUGIN_CLASS = MinecraftVesselPlugin` (a thin, action-less `PluginBase` that gives Minecraft its own WebUI banner and owns the Minecraft-specific config under component `minecraft_vessel`: `MINECRAFT_BRIDGE_RUN_AT_START`/`MINECRAFT_BRIDGE_HOST`/`MINECRAFT_BRIDGE_PORT`, `MINECRAFT_SERVER_*`, `MINECRAFT_BOT_USERNAME_OVERRIDE`, `MINECRAFT_SKIN_*` — the bridge enable state is the plugin toggle itself, `PLUGIN_ENABLED__minecraft_vessel`). The world sub-plugin's `get_supported_actions()` returns `{}` — the generic `vessel_*` actions stay in the core. This is the Grillo model applied to worlds; a world without a `PLUGIN_CLASS` would still register as a connector but would have no separate banner/config.

**Adding a world:** create `plugins/rift_vessel/<world>/<world>.py` with (1) a `VesselConnectorBase` subclass + module-level `CONNECTOR_CLASS` calling `register_vessel_connector(name, __name__, capabilities=..., label=...)` at import, and (2) a thin `PluginBase` subclass + module-level `PLUGIN_CLASS` that calls `register_plugin("<world>_vessel", self)` in `__init__`, declares its config under component `<world>_vessel`, returns `category: "Vessels"` from `get_metadata()`, and returns `{}` from `get_supported_actions()`. The world automatically gets the Vessel's core action set exposed as `vessel_<world>_<verb>`; to add **world-specific** verbs, override `get_world_actions()` on the connector (return a `{verb: schema}` mapping keyed by bare verb — same schema shape as `get_supported_actions`, **no** `external_effects` — the core plugin namespaces and dispatches them via `connector.act`). Removing any connector/sub-plugin/core/interface must not break the rest of the system.

**Scope rule (where a feature belongs).** When deciding whether a capability lives in the Rift Vessel **core** or in a **world adapter/plugin**, apply this rule: *if the feature is common to the great majority of games/worlds, its scope is the Rift Vessel core (a generic `vessel_*` verb on `vessel_base`/`vessel_plugin`); if it is specific to one game, it belongs to that game's adapter/plugin* (a world-specific verb via `get_world_actions()` on the connector, namespaced `vessel_<world>_<verb>`). E.g. move/look/observe/goals are core; **crafting is a Minecraft-specific verb** and therefore lives on the Minecraft connector, not the core.

**Spontaneity rule (autonomous play is not hard-coded).** Autonomous play must be **spontaneous and human-like, never a hard-coded/scripted quest list**. Synth chooses *for itself* — out of its personality and mood — what to do in a world; the code owns only lifecycle/persistence, never the *content* of goals or a fixed catalogue of objectives. `plugins/rift_vessel/minecraft/goals.py` embodies this: it persists free-text, self-authored goals in the generic `goals` table (via the Goals plugin) and judges nothing — there is no `gather_wood`/`find_diamonds` template and no auto-progress counter. Affordances are structural (`{kind, target, verb, distance}`, distance-sorted) and are **never** matched by name keywords. Two different Synths, or the same Synth on two different days, should set completely different goals.

**Minecraft world-specific verbs.** Beyond the core set, the Minecraft connector adds world-specific verbs via `get_world_actions()`, including **`craft`** (`vessel_minecraft_craft`): required `item` (lowercase Minecraft item id, e.g. `oak_planks`, `stick`, `crafting_table`, `wooden_pickaxe`), optional `count` (clamped `[1, 64]`), `search_radius`, `timeout_ms`; `security_level: "low"`, **no** `external_effects`. The bridge resolves the recipe via `bot.recipesFor()`/`bot.craft()`, auto-locates a nearby `crafting_table` and pathfinds to it when a 3×3 recipe requires one, and returns a structural fail-safe result (`ok:false` with a clear reason) when materials or a reachable table are missing — no keyword logic. The generic `bed` name is accepted and resolved to the first dyed variant (`white_bed`, `red_bed`, … — modern ids have no bare `bed`), and the resolved id is returned so cognition learns the exact item. The `status`/`scan` world snapshot also exposes `game_mode` (e.g. `creative`/`survival`) so cognition can reason about world limitations (e.g. in `creative` mined blocks yield no drops, a vanilla behaviour, not a bug).

**Game knowledge base (reference, not a script).** A Synth that doesn't know a world's *rules* plays badly — e.g. it mines iron ore bare-handed and gets nothing, never having learned iron needs at least a stone pickaxe. Each world may ship a small **knowledge base (KB)**: the *mechanism* is world-agnostic (the Vessel core renders whatever facts a world supplies) and the *content* is world-specific (the Minecraft adapter owns its facts). The KB is strictly **reference** — it states how the world works, never what to do — so the spontaneity rule (self-authored goals, no catalogue) is preserved. **World-agnostic mechanism + per-game sources.** The KB *mechanism* lives in the core module `plugins/rift_vessel/knowledge_client.py` (`knowledge_client`) — search/cache/summarise/web-fallback are all world-agnostic and driven entirely by adapter-supplied descriptors. A world declares its own knowledge source(s) via `WikiSource` descriptors returned from the connector's `get_knowledge_wiki_sources()` hook (`plugins/rift_vessel/vessel_base.py` returns `[]` by default). A `WikiSource` carries only structural, game-specific data: `name`, `api_url` (MediaWiki `api.php`, or `""` for a web-only world), `page_url` (page-link prefix), `user_agent`, `game` (name substituted into the default summary prompt), and an optional full `summary_prompt` override. **No wiki endpoint is hardcoded in core code** — `knowledge_client.lookup(cache_dir, sources, query, limit, *, cache_only=False)` takes the sources as a parameter. **Local-first precedence** (`TODO - Rift Vessel.md` §9): (1) **local cache** first — offline-safe and instant, and the only tier consulted when `cache_only` is set; (2) **per-game wiki(s)** in declared order — each page fetched, summarised once, cached; (3) **generic web search** as a last resort *only when no declared wiki matched* — reusing `plugins/web_search/search_engine.py::collect_valid_results`, gated by `VESSEL_KNOWLEDGE_WEB_FALLBACK` (default True), results summarised + cached exactly like a wiki page. Every tier writes back to `cache_dir` keyed by a slug of the title (`{title, url, raw_extract, summary, fetched_at}`), so a fact is fetched at most once; each note is `{title, text, url}`; the whole thing is fail-safe (offline / any error → returns whatever is cached, never raises). Matching is keyword-free/structural: the `query` is whitespace-joined game tokens (a goal `target_name`, block/item ids), matched against page-title slugs. **Minecraft adapter:** `plugins/rift_vessel/minecraft/wiki_client.py` is a thin shim that declares the Minecraft `WikiSource` (the **live [minecraft.wiki](https://minecraft.wiki)** MediaWiki API, no auth) and delegates to the core client; `MinecraftConnector.get_knowledge_wiki_sources()` returns it, and the cache lives at `plugins/rift_vessel/minecraft/wiki/cache/<slug>.json`. **Verb:** the connector exposes a Fast-Lane, `external_effects`-free `lookup_knowledge` (`vessel_minecraft_lookup_knowledge`, `required_fields: ["query"]`, `optional_fields: ["limit"]`, `security_level: "low"`); `MinecraftConnector.lookup_knowledge(query, limit=5, *, cache_only=False)` delegates to `wiki_client.lookup` (→ core client) and returns notes as `{title, text, url}`. **Beat vs verb split (important):** the automatic will/motor-beat path (`_resolve_knowledge`) calls the lookup with **`cache_only=True`** so a `WorldState` build never blocks on the network or the LLM — it serves only already-cached pages; the **explicit** `lookup_knowledge` verb and the goal-expansion Drone use the default live path (`cache_only=False`), which is allowed to fetch + summarise. Everything is best-effort/fail-safe: offline or on any error the client returns whatever it has cached (possibly empty) and never raises, so a Fast-Lane beat can't break. Config: `VESSEL_KNOWLEDGE_LIVE_FETCH` (bool, default True — disables all network, cache-only everywhere), `VESSEL_KNOWLEDGE_FETCH_TIMEOUT_SEC` (int, default 4, clamp 1–30), `VESSEL_KNOWLEDGE_SUMMARY_MAX_CHARS` (int, default 600, clamp 120–4000). Fully offline-testable with the live API + LLM mocked (`tests/test_vessel_knowledge.py`). **Prompt injection:** when a beat's `WorldState.extra["knowledge"]` is populated, `core/vessel_beat.py::_fmt_knowledge` renders it into both the will and action prompts as a bulleted **"Game knowledge"** block headed by an explicit *reference, not a script* framing (purely structural — never inspects fact text for keywords — and drops the block when nothing renderable survives). **Drone goal expansion:** when Synth authors a *new* goal, a Drone (single-level ephemeral sub-agent, §5b) can expand it into ordered sub-steps by consulting the KB via `lookup_knowledge` — turning *"get some iron"* into *"craft a wooden pickaxe → mine stone → craft a stone pickaxe → mine iron ore"* (the mapping is the Drone's reasoning, no fixed table, no keyword routing). **After the goal is updated with its sub-steps it is re-notified to Synth via a will beat**, so the next volition turn acts on the freshly-expanded plan. The WebUI Goals sub-tab renders the sub-steps **collapsed by default** (a `<details>` disclosure labelled `Plan · done/total steps`, styled in `core/webui_templates/sections/history.html`, built in `res/synth_webui/js/history.js::renderGoalCard`).

**Minecraft deployment:** single-container, gated by the Minecraft Vessel plugin's own enable toggle (`PLUGIN_ENABLED__minecraft_vessel`) — there is no separate `MINECRAFT_BRIDGE_ENABLED` key; enable/disable the connector from its WebUI plugin card. Node.js is **baked into the Docker image by default** (Dockerfile `ARG INSTALL_NODE=true` + conditional NodeSource install), so the Minecraft Vessel works out of the box in Docker with no extra build flags — only non-Docker / bare-metal deployments need to install Node themselves, or opt out of the baked-in Node with `docker build --build-arg INSTALL_NODE=false …`. The provisioner runs the bridge as a **non-root** subprocess and returns a clear error if `node`/`npm` are missing. Uses offline auth; real Microsoft/XBL auth is out of scope. **The bridge's Node runtime is self-contained inside the plugin package.** `interface/minecraft_provisioner.py::BridgeProvisioner` keeps the whole runtime under `plugins/rift_vessel/minecraft/mineflayer/` (constant `_BRIDGE_RUNTIME_SUBDIR = "mineflayer"`): a **committed** `package.json` pins the deps (`mineflayer`, `mineflayer-pathfinder`, `minecraft-data`), and `node_modules/`/`bridge.json`/`bridge.log` are per-run artefacts (gitignored, installed at first run via `npm install` into that folder, or pre-bundled in the shipped zip for a fully offline package). The bridge *script* `minecraft_bridge.js` stays in the plugin folder one level up and is executed with `NODE_PATH` prepended with `mineflayer/node_modules`, so `require('mineflayer')` resolves against the package's own modules regardless of the script's location. The whole `plugins/rift_vessel/minecraft/` tree is therefore self-sufficient and zip-shippable — nothing lives under a shared `/opt` path that a container recreate would lose. Tests override the location with an explicit constructor `bridge_root` (or env `MINECRAFT_BRIDGE_ROOT`). **Bridge memory footprint — pin the render distance or Node OOM-crashes in ~2 min.** mineflayer caches every chunk the server streams, so on a normal render distance the Node old-space grows unbounded toward its ~4 GB default heap limit; observed live: RSS climbs to ~3.9 GB then the process dies with `FATAL ERROR: Ineffective mark-compacts near heap limit — JavaScript heap out of memory` (via `node::OOMErrorHandler`) at ~137 s, killing the session shortly after a successful connect. Two guards prevent this: (1) `minecraft_bridge.js` passes `viewDistance: 'tiny'` in the `createBot` options (the bot does not need to see far to play, so the smallest view keeps the chunk cache tiny — root fix); (2) `interface/minecraft_provisioner.py::_bridge_env()` appends `--max-old-space-size=512` to `NODE_OPTIONS` on the bridge subprocess (safety belt — forces aggressive GC well before the 4 GB limit). Validated: with both guards the bridge holds ~195 MB RSS and stays alive indefinitely while playing autonomously. A successful `/connect` is NOT proof of a durable session — poll `/health` over several minutes to confirm the bridge survives.

**Vessel config keys:** `ACTIVE_VESSEL` (`"disabled"`), `VESSEL_SETTINGS`, `VESSEL_SESSION_COOLDOWN_SEC` (3600), `VESSEL_AUTONOMY_ENABLED` (False — enable autonomous play, both layers), `VESSEL_WILL_INTERVAL_SEC` (45, clamped `[10, 3600]`, falls back to the legacy `VESSEL_BEAT_INTERVAL_SEC` — seconds between slow volition/will beats), `VESSEL_WILL_QUIET_SEC` (60, clamped `[0, 3600]`, `0` disables — quiet window a player interaction must have elapsed before the will beat may fire, so a directly-addressing player is answered reactively rather than ignored by the "on your own" volition prompt), `VESSEL_MOTOR_ENABLED` (True — enable the fast motorics reflex), `VESSEL_MOTOR_INTERVAL_SEC` (3, clamped `[1, 60]` — seconds between fast motor ticks that move the body with no LLM). The Minecraft connector is enabled/disabled via its plugin toggle `PLUGIN_ENABLED__minecraft_vessel` (no `MINECRAFT_BRIDGE_ENABLED` key). Minecraft keys: `MINECRAFT_BRIDGE_RUN_AT_START` (False — optional boot pre-warm; the bridge otherwise starts **on demand** when the connector connects, i.e. when Synth enters the world), `MINECRAFT_BRIDGE_HOST` (127.0.0.1, advanced), `MINECRAFT_BRIDGE_PORT` (8137, advanced), `MINECRAFT_SERVER_HOST` (127.0.0.1), `MINECRAFT_SERVER_PORT` (44383), `MINECRAFT_BOT_USERNAME_OVERRIDE` (empty, advanced — falls back to `SYNTH_NAME`), and the skin keys `MINECRAFT_SKIN_FILE` (empty — file upload in the plugin card, served over HTTP)/`MINECRAFT_SKIN_MODEL` (classic — `select` dropdown, `classic`/`slim`)/`MINECRAFT_SKIN_PUBLIC_BASE_URL` (empty, advanced — public base URL the MC server uses to fetch the skin; when empty it is auto-derived from the WebUI host, substituting the machine's primary LAN IP for a loopback host via `_detect_lan_ip()` so a same-LAN server can reach it out of the box; set explicitly for a VPN/public/reverse-proxy address)/`MINECRAFT_SKIN_COMMAND_TEMPLATES` (empty, advanced — newline-separated list of chat commands tried in order at spawn; empty tries both built-in provider syntaxes)/`MINECRAFT_SKIN_COMMAND_TEMPLATE` (empty, advanced — legacy single-command override)/`MINECRAFT_KNOWN_PLAYERS` (empty JSON map — in-world player username → identity label, e.g. `{"remuraine": "Scar - your papa"}`; rendered next to the username in every world-state prompt and sighting so the vessel never guesses who a nearby player is).

**Minecraft skin (offline-mode caveat).** A real client-side skin *upload* is impossible for an offline-mode Mineflayer bot — the skin is decided server-side (username/UUID or a skin-management plugin/mod), and Mineflayer only exposes read-only skin data + cape/sleeve visibility toggles, never the texture. The supported path is a **server-side skin provider**; two are supported out of the box: the classic **SkinsRestorer** Bukkit/Spigot plugin (`/skin url <url>`) and the **SkinRestorer** Fabric/Forge/NeoForge/Quilt mod by Lionarius (`/skin set web <model> "<url>"` — the URL **must** be double-quoted). The skin PNG is **uploaded directly** from the plugin card (`MINECRAFT_SKIN_FILE`, a `register_exposed_var(..., ui_type="file")` upload) and served by SyntH at `<base>/api/config/MINECRAFT_SKIN_FILE/file` — where `<base>` is `MINECRAFT_SKIN_PUBLIC_BASE_URL` if set, else auto-derived from the WebUI host/port (the MC server must be able to reach it). Because providers use different syntaxes, the connector's `_apply_skin()` **tries every configured command in turn** at spawn — the server accepts the one it understands and ignores the rest, so both providers work with **no keyword logic**. Resolution order (first non-empty wins): `MINECRAFT_SKIN_COMMAND_TEMPLATES` (newline-separated list) → the legacy `MINECRAFT_SKIN_COMMAND_TEMPLATE` (single) → the built-in defaults covering both providers (`/skin set web {model} "{url}"` then `/skin url {url}`); each substitutes `{url}` and `{model}`. Commands are forwarded to the bridge's `skin` action (`bot.chat`). Fast-Lane connector action (no `external_effects`), best-effort — if `MINECRAFT_SKIN_FILE` is empty no command is sent, and a failed/ignored command never breaks the session.

### Working autonomously on the Vessel (agent-mode rules)

When tasked with Vessel work, an agent is expected to work **autonomously and
end-to-end** — the human may step away from the computer for long stretches.
Operate under these rules:

- **Iterate freely.** Make changes, re-iterate, rebuild the container when
  needed, and test on your own. Do not stop after a single attempt; keep
  refining until the acceptance criteria are met (still honouring the 2-attempt
  limit *per identical error* — a repeated identical failure means escalate, not
  a hard cap on total iterations).
- **Drive Synth in-world for testing.** You may ask Synth to join a world (do it
  via the OpenAI-compatible API, see §9), read the logs, and modify the code.
- **You may join the game world yourself** with a separate Mineflayer instance
  named **`CoachAgent`** to observe/interact while Synth plays.
- **Be honest and open.** If anyone in-world asks who you are, answer truthfully
  that you are a coding agent and be transparent about what you are doing — no
  secrets.
- **Every acceptance criterion carries its own scope.** Tag each as
  **Minecraft-specific** (adapter) or **Rift Vessel core** (usable across all
  worlds), per the Scope rule above (a capability common to most games belongs
  in the core; a game-specific mechanic belongs in that game's adapter/plugin).
- **Do not change any engine** (e.g. cortex) while doing Vessel work.

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

### Unified outbound messaging (`send_message`)

Every chat interface exposes **one** LLM-facing delivery action: `send_message`
(schema owned by `core/message_registry.py`, dispatched by
`core/action_parser.py::_dispatch_send_message`). The per-interface legacy
`message_*` / `send_file_*` / `audio_*` actions are removed — do not write new
plugins using them.

Payload contract:

- `text` — message body; also the caption for media. `media` — sandbox file
  path(s), auto-detected image/video/audio/document via
  `core/outbound_file_utils.py` (`resolve_safe_outbound_path`,
  `classify_media`, `guess_mime_type`).
- At least one of `text`/`media` is required (OR validation via the
  `one_of_groups` extension on `ValidationRule` in `core/validation_registry.py`;
  read from action schemas by `core/component_auto_registration.py`).
- `interface_path` is **conditional**: optional when replying to an incoming
  message (`original_message.interface_path` is the fallback), required for
  spontaneous sends.
- Optional `send_as_voice` (TTS voice note; routed through Vox at dispatch) and
  `reply_to` (unified reply id mapped to the interface-native field).

Capabilities: interfaces declare what they can deliver via a `get_capabilities()`
hook or method presence (`core/interface_capabilities.py`). The set is stored at
registration time in `InterfaceRegistry` (`register_interface_capabilities`).
Requested features an interface does not support are dropped with a log warning,
stripped from the payload before delivery, and reported back to the model as
structured `capability_drops` (`{"feature", "reason", "interface"}`,
`core/capability_drops.py`) so Synth can acknowledge the limitation in its own
words on the next turn — never a canned message, never an error.

**New interface checklist:** implement `async send_message(payload,
original_message=None)` accepting the unified payload above; expose
`send_message` from `get_supported_actions()` via
`message_registry.get_send_message_schema()`; declare capabilities; never emit
per-interface action names.

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
uv sync
```

Use the repository’s configured MCP servers only after their dependencies and credentials are available.

### Developer MCP usage

- Prefer a configured developer MCP server when its tools directly match the task; use targeted MCP results to reduce broad file, log, database, or codebase inspection.
- At the start of relevant work, verify that the required MCP server and tool are available in the current agent session. Configuration on disk alone does not prove that the session loaded them.
- If a required or explicitly requested MCP server or tool is unavailable, notify the user promptly, name what is missing, and state the affected workflow. Do not silently substitute a slower or less precise path.
- After notifying the user, use a safe local fallback when one exists and the request does not require that specific MCP server. If no suitable fallback exists, stop and request direction.
- Do not call MCP tools merely because they are available; select them when they are relevant and likely to return narrower, more useful context than general-purpose inspection.

Never use `pip install` or create an ad-hoc virtual environment. Dependency changes go through `uv` so the lockfile remains authoritative.

### Test environment

Run Python tests through the repository-managed environment. Prefer `uv run pytest`;
when invoking the interpreter directly, use `.venv/Scripts/python.exe` on Windows or
`.venv/bin/python` on Linux/macOS. Do not use bare `python -m pytest` or `pytest`,
because the system Python may not have the test dependencies installed and can produce
a misleading "pytest is not available" result.

### Process cleanup and `uv run` network fallbacks

- **Kill the main process when you are done with it.** Any Synth instance you start
  (`main.py`, smoke runs, `--help` probes) must be stopped before you finish, or it
  will keep consuming the DB pool, message queue, and LLM endpoints indefinitely.
  On Windows: `Get-CimInstance Win32_Process -Filter "Name = 'python.exe'"` to find
  it by command line (match `main.py`), then `Stop-Process -Id <pid> -Force`.
  Do not kill unrelated long-lived infra (the `mcp_servers/*.py` processes, the
  `tencentdb_knowledge_mcp.py` launcher) unless asked.
- **`uv run main.py` may fail on a transient network error fetching the
  `kittentts` GitHub wheel** (e.g. `http2 error ... refused stream`). The venv is
  already synced — the failure is only `uv` re-resolving metadata. Use
  `uv run --offline main.py` or `uv run --no-sync main.py` (or run
  `.venv/Scripts/python.exe main.py` directly) instead of retrying the fetch.

### Before editing

1. Read the relevant wiki/docs and nearby tests.
2. Inspect repository status and existing uncommitted work.
3. Trace the current execution path.
4. Use TencentDB `code_impact` for symbols you plan to modify materially when the MCP tool and code graph are available.
5. Identify the smallest safe change and its validation plan.

Do not overwrite unrelated work or “clean up” files outside the task.

### While editing

- Keep changes scoped.
- Follow existing naming, architecture, and error-handling patterns.
- Add complete Python parameter and return annotations.
- Prefer explicit failure over ambiguous partial success.
- Add or update focused tests with the behavior change.
- Avoid broad rewrites unless the task requires one.
- Do not silently alter public schemas, action names, config keys, database layouts, or import paths.

### Two-attempt escalation rule

After two materially different attempts at the same failing fix, stop repeating speculative edits.

Report:

```text
⚠️ Stuck on <error or unresolved condition>.
Evidence collected:
- ...
Attempts made:
- ...
Likely next investigation:
- ...
```

This rule does not prohibit deeper investigation; it prevents looping on the same unsupported fix.

### Git rules

- Do not push.
- Do not stage or commit unless the user explicitly asks.
- Never discard, reset, rewrite, or amend the user’s work without explicit authorization.
- Before a requested commit, inspect the diff and run the required validation.
- Recheck affected callers and dependency paths with TencentDB code-graph tools after non-trivial code changes and before committing.

---

## 7. Investigation and Debugging

When asked to fix a runtime problem:

1. Reproduce or locate the concrete failure.
2. Read relevant logs before proposing a cause.
3. Trace the execution path from the observed symptom.
4. Compare current behavior with focused tests and documentation.
5. Form a falsifiable hypothesis.
6. Make the smallest change that addresses the evidenced cause.
7. Re-run the reproduction and regression tests.

Do not answer a bug-fix request with guesses when logs or runtime evidence are available.

Useful commands:

```bash
docker exec synth-dev tail -f /app/logs/synth.log
docker exec synth-dev tail -f /app/logs/synth.log | grep -E "run_action|execute_action"
docker exec synth-dev tail -f /app/logs/synth.log | grep -E "\[grillo\]|grillo"
```

Record genuinely recurring, non-obvious defects in the repository’s established issue/changelog location. Do not duplicate resolved issue narratives inside `AGENTS.md`.

---

## 8. TencentDB Knowledge MCP Rules

Use the `tencentdb-knowledge` developer MCP server as code-intelligence and repository-wiki support, not as a substitute for reading current source code and tests.

The launcher is `scripts/tencentdb_knowledge_mcp.py`. It starts or reuses the local Knowledge Service and exposes query-only MCP tools. It must remain separate from Synth runtime MCP configuration under `config/synth_mcp.json`.

The default repository identifiers are supplied by the launcher through `TDAI_CODE_GRAPH_ID` and `TDAI_WIKI_ID`. Use the configured values when invoking tools; do not invent IDs.

### Required before changing a symbol

Run `code_impact` for each materially modified function, class, or method when the server and indexed graph are available.

- Review direct callers and affected execution flows.
- Warn the user before proceeding when the result is HIGH or CRITICAL risk.
- Update all direct dependents required by the change.
- Use `code_callers`, `code_callees`, and `code_node` to resolve ambiguous or incomplete impact results.

### Required for refactors

- Use `code_search` or `code_explore` to locate the relevant symbols and files before extracting or moving code.
- Inspect callers and callees before renaming a symbol; do not rely on global text replacement.
- Re-run `code_impact` after the refactor and confirm the affected dependency paths match the intended scope.

### Required after non-trivial code changes

Re-query impact for materially changed symbols and confirm the returned callers and dependency paths match the intended scope. Repeat the check before a requested commit if the working tree changed afterward.

Use `code_status` to verify graph availability before trusting graph results. If the graph is unavailable, stale, or the MCP server/tool is missing, notify the user as required by the Developer MCP usage policy and fall back to direct source search and tests when safe.

Use `wiki_search` and `wiki_read` for focused repository documentation lookup. Treat indexed wiki content as navigation context rather than proof when it disagrees with current implementation or tests.

---

## 9. Validation

Run the narrowest useful checks during development, then complete the applicable final sequence.

### Python changes

```bash
uv run ruff format <edited paths>
uv run ruff check --fix <edited paths>
uv run ty check <edited Python files>
uv run pytest <focused tests>
```

Before marking a broad code task complete, run the wider relevant suite:

```bash
uv run pytest
```

Do not run whole-repository type checking unless requested; use scoped `ty` checks because the repository may contain unrelated legacy findings.

### Validation expectations

- A passing formatter is not a test.
- A passing unit test is not proof that container startup works.
- Changes involving startup, imports, plugins, interfaces, migrations, or Docker require an appropriate smoke test.
- Changes involving schemas or migrations require validation against the supported database paths.
- Changes involving an external service must test failure and unavailable-service behavior.
- Report any checks you could not run and why.

### OpenAI-compatible API smoke test

When appropriate:

```bash
curl -X POST http://localhost:11435/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "default",
    "messages": [
      {"role": "system", "content": "Respond with ONLY valid JSON: {\"actions\": []}"},
      {"role": "user", "content": "Your test message"}
    ],
    "stream": false
  }'
```

---

## 10. Documentation

### Wiki synchronization

When asked to update the exported repository wiki, follow
`docs/WIKI_MAINTENANCE.md`.

Treat the requested Git range as the change scope, current source and tests as
implementation evidence, and `docs/wiki/` as the structural baseline. Do not
regenerate the export unless explicitly requested. Patch affected pages in both
wiki trees where applicable, apply the documented metadata rules, validate
links and references, and report the resulting wiki diff.

Update documentation when a change affects:

- public behavior;
- installation or deployment;
- configuration;
- action schemas;
- plugin/interface layout;
- architecture or extension points;
- troubleshooting;
- persistent data;
- security boundaries.

Documentation locations:

- `docs/wiki/en/content/` — exported reader-facing wiki
- `docs/wiki/knowledge/en/` — exported generated subsystem knowledge
- `docs/` — maintained user/developer documentation
- component `guide.md` — source of truth for plugin/interface guides
- root `README.md` — project entry point
- `CHANGELOG.md` — user-visible or recurring change history

Do not turn `AGENTS.md` back into an encyclopedic architecture dump. Keep durable operating constraints here and put subsystem detail in the wiki or maintained docs.

When code changes invalidate a wiki page, update that page in the same task or clearly report the stale page.

---

## 11. Infrastructure Notes

Container rebuild:

```bash
docker compose up -d --build
```

Development deployment, when that file/environment exists:

```bash
docker compose -f docker-compose-dev.yml --env-file .env-dev up -d --build
```

Do not delete logs before collecting evidence for a bug. Clear generated logs only when explicitly required and after preserving relevant diagnostics.

Selkies convention:

- HTTP: container port `3000`
- HTTPS: container port `3001`
- certificates: `/config/ssl/`

---

## 12. External Planning Systems

The AFFiNE board is a shared planning system.

- Read it when the task depends on roadmap, status, or recorded design decisions.
- Do not write, edit, or reorganize board content unless the user explicitly asks.
- Keep AFFiNE credentials outside the repository.
- Documentation must show placeholders, never real passwords.
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
| `vessel_diary` | `core/db.py` / `init-db.sql` | Rift Vessel end-of-session **operational recap** (`session_id`, `interface_path`, `environment`, `summary` LONGTEXT, `moments_count`, `reason`, `created_at`). Written by the **Rift Vessel Compactor** plugin (`plugins/rift_vessel/vessel_compactor/` → `core/vessel_diary_compactor.py::compact_activity_recap`, `reason = "activity_recap"`) from `vessel_activity_log` rows — factual/third-person, **separate from the shared `ai_diary`** so in-world telemetry never pollutes ordinary Fast-Lane prompts |
| `goals` | `plugins/goals/goals.py` / `init-db.sql` | **Generic scope-aware goal store** owned by the standalone Goals plugin — Synth's own free-text goals for any game/planner/personal-life use, isolated by a three-part scope tuple (`scope`, `game`, `world`) plus `session_id`, `description`, `note`, `destination`, `steps` JSON, `current_step`, `target_kind`, `target_name`, `status`, `created_at`, `updated_at`. No catalogue; stepped goals auto-complete when `current_step` passes the last step. Minecraft goals are pinned `scope="vessel"`/`game="minecraft"`/`world="none"` via the shim `plugins/rift_vessel/minecraft/goals.py`. Legacy `minecraft_goals` is renamed to `goals` (scope columns backfilled) by `core/migrations.py::_migrate_goals_table` |
| `external_endpoints` | `init-db.sql` | LLM/API endpoint registry (name, protocol, URL, key, capabilities, model list) |
| `scheduled_events` | `plugins/event_plugin.py` | Date/time triggered events Synth should act on |
| `blocklist` | `plugins/blocklist.py` | Blocked users/entities |
| `message_map` | `plugins/message_map.py` | Message ID mapping across interfaces |

Example user-owned config:

```text
AFFINE_BASE_URL=<board URL>
AFFINE_EMAIL=<agent account>
AFFINE_PASSWORD=<secret stored outside the repository>
```

---

## 13. Completion Report

Before finishing a code task, verify:

- [ ] Relevant repository guidance and wiki pages were read.
- [ ] Existing user work was preserved.
- [ ] Impact analysis was run for modified symbols.
- [ ] The implementation follows the single-chain and optional-component rules.
- [ ] No keyword-based semantic behavior was introduced.
- [ ] No credential or sensitive data was added.
- [ ] Focused tests cover the change.
- [ ] Formatting, linting, and scoped type checks pass.
- [ ] Wider tests or smoke checks were run where appropriate.
- [ ] For non-trivial code changes, TencentDB impact queries (or a disclosed direct-source fallback) match the intended scope.
- [ ] Documentation was updated or explicitly identified as unchanged.
- [ ] No staging, commit, push, reset, or destructive action occurred without authorization.
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
| `LOG_RETENTION_DAYS` | **Env var** (not config-registry — logging is initialised at bootstrap before the DB). Days of logs to keep (plain + gzip combined) before deletion (default `7`). Today/yesterday plain text; older days gzip-compressed; past the window deleted. See `docs/logging.rst`. |
| `PROJECT_DEFAULT_LANGUAGE` | Default language for responses |
| `PROJECT_DEFAULT_TONE` | Default response tone |
| `INTERFACE_LANGUAGE_OVERRIDES` | JSON: per-interface language overrides |
| `INTERFACE_TONE_OVERRIDES` | JSON: per-interface tone overrides |
| `DIARY_HISTORY_DAYS` | How many days of diary to inject into context |
| `EMOTION_DECAY_TAU` | Emotion decay time constant (seconds) |
| `EMOTION_MAX_DISPLAY` | Max emotions to display in UI |
| `SOUL_COMPILE_IDLE_SECONDS` | Idle seconds before SOUL compiles buffered transcript |
| `SOUL_SCHEDULER_INTERVAL_SECONDS` | Scheduler tick interval for SOUL compile/rollup checks |
| `SOUL_REPOSITORY_BACKEND` | SOUL persistence backend selector (`memory` or `postgres`) |
| `SOUL_POSTGRES_DSN` | PostgreSQL DSN used when SOUL backend is `postgres` |
| `MEMORY_SEARCH_MAX_RESULTS` | Max memories returned per query |
| `GRILLO_ALLOWED_ACTIONS` | Actions Grillo is permitted to execute |
| `GRILLO_ALLOWED_SECURITY_LEVEL` | Max security level for Grillo actions |
| `AUTONOMY_ALLOWED_ACTIONS` | Actions allowed in autonomy mode |
| `AUTONOMY_ALLOWED_SECURITY_LEVEL` | Max security level for autonomous actions |
| `DRONE_MAX_ITERATIONS` | Hard cap on Drone sub-agent loop iterations (default 3) |
| `DRONE_TURN_TIMEOUT_SEC` | Wall-clock budget per Drone turn in seconds (default 90) |
| `AGENT_ENABLE_THINKING` | Enable model thinking on Agent Lane engine calls (default `True`; Venice `disable_thinking: false`). The agentic loop is the ONLY route that turns thinking on — ordinary chat keeps its configured default. |
| `AGENT_NATIVE_TOOLS` | Pass OpenAI function schemas (`tools` + `tool_choice=auto`) on Agent Lane engine calls so capable engines (e.g. Venice deepseek) return structured `tool_calls` instead of ad-hoc text-protocol formats (default `True`). Only applied to external endpoint bridges; plugin engines keep the plain positional call. |
| `AGENT_PARALLEL_TOOL_CALLS` | Allow multiple native tool calls per Agent Lane response (default `True`). |
| `AGENT_SHELL_ALLOW_HOST` | Allow `agent_run_shell` to run when NOT in a container (default `False`; a host shell is a real compromise risk) |
| `VESSEL_AUTONOMY_ENABLED` | Enable autonomous Rift Vessel play — the slow will beat, the middle action beat, and the fast motor tick (default `False`) |
| `VESSEL_WILL_INTERVAL_SEC` | Seconds between slow volition/will beats — the LLM turn that authors/updates Synth's goal (default 45, clamped `[10, 3600]`, falls back to legacy `VESSEL_BEAT_INTERVAL_SEC`) |
| `VESSEL_WILL_QUIET_SEC` | Quiet window (s) required before a will beat may fire after a *player* interacts with Synth in-world (default 60, clamped `[0, 3600]`, `0` disables). Defers the "reflect on your own" volition turn while a player is present so a direct address is answered reactively instead of being ignored |
| `VESSEL_ACTION_BEAT_ENABLED` | Enable the middle "idea → concrete step" action beat — the LLM Fast-Lane turn that maps the free-text goal to one concrete world verb (default `True`) |
| `VESSEL_ACTION_INTERVAL_SEC` | Seconds between action beats (default 20, clamped `[3, 300]`) |
| `VESSEL_GOAL_DEBRIEF_ENABLED` | Enable the goal debrief — a slow, structural postflight check (`core/vessel_goal_debrief.py`) that supervises the single active vessel goal: it **deterministically auto-completes** a goal already satisfied by the live world/inventory (via the connector's world-owned `evaluate_goal_completion`/`complete_active_goal` hooks) and **arms a stall cue** on the next will beat when a goal sits unchanged too long. Closes the gap where Synth progresses physically but never declares a goal done. Structural only (never reads goal text as intent), Fast Lane only (no cognition turn, no diary) (default `True`). **Multi-part + quantity aware:** a goal naming several products is only completed when **every** named product is present at its stated count (`target_names.derive_quantity`, e.g. "gather 20 oak logs" needs 20) — a single ingredient never closes a whole build goal (the "runs around re-authoring the same goal" churn fix); a derived raw-material target never completes a goal that also names products; stepped goals are never auto-completed here (their plan owns the progression) |
| `VESSEL_GOAL_EXPAND_ENABLED` | Enable the **Goal Plan Expansion (Drone)** — each time Synth authors a fresh goal, a short-lived Drone runs out of band (Fast Lane only, no in-world turn) to expand it into an ordered `steps` plan, consulting the game knowledge base for real rules, and writes it back via `update_goal`, then re-notifies Synth via a will beat. Without it goals stay stepless free text and the action beat/motor have nothing concrete to chase (the "running around" gap). Default `True`; gated additionally on `VESSEL_AUTONOMY_ENABLED` and `VESSEL_KNOWLEDGE_ENABLED`. NOTE: `set_goal` deliberately never threads `steps` so the expander is not gated out; the plan arrives via `update_goal`. If it was turned off during debugging, re-enable it or goals will not get plans |
| `VESSEL_GOAL_DEBRIEF_USE_HISTORY` | Enable the goal debrief's **history-based** completion check (default `True`). When the fast inventory/world-state check did not already satisfy the goal, the debrief additionally consults the session's own `vessel_activity_log` (via the connector's `evaluate_goal_completion_from_history` hook — Minecraft implemented) and auto-completes the goal when a successful action *actually taken this session* structurally matches the goal's concrete target (place/mine/collect a block, attack/shoot a mob, craft/smelt an item). Closes the gap where a goal is fulfilled by an action that leaves **no lasting inventory trace**. Purely structural — id-based matching on the logged target ids (`_HISTORY_TARGET_KEYS`), never a text parse; fully fail-safe. **Multi-part + quantity aware:** a goal naming several products is only completed when **every** named product is matched (a single intermediate `collect_block oak_log` row no longer completes a whole cottage goal — the "runs around re-authoring the same goal" churn fix), and the goal text's stated count (`target_names.derive_quantity`, e.g. "gather 20 oak logs") is summed from each row's logged `_result.data` (`collected`/`count`) before the goal closes |
| `VESSEL_GOAL_DEBRIEF_INTERVAL_SEC` | Seconds between goal-debrief checks (default 30, clamped `[5, 3600]`) |
| `VESSEL_GOAL_DEBRIEF_STALL_TICKS` | Consecutive unchanged debrief checks (same goal id + `current_step` + `updated_at`) before the debrief arms a will-beat stall cue prompting Synth to reconsider or change approach (default 4, clamped `[2, 100]`) |
| `VESSEL_MOTOR_ENABLED` | Enable the fast motorics reflex that moves the body toward the goal with no LLM (default `True`) |
| `VESSEL_MOTOR_INTERVAL_SEC` | Seconds between fast motor ticks (default 3, clamped `[1, 60]`) |
| `VESSEL_STATICITY_WARD_ENABLED` | Enable the always-on **staticity ward** (Minecraft adapter, default `True`): if the body lingers within a small radius of a moving anchor for too many consecutive motor ticks — even while it has a goal or keeps poking an in-reach block — it breaks the parking by rotating its heading and `goto`-ing a fresh distant waypoint (fallback `wander`). Broader than the tick-to-tick stuck-body watchdog: it also catches a goalless or physically-inert body that the other watchdog misses. Runs FIRST each motor tick (after the survival guard, before the `no goal` early-return). Purely positional/numeric (no goal text, no keywords), Fast Lane only (no LLM, no diary) |
| `VESSEL_STATICITY_TICKS` | Consecutive motor ticks the body may stay within `VESSEL_STATICITY_RADIUS` of its anchor before the ward relocates it (default 8 ≈ 24 s at the default ~3 s motor tick, clamped `[2, 1000]`) |
| `VESSEL_STATICITY_RADIUS` | Horizontal radius (blocks) that still counts as "the same place" for the staticity ward; stepping outside it re-anchors and resets the idle counter so normal travel never trips the ward (default 2.0, clamped `[0.5, 32.0]`) |
| `VESSEL_REFLECTION_ENABLED` | Enable the deliberate reflection pause — an elevated-priority LLM turn that fires when Synth is playing with no active goal (or a goal with no step plan), prunes its own pending autonomous beats, and authors/refines the goal before autonomy resumes (default `True`) |
| `VESSEL_REFLECTION_DURATION_SEC` | Duration (s) of the reflection window during which the will/action beats are held off — the motor tick and survival reflex keep running (default 15, clamped `[3, 300]`) |
| `VESSEL_REFLECTION_MIN_INTERVAL_SEC` | Anti-thrash floor (s): minimum time between two reflection pauses (default 60, clamped `[10, 3600]`) |
| `VESSEL_GOAL_BEAT_ENABLED` | Enable the dedicated **goal beat** — a Fast-Lane LLM turn, fired only while Synth has **no active goal**, whose exposed action allowlist is restricted (per-turn, via `context_memory["allowed_action_types"]`) to just `vessel_<world>_set_goal` + `vessel_<world>_update_goal`. Structurally forces even a weak model to author a goal instead of falling back to passive `observe`/`status`. Uses the persona/system prompt like every other beat (default `True`) |
| `VESSEL_GOAL_BEAT_INTERVAL_SEC` | Seconds between goal beats while there is no active goal (default 45, clamped `[10, 3600]`) |
| `VESSEL_SELF_PRESERVATION_ENABLED` | Enable the fast self-preservation survival reflex on the motor tick (default `True`) |
| `VESSEL_SP_LOW_OXYGEN` | Oxygen threshold at/below which the drowning reflex surfaces the body (default 6). **On the 0..20 air-bubble scale** (mineflayer `oxygenLevel` at runtime: 20 = full lungs, 0 = out of air), NOT air ticks — a healthy submerged bot reads ~20, so an air-ticks value would false-fire constantly |
| `VESSEL_SP_LOW_HEALTH` | Health at/below which a hostile encounter escalates from defend to flee (default 6) |
| `VESSEL_SP_HOSTILE_DIST` | Distance (blocks) within which a hostile mob triggers the defend/flee reflex (default 8) |
| `VESSEL_SP_FIGHT_BACK` | Whether Synth fights nearby aggressors (`attack`/`shoot`) before escalating to flee (default `True`) |
| `VESSEL_SP_FIGHT_MAX_FAILS` | Consecutive failed fight attempts before escalating from defend to flee (default 8 — health-primary escalation, so the body keeps fighting while healthy) |
| `VESSEL_SP_USE_RANGED` | Whether Synth may use a carried ranged weapon (bow/crossbow) with ammo against a distant aggressor via the `shoot` verb before closing to melee (default `True`) |
| `VESSEL_SP_RANGED_MIN_DIST` | Minimum target distance (blocks) at/above which the combat reflex prefers ranged (`shoot`) over melee (`attack`), when a loaded ranged weapon is carried (default 5.0) |
| `VESSEL_SP_NIGHT_SHELTER` | Enable the priority-6 night-shelter reflex: at night with aggressive mobs within `VESSEL_SP_SHELTER_DIST`, Synth fully encloses (roofed bed → seal open cells → dig-in niche) via the `shelter` verb — a torch is not enough (default `True`) |
| `VESSEL_SP_SHELTER_DIST` | Distance (blocks) within which a hostile mob at night triggers the shelter reflex (default 16.0 — deliberately wider than `VESSEL_SP_HOSTILE_DIST`=8 so the body walls itself in *before* a mob closes to melee) |
| `VESSEL_MORNING_EXIT_ENABLED` | Enable the priority-7 (lowest) morning surface-exit reflex (default `True`): if Synth dug in / walled itself in overnight and has **no reachable base**, then at **day** with **no open sky above** (structural telemetry `is_day` + `sky_access`) it digs a jumpable ascending staircase (one block forward + one up per step) back to the surface via the `climb_staircase` verb, stopping early once open sky is reached. A day-latch (`_surfaced_last_day`) prevents refiring the same day. Structural only (numeric time + sky-access bool, never text), Fast Lane, no diary |
| `VESSEL_SP_APPRAISAL_ENABLED` | Enable the post-damage appraisal will beat — a high-priority (`PRIORITY_URGENT`) Fast-Lane LLM turn fired on taking damage, so Synth reacts to a hit in character (fight a mob / stay social with a player) (default `True`) |
| `VESSEL_SP_ENGAGE_RATIO` | Minimum `own_power / mob_power` ratio at/above which an **armed** Synth engages a hostile mob instead of fleeing (default 1.0, clamped `[0.2, 5.0]`). Structural, telemetry-only — see §5c "Power-aware fight-vs-flee" |
| `VESSEL_SP_WEAK_MOB_POWER` | Structural power floor below which a **disarmed** (no melee *and* no ranged weapon) Synth still punches out a mob bare-handed instead of fleeing (default 6.0). `_mob_power(entity) = max_health * (1 + attack_damage/8)`. See §5c "Power-aware fight-vs-flee" |
| `VESSEL_PERCEPTION_CONTEXT_CAP` | Max autonomous vessel perceptions merged into a vessel-focus prompt (default 3). Perceptions live in a SEPARATE in-memory ring buffer (`_perception_memory`, maxlen 32) so a rapid ambient burst (e.g. drowning damage) can never evict player chat from the bounded conversational deque; the prompt merges conversation + the last N perceptions chronologically |
| `VESSEL_ACTION_WHITELIST` | Comma/newline-separated fnmatch patterns for the **core-extra** actions kept in the prompt during a vessel turn (advanced; default `send_message, event, schedule_message, blocklist, spawn_drone`). This is the *editable* Tier 3 of the vessel action whitelist — the vessel's own verbs (`vessel_*`) and the connected world's verbs (`*_<world>_*`) are **hardcoded** and always kept. See §5c "Vessel action whitelist" |
| `VESSEL_COMPACTOR_ENABLED` | Enable the **Rift Vessel Compactor** plugin (`plugins/rift_vessel/vessel_compactor/`, default `True`) — the dedicated plugin that, on end-of-session, compacts the session's `vessel_activity_log` rows into one factual, third-person **operational recap** in `vessel_diary` (`reason = "activity_recap"`). Uses its own internal off-chain low-priority asyncio worker queue (not the message chain); also manually runnable from the WebUI Plugins tab (`run_action("compact_now")`). Fully fail-safe |
| `VESSEL_DIARY_COMPACTION_ENABLED` | Gate for the **legacy inline fallback** compaction path only (default `True`). Used when no compaction handler is registered (the Rift Vessel Compactor plugin is absent/disabled): `VesselSessionManager._compact_and_store` writes the old autobiographical entry to `vessel_diary` via `core/vessel_diary_compactor.py::compact_session`. With the plugin enabled this path is not used. Fully fail-safe: any LLM error degrades to a deterministic plain-text join |
| `VESSEL_DIARY_CHUNK_ITEMS` | Max experience-buffer items per compaction chunk before it is summarised into a partial (default 40, clamped `[4, 400]`). Keeps each chunk LLM call small enough that a long session never produces a single oversized prompt |
| `VESSEL_DIARY_CHUNK_CHARS` | Max characters per compaction chunk before it is closed and summarised (default 6000, clamped `[1000, 40000]`). A chunk closes when either the item or the char budget would be exceeded by the next line; the resulting partials are folded (recursively if still oversized) into one coherent entry |
| `VESSEL_KNOWLEDGE_LIVE_FETCH` | Allow the KB to fetch + LLM-summarise pages from a world's live wiki (Minecraft → [minecraft.wiki](https://minecraft.wiki)) (default `True`). When `False` the client is cache-only everywhere (no network, no LLM) — it serves only already-cached pages |
| `VESSEL_KNOWLEDGE_WEB_FALLBACK` | Allow the KB to fall back to a generic web search (via `plugins/web_search/search_engine.py`) when *no* declared per-game wiki matched the query (default `True`). Web results are summarised + cached like a wiki page. `cache_only` beats always skip it |
| `VESSEL_KNOWLEDGE_FETCH_TIMEOUT_SEC` | HTTP timeout (s) for a live wiki search/fetch (default 4, clamp 1–30). Only the explicit `lookup_knowledge` verb / goal-expansion Drone fetch live; the automatic will/motor beat path is `cache_only` and never hits the network |
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

### External Cortex selection reverted to Anthropic after a successful switch  <!-- 2026-08-08 -->
**Symptom:** The WebUI reports a successful switch to an external engine such as Venice, but the next prompt logs that the engine is unregistered and persists `BASE_CORTEX=anthropic`.
**Location:** `core/config.py::get_active_cortex_engine`; `core/external_endpoints/registry.py::_sync_registries`.
**Status:** fixed 2026-08-08.
**Root cause:** The external registry registers endpoints using `effective_subsystem_map()`, while the resolver pruned endpoints using only raw probe capabilities. Venice had `capabilities.cortex=False` but an explicit effective `cortex=True` map, so the two paths disagreed.
**Fix:** The resolver now uses `effective_subsystem_map()` consistently. Endpoints whose effective map disables Cortex still fall back normally.

### Engine Config save caused the active WebUI to replace its endpoint card tree  <!-- 2026-08-08 -->
**Symptom:** Changing an Engine Configuration toggle such as `enable_tools` could make the WebUI appear as a full blue/blocked surface immediately after saving.
**Location:** `res/synth_webui/js/main.js` Engine Config editor; `res/synth_webui/js/engines.js::loadEndpoints`.
**Status:** fixed 2026-08-08.
**Root cause:** The save path called `refreshEndpoints()` after the backend save. That rebuilt the entire external-endpoint card DOM while the active Engine Config editor and its event handlers were still attached to the old tree. The failure was introduced by the in-place endpoint-refresh change; D17 does not contain that refresh on this path.
**Fix:** Engine Config save/apply no longer rebuilds the endpoint cards. The backend still persists and applies the configuration, while the active editor remains stable.

Final responses should state:

1. what changed;
2. why;
3. files affected;
4. validation performed;
5. remaining risks, limitations, or unrun checks.
