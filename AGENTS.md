
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
docker exec synth-dev tail -f /app/logs/synth.log | grep -E "execute_action|schedule_message|Retrieved.*rows"
```

**Expected log sequence:**
1. `[action_parser] 🎬 run_action called with action: {'type': 'schedule_message', ...}`
2. `[event_plugin] 🎬 execute_action: type=schedule_message, payload={...}`
3. `[event_plugin] ⏰ _handle_schedule_message_payload CALLED`
4. `[event_plugin] 🎯 Schedule message task created`
5. After delay: `[event_plugin] Event scheduler checking for due events...`
6. `[event_plugin] Retrieved 1 rows` (or more if multiple events)
7. Event delivered to LLM via interface_to_llm transport

**Key points:**
- The system prompt MUST instruct the LLM to respond with ONLY valid JSON
- Actions must match the `get_supported_actions()` schema from plugins
- The `schedule_message` action requires: `text` (message content) and `send_in` (delay like "10 seconds", "5 minutes", "1 hour")
- Monitor logs to verify the action execution pipeline

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