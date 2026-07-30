"""Rift Vessel Compactor — end-of-session operational recap plugin.

A **separate scope** from the G.R.I.L.L.O. Compactor (memory synthesis): this
plugin only ever compacts a single Rift Vessel embodiment session, and only when
that session reaches the **ENDED** state (a true disconnect, logout or cooldown
close — never during CONNECTED or RECONNECTING). It reads the session's audit
rows from ``vessel_activity_log`` and produces a factual, third-person
*operational recap* (positions, quantities, world state, actions and their
outcomes) which is stored in the dedicated ``vessel_diary`` table with
``reason = "activity_recap"``. It deliberately does **not** write to the real
``ai_diary`` (that polluted every non-vessel Fast-Lane prompt).

How it wires up (registration pattern — ``core`` never imports this plugin):

* On :meth:`start` it registers a compaction handler into
  ``core.vessel_session_manager`` via ``set_compaction_handler``. When a session
  ends, the manager calls that handler, which simply **enqueues** the session id
  onto this plugin's own low-priority, off-chain worker queue and returns
  immediately (teardown never blocks on the LLM).
* An internal ``asyncio`` worker drains the queue one session at a time, calling
  :func:`core.vessel_diary_compactor.compact_activity_recap` (chunked, fold,
  fail-safe). This is **not** the message chain / ``message_queue`` — no in-world
  turn, no Agent Lane, no Drone (Vessel Fast-Lane constraint).

It can also be triggered manually from the WebUI Plugins tab (the runnable "Run
compaction" quartet → ``run_action("compact_now", ...)``), mirroring the Grillo
compactor's manual trigger. With no payload it recaps the most recent ended
session; a ``session_id`` in the payload targets a specific one.
"""

from __future__ import annotations

import asyncio
from typing import Any

from core.config_manager import config_registry
from core.core_initializer import register_plugin
from core.logging_utils import log_debug, log_error, log_info, log_warning


