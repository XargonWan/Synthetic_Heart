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
2. **No diary during a session; one factual operational recap at end-of-session.**
   A session accumulates an in-DB *experience buffer* and an audit trail in
   ``vessel_activity_log``. At **end-of-session** — an explicit logout or after
   ``VESSEL_SESSION_COOLDOWN_SEC`` (default 3600 s) of inactivity — a **single
   factual, third-person operational recap** is written to the dedicated
   ``vessel_diary`` table (``reason = "activity_recap"``), **never** the shared
   ``ai_diary``. The old first-person "lived experience" narrative is removed
   (Decision A): the recap records *what was done in concrete, resumable terms*
   (coordinates/quantities/state) so it is Synth's working memory at the next
   login. It is produced by the dedicated **Rift Vessel Compactor** plugin
   (see :ref:`vessel-compactor`), not an inline task.
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

**Priority — a pure 0–11 numeric ranking (higher = more urgent), with NO
de-prioritisation.** Each message is assigned an absolute urgency from its
**structural origin only** (never from message text, never conditional on
whether a session is active): an emergency → ``PRIORITY_EMERGENCY`` (11); an
urgent (``priority=True``) message or calendar reminder → ``PRIORITY_URGENT``
(10); a reflection-pause turn (``_vessel_reflection``) →
``PRIORITY_REFLECTION`` (9, above player chat so a stop-and-think turn is
consumed before ordinary in-world traffic, yet below urgent/emergency); a real
in-world **player** chat → ``PRIORITY_HIGH`` (8); the trainer →
``PRIORITY_TRAINER`` (7); radio banter → ``PRIORITY_RADIO`` (6, above ordinary
chat); ordinary chat → ``PRIORITY_GENERAL`` (5); Synth's **own** autonomous
perception/will-beat → ``PRIORITY_AMBIENT`` (4, below every human); background
Grillo beats → ``PRIORITY_BACKGROUND`` (2). Ordinary chat is **never demoted**
because Synth happens to be embodied — a person addressing Synth is always
answered promptly, and the game's own perceptions simply sit at a lower rung so
they yield to humans. The whole block is lazily imported and fully guarded, so
removing the Vessel plugin leaves queueing untouched.

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

Sessions & operational recap
----------------------------

``VesselSessionManager`` owns the lifecycle:

* ``start_session(environment, interface_path)`` — reuses an active session for
  the same environment when one exists.
* ``record_experience(session_id, event_type, summary, data)`` — appends to the
  in-DB ``experience_buffer``. **No diary is written here.**
* ``touch(session_id)`` — updates ``last_event_at``.
* ``end_session(session_id, reason)`` — marks the session ``ended`` and triggers
  compaction. If the **Rift Vessel Compactor** plugin has registered a handler
  (via ``set_compaction_handler``), the manager delegates to it — the plugin
  enqueues the session id on its own off-chain low-priority worker to build the
  factual operational recap. If no handler is registered, it falls back to the
  legacy inline ``_launch_compaction``/``_compact_and_store`` path
  (autobiographical, gated by ``VESSEL_DIARY_COMPACTION_ENABLED``). Idempotent.
* ``set_compaction_handler(handler)`` — registration hook mirroring
  ``set_liveness_probe`` so core never imports the plugin.
* ``close_expired_sessions(cooldown_sec)`` — the scheduler ends sessions idle
  longer than ``VESSEL_SESSION_COOLDOWN_SEC``.

.. _vessel-compactor:

Rift Vessel Compactor plugin
----------------------------

The **Rift Vessel Compactor** (``plugins/rift_vessel/vessel_compactor/``) is a
dedicated plugin — a *separate scope* from the G.R.I.L.L.O. Compactor, sharing
only its runnable *shape*. It owns end-of-session compaction:

* **Automatic on ENDED.** At ``start()`` it registers ``_on_session_ended`` via
  ``vessel_session_manager.set_compaction_handler``. When a session ends the
  handler **enqueues** the session id onto the plugin's **internal, off-chain,
  low-priority asyncio worker queue** and returns immediately (teardown never
  blocks on the LLM). This is **not** the message chain — no in-world turn, no
  Agent Lane, no Drone.
