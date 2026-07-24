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

The first shipped connector is a **Minecraft** connector built on a
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
``plugins/rift_vessel/vessel_plugin.py``        Facade ``AIPluginBase`` with
                                                **connection-driven** action exposure (see below).
                                                While disconnected it exposes a single
                                                ``vessel_connect`` entry point whose ``game`` enum
                                                lists every enabled world (plus optional
                                                ``host``/``port`` overrides). Once connected to
                                                world *W* the world-agnostic **core set** —
                                                ``say``, ``move``, ``look``, ``use``, ``attack``,
                                                ``follow``, ``unfollow``, ``respawn``, ``status``
                                                (``say`` takes
                                                an optional ``audio`` flag; all
                                                ``security_level: "low"``, **no**
                                                ``external_effects``) plus each connector's own
                                                ``get_world_actions()`` extras — appears namespaced
                                                ``vessel_<W>_<verb>`` alongside
                                                ``vessel_disconnect``, and ``vessel_connect``
                                                disappears. ``vessel_connect`` loads the chosen
                                                connector, opens a session, and calls
                                                ``connector.connect(settings, on_event)`` to embody
                                                the world; ``vessel_disconnect`` leaves it.
``core/vessel_session_manager.py``              Session lifecycle + experience buffer +
                                                end-of-session flush to a single diary entry.
``interface/vessel_interface.py``               Duck-typed I/O interface. Inbound world
                                                events → salience filter → ``message_queue``;
                                                outbound actions → connector. Scheduler closes
                                                idle sessions.
