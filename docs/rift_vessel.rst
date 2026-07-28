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
   Both **inbound** perceptions (``session_start``, ``spawn``, ``chat``,
   ``proximity``, …) **and Synth's own outbound in-world actions** are logged:
   every successful ``vessel_plugin.act()`` writes an ``action_<verb>`` row
   (e.g. ``action_say``, ``action_move``), rendered in the History tab as
   "Action Say", "Action Move", etc. The action summary is built structurally
   from the payload fields — no keyword/content inspection — and this logging is
   audit-only (it never feeds cognition, per constraint 2 above).

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
                                                ``say``, ``move``, ``look``, ``observe``, ``use``,
                                                ``attack``, ``follow``, ``unfollow``, ``respawn``,
                                                ``status`` (``say`` takes
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
``core/vessel_beat.py``                         World-agnostic **autonomous volition** —
                                                pure, keyword-free will-prompt builder + interval
                                                helpers that let Synth play on its own (see
                                                *Autonomous play* below).
``interface/vessel_interface.py``               Duck-typed I/O interface. Inbound world
                                                events → salience filter → ``message_queue``;
                                                outbound actions → connector. Scheduler closes
                                                idle sessions **and** fires the slow will beat +
                                                the fast motor tick.
``plugins/rift_vessel/minecraft/minecraft.py``  Minecraft connector (HTTP
                                                client to the bridge). Self-registers at import.
``plugins/rift_vessel/minecraft/goals.py``      Minecraft **goal store** — persists and recalls
                                                Synth's own free-text goals (no catalogue; see
                                                *Autonomous play* below).
``plugins/rift_vessel/minecraft/minecraft_bridge.js``  Node.js Mineflayer ↔ HTTP
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
        possible_actions=["say", "move", "look", "observe", "use", "attack", "follow", "unfollow", "respawn", "status"],
        flags={"connected": True, "is_day": True},
        extra={
            "username": "Synth",
            # Rich perception + progression, filled by the connector for the
            # autonomous will beat + motor tick (all optional, structural, keyword-free):
            "entities": [{"name": "Steve", "type": "player", "distance": 4.2}],
            "blocks": [{"name": "oak_log", "distance": 3.0}],
            "inventory": [{"name": "oak_log", "count": 5}],
            "affordances": [{"kind": "block", "target": "oak_log", "verb": "mine", "distance": 3.0}],
            # Self-authored goal (free text Synth wrote itself), plus its own recent goals.
            "current_goal": {"id": 3, "description": "explore the caves", "note": "digging down", "status": "active"},
            "recent_goals": [{"description": "build a house by the lake"}],
            "time_of_day": 18000,
        },
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

**Priority — a pure 0–10 numeric ranking (higher = more urgent), with NO
de-prioritisation.** Each message is assigned an absolute urgency from its
**structural origin only** (never from message text, never conditional on
whether a session is active): a real in-world **player** chat →
``PRIORITY_HIGH``; Synth's **own** autonomous perception/will-beat →
``PRIORITY_AMBIENT`` (below every human); the trainer → ``PRIORITY_TRAINER``;
ordinary chat → ``PRIORITY_GENERAL``; an urgent (``priority=True``) message →
``PRIORITY_URGENT``. Ordinary chat is **never demoted** because Synth happens to
be embodied — a person addressing Synth is always answered promptly, and the
game's own perceptions simply sit at a lower rung so they yield to humans. The
whole block is lazily imported and fully guarded, so removing the Vessel plugin
leaves queueing untouched.

**Connection-driven session lifecycle (3 states).** Whether the game generates
work is tied to the **real** connection, tracked by
:meth:`core.vessel_session_manager.VesselSessionManager.has_active_session`.
The interface registers a *liveness probe* into the session manager (keeping
``core/`` free of interface imports) so the flag reflects the connector's actual
``is_connected`` state, not just a DB row:

* **CONNECTED** — a session exists *and* its connector is really connected.
  ``has_active_session()`` is true; will/action/motor beats and perception
  intake run normally.