* **Manual "Run compaction".** ``get_metadata()`` declares the runnable quartet
  (``run_action = "compact_now"``); the WebUI Plugins tab can trigger it. With no
  payload it compacts the most recently ended session; ``{"session_id": "..."}``
  targets a specific one.
* **Recap builder.** The worker calls
  :func:`core.vessel_diary_compactor.compact_activity_recap`, which reads the
  session's ``vessel_activity_log`` rows, summarises them in chunks into a
  **factual, third-person operational recap** (no persona/first person), folds
  the partials (recursing when oversized) on the vessel-scope Cortex, and stores
  one row in ``vessel_diary`` with ``reason = "activity_recap"``. Fully
  fail-safe (LLM error → deterministic plain-text join; empty log → no entry;
  never raises).
* **Config:** ``VESSEL_COMPACTOR_ENABLED`` (component ``vessel_compactor``,
  default ``True``). Disabling it makes ``end_session`` fall back to the legacy
  inline path.

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
* ``goals`` — ``id``, ``session_id``, ``scope``, ``game``, ``world``,
  ``description`` (free text Synth authored itself), ``note`` (Synth's own
  progress reflection), ``destination``, ``steps`` (JSON), ``current_step``,
  ``target_kind``, ``target_name``, ``status`` (``active``/``done``/
  ``abandoned``), ``created_at``, ``updated_at``. Owned by the **generic Goals
  plugin** (``plugins/goals/goals.py``) — a standalone, *scope-aware* goal store
  that any game, planner, or the Synth itself (personal life goals) can use, not
  just Minecraft. The three-part **scope tuple** (``scope`` / ``game`` /
  ``world``) isolates goal sets: Minecraft goals are pinned to
  ``scope="vessel"`` / ``game="minecraft"`` / ``world="none"``; a personal goal
  uses ``scope="none"``. Goals are still **free text, no predefined catalogue**.
  The legacy ``minecraft_goals`` table is renamed to ``goals`` (and the scope
  columns backfilled) by a startup migration
  (``core/migrations.py::_migrate_goals_table``); seeded in ``init-db.sql``.
  The Minecraft connector reaches the store through a thin compatibility shim
  (``plugins/rift_vessel/minecraft/goals.py``) that forwards every call with the
  Minecraft scope tuple pinned.

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
``VESSEL_REFLECTION_ENABLED``   Enable the deliberate reflection pause (``True`` default)
``VESSEL_REFLECTION_DURATION_SEC``  Reflection window seconds — will/action beats held off, motor keeps moving (15, clamped 3–300)
``VESSEL_REFLECTION_MIN_INTERVAL_SEC``  Anti-thrash floor between two reflection pauses (60, clamped 10–3600)
``VESSEL_SELF_PRESERVATION_ENABLED``  Enable the fast survival reflex on the motor tick (``True`` default)
``VESSEL_SP_LOW_OXYGEN``        Drowning threshold on the 0..20 air-bubble scale (6); NOT air ticks
``VESSEL_SP_LOW_HEALTH``        Health at/below which a fight escalates from defend to flee (6)
``VESSEL_SP_HOSTILE_DIST``      Distance (blocks) within which a hostile mob triggers the reflex (8)
``VESSEL_SP_FIGHT_BACK``        Fight nearby aggressors (``attack``/``shoot``) before fleeing (``True`` default)
``VESSEL_SP_FIGHT_MAX_FAILS``   Consecutive failed fight attempts before escalating to flee (8 — health-primary, keeps fighting while healthy)
``VESSEL_SP_USE_RANGED``        Use a carried bow/crossbow (with ammo) against a distant aggressor via ``shoot`` (``True`` default)
``VESSEL_SP_RANGED_MIN_DIST``   Target distance (blocks) at/above which the reflex prefers ranged over melee (5.0)
``VESSEL_SP_APPRAISAL_ENABLED``  Enable the post-damage appraisal will beat — a ``PRIORITY_URGENT`` Fast-Lane LLM turn on taking damage (``True`` default)
``VESSEL_SP_ENGAGE_RATIO``      Minimum ``own_power / mob_power`` at/above which an **armed** Synth engages instead of fleeing (1.0, clamp 0.2–5.0)
``VESSEL_SP_WEAK_MOB_POWER``    Power floor below which a **disarmed** Synth still punches out a mob bare-handed instead of fleeing (6.0)
``VESSEL_SP_NIGHT_SHELTER``     Enable the priority-6 night-shelter reflex — fully enclose (roofed bed → seal → dig-in) at night with hostiles near (``True`` default)
``VESSEL_SP_SHELTER_DIST``      Distance (blocks) within which a hostile mob at night triggers the shelter reflex (16.0 — wider than ``VESSEL_SP_HOSTILE_DIST`` so the body walls in before a mob closes)
``VESSEL_MORNING_EXIT_ENABLED``  Enable the priority-7 morning surface-exit reflex — if buried with no reachable base, dig a jumpable ``climb_staircase`` back to daylight at day with no open sky (``True`` default)
``VESSEL_GOAL_DEBRIEF_ENABLED``  Enable the goal debrief — a slow structural postflight check that auto-completes a satisfied goal and arms a stall cue (``True`` default)
``VESSEL_GOAL_DEBRIEF_USE_HISTORY``  Also confirm goal completion from the session's ``vessel_activity_log`` (place/mine/kill/craft) when it leaves no lasting inventory trace — structural id match, fail-safe (``True`` default)
``VESSEL_GOAL_DEBRIEF_INTERVAL_SEC``  Seconds between goal-debrief checks (30, clamped 5–3600)
``VESSEL_GOAL_DEBRIEF_STALL_TICKS``  Consecutive unchanged debrief checks before arming a will-beat stall cue (4, clamped 2–100)
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