``plugins/rift_vessel/minecraft/minecraft.py``  Minecraft connector (HTTP
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
        possible_actions=["say", "move", "look", "use", "attack", "follow", "unfollow", "respawn", "status"],
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

The salience filter is intentionally simple and **LLM-free**: a dedup
window (``_DEDUP_WINDOW_SEC = 30 s``) and a rate limiter
(``_RATE_LIMIT_SEC = 2 s``) in :mod:`interface.vessel_interface`. A richer
LLM-driven salience/attention worker (modelled on a Grillo *RAW cognition*
beat, without personality/memory context) is a **future phase** — it must also
respect constraint 1 (no agentic tasks).

Real-time gaming focus
----------------------

The Rift Vessel is primarily used in interactive game worlds, where
responsiveness matters. Two behaviours make embodiment feel like a person who
is *concentrating on the game* rather than multitasking every chat:

**Priority — the game takes top priority.** While at least one embodiment
session is active
(:meth:`core.vessel_session_manager.VesselSessionManager.has_active_session`),
the game is the most urgent thing right now, so
:func:`core.message_queue.enqueue` **raises the Vessel's own in-world
perceptions to** ``HIGH_PRIORITY`` and **deprioritises ordinary chat** from
other interfaces from ``NORMAL_PRIORITY`` down to ``AGENT_PRIORITY``. In-world
perceptions therefore drain first while chat waits. The decision is made
**purely from the message's origin interface and the active-session flag —
never from message text** (project rule: no keyword logic). Two exemptions keep
it safe: urgent/HIGH messages (events) always pass untouched, and the trainer
(``TRAINER_CHAT_ID``) is never deprioritised. When no session is active, queue
behaviour is unchanged. The check is a cheap, DB-free, in-memory flag, so it is
safe to run on every enqueue; the whole block is lazily imported and fully
guarded, so removing the Vessel plugin leaves queueing untouched.

**Context — SyntH isn't omniscient while playing.** A turn that originates from
an embodiment (detected from routing metadata: an ``vessel/...`` interface path,
a ``chat.type == "vessel"`` message, or an explicit ``vessel_focus`` context
flag — **never** from text) is scoped to the world in
:func:`core.history_engine.build_context`: unified cross-interface history is
forced off and the global diary/memory injections are suppressed, keeping only
the persona/profile and the **local** vessel conversation history. Like a real
person absorbed in a game, SyntH does not read every other chat in real time or
notice unrelated global events mid-session — that catch-up happens in the quiet
moments (and at end-of-session, when the experience buffer is flushed to a
single diary entry). The gate is additive and guarded: if detection fails, the
normal full-context path is used.

**Action speed.** In-world actions already run on the Fast Lane as a single
connector round-trip. :meth:`plugins.rift_vessel.vessel_plugin.VesselPlugin.act`
logs that round-trip time at ``INFO`` (``act('...') dispatched via '...' in N
ms``) so latency regressions are visible. Note that the *decision* to act still
costs a full cognition turn; a lightweight reflex/attention layer that reacts
without a full LLM turn is a documented **future phase** and must also respect
constraint 1 (no agentic tasks).

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
``MINECRAFT_BRIDGE_RUN_AT_START``  Optional boot pre-warm (False). The bridge starts **on demand** by default, only when Synth enters the world.
``MINECRAFT_BRIDGE_HOST``       Bridge bind host (``127.0.0.1``)
``MINECRAFT_BRIDGE_PORT``       Bridge HTTP port (``8137``)
``MINECRAFT_SERVER_HOST``       Target MC server host (``127.0.0.1``)
``MINECRAFT_SERVER_PORT``       Target MC server port (``44383``)
``MINECRAFT_BOT_USERNAME_OVERRIDE``  Optional in-world bot name (advanced); empty falls back to ``SYNTH_NAME``
``MINECRAFT_SKIN_FILE``         Uploaded skin texture PNG (file upload in the plugin card), served over HTTP and applied at spawn (needs a server skin plugin)
``MINECRAFT_SKIN_MODEL``        Skin model variant (dropdown): ``classic`` (Steve) or ``slim`` (Alex)
``MINECRAFT_SKIN_PUBLIC_BASE_URL``  Public base URL the MC server uses to fetch the skin (advanced); empty auto-derives from the WebUI host/port
``MINECRAFT_SKIN_COMMAND_TEMPLATE``  Chat command run at spawn (default ``/skin url {url}``; ``{url}``/``{model}`` substituted)
==============================  =========================================

.. note::

   The Minecraft connector no longer has a dedicated ``MINECRAFT_BRIDGE_ENABLED``
   switch. The bridge provisioner is gated by the Minecraft Vessel plugin's own
   enable toggle (``PLUGIN_ENABLED__minecraft_vessel``) — enable/disable the
   whole connector from its card in the WebUI Plugins tab.

.. note::

   The Mineflayer bridge is **not** started at boot. It launches automatically
   the first time the connector connects (i.e. when Synth actually enters the
   world) via ``BridgeProvisioner``, and stays up for the session. Set
   ``MINECRAFT_BRIDGE_RUN_AT_START = True`` only to pre-warm the bridge before
   the first session.

Minecraft
---------

The bridge is a small Node.js process (Mineflayer) exposing a local HTTP API:

* ``GET /health`` → ``{ok, connected, username, environment, mineflayer}``
* ``GET /events`` → ``{events}`` (drains the event buffer)
* ``POST /cmd`` ``{action, payload}`` — ``say``/``move``/``look``/``use``/``attack``/``follow``/``unfollow``/``respawn``/``status``/``skin`` (``follow`` needs ``mineflayer-pathfinder``; without it the action fails gracefully; ``respawn`` calls Mineflayer ``bot.respawn()`` and is guarded to no-op when the bot is already alive)
* ``POST /connect`` / ``POST /disconnect``

``BridgeProvisioner`` manages its lifecycle as a **non-root** subprocess inside
the same container (single-container), gated by the Minecraft Vessel plugin's
enable toggle (``PLUGIN_ENABLED__minecraft_vessel``).
Node.js is **baked into the Docker image by default** (``INSTALL_NODE=true``),
so the Minecraft Vessel works out of the box in Docker. Only non-Docker /
bare-metal deployments need to install Node themselves; a node-free image can be
built explicitly with:

.. code-block:: bash

    docker build --build-arg INSTALL_NODE=false -t synth:slim .

The offline auth mode (``MC_AUTH=offline``) is used; real
Microsoft/XBL auth and multiplayer sync are out of scope.

Skin
~~~~

A real client-side skin upload is **not possible** for an offline-mode
Mineflayer bot: the skin is not carried by the client, it is decided by the
server (by username/UUID or a skin-management plugin). Mineflayer exposes only
read-only skin data and cape/sleeve *visibility* toggles — never the texture.

The supported path is a **server-side skin plugin** (e.g. `SkinsRestorer
<https://skinsrestorer.net/>`_). When one is present, the connector applies the
skin automatically at spawn by running a configurable chat command
(``_apply_skin`` in the connector, forwarded to the bridge ``skin`` action which
calls ``bot.chat``). The skin texture is **uploaded directly** from the plugin
card (``MINECRAFT_SKIN_FILE``, a file-upload exposed variable): SyntH stores the
PNG and serves it at ``<base>/api/config/MINECRAFT_SKIN_FILE/file``, where
``<base>`` is ``MINECRAFT_SKIN_PUBLIC_BASE_URL`` if set, otherwise auto-derived
from the WebUI host/port. At spawn the connector substitutes that URL for
``{url}`` and ``MINECRAFT_SKIN_MODEL`` for ``{model}`` in
``MINECRAFT_SKIN_COMMAND_TEMPLATE`` (default ``/skin url {url}``). The template
is config-driven (no keyword logic) so any skin plugin or locale is supported.
**The MC server must be able to reach that URL** to fetch the texture — set
``MINECRAFT_SKIN_PUBLIC_BASE_URL`` when the server runs on another host. Applying
the skin is best-effort: if ``MINECRAFT_SKIN_FILE`` is empty no command is sent,
and if no skin plugin is installed the command is ignored and the session is
unaffected.

Commands
--------

Two slash commands (trainer-only) drive the subsystem:

.. code-block:: text

    /vessel status
    /minecraft provision start|stop|status|logs [n]

Core + attachable sub-plugins (Grillo-style)
--------------------------------------------

The Rift Vessel follows the same **core + attachable sub-plugins** shape as
G.R.I.L.L.O.: the ``vessel_plugin`` *core* owns the generic ``vessel_*``
actions and the **global** settings (``ACTIVE_VESSEL``, ``VESSEL_SETTINGS``,
``VESSEL_SESSION_COOLDOWN_SEC``), while each world ships as its own attachable
sub-plugin under ``plugins/rift_vessel/<world>/``. A world sub-plugin gets its
own WebUI banner, ``icon.<ext>``, ``guide.md``, and a **separate config
namespace** so its world-specific options are not conflated with the global
Rift Vessel entity. The Minecraft connector lives in
``plugins/rift_vessel/minecraft/minecraft.py`` and ships **both**:

* a ``CONNECTOR_CLASS`` (``MinecraftConnector``) that self-registers on
  ``VESSEL_REGISTRY`` — the actual world driver; and
* a ``PLUGIN_CLASS`` (``MinecraftVesselPlugin``) — a thin, action-less
  ``PluginBase`` that surfaces Minecraft as a first-class, separately
  toggleable WebUI entity and owns the Minecraft-specific config keys under the
  ``minecraft_vessel`` component (``MINECRAFT_BRIDGE_*``, ``MINECRAFT_SERVER_*``,
  ``MINECRAFT_BOT_USERNAME_OVERRIDE``, ``MINECRAFT_SKIN_*``).

Extending — new worlds
---------------------

To add a world, create ``plugins/rift_vessel/<world>/<world>.py``. Provide a
connector (world driver) and — to give the world its own WebUI banner and
config namespace — a thin attachable sub-plugin:

.. code-block:: python

    from core.core_initializer import register_plugin
    from core.plugin_base import PluginBase
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

    class MyWorldVesselPlugin(PluginBase):
        display_name = "My World Vessel"

        def __init__(self) -> None:
            super().__init__()
            # register world-specific config under component="myworld_vessel"
            register_plugin("myworld_vessel", self)

        def get_metadata(self) -> dict:
            return {
                "name": "myworld_vessel",
                "display_name": "My World Vessel",
                "category": "Vessels",
                "icon": "icon.svg",
                "guide": "guide.md",
            }

        def get_supported_actions(self) -> dict:
            return {}  # generic vessel_* actions live in the core plugin

    PLUGIN_CLASS = MyWorldVesselPlugin

The connector automatically inherits the Vessel's world-agnostic **core set**
of verbs (``say``, ``move``, ``look``, ``use``, ``attack``, ``follow``,
``unfollow``, ``respawn``, ``status``), exposed namespaced as ``vessel_<world>_<verb>`` **only
while Synth is connected to that world** (see *Connection-driven action
exposure* below). To add **world-specific** verbs (e.g. Minecraft
``craft``/``mine``, Skyrim ``cast_spell``/``sneak``), override
``get_world_actions()`` on the connector:

.. code-block:: python

    class MyWorldConnector(VesselConnectorBase):
        def get_world_actions(self) -> dict:
            return {
                "cast_spell": {
                    "description": "Cast a spell in {world}.",
                    "required_fields": ["spell"],
                    "optional_fields": [],
                    "security_level": "low",
                    # NEVER declare external_effects — stays on the Fast Lane
                },
            }

The returned mapping is keyed by the bare verb (same schema shape as a plugin's
``get_supported_actions``, minus ``external_effects``); the core plugin
namespaces each key under ``vessel_<world>_`` and dispatches it back to your
connector's ``act(verb, payload)``.

The connector is imported so it self-registers on ``VESSEL_REGISTRY``; the
``PLUGIN_CLASS`` makes the world appear as its own banner. Removing any
connector, sub-plugin, the core plugin, or the interface must never break the
rest of the system (SyntH golden rule).

Connection-driven action exposure
----------------------------------

The Vessel action set is **not static** — it reflects the live connection
state, because ``VesselPlugin.get_supported_actions()`` is a *pure* read that
the core calls on every prompt build/dispatch/validation. The exposed verbs
therefore change automatically on the next prompt, with no restart:

* **Disconnected** — a single entry point ``vessel_connect`` is exposed. Its
  required ``game`` field is an enum of every *enabled* world (a world is
  enabled when its ``<world>_vessel`` sub-plugin is on); optional ``host`` and
  ``port`` override the world's server address for that connect only. No
  gameplay verbs are visible — Synth cannot ``say``/``move``/… before it has
  entered a world.
* **Connected to world W** — the world-agnostic core set (minus ``connect``)
  plus W's own ``get_world_actions()`` extras appear namespaced
  ``vessel_<W>_<verb>``, together with ``vessel_disconnect``. ``vessel_connect``
  disappears while embodied.
* **Logout or inactivity cooldown** (``VESSEL_SESSION_COOLDOWN_SEC``, default
  3600 s) — the session closes, ``has_active_session()`` becomes false, and on
  the very next prompt the gameplay verbs vanish and ``vessel_connect`` returns.
  This is free: the scheduler's ``close_expired_sessions`` drives it and no
  action code runs.
* **No enabled worlds** — the action set is empty, keeping the prompt clean.

Detection is entirely **structural** (no keyword/regex logic): the connected
world is probed via ``vessel_session_manager.has_active_session()`` plus the
cached connector's ``is_connected`` flag, and ``connect``'s ``game`` field is an
enum *value*, never text matched. ``vessel_connect`` accepts either the plain
form or a legacy namespaced ``vessel_<world>_connect``; the world is taken from
the ``game`` field first.

WebUI coherence LED (orange)
----------------------------

Because a world (e.g. Minecraft) can be enabled while the Vessel *core*
(``vessel_plugin``) is disabled — a state in which that world can never actually
connect — the classic WebUI **Plugins** tab shows an **orange** status dot on
any ``Vessels``-category world sub-plugin whose LED would otherwise be green
when ``vessel_plugin`` is not loaded. The tooltip explains that the world cannot
connect until the Rift Vessel plugin is enabled. Enabling the core plugin
restores the normal green/grey LED.
