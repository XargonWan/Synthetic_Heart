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
    # Cooldown scheduler
    # ------------------------------------------------------------------

    async def _scheduler_loop(self) -> None:
        """Periodically close idle sessions past the inactivity cooldown."""
        while True:
            try:
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
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log_warning(f"[vessel_interface] scheduler tick failed: {exc}")
            await asyncio.sleep(60)

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