**Three speeds + a reflection pause: volition (slow, LLM) + action (middle, LLM)
+ motorics (fast, reflex), with a reflection pause (LLM, elevated priority) on
top.** Autonomy is split into three independently-paced layers so that
*deciding what to want* (slow, deliberate, personality-driven) never bottlenecks
*deciding the next concrete step* (middle), which in turn never bottlenecks
*moving the body* (fast, reactive). All three are driven by the interface
scheduler's fine 10 s tick while a session is active. The middle **action beat**
exists to close the "walks around but never accomplishes anything" gap: the will
beat authors a free-text goal but is forbidden to move or act, while the motor
tick can move but never reads the goal's words — so nothing translated *"gather
wood"* into the concrete verb ``vessel_minecraft_collect_block`` / ``mine`` /
``craft``. The action beat is that translator. On top of these three sits the
**reflection pause** (described below): a deliberate stop-and-think turn that
fires when Synth is playing without a real objective, prunes its own pending
autonomous beats, and spends one elevated-priority cognition turn authoring or
refining the goal before ordinary autonomy resumes.

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

**Reflection pause — deliberate stop-and-think (LLM, elevated priority).** On
top of the three speeds sits a *reflection pause*. Because the single message
consumer can be blocked for a long time by a slow, uncancellable Base-Cortex
turn, will beats may pile up unconsumed and Synth can end up aimlessly wandering
with **no goal**. The reflection pause fixes the queue *ordering* of that
situation. Every scheduler tick, *before* the will/action beats, the scheduler
(``interface/vessel_interface.py::_maybe_run_reflection``) checks — from
**structure only, never message text** — whether Synth is playing without a real
objective. When a session is active, autonomy and reflection are enabled
(``VESSEL_AUTONOMY_ENABLED`` and ``VESSEL_REFLECTION_ENABLED``, both default
True), a player has been quiet for ``VESSEL_WILL_QUIET_SEC``, the anti-thrash
floor ``VESSEL_REFLECTION_MIN_INTERVAL_SEC`` (default 60, clamped ``[10, 3600]``)
has elapsed, and Synth has **no active goal or a goal with no step plan**
(``_goal_from_world_state`` / ``_goal_needs_expansion``), it:

#. builds a structural prompt via :mod:`core.vessel_beat`
   (``build_reflection_prompt``) framed as an intentional, *private* pause that
   must **not** speak (no ``say``) — only author/refine the goal via
   ``vessel_<world>_set_goal`` / ``vessel_<world>_update_goal``;
