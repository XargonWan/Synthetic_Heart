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
        # active session bookkeeping (session_id -> interface_path)
        self._sessions: dict[str, str] = {}
        # Autonomy pacing (monotonic clocks). Volition (the slow "will beat",
        # an LLM cognition turn that sets/updates the goal) and motorics (the
        # fast, prompt-less "motor tick" that steps the body toward the goal)
        # are paced independently — see core.vessel_beat and AGENTS.md §5c.
        self._last_will_beat_at: float = 0.0
        self._last_motor_tick_at: float = 0.0
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
                if getattr(result, "ok", False):
                    log_info(
                        f"[vessel_interface] Reattached active session in "
                        f"'{environment}' after restart"
                    )
                else:
                    # The bot could not be brought back in-world (bridge dead or
                    # world server unreachable). Close the stale active row so it
                    # does not linger as a ghost that keeps gameplay verbs hidden
                    # while looking connected in the DB.
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
        return len(targets)

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
        """
        interface_path = build_interface_path(
            INTERFACE_NAME, environment, server, entity
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

        await self._enqueue_perception(
            interface_path=interface_path,
            summary=summary,
            environment=environment,
            event_type=event_type,
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

        Two structural gates only (no natural-language matching):

        * **Dedup**: identical (event_type, summary) within ``_DEDUP_WINDOW_SEC``
          is dropped.
        * **Rate-limit**: at most one forwarded event per ``_RATE_LIMIT_SEC``.
        """
        now = time.time()
        signature = f"{event_type}\x1f{summary}"

        # prune old signatures
        cutoff = now - _DEDUP_WINDOW_SEC
        self._recent_signatures = {
            sig: ts for sig, ts in self._recent_signatures.items() if ts >= cutoff
        }

        if self._recent_signatures.get(signature, 0.0) >= cutoff:
            return False
        self._recent_signatures[signature] = now

        if now - self._last_enqueue_at < _RATE_LIMIT_SEC:
            return False
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
    ) -> None:
        """Wrap a perception as a normal message and enqueue it (Fast Lane)."""
        try:
            from core import message_queue

            wrapped = SimpleNamespace(
                message_id=None,
                chat_id=interface_path,
                interface_path=interface_path,
                text=summary,
                caption=None,
                date=None,
                thread_id=None,
                from_user=SimpleNamespace(
                    id=environment,
                    username=environment,
                    full_name=f"{environment} world",
                ),
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

    # Fine-grained scheduler tick. The cooldown sweep runs at most once a
    # minute (via ``_last_cooldown_sweep_at``) while the autonomous decision
    # beat is paced by ``VESSEL_BEAT_INTERVAL_SEC`` (see vessel_beat.py).
    _TICK_SEC = 10.0

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
                # Autonomous play: only while a session is active and enabled.
                # Volition first (may set a fresh goal), then motorics acts on it.
                await self._maybe_run_will_beat()
                await self._maybe_run_motor_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_warning(f"[vessel_interface] scheduler tick failed: {exc}")
            await asyncio.sleep(self._TICK_SEC)

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

        goal = await self._active_goal(connector)
        try:
            result = await connector.motor_step(goal)
        except Exception as exc:
            log_debug(f"[vessel_interface] motor_step failed: {exc}")
            return
        if isinstance(result, dict) and result.get("acted"):
            log_debug(
                f"[vessel_interface] motor tick: {result.get('action')} "
                f"(interval={interval}s)"
            )

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