* **RECONNECTING** — the session still exists but the connector has dropped.
  ``has_active_session()`` reads **false**, so vessel elements are *frozen*
  (no new beats or perceptions enqueued; existing priorities untouched). The
  disconnect sweep retries the connection every tick for
  ``VESSEL_DISCONNECT_GRACE_SEC`` seconds (default 30, clamped 5–3600). A
  successful reconnection flips the connector live again → back to CONNECTED.
* **ENDED** — the connection could not be restored within the grace window.
  The session is closed, flushed to a single diary entry, and **all queued
  vessel traffic for that world is purged** (``drop_vessel_queue_for_world``),
  so nothing stale is dispatched into a dead world.

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
* ``minecraft_goals`` — ``id``, ``session_id``, ``description`` (free text Synth
  authored itself), ``note`` (Synth's own progress reflection), ``status``
  (``active``/``done``/``abandoned``), ``created_at``, ``updated_at``.
  Minecraft-specific (goal store, **no** predefined catalogue); created in
  ``plugins/rift_vessel/minecraft/goals.py`` and seeded in ``init-db.sql``.

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
``VESSEL_AUTONOMY_ENABLED``     Enable autonomous play — all three beats (``False`` default)
``VESSEL_WILL_INTERVAL_SEC``    Seconds between slow volition/will beats (45, clamped 10–3600; falls back to legacy ``VESSEL_BEAT_INTERVAL_SEC``)
``VESSEL_ACTION_BEAT_ENABLED``  Enable the middle "idea → concrete step" action beat (``True`` default)
``VESSEL_ACTION_INTERVAL_SEC``  Seconds between action beats — LLM Fast-Lane turn (20, clamped 3–300)
``VESSEL_MOTOR_ENABLED``        Enable the fast motorics reflex (``True`` default)
``VESSEL_MOTOR_INTERVAL_SEC``   Seconds between fast motor ticks — no LLM (3, clamped 1–60)
``MINECRAFT_BRIDGE_RUN_AT_START``  Optional boot pre-warm (False). The bridge starts **on demand** by default, only when Synth enters the world.
``MINECRAFT_BRIDGE_HOST``       Bridge bind host (``127.0.0.1``)
``MINECRAFT_BRIDGE_PORT``       Bridge HTTP port (``8137``)
``MINECRAFT_SERVER_HOST``       Target MC server host (``127.0.0.1``)
``MINECRAFT_SERVER_PORT``       Target MC server port (``44383``)
``MINECRAFT_BOT_USERNAME_OVERRIDE``  Optional in-world bot name (advanced); empty falls back to ``SYNTH_NAME``
``MINECRAFT_SKIN_FILE``         Uploaded skin texture PNG (file upload in the plugin card), served over HTTP and applied at spawn (needs a server skin plugin)
``MINECRAFT_SKIN_MODEL``        Skin model variant (dropdown): ``classic`` (Steve) or ``slim`` (Alex)
``MINECRAFT_SKIN_PUBLIC_BASE_URL``  Public base URL the MC server uses to fetch the skin (advanced); empty auto-derives from the WebUI host, substituting the machine's LAN IP for a loopback host
``MINECRAFT_SKIN_COMMAND_TEMPLATES``  Newline-separated list of chat commands tried at spawn (advanced); empty tries both built-in provider syntaxes
``MINECRAFT_SKIN_COMMAND_TEMPLATE``  Legacy single-command override (advanced); empty by default
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
* ``POST /cmd`` ``{action, payload}`` — ``say``/``move``/``look``/``use``/``attack``/``follow``/``unfollow``/``respawn``/``status``/``skin`` plus the autonomous-play verbs ``goto``/``scan``/``mine``/``place``/``inventory``/``wander`` (``follow``/``goto``/``mine`` need ``mineflayer-pathfinder`` and ``minecraft-data``; without them the action fails gracefully; ``respawn`` calls Mineflayer ``bot.respawn()`` and is guarded to no-op when the bot is already alive). The ``worldSnapshot`` helper feeds ``get_world_state`` (entities, blocks, inventory, time).
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

The supported path is a **server-side skin provider**. Two are supported out of
the box: the classic `SkinsRestorer <https://skinsrestorer.net/>`_ Bukkit/Spigot
plugin (``/skin url <url>``) and the `SkinRestorer
<https://modrinth.com/mod/skinrestorer>`_ Fabric/Forge/NeoForge/Quilt mod by
Lionarius (``/skin set web <model> "<url>"`` — the URL **must** be wrapped in
double quotes). When one is present, the connector applies the skin
automatically at spawn by running the relevant chat command (``_apply_skin`` in
the connector, forwarded to the bridge ``skin`` action which calls ``bot.chat``).
The skin texture is **uploaded directly** from the plugin card
(``MINECRAFT_SKIN_FILE``, a file-upload exposed variable): SyntH stores the PNG
and serves it at ``<base>/api/config/MINECRAFT_SKIN_FILE/file``, where ``<base>``
is ``MINECRAFT_SKIN_PUBLIC_BASE_URL`` if set, otherwise auto-derived from the
WebUI host — with a loopback host (``127.0.0.1``/``localhost``/``0.0.0.0``)
replaced by the machine's primary LAN IP so a same-LAN server can reach it.

Because different providers use different command syntaxes, the connector
**tries every configured command in turn** at spawn — the server accepts the one
it understands and silently ignores the rest, so it works without any keyword
logic. Resolution order (first non-empty wins): the newline-separated list
``MINECRAFT_SKIN_COMMAND_TEMPLATES`` → the legacy single key
``MINECRAFT_SKIN_COMMAND_TEMPLATE`` → the built-in defaults, which cover **both**
providers (``/skin set web {model} "{url}"`` then ``/skin url {url}``). Each
template substitutes the served URL for ``{url}`` and ``MINECRAFT_SKIN_MODEL``
for ``{model}``. **The MC server must be able to reach that URL** to fetch the
texture — set ``MINECRAFT_SKIN_PUBLIC_BASE_URL`` when the server runs on another
host. Applying the skin is best-effort: if ``MINECRAFT_SKIN_FILE`` is empty no
command is sent, and a failed/ignored command never breaks the session.

.. important::

   **A server-side skin provider is required.** On an offline-mode server the
   only working path to a custom skin is a plugin/mod such as `SkinsRestorer
   <https://skinsrestorer.net/>`_ or the `SkinRestorer
   <https://modrinth.com/mod/skinrestorer>`_ mod (or any provider that
   understands a ``/skin``-style command) installed **on the Minecraft server**.
   Without it every ``/skin …`` command is silently ignored — the client
   (Mineflayer) cannot set the texture. The connector logs ``skin command sent:
   …`` for each attempt even when no provider is present, so those log lines do
   *not* confirm the skin was applied.

   Also make sure the server can actually **reach** the skin URL: when
   ``MINECRAFT_SKIN_PUBLIC_BASE_URL`` is empty the base auto-derives from the
   WebUI host, substituting the machine's LAN IP for a loopback host — which
   covers a same-LAN server automatically but still cannot be opened by a server
   on a different network (a different subnet, a VPN-only peer, a public host).
   In that case set it to the SyntH host's VPN/public IP and verify with
   ``curl -I http://<synth-host-ip>:<port>/api/config/MINECRAFT_SKIN_FILE/file``
   (must return ``200``), then reconnect the Vessel to re-run the skin command.

Commands
--------

Two slash commands (trainer-only) drive the subsystem:

.. code-block:: text

    /vessel status
    /minecraft provision start|stop|status|logs [n]

Autonomous play
---------------

By default a Vessel session is *reactive*: Synth responds to perceptions
(chat, proximity, damage…) but only acts when something reaches it. When
``VESSEL_AUTONOMY_ENABLED`` is on, Synth *plays on its own* — wander, look
around, set and pursue its own goals, gather, build, and interact — while still
obeying the Vessel's hard constraints (Fast Lane only, no Agent Lane/Drones, a
single diary at end-of-session).