class VesselCompactorPlugin:
    display_name = "Rift Vessel Compactor"

    def __init__(self) -> None:
        self.enabled: bool = bool(
            config_registry.get_value(
                "VESSEL_COMPACTOR_ENABLED",
                True,
                label="Enable Rift Vessel Compactor",
                description=(
                    "Compact each Rift Vessel session into a factual operational "
                    "recap (stored in vessel_diary) when the session ends."
                ),
                value_type=bool,
                group="plugins",
                component="vessel_compactor",
            )
        )

        # Internal, off-chain, low-priority work queue. Session ids land here
        # when a session reaches the ENDED state (via the handler registered on
        # the session manager) or on a manual run; a single background worker
        # drains it. This is NOT the message chain — no in-world turn is ever
        # produced.
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._worker_task: asyncio.Task[None] | None = None
        self._running: bool = False

        register_plugin("vessel_compactor", self)
        log_info("[vessel_compactor] Registered VesselCompactorPlugin")

        def _update_enabled(val: Any) -> None:
            try:
                self.enabled = bool(val)
                log_info(f"[vessel_compactor] enabled set to {self.enabled}")
            except Exception:
                pass

        config_registry.add_listener("VESSEL_COMPACTOR_ENABLED", _update_enabled)

    # ------------------------------------------------------------------
    # Plugin contract
    # ------------------------------------------------------------------
    def get_supported_actions(self) -> dict:
        """No LLM actions. Compaction is triggered by the session-end handler or
        the WebUI "Run Now" button, never by the LLM emitting an action."""
        return {}

    def get_metadata(self) -> dict:
        """Declare the on-demand "Run Now" button for the WebUI Plugins tab."""
        return {
            "name": "rift_vessel.vessel_compactor",
            "display_name": self.display_name,
            "description": (
                "Compact a Rift Vessel embodiment session into a factual, "
                "third-person operational recap (stored in vessel_diary) when the "
                "session ends. Runs automatically on session end; can also be "
                "triggered manually."
            ),
            "category": "Vessels",
            "runnable": True,
            "run_action": "compact_now",
            "run_label": "Run compaction",
            "run_title": "Compact all pending ended vessel sessions now",
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if not self.enabled:
            log_info(
                "[vessel_compactor] Disabled by configuration; not starting worker"
            )
            return

        # Register the end-of-session compaction handler on the session manager.
        # The manager (core) never imports this plugin; we push a callable in.
        try:
            from core.vessel_session_manager import vessel_session_manager

            vessel_session_manager.set_compaction_handler(self._on_session_ended)
            log_debug("[vessel_compactor] Compaction handler registered")
        except Exception as exc:
            log_error(
                f"[vessel_compactor] Failed to register compaction handler: {exc}"
            )

        if self._worker_task and not self._worker_task.done():
            log_debug("[vessel_compactor] Worker already running")
            return
        self._running = True
        self._worker_task = asyncio.create_task(self._worker_loop())
        log_info("[vessel_compactor] Worker started")

    async def stop(self) -> None:
        self._running = False
        # Deregister the handler so end_session falls back to the legacy inline
        # path once we are gone.
        try:
            from core.vessel_session_manager import vessel_session_manager

            vessel_session_manager.set_compaction_handler(None)
        except Exception:
            pass

        task = self._worker_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._worker_task = None
        log_info("[vessel_compactor] Worker stopped")

    # ------------------------------------------------------------------
    # Session-end handler (called by the session manager on ENDED)
    # ------------------------------------------------------------------
    def _on_session_ended(
        self,
        session_id: str,
        environment: str,
        interface_path: str | None,
        reason: str,
    ) -> None:
        """Enqueue the ended session for a low-priority off-chain recap.

        Called synchronously by ``VesselSessionManager.end_session``. Must return
        immediately and never raise — teardown must not block on the LLM. The
        actual recap happens on the internal worker.
        """
        if not self.enabled:
            return
        if not session_id:
            return
        try:
            self._queue.put_nowait(session_id)
            log_debug(
                f"[vessel_compactor] Queued recap for ended session {session_id} "
                f"({environment}, reason={reason})"
            )
        except Exception as exc:
            log_warning(
                f"[vessel_compactor] Failed to enqueue recap for {session_id}: {exc}"
            )

    async def _worker_loop(self) -> None:
        log_info("[vessel_compactor] Recap worker loop running")
        try:
            while self._running:
                try:
                    session_id = await self._queue.get()
                except asyncio.CancelledError:
                    break
                try:
                    await self._compact_one(session_id)
                except Exception as exc:
                    log_error(
                        f"[vessel_compactor] Recap failed for {session_id}: {exc}"
                    )
                finally:
                    self._queue.task_done()
        finally:
            log_info("[vessel_compactor] Recap worker loop exiting")

    async def _compact_one(self, session_id: str) -> int | None:
        """Compact one session into an operational recap. Fully fail-safe."""
        environment, interface_path = await self._session_facts(session_id)
        from core.vessel_diary_compactor import compact_activity_recap

        return await compact_activity_recap(
            session_id=session_id,
            environment=environment,
            interface_path=interface_path,
            reason="session_ended",
        )

    async def _session_facts(self, session_id: str) -> tuple[str, str | None]:
        """Look up ``(environment, interface_path)`` for a session id."""
        try:
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT environment, interface_path FROM vessel_sessions "
                        "WHERE session_id = %s",
                        (session_id,),
                    )
                    row = await cur.fetchone()
        except Exception as exc:
            log_debug(f"[vessel_compactor] _session_facts failed: {exc}")
            return "unknown", None
        # Column order is fixed by the SELECT above; do NOT rely on
        # ``cur.description`` — PostgresCompatCursor does not expose it.
        columns = ["environment", "interface_path"]
        if not row:
            return "unknown", None
        data = row if isinstance(row, dict) else dict(zip(columns, row))
        environment = str(data.get("environment") or "unknown")
        interface_path = data.get("interface_path")
        return environment, interface_path

    async def _latest_ended_session(self) -> str | None:
        """Return the id of the most recently ended session, if any.

        Note: ``vessel_sessions`` has **no** ``id`` column — its primary key is
        ``session_id`` — so ordering must use the real timestamp columns.
        """
        try:
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT session_id FROM vessel_sessions "
                        "WHERE status = 'ended' "
                        "ORDER BY ended_at DESC NULLS LAST, started_at DESC LIMIT 1"
                    )
                    row = await cur.fetchone()
        except Exception as exc:
            log_debug(f"[vessel_compactor] _latest_ended_session failed: {exc}")
            return None
        if not row:
            return None
        if isinstance(row, dict):
            return row.get("session_id")
        return row[0]

    async def _pending_ended_sessions(self, limit: int = 200) -> list[str]:
        """Return ended sessions that still need a recap.

        A session is *pending* when it is ``ended``, has at least one
        ``vessel_activity_log`` row, and does **not** yet have a ``vessel_diary``
        entry. Oldest-first so a manual "Run Now" clears the backlog in
        chronological order. Fully fail-safe (any error → ``[]``).
        """
        try:
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "SELECT s.session_id FROM vessel_sessions s "
                        "WHERE s.status = 'ended' "
                        "AND EXISTS (SELECT 1 FROM vessel_activity_log a "
                        "            WHERE a.session_id = s.session_id) "
                        "AND NOT EXISTS (SELECT 1 FROM vessel_diary d "
                        "                WHERE d.session_id = s.session_id) "
                        "ORDER BY s.ended_at ASC NULLS LAST, s.started_at ASC "
                        "LIMIT %s",
                        (int(limit),),
                    )
                    rows = await cur.fetchall()
        except Exception as exc:
            log_debug(f"[vessel_compactor] _pending_ended_sessions failed: {exc}")
            return []
        out: list[str] = []
        for row in rows or []:
            sid = row.get("session_id") if isinstance(row, dict) else row[0]
            if sid:
                out.append(str(sid))
        return out

    # ------------------------------------------------------------------
    # Manual trigger (WebUI "Run Now")
    # ------------------------------------------------------------------
    async def run_action(
        self,
        action_type: str,
        payload: dict | None = None,
        context: dict | None = None,
    ) -> dict:
        """Manual trigger. ``compact_now`` recaps sessions immediately.

        Payload: ``{"session_id": "..."}`` to target a single session; omitted
        → recaps the whole backlog of ended sessions that still lack a
        ``vessel_diary`` entry (oldest-first).
        """
        if action_type != "compact_now":
            raise ValueError(f"Unsupported run_action: {action_type}")

        payload = payload or {}
        session_id = str(payload.get("session_id") or "").strip()

        # Targeted single-session recap.
        if session_id:
            log_info(f"[vessel_compactor] run_action compact_now for {session_id}")
            entry_id = await self._compact_one(session_id)
            return {
                "status": "ok" if entry_id else "empty",
                "session_id": session_id,
                "vessel_diary_id": entry_id,
            }

        # No session_id → drain the whole backlog of un-recapped ended sessions.
        pending = await self._pending_ended_sessions()
        if not pending:
            log_info("[vessel_compactor] run_action: no ended session to recap")
            return {"status": "empty", "reason": "no_ended_session"}

        log_info(
            f"[vessel_compactor] run_action compact_now: {len(pending)} pending session(s)"
        )
        results: list[dict] = []
        compacted = 0
        for sid in pending:
            entry_id = await self._compact_one(sid)
            if entry_id:
                compacted += 1
            results.append({"session_id": sid, "vessel_diary_id": entry_id})
        return {
            "status": "ok" if compacted else "empty",
            "pending": len(pending),
            "compacted": compacted,
            "results": results,
        }


# Expose plugin class for the loader.
PLUGIN_CLASS = VesselCompactorPlugin
