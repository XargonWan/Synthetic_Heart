Rift Vessel — Multi-World Embodiment
====================================

.. versionadded:: 2.1

Overview
--------

**SyntH is a persistent cognitive entity; a "Vessel" is a layer of embodiment
into an external world.** The Rift Vessel subsystem lets SyntH *inhabit*
game/virtual worlds (Minecraft, and — architecturally — Skyrim, VRChat,
Hytale) through pluggable **connectors**, while its identity, memory, and
personality persist across every world and every chat interface.

The first shipped connector is a **Minecraft proof-of-concept** built on a
Node.js `Mineflayer <https://github.com/PrismarineJS/mineflayer>`_ bridge.

Design constraints
------------------

Three hard constraints shape the whole subsystem:

1. **Vessel actions never create agentic tasks.** ``vessel_*`` actions declare
   **no** ``external_effects``, so :mod:`core.agent_router` keeps them on the
   **Fast Lane** (direct ``run_actions``). They never touch the Agent Lane,
   ``spawn_drone``, or ``run_agentic_turn``. The connector talks to the world
   directly; no reasoning loop is required. (Each action is still passively
   auto-exposed as an MCP tool, ``synth_vessel_*`` — that is read-only exposure,
   not task creation.)
2. **No diary during a session.** A session accumulates an in-DB
   *experience buffer*. The autobiographical **diary/memory entry is written
   exactly once, at end-of-session** — either an explicit logout or after
   ``VESSEL_SESSION_COOLDOWN_SEC`` (default 3600 s) of inactivity. This mirrors
   how a person remembers "an afternoon of playing", not every footstep.
3. **The Vessel has its own Activities voice.** Like Radio and Grillo, vessel
   events are logged to ``vessel_activity_log`` and surfaced through a dedicated
   ``/api/history/vessel`` endpoint and History sub-tab (with per-item delete).

Architecture
------------

.. code-block:: text

    Game world  →  Connector (Perception)  →  Salience filter  →  message_queue
    (Mineflayer)   PerceptionEvent             (dedup+rate-limit)   (Fast Lane)
                                                                        │
        vessel_* action  ←  VesselPlugin  ←  run_actions  ←  SyntH cognition
             │
             ▼
        Connector.act → world

==============================================  =============================================
File                                            Role
==============================================  =============================================
``core/vessel_registry.py``                     Pluggable connector registry (Iris pattern):
                                                ``VESSEL_REGISTRY``,
                                                ``register_vessel_connector``. A connector module
                                                must expose module-level ``CONNECTOR_CLASS``.
``plugins/rift_vessel/vessel_base.py``          ``VesselConnectorBase`` (ABC) plus the normalised
                                                dataclasses ``WorldState``, ``PerceptionEvent``,
                                                ``VesselActionResult``.
``plugins/rift_vessel/vessel_plugin.py``        Facade ``AIPluginBase`` exposing the actions
                                                ``vessel_say``, ``vessel_move``, ``vessel_look``,
                                                ``vessel_use``, ``vessel_status`` (all
                                                ``security_level: "low"``, **no**
                                                ``external_effects``).
``core/vessel_session_manager.py``              Session lifecycle + experience buffer +
                                                end-of-session flush to a single diary entry.
``interface/vessel_interface.py``               Duck-typed I/O interface. Inbound world
                                                events → salience filter → ``message_queue``;
                                                outbound actions → connector. Scheduler closes
                                                idle sessions.
``plugins/rift_vessel/minecraft/minecraft.py``  Minecraft PoC connector (HTTP
                                                client to the bridge). Self-registers at import.
``interface_dev/minecraft_bridge_minimal.js``   Node.js Mineflayer ↔ HTTP
                                                bridge.
``interface/minecraft_provisioner.py``          ``BridgeProvisioner`` — installs and
                                                controls the bridge subprocess.
==============================================  =============================================

Normalised schema
------------------

``WorldState`` — a snapshot the LLM can reason about:

.. code-block:: python

    WorldState(
        environment="minecraft",
        health=18.0,
        position={"x": 1, "y": 64, "z": -3},
        possible_actions=["say", "move", "look", "use", "status"],
        flags={"connected": True},
        extra={"username": "Synth"},
    )

``PerceptionEvent`` — a condensed cognitive event (never raw telemetry):

.. code-block:: python

    PerceptionEvent(
        environment="minecraft",
        event_type="chat",          # spawn|chat|proximity|damage|death|disconnect
        summary="Steve: hello",
        actor="Steve",
        salience=0.7,
        data={"message": "hello"},
    )

Perception & salience
---------------------