**Three speeds: volition (slow, LLM) + action (middle, LLM) + motorics (fast,
reflex).** Autonomy is split into three independently-paced layers so that
*deciding what to want* (slow, deliberate, personality-driven) never bottlenecks
*deciding the next concrete step* (middle), which in turn never bottlenecks
*moving the body* (fast, reactive). All three are driven by the interface
scheduler's fine 10 s tick while a session is active. The middle **action beat**
exists to close the "walks around but never accomplishes anything" gap: the will
beat authors a free-text goal but is forbidden to move or act, while the motor
tick can move but never reads the goal's words — so nothing translated *"gather
wood"* into the concrete verb ``vessel_minecraft_collect_block`` / ``mine`` /
``craft``. The action beat is that translator.

**Will beat — volition (slow, LLM), like G.R.I.L.L.O.** Every
``VESSEL_WILL_INTERVAL_SEC`` seconds (default 45, falling back to the legacy
``VESSEL_BEAT_INTERVAL_SEC``, clamped ``[10, 3600]``) the scheduler
(``interface/vessel_interface.py::_maybe_run_will_beat``) fires a will beat. It:

#. reads the connected world's current ``WorldState`` from the live connector;
#. builds a **structural, keyword-free** volition prompt via
   :mod:`core.vessel_beat` (``build_will_prompt``) — surfacing position,
   health, time, nearby entities/blocks, inventory, affordances, and the
   current/recent goals straight from the ``WorldState`` contract, and framing
   the turn as *will, not motion* (*"your body will move toward it on its
   own"*);
#. enqueues it as a **normal** ``vessel`` message (``chat.type == "vessel"``,
   ``interface_path`` starting with ``vessel/``), so ``build_context`` applies
   the world-scoped context and the core runs one ordinary **Fast-Lane**
   cognition turn in which Synth writes/keeps/updates a free-text goal via
   ``vessel_<world>_set_goal`` / ``vessel_<world>_update_goal``.

