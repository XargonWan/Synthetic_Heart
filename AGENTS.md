
# AGENTS.md

## Overview
Project name is "Synthetic Heart", stylized in "SyntH".
Synth is even the name given to the digital person "speicement" that this project is made for.

This project is structured around a **core** with modular components:
- **Core**: message chain, validation, dispatcher, DB, notifier. Includes automatic registration of validation rules from component actions.
- **Plugins**: provide actions (must register them via `get_supported_actions()` or delegate to interfaces). Sometimes called ActionPlugins. Some subclass `AIPluginBase` for LLM-like behavior.
- **LLM Engines**: interchangeable reasoning backends, implementing `AIPluginBase`.
- **Interfaces**: input/output handlers (e.g. Telegram, Discord). Register actions via `get_supported_actions()`.  

The **core must never hardcode plugin, LLM, or interface logic**.  
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

## LLM Engines
- Engines subclass `AIPluginBase`.
- They handle reasoning and output JSON actions.
- Interchangeable: multiple engines can coexist.

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
If you need to create some tsts please check the tests folder, do not create persistent tests outside that folder.
If you need a throwaway test instead the root is good but please delete it when you finished.

To run tests locally, the agent may:
1. Create a Python virtual environment:
```bash
   python -m venv venv
   source venv/bin/activate
```

2. Install requirements:

```bash
   pip install -r requirements.txt
```
3. Run the test suite:

```bash
   ./run_tests.sh
```

If you need to restar the dev container use:
```bash
docker compose -f docker-compose-dev.yml --env-file .env-dev up -d --build && rm -rf logs/dev/* && videodrome synth restart dev
```
In this ay we thor away the old logs and we don't bother the stable deployment.

---

## Testing via Ollama API

### Quick Test: Send Message to SyntH via Ollama

The Ollama API (port 11434) can be used to send messages to SyntH for testing without needing Telegram or other interfaces.

**Basic curl command:**

```bash
curl -X POST http://localhost:11434/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "default",
    "messages": [
      {
        "role": "system",
        "content": "You are Rekku. Respond with ONLY this JSON: {\"actions\": [{\"type\": \"schedule_message\", \"payload\": {\"text\": \"Test message!\", \"send_in\": \"10 seconds\"}}]}"
      },
      {
        "role": "user",
        "content": "Send a test message!"
      }
    ],
    "stream": false
  }'
```

**For testing `schedule_message` action:**

```bash
curl -X POST http://localhost:11434/api/chat \
  -H "Content-Type: application/json" \
  -d '{
    "model": "default",
    "messages": [
      {
        "role": "system",
        "content": "You are Rekku. Respond with ONLY this JSON: {\"actions\": [{\"type\": \"schedule_message\", \"payload\": {\"text\": \"Reminder text here\", \"send_in\": \"15 seconds\"}}]}"
      },
      {
        "role": "user",
        "content": "Schedule a reminder!"
      }
    ],
    "stream": false
  }'
```

**Monitor the logs while testing:**

```bash
docker exec synth-dev tail -f /app/logs/synth.log | grep -E "run_action|execute_action|schedule_message|Event scheduler|get_due_events|Retrieved.*rows|delivered|marked as delivered"
```

**Expected log sequence (accurate messages & levels):**
1. `[action_parser] 🎬 run_action called with action: {...}` — emitted from `core/action_parser.py` (INFO)
2. `[event_plugin] 🎬 execute_action: type=schedule_message, payload=...` — `plugins/event_plugin.py` (INFO)
3. `[event_plugin] ⏰ _handle_schedule_message_payload CALLED with payload: {...}` — (INFO)
4. `[event_plugin] 🎯 Schedule message task created: <task_name>` — (INFO)
5. `[event_plugin] Event scheduler checking for due events...` — scheduler heartbeat logged with **DEBUG** level (may be hidden if DEBUG is not enabled)
6. `[get_due_events] Retrieved {n} rows` — logged in `core/db.py` inside `get_due_events()` with **DEBUG** level (shows actual number retrieved)
7. `[event_plugin] Event {id} delivered to LLM` — logged when `request_llm_delivery` returns success (INFO)
8. `[event_plugin] ✅ Event {id} successfully marked as delivered in DB` — logged after `mark_event_delivered(event_id)` succeeds (INFO)

