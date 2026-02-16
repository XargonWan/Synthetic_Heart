# AGENTSWIN.md

## ⚡️ DEVELOPMENT WORKFLOW (STRICT - WINDOWS)

**Role:** You are a Senior Python Architect.
**Environment:** VS Code (Local Windows Machine).
**Toolchain:** **Astral** (`uv` + `ruff` + `ty`). Do NOT use `pip`.
`uv` can be used inside a venv if needed.

### 1. The Rules of Engagement
* **❌ NO GIT PUSH:** You are **strictly forbidden** from running `git push`. You may stage/commit locally if asked, but I (the human) perform the push.
* **❌ NO INFINITE LOOPS:** If a specific error persists after **2 attempts** to fix it, **STOP**. Output: *"⚠️ Stuck on [Error]. Requesting human or advanced model intervention."*
* **✅ "FAST-FAIL" VALIDATION:** You must run this sequence before marking *any* task as done.

### 2. The Validation Sequence
Run these commands using the auto-approved terminal. If any step fails, FIX it before proceeding.

1.  **Format & Polish:**
    `uv run ruff format .`
    `uv run ruff check --fix .`

2.  **Logic Check (Scoped):**
    # 🛑 CRITICAL: Only check the specific file(s) you just modified!
    # Do NOT run "ty check" on the whole repo to avoid noise.
    `uv run ty check path\to\file_you_edited.py`

3.  **Verify (Test):**
    `uv run pytest`

### 3. Package Management
* **Add Package:** `uv add <package_name>`
* **Add Dev Tool:** `uv add --dev <tool_name>`
* **Sync Environment:** `uv sync`
* **NEVER** run `pip install`. It will break the lockfile.

---

## Overview
Project name is "Synthetic Heart", stylized in "SyntH".
Synth is even the name given to the digital person "speicement" that this project is made for.

This project is structured around a **core** with modular components:
- **Core**: message chain, validation, dispatcher, DB, notifier. Includes automatic registration of validation rules from component actions.
- **Plugins**: provide actions (must register them via `get_supported_actions()` or delegate to interfaces). Sometimes called ActionPlugins. Some subclass `AIPluginBase` for LLM-like behavior.
- **Cortex Engines**: interchangeable reasoning backends (LLM providers, Selenium engines, live adapters, agent engines) implementing `AIPluginBase`.
- **Interfaces**: input/output handlers (e.g. Telegram, Discord). Register actions via `get_supported_actions()`.  

The **core must never hardcode plugin, Cortex, or interface logic**.  
If a plugin/engine/interface is removed, the rest of the system should continue working.

---

## Core Principles
- All messages flow through a **single chain** managed by the core.  
- Actions must **attach to the existing chain**, not create new flows.  
- The **action parser** dynamically detects supported actions by querying plugins and interfaces.  
- Plugins are optional, but **useless if they don’t declare actions** (directly or via interfaces).  
- **Validation rules** are automatically registered from `get_supported_actions()` methods for backward compatibility.  

---

## Plugins
Each plugin must implement:
- `get_supported_actions()` → returns supported actions and their prompt instructions (or empty dict if delegating to interfaces).
- Optional hooks for initialization, teardown, or extended behavior.

Plugins can be:
- Standard plugins (subclass `PluginBase`): handle specific logic without LLM.
- AIPlugins (subclass `AIPluginBase`): handle actions with LLM-like behavior.

If a plugin is missing:
- Its actions are ignored.
- The rest of the system remains operational.

---

## Background Agents (e.g., Grillo)
Some functionality in SyntH is provided by long-running, scheduled "agents" implemented as plugins rather than simple action handlers. The canonical example is **G.R.I.L.L.O.** (the "Grillo" plugin), which performs periodic "beats" to drive internal introspection tasks such as tag elaboration, memory consolidation, self-reflection, curiosity probes, and relationship insights.

Key points:
- Implementation: located under `plugins/grillo/` with a lightweight backward-compatible wrapper at `plugins/grillo_plugin.py`.
- Purpose: generates internal prompts (beats) which are enqueued as low-priority internal messages via `core.message_queue.enqueue_low_priority` and processed by the normal message chain.
- DB: uses `grillo_activity_log`, `grillo_beats` and `grillo_action_execs` (see `init-db.sql`) to record prompts, responses, and execution history for the WebUI History > Grillo view.
- Integration: when Grillo enqueues a beat it attaches context keys like `grillo_beat`, `beat_type` and `activity_log_id`. Outbound messages produced by beats are recorded by `core.action_parser._maybe_record_grillo_outbound_message` so the activity log can show human-readable response text.
- Extensibility: Grillo can discover optional beat-specific plugins (e.g., tag compactor, memory compactor, curiosity generators) via the plugin registry and defer prompt building to them when available.
- Configuration & safety: configurable via `GRILLO_BEAT_INTERVAL` and includes duplicate suppression and simple rate-limiting to avoid flooding.
- Testing: there are tests under `tests/` (e.g., `test_llm_to_interface_grillo_integration.py`) and a helper `tmp/grillo_e2e.py` for manual/CI experiments.
  - Monitoring: watch Grillo logs with `docker exec synth-dev tail -f /app/logs/synth.log | grep -E "\[grillo\]|grillo"` and inspect `grillo_activity_log` / `grillo_beats` in the DB to verify beats and responses.

Notes for contributors:
- Treat Grillo as an internal agent — it uses the same action pipeline but is intended for background maintenance tasks. Ensure any new beat types are documented and recorded in the activity logs so the WebUI can present results.

---

## Cortex Engines
- Engines subclass `AIPluginBase`.
- They handle reasoning and output JSON actions.
- Interchangeable: multiple engines can coexist across kinds (`llm_provider`, `selenium_engine`, `live`, `agent`).
- Base modules live in `cortex/<kind>/*_base.py` and must register their kind + children via `discover_and_register()`.
- Dev engines live under `cortex/<kind>/dev` and are only discovered when dev components are enabled.

---

## Interfaces
- Interfaces manage I/O with external systems (Telegram, Discord, etc.).
- Must not bypass the core's message chain.
- Should forward incoming data into the chain and dispatch core outputs.
- Register supported actions via `get_supported_actions()`.

### Animation handler notes

- The core exposes an `AnimationHandler` in `core/animation_handler.py` which provides dynamic animation discovery and lifecycle control.
- New plugin-friendly APIs:
  - `register_state_animations(state: str, animations: Dict[str, List[str]], sequential=False)` to override or register state animations
  - `register_state_aliases(aliases: Dict[str, List[str]])` to declare alias names for canonical states
  - `set_animation_search_paths(paths: List[Path])` to add custom search paths
  - `get_animation_variants(state: str)` which returns categorized variants (`loop`, `post`, `other`) discovered from descriptors

Plugins and interfaces should call `AnimationHandler.play_animation()` and `stop_animation()` using logical state names (e.g., 'think', 'write') rather than raw file paths. See `skins/Rei/animations/README.md` for examples and descriptor format.

---

## Testing
If you need to create some tests please check the tests folder, do not create persistent tests outside that folder.
If you need a throwaway test instead the root is good but please delete it when you finished.

To run tests locally on Windows, the agent must use `uv`:

1. **Sync Dependencies (Install):**
```powershell
   uv sync