#. enqueues it as a ``vessel`` message tagged ``_vessel_reflection`` →
   ``PRIORITY_REFLECTION`` (9, above player chat), whose enqueue path **prunes
   older pending autonomous vessel beats** for that world
   (``core/message_queue.py::_supersede_pending_vessel_beats`` — which keeps
   player chat and ``no_compact`` items; it never uses
   ``drop_vessel_queue_for_world``, which would also drop player chat);
#. opens a ``_reflecting`` window of ``VESSEL_REFLECTION_DURATION_SEC`` (default
   15, clamped ``[3, 300]``) during which the will and action **beats** are held
   off — but the **motor tick and survival reflex keep running, so the body
   still moves**;
#. on expiry, resets ``_last_will_beat_at = 0.0`` so the will beat re-fires
   immediately and resumes normal autonomy on the freshly-committed goal.

**Known tension (by design).** Clearing the queue removes only *pending* items —
a reflection turn still cannot run until the in-flight (possibly slow selenium)
turn drains, so this fixes queue *order*, not consumer *starvation*. The
reflection pause **complements** the goal-expander Drone; both are kept. It is
keyword-free and fully guarded; the pure prompt and config helpers
(``build_reflection_prompt``, ``is_reflection_enabled``,
``resolve_reflection_duration``, ``resolve_reflection_min_interval``) are
unit-tested in ``tests/test_vessel_beat.py``, and the ``PRIORITY_REFLECTION``
band ordering in ``tests/test_vessel_realtime.py``.

**Structured inventory.** So cognition can judge *how many* of a thing it still
needs without rescanning, ``get_world_state`` aggregates the raw inventory (a
flat list of stacks, where the same id can appear in several stacks) into an
id→total map exposed as ``WorldState.extra["inventory_counts"]`` (via the
``MinecraftConnector._inventory_counts`` helper — plain, fail-safe aggregation,
no keyword logic). The raw ``inventory`` list is still available alongside it.

Self-preservation & combat
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Evaluated **first** on every motor tick (before the no-goal early return, so
Synth reacts even with no active goal), ``MinecraftConnector._survival_threat``
classifies threats from **numeric telemetry + game enum ids only** (never user
text) in strict priority: **dead** → ``respawn``; **drowning** (liquid block id
at head *or* ``is_in_water``, AND ``oxygen <= VESSEL_SP_LOW_OXYGEN``) →
``goto_surface``; **burning** (feet/head on a hot block id) → ``flee``;
**aggressive combat** → defend or escalate to flee.

**Fight all aggressors, not just the nearest, and pre-emptively.** The combat
branch calls ``MinecraftConnector._aggressive_targets(state, near_dist)`` — every
aggressive mob, nearest-first, that either carries the structural ``hostile``
flag within ``VESSEL_SP_HOSTILE_DIST`` **or** is ``is_targeting_me`` at **any**
distance (so a skeleton firing from range, or a mob that has already locked aggro
before closing, is engaged pre-emptively). **Players are never a reflex target**
(``kind == "player"`` is skipped — a human hit is social, handled by the
appraisal beat). The nearest aggressor is latched as ``_fight_target``; on target
change ``_fight_fail_count`` resets.

**Weapon selection is structural.** While ``VESSEL_SP_FIGHT_BACK`` is on AND
``health > VESSEL_SP_LOW_HEALTH`` (health is the *primary* escalation driver) AND
``_fight_fail_count < VESSEL_SP_FIGHT_MAX_FAILS`` (default 8):

#. if ``VESSEL_SP_USE_RANGED`` AND ``has_ranged_weapon`` (a bow/crossbow carried
   with ammo) AND the target distance ``>= VESSEL_SP_RANGED_MIN_DIST`` (5.0) →
   **ranged** via the ``shoot`` verb (native bow firing in the bridge — draw with
   ``activateItem()``, charge, release with ``deactivateItem()`` — no bow plugin);
#. otherwise → **melee** via ``attack``, which equips the highest-damage weapon
   carried (``bestMeleeWeapon()`` in the bridge) and swings a short burst.