**Notes:**
- Some messages (steps 5 and 6) are logged at DEBUG level; enable DEBUG logging to see them. ⚠️
- If delivery fails you'll see warnings/errors such as `[event_plugin] Failed to deliver event {id} after {n} attempts` or DB/transport errors. 
- Use the `get_due_events` / `Event scheduler` logs to confirm the scheduler loop is running and finding events.

**Key points:**
- The system prompt MUST instruct the LLM to respond with ONLY valid JSON
- Actions must match the `get_supported_actions()` schema from plugins
- The `schedule_message` action requires: `text` (message content) and `send_in` (delay like "10 seconds", "5 minutes", "1 hour")
- Monitor logs to verify the action execution pipeline

Selkies TLS note
-----------------
- By default Selkies serves HTTP on container port **3000** and HTTPS on container port **3001**.
- In development we expose host port `${SELKIES_HTTPS_PORT:-3000}` to container **3001** for TLS, and `${SELKIES_HTTP_PORT:-3000}` to container **3000** for plain HTTP. If you changed the compose mapping, ensure the host port is mapped to the matching container port (3001 for HTTPS).
- Selkies expects TLS certificates to be available in `/config/ssl/cert.pem` and `/config/ssl/cert.key`. The dev image includes self-signed certs under `/config/ssl` so HTTPS should work out-of-the-box when the compose mapping is correct.

---

## Animation System (AnimationHandler & VRM Integration)

The **AnimationHandler** (`core/animation_handler.py`) manages VRM avatar animations with state-based triggering (THINK, WRITE, TALK, IDLE) and intelligent transitions.

### Key Architecture

- **State-Based Animation**: Components call `handler.play_animation(AnimationState.THINK, session_id)` instead of hardcoding filenames.
- **Dynamic Resolution**: Animation files are resolved from persona skins → Rei fallback using the pattern: `skins/<persona>/animations/<state>/` → `skins/Rei/animations/<state>/`.
- **Descriptor System**: Each FBX file has an optional `.fbx.json` descriptor defining `intro`, `loop`, `outro` sections (frame ranges), `fps`, and metadata (e.g., `play_once`, `lipsync`, `expressions`).
- **Pre-Loading**: Before sending a `play` command to the WebUI, the AnimationHandler sends a `preload_animation` message to ensure the client has loaded the FBX/descriptor data, preventing T-pose.
- **Transition Management**: When switching states (e.g., THINK → WRITE), the handler plays the `outro` section of the previous animation before transitioning, ensuring smooth visual flow.

### Descriptor Format (.fbx.json)

Each animation file should have a corresponding `.fbx.json` descriptor:

```json
{
  "intro": { "start_frame": 0, "end_frame": 15 },
  "loop": { "start_frame": 16, "end_frame": 60 },
  "outro": { "start_frame": 61, "end_frame": 90 },
  "fps": 30,
  "play_once": false,
  "lipsync": false,
  "expressions": [
    { "start_frame": 0, "end_frame": 30, "targets": { "eyes_closed": 0.1 }, "source": "descriptor", "priority": 10 }
  ],
  "blink": {
    "auto": true,
    "rate_s": 3.5,
    "intensity": 0.6,
    "close_ms": 60,
    "hold_ms": 120,
    "open_ms": 60
  },
  "eye_movement": {
    "auto": true,
    "saccade_rate_s": 2.0
  }
}
```

**Fields:**
- `intro`, `loop`, `outro`: Optional frame ranges for structured animations.
- `fps`: Frames per second (default 30).
- `play_once`: If true, animation plays once and does not loop (used for transitional animations).
- `lipsync`: Boolean flag indicating if the animation is suitable for lip-sync synthesis (prepared for future use).
- `expressions`: Optional array of blendshape targets applied over frame ranges.
  - `targets`: Object mapping logical blendshape names to intensity (0.0-1.0), resolved via `persona.json` → `blendshape_map`
  - `priority`: Numeric priority (higher values applied later)
  - `source`: Origin identifier ("descriptor", "server", "persona_override", etc.)
  - Notes: If **both** `start_frame` and `end_frame` are omitted the expression is treated as always-on. If **only** `start_frame` is provided, the expression will be treated as active from that `start_frame` through the end of the clip (useful to express e.g. eyes closed from a given frame to clip end).
