Rift Vessel Architecture
========================

..  This document is the canonical output of issue #310 investigation
    (https://github.com/XargonWan/Synthetic_Heart/issues/310).

..  Status: investigation complete — ready for phased implementation.

..  contents::

1. Executive Summary
--------------------

SyntH's architecture already supports clean **cognition–embodiment separation**.
The core enforces a strict boundary:

- **Cognition / reasoning** is owned by the Cortex engines and the message chain.
- **I/O to the outside world** is owned by interfaces.
- **Plugin actions** are the only bridge between the two.

A "Rift Vessel" is therefore a natural extension of the existing interface
pattern — an interface that does not connect to a chat platform but to a
**game engine, virtual world, or simulation environment**.

SyntH can ship a **generalized ``rift_vessel/``** module (alongside
``interface/``, ``plugins/``, ``engines/``) containing:

- ``rift_vessel/rift_vessel_base.py`` — abstract base class
- ``rift_vessel/skyrim/`` — Skyrim SKSE/Papyrus bridge
- ``rift_vessel/vrchat/`` — VRChat OSC/Udon bridge
- ``rift_vessel/schema.py`` — world-state schemas, action negotiation

The Rift Vessel IS an interface from the core's perspective, but with
additional semantics:

- It carries **world-state** (not just chat messages)
- It exposes **environment actions** (not just ``send_message``)
- It translates game events into **episodic memory**
- It translates SyntH intents into **game commands**

2. Current SyntH Architecture
-----------------------------

2.1. Runtime Structure
^^^^^^^^^^^^^^^^^^^^^^

::

    main.py
      └─ core_initializer.py     # Orchestrates startup
           ├─ Registries         # Cortex, Component, Interface, Validation
           ├─ Plugin discovery    # Auto-scans plugins/, cortex/, interface/
           ├─ Engine loading      # Loads BASE_CORTEX from DB
           └─ Interface init      # Calls initialize_interface() per module

2.2. Message Flow
^^^^^^^^^^^^^^^^^

::

    User → Interface → message_queue.enqueue()
                            ↓
                    PriorityQueue consumer loop
                            ↓
                    plugin_instance.handle_incoming_message()
                            ↓
                    prompt_engine.build_prompt_request()
                            ↓
                    LLM Engine (Gemini, OpenRouter, etc.)
                            ↓
                    message_chain.handle_incoming_message(source="llm")
                            ↓
                    action_parser.run_actions()
                            ↓
                    Interface.send_message() / Plugin.execute_action()

2.3. Subsystem Breakdown
^^^^^^^^^^^^^^^^^^^^^^^^

+-------------------+----------------------------------------------------------+
| Subsystem         | Location(s)                                             |
+===================+==========================================================+
| **Cognition**     | ``cortex/``, ``engines/``, ``llm_engines/``             |
| (LLM engines)     | Subclass ``AIPluginBase``                               |
+-------------------+----------------------------------------------------------+
| **Memory**        | ``plugins/ai_diary.py`` (diary)                         |
| (short & long)    | ``core/synth_core_memory.py`` (searchable memories)     |
|                   | ``core/soul/`` (episodic memcells + embeddings)         |
|                   | ``core/chat_history_cache.py`` (per-chat message log)   |
|                   | ``plugins/bio_manager.py`` (self-knowledge)             |
+-------------------+----------------------------------------------------------+
| **Action System** | ``core/action_parser.py`` — dispatches actions           |
|                   | ``core/message_chain.py`` — validation + correction     |
|                   | ``core/transport_layer.py`` — JSON extraction, sending  |
|                   | ``core/validation_registry.py`` — per-action validation |
+-------------------+----------------------------------------------------------+
| **Plugin System** | ``plugins/`` — subclass ``PluginBase`` or ``AIPluginBase`` |
|                   | Register actions via ``get_supported_actions()``        |
+-------------------+----------------------------------------------------------+
| **Interfaces**    | ``interface/`` — ``telegram_bot.py``, ``discord_interface.py``, |
| (I/O adapters)    | ``matrix_interface.py``, ``ollama_compat_server.py``,  |
|                   | ``core/webui.py``                                       |
+-------------------+----------------------------------------------------------+
| **Events**        | ``core/event_dispatcher.py`` — scheduled event dispatch   |
|                   | ``plugins/event_plugin.py`` — event triggers             |
+-------------------+----------------------------------------------------------+
| **Background**    | ``plugins/grillo/`` — autonomous scheduled beats        |
| (Grillo)          | ``core/message_queue.py`` — priority queue              |
+-------------------+----------------------------------------------------------+
| **Notifications** | ``core/notifier.py`` — trainer/log notifications        |
+-------------------+----------------------------------------------------------+
| **Animation**     | ``core/animation_handler.py`` — VRM avatar animations   |
| (Karada)          | ``core/karada_*.py`` — WebSocket transport              |
+-------------------+----------------------------------------------------------+
| **Media**         | ``core/media_dispatcher.py`` — Auris (STT) → Iris       |
| (Vision/Audio)    | (Vision) → Live pipeline                                |
+-------------------+----------------------------------------------------------+
| **External LLM**  | ``core/external_endpoints/`` — OpenAI-compat, Gemini,   |
| (endpoints)       | Anthropic adapters, bridge to cortex                    |
+-------------------+----------------------------------------------------------+

3. Separation of Concerns — Current State
-----------------------------------------

3.1. What SyntH Already Separates Well
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

- **Cognition ≠ Embodiment**: Cortex engines never call interface methods
  directly. They return JSON actions; the message chain/action parser routes
  them.
- **Plugin ≠ Core**: Plugins register actions via ``get_supported_actions()``.
  Core never imports plugin internals.
- **Interface ≠ Plugin**: Interfaces register their own ``send_message`` /
  ``get_supported_actions()``. The action parser discovers them dynamically.
- **Memory ≠ Interface**: Diary, SOUL, chat_history_cache are agnostic of
  which interface produced a message — they key on ``interface_path`` strings.

3.2. What Needs Work for Rift Vessel
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

- **No world-state model**: ``interface_path`` is purely a routing key.
  There is no concept of "this interface_path represents an entity with
  health=42, location=Whiterun, combat_state=true".
- **Actions are interface-specific**: ``message_telegram_bot`` assumes a chat.
  Game actions like ``attack(target)``, ``move(location)``, ``cast(spell)``
  have no registered handler.
- **No environment-agnostic action bus**: Every action type must be
  registered by a plugin or interface. Rift Vessel needs a registry of
  *generic environment actions* that game-specific adapters implement.
- **No bidirectional event stream**: Interfaces receive user messages and
  send replies. Rift Vessel also needs a *push* of world-state changes
  (e.g., "health changed", "entered combat", "NPC spoke") that are NOT user
  messages but environmental triggers.

4. Rift Vessel Abstraction
--------------------------

4.1. Architecture Boundary
^^^^^^^^^^^^^^^^^^^^^^^^^^

::

    [SyntH Core]
       |
       | JSON actions (type, payload)
       | environment_state context
       v
    [Rift Vessel Bridge]    ← NEW: core/rift_vessel_bridge.py
       |
       | typed commands
       | world-state events
       v
    [Game Adapter]
       |  ┌─────────────┐   ┌─────────────┐
       |  │ Skyrim SE   │   │ VRChat      │   │ Godot │ ...
       |  │ (SKSE DLL)  │   │ (OSC/Udon)  │   │       │
       |  └─────────────┘   └─────────────┘
       |
       v
    [Game Engine]

4.2. Rift Vessel Base Class
^^^^^^^^^^^^^^^^^^^^^^^^^^^

Location: ``rift_vessel/rift_vessel_base.py``

.. code-block:: python

    class RiftVesselBase(ABC):
        """Abstract base for all game/environment embodiment adapters.

        A RiftVessel IS a SyntH interface (it registers in INTERFACE_REGISTRY)
        but carries additional semantics for world interaction.
        """

        @abstractmethod
        def get_interface_id(self) -> str:
            """Return e.g. 'skyrim', 'vrchat', 'godot'."""

        @abstractmethod
        def get_supported_actions(self) -> dict:
            """Return environment-specific actions the vessel can execute.

            Example::
                {
                    "game_attack": {
                        "required_fields": ["target"],
                        "description": "Attack a target entity",
                    },
                    "game_move": {
                        "required_fields": ["location"],
                        "description": "Move to a named location",
                    },
                }
            """

        @abstractmethod
        async def send_world_state(self, state: "WorldState") -> None:
            """Push current SyntH world-state understanding to the game adapter.

            Called periodically by the bridge to sync SyntH's mental model
            with the game's actual state.
            """

        @abstractmethod
        async def execute_game_action(self, action: str, params: dict) -> dict:
            """Execute a game-native action and return the result."""

        async def on_world_event(self, event: "WorldEvent") -> None:
            """Called when the game adapter pushes a world-state change.

            Default: enqueue as a system message into the core message queue,
            so SyntH's cognition layer can process it.
            """
            ...

        def get_world_schema(self) -> dict:
            """Return the JSON schema for this environment's WorldState.

            Example::
                {
                    "health": {"type": "integer", "min": 0, "max": 100},
                    "location": {"type": "string"},
                    "combat_state": {"type": "boolean"},
                }
            """
            return {}

4.3. Discovery and Registration
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Rift Vessel adapters live in ``rift_vessel/<name>/`` and are discovered
by the core initializer alongside plugins and interfaces:

.. code-block:: python

    # In core_initializer._load_plugins():
    search_dirs = ["plugins", "cortex", "interface", "rift_vessel"]

Each adapter module exports:

.. code-block:: python

    VESSEL_CLASS: type[RiftVesselBase]
    VESSEL_NAME: str   # e.g. "skyrim"

5. World-State Schema
---------------------

5.1. Generic WorldState
^^^^^^^^^^^^^^^^^^^^^^^

Location: ``rift_vessel/schema.py``

.. code-block:: python

    @dataclass
    class WorldState:
        """Canonical representation of the environment at a point in time."""

        environment: str                  # "skyrim", "vrchat", "godot"
        timestamp: datetime

        # Entity state
        entity_id: str                    # Actor/form ID in the game
        health: float | None = None
        max_health: float | None = None
        magicka: float | None = None
        stamina: float | None = None
        location: str | None = None       # Named location / cell
        position: tuple[float, float, float] | None = None

        # World context
        combat_state: bool = False
        is_sneaking: bool = False
        is_mounted: bool = False
        current_weapon: str | None = None
        current_spell: str | None = None

        # Sensory
        visible_entities: list[EntityRef] = field(default_factory=list)
        audible_events: list[str] = field(default_factory=list)
        recent_dialogue: list[str] = field(default_factory=list)

        # Available actions (what the LLM can choose to do)
        possible_actions: list[ActionDef] = field(default_factory=list)

        # Environment-specific extras
        extra: dict[str, Any] = field(default_factory=dict)


    @dataclass
    class EntityRef:
        """Reference to another entity in the world."""
        id: str
        name: str
        relationship: str = "neutral"    # hostile, friendly, neutral
        health_pct: float | None = None
        distance: float | None = None


    @dataclass
    class ActionDef:
        """An action the entity CAN perform at this moment."""
        name: str                        # "attack", "talk", "follow"
        target_required: bool = False
        description: str = ""
        parameters: dict = field(default_factory=dict)


    @dataclass
    class WorldEvent:
        """An event the game adapter pushes to SyntH."""
        event_type: str                  # "damage_taken", "entered_combat",
                                         # "npc_spoke", "location_changed"
        source: str                      # environment name
        data: dict                       # event-specific payload
        timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

5.2. WorldState → Context Injection
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The Rift Vessel Bridge injects the current ``WorldState`` into the LLM
prompt context so SyntH can react to it:

.. code-block:: python

    # In core/prompt_engine.py:
    world_state = await rift_vessel_bridge.get_world_state(interface_path)
    if world_state:
        runtime_context["world_state"] = world_state

The ``PromptRequest`` dataclass gains an optional field:

.. code-block:: python

    @dataclass
    class PromptRequest:
        ...
        world_state: WorldState | None = None

6. Action Abstraction Layer
---------------------------

6.1. Generic Environment Actions
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

A set of *abstract* action types that any Rift Vessel can implement:

+--------------------+------------------+------------------------------------+
| Action Type        | Payload          | Description                        |
+====================+==================+====================================+
| ``game_attack``    | ``target``       | Attack a named entity              |
+--------------------+------------------+------------------------------------+
| ``game_move``      | ``location``     | Navigate to a named location       |
+--------------------+------------------+------------------------------------+
| ``game_talk``      | ``target``,      | Speak to an NPC                    |
|                    | ``dialogue``     |                                    |
+--------------------+------------------+------------------------------------+
| ``game_inspect``   | ``target``       | Examine an object/entity           |
+--------------------+------------------+------------------------------------+
| ``game_use``       | ``item``,        | Use an item from inventory         |
|                    | ``target``       |                                    |
+--------------------+------------------+------------------------------------+
| ``game_cast``      | ``spell``,       | Cast a spell                       |
|                    | ``target``       |                                    |
+--------------------+------------------+------------------------------------+
| ``game_equip``     | ``item``         | Equip a weapon/item                |
+--------------------+------------------+------------------------------------+
| ``game_wait``      | ``seconds``      | Wait / pass time                   |
+--------------------+------------------+------------------------------------+
| ``game_observe``   | *(none)*         | Request updated world-state        |
+--------------------+------------------+------------------------------------+

These actions are registered by ``RiftVesselBridge`` at startup by
aggregating all loaded ``RiftVesselBase`` adapters.

6.2. Action Negotiation
^^^^^^^^^^^^^^^^^^^^^^^

Not all actions are available at all times. The ``WorldState.possible_actions``
list tells the LLM what it CAN do right now. If the LLM emits an action that
is NOT in ``possible_actions``, the corrector can ask it to pick a valid one.

7. Memory Architecture for Embodied Experiences
-----------------------------------------------

7.1. What Changes
^^^^^^^^^^^^^^^^^

- **Diary entries** (``ai_diary``) already store free-form ``content`` with
  ``user_message`` and ``timestamp``. Rift Vessel experiences can be logged
  as diary entries with ``interface_path`` = ``skyrim/player_ref`` or
  ``vrchat/avatar_id``.
- **SOUL memcells** already store episodic traces with embeddings. Game
  events become memcells tagged with ``environment`` metadata.
- **Chat history cache** can store Rift Vessel turns as synthetic messages.

7.2. Environment Tagging
^^^^^^^^^^^^^^^^^^^^^^^^

Every memory/diary entry from a Rift Vessel should carry:

.. code-block:: python

    {
        "environment": "skyrim",
        "world_state_snapshot": {
            "location": "Whiterun",
            "health": 75,
            "combat_state": True,
        }
    }

This lets the prompt engine filter memories by environment when building
context — a Telegram conversation should not surface Skyrim combat memories
unless cross-environment recall is desired.

7.3. Lived Experiences vs. NPC Lore
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

Critical rule: diary entries from Rift Vessel sessions store **what SyntH
experienced**, not what the game's lore says. If SyntH gets attacked by a
wolf in Skyrim, the diary says:

    "I was attacked by a wolf near Whiterun and fought back."

NOT:

    "As the Dragonborn, I fulfilled my destiny to defeat Alduin."

The distinction is enforced at the adapter level — the Skyrim adapter sends
game events (damage, combat start/end, dialogue options), not narrative
prompts.

8. Skyrim Rift Vessel — Architecture
-------------------------------------

8.1. Adapter Design
^^^^^^^^^^^^^^^^^^^

::

    rift_vessel/skyrim/
    ├── __init__.py          # VESSEL_CLASS = SkyrimVessel, VESSEL_NAME = "skyrim"
    ├── adapter.py           # SkyrimVessel(RiftVesselBase)
    ├── papyrus_bridge.py    # SKSE plugin: Papyrus → WebSocket → SyntH
    ├── schema.py            # Skyrim-specific WorldState shape
    ├── action_map.py        # game_* → Papyrus function mapping
    └── README.md

8.2. Papyrus Bridge (SKSE DLL)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

The SKSE DLL runs inside the Skyrim process:

- **Events pushed to SyntH**: ``OnHit()``, ``OnCombatStateChanged()``,
  ``OnLocationChange()``, ``OnItemEquipped()``, ``OnSpellCast()``,
  ``SayOverride()`` (dialogue).
- **Commands received from SyntH**: ``TranslateTo()``,
  ``EvaluatePackage()``, ``CastSpell()``, ``EquipItem()``,
  ``SetDialogueResponse()``.
- **Transport**: WebSocket client in the DLL connects to a local port
  that the ``SkyrimVessel`` adapter listens on.

8.3. Action Flow
^^^^^^^^^^^^^^^^

::

    SyntH (cognition)
      │ JSON: {"type": "game_attack", "payload": {"target": "wolf_01"}}
      v
    RiftVesselBridge
      │ calls SkyrimVessel.execute_game_action("attack", {"target": "wolf_01"})
      v
    SkyrimVessel
      │ sends WebSocket packet: {"command": "attack", "target": "wolf_01"}
      v
    SKSE DLL
      │ calls actor[wolf_01].StartCombat(player_ref)
      v
    Skyrim Engine

    Later:
    Skyrim Engine → OnCombatStateChanged() → SKSE DLL
      │ sends WebSocket packet: {"event": "combat_ended"}
      v
    SkyrimVessel.on_world_event()
      │ enqueues system message with updated world state
      v
    SyntH cognises: "The wolf is dead. I should continue to Riverwood."

9. Phased Integration Roadmap
-----------------------------

Phase 0 — Foundation (current sprint)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

- [ ] Add ``rift_vessel/`` directory to the project root
- [ ] Implement ``RiftVesselBase`` (abstract class)
- [ ] Implement ``rift_vessel/schema.py`` (WorldState, WorldEvent, etc.)
- [ ] Add ``rift_vessel`` to ``core_initializer._load_plugins()`` search dirs
- [ ] Implement ``RiftVesselBridge`` in core (aggregates adapters, injects
      world state into prompts, routes game actions)
- [ ] Add ``world_state`` to ``PromptRequest`` dataclass
- [ ] Add ``environment`` filter to SOUL memory recall (optional filtering
      by ``interface_path`` prefix)

Phase 1 — Skyrim Minimum Viable Vessel
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

- [ ] Build the SKSE WebSocket DLL (Papyrus → events, WebSocket → commands)
- [ ] Implement ``SkyrimVessel(RiftVesselBase)`` with basic action map
      (``game_attack``, ``game_move``, ``game_talk``, ``game_observe``)
- [ ] Wire world-state push into prompt context
- [ ] Test: "Please defend me from wolves" via Telegram → SyntH → Skyrim

Phase 2 — Memory Continuity
^^^^^^^^^^^^^^^^^^^^^^^^^^^^

- [ ] Rift Vessel experiences flow into ``ai_diary`` automatically via
      the existing Debrief/intent-recovery pipeline
- [ ] Environment-tagged SOUL memcells for Skyrim events
- [ ] Cross-environment recall in prompts (configurable toggle)

Phase 3 — Multi-Environment
^^^^^^^^^^^^^^^^^^^^^^^^^^^^

- [ ] VRChat adapter (OSC + Udon WebSocket bridge)
- [ ] VRChat → SyntH via same ``RiftVesselBase``

Phase 4 — Generalized Environment Embodiment
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

- [ ] Godot SDK/adapter
- [ ] Full action negotiation (``WorldState.possible_actions`` enforcement)
- [ ] WebUI "Rift Vessel" dashboard — shows active vessels, world states,
      recent environment events

10. Decision Boundary Analysis
------------------------------

10.1. Where Cognition Stops
^^^^^^^^^^^^^^^^^^^^^^^^^^^^

| Layer | Owns | Does NOT own |
|-------|------|--------------|
| SyntH Core | Reasoning, intent, memory, identity | Game-specific knowledge, cooldowns, physics |
| RiftVesselBridge | Action routing, world-state schema | Game-specific animation, AI packages |
| Game Adapter | Command translation, event extraction | Narrative, identity, long-term goals |
| Game Engine | Physics, AI packages, dialogue UI | Reasoning, memory, personality |

10.2. Anti-Patterns to Avoid
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

- **ChatGPT-as-NPC**: The game should never control the LLM's prompt to
  inject lore or force roleplay. SyntH always uses its own persona, not
  an NPC's.
- **Cognition in the game process**: The SKSE DLL is a thin bridge. All
  reasoning happens in SyntH.
- **Tight coupling**: The Skyrim adapter should be removable without
  affecting any other subsystem (same as any interface).

11. Constraints and Risks
--------------------------

+----------------------------+------------------------------------------------------+
| Risk                       | Mitigation                                           |
+============================+======================================================+
| **Latency**                | Game actions are async — SyntH does not block.       |
| Game → Papyrus → WS →      | World-state push is periodic, not per-frame.         |
| SyntH → LLM → actions      |                                                      |
+----------------------------+------------------------------------------------------+
| **Token pressure**         | World state is injected as a compact structured       |
| World state in every prompt | block (~200 chars). ``possible_actions`` only lists   |
|                            | what changed.                                         |
+----------------------------+------------------------------------------------------+
| **Embodiment desync**      | ``game_observe`` action forces a full state sync.    |
| SyntH thinks it's at       | Every game action returns updated state.             |
| location X but game says Y |                                                      |
+----------------------------+------------------------------------------------------+
| **Papyrus VM**             | The SKSE DLL runs async network I/O on a separate    |
| blocking the game thread   | thread; Papyrus calls are fire-and-forget via        |
|                            | ``RegisterForSingleUpdate()`` + ``ModEvent``.        |
+----------------------------+------------------------------------------------------+
| **Multiplayer**            | Phase 0–2 are single-player only. VRChat multiplayer |
| (VRChat, co-op)            | requires per-user state tracking (Phase 3+).         |
+----------------------------+------------------------------------------------------+
| **Action safety**          | Game actions go through the same                     |
| LLM tells SyntH to attack  | ``action_safety.py`` / autonomy policy as any other  |
| a friendly NPC             | action. ``RESTRICT_ACTIONS`` mode is honoured.       |
+----------------------------+------------------------------------------------------+
| **Concurrency**            | The message queue already serializes processing.     |
| Game events vs. user msgs  | Rift Vessel events enter as LOW_PRIORITY or          |
|                            | NORMAL_PRIORITY system messages.                     |
+----------------------------+------------------------------------------------------+

12. Implementation Recommendations
----------------------------------

12.1. Start with Phase 0 (this sprint)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

These are pure Python changes with no external dependency:

1. Create ``rift_vessel/`` directory structure
2. Implement ``RiftVesselBase``, ``WorldState``, ``WorldEvent``,
   ``EntityRef``, ``ActionDef``
3. Implement ``RiftVesselBridge`` in core
4. Wire bridge into prompt_engine (``PromptRequest.world_state``)
5. Add ``rift_vessel`` to core_initializer discovery paths
6. Write unit tests for the bridge (using a mock vessel adapter)

12.2. File-by-File Plan (Phase 0)
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

.. code-block::

    NEW: rift_vessel/__init__.py
    NEW: rift_vessel/rift_vessel_base.py    — RiftVesselBase(ABC)
    NEW: rift_vessel/schema.py              — WorldState, WorldEvent, etc.
    NEW: rift_vessel/bridge.py              — RiftVesselBridge

    MOD: core/prompt_request.py             — add world_state field
    MOD: core/prompt_engine.py              — inject world_state into runtime context
    MOD: core/core_initializer.py           — add "rift_vessel" to search_dirs
    MOD: core/action_parser.py              — route game_* actions to bridge
    MOD: core/validation_registry.py        — register game_* action validation

12.3. Key Integration Points
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

| Integration Point | What to Do |
|-------------------|------------|
| ``plugin_instance.handle_incoming_message()`` | The Rift Vessel bridge enqueues ``WorldEvent`` objects as system messages through ``message_queue.enqueue()``, which already routes to ``handle_incoming_message()`` as ``source="interface"``. No changes needed. |
| ``prompt_engine.build_prompt_request()`` | Check if the active ``interface_path`` has an active Rift Vessel world state. If yes, attach it to the prompt. |
| ``action_parser.run_actions()`` | Route ``game_*`` action types to ``RiftVesselBridge.execute_game_action()`` instead of ``Interface.send_message()``. |
| ``message_chain.handle_incoming_message()`` | No changes — game actions are validated and corrected through the same pipeline as chat actions. |
| ``core/soul/`` | When creating a memcell, include ``environment`` metadata from the world state. The existing recall pipeline can filter by environment. |

13. Existing Ecosystem Analysis
-------------------------------

13.1. Herika
^^^^^^^^^^^^

- **Status**: Active Skyrim AI NPC mod
- **What it does**: Replaces NPC dialogue with LLM-generated responses,
  in-game via Papyrus + SKSE
- **Relevance to SyntH**: Embodiment middleware, not cognition. Herika's
  Papyrus event hooks and dialogue injection are a direct reference for
  building the SKSE bridge. However, Herika tightly couples LLM reasoning
  to NPC identity — SyntH must NOT reuse this pattern.

13.2. DwemerDynamics / MinAI
^^^^^^^^^^^^^^^^^^^^^^^^^^^^^

- **What it does**: General AI companion framework for Skyrim
- **Relevance**: Shows that Papyrus → external API communication is feasible.
  The action/event abstraction layer in MinAI is a reference for the
  ``game_*`` action mapping.

13.3. CHIM
^^^^^^^^^^

- **What it does**: Skyrim AI framework by MinAI team
- **Relevance**: Advanced event-driven architecture. CHIM's event bus pattern
  (game events → Lua scripts → API calls) is similar to what
  ``RiftVesselBase.on_world_event()`` provides.

13.4. Key Takeaway
^^^^^^^^^^^^^^^^^^^

None of these systems provide cognition; they provide embodiment middleware.
SyntH should integrate with them at the **event/action transport layer**,
not at the reasoning layer. The SKSE DLL is the only external component
that needs to be built; everything else is orchestrated by SyntH's existing
action pipeline.