This is where Synth's **will and memories** live — the goal is authored from
personality, not a script. No new lane, no Drones, no mid-session diary — the
beat is just another perception-shaped message. ``build_decision_prompt``
remains a backward-compat alias of ``build_will_prompt``.

**Action beat — the "idea → concrete step" translator (middle, LLM).** Every
``VESSEL_ACTION_INTERVAL_SEC`` seconds (default 20, clamped ``[3, 300]``, gated
by ``VESSEL_ACTION_BEAT_ENABLED``, default True) the scheduler
(``interface/vessel_interface.py::_maybe_run_action_beat``) fires a second,
faster LLM beat. Unlike the will beat — which reflects and *authors* the goal
but is explicitly forbidden to move or act — the action beat frames the turn as
*"a moment to actually do something toward your goal"* and asks Synth for exactly
**one** concrete step. It is built by :mod:`core.vessel_beat`
(``build_action_prompt``), returns an empty string (no beat) when there is no
active goal, and — like every autonomy beat — is enqueued as an ordinary
Fast-Lane ``vessel`` message. In that turn Synth picks a concrete world verb
(``vessel_<world>_collect_block`` / ``mine`` / ``craft`` / ``smelt`` / ``place``
/ ``goto`` / ``say``) and may record progress via
``vessel_<world>_update_goal`` with ``advance=true``. This is the layer that
turns a free-text goal into technical actions without any keyword logic (the
mapping is cognition's, not the code's). It respects all three Vessel
constraints: still Fast Lane, still no Agent Lane/Drones, still no mid-session
diary. The player-quiet deferral (``VESSEL_WILL_QUIET_SEC``) applies here too, so
a player addressing Synth in-world is answered reactively instead of being
overridden by an autonomy beat.

**Motor tick — motorics (fast, no LLM).** A separate, much faster loop moves
the body toward the current goal with **no prompt, no cognition turn, no
diary**. Every ``VESSEL_MOTOR_INTERVAL_SEC`` seconds (default 3, clamped
``[1, 60]``, gated by ``VESSEL_MOTOR_ENABLED``, default True) the scheduler
(``interface/vessel_interface.py::_maybe_run_motor_tick``) fetches the active
connector and current goal and calls ``await connector.motor_step(goal)``
**directly** — never enqueuing a message. ``motor_step`` is a pure reflex over
the **structural affordance contract only** (``{kind, target, verb, distance}``,
distance-sorted): it picks the nearest benign affordance (verb ``use`` /
``mine``, hostile ``attack`` skipped), then ``mine`` s a block or ``use`` s an
entity within ``_MOTOR_REACH`` (3.0 m), else ``goto`` s it, else ``wander`` s.
The goal's **already-validated structural fields** (``target_kind`` /
``target_name``, populated by cognition — never free text) may steer *where* the
body walks: when the goal names a block target and that exact block is a live
affordance within reach, the reflex ``mine`` s it (returning
``{"action": "mine", "target": …, "target_kind": "block"}``) instead of standing
next to it re-issuing ``goto`` — the "walks up but never picks anything up" gap.
Entities are never mined (mining is block-only). The reflex still **never reads
the goal's free text**. The base ``VesselConnectorBase.motor_step`` is a no-op
returning ``{"acted": False, "reason": "no_motorics"}``, so a world without
motorics degrades gracefully.