Below the health floor, or once the fail cap is hit, the reflex escalates to
``flee``. The bridge ``worldSnapshot`` surfaces the structural combat fields
``has_ranged_weapon`` / ``ranged_ammo`` / ``best_melee_damage`` and tags each
nearby entity with ``is_targeting_me``.

**Power-aware fight-vs-flee (Minecraft adapter scope).** The decision to engage
a mob is not a flat "always defend while healthy" — it is a **structural power
comparison** so a disarmed Synth flees a mob it cannot win, while an
armed/armored one engages. Two numeric helpers on the connector (keyword-free,
telemetry-only) drive it:

* ``_own_power(extra)`` = ``offense * survivability``, where ``offense`` =
  ``best_melee_damage`` (bare-hand floor ``1.0`` when carrying no weapon) and
  ``survivability`` = ``1.0 + armor_points/20 + health/40``. ``armor_points``
  comes from the bridge's ``armorPoints()`` (summed ``_ARMOR_DEFENSE`` per
  equipped piece) and is forwarded into ``WorldState.extra``.
* ``_mob_power(entity)`` = ``max_health * (1 + attack_damage/8)``, using the
  per-entity ``max_health`` / ``attack_damage`` the bridge attaches via
  ``mobCombatStats()`` (falling back to ``_DEFAULT_MOB_POWER`` = 12.0 when a
  mob's stats are unknown).

The gate computes ``ratio = own_power / mob_power``. If the body is **disarmed**
(``_is_disarmed`` — no melee weapon *and* no ranged weapon) it only engages a
**weak** mob (``_mob_power < VESSEL_SP_WEAK_MOB_POWER``, default 6.0); otherwise
``power_ok = ratio >= VESSEL_SP_ENGAGE_RATIO`` (default 1.0). ``power_ok`` (plus
``fight_back``, ``health > low_health`` and the fail-cap) decides defend/shoot vs
flee, and every combat reason dict carries ``own_power`` / ``mob_power`` /
``ratio`` for debugging.

**Per-mob strategy override (Rift Vessel core mechanism + Minecraft content).**
Before the power gate runs, ``_survival_threat`` calls
``apply_combat_strategy(ENVIRONMENT, target, extra)``; a non-``None`` result
short-circuits the reflex with a mob-specific tactic. The **mechanism** is the
world-agnostic core module ``plugins/rift_vessel/vessel_combat_strategy.py`` — it
mirrors ``core/vessel_registry.py``: a ``CombatStrategyRegistry`` keyed
``{world: {entity_id: strategy}}``, a module-level singleton
``combat_strategy_registry``, and the wrappers ``register_combat_strategy`` /
``resolve_combat_strategy`` / ``apply_combat_strategy`` (fail-safe, resolves by
the entity's structural ``name`` id, never display text). The **content** lives
in the Minecraft adapter: ``_mc_strategy_creeper`` and ``_mc_strategy_enderman``
both return a ``keep_distance`` plan (a creeper must never be chased into its
explosion; an enderman is disengaged rather than meleed), registered at import
via ``register_combat_strategy("minecraft", "creeper"/"enderman", …)``. A generic
mob has no registered strategy → ``apply_combat_strategy`` returns ``None`` → the
power gate decides. Adding a special mob for any world is a one-liner
registration; removing the mechanism leaves the power gate intact. Unit-tested in
``tests/test_vessel_survival.py``.

**Damage-appraisal will beat (LLM, elevated priority).** The reflex above is the
*fast* motor response; on top of it a high-priority appraisal beat lets Synth
*think about* the hit in character. The trigger is **taking damage this tick**:
``get_world_state`` computes ``damage_taken`` as the drop in ``health`` since the
previous snapshot (numeric-only) and surfaces it in
``WorldState.extra["damage_taken"]`` — so an unseen/ranged attacker or a trap
also fires the beat. The delta is **single-read** (the baseline advances on every
``get_world_state`` call), so ``_maybe_run_damage_appraisal`` runs **first** in
the scheduler autonomy checks. When ``damage_taken > 0`` and
``VESSEL_SP_APPRAISAL_ENABLED`` (default True), it builds
``core/vessel_beat.py::build_damage_appraisal_prompt`` and enqueues it as an
ordinary Fast-Lane ``vessel`` message tagged ``_vessel_appraisal`` →
``PRIORITY_URGENT`` (superseding older pending autonomous vessel beats) and
``no_compact``. Anti-thrash: at most one appraisal per ``resolve_will_interval``.

**Player vs mob framing (structural).** The bridge attributes each hit with a
time-boxed ``lastDamage`` (``DAMAGE_ATTRIBUTION_WINDOW_MS = 2500``);
``worldSnapshot`` exposes ``damage_from_player`` = true only when the last hit's
source was a **player**, null when stale/environmental.
``build_damage_appraisal_prompt`` branches on it: a **player** hit gets a
*social* framing (do NOT reflexively swing back; consider ``vessel_<world>_say``,
ask/back off/remember), a **mob** hit gets a *combat* framing offering
``vessel_<world>_attack`` and — when a ranged weapon with ammo is carried —
``vessel_<world>_shoot``. Fast-Lane only (no ``external_effects`` → never the
Agent Lane/Drones, no mid-session diary). Unit-tested in
``tests/test_vessel_beat.py`` (prompt) and ``tests/test_vessel_survival.py``
(combat reflex).

**Night shelter (priority 6).** Below drowning/burning/combat: when it is
**not day** (``is_day`` False — structural time telemetry) AND aggressive mobs
are within ``VESSEL_SP_SHELTER_DIST`` (default 16.0, deliberately wider than the
melee ``VESSEL_SP_HOSTILE_DIST`` = 8 so the body walls in *before* a mob closes),
``_survival_threat`` returns ``{"threat":"night_shelter","verb":"shelter"}``. A
torch is **not** enough — Synth must fully enclose: the bridge ``shelter`` verb
tries, in order, a nearby **roofed bed** (``bot.sleep`` → ``method:"bed"``), then
**sealing** the ~10 open cells around the body (``method:"seal"``), then a 1×2
**dig-in** niche as a last resort (``method:"dig_in"``). Gated by
``VESSEL_SP_NIGHT_SHELTER`` (default True).

**Morning surface-exit (priority 7, lowest).** If Synth dug in / walled itself
in overnight and has **no real base**, at dawn it must climb back to daylight
instead of staying buried. When it is **day** (``is_day`` True) AND there is **no
open sky above** (the bridge's ``hasOpenSkyAbove(maxUp)`` → ``sky_access`` False),
and only when a ``_surfaced_last_day`` day-latch has not already fired,
``_survival_threat`` returns ``{"threat":"morning_exit","verb":"climb_staircase"}``.
The bridge ``climb_staircase`` verb digs a **jumpable ascending staircase** — one
block forward + one block up per step, placing the tread — via direct dig/place
(no pathfinder), stopping early once ``skyClear()`` reports open sky. The async
``_run_survival_guard`` gates the actual climb on ``_has_reachable_base(state)``:
if a registered base is within ``_base_retreat_radius`` it just sets the day-latch
and skips (a real base already has an exit); otherwise it dispatches
``climb_staircase``. The day-latch prevents refiring the same day and re-arms at
night. Structural only (numeric time + sky-access bool, never text), Fast Lane,
no diary. Gated by ``VESSEL_MORNING_EXIT_ENABLED`` (default True). Unit-tested in
``tests/test_vessel_survival.py``.

**GOTCHA — oxygen is the 0..20 bubble scale.** At runtime mineflayer
``bot.oxygenLevel`` reports the vanilla 0..20 air-bubble scale (20 = full lungs,
0 = out of air), **not** air ticks — a healthy submerged bot reads ~20, so
``VESSEL_SP_LOW_OXYGEN`` must be on that scale (default 6 ≈ two bubbles left). An
air-ticks threshold would false-fire the drowning reflex constantly.

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
Instead goals live in the **generic Goals plugin** (``plugins/goals/goals.py``),
a standalone *scope-aware* store that only *persists* and *recalls* the free-text
goals Synth writes for itself. It is deliberately **not** Minecraft-specific: the
same store backs any game world, a general planner, or the Synth pursuing a
personal life goal — every goal set is isolated by a three-part **scope tuple**
(``scope`` / ``game`` / ``world``). Minecraft goals are pinned to
``scope="vessel"`` / ``game="minecraft"`` / ``world="none"``; a personal goal uses
``scope="none"``. The Minecraft connector reaches the store through a thin
compatibility shim (``plugins/rift_vessel/minecraft/goals.py``) that forwards
every call with the Minecraft scope tuple pinned, so the historical
``mc_goals.<fn>(...)`` call surface is unchanged.

During a will beat Synth reads its situation (``observe`` / inventory / world
state) and authors a goal in its own words via ``vessel_minecraft_set_goal``
(required ``description``, optional ``note``) — e.g. *"build a cozy shelter
before it gets dark"* or *"go spelunking and see what I find"*.
``vessel_minecraft_goals`` returns its ``current_goal`` plus ``recent_goals``;
``vessel_minecraft_update_goal`` lets Synth record its own progress ``note``, mark
the goal ``done`` / ``abandoned``, or advance its step plan. **A stepped goal
auto-completes** when its ``current_step`` advances past the last step (this was
the fix for goals never being marked ``done`` once all their steps were finished);
a stepless goal is only completed explicitly. Setting a new goal automatically
abandons the previous active one *in the same scope*. Progress is otherwise judged
by Synth itself from what it perceives — never by an item counter. For non-vessel
use the plugin additionally exposes the generic actions ``goal_set`` /
``goal_update`` / ``goal_list`` (security ``low``, no ``external_effects``). The
connector still exposes the bridge-backed verbs ``goto``, ``scan``, ``mine``,
``place``, ``inventory``, ``wander`` through ``get_world_actions()`` and enriches
``WorldState.extra`` with ``current_goal`` / ``recent_goals``. Goals are persisted
in the ``goals`` table so a goal survives across beats within a session.
*"Do I go looking for diamonds or build a chest first?"* is Synth's decision,
driven by its personality and wants — not a hardcoded script.

**Goal debrief — a structural postflight check.** Synth often progresses
*physically* (places a block, kills a mob, crafts a tool) yet never declares the
goal ``done`` — so a slow, structural supervisor (``core/vessel_goal_debrief.py``
+ the wiring in ``interface/vessel_interface.py``) runs every
``VESSEL_GOAL_DEBRIEF_INTERVAL_SEC`` while a session is active, gated by
``VESSEL_GOAL_DEBRIEF_ENABLED`` (default True). It supervises the single active
goal in two ways, **never** reading the goal text as intent:

#. **Deterministic completion.** It first asks the connector's world-owned
   ``evaluate_goal_completion`` / ``complete_active_goal`` hooks whether the live
   world/inventory already satisfies the goal (e.g. the target item is now held).
#. **History-based completion** (gated by ``VESSEL_GOAL_DEBRIEF_USE_HISTORY``,
   default True). When the fast check did **not** satisfy the goal, it additionally
   consults the session's own ``vessel_activity_log`` via the connector's
   ``evaluate_goal_completion_from_history`` hook (Minecraft implemented) and
   auto-completes the goal when a **successful action actually taken this session**
   structurally matches the goal's concrete target — place/mine/collect a block,
   attack/shoot a mob, craft/smelt an item. This closes the gap where a goal is
   fulfilled by an action that leaves **no lasting inventory trace**. Matching is
   purely id-based on the logged target ids (``_HISTORY_TARGET_KEYS``), never a
   text parse, and fully fail-safe (a loader error → unsatisfied).

If neither path completes the goal, the debrief tracks *staleness*: after
``VESSEL_GOAL_DEBRIEF_STALL_TICKS`` consecutive unchanged checks (same goal id +
``current_step`` + ``updated_at``) it arms a will-beat stall cue prompting Synth
to reconsider or change approach. The debrief is structural only, Fast Lane only
(no cognition turn, no diary). The completion hook is a world-agnostic core
contract (``VesselConnectorBase.evaluate_goal_completion_from_history``, default
unsatisfied); the event-class → target-kind mapping and Minecraft ids live in the
adapter. Unit-tested in ``tests/test_vessel_goal_debrief.py``.

**Prefab closed-house build (``build_base``).** The Minecraft ``build_base`` verb
builds a small, fully-enclosed hollow-cube shelter (walls + roof + floor + one
door gap + interior torch + crafting table, optional bed) from a deterministic,
inventory-aware layout — a *model/reference*, not a scripted quest (the
spontaneity rule holds). The layout recipe is
``plugins/rift_vessel/minecraft/base_spec.py::derive_base_layout(origin,
inventory_counts)`` (pure, bounded, structural id-only, fail-open): it emits the
shell **bottom-up — floor → walls → roof** so every cell has a solid neighbour
already placed to click against (the roof, placed last, anchors onto the finished
wall tops — the fix for the earlier "house was not closed" bug). The bridge
``build_base`` case then runs the shell placement plus a bounded **seal pass**
(max 3 idempotent re-attempt rounds over the cells that first failed with
``no-solid-face``, no-progress early-bail) so edge/corner/roof cells that were
floating in air on the first pass get closed once the rest of the shell exists. A
material shortfall surfaces structurally as ``ok=False`` + ``missing`` rather than
dispatching an unbuildable plan. Unit-tested in ``tests/test_base_spec.py``.

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

* **World-agnostic mechanism + per-game sources.** The KB *mechanism* lives in
  the core module ``plugins/rift_vessel/knowledge_client.py``
  (``knowledge_client``) — search, cache, summarise and web-fallback are all
  world-agnostic and driven entirely by adapter-supplied descriptors. A world
  declares its own knowledge source(s) via ``WikiSource`` descriptors returned
  from the connector's ``get_knowledge_wiki_sources()`` hook
  (``VesselConnectorBase`` returns ``[]`` by default). A ``WikiSource`` carries
  only structural, game-specific data: ``name``, ``api_url`` (a MediaWiki
  ``api.php`` endpoint, or ``""`` for a web-only world), ``page_url`` (page-link
  prefix), ``user_agent``, ``game`` (name substituted into the default summary
  prompt) and an optional full ``summary_prompt`` override. **No wiki endpoint
  is hardcoded in core code** — ``knowledge_client.lookup(cache_dir, sources,
  query, limit, *, cache_only=False)`` takes the sources as a parameter.
* **Local-first precedence.** The lookup consults, in order: (1) the **local
  cache** first — offline-safe and instant, and the only tier read when
  ``cache_only`` is set; (2) each **per-game wiki** in declared order — every
  matching page is fetched, summarised once, and cached; (3) a **generic web
  search** as a last resort, *only when no declared wiki matched* — reusing
  ``plugins/web_search/search_engine.py::collect_valid_results``, gated by
  ``VESSEL_KNOWLEDGE_WEB_FALLBACK`` (default ``True``), with results summarised
  and cached exactly like a wiki page. Every tier writes back to ``cache_dir``
  keyed by a slug of the title
  (``{title, url, raw_extract, summary, fetched_at}``), so a fact is fetched at
  most once. Each note is ``{title, text, url}``. Matching is keyword-free and
  structural: the ``query`` is whitespace-joined game tokens (a goal
  ``target_name``, block/item ids) matched against page-title slugs.
* **Minecraft source — the live wiki, not a curated file.** The Minecraft
  adapter ships ``plugins/rift_vessel/minecraft/wiki_client.py``, a thin shim
  that declares the Minecraft ``WikiSource`` (the **live**
  `minecraft.wiki <https://minecraft.wiki>`_ MediaWiki API, no auth) and
  delegates to the core client. There is **no** hand-written fact file.
  ``MinecraftConnector.get_knowledge_wiki_sources()`` returns that descriptor and
  the cache lives at ``plugins/rift_vessel/minecraft/wiki/cache/<slug>.json``.
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
  ``VESSEL_KNOWLEDGE_WEB_FALLBACK`` (bool, default ``True`` — allow the generic
  web-search fallback when no declared wiki matched; ``cache_only`` beats always
  skip it), ``VESSEL_KNOWLEDGE_FETCH_TIMEOUT_SEC`` (int, default 4, clamp 1–30)
  and ``VESSEL_KNOWLEDGE_SUMMARY_MAX_CHARS`` (int, default 600, clamp 120–4000).
  The
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