- `blink`: Controls autonomous blinking behavior during the animation.
  - `auto`: Enable autonomous blinking (default: true)
  - `rate_s`: Average blink rate in seconds (default: 3.5)
  - `intensity`: Blink intensity 0.0-1.0 (default: 0.6)
  - `close_ms`: Milliseconds to close eyes (default: 60)
  - `hold_ms`: Milliseconds to hold eyes closed (default: 120)
  - `open_ms`: Milliseconds to open eyes (default: 60)
- `eye_movement`: Controls autonomous eye saccades during the animation.
  - `auto`: Enable autonomous saccades (default: true)
  - `saccade_rate_s`: Average time between saccades in seconds (default: 2.0)

### Animation Folder Structure

```
skins/Rei/animations/
├── think/
│   ├── Thinking.fbx
│   └── Thinking.fbx.json
├── write/
│   ├── Texting.fbx
│   ├── Texting.fbx.json
│   ├── Texting While Standing.fbx
│   └── Texting While Standing.fbx.json
├── talk/
│   ├── talking.fbx
│   └── talking.fbx.json
├── idle/
│   ├── Idle.fbx
│   ├── Idle.fbx.json
│   ├── Idle2.fbx
│   └── Idle2.fbx.json
└── [other_states]/
```

Custom personas can override animations by placing files in `skins/<PersonaName>/animations/<state>/`.

### Typical Animation Flow

1. **Message Received** → Interface calls `persona_manager.set_animation_state("think", session_id)` → AnimationHandler sends `preload_animation` for THINK variants → WebUI client loads FBX/descriptor.
2. **LLM Starts** → Interface calls `set_animation_state("write", session_id)` → AnimationHandler plays THINK's `outro` (if exists), then switches to WRITE loop.
3. **Message Complete** → Interface calls `set_animation_state("idle", session_id)` → AnimationHandler plays WRITE's `outro`, then returns to IDLE rotation.

### Pre-Loading Mechanism

When `play_animation()` is called, the handler:
1. Selects an animation file for the state.
2. Sends a `preload_animation` WS message to the client with the animation path and descriptor.
3. Client calls `AnimationHandler.preloadAnimation()` in JavaScript to cache the FBX and descriptor before playback.
4. Backend then sends the `play` command, confident that the client has loaded the data.

This prevents T-pose and animation skipping caused by missing FBX data.

### Common Issues & Troubleshooting

- **T-pose when animation starts**: Descriptor is missing or FBX file is not found. Add descriptor `.fbx.json` and verify animation file exists.
- **Animation skipped or shows wrong file**: Check that `get_animation_variants()` is discovering the file correctly. Use logs: `[AnimationHandler] Found candidate animation files for state`.
- **Transition is abrupt (no outro)**: Descriptor lacks an `outro` section. Add frame range to descriptor.
- **Animation loops when it should not**: Set `play_once: true` in descriptor or ensure the loop section is defined correctly.

### API Methods

**Backend (Python):**
- `handler.play_animation(state, session_id, loop=True, context_id=None, priority=None)` — Play animation for a state.
- `handler.stop_animation(context_id, session_id)` — Stop animation and return to idle (respects outro).
- `handler.get_animation_variants(state)` — Discover available animations for a state → `{'loop': [...], 'post': [...], 'other': [...]}`.
- `handler.register_state_animations(state, animations_dict, sequential=False)` — Plugins can override animations for a state.

**Frontend (JavaScript):**
- `AnimationHandler.preloadAnimation(animationFile, descriptor)` — Pre-load animation (called by WebUI on `preload_animation` message).
- `AnimationHandler.startAction(actionName, animationFile, playOnce, playSection, descriptor)` — Play animation.
- `AnimationHandler.applyAnimationState(state)` — Apply rich animation state (expressions, emotions, timing).

