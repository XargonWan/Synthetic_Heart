# interface/vessel_interface.py
"""Rift Vessel interface — SyntH's embodied I/O channel for game worlds.

This interface is the bridge between world *connectors* (Minecraft, and future
Skyrim / VRChat / Hytale) and the SyntH message chain. It is deliberately
duck-typed like the other interfaces (no shared base class):

* ``display_name`` / ``get_interface_id()`` / ``get_supported_actions()``
* ``send_message(...)`` for outbound in-world speech/actions
* ``start()`` / ``stop()`` lifecycle
* module-level ``initialize_interface()`` that registers the singleton

**Inbound perception → message chain.** When the active connector reports a
world event, :meth:`VesselInterface.on_world_event` applies a *simple,
language-agnostic* salience filter (dedup + rate-limit, **no LLM**) and, if the
event survives, enqueues a normal message onto ``core.message_queue`` using the
canonical interface path ``vessel/<game>/<server>/<entity>``. The event never
touches the Agent Lane — vessel actions carry no ``external_effects``.

**Lived experience.** Each surviving event is recorded in the current session's
experience buffer (``core.vessel_session_manager``) and logged to
``vessel_activity_log`` for the WebUI Activities tab. No diary/memory is written
mid-session; a single autobiographical entry is flushed when the session ends
(explicit logout or ``VESSEL_SESSION_COOLDOWN_SEC`` inactivity).
"""

from __future__ import annotations

import asyncio
import math
import time
from types import SimpleNamespace
from typing import Any

import aiomysql

from core.config_manager import config_registry
from core.core_initializer import register_interface
from core.db import get_conn_ctx
from core.interface_path_utils import build_interface_path
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.vessel_session_manager import get_vessel_session_manager

INTERFACE_NAME = "vessel"

# Salience filter tuning (simple, language-agnostic — never keyword based).
_DEDUP_WINDOW_SEC = 30.0
_RATE_LIMIT_SEC = 2.0

# Max length of a slugified world-identity token in the interface path.
_MAX_WORLD_TOKEN_CHARS = 48

vessel_interface: "VesselInterface | None" = None


def _slugify_world_token(value: Any) -> str | None:
    """Slugify a connector-supplied world/server identity into a path-safe token.

    The token becomes the ``<world>`` level of ``vessel/<game>/<world>`` and the
    ``world`` scope of the goal store, so it must never contain the path
    separator ``/`` or whitespace. Purely structural: it lowercases, replaces
    every run of non-alphanumeric characters with a single ``_``, trims and
    length-caps. Returns ``None`` for an empty/``None``/whitespace token so the
    caller falls back to the legacy single-scope path (no ``<world>`` level).
    Never inspects semantics — only sanitises structure. Fully fail-safe.
    """
    try:
        raw = str(value or "").strip().lower()
        if not raw:
            return None
        out: list[str] = []
        prev_us = False
        for ch in raw:
            if ch.isalnum():
                out.append(ch)
                prev_us = False
            elif not prev_us:
                out.append("_")
                prev_us = True
        token = "".join(out).strip("_")
        if not token:
            return None
        if len(token) > _MAX_WORLD_TOKEN_CHARS:
            token = token[:_MAX_WORLD_TOKEN_CHARS].strip("_")
        return token or None
    except Exception:  # pragma: no cover - defensive
        return None


class VesselInterface:
    """Embodied game-world interface. Forwards perception into the message
    chain and dispatches outbound actions through the Vessel plugin."""

    display_name = "Rift Vessel"

    # Internal I/O adapter for embodiment — not a user-configurable chat
    # interface. Hidden from the WebUI Interfaces list to avoid a duplicate
    # banner: the user-facing entry is the "Rift Vessel" plugin (Vessels
    # category). See core/webui.py interfaces_data loop.
    hidden_from_ui = True

    def __init__(self) -> None:
        """Create the interface in a stopped, un-connected state."""
        self.is_enabled = False
        self.disabled_reason: str | None = None
        self._scheduler_task: asyncio.Task | None = None
        # salience state
        self._recent_signatures: dict[str, float] = {}
        self._last_enqueue_at: float = 0.0
        # per-event-type rate-limit clocks so a burst of one kind of
        # perception (e.g. movement) never starves reactions of another kind
        # (e.g. ambient chat) — see :meth:`_is_salient`.
        self._last_enqueue_by_type: dict[str, float] = {}
        # active session bookkeeping (session_id -> interface_path)
        self._sessions: dict[str, str] = {}
        # En-route element collection (world-agnostic). While the body travels,
        # the motor tick reads the connected world's *structural* affordance
        # list and remembers every element it has already seen this session,
        # keyed per world scope. Anything genuinely new that turns up mid-trip
        # is surfaced once as a ``sighting`` perception so cognition (the will
        # beat) can decide to divert, revisit it later, or tell other players —
        # exactly the "found something interesting on the way from A to B" case.
        # Novelty is the only structural trigger; whether a sighting is "rare"
        # or "a quest item" is Synth's own judgement, never a hardcoded keyword.
        # Map: world scope (``vessel/<world>``) -> set of ``kind:target`` keys.
        self._seen_elements: dict[str, set[str]] = {}
        # Per-player attack tally (world-agnostic). When Synth takes damage from
        # a real *player* the running count of that player's hits this session
        # is surfaced into the perception summary so cognition can decide how to
        # react (a first hit might be an accident, repeated hits are a pattern).
        # Mob/environmental damage is NOT counted — a hostile mob is simply
        # flagged so ``attack`` is the natural counter-response. The reaction
        # itself is always Synth's own decision, never hardcoded here.
        # Map: world scope (``vessel/<world>``) -> {attacker name -> hit count}.
        self._attack_counts: dict[str, dict[str, int]] = {}
        # Autonomy pacing (monotonic clocks). Volition (the slow "will beat",
        # an LLM cognition turn that sets/updates the goal) and motorics (the
        # fast, prompt-less "motor tick" that steps the body toward the goal)
        # are paced independently — see core.vessel_beat and AGENTS.md §5c.
        self._last_will_beat_at: float = 0.0
        self._last_motor_tick_at: float = 0.0
        # Action beat (the middle "concrete-doing" cognition turn that maps the
        # free-text goal onto a real verb — gather/craft/place — since the
        # reflex must never read the goal text). Paced faster than volition,
        # slower than motorics; see core.vessel_beat and AGENTS.md §5c.
        self._last_action_beat_at: float = 0.0
        # Last time a *player* interacted with Synth in-world (monotonic clock).
        # Updated whenever a salient player-originated perception is enqueued
        # (an in-world ``chat`` line carrying an actor). Used by
        # :meth:`_maybe_run_will_beat` to defer the autonomous "quiet moment to
        # reflect… on your own" volition turn while a player is actively
        # present/talking, so a direct address is answered as a normal reactive
        # chat turn instead of being swallowed by a "you are alone" prompt.
        # Structural (actor-based), never keyword matching.
        self._last_player_activity_at: float = 0.0
        # Reflection-pause bookkeeping. When Synth is playing without a real
        # objective (no goal, or a goal with no step plan) the scheduler stops
        # to think: it prunes its own pending autonomous beats and dedicates one
        # elevated cognition turn to authoring/refining the goal. While the pause
        # is active (``_reflecting`` until ``_reflecting_until``) the slow will
        # beat and the middle action beat are held off — but never the fast
        # motor tick (the body keeps moving). ``_last_reflection_at`` throttles
        # how often a pause may fire (anti-thrash). All monotonic clocks; state,
        # never keyword logic. See core.vessel_beat and AGENTS.md §5c.
        self._reflecting: bool = False
        self._reflecting_until: float = 0.0
        self._last_reflection_at: float = 0.0
        # Post-damage appraisal bookkeeping. When Synth takes damage the
        # connector surfaces a positive ``extra["damage_taken"]`` delta for that
        # one tick. The scheduler then fires a single elevated (PRIORITY_URGENT)
        # cognition turn — "I was just hurt, what do I do?" — on top of the fast
        # survival reflex. ``_last_appraisal_at`` is an anti-thrash floor so a
        # sustained damage stream (e.g. lava) does not enqueue an appraisal on
        # every tick. Monotonic clock; structural, never keyword logic.
        self._last_appraisal_at: float = 0.0
        # Goal debrief bookkeeping. A slow postflight supervisor that watches
        # the single active vessel goal (see core.vessel_goal_debrief and
        # AGENTS.md §5c): it (B) auto-closes a goal the world reports already
        # satisfied by the live state, and (A) surfaces a structural stall cue
        # for the next will beat when a goal sits unchanged for several ticks.
        # ``_last_goal_debrief_at`` paces the check; ``_goal_debrief_stall``
        # holds the caller-owned {"sig", "count"} progress fingerprint; the flag
        # arms the stall cue read by the will beat. All structural, no keywords.
        self._last_goal_debrief_at: float = 0.0
        self._goal_debrief_stall: dict[str, Any] = {"sig": None, "count": 0}
        self._goal_debrief_cue_armed: bool = False
        # Disconnect-grace bookkeeping: environment -> monotonic timestamp when
        # its connector was first observed no longer ``is_connected`` while a
        # session was still active. Once the grace window elapses the session is
        # force-closed so autonomous beats stop and the message flow unblocks.
        self._disconnected_since: dict[str, float] = {}
        # Out-of-band drone planner bookkeeping. When the will beat has run but
        # the active goal still carries neither a structural target nor a
        # numeric destination — so the motor tick can only fall back to the
        # directional march and looks like it is "circling" — a short-lived
        # Drone is dispatched *out of band* (a plain asyncio task, NEVER an
        # in-turn vessel action) to observe the world and translate the idea
        # into a reachable target/destination via the world's own
        # ``set_goal``/``update_goal`` verbs. See AGENTS.md §5c: a vessel turn
        # must never create an agentic task, so the planner runs off the
        # scheduler, not inside cognition. One planner per world at a time.
        self._drone_plan_tasks: dict[str, asyncio.Task[Any]] = {}
        self._last_drone_plan_at: dict[str, float] = {}
        # Out-of-band goal-*expansion* drone bookkeeping (distinct from the
        # anti-circling planner above). When a *new* active goal appears — one
        # this world has not expanded yet — a short-lived Drone is dispatched
        # *out of band* (a plain asyncio task, NEVER an in-turn vessel action,
        # so the vessel chain stays Fast-Lane only, AGENTS.md §5c) to consult
        # the per-world knowledge base and flesh the goal out into an ordered
        # ``steps`` plan via the world's ``update_goal`` verb. After it commits
        # the plan we re-notify Synth with a fresh will beat (by resetting
        # ``_last_will_beat_at``) so the newly detailed goal re-enters volition.
        # De-duplicated per world by the last expanded goal id so a goal is
        # expanded exactly once. One expansion task per world at a time.
        self._goal_expand_tasks: dict[str, asyncio.Task[Any]] = {}
        self._expanded_goal_ids: dict[str, int] = {}
        # Monotonic timestamp of the last goal-expansion *attempt* per world.
        # When a Drone fails to commit a steps plan we clear the de-dup marker
        # so a later tick can retry — but without a cooldown that retry would
        # fire on the *very next* tick, spinning a fresh Drone every second and
        # burning cognition. This gates the retry to VESSEL_GOAL_EXPAND_RETRY_SEC.
        self._last_goal_expand_at: dict[str, float] = {}
        log_debug("[vessel_interface] Instance initialized")

    # ------------------------------------------------------------------
    # Duck-typed interface contract
    # ------------------------------------------------------------------

    @staticmethod
    def get_interface_id() -> str:
        """Return the unique identifier for this interface."""
        return INTERFACE_NAME

    @staticmethod
    def get_supported_actions() -> dict:
        """The Vessel interface declares no actions of its own.

        The embodiment actions (``vessel_connect``, the per-world gameplay
        verbs, ``vessel_disconnect``) live exclusively on the ``vessel_plugin``,
        which is always loaded. The interface is purely an I/O channel that
        forwards world perception events into the message chain.

        Crucially the interface must **not** re-declare the plugin's actions:
        doing so made the core merge the interface name (``vessel``) into each
        action's ``source``, which then caused
        :func:`core.prompt_engine._derive_default_prompt_action_types` to treat
        ``vessel_connect`` as *interface-scoped* and hide it from the prompt on
        every other interface (Telegram, WebUI, …) — the exact interfaces from
        which Synth actually decides to enter a world. Returning an empty set
        keeps the action a plain plugin action, visible everywhere.
        """
        return {}

    async def start(self) -> None:
        """Start the interface and its cooldown scheduler."""
        log_info("[vessel_interface] Starting Vessel interface...")
        self.is_enabled = True
        self.disabled_reason = None
        # Wire the connection-liveness probe into the session manager so
        # ``has_active_session()`` reflects the real 3-state connection model
        # (CONNECTED only when a tracked session's connector is really
        # connected; RECONNECTING/ENDED both read as inactive). Keeps ``core``
        # free of interface deps — the manager only holds an opaque callable.
        try:
            get_vessel_session_manager().set_liveness_probe(self._any_connector_live)
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"[vessel_interface] liveness probe registration failed: {exc}")
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        await self._maybe_autostart_bridge()
        await self._reattach_active_sessions()
        log_info("[vessel_interface] Vessel interface started")

    def _any_connector_live(self) -> bool:
        """Return True if any tracked session's connector is really connected.

        The connection-liveness probe backing
        :meth:`~core.vessel_session_manager.VesselSessionManager.has_active_session`.
        Purely structural (matches each locally-tracked session's world scope to
        a registered connector and reads its ``is_connected`` flag) and fully
        guarded — a lookup failure or absent connector counts as *not* live, so
        the session is treated as RECONNECTING rather than CONNECTED (autonomy
        paused, never dispatched into a dead world). Cheap and synchronous:
        safe to call on the message-queue hot path.
        """
        environments = self._active_session_environments()
        if not environments:
            return False
        connectors = self._connector_environments()
        for environment in environments:
            connector = connectors.get(environment)
            if connector is None:
                continue
            try:
                if bool(getattr(connector, "is_connected", False)):
                    return True
            except Exception:  # pragma: no cover - defensive
                continue
        return False

    async def _maybe_autostart_bridge(self) -> None:
        """Optionally start the Minecraft bridge at boot.

        By default the bridge is started on demand (when Synth actually enters
        the world, via the connector's ``connect()``). This honours the opt-in
        ``MINECRAFT_BRIDGE_RUN_AT_START`` override for deployments that want the
        bridge running before the first session.
        """
        try:
            run_at_start = bool(
                config_registry.get_value("MINECRAFT_BRIDGE_RUN_AT_START", False)
            )
        except Exception:
            run_at_start = False
        if not run_at_start:
            return
        try:
            from interface.minecraft_provisioner import get_bridge_provisioner

            res = await get_bridge_provisioner().start()
            if res.get("ok"):
                log_info("[vessel_interface] Minecraft bridge autostarted at boot")
            else:
                log_info(
                    "[vessel_interface] Minecraft bridge autostart skipped: "
                    f"{res.get('detail')}"
                )
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"[vessel_interface] bridge autostart error: {exc}")

    async def _reattach_active_sessions(self) -> None:
        """Re-embody worlds whose sessions survived a process restart.

        A container/process restart destroys every in-memory connector (and its
        perception poll loop) while leaving the ``vessel_sessions`` row marked
        ``active`` and the world bridge (e.g. the Minecraft Mineflayer bridge)
        still logged into the world. Without this step Synth appears in-world
        but is inert: nothing drains the connector's event stream, so chat and
        world events never reach the message chain.

        For **every** environment with an ``active`` session, ask the Vessel
        plugin to reconnect its connector — regardless of the inactivity
        cooldown. The cooldown governs when an *idle* session is **closed**
        (:meth:`~core.vessel_session_manager.VesselSessionManager.close_expired_sessions`),
        not whether a bot that may still be physically in-world is reattached.
        Filtering the reattach by the cooldown would abandon a bot that is still
        logged into the world after a long downtime, leaving Synth inert (an
        ``active`` DB row with no connector draining its event stream).

        ``connect_world`` is reattach-safe: the freshly-loaded connector reports
        ``is_connected == False``, so it re-runs ``connect()`` (idempotent
        against an already-connected bridge) and restarts the poll loop, while
        :meth:`begin_session` reuses the existing active DB session (so the
        lived experience is continued, not fragmented). If the reattach fails
        (bridge dead, world server unreachable), the stale ``active`` row is
        **closed** so it can never become a ghost that confuses the
        connection-driven action exposure.

        Best-effort and fully guarded: any failure (no plugin, connector load
        error, world disabled) is logged and skipped so it can never break
        interface startup.
        """
        environments: list[str] = []
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT DISTINCT environment FROM vessel_sessions"
                        " WHERE status = 'active'"
                    )
                    rows = await cur.fetchall()
            environments = [
                str(r["environment"]) for r in (rows or []) if r.get("environment")
            ]
        except Exception as exc:
            log_debug(f"[vessel_interface] reattach lookup failed: {exc}")

        try:
            from core.core_initializer import PLUGIN_REGISTRY

            plugin = PLUGIN_REGISTRY.get("vessel_plugin")
        except Exception as exc:
            log_debug(f"[vessel_interface] reattach: plugin lookup failed: {exc}")
            plugin = None
        if plugin is None or not hasattr(plugin, "connect_world"):
            log_debug("[vessel_interface] reattach: vessel_plugin unavailable")
            return

        # Phase 2 — adopt an already-embodied external body that has NO active
        # DB session. A world whose external body (e.g. the Minecraft Mineflayer
        # bridge) stayed logged into the world can outlive its SyntH session:
        # a genuine long drop, or a transient the connector mis-read, ends the
        # session while the bridge keeps the body in-world. Without this Synth
        # is inert forever (no active row → nothing reattaches → beats frozen).
        # For each ENABLED world not already handled above, cheaply probe the
        # external body's liveness (read-only, never starts a bridge); if it is
        # alive and embodied, run ``connect_world`` whose bridge-alive-no-session
        # branch re-opens the session and restarts the poll loop. Best-effort.
        adopt_worlds: list[str] = []
        try:
            enabled = (
                plugin._enabled_worlds() if hasattr(plugin, "_enabled_worlds") else []
            )
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(
                f"[vessel_interface] reattach: enabled-worlds lookup failed: {exc}"
            )
            enabled = []
        for world in enabled:
            if world in environments:
                continue
            try:
                from core.vessel_registry import VESSEL_REGISTRY

                connector = VESSEL_REGISTRY.load_connector(world)
            except Exception as exc:  # pragma: no cover - defensive
                log_debug(
                    f"[vessel_interface] reattach: connector load failed for "
                    f"'{world}': {exc}"
                )
                continue
            probe = getattr(connector, "probe_external_liveness", None)
            if probe is None:
                continue
            try:
                if await probe():
                    adopt_worlds.append(world)
            except Exception as exc:  # pragma: no cover - defensive
                log_debug(
                    f"[vessel_interface] reattach: liveness probe raised for "
                    f"'{world}': {exc}"
                )

        if not environments and not adopt_worlds:
            return

        for environment in adopt_worlds:
            try:
                result = await plugin.connect_world(connector_name=environment)
                if getattr(result, "ok", False) and self._connector_live(environment):
                    log_info(
                        f"[vessel_interface] Adopted already-embodied body in "
                        f"'{environment}' (no active session) after restart"
                    )
                else:
                    detail = getattr(result, "detail", "unknown")
                    log_debug(
                        f"[vessel_interface] Adoption of '{environment}' did not "
                        f"take ({detail})"
                    )
            except Exception as exc:
                log_warning(
                    f"[vessel_interface] adoption failed for '{environment}': {exc}"
                )

        for environment in environments:
            try:
                result = await plugin.connect_world(connector_name=environment)
                # A process/container restart kills the world client/bridge, so
                # the connection to the world is necessarily lost. ``ok`` alone
                # is not proof the bot is back in-world: ``connect_world`` can
                # return ``ok`` against a stale/zombie bridge, or short-circuit
                # on a cached ``is_connected`` flag. Treat the reattach as
                # successful **only** when the freshly-loaded connector reports a
                # real live connection; otherwise the ``active`` row is stale and
                # must be closed so it never becomes a ghost that keeps the chat
                # deprioritised and the will-beat firing after a restart.
                reattached = getattr(result, "ok", False) and self._connector_live(
                    environment
                )
                if reattached:
                    log_info(
                        f"[vessel_interface] Reattached active session in "
                        f"'{environment}' after restart"
                    )
                else:
                    # The bot could not be brought back in-world (bridge dead,
                    # world server unreachable, or the connector is not really
                    # connected). Close the stale active row so it does not
                    # linger as a ghost that keeps gameplay verbs hidden while
                    # looking connected in the DB.
                    detail = getattr(result, "detail", "unknown")
                    log_info(
                        f"[vessel_interface] Reattach failed for '{environment}' "
                        f"({detail}); closing stale session"
                    )
                    await self._close_stale_sessions(environment)
            except Exception as exc:
                log_warning(
                    f"[vessel_interface] reattach failed for '{environment}': {exc}"
                )
                await self._close_stale_sessions(environment)

    def _connector_live(self, environment: str) -> bool:
        """Return True only if ``environment``'s connector is really connected.

        Probes the already-loaded connector instance in the registry (whose
        ``is_connected`` reflects the real socket/bridge state) rather than
        trusting the ``connect_world`` return value alone. Never loads a fresh
        connector and never raises — a lookup failure means "not live".
        """
        try:
            from core.vessel_registry import VESSEL_REGISTRY

            connector = VESSEL_REGISTRY.get_instance(environment)
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(
                f"[vessel_interface] connector liveness lookup failed for "
                f"'{environment}': {exc}"
            )
            return False
        if connector is None:
            return False
        try:
            return bool(getattr(connector, "is_connected", False))
        except Exception:  # pragma: no cover - defensive
            return False

    async def _close_stale_sessions(self, environment: str) -> None:
        """End every active session for ``environment`` that could not reattach.

        Called when :meth:`_reattach_active_sessions` fails to bring the bot
        back in-world. Marks the lingering ``active`` row(s) ``ended`` (via the
        normal end-of-session flush) so the connection-driven action exposure no
        longer sees a phantom session — otherwise gameplay verbs would stay
        hidden while the DB claims Synth is embodied. Best-effort and fully
        guarded: a failure here must never break interface startup.
        """
        manager = get_vessel_session_manager()
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    await cur.execute(
                        "SELECT session_id FROM vessel_sessions"
                        " WHERE environment = %s AND status = 'active'",
                        (environment,),
                    )
                    rows = await cur.fetchall()
            session_ids = [
                str(r["session_id"]) for r in (rows or []) if r.get("session_id")
            ]
        except Exception as exc:
            log_debug(
                f"[vessel_interface] stale-session lookup failed for "
                f"'{environment}': {exc}"
            )
            return

        for session_id in session_ids:
            try:
                await manager.end_session(session_id, reason="reattach_failed")
            except Exception as exc:
                log_warning(
                    f"[vessel_interface] closing stale session {session_id} "
                    f"failed: {exc}"
                )

    async def stop(self) -> None:
        """Stop the scheduler and suspend active sessions across the restart.

        A process/container stop is **not** a world logout: the world persists
        and Synth re-embodies on the next boot via
        :meth:`_reattach_active_sessions`. So instead of ending sessions here —
        which would mark them ``ended`` (making reattach impossible) and flush a
        premature diary entry, leaving the in-world bot inert until the world
        server times it out — each session is *suspended*: kept ``active`` in
        the DB with a refreshed ``last_event_at`` so reattach picks it up. The
        experience buffer is preserved and continues in the same session after
        the restart. Genuine logouts and the inactivity cooldown still end
        sessions normally elsewhere.
        """
        log_info("[vessel_interface] Stopping Vessel interface...")
        if self._scheduler_task is not None:
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except (asyncio.CancelledError, Exception):
                pass
            self._scheduler_task = None

        manager = get_vessel_session_manager()
        for session_id in list(self._sessions.keys()):
            try:
                await manager.suspend_session(session_id)
            except Exception as exc:
                log_warning(f"[vessel_interface] suspend_session on stop failed: {exc}")
        self._sessions.clear()
        self.is_enabled = False

    # ------------------------------------------------------------------
    # Session lifecycle (called by connectors on connect/disconnect)
    # ------------------------------------------------------------------

    async def begin_session(self, environment: str, world: str | None = None) -> str:
        """Open an embodiment session for ``environment`` and return its id.

        ``world`` is the connector-supplied per-world/server identity (see
        :meth:`VesselConnectorBase.get_world_identity`). When present it becomes
        the ``<world>`` level of the canonical interface path
        ``vessel/<game>/<world>`` so progression (goals) is scoped per concrete
        server; when ``None`` the legacy single-scope path ``vessel/<game>`` is
        used. The token is slugified defensively so it is always path-safe.
        """
        world_token = _slugify_world_token(world)
        interface_path = build_interface_path(INTERFACE_NAME, environment, world_token)
        manager = get_vessel_session_manager()
        session_id = await manager.start_session(environment, interface_path)
        self._sessions[session_id] = interface_path
        await self._log_activity(
            session_id=session_id,
            interface_path=interface_path,
            environment=environment,
            event_type="session_start",
            summary=f"Embodied in {environment}",
        )
        return session_id

    async def end_session(self, session_id: str, reason: str = "logout") -> None:
        """End an embodiment session (flushes buffered experience to diary)."""
        manager = get_vessel_session_manager()
        interface_path = self._sessions.get(session_id, "")
        environment = (
            interface_path.split("/")[1] if "/" in interface_path else "unknown"
        )
        await manager.end_session(session_id, reason=reason)
        await self._log_activity(
            session_id=session_id,
            interface_path=interface_path,
            environment=environment,
            event_type="session_end",
            summary=f"Left {environment} ({reason})",
        )
        self._sessions.pop(session_id, None)

    def _tracked_path_for_environment(self, environment: str) -> str | None:
        """Return the interface path of a tracked session for ``environment``.

        Lets a perception that knows only the game (e.g. the en-route sighting
        collector) reuse the exact ``vessel/<game>/<world>`` scope the session
        was opened with, instead of forking a bare ``vessel/<game>`` scope.
        Structural prefix match on the tracked paths; returns the most-specific
        (deepest) match, or ``None`` when nothing is tracked. Fully guarded.
        """
        try:
            prefix = f"{INTERFACE_NAME}/{environment}"
            candidates = [
                path
                for path in self._sessions.values()
                if path == prefix or path.startswith(f"{prefix}/")
            ]
            if not candidates:
                return None
            # Prefer the deepest path so a per-world session wins over a bare
            # game scope when both happen to be tracked.
            return max(candidates, key=lambda p: p.count("/"))
        except Exception:  # pragma: no cover - defensive
            return None

    async def end_sessions_for_environment(
        self, environment: str, reason: str = "logout"
    ) -> int:
        """End all locally-tracked sessions for ``environment``.

        Called by the Vessel plugin when Synth leaves a world so every buffered
        session is flushed to its single autobiographical diary entry. Returns
        the number of sessions closed.
        """
        prefix = f"{INTERFACE_NAME}/{environment}"
        targets = [
            session_id
            for session_id, path in self._sessions.items()
            if path == prefix or path.startswith(f"{prefix}/")
        ]
        for session_id in targets:
            try:
                await self.end_session(session_id, reason=reason)
            except Exception as exc:
                log_warning(
                    f"[vessel_interface] end_sessions_for_environment "
                    f"({environment}) failed for {session_id}: {exc}"
                )
        # Belt-and-braces: also purge the bare world scope in case a perception
        # was queued under ``vessel/<world>`` without a matching tracked session
        # (each ``end_session`` above already purges its own scope). Purely
        # structural (world scope), never message text; fully guarded.
        try:
            from core import message_queue

            message_queue.drop_vessel_queue_for_world(prefix)
        except Exception as exc:
            log_warning(
                f"[vessel_interface] end_sessions_for_environment "
                f"({environment}) queue purge failed: {exc}"
            )
        # Forget the world's en-route sighting registry so the next session
        # starts fresh and rediscovers its surroundings (see
        # :meth:`_collect_en_route_sightings`).
        self._seen_elements.pop(prefix, None)
        # Forget the per-player attack tally so grudges do not carry across
        # sessions (see :meth:`_enrich_damage_summary`).
        self._attack_counts.pop(prefix, None)
        return len(targets)

    def _enrich_damage_summary(
        self,
        interface_path: str,
        summary: str,
        data: dict[str, Any] | None,
    ) -> str:
        """Augment a ``damage`` perception with source + player hit count.

        World-agnostic and fully structural (no keyword logic): the connector
        supplies ``data["attacker"] = {name, source, distance, ...}`` where
        ``source`` is a structural entity classification (``"player"`` for a
        real person, ``"mob"``/other for game entities). We surface:

        * **mob / environmental damage** — flagged as a hostile source so
          ``attack`` is a natural counter-response;
        * **player damage** — accompanied by the running count of that player's
          hits *this session* (accident on the first, a pattern by the third),

        leaving the actual reaction entirely to Synth's cognition. Any failure
        degrades gracefully to the original summary so perception is never lost.
        """
        try:
            attacker = (data or {}).get("attacker")
            if not isinstance(attacker, dict):
                # No attributable source (e.g. fall/environmental damage).
                return summary
            name = str(attacker.get("name") or "").strip()
            source = str(attacker.get("source") or "").strip().lower()
            if source == "player" and name:
                counts = self._attack_counts.setdefault(interface_path, {})
                counts[name] = counts.get(name, 0) + 1
                n = counts[name]
                return (
                    f"{summary} — {name} is a player and has now attacked you "
                    f"{n} time(s) this session. Decide how to react."
                )
            if name:
                return (
                    f"{summary} — {name} is a hostile {source or 'creature'} "
                    "(not a player)."
                )
            return summary
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"[vessel_interface] damage enrichment failed: {exc}")
            return summary

    # ------------------------------------------------------------------
    # Inbound perception
    # ------------------------------------------------------------------

    async def on_world_event(
        self,
        environment: str,
        event_type: str,
        summary: str,
        world: str | None = None,
        entity: str | None = None,
        session_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> bool:
        """Handle one perception event from a connector.

        Applies a simple dedup + rate-limit salience filter, records lived
        experience, logs to the activity log, and — if the event is salient —
        enqueues a normal message onto the SyntH message chain.

        Returns ``True`` if the event was forwarded to the message chain.

        **World-scoped history.** A game world is a single shared space: every
        participant hears the same conversation, so the message-chain scope
        (``interface_path``) is ``vessel/<world>[/<server>]`` — it deliberately
        does **not** include the acting player (``entity``). Scoping per actor
        would fragment the one in-world conversation into disjoint histories and
        — critically — hide players' chat from the autonomous *will beat*, which
        runs on the bare ``vessel/<world>`` scope. The actor's identity is not
        lost: it is carried on the enqueued message's ``from_user`` for
        attribution and mention detection.
        """
        # Prefer the path the open session was created with (so a perception
        # lands on the exact same ``vessel/<game>/<world>`` scope the will beat
        # and goals use); fall back to building one from the world token.
        world_token = _slugify_world_token(world)
        tracked_path = self._sessions.get(session_id) if session_id else None
        # When the caller gave neither a session id nor an explicit world token
        # (e.g. the en-route sighting collector, which only knows the game),
        # reuse any tracked session already open for this ``environment`` so the
        # perception lands on the exact same ``vessel/<game>/<world>`` scope the
        # session was opened with — instead of forking a bare ``vessel/<game>``
        # scope and a second session. Structural prefix match, no keyword logic.
        if tracked_path is None and world_token is None:
            tracked_path = self._tracked_path_for_environment(environment)
        interface_path = tracked_path or build_interface_path(
            INTERFACE_NAME, environment, world_token
        )
        manager = get_vessel_session_manager()

        # Resolve/auto-open a session so experience is always buffered.
        if session_id is None:
            session_id = await manager.start_session(environment, interface_path)
            self._sessions.setdefault(session_id, interface_path)

        # Always record experience + activity (audit is not gated by salience).
        await manager.record_experience(
            session_id=session_id,
            event_type=event_type,
            summary=summary,
            data=data,
        )
        await self._log_activity(
            session_id=session_id,
            interface_path=interface_path,
            environment=environment,
            event_type=event_type,
            summary=summary,
            metadata=data,
        )

        if not self._is_salient(event_type, summary):
            log_debug(
                f"[vessel_interface] Event suppressed by salience filter: {event_type}"
            )
            return False

        # Damage perception: enrich the summary with the attacker source and,
        # for repeat player attackers, the running hit count — so cognition can
        # decide the reaction (counter a mob, weigh talk-vs-attack for a player).
        # The decision is never made here; we only surface the facts.
        if event_type == "damage":
            summary = self._enrich_damage_summary(interface_path, summary, data)

        # Record real-player interaction so the autonomous will beat can defer
        # while a player is actively present. A salient in-world ``chat`` line
        # carrying an actor (``entity``) is a human speaking — this is *not* one
        # of Synth's own synthetic perceptions (sighting/movement/will_beat).
        # Structural (actor + event kind), never keyword matching. Uses the same
        # event-loop clock the will beat compares against.
        if event_type == "chat" and entity:
            try:
                self._last_player_activity_at = asyncio.get_event_loop().time()
            except Exception:  # pragma: no cover - defensive
                pass

        # Persist the perception into the shared world chat history (Bug A).
        # Without this the in-world conversation never lands in
        # ``chat_history_cache`` and so never appears as ``history_current_chat``
        # in the vessel prompt — e.g. a player's "Rekku, mi senti?" was lost.
        # The connector's ``summary`` already carries the actor prefix
        # (e.g. ``"XargonWan: Rekku, mi senti?"``), so we store it verbatim and
        # attribute it to the acting player (falling back to the world itself).
        try:
            from core.chat_context_manager import add_message_to_context
            from plugins.rift_vessel.vessel_base import is_ephemeral_event

            speaker = entity or f"{environment} world"
            speaker_id = entity or environment
            # Structurally mark Synth's *own* synthetic perceptions
            # (sightings/movement/will beats/etc.) as such, so the prompt layer
            # can cap how many of them frame a reactive turn. A real in-world
            # player chat (``chat`` carrying an actor) is NOT marked — it is a
            # human speaking and must always stay in the conversational context.
            # Structural (event kind + actor presence), never keyword matching.
            is_player_chat = event_type == "chat" and bool(entity)
            perception_meta: dict[str, Any] | None = (
                None
                if is_player_chat
                else {"vessel_perception": True, "vessel_event_type": event_type}
            )
            # Separate *pure-log telemetry* (sightings/gather/proximity/spawn/
            # movement/status) from durable *game-experience* (player chat,
            # damage, death, self-monologue, ...). Ephemeral telemetry provides
            # live ambient grounding via the in-memory perception ring but must
            # NOT persist to ``chat_history_cache`` — otherwise the durable
            # vessel history fills with log noise and is re-loaded every
            # restart. A player chat carries an actor and is always durable.
            # Structural (normalized event kind), never keyword matching.
            is_pure_log = is_ephemeral_event(event_type) and not is_player_chat
            await add_message_to_context(
                interface_path=interface_path,
                message_text=summary,
                sender_name=speaker,
                sender_id=speaker_id,
                metadata=perception_meta,
                persist_to_db=not is_pure_log,
            )
        except Exception as exc:
            log_error(f"[vessel_interface] Failed to persist perception to chat: {exc}")

        await self._enqueue_perception(
            interface_path=interface_path,
            summary=summary,
            environment=environment,
            event_type=event_type,
            actor=entity,
        )
        return True

    # Backwards-compatible alias used internally by connectors' callbacks.
    async def _on_world_event(self, *args: Any, **kwargs: Any) -> bool:
        return await self.on_world_event(*args, **kwargs)

    # ------------------------------------------------------------------
    # Outbound
    # ------------------------------------------------------------------

    async def send_message(
        self,
        target: Any = None,
        text: str | None = None,
        **kwargs: Any,
    ) -> bool:
        """Send an outbound in-world message through the active connector.

        Mirrors the other interfaces' ``send_message`` shape (accepts either a
        payload dict or positional target/text). Delegates to the Vessel
        plugin's ``vessel_say`` action.
        """
        if isinstance(target, dict):
            payload = target
            text = payload.get("text", text)

        if not text:
            log_warning("[vessel_interface] send_message called with empty text")
            return False

        try:
            from core.core_initializer import PLUGIN_REGISTRY

            plugin = PLUGIN_REGISTRY.get("vessel_plugin")
            if plugin is None:
                log_warning("[vessel_interface] Vessel plugin not registered")
                return False
            result = await plugin.act("say", {"text": text})
            return bool(getattr(result, "ok", False))
        except Exception as exc:
            log_error(f"[vessel_interface] send_message failed: {exc}")
            return False

    # ------------------------------------------------------------------
    # Salience filter — simple, language-agnostic (NO LLM, NO keywords)
    # ------------------------------------------------------------------

    def _is_salient(self, event_type: str, summary: str) -> bool:
        """Return True if this event should reach the message chain.

        Structural gates only (no natural-language intent matching):

        * **Direct address bypass**: an in-world *chat* line (``event_type ==
          "chat"``) that names Synth — resolved via the persona's own
          name/aliases with :func:`core.mention_utils.is_synth_mentioned`, the
          very same mechanism Telegram/Discord use — is *always* salient. It
          skips the dedup and rate-limit gates so that when a player speaks to
          Synth directly she never misses it under a burst of automatic movement
          perceptions. This is address detection (who is being spoken to), not
          keyword/intent matching, and works in any language because the aliases
          are the user-configured persona names.
        * **Dedup**: identical (event_type, summary) within ``_DEDUP_WINDOW_SEC``
          is dropped.
        * **Rate-limit (per event type)**: at most one forwarded event of a
          given ``event_type`` per ``_RATE_LIMIT_SEC``. The clock is kept
          *per type* rather than globally so a fast stream of one kind of
          perception (e.g. movement) never starves reactions of another kind
          (e.g. ambient chat) — keeping Synth responsive to the world rather
          than pachydermic.

        Note that ambient chat *between other players* (a ``chat`` event that
        does **not** name Synth) stays subject to the normal per-type
        rate-limit, so a lively human conversation never keeps Synth pinned to
        processing lines that are not addressed to her — but it also is never
        drowned out by a movement burst on its own clock.
        """
        now = time.time()

        # Direct address always wins: a player speaking *to* Synth bypasses the
        # dedup/rate-limit gates entirely. Detected structurally by persona
        # name/alias (same as the chat interfaces), never by intent keywords.
        if event_type == "chat":
            try:
                from core.mention_utils import is_synth_mentioned

                if is_synth_mentioned(summary):
                    self._last_enqueue_at = now
                    self._last_enqueue_by_type[event_type] = now
                    return True
            except Exception as exc:  # pragma: no cover - defensive
                log_debug(
                    f"[vessel_interface] mention check failed, "
                    f"falling back to rate-limit: {exc}"
                )

        signature = f"{event_type}\x1f{summary}"

        # prune old signatures
        cutoff = now - _DEDUP_WINDOW_SEC
        self._recent_signatures = {
            sig: ts for sig, ts in self._recent_signatures.items() if ts >= cutoff
        }

        # Damage bypasses the 30s dedup: every hit matters (a repeated attacker
        # is precisely the escalation signal cognition needs). The identical raw
        # summary ("Took damage from X") would otherwise be swallowed and Synth
        # would never learn a player is attacking again. The per-type rate-limit
        # below still applies, so a rapid melee flurry cannot flood the chain.
        # Structural (event kind), never keyword matching.
        if event_type != "damage":
            if self._recent_signatures.get(signature, 0.0) >= cutoff:
                return False
            self._recent_signatures[signature] = now

        # Per-type rate-limit: a burst of one perception kind (e.g. movement)
        # only throttles that same kind, so other kinds (e.g. chat) stay
        # reactive on their own clock.
        if now - self._last_enqueue_by_type.get(event_type, 0.0) < _RATE_LIMIT_SEC:
            return False
        self._last_enqueue_by_type[event_type] = now
        self._last_enqueue_at = now
        return True

    # ------------------------------------------------------------------
    # Message-chain enqueue
    # ------------------------------------------------------------------

    async def _enqueue_perception(
        self,
        interface_path: str,
        summary: str,
        environment: str,
        event_type: str,
        actor: str | None = None,
    ) -> None:
        """Wrap a perception as a normal message and enqueue it (Fast Lane).

        The message-chain *scope* is ``interface_path`` (``vessel/<world>`` —
        the single shared world conversation, see :meth:`on_world_event`).

        **No ``from_user`` attribution (Bug C).** The connector's ``summary``
        already carries the actor prefix (e.g. ``"XargonWan: Rekku, mi senti?"``
        built by the bridge). Carrying the actor *again* on ``from_user`` made
        the history/prompt layer prepend the speaker a second time, producing a
        double prefix (``@XargonWan: XargonWan: ...``). We therefore drop
        ``from_user`` entirely and rely on the prefix baked into the text; the
        ``actor`` argument is kept for signature/back-compat but no longer
        re-attributed.
        """
        try:
            from core import message_queue

            # A salient in-world player chat carrying an actor is a human
            # directly addressing Synth. It must run as its own turn — never
            # coalesced by the queue with autonomous perceptions (sightings) or
            # will-beat prompts that share this ``vessel/<world>`` scope, which
            # would bury the chat and let the "reflect on your own" framing win,
            # so Synth never replies. Structural (event kind + actor presence),
            # never keyword matching.
            is_player_chat = event_type == "chat" and bool(actor)
            # A reflection turn is a deliberate "stop & think about my goal"
            # cognition turn. It must run standalone (never coalesced) and, via
            # the queue's ``vessel_reflection`` band, jump ahead of ordinary
            # in-world player chat while yielding to any real emergency. Purely
            # structural (event kind), never keyword text.
            is_reflection = event_type == "reflection"
            # A damage-appraisal turn is the deliberate "I was just hurt — what
            # do I do?" cognition turn fired right after Synth took damage. It
            # must run standalone (never coalesced) and, via the queue's
            # ``vessel_appraisal`` band, jump ahead of ordinary autonomous play
            # and in-world chat (PRIORITY_URGENT). Structural (event kind), never
            # keyword text.
            is_appraisal = event_type == "damage_appraisal"
            no_compact = is_player_chat or is_reflection or is_appraisal

            wrapped = SimpleNamespace(
                message_id=None,
                chat_id=interface_path,
                interface_path=interface_path,
                text=summary,
                caption=None,
                date=None,
                thread_id=None,
                from_user=None,
                _no_compact=no_compact,
                # Structural signal for the queue: a human speaking to Synth
                # in-world must outrank Synth's own autonomous perceptions
                # (will beats / sightings) that share the same ``vessel/<world>``
                # scope and are produced faster than a slow (e.g. Selenium)
                # engine can consume them — otherwise the player chat starves
                # behind an ever-growing will-beat backlog. Set only for a real
                # player chat; every synthetic perception leaves it False.
                # Structural (event kind + actor presence), never keyword text.
                _vessel_player_chat=is_player_chat,
                # Structural signal for the queue: a reflection turn ranks at
                # PRIORITY_REFLECTION — ahead of ordinary player chat, below any
                # emergency/urgent — and prunes the older autonomous beats for
                # this world so it runs unobstructed. Set only for the reflection
                # perception; everything else leaves it False.
                _vessel_reflection=is_reflection,
                # Structural signal for the queue: a post-damage appraisal turn
                # ranks at PRIORITY_URGENT — ahead of ordinary autonomous play
                # and in-world chat — and prunes the older autonomous beats for
                # this world so it runs unobstructed. Set only for the
                # damage-appraisal perception; everything else leaves it False.
                _vessel_appraisal=is_appraisal,
                chat=SimpleNamespace(
                    id=interface_path,
                    type="vessel",
                    title=f"{environment} ({event_type})",
                    username=None,
                    first_name=None,
                    human_count=0,
                ),
                entities=None,
                reply_to_message=None,
            )
            await message_queue.enqueue(
                self,
                wrapped,
                interface_id=INTERFACE_NAME,
                skip_mention_check=True,
            )
        except Exception as exc:
            log_error(f"[vessel_interface] Failed to enqueue perception: {exc}")

    # ------------------------------------------------------------------
    # Cooldown scheduler + autonomous decision beat
    # ------------------------------------------------------------------

    # Upper bound for the scheduler tick. The loop actually sleeps for the
    # *smaller* of this value and the configured motor interval (see
    # ``_resolve_tick_sec``) so the fast motorics layer stays reactive at its
    # own cadence (``VESSEL_MOTOR_INTERVAL_SEC``, default 3s) instead of being
    # throttled to a coarse fixed tick. The cooldown sweep (once a minute) and
    # the slow will beat (``VESSEL_WILL_INTERVAL_SEC``) are self-throttled by
    # their own timestamp checks, so a finer tick only benefits motion.
    _TICK_SEC = 10.0
    # Never spin faster than this, to keep the idle loop cheap.
    _MIN_TICK_SEC = 1.0

    async def _scheduler_loop(self) -> None:
        """Drive the cooldown sweep and the two autonomy layers.

        The loop ticks every :data:`_TICK_SEC` seconds. It orchestrates three
        independently-paced concerns:

        * the inactivity-cooldown sweep, throttled to roughly once a minute;
        * the **will beat** (volition) — a slow LLM cognition turn, paced by
          ``VESSEL_WILL_INTERVAL_SEC``, that lets Synth set/keep/change its
          free-text goal from its own persona and memories;
        * the **motor tick** (motorics) — a fast, prompt-less body step toward
          the active goal, paced by ``VESSEL_MOTOR_INTERVAL_SEC``.

        Splitting the two keeps volition thoughtful and rare while motion stays
        cheap and reactive (see AGENTS.md §5c).
        """
        last_cooldown_sweep = 0.0
        while True:
            now = asyncio.get_event_loop().time()
            try:
                if now - last_cooldown_sweep >= 60.0:
                    last_cooldown_sweep = now
                    cooldown = int(
                        config_registry.get_value(
                            "VESSEL_SESSION_COOLDOWN_SEC",
                            3600,
                            value_type=int,
                            group="vessel",
                            component="vessel",
                        )
                    )
                    manager = get_vessel_session_manager()
                    await manager.close_expired_sessions(cooldown)
                    # Drop local bookkeeping for sessions no longer active.
                    await self._reap_local_sessions()
                # Safety net (every tick, not throttled): force-close sessions
                # whose connector has dropped, so a hung client can't keep
                # ``has_active_session()`` true and pile up autonomous beats.
                await self._close_disconnected_sessions()
                # Autonomous play: only while a session is active and enabled.
                # Reflection pause first: if Synth is playing without a real
                # objective, stop and dedicate one elevated turn to sorting out
                # the goal. While a pause is in flight the will/action beats are
                # held off inside their own gates (the motor tick keeps moving).
                # Post-damage appraisal FIRST: if Synth just took damage, fire
                # one elevated (URGENT) cognition turn to decide how to respond
                # (fight smart / disengage / respond socially). The fast survival
                # reflex already reacted mechanically on the motor tick; this is
                # the deliberate combat/social judgement on top. It must run
                # first because the ``extra["damage_taken"]`` delta is consumed
                # by the *first* ``get_world_state`` read of the tick (the read
                # advances the connector's ``_last_health`` baseline), so any
                # earlier beat that reads the world state would clear it.
                await self._maybe_run_damage_appraisal()
                await self._maybe_run_reflection()
                # Volition (may set a fresh goal), then motorics acts on it.
                await self._maybe_run_will_beat()
                # If volition left the goal without a reachable target/destination,
                # translate the idea into a concrete waypoint out of band (Drone).
                await self._maybe_run_drone_planner()
                # When a fresh goal appears, expand it into an ordered ``steps``
                # plan out of band (Drone consulting the knowledge base), then
                # re-notify Synth with a will beat. Runs once per new goal id.
                await self._maybe_run_goal_expander()
                # Concrete-doing cognition: map the free-text goal onto a real
                # verb (gather/craft/place) — what actually accomplishes work.
                await self._maybe_run_action_beat()
                # Goal supervision (slow postflight): deterministically close a
                # goal already satisfied by the live world/inventory, and arm a
                # will-beat cue when a goal has sat unchanged too long. Closes
                # the gap where Synth progresses physically but never completes.
                await self._maybe_run_goal_debrief()
                await self._maybe_run_motor_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_warning(f"[vessel_interface] scheduler tick failed: {exc}")
            await asyncio.sleep(self._resolve_tick_sec())

    def _resolve_tick_sec(self) -> float:
        """Sleep interval for the scheduler loop.

        Returns the smaller of :data:`_TICK_SEC` and the configured motor
        interval (clamped to at least :data:`_MIN_TICK_SEC`) so the fast
        motorics layer can run at ``VESSEL_MOTOR_INTERVAL_SEC`` cadence instead
        of being capped by the coarse fixed tick. Fully guarded — any failure
        falls back to :data:`_TICK_SEC`.
        """
        try:
            from core import vessel_beat

            motor_interval = float(
                vessel_beat.resolve_motor_interval(
                    lambda key, default: config_registry.get_value(
                        key, default, group="vessel", component="vessel"
                    )
                )
            )
        except Exception:
            return self._TICK_SEC
        return max(self._MIN_TICK_SEC, min(self._TICK_SEC, motor_interval))

    async def _maybe_run_reflection(self) -> None:
        """Stop and think when Synth is playing without a real objective.

        The deliberate *pause & reflect* turn (AGENTS.md §5c). When Synth is
        wandering without a real goal — no active goal at all, or a goal that
        still has no concrete ``steps`` plan — this fires **one** elevated
        cognition turn dedicated to sorting out *what to do*: author a fresh
        goal, or make the current one actionable. It is ranked at
        :data:`core.message_queue.PRIORITY_REFLECTION` (ahead of ordinary
        in-world player chat, below any emergency) and, on enqueue, prunes the
        older pending autonomous beats for this world so it runs unobstructed.

        Fully guarded. Gated: (1) ``VESSEL_AUTONOMY_ENABLED`` on; (2) a session
        active; (3) ``VESSEL_REFLECTION_ENABLED`` on; (4) not already reflecting;
        (5) the goal is actually missing or step-less (structural, never keyword
        text); (6) no player active within the will-quiet window (a player is
        answered reactively first); (7) the anti-thrash floor
        ``VESSEL_REFLECTION_MIN_INTERVAL_SEC`` has elapsed.

        Crucially this does NOT gate the fast motor tick — the body keeps moving
        during the pause; only the slow will beat and middle action beat are
        held off (inside their own gates) for ``VESSEL_REFLECTION_DURATION_SEC``.
        On resume the will-beat clock is reset so volition re-enters promptly
        with the freshly-authored goal.
        """
        try:
            from core import vessel_beat
        except Exception:
            return

        def _cfg(key: str, default: Any) -> Any:
            return config_registry.get_value(
                key, default, group="vessel", component="vessel"
            )

        now = asyncio.get_event_loop().time()

        # Clear an expired pause and re-prime volition so it re-enters promptly
        # with whatever goal the reflection turn just committed.
        if self._reflecting and now >= self._reflecting_until:
            self._reflecting = False
            self._last_will_beat_at = 0.0

        if not vessel_beat.is_autonomy_enabled(_cfg):
            return
        if not vessel_beat.is_reflection_enabled(_cfg):
            return

        manager = get_vessel_session_manager()
        if not manager.has_active_session():
            return

        # Already thinking — let the current pause run its course.
        if self._reflecting and now < self._reflecting_until:
            return

        # Anti-thrash: never fire two pauses back-to-back.
        min_interval = vessel_beat.resolve_reflection_min_interval(_cfg)
        if now - self._last_reflection_at < min_interval:
            return

        # A player addressing Synth in-world is answered reactively first; do
        # not pre-empt them with a private reflection. Structural (actor-based),
        # never keyword matching; ``0`` disables the deferral.
        quiet_sec = vessel_beat.resolve_will_quiet_sec(_cfg)
        if quiet_sec > 0 and now - self._last_player_activity_at < quiet_sec:
            return

        world, world_state = await self._read_active_world_state()
        if world is None or world_state is None:
            return

        # Structural trigger: reflect only when there is no real objective yet —
        # no goal, or a goal with no ordered step plan. Never inspects the goal's
        # free-text description (no keyword logic).
        goal = self._goal_from_world_state(world_state)
        if goal is not None and not self._goal_needs_expansion(goal):
            return

        interface_path = self._decision_interface_path(world)
        if interface_path is None:
            return

        prompt = vessel_beat.build_reflection_prompt(world_state, world)
        duration = vessel_beat.resolve_reflection_duration(_cfg)
        self._reflecting = True
        self._reflecting_until = now + duration
        self._last_reflection_at = now
        log_debug(
            f"[vessel_interface] Reflection pause for '{world}' "
            f"(duration={duration:.0f}s, "
            f"goal={'missing' if goal is None else 'step-less'})"
        )
        await self._enqueue_perception(
            interface_path=interface_path,
            summary=prompt,
            environment=world,
            event_type="reflection",
        )

    async def _maybe_run_will_beat(self) -> None:
        """Enqueue a **volition** cognition turn when it is due (slow layer).

        Fully guarded so a failure here never breaks the scheduler. Gated three
        ways: (1) ``VESSEL_AUTONOMY_ENABLED`` must be on; (2) a Vessel session
        must be active (cheap in-memory check); (3) at least
        ``VESSEL_WILL_INTERVAL_SEC`` (falling back to the legacy
        ``VESSEL_BEAT_INTERVAL_SEC``) must have elapsed since the last beat.

        The beat reads the connected world's :class:`WorldState`, builds a
        structural (keyword-free) *volition* prompt via :mod:`core.vessel_beat`,
        and enqueues it as a normal ``vessel`` message. The core then runs a
        single ordinary Fast-Lane cognition turn in which Synth decides what it
        *wants* — setting or updating its free-text goal — without planning
        every physical step (the motor tick handles motion). No Agent Lane, no
        diary.
        """
        try:
            from core import vessel_beat
        except Exception:
            return

        def _cfg(key: str, default: Any) -> Any:
            return config_registry.get_value(
                key, default, group="vessel", component="vessel"
            )

        if not vessel_beat.is_autonomy_enabled(_cfg):
            return

        manager = get_vessel_session_manager()
        if not manager.has_active_session():
            return

        now = asyncio.get_event_loop().time()
        # Hold off volition while a reflection pause is in flight: the elevated
        # reflection turn is authoring/refining the goal, so a competing "quiet
        # moment" will beat would only muddy it. The fast motor tick is NOT
        # gated (the body keeps moving during the pause).
        if self._reflecting and now < self._reflecting_until:
            return

        interval = vessel_beat.resolve_will_interval(_cfg)
        if now - self._last_will_beat_at < interval:
            return

        # Defer the volition turn while a player is actively present. The will
        # beat is framed as "a quiet moment to reflect… on your own" (see
        # core.vessel_beat.build_will_prompt); firing it right after a player
        # addressed Synth made her ignore the player — the turn ran on the
        # "you are alone" prompt while the player's chat sat only in scrollback.
        # If a real player interacted within the quiet window, skip this beat so
        # the enqueued chat is handled as an ordinary reactive turn. Structural
        # (actor-based), never keyword matching; ``0`` disables the deferral.
        quiet_sec = vessel_beat.resolve_will_quiet_sec(_cfg)
        if quiet_sec > 0 and now - self._last_player_activity_at < quiet_sec:
            log_debug(
                "[vessel_interface] Will beat deferred: player active "
                f"{now - self._last_player_activity_at:.0f}s ago "
                f"(quiet window {quiet_sec}s)"
            )
            return

        world, world_state = await self._read_active_world_state()
        if world is None or world_state is None:
            return

        interface_path = self._decision_interface_path(world)
        if interface_path is None:
            return

        # If the goal debrief armed a stall cue, surface it on this turn's world
        # state so ``build_will_prompt`` can nudge Synth to reconsider a goal
        # that has sat unchanged (e.g. one already completable with what it
        # holds). Consumed once — disarm after arming the flag on ``extra``.
        if self._goal_debrief_cue_armed:
            try:
                from core.vessel_goal_debrief import STALL_FLAG_KEY

                extra = getattr(world_state, "extra", None)
                if isinstance(extra, dict):
                    extra[STALL_FLAG_KEY] = True
            except Exception:  # pragma: no cover - defensive
                pass
            self._goal_debrief_cue_armed = False

        prompt = vessel_beat.build_will_prompt(world_state, world)
        self._last_will_beat_at = now
        log_debug(
            f"[vessel_interface] Autonomous will beat for '{world}' "
            f"(interval={interval}s)"
        )
        await self._enqueue_perception(
            interface_path=interface_path,
            summary=prompt,
            environment=world,
            event_type="will_beat",
        )

    async def _maybe_run_action_beat(self) -> None:
        """Enqueue a **concrete-doing** cognition turn when due (middle layer).

        This is the beat that turns an authored goal into accomplished work.
        The will beat decides *what* Synth wants; the motor tick moves the body
        reflexively; but only a cognition turn can map the goal's free-text
        meaning onto the *right verb* (gather the wood, craft the pickaxe, place
        the block) — because the reflex must never read that text (keyword
        rule). Without this middle beat Synth "walks but accomplishes nothing".

        Fully guarded so a failure never breaks the scheduler. Gated: (1)
        ``VESSEL_AUTONOMY_ENABLED`` on; (2) ``VESSEL_ACTION_BEAT_ENABLED`` on;
        (3) a session active; (4) ``VESSEL_ACTION_INTERVAL_SEC`` elapsed since
        the last action beat; (5) the same player-quiet deferral as the will
        beat (a present player is answered reactively, not overridden by an
        autonomous turn); (6) an active goal exists (``build_action_prompt``
        returns ``""`` otherwise, and we skip). Runs on the Fast Lane — no Agent
        Lane, no Drone, no diary.
        """
        try:
            from core import vessel_beat
        except Exception:
            return

        def _cfg(key: str, default: Any) -> Any:
            return config_registry.get_value(
                key, default, group="vessel", component="vessel"
            )

        if not vessel_beat.is_autonomy_enabled(_cfg):
            return
        if not vessel_beat.is_action_beat_enabled(_cfg):
            return

        manager = get_vessel_session_manager()
        if not manager.has_active_session():
            return

        now = asyncio.get_event_loop().time()
        # Same reflection hold-off as the will beat: while Synth is deliberately
        # thinking about its goal, do not fire a competing "act on your goal"
        # turn. The motor tick is not gated (the body keeps moving).
        if self._reflecting and now < self._reflecting_until:
            return

        interval = vessel_beat.resolve_action_interval(_cfg)
        if now - self._last_action_beat_at < interval:
            return

        # Same player-quiet deferral as the will beat: while a player is
        # actively present, let their message be handled as an ordinary reactive
        # turn instead of firing an autonomous "act on your goal" turn.
        quiet_sec = vessel_beat.resolve_will_quiet_sec(_cfg)
        if quiet_sec > 0 and now - self._last_player_activity_at < quiet_sec:
            return

        world, world_state = await self._read_active_world_state()
        if world is None or world_state is None:
            return

        interface_path = self._decision_interface_path(world)
        if interface_path is None:
            return

        prompt = vessel_beat.build_action_prompt(world_state, world)
        if not prompt:
            # No active goal yet — the will beat authors one first.
            return

        self._last_action_beat_at = now
        log_debug(
            f"[vessel_interface] Autonomous action beat for '{world}' "
            f"(interval={interval}s)"
        )
        await self._enqueue_perception(
            interface_path=interface_path,
            summary=prompt,
            environment=world,
            event_type="action_beat",
        )

    async def _maybe_run_goal_debrief(self) -> None:
        """Supervise the single active vessel goal (slow postflight check).

        A world-agnostic debrief (see ``core.vessel_goal_debrief`` and AGENTS.md
        §5c) that closes the gap where Synth *progresses physically but never
        completes a goal*. It runs on its own slow cadence and does two things,
        both purely **structural** (never keyword/text intent detection):

        * **(B) Auto-completion.** Ask the connector whether the active goal is
          already satisfied by the live world/inventory
          (``evaluate_goal_completion``). If so, close it via the world's
          ``complete_active_goal`` hook — the deterministic outcome check the
          cognitive beats were failing to make.
        * **(A) Stall feedback.** Fingerprint the goal (id + step + updated_at);
          when it sits unchanged for ``VESSEL_GOAL_DEBRIEF_STALL_TICKS`` checks,
          arm a stall cue so the next will beat nudges Synth to reconsider the
          goal (is it already done? change approach?).

        Fully guarded so a failure never breaks the scheduler. Gated on
        ``VESSEL_GOAL_DEBRIEF_ENABLED`` + a session active + the configured
        interval elapsed. Fast Lane only — it never enqueues a cognition turn or
        writes a diary; the auto-close is a direct goal-store write.
        """
        try:
            from core import vessel_goal_debrief as vgd
        except Exception:
            return

        def _cfg(key: str, default: Any) -> Any:
            return config_registry.get_value(
                key, default, group="vessel", component="vessel"
            )

        if not vgd.is_debrief_enabled(_cfg):
            return

        manager = get_vessel_session_manager()
        if not manager.has_active_session():
            return

        now = asyncio.get_event_loop().time()
        interval = vgd.resolve_debrief_interval(_cfg)
        if now - self._last_goal_debrief_at < interval:
            return
        self._last_goal_debrief_at = now

        world, world_state = await self._read_active_world_state()
        if world is None or world_state is None:
            return

        connector = self._connected_connector(world)
        if connector is None:
            return

        try:
            goal = await connector.get_active_goal()
        except Exception:
            return
        if not goal:
            # No active goal to supervise — reset stall bookkeeping so a fresh
            # goal starts its stall count clean.
            self._goal_debrief_stall = {"sig": None, "count": 0}
            return

        # (B) Deterministic completion check against the live world/inventory.
        try:
            verdict = await connector.evaluate_goal_completion(goal, world_state)
        except Exception:
            verdict = {"satisfied": False}
        if isinstance(verdict, dict) and verdict.get("satisfied"):
            reason = str(verdict.get("reason") or "auto_completed")
            try:
                result = await connector.complete_active_goal(reason)
            except Exception as exc:  # pragma: no cover - defensive
                log_debug(f"[vessel_interface] goal debrief close failed: {exc}")
                result = None
            log_debug(
                f"[vessel_interface] Goal debrief auto-completed goal for "
                f"'{world}' (reason={reason}, item={verdict.get('item')}, "
                f"result={result})"
            )
            # Goal closed — reset stall bookkeeping and disarm any stale cue.
            self._goal_debrief_stall = {"sig": None, "count": 0}
            self._goal_debrief_cue_armed = False
            return

        # (B2) History-based completion check. Many goals leave no inventory
        # trace (placing a block, killing a mob, saying something), so the
        # inventory verdict above never marks them done. Ask the world whether
        # a *successful action actually taken this session* structurally
        # matches the goal's concrete target (by canonical game id, never by
        # parsing free text). Gated on VESSEL_GOAL_DEBRIEF_USE_HISTORY.
        if vgd.is_debrief_history_enabled(_cfg) and hasattr(
            connector, "evaluate_goal_completion_from_history"
        ):
            session_id, _path = self._resolve_session_for_environment(world)
            if session_id:
                try:
                    hist = await connector.evaluate_goal_completion_from_history(
                        goal, session_id, world_state
                    )
                except Exception:
                    hist = {"satisfied": False}
                if isinstance(hist, dict) and hist.get("satisfied"):
                    reason = str(hist.get("reason") or "action_in_history")
                    try:
                        result = await connector.complete_active_goal(reason)
                    except Exception as exc:  # pragma: no cover - defensive
                        log_debug(
                            f"[vessel_interface] goal debrief (history) close "
                            f"failed: {exc}"
                        )
                        result = None
                    log_debug(
                        f"[vessel_interface] Goal debrief auto-completed goal "
                        f"for '{world}' via history (reason={reason}, "
                        f"event_type={hist.get('event_type')}, "
                        f"item={hist.get('item')}, result={result})"
                    )
                    self._goal_debrief_stall = {"sig": None, "count": 0}
                    self._goal_debrief_cue_armed = False
                    return

        # (A) Stall detection: fingerprint the goal and count unchanged checks.
        stall_ticks = vgd.resolve_stall_ticks(_cfg)
        stalled = vgd.update_stall_state(self._goal_debrief_stall, goal, stall_ticks)
        if stalled:
            self._goal_debrief_cue_armed = True
            log_debug(
                f"[vessel_interface] Goal debrief detected stall for '{world}' "
                f"(sig={self._goal_debrief_stall.get('sig')}, "
                f"count={self._goal_debrief_stall.get('count')}) — arming will cue"
            )

    async def _maybe_run_damage_appraisal(self) -> None:
        """Enqueue a **post-damage appraisal** cognition turn when Synth is hurt.

        The fast survival reflex (``MinecraftConnector._survival_threat`` on the
        motor tick) already reacts *mechanically* to danger — fighting back,
        fleeing, surfacing. This adds the deliberate *appraisal* on top: right
        after Synth takes damage, one elevated (:data:`PRIORITY_URGENT`)
        cognition turn asks "I was just hurt — do I press the attack with my
        best weapon, loose a ranged shot, break off and heal, or (if a *person*
        struck me) respond in character rather than reflexively swinging back?".

        Detection is purely **structural**: the connector surfaces a positive
        ``extra["damage_taken"]`` delta on the single tick the health bar
        dropped (and, when known, ``extra["damage_from_player"]`` for the
        attacker kind). No keyword logic. Fully guarded so a failure never
        breaks the scheduler.

        Gated: (1) ``VESSEL_AUTONOMY_ENABLED`` on (autonomous play must be
        enabled for Synth to act on its own); (2) ``VESSEL_SP_APPRAISAL_ENABLED``
        on; (3) a session active; (4) a positive ``damage_taken`` delta this
        tick; (5) an anti-thrash floor (``VESSEL_WILL_INTERVAL_SEC`` reused as a
        minimum spacing) so a sustained damage stream (lava/drowning) fires at
        most one appraisal per window. Runs on the Fast Lane — no Agent Lane, no
        Drone, no diary.
        """
        try:
            from core import vessel_beat
        except Exception:
            return

        def _cfg(key: str, default: Any) -> Any:
            return config_registry.get_value(
                key, default, group="vessel", component="vessel"
            )

        if not vessel_beat.is_autonomy_enabled(_cfg):
            return
        # Gate on the self-preservation appraisal toggle (default on). Structural
        # bool read; any failure disables the beat rather than crashing.
        try:
            appraisal_on = bool(
                _cfg("VESSEL_SP_APPRAISAL_ENABLED", True)
                in (True, "true", "True", 1, "1")
            )
        except Exception:
            appraisal_on = True
        if not appraisal_on:
            return

        manager = get_vessel_session_manager()
        if not manager.has_active_session():
            return

        now = asyncio.get_event_loop().time()
        # Anti-thrash: reuse the will interval as a minimum spacing so a
        # sustained damage stream (lava, drowning) cannot enqueue an appraisal
        # every tick. The mechanical survival reflex still reacts each tick.
        min_spacing = vessel_beat.resolve_will_interval(_cfg)
        if now - self._last_appraisal_at < min_spacing:
            return

        world, world_state = await self._read_active_world_state()
        if world is None or world_state is None:
            return

        # Positive damage delta this tick? Purely structural read of the
        # connector-surfaced numeric field; absent/non-positive → no appraisal.
        extra = getattr(world_state, "extra", None)
        if not isinstance(extra, dict):
            return
        damage_taken = extra.get("damage_taken")
        try:
            if damage_taken is None or float(damage_taken) <= 0.0:
                return
        except (TypeError, ValueError):
            return

        interface_path = self._decision_interface_path(world)
        if interface_path is None:
            return

        prompt = vessel_beat.build_damage_appraisal_prompt(world_state, world)
        if not prompt:
            return

        self._last_appraisal_at = now
        log_debug(
            f"[vessel_interface] Damage appraisal beat for '{world}' "
            f"(damage_taken={damage_taken})"
        )
        await self._enqueue_perception(
            interface_path=interface_path,
            summary=prompt,
            environment=world,
            event_type="damage_appraisal",
        )

    @staticmethod
    def _goal_has_route(goal: dict[str, Any] | None) -> bool:
        """True when the goal names a concrete block/entity **target** to act on.

        Purely **structural** (never keyword matching on the free text): a goal
        is "routable" only when it names a block/entity **target** (a
        closed-enum ``target_kind`` of ``block``/``entity`` with a non-empty
        ``target_name``). A bare numeric **destination** is deliberately **not**
        enough: it only tells the body *where to walk*, not *what to do* when it
        gets there, so the motor tick would still only march-and-arrive with
        nothing to mine/use. That is exactly the aimless situation the
        out-of-band Drone planner exists to resolve — it must therefore still
        fire for a goal that has only a destination but no gameplay target.
        Fully guarded.
        """
        if not isinstance(goal, dict):
            return False
        try:
            kind = goal.get("target_kind")
            name = goal.get("target_name")
            if kind in ("block", "entity") and isinstance(name, str) and name.strip():
                return True
        except Exception:
            return False
        return False

    async def _maybe_run_drone_planner(self) -> None:
        """Translate a directionless goal into a reachable waypoint (out of band).

        The anti-circling fallback. When the slow will beat has authored a goal
        but left it **without** a structural target or numeric destination, the
        motor tick can only march in a straight line — which, once it turns on
        stalls, reads as aimless circling. Rather than making cognition plan the
        route inside the vessel turn (forbidden: a vessel turn must stay on the
        Fast Lane and never create an agentic task, AGENTS.md §5c), this
        dispatches a short-lived **Drone** *out of band* — a plain background
        :class:`asyncio.Task`, not an in-turn action — that observes the world
        and re-aims the goal via the world's own ``set_goal``/``update_goal``
        verbs (reusing the Fase-1 ``target_kind``/``target_name`` fields).

        Gated: (1) ``VESSEL_AUTONOMY_ENABLED`` on; (2) a session active; (3) the
        will beat has run at least once this session; (4) the active goal is not
        already routable; (5) no planner already running for this world; (6) a
        per-world cooldown (``VESSEL_DRONE_PLAN_INTERVAL_SEC``) has elapsed.
        Fully guarded — any failure degrades to a no-op and never breaks the
        scheduler or the motor tick.
        """
        try:
            from core import vessel_beat
        except Exception:
            return

        def _cfg(key: str, default: Any) -> Any:
            return config_registry.get_value(
                key, default, group="vessel", component="vessel"
            )

        if not vessel_beat.is_autonomy_enabled(_cfg):
            return

        manager = get_vessel_session_manager()
        if not manager.has_active_session():
            return

        # Only plan after volition has had a chance to author a goal.
        if self._last_will_beat_at <= 0.0:
            return

        world, world_state = await self._read_active_world_state()
        if world is None or world_state is None:
            return

        # Reap a finished planner for this world before deciding to launch one.
        existing = self._drone_plan_tasks.get(world)
        if existing is not None and existing.done():
            self._drone_plan_tasks.pop(world, None)
            existing = None
        if existing is not None:
            return  # a planner is already working on this world

        goal = self._goal_from_world_state(world_state)
        if goal is None:
            return  # no goal yet — nothing to make routable
        if self._goal_has_route(goal):
            return  # already has a target/destination; motor tick can steer

        interval = self._resolve_drone_plan_interval(_cfg)
        now = asyncio.get_event_loop().time()
        if now - self._last_drone_plan_at.get(world, 0.0) < interval:
            return

        self._last_drone_plan_at[world] = now
        task = asyncio.create_task(self._run_drone_planner(world, goal))
        self._drone_plan_tasks[world] = task
        log_debug(
            f"[vessel_interface] Drone planner dispatched for '{world}' "
            f"(goal id={goal.get('id')}, interval={interval}s)"
        )

    @staticmethod
    def _resolve_drone_plan_interval(cfg: Any) -> float:
        """Per-world cooldown between out-of-band drone-planner dispatches.

        Reads ``VESSEL_DRONE_PLAN_INTERVAL_SEC`` (default 120 s, clamped
        ``[30, 3600]``) so a directionless goal is re-planned no more often than
        this. Fully guarded — any failure falls back to the default.
        """
        try:
            raw = cfg("VESSEL_DRONE_PLAN_INTERVAL_SEC", 120)
            value = float(raw)
        except Exception:
            value = 120.0
        return max(30.0, min(3600.0, value))

    @staticmethod
    def _resolve_goal_expand_retry(cfg: Any) -> float:
        """Per-world cooldown before a *failed* goal expansion is retried.

        Reads ``VESSEL_GOAL_EXPAND_RETRY_SEC`` (default 300 s, clamped
        ``[30, 3600]``) so a Drone that exhausts its iteration budget without
        committing a steps plan is retried after this interval rather than on
        the very next scheduler tick. Fully guarded — any failure falls back to
        the default.
        """
        try:
            raw = cfg("VESSEL_GOAL_EXPAND_RETRY_SEC", 300)
            value = float(raw)
        except Exception:
            value = 300.0
        return max(30.0, min(3600.0, value))

    async def _run_drone_planner(self, world: str, goal: dict[str, Any]) -> None:
        """Body of the out-of-band drone planner (runs in its own task).

        Builds a free-text objective describing the current goal and asks a
        Drone to look around the world and commit a concrete, reachable
        target/destination through the world's ``set_goal``/``update_goal``
        verbs. The Drone context deliberately carries **no** vessel
        ``interface_path`` so its agentic task is never attributed to the
        embodiment turn (keeping the vessel chain Fast-Lane only). Fully guarded.
        """
        try:
            from core.agent_core import get_agent_loop_manager

            description = ""
            if isinstance(goal, dict):
                raw = goal.get("description")
                if isinstance(raw, str):
                    description = raw.strip()

            drone_goal = (
                f"You are embodied in the '{world}' world and pursuing this "
                f'self-authored goal: "{description}". Look around the world '
                f"(use the vessel_{world}_scan / vessel_{world}_observe tools) "
                "to find a concrete, currently reachable place or thing that "
                "advances this goal. Then commit it by calling "
                f"vessel_{world}_update_goal (or vessel_{world}_set_goal): set "
                "'target_kind' to 'block' or 'entity' and 'target_name' to the "
                "EXACT id you saw in the scan (copy it verbatim, never invent "
                "one), or give numeric destination coordinates when you mean a "
                "bare spot. Give exactly one waypoint, then stop. If nothing "
                "relevant is reachable right now, pick a sensible direction to "
                "explore and set that as a destination instead."
            )

            manager = get_agent_loop_manager()
            # Restrict this out-of-band planner Drone to look-and-commit tools
            # only. It must NEVER speak in-world: an in-world ``vessel_<world>_say``
            # in its tool set let a broken/hallucinating cortex emit stray chatter
            # (the "Mirtillo" bug). The allow-list is structural — no keyword
            # logic — and keeps the Drone to scan/observe/knowledge + goal-commit.
            result = await manager.run_drone(
                goal=drone_goal,
                allowed_tools={
                    f"vessel_{world}_scan",
                    f"vessel_{world}_observe",
                    f"vessel_{world}_lookup_knowledge",
                    f"vessel_{world}_update_goal",
                    f"vessel_{world}_set_goal",
                },
                # Run on the vessel cortex (VESSEL_CORTEX) — see the goal-expander
                # rationale below: AGENT_CORTEX often falls back to the slow
                # browser-driven Base Cortex, which times out on this multi-step
                # scan → commit turn before the waypoint is ever written.
                cortex_scope="vessel",
            )
            if isinstance(result, dict):
                log_debug(
                    f"[vessel_interface] Drone planner for '{world}' finished: "
                    f"stop_reason={result.get('stop_reason')} "
                    f"iterations={result.get('iterations')}"
                )
        except Exception as exc:
            log_debug(f"[vessel_interface] Drone planner for '{world}' failed: {exc}")
        finally:
            existing = self._drone_plan_tasks.get(world)
            if existing is not None and existing.done():
                self._drone_plan_tasks.pop(world, None)

    @staticmethod
    def _goal_needs_expansion(goal: dict[str, Any] | None) -> bool:
        """True when a goal has no ordered ``steps`` plan yet (structural only).

        A goal authored by the slow will beat is free text with no breakdown —
        the ``steps`` list is empty. That is exactly the goal a knowledge-base
        Drone should expand into concrete sub-steps (gather wood → craft axe →
        …). Purely structural: it inspects the ``steps`` field only, never the
        free-text description. Fully guarded.
        """
        if not isinstance(goal, dict):
            return False
        try:
            steps = goal.get("steps")
            if isinstance(steps, (list, tuple)) and len(steps) > 0:
                return False
        except Exception:
            return False
        return True

    async def _maybe_run_goal_expander(self) -> None:
        """Expand a freshly-authored goal into an ordered ``steps`` plan (out of band).

        The knowledge half of goal pursuit. When the slow will beat authors a
        new goal it is just free text (e.g. *"build a wooden house"*) with no
        breakdown — so the action beat and motor tick have nothing concrete to
        chase. This dispatches a short-lived **Drone** *out of band* (a plain
        background :class:`asyncio.Task`, NEVER an in-turn vessel action, so the
        vessel chain stays Fast-Lane only, AGENTS.md §5c) that consults the
        per-world knowledge base and writes an ordered ``steps`` plan back via
        the world's ``update_goal`` verb. When the plan is committed we re-notify
        Synth by resetting the will-beat clock so volition re-enters with the
        now-detailed goal (the user's explicit requirement: the updated goal
        must go back to Synth via will).

        Gated: (1) ``VESSEL_AUTONOMY_ENABLED`` on; (2) ``VESSEL_GOAL_EXPAND_ENABLED``
        on (the feature's own master switch); (3) ``VESSEL_KNOWLEDGE_ENABLED``
        on (the expander consults the knowledge base); (4) a session active;
        (5) the will beat has run at least once; (6) an active goal exists that
        still has no ``steps``; (7) that goal id has not already been expanded
        for this world; (8) no expansion already running for this world. Fully
        guarded — any failure degrades to a no-op and never breaks the scheduler.
        """
        try:
            from core import vessel_beat
        except Exception:
            return

        def _cfg(key: str, default: Any) -> Any:
            return config_registry.get_value(
                key, default, group="vessel", component="vessel"
            )

        if not vessel_beat.is_autonomy_enabled(_cfg):
            return
        # Honour the feature's own master switch (the toggle exposed in the
        # WebUI as "Goal Plan Expansion (Drone)"). Without this check the toggle
        # was inert — the expander only ever looked at VESSEL_KNOWLEDGE_ENABLED.
        try:
            if not bool(_cfg("VESSEL_GOAL_EXPAND_ENABLED", True)):
                return
        except Exception:
            pass
        # Goal expansion consults the knowledge base: honour its switch too.
        try:
            if not bool(_cfg("VESSEL_KNOWLEDGE_ENABLED", True)):
                return
        except Exception:
            pass

        manager = get_vessel_session_manager()
        if not manager.has_active_session():
            return
        if self._last_will_beat_at <= 0.0:
            return

        world, world_state = await self._read_active_world_state()
        if world is None or world_state is None:
            return

        existing = self._goal_expand_tasks.get(world)
        if existing is not None and existing.done():
            self._goal_expand_tasks.pop(world, None)
            existing = None
        if existing is not None:
            return  # an expansion is already working on this world

        goal = self._goal_from_world_state(world_state)
        if goal is None:
            return
        if not self._goal_needs_expansion(goal):
            return  # already has a steps plan

        goal_id = goal.get("id")
        try:
            goal_id_int = int(goal_id) if goal_id is not None else None
        except (TypeError, ValueError):
            goal_id_int = None
        if goal_id_int is None:
            return  # cannot de-dup without a stable id — skip
        if self._expanded_goal_ids.get(world) == goal_id_int:
            return  # already expanded this exact goal

        # Retry cooldown: after a failed expansion the de-dup marker is cleared
        # so a later tick can retry — but only after VESSEL_GOAL_EXPAND_RETRY_SEC
        # has elapsed, so a persistently-failing expansion does not respawn a
        # Drone on every scheduler tick (a tight loop that burns cognition).
        retry_sec = self._resolve_goal_expand_retry(_cfg)
        now = asyncio.get_event_loop().time()
        last_attempt = self._last_goal_expand_at.get(world, 0.0)
        if last_attempt > 0.0 and now - last_attempt < retry_sec:
            return  # still within the retry cooldown for this world

        self._last_goal_expand_at[world] = now
        self._expanded_goal_ids[world] = goal_id_int
        task = asyncio.create_task(self._run_goal_expander(world, goal))
        self._goal_expand_tasks[world] = task
        log_debug(
            f"[vessel_interface] Goal expander dispatched for '{world}' "
            f"(goal id={goal_id_int})"
        )

    async def _run_goal_expander(self, world: str, goal: dict[str, Any]) -> None:
        """Body of the out-of-band goal-expansion sub-agent (runs in its own task).

        Breaking a self-authored goal into an ordered plan is genuine reasoning
        work, so this uses an **agent Drone** (``run_agent_drone``): a
        task-scoped sub-agent that runs the bounded agentic loop with the full
        **Agent** budget (``AGENT_MAX_ITERATIONS`` / ``AGENT_TURN_TIMEOUT_SEC``)
        rather than the tight Drone budget — giving the model room to ask itself
        questions, consult the world's knowledge base and iterate before
        committing. It keeps every Drone safety property (single-level
        delegation, tool allow-list, no nested spawning).

        It asks the sub-agent to expand the goal into a **detailed, ordered
        bullet list** of concrete sub-steps — taking nothing for granted, so
        every tool, material, place or precondition the goal needs becomes its
        own explicit sub-step placed before the step that uses it — and to
        **commit early**: the turn's single deliverable is the committed plan,
        so the sub-agent is told to build a solid ordered plan from what it
        already knows and commit it through ``vessel_<world>_update_goal``
        (``steps=[...]``) FIRST, then end the turn with ``attempt_completion``.
        ``vessel_<world>_lookup_knowledge`` is allowed only for a genuine gap
        (at most once or twice) — the previous open-ended "research in detail"
        framing made the slow vessel cortex loop on lookups until the wall-clock
        budget expired **without ever committing** ``update_goal``, leaving the
        goal stepless. The plan is **grounded in Synth's real situation**:
        the current inventory and position are read from the live ``WorldState``
        and passed to the sub-agent so it plans from what Synth actually has and
        where it is (the first step must be doable with the current inventory),
        instead of an abstract textbook chain that assumes endgame materials. The
        sub-agent context carries **no** vessel ``interface_path`` so its agentic
        task is never attributed to the embodiment turn (Fast-Lane invariant,
        AGENTS.md §5c).

        There is **no** description-copy fallback: if the sub-agent does not
        commit a real multi-step plan the goal is left stepless and a later tick
        retries — a genuine ordered plan is the only accepted outcome.

        On success it re-notifies Synth: resetting ``_last_will_beat_at`` to 0
        forces the next scheduler tick to run a fresh will beat, so volition
        re-enters with the now-detailed goal (the user's requirement that the
        updated goal go back to Synth via will). Fully guarded.
        """
        committed = False
        try:
            from core.agent_core import get_agent_loop_manager
            from core import vessel_beat

            description = ""
            if isinstance(goal, dict):
                raw = goal.get("description")
                if isinstance(raw, str):
                    description = raw.strip()

            # Ground the plan in Synth's ACTUAL situation: read the live world
            # state and surface the real inventory + position to the Drone. A
            # plan made in the abstract ("how to craft a Conduit in general")
            # ignores that Synth only holds a few pieces of wood and is on a
            # coast — producing impossible, out-of-order steps. Reusing the same
            # structural formatters as the will beat keeps it keyword-free.
            inventory_txt = "unknown"
            position_txt = "unknown"
            try:
                _, live_state = await self._read_active_world_state()
                extra = getattr(live_state, "extra", None)
                if isinstance(extra, dict):
                    inventory_txt = vessel_beat._fmt_items(extra.get("inventory") or [])
                    position_txt = vessel_beat._fmt_position(extra.get("position"))
            except Exception:
                pass

            drone_goal = (
                f"You are embodied in the '{world}' world and have just set "
                f'yourself this goal: "{description}". This is your own '
                "personal goal — you chose it for yourself out of your own "
                "will, nobody asked you to do it.\n\n"
                "Your ACTUAL situation right now:\n"
                f"- Inventory: {inventory_txt}\n"
                f"- Position: {position_txt}\n\n"
                "YOUR JOB: turn this goal into a detailed, ordered plan — a "
                "bullet list of concrete sub-steps a player would actually "
                "follow — and COMMIT that plan. Take nothing for granted: "
                "everything the goal needs (each tool, material, place or "
                "precondition) becomes its own explicit sub-step, placed "
                "BEFORE the step that uses it.\n\n"
                "This turn has exactly ONE deliverable: a committed plan. You "
                "have only two useful tools and you MUST use them in this "
                "order:\n"
                f"  A) vessel_{world}_update_goal — call it ONCE with 'steps' "
                "set to your ordered list of short sub-step strings (each a "
                "single concrete action). This is the whole point of the turn.\n"
                f"  B) attempt_completion — call it IMMEDIATELY after "
                "update_goal succeeds, with a one-line summary. This is the "
                "ONLY way to end the turn.\n\n"
                "COMMIT EARLY, then stop. Do NOT keep researching forever. Your "
                "FIRST action should be to commit a solid ordered plan with "
                f"vessel_{world}_update_goal built from what you already know, "
                "and then attempt_completion. A committed, imperfect plan is "
                "infinitely better than no plan.\n\n"
                f"You MAY call vessel_{world}_lookup_knowledge AT MOST once or "
                "twice, and ONLY if you are genuinely unsure what a specific "
                "prerequisite needs (e.g. what tool mines a block, what a "
                "recipe requires). Look up the specific thing by its in-game "
                "id, take the answer, and move straight to committing the "
                "plan. Never loop on lookups — if you have already looked "
                "something up, do NOT look it up again; commit the plan "
                "instead.\n\n"
                "PLAN QUALITY (apply from what you know; a lookup is only for a "
                "genuine gap):\n"
                "- Whenever the goal needs an object, a tool, a material, a "
                "PLACE or any precondition you do not already have or have not "
                "met, make OBTAINING or REACHING it its own earlier sub-step. "
                "A precondition can be a thing (craft a pickaxe, gather wood) "
                "or a condition (reach the beach, level the ground).\n"
                "- Order the steps so each is reachable from the one before "
                "it (gather the prerequisite before the thing that needs it). "
                "The FIRST step must be doable immediately with the inventory "
                "you hold right now, from where you are.\n"
                "- Break big steps into small ones. 'Get glass for windows' is "
                "not one step — it is: gather sand, build a furnace, get fuel, "
                "smelt the sand, craft the glass. Prefer many small concrete "
                "actions over a few vague ones.\n"
                "- If the goal is far beyond your current means, write the "
                "realistic steps that genuinely progress toward it from here "
                "and stop where your means run out — do not fabricate a chain "
                "of impossible endgame steps.\n\n"
                f"Remember: update_goal (with a steps list) FIRST, then "
                "attempt_completion. Do not end the turn any other way, and do "
                "not end it without having committed the steps."
            )

            manager = get_agent_loop_manager()
            # Restrict this out-of-band expander to knowledge lookup + goal
            # commit only. It must NEVER speak in-world: leaving an in-world
            # ``vessel_<world>_say`` in its tool set let a broken/hallucinating
            # cortex emit stray chatter (the "Mirtillo" bug). Structural
            # allow-list — no keyword logic.
            #
            # Use an *agent* Drone (``run_agent_drone``), not the tight
            # ``run_drone``: breaking a self-authored goal into an ordered plan
            # is genuine reasoning work — the sub-agent must be able to ask
            # itself questions, consult the knowledge base over several
            # iterations and refine before committing. The 3-iteration Drone
            # budget was too small for that (it timed out before ever emitting a
            # parseable ``update_goal``). The agent Drone runs the same bounded
            # loop with the full Agent budget (AGENT_MAX_ITERATIONS /
            # AGENT_TURN_TIMEOUT_SEC) while keeping every Drone safety property
            # (single-level delegation, the tool allow-list, no vessel
            # interface_path so it is never attributed to the embodiment turn).
            result = await manager.run_agent_drone(
                goal=drone_goal,
                allowed_tools={
                    f"vessel_{world}_lookup_knowledge",
                    f"vessel_{world}_update_goal",
                },
                # Run the expander on the vessel cortex (VESSEL_CORTEX), not the
                # generic agent cortex. AGENT_CORTEX often falls back to the
                # browser-driven Base Cortex, which cannot complete this
                # multi-step tool-calling turn — so steps were never written. The
                # vessel cortex is the same proper API engine that authors the
                # goals.
                cortex_scope="vessel",
            )
            if isinstance(result, dict):
                log_debug(
                    f"[vessel_interface] Goal expander for '{world}' finished: "
                    f"stop_reason={result.get('stop_reason')} "
                    f"iterations={result.get('iterations')}"
                )

            # Verify the drone actually wrote a steps plan before re-notifying.
            _, refreshed = await self._read_active_world_state()
            refreshed_goal = self._goal_from_world_state(refreshed)
            if refreshed_goal is not None and not self._goal_needs_expansion(
                refreshed_goal
            ):
                committed = True
        except Exception as exc:
            log_debug(f"[vessel_interface] Goal expander for '{world}' failed: {exc}")
        finally:
            existing = self._goal_expand_tasks.get(world)
            if existing is not None and existing.done():
                self._goal_expand_tasks.pop(world, None)
            if committed:
                # Re-notify Synth via will: force the next tick to run a fresh
                # will beat so volition re-enters with the now-detailed
                # (steps-filled) goal.
                self._last_will_beat_at = 0.0
                # A *successful* expansion resets the retry cooldown for this
                # world so a subsequent *different* goal can be expanded on the
                # next tick without waiting out the failure cooldown. The de-dup
                # marker still prevents re-expanding this same goal.
                self._last_goal_expand_at.pop(world, None)
                log_debug(
                    f"[vessel_interface] Goal expanded for '{world}' — "
                    "re-notifying Synth via will beat."
                )
            else:
                # The expansion did NOT commit a steps plan (e.g. the agent Drone
                # hit its iteration budget with stop_reason=paused_max_iterations,
                # or errored). Clear the de-dup marker for this world so a future
                # tick can retry, instead of permanently marking the goal as
                # "already expanded" and leaving it stepless forever.
                goal_id = goal.get("id") if isinstance(goal, dict) else None
                try:
                    goal_id_int = int(goal_id) if goal_id is not None else None
                except (TypeError, ValueError):
                    goal_id_int = None
                if (
                    goal_id_int is not None
                    and self._expanded_goal_ids.get(world) == goal_id_int
                ):
                    self._expanded_goal_ids.pop(world, None)
                    log_debug(
                        f"[vessel_interface] Goal expansion for '{world}' did "
                        f"not commit steps (goal id={goal_id_int}); cleared "
                        "de-dup marker so a later tick can retry."
                    )

    async def _maybe_run_motor_tick(self) -> None:
        """Step the body toward the active goal when due (fast layer, no LLM).

        This is the *motorics* half of autonomy. Unlike the will beat it does
        **not** build a prompt, enqueue a message or run a cognition turn — it
        reads the active goal and calls the connected connector's
        :meth:`~plugins.rift_vessel.vessel_base.VesselConnectorBase.motor_step`
        directly, which picks one structural in-world move and performs it.

        Fully guarded. Gated four ways: (1) ``VESSEL_AUTONOMY_ENABLED`` and
        (2) ``VESSEL_MOTOR_ENABLED`` must be on; (3) a Vessel session must be
        active; (4) at least ``VESSEL_MOTOR_INTERVAL_SEC`` must have elapsed.
        Never creates an Agent Lane task, Drone or diary entry.
        """
        try:
            from core import vessel_beat
        except Exception:
            return

        def _cfg(key: str, default: Any) -> Any:
            return config_registry.get_value(
                key, default, group="vessel", component="vessel"
            )

        if not vessel_beat.is_autonomy_enabled(_cfg):
            return
        if not vessel_beat.is_motor_enabled(_cfg):
            return

        manager = get_vessel_session_manager()
        if not manager.has_active_session():
            return

        interval = vessel_beat.resolve_motor_interval(_cfg)
        now = asyncio.get_event_loop().time()
        if now - self._last_motor_tick_at < interval:
            return
        self._last_motor_tick_at = now

        connector = await self._active_connector()
        if connector is None or not hasattr(connector, "motor_step"):
            return

        # Read the world state once and reuse it for both motorics and en-route
        # element collection, so the fast tick never double-polls the connector.
        world, world_state = await self._read_active_world_state()
        goal = self._goal_from_world_state(world_state)
        try:
            result = await connector.motor_step(goal)
        except Exception as exc:
            log_debug(f"[vessel_interface] motor_step failed: {exc}")
            return
        if isinstance(result, dict) and result.get("acted"):
            log_debug(
                f"[vessel_interface] motor tick: {result.get('action')} "
                f"target={result.get('target')} target_kind={result.get('target_kind')} "
                f"target_result={result.get('target_result')} "
                f"reason={result.get('reason')} remaining={result.get('remaining')} "
                f"dest={result.get('destination')} (interval={interval}s)"
            )

        # Defer en-route sighting perceptions while a player is actively present.
        # The body still moves (motor_step above needs no LLM), but each sighting
        # is enqueued as an AMBIENT cognition perception on the shared
        # ``vessel/<world>`` scope. On a slow vessel cortex (e.g. Selenium) the
        # single consumer can spend the whole turn draining these autonomous
        # perceptions, so a HIGH player chat that arrives while one is in-flight
        # waits behind it — an isolated test (motor/sightings silenced) got an
        # in-world reply in ~37s, while under the normal sighting stream the same
        # chat got none for minutes. Suppressing NEW sightings during the quiet
        # window keeps the consumer free to pick up the player chat promptly.
        # Mirrors the will/action-beat deferral; structural (actor-based via
        # ``_last_player_activity_at``), never keyword matching; ``0`` disables it.
        try:
            from core import vessel_beat as _vb

            quiet_sec = _vb.resolve_will_quiet_sec(_cfg)
        except Exception:  # pragma: no cover - defensive
            quiet_sec = 0
        if quiet_sec > 0 and now - self._last_player_activity_at < quiet_sec:
            log_debug(
                "[vessel_interface] En-route sightings deferred: player active "
                f"{now - self._last_player_activity_at:.0f}s ago "
                f"(quiet window {quiet_sec}s)"
            )
            return

        # En-route element collection: surface anything new the body can see as
        # it moves, so a chance encounter mid-trip can change Synth's plans.
        await self._collect_en_route_sightings(world, world_state)

    async def _active_connector(self) -> Any | None:
        """Return the live, connected connector instance, or ``None``.

        Mirrors :meth:`_read_active_world_state`'s registry lookup but hands
        back the connector itself so the motor tick can call ``motor_step``
        without re-reading the whole world state. Fully guarded.
        """
        try:
            from core.vessel_registry import VESSEL_REGISTRY

            instances = getattr(VESSEL_REGISTRY, "_instances", {}) or {}
            for connector in instances.values():
                try:
                    if getattr(connector, "is_connected", False):
                        return connector
                except Exception:
                    continue
        except Exception as exc:
            log_debug(f"[vessel_interface] active connector lookup failed: {exc}")
        return None

    @staticmethod
    async def _active_goal(connector: Any) -> dict[str, Any] | None:
        """Best-effort read of the connector's active free-text goal.

        Reads it from the connector's own :meth:`get_world_state` extra payload
        (where each world publishes ``current_goal``) so the interface stays
        world-agnostic and never touches a world-specific goal store. Fully
        guarded — any error degrades to ``None`` (motor tick idles).
        """
        try:
            if not hasattr(connector, "get_world_state"):
                return None
            state = await connector.get_world_state()
            if state is None:
                return None
            extra = getattr(state, "extra", None) or {}
            goal = extra.get("current_goal")
            return goal if isinstance(goal, dict) else None
        except Exception:
            return None

    @staticmethod
    def _goal_from_world_state(world_state: Any) -> dict[str, Any] | None:
        """Extract the active free-text goal from an already-read WorldState.

        Sibling of :meth:`_active_goal` that works off a WorldState the caller
        has *already* fetched, so the motor tick can read the world once and
        reuse it for both motorics and en-route element collection instead of
        polling the connector twice. Fully guarded.
        """
        try:
            if world_state is None:
                return None
            extra = getattr(world_state, "extra", None) or {}
            goal = extra.get("current_goal")
            return goal if isinstance(goal, dict) else None
        except Exception:
            return None

    @staticmethod
    def _element_signature(element: Any) -> str | None:
        """Stable structural key for one perceivable element.

        Built purely from the affordance contract shared by every world
        (``{kind, target, verb, distance}``) — ``kind:target`` — so novelty is
        judged by *what* an element is, never by any language-specific text.
        Returns ``None`` for a malformed element (skipped).
        """
        if not isinstance(element, dict):
            return None
        target = element.get("target")
        if not target:
            return None
        kind = element.get("kind") or "thing"
        return f"{kind}:{target}"

    @staticmethod
    def _cardinal_bearing(
        origin: Any,
        target_pos: Any,
    ) -> str | None:
        """Compass bearing (``N``/``NE``/``E``/…) from ``origin`` to ``target_pos``.

        Purely geometric and world-agnostic: derives an 8-point compass label
        from the planar offset between two ``{x, y, z}`` positions. Uses the
        standard Minecraft axis convention (``+x`` → East, ``-x`` → West,
        ``+z`` → South, ``-z`` → North). Returns ``None`` when either position
        is unusable or the target coincides with the origin (no meaningful
        heading). No keywords, no language dependence.
        """
        if not isinstance(origin, dict) or not isinstance(target_pos, dict):
            return None
        try:
            dx = float(target_pos["x"]) - float(origin["x"])
            dz = float(target_pos["z"]) - float(origin["z"])
        except (KeyError, TypeError, ValueError):
            return None
        if abs(dx) < 0.5 and abs(dz) < 0.5:
            return None
        # atan2(east, -north) → 0 rad points North, increasing clockwise, so an
        # 8-sector split maps cleanly onto N, NE, E, SE, S, SW, W, NW.
        angle = math.degrees(math.atan2(dx, -dz)) % 360.0
        sectors = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
        index = int((angle + 22.5) // 45.0) % 8
        return sectors[index]

    @staticmethod
    def _describe_sighting(element: Any, origin: Any) -> str | None:
        """One-line compact description of a sighted element (``target`` + bearing).

        Renders ``"<target> (~N blocks <BEARING>)"`` — distance rounded, an
        8-point compass bearing appended when a position is available. Falls
        back gracefully (drops the bearing, then the distance) when data is
        missing. Returns ``None`` for a malformed element.
        """
        if not isinstance(element, dict):
            return None
        target = element.get("target")
        if not target:
            return None
        distance = element.get("distance")
        bearing = VesselInterface._cardinal_bearing(origin, element.get("position"))
        if isinstance(distance, (int, float)):
            dist_txt = f"~{round(float(distance))} blocks"
            if bearing:
                return f"{target} ({dist_txt} {bearing})"
            return f"{target} ({dist_txt})"
        if bearing:
            return f"{target} ({bearing})"
        return str(target)

    async def _collect_en_route_sightings(
        self,
        world: str | None,
        world_state: Any,
    ) -> None:
        """Notice and surface genuinely-new elements the body sees while moving.

        Fulfils the "collect everything seen en route" behaviour (TODO): while
        Synth travels from A to B the body keeps a running, session-scoped
        record of every element it has already perceived, and the moment a
        *new* one appears it is surfaced **once** as a ``sighting`` perception
        so cognition (the slow will beat) can react — divert toward it, note it
        to revisit later, or mention it to other players.

        World-agnostic and keyword-free: it reads only the connector's
        structural :class:`WorldState` ``affordances`` (``{kind, target, verb,
        distance}``) and keys novelty on ``kind:target``. Whether a sighting is
        "rare" or "the quest item I was after" is Synth's own judgement in the
        cognition turn, never decided here. Fully guarded — any failure is
        swallowed so the fast motor tick is never disrupted, and the surfaced
        ``sighting`` events still pass through the normal dedup/rate-limit
        salience filter in :meth:`on_world_event`.
        """
        if not world or world_state is None:
            return
        try:
            extra = getattr(world_state, "extra", None) or {}
            affordances = extra.get("affordances") or []
            if not isinstance(affordances, list) or not affordances:
                return

            scope = f"{INTERFACE_NAME}/{world}"
            seen = self._seen_elements.setdefault(scope, set())
            origin = getattr(world_state, "position", None)

            # Collect *all* genuinely-new elements this tick, grouped by kind,
            # then surface them as ONE compact perception instead of a flood of
            # one-line "You notice a block nearby: X" events. This keeps the
            # sighting view specific and compact (each line carries a distance
            # and an 8-point compass bearing) and, because it is a single
            # ``sighting`` event, plays nicely with the rate-limit salience
            # filter. Novelty (and remembering everything seen) is unchanged.
            new_by_kind: dict[str, list[str]] = {}
            new_records: list[dict[str, Any]] = []
            for element in affordances:
                signature = self._element_signature(element)
                if signature is None or signature in seen:
                    continue
                seen.add(signature)
                line = self._describe_sighting(element, origin)
                if line is None:
                    continue
                kind = element.get("kind") or "thing"
                new_by_kind.setdefault(str(kind), []).append(line)
                new_records.append(
                    {
                        "kind": kind,
                        "target": element.get("target"),
                        "distance": element.get("distance"),
                        "position": element.get("position"),
                    }
                )

            if not new_by_kind:
                return

            # Build the compact grouped summary, e.g.:
            #   You notice the following blocks:
            #   - tall_seagrass (~6 blocks NE)
            #   - dirt (~5 blocks SW)
            # A pluralised group header per kind ("blocks"/"entities") is a
            # simple English suffix, not language-dependent feature logic.
            sections: list[str] = []
            for kind, lines in new_by_kind.items():
                header = f"You notice the following {kind}s:"
                body = "\n".join(f"- {line}" for line in lines)
                sections.append(f"{header}\n{body}")
            summary = "\n".join(sections)

            await self.on_world_event(
                environment=world,
                event_type="sighting",
                summary=summary,
                data={"sightings": new_records},
            )
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"[vessel_interface] en-route sighting collection failed: {exc}")

    async def _read_active_world_state(self) -> tuple[str | None, Any | None]:
        """Return ``(world, WorldState)`` for the connected world, or Nones.

        Resolves the live connector instance the same way the Vessel plugin
        does (iterate the registry's built instances and pick the connected
        one), then reads its :meth:`get_world_state`. Fully guarded.
        """
        try:
            from core.vessel_registry import VESSEL_REGISTRY

            instances = getattr(VESSEL_REGISTRY, "_instances", {}) or {}
            for name, connector in instances.items():
                try:
                    if not getattr(connector, "is_connected", False):
                        continue
                    if not hasattr(connector, "get_world_state"):
                        continue
                    world_state = await connector.get_world_state()
                    if world_state is not None:
                        return name, world_state
                except Exception as exc:
                    log_debug(
                        f"[vessel_interface] world-state read failed for "
                        f"'{name}': {exc}"
                    )
        except Exception as exc:
            log_debug(f"[vessel_interface] active world lookup failed: {exc}")
        return None, None

    def _connected_connector(self, world: str) -> Any | None:
        """Return the live, connected connector for ``world`` (or ``None``).

        Resolves the built connector instance the same way
        :meth:`_read_active_world_state` does. Fully guarded.
        """
        try:
            from core.vessel_registry import VESSEL_REGISTRY

            instances = getattr(VESSEL_REGISTRY, "_instances", {}) or {}
            connector = instances.get(world)
            if connector is not None and getattr(connector, "is_connected", False):
                return connector
        except Exception as exc:
            log_debug(
                f"[vessel_interface] connector lookup failed for '{world}': {exc}"
            )
        return None

    def _decision_interface_path(self, world: str) -> str | None:
        """Return the ``vessel/…`` path to attribute the beat to.

        Prefers a locally-tracked session path for the world (so the cognition
        turn lands in the same world-scoped history the perceptions use); falls
        back to the environment prefix so the beat still fires right after a
        connect before the local session map is populated.
        """
        prefix = f"{INTERFACE_NAME}/{world}"
        for path in self._sessions.values():
            if path == prefix or path.startswith(f"{prefix}/"):
                return path
        # Fall back to the environment prefix; build_context still routes it as
        # a world-scoped vessel turn (interface_path starts with "vessel").
        return prefix

    async def _reap_local_sessions(self) -> None:
        """Forget locally-tracked sessions that the DB reports as ended."""
        if not self._sessions:
            return
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor(aiomysql.DictCursor) as cur:
                    placeholders = ",".join(["%s"] * len(self._sessions))
                    await cur.execute(
                        f"SELECT session_id FROM vessel_sessions"
                        f" WHERE session_id IN ({placeholders})"
                        f" AND status = 'active'",
                        tuple(self._sessions.keys()),
                    )
                    rows = await cur.fetchall()
            active = {r["session_id"] for r in rows or []}
            for session_id in list(self._sessions.keys()):
                if session_id not in active:
                    self._sessions.pop(session_id, None)
        except Exception as exc:
            log_debug(f"[vessel_interface] _reap_local_sessions failed: {exc}")

    def _connector_environments(self) -> dict[str, Any]:
        """Return a ``{environment: connector}`` map of registered connectors.

        Purely structural (registry instance keys are the environment names);
        fully guarded. Used by the disconnect-grace sweep to probe each active
        session's connector liveness without any keyword logic.
        """
        try:
            from core.vessel_registry import VESSEL_REGISTRY

            return dict(getattr(VESSEL_REGISTRY, "_instances", {}) or {})
        except Exception as exc:
            log_debug(f"[vessel_interface] connector map lookup failed: {exc}")
            return {}

    async def _close_disconnected_sessions(self) -> None:
        """Drive the RECONNECTING → CONNECTED / ENDED transitions.

        This is the sweep that advances the connection-driven 3-state model
        (see :meth:`~core.vessel_session_manager.VesselSessionManager.has_active_session`).
        For each locally-tracked session it probes the matching connector's
        ``is_connected`` flag (structural, keyword-free):

        * **connector live** → CONNECTED: clear any grace timer and leave the
          session running.
        * **connector dropped** → RECONNECTING: record the drop time, freeze new
          vessel elements (the liveness probe now makes ``has_active_session()``
          read false, so beats/perceptions stop and priorities are untouched),
          and retry the connection (:meth:`_attempt_reconnect`) on each sweep
          within the grace window. A reconnection that succeeds flips
          ``is_connected`` back true on the next sweep, restoring CONNECTED.
        * **still dropped past grace** → ENDED: end the session(s) (reason
          ``disconnected``), which flushes the diary *and* purges all queued
          vessel traffic for the world (via ``drop_vessel_queue_for_world`` in
          ``end_session``).

        The grace window is ``VESSEL_DISCONNECT_GRACE_SEC`` (default 30s, clamped
        5–3600) — long enough to absorb transient blips and retry. Fully
        guarded: any failure leaves the session untouched.
        """
        environments = self._active_session_environments()
        if not environments:
            # Nothing active — clear any stale grace timers and bail cheaply.
            if self._disconnected_since:
                self._disconnected_since.clear()
            return

        connectors = self._connector_environments()
        try:
            grace = int(
                config_registry.get_value(
                    "VESSEL_DISCONNECT_GRACE_SEC",
                    30,
                    value_type=int,
                    group="vessel",
                    component="vessel",
                )
            )
        except Exception:
            grace = 30
        grace = max(5, min(3600, grace))
        now = asyncio.get_event_loop().time()

        for environment in environments:
            connector = connectors.get(environment)
            connected = False
            if connector is not None:
                try:
                    connected = bool(getattr(connector, "is_connected", False))
                except Exception:
                    connected = False

            if connected:
                # Live again — drop any pending grace timer for this world.
                self._disconnected_since.pop(environment, None)
                continue

            first_seen = self._disconnected_since.get(environment)
            if first_seen is None:
                self._disconnected_since[environment] = now
                log_debug(
                    f"[vessel_interface] '{environment}' connector reported "
                    f"disconnected — entering RECONNECTING (freeze), {grace}s grace"
                )
                # RECONNECTING state: the session is frozen (has_active_session()
                # now reads false via the liveness probe, so beats/perceptions
                # stop and priorities are untouched) while we try to bring the
                # connector back before the grace elapses.
                await self._attempt_reconnect(environment)
                continue

            if now - first_seen < grace:
                # Still within the reconnection window — keep retrying.
                await self._attempt_reconnect(environment)
                continue

            # Grace elapsed and still disconnected: close the stale session(s).
            log_warning(
                f"[vessel_interface] '{environment}' still disconnected after "
                f"{grace}s grace — closing stale Vessel session(s)"
            )
            try:
                await self.end_sessions_for_environment(
                    environment, reason="disconnected"
                )
            except Exception as exc:
                log_warning(
                    f"[vessel_interface] disconnect close failed for "
                    f"'{environment}': {exc}"
                )
            self._disconnected_since.pop(environment, None)

    async def _attempt_reconnect(self, environment: str) -> None:
        """Try to bring a dropped connector back during the RECONNECTING window.

        Best-effort reconnection attempt fired by the disconnect sweep while a
        world is in the RECONNECTING (frozen) state — the session is still known
        but ``has_active_session()`` reads false, so no new vessel elements are
        produced. Asks the Vessel plugin to reconnect; ``connect_world`` is
        idempotent and reattach-safe (reuses the existing active session). If it
        succeeds the connector's ``is_connected`` flips true on the next sweep,
        which clears the grace timer and restores the CONNECTED state
        automatically — no extra bookkeeping here. Fully guarded: any failure is
        logged and left for the grace window to time out into ENDED.
        """
        try:
            from core.core_initializer import PLUGIN_REGISTRY

            plugin = PLUGIN_REGISTRY.get("vessel_plugin")
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"[vessel_interface] reconnect: plugin lookup failed: {exc}")
            return
        if plugin is None or not hasattr(plugin, "connect_world"):
            return
        try:
            await plugin.connect_world(connector_name=environment)
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(
                f"[vessel_interface] reconnect attempt for '{environment}' "
                f"failed: {exc}"
            )

    def _active_session_environments(self) -> set[str]:
        """Return the set of environments with a locally-tracked session.

        Derives the world name structurally from each tracked interface path
        (``vessel/<world>[/<server>]``). Fully guarded.
        """
        environments: set[str] = set()
        for path in self._sessions.values():
            try:
                parts = path.split("/")
                if len(parts) >= 2 and parts[0] == INTERFACE_NAME and parts[1]:
                    environments.add(parts[1])
            except Exception:
                continue
        return environments

    # ------------------------------------------------------------------
    # Outbound action logging
    # ------------------------------------------------------------------

    def _resolve_session_for_environment(
        self, environment: str
    ) -> tuple[str | None, str | None]:
        """Return ``(session_id, interface_path)`` for a connected world.

        Looks up the locally-tracked session whose interface path matches the
        environment. Returns ``(None, None)`` when no session is tracked (the
        outbound log then falls back to environment-only attribution).
        """
        prefix = f"{INTERFACE_NAME}/{environment}"
        for session_id, path in self._sessions.items():
            if path == prefix or path.startswith(f"{prefix}/"):
                return session_id, path
        return None, None

    def has_local_session(self, environment: str) -> bool:
        """Return whether a live session is locally tracked for ``environment``.

        The vessel plugin uses this to reconcile a live connector whose DB
        session has since ended (inactivity cooldown or a disconnect-grace
        close while the Node bridge stayed embodied): if the connector reports
        connected but no session is tracked here, autonomy is inert until a new
        session is opened. Fully guarded; purely structural (matches the
        ``vessel/<world>`` interface-path prefix).
        """
        return self._resolve_session_for_environment(environment)[0] is not None

    async def log_outbound_action(
        self,
        environment: str,
        action: str,
        summary: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Record one outbound in-world action Synth performed.

        This is the Vessel counterpart of :meth:`on_world_event` for *outgoing*
        actions (``say``/``move``/``look``/...): it buffers the action as lived
        experience and writes an ``action`` row to ``vessel_activity_log`` so
        Synth's own in-world responses appear in the WebUI Activities tab
        alongside the incoming perceptions. Fully guarded — never raises.
        """
        session_id, interface_path = self._resolve_session_for_environment(environment)
        manager = get_vessel_session_manager()
        event_type = f"action_{action}"
        if session_id is not None:
            try:
                await manager.record_experience(
                    session_id=session_id,
                    event_type=event_type,
                    summary=summary,
                    data=metadata,
                )
            except Exception as exc:  # pragma: no cover - defensive
                log_debug(
                    f"[vessel_interface] record_experience (action) failed: {exc}"
                )
        await self._log_activity(
            session_id=session_id,
            interface_path=interface_path,
            environment=environment,
            event_type=event_type,
            summary=summary,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Activity log
    # ------------------------------------------------------------------

    @staticmethod
    async def _log_activity(
        session_id: str | None,
        interface_path: str | None,
        environment: str,
        event_type: str,
        summary: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Insert one row into ``vessel_activity_log`` (WebUI Activities audit)."""
        import json

        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        INSERT INTO vessel_activity_log
                            (session_id, interface_path, environment,
                             event_type, summary, metadata)
                        VALUES (%s, %s, %s, %s, %s, %s)
                        """,
                        (
                            session_id,
                            interface_path,
                            environment,
                            event_type,
                            summary,
                            json.dumps(metadata) if metadata else None,
                        ),
                    )
                await conn.commit()
        except Exception as exc:
            log_debug(f"[vessel_interface] _log_activity failed: {exc}")


def initialize_interface() -> "VesselInterface":
    """Create and register the Vessel interface singleton.

    Called by the core initializer after DB config is loaded. Safe to call
    again to reload the interface.
    """
    global vessel_interface

    if vessel_interface is not None:
        log_info("[vessel_interface] Reloading interface instance...")

    log_info("[vessel_interface] Creating Vessel interface instance...")
    vessel_interface = VesselInterface()
    register_interface(INTERFACE_NAME, vessel_interface)
    log_info("[vessel_interface] Vessel interface instance created and registered")
    return vessel_interface