**Structured inventory.** So cognition can judge *how many* of a thing it still
needs without rescanning, ``get_world_state`` aggregates the raw inventory (a
flat list of stacks, where the same id can appear in several stacks) into an
id→total map exposed as ``WorldState.extra["inventory_counts"]`` (via the
``MinecraftConnector._inventory_counts`` helper — plain, fail-safe aggregation,
no keyword logic). The raw ``inventory`` list is still available alongside it.

``core/vessel_beat.py`` is pure and side-effect-free (dataclass **or** dict
input, fail-safe autonomy gating, interval clamp/failsafe on both
``resolve_will_interval`` and ``resolve_motor_interval``, ``is_motor_enabled``)
so it is fully unit-tested without a DB, bridge, or LLM
(``tests/test_vessel_beat.py``); ``MinecraftConnector.motor_step``'s structural
rules are unit-tested in ``tests/test_vessel_minecraft_motor.py``.

**Generic self-awareness — the ``observe`` verb.** The world-agnostic core set
gains an ``observe`` verb: a Fast-Lane, ``external_effects``-free action that
reads the current ``WorldState`` and reports what is around (affordances,
entities, blocks) in character. Because it is generic, every world inherits it
as ``vessel_<world>_observe``. Affordances follow a generic structural contract
— ``{kind, target, verb, distance}`` — built by the connector from the raw
world snapshot, never from keyword matching, so the decision engine stays
world-agnostic.

**Self-authored goals — no catalogue.** *What to play* is entirely Synth's own
call: there is **no** predefined quest list, no goal templates, no
inventory-count progression. If goals were a fixed menu, every Synth would play
Minecraft identically, like a scripted bot — which is exactly what SyntH is not.
Instead ``plugins/rift_vessel/minecraft/goals.py`` is a thin **goal store**: it
only *persists* and *recalls* the free-text goals Synth writes for itself. During
a will beat Synth reads its situation (``observe`` / inventory / world state)
and authors a goal in its own words via ``vessel_minecraft_set_goal`` (required
``description``, optional ``note``) — e.g. *"build a cozy shelter before it gets
dark"* or *"go spelunking and see what I find"*. ``vessel_minecraft_goals``
returns its ``current_goal`` plus ``recent_goals``; ``vessel_minecraft_update_goal``
lets Synth record its own progress ``note`` or mark the goal ``done`` /
``abandoned``. Setting a new goal automatically abandons the previous active one.
Progress is judged by Synth itself from what it perceives — never by an item
counter. The connector still exposes the bridge-backed verbs ``goto``, ``scan``,
``mine``, ``place``, ``inventory``, ``wander`` through ``get_world_actions()`` and
enriches ``WorldState.extra`` with ``current_goal`` / ``recent_goals``. Goals are
persisted in the ``minecraft_goals`` table so a goal survives across beats within
a session. *"Do I go looking for diamonds or build a chest first?"* is Synth's
decision, driven by its personality and wants — not a hardcoded script.

**Game knowledge base — reference facts, never a script.** A Synth that does not
know a world's *rules* plays badly — e.g. it tries to mine iron ore bare-handed
and gets nothing, because it never learned that iron needs at least a stone
pickaxe. To close this gap without turning autonomy into a scripted quest list,
each world may ship a small **knowledge base (KB)**. The *mechanism* is
world-agnostic (the Vessel core renders whatever facts a world supplies); the
*content* is world-specific (the Minecraft adapter owns its own facts). The KB is
strictly **reference**: it states how the world works, it never tells Synth what
to do — the spontaneity rule (self-authored goals, no catalogue) is fully
preserved.