### Plugin/Interface Integration

Plugins and interfaces should **not** hardcode animation filenames. Instead:

```python
# ❌ Wrong
await webui.send_animation_command(session_id, "/skins/Rei/animations/Think/Thinking.fbx")

# ✅ Correct
await persona_manager.set_animation_state("think", session_id=session_id)
```

This ensures consistency, allows personas to override animations, and leverages pre-loading.

### Graceful Fallback to IDLE

When an animation ends or is stopped, the system gracefully returns to IDLE:

1. **Outro Playback**: If the current animation has an `outro` section, it is played before transitioning.
2. **IDLE Pre-Loading**: When any non-IDLE animation starts (THINK, WRITE, TALK), the AnimationHandler automatically pre-loads IDLE variants in the background via `ensure_idle_preloaded()`.
3. **Instant Fallback**: When `stop_animation()` is called, the fallback to IDLE is instantaneous because the variants were already pre-loaded.

This ensures **zero T-pose** when animations end and smooth transitions back to the default idle state.

### Smart Eye-Closed Behavior

When expressions intentionally close the avatar's eyes (via `eyes_closed` blendshape > 0.5), both **blink** and **eye movement (saccades)** are automatically suspended until the eyes are reopened. This prevents conflicting autonomous animations while the avatar has its eyes closed.

**Two-Layer Implementation:**

1. **Execution-Time Check**: In `_performBlink()` and `_performSaccade()` methods, before executing a blink or saccade, the code checks if `eyes_closed > 0.5`. If true, the action is skipped entirely.

2. **Loop-Time Management**: In `applyExpressionsForFrame()`, the system continuously monitors the `eyes_closed` blendshape value every frame:
   - **Eyes close** (value crosses 0.5): Automatically calls `_stopBlinkLoop()` and `_stopEyeMovement()` with debug logging
   - **Eyes reopen** (value drops below 0.5): Automatically calls `_startBlinkLoop()` and `_startEyeMovement()` (if enabled) with debug logging

**User Requirement Implementation:**
> "fintanto che gli occhi sono chiusi bisogna disattivare il blink finchè non venegono riaperti"  
> (While eyes are closed, blink must be disabled until they are reopened)

This is achieved automatically with no additional configuration needed. Expressions that set `eyes_closed` will trigger automatic suspension of all eye-related autonomous animations.

---

## Implicit Animation Descriptor

For animations **without a descriptor file** (`.fbx.json`), the system applies an **implicit descriptor** with sensible default behavior:

**Implicit Descriptor Defaults (per state):**
- **IDLE state**: Play frame 0 to max frames in a loop (repeating idle animations)
- **Non-IDLE states** (THINK, WRITE, TALK): Play frame 0 to max frames **once** (`play_once=True`)

**Rationale:**
- Users can drop animation FBX files without needing to create descriptors
- Non-IDLE animations naturally transition back to IDLE after completing once
- Prevents infinite looping of transient animations (thinking, typing, talking)
- IDLE loops by default to ensure the avatar is always animated when not doing something specific

**Example Flow:**
1. User sends message → `play_animation(THINK)` → Animation plays once (0→maxframes)
2. LLM starts generating → `play_animation(WRITE)` → Animation plays once (0→maxframes)
3. Message sent → `set_animation_state(IDLE)` → Returns to idle loop

**Customization:**
- Users **can** provide `.fbx.json` descriptors to override implicit behavior
- Descriptors with `play_once: true` or `intro`/`loop`/`outro` sections take full precedence
- See `skins/Rei/animations/README.md` for descriptor format

---

## Documentation
Everytime you do a change evaluate if it's needed to updated the documentation in `./docs`.
The documentation must be written in English and in ReadTheDocs format.

---

## Notes

* Removing a plugin or engine should not break the system.
* Every action must integrate with the core.
* No direct, hardcoded coupling between the core and any specific plugin/interface/engine.
* Validation rules are auto-discovered from `get_supported_actions()` methods.
* Every time you edit a python file use `python3 -m py_compile` against it. If you edited more than one please check them in a single command to save time and user interaction.
* Never use `git add` or `git commit`, thatś the human developer's role