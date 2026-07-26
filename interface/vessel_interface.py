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

vessel_interface: "VesselInterface | None" = None


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
        # Last time a *player* interacted with Synth in-world (monotonic clock).
        # Updated whenever a salient player-originated perception is enqueued
        # (an in-world ``chat`` line carrying an actor). Used by
        # :meth:`_maybe_run_will_beat` to defer the autonomous "quiet moment to
        # reflect… on your own" volition turn while a player is actively
        # present/talking, so a direct address is answered as a normal reactive
        # chat turn instead of being swallowed by a "you are alone" prompt.
        # Structural (actor-based), never keyword matching.
        self._last_player_activity_at: float = 0.0
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
        if self._scheduler_task is None or self._scheduler_task.done():
            self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        await self._maybe_autostart_bridge()
        await self._reattach_active_sessions()
        log_info("[vessel_interface] Vessel interface started")

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
            return

        if not environments:
            return

        try:
            from core.core_initializer import PLUGIN_REGISTRY

            plugin = PLUGIN_REGISTRY.get("vessel_plugin")
        except Exception as exc:
            log_debug(f"[vessel_interface] reattach: plugin lookup failed: {exc}")
            plugin = None
        if plugin is None or not hasattr(plugin, "connect_world"):
            log_debug("[vessel_interface] reattach: vessel_plugin unavailable")
            return

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

    async def begin_session(self, environment: str, server: str | None = None) -> str:
        """Open an embodiment session for ``environment`` and return its id."""
        interface_path = build_interface_path(INTERFACE_NAME, environment, server)
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
        server: str | None = None,
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
        interface_path = build_interface_path(INTERFACE_NAME, environment, server)
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
            await add_message_to_context(
                interface_path=interface_path,
                message_text=summary,
                sender_name=speaker,
                sender_id=speaker_id,
                metadata=perception_meta,
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
            no_compact = is_player_chat

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
                # Volition first (may set a fresh goal), then motorics acts on it.
                await self._maybe_run_will_beat()
                # If volition left the goal without a reachable target/destination,
                # translate the idea into a concrete waypoint out of band (Drone).
                await self._maybe_run_drone_planner()
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

        interval = vessel_beat.resolve_will_interval(_cfg)
        now = asyncio.get_event_loop().time()
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

    @staticmethod
    def _goal_has_route(goal: dict[str, Any] | None) -> bool:
        """True when the goal already carries a concrete waypoint to walk to.

        Purely **structural** (never keyword matching on the free text): a goal
        is "routable" when it names a block/entity **target** (a closed-enum
        ``target_kind`` of ``block``/``entity`` with a non-empty
        ``target_name``) *or* a numeric **destination** the motor tick can steer
        toward. When both are absent the motor tick can only fall back to the
        directional march — the situation the out-of-band Drone planner exists
        to resolve. Fully guarded.
        """
        if not isinstance(goal, dict):
            return False
        try:
            kind = goal.get("target_kind")
            name = goal.get("target_name")
            if kind in ("block", "entity") and isinstance(name, str) and name.strip():
                return True
            dest = goal.get("destination")
            if isinstance(dest, dict):
                if isinstance(dest.get("x"), (int, float)) and isinstance(
                    dest.get("z"), (int, float)
                ):
                    return True
            # Flat form some worlds publish alongside the goal.
            if isinstance(goal.get("destination_x"), (int, float)) and isinstance(
                goal.get("destination_z"), (int, float)
            ):
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
            result = await manager.run_drone(goal=drone_goal)
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
                f"reason={result.get('reason')} remaining={result.get('remaining')} "
                f"dest={result.get('destination')} (interval={interval}s)"
            )

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
        """Force-close active sessions whose connector has dropped.

        Distinct from the (much longer) inactivity cooldown: this is the safety
        net for a *hung* client/bridge. When a world's connector reports
        ``is_connected == False`` while its session is still ``active`` in the
        DB, ``has_active_session()`` would otherwise stay true for the whole
        cooldown window (default 1h) — during which the scheduler keeps firing
        will beats that accumulate and starve the message flow.

        For each active session we probe the matching connector's ``is_connected``
        flag (structural, keyword-free). A connector must stay disconnected for
        ``VESSEL_DISCONNECT_GRACE_SEC`` (default 30s, clamped 5–3600) — absorbing
        transient blips — before its sessions are ended (reason ``disconnected``),
        which flushes the diary and flips ``has_active_session()`` false so the
        beats stop. Fully guarded: any failure leaves the session untouched.
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
                    f"disconnected — starting {grace}s grace"
                )
                continue

            if now - first_seen < grace:
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