* **Source (Minecraft) — the live wiki, not a curated file.** The Minecraft
  adapter consults the **live** `minecraft.wiki <https://minecraft.wiki>`_ (its
  MediaWiki API is open to bots, no auth) via
  ``plugins/rift_vessel/minecraft/wiki_client.py``. There is **no** hand-written
  fact file. ``wiki_client.lookup(query, limit, *, cache_only=False)`` searches
  for pages matching the query, then for each page serves a **one-time LLM
  summary** — a short EN factual note (*how the game works*, never *what to do*)
  — cached incrementally on disk as
  ``plugins/rift_vessel/minecraft/wiki/cache/<slug>.json``
  (``{title, url, raw_extract, summary, fetched_at}``). Repeated lookups of the
  same page are served straight from cache with no re-fetch and no
  re-summarise. Matching is keyword-free and structural: the ``query`` is
  whitespace-joined game tokens (a goal ``target_name``, block/item ids) matched
  against page-title slugs.
* **Lookup verb.** The connector exposes a Fast-Lane, ``external_effects``-free
  ``lookup_knowledge`` verb (namespaced ``vessel_minecraft_lookup_knowledge``,
  ``required_fields: ["query"]``, ``optional_fields: ["limit"]``,
  ``security_level: "low"``). ``MinecraftConnector.lookup_knowledge(query,
  limit=5, *, cache_only=False)`` delegates to ``wiki_client.lookup`` and returns
  the notes as ``{title, text, url}``. It is fully fail-safe — offline or on any
  error the client returns whatever it has cached (possibly empty) and never
  raises — so a Fast-Lane beat can never break.
* **Beat vs verb split.** The automatic will/motor-beat path
  (``_resolve_knowledge``) calls the lookup with **``cache_only=True``**, so a
  ``WorldState`` build never blocks on the network or the LLM — it serves only
  already-cached pages. The **explicit** ``lookup_knowledge`` verb and the
  goal-expansion Drone use the default live path (``cache_only=False``), which is
  allowed to fetch and summarise. Config:
  ``VESSEL_KNOWLEDGE_LIVE_FETCH`` (bool, default ``True`` — set ``False`` to
  disable all network and stay cache-only everywhere),
  ``VESSEL_KNOWLEDGE_FETCH_TIMEOUT_SEC`` (int, default 4, clamp 1–30) and
  ``VESSEL_KNOWLEDGE_SUMMARY_MAX_CHARS`` (int, default 600, clamp 120–4000). The
  KB is fully offline-testable with the live API and the LLM mocked
  (``tests/test_vessel_knowledge.py``).
* **Prompt injection.** When a beat's ``WorldState.extra["knowledge"]`` is
  populated, ``core/vessel_beat.py::_fmt_knowledge`` renders it into both the
  will and the action prompts as a bulleted **"Game knowledge"** block, headed
  by an explicit *reference, not a script* framing. The renderer is purely
  structural — it never inspects the fact text for keywords — and it drops the
  whole block when nothing renderable survives, keeping empty-KB beats lean.
* **Drone goal expansion.** When Synth authors a *new* goal, a Drone (the
  single-level ephemeral sub-agent, see AGENTS.md §5b) can expand it into an
  ordered list of concrete sub-steps by consulting the KB via
  ``lookup_knowledge`` — turning *"get some iron"* into *"craft a wooden
  pickaxe → mine stone → craft a stone pickaxe → mine iron ore"*. The mapping is
  the Drone's own reasoning over the reference facts; there is no fixed
  expansion table and no keyword routing. **After the goal is updated with its
  sub-steps, it is re-notified to Synth via a will beat**, so the next volition
  turn sees (and can act on) the freshly-expanded plan. The WebUI Goals sub-tab
  renders these sub-steps **collapsed by default** (a ``<details>`` disclosure
  labelled ``Plan · done/total steps``) so a goal card stays compact until the
  user expands it.

Removing autonomy support (the beat module, the goal store, the motor tick, the
knowledge base, or disabling the config flags) must never break the reactive
Vessel — all wiring is lazily imported and fully guarded.

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
of verbs (``say``, ``move``, ``look``, ``observe``, ``use``, ``attack``,
``follow``, ``unfollow``, ``respawn``, ``status``), exposed namespaced as
``vessel_<world>_<verb>`` **only
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