The PoC salience filter is intentionally simple and **LLM-free**: a dedup
window (``_DEDUP_WINDOW_SEC = 30 s``) and a rate limiter
(``_RATE_LIMIT_SEC = 2 s``) in :mod:`interface.vessel_interface`. A richer
LLM-driven salience/attention worker (modelled on a Grillo *RAW cognition*
beat, without personality/memory context) is a **future phase** — it must also
respect constraint 1 (no agentic tasks).

Sessions & lived experience
--------------------------

``VesselSessionManager`` owns the lifecycle:

* ``start_session(environment, interface_path)`` — reuses an active session for
  the same environment when one exists.
* ``record_experience(session_id, event_type, summary, data)`` — appends to the
  in-DB ``experience_buffer``. **No diary is written here.**
* ``touch(session_id)`` — updates ``last_event_at``.
* ``end_session(session_id, reason)`` — flushes the buffer to a **single**
  ``plugins.ai_diary`` "lived experience" entry (tagged with the environment),
  marks the session ``ended``. Idempotent.
* ``close_expired_sessions(cooldown_sec)`` — the scheduler ends sessions idle
  longer than ``VESSEL_SESSION_COOLDOWN_SEC``.

Database
--------

Two tables (created in :mod:`core.db` ``init_vessel_tables`` and seeded in
``init-db.sql``). Time columns use ``created_at`` / ``event_timestamp`` — never
the reserved word ``timestamp``.

* ``vessel_sessions`` — ``session_id``, ``environment``, ``interface_path``,
  ``status`` (``active``/``ended``), ``experience_buffer`` (JSON),
  ``started_at``, ``last_event_at``, ``ended_at``, ``diary_entry_id``.
* ``vessel_activity_log`` — ``id``, ``session_id``, ``interface_path``,
  ``environment``, ``event_type``, ``summary``, ``metadata`` (JSON),
  ``created_at``.

Configuration keys
-------------------

Registered eagerly in :mod:`core.core_initializer` (group/component
``vessel``):

==============================  =========================================
Key                             Purpose
==============================  =========================================
``ACTIVE_VESSEL``               Selected connector (``"disabled"`` default)
``VESSEL_SETTINGS``             JSON connector settings blob
``VESSEL_SESSION_COOLDOWN_SEC`` Idle seconds before a session flushes (3600)
``MINECRAFT_BRIDGE_ENABLED``    Opt-in switch for the Minecraft PoC (False)
``MINECRAFT_BRIDGE_RUN_AT_START``  Auto-start the bridge on boot
``MINECRAFT_BRIDGE_HOST``       Bridge bind host (``127.0.0.1``)
``MINECRAFT_BRIDGE_PORT``       Bridge HTTP port (``8137``)
``MINECRAFT_SERVER_HOST``       Target MC server host (``127.0.0.1``)
``MINECRAFT_SERVER_PORT``       Target MC server port (``25565``)
``MINECRAFT_BOT_USERNAME``      In-world bot name (``Synth``)
==============================  =========================================

Minecraft PoC
-------------

The bridge is a small Node.js process (Mineflayer) exposing a local HTTP API:

* ``GET /health`` → ``{ok, connected, username, environment, mineflayer}``
* ``GET /events`` → ``{events}`` (drains the event buffer)
* ``POST /cmd`` ``{action, payload}`` — ``say``/``move``/``look``/``use``/``status``
* ``POST /connect`` / ``POST /disconnect``

``BridgeProvisioner`` manages its lifecycle as a **non-root** subprocess inside
the same container (single-container PoC), gated by ``MINECRAFT_BRIDGE_ENABLED``.
Node is **not** installed in the default image; build with Node support:

.. code-block:: bash

    docker build --build-arg INSTALL_NODE=true -t synth:mc .

The offline auth mode (``MC_AUTH=offline``) is used for the PoC; real
Microsoft/XBL auth and multiplayer sync are out of scope.

Commands
--------

Two slash commands (trainer-only) drive the subsystem:

.. code-block:: text

    /vessel status
    /minecraft provision start|stop|status|logs [n]

Extending — new worlds
---------------------

To add a connector, subclass ``VesselConnectorBase``, set module-level
``CONNECTOR_CLASS``, and self-register at import time:

.. code-block:: python

    from core.vessel_registry import register_vessel_connector

    class MyWorldConnector(VesselConnectorBase):
        display_name = "My World"
        ...

    CONNECTOR_CLASS = MyWorldConnector
    register_vessel_connector(
        "myworld", __name__,
        capabilities={"chat": True, "movement": True},
        label="My World",
    )

Removing any connector, the plugin, or the interface must never break the rest
of the system (SyntH golden rule).
