"""
plugins/grillo/grillo_llm_failure_recovery.py

G.R.I.L.L.O. LLM-Failure Recovery plugin.

Scans the ``llm_failure_log`` table for recent ``llm_fallback`` failures and
attempts to recover them by regenerating the response that should have been
sent instead of the fallback message.

Key design points (per project requirements):
- This is a Grillo beat-style plugin: it does NOT register any user-facing
  action. It only polls failures and asks the core to regenerate a message.
- It simply re-injects the original user text (or, when that is unavailable, a
  recovery prompt) into the message chain so the proper reply is generated and
  delivered through the normal interface pipeline.
- CRITICAL ANTI-LOOP GUARANTEE: every failure that enters recovery is marked as
  processed (in-memory + DB metadata) in a ``finally`` block, EVEN IF the
  regeneration fails or the interface cannot deliver/cancel the fallback.
  Without this, the same un-processed failure would re-appear on every scan and
  the plugin would spam new messages forever, believing there is always a fresh
  LLM failure.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core.ai_plugin_base import AIPluginBase
from core.config_manager import config_registry
from core.logging_utils import log_debug, log_error, log_info, log_warning


class GrilloLLMFailureRecoveryPlugin(AIPluginBase):
    display_name = "G.R.I.L.L.O. LLM-Failure Recovery"

    # How often to scan for new failures (seconds).
    _DEFAULT_INTERVAL = 120
    # Only consider failures newer than this window (minutes). Older failures
    # are assumed already handled or intentionally abandoned.
    _DEFAULT_WINDOW_MIN = 30

    def __init__(self):
        super().__init__()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        # In-memory guard: failure ids already processed this process lifetime.
        self._processed_ids: set[int] = set()
        # Per-interface_path recovery timestamps (for rate limiting).
        self._last_recovery: dict[str, float] = {}

    def get_supported_actions(self) -> dict:
        # Internal plugin — no user-facing actions.
        return {}

    async def start(self) -> None:
        if self._running:
            log_debug("[grillo_failure_recovery] already running")
            return
        self._running = True
        self._task = asyncio.create_task(self._recovery_loop())
        log_info("[grillo_failure_recovery] 🦗 recovery loop started")

    async def stop(self) -> None:
        self._running = False
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except Exception:
                pass
        log_info("[grillo_failure_recovery] recovery loop stopped")

    # ------------------------------------------------------------------
    # Configuration helpers
    # ------------------------------------------------------------------
    def _interval(self) -> int:
        try:
            return int(
                config_registry.get_value(
                    "GRILLO_FAILURE_RECOVERY_INTERVAL", self._DEFAULT_INTERVAL
                )
            )
        except Exception:
            return self._DEFAULT_INTERVAL

    def _window_min(self) -> int:
        try:
            return int(
                config_registry.get_value(
                    "GRILLO_FAILURE_RECOVERY_WINDOW_MIN", self._DEFAULT_WINDOW_MIN
                )
            )
        except Exception:
            return self._DEFAULT_WINDOW_MIN

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------
    async def _recovery_loop(self) -> None:
        while self._running:
            try:
                await self._scan_and_recover()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log_error(f"[grillo_failure_recovery] scan error: {e}")
            await asyncio.sleep(self._interval())

    async def _scan_and_recover(self) -> None:
        from core.llm_failure_log import list_failure_entries

        try:
            result = await list_failure_entries(
                stage="llm_fallback",
                per_page=50,
                sort="desc",
            )
        except Exception as e:
            log_debug(f"[grillo_failure_recovery] list_failure_entries failed: {e}")
            return

        entries = result.get("entries", []) if isinstance(result, dict) else []
        if not entries:
            return

        window = timedelta(minutes=self._window_min())
        now = datetime.now(timezone.utc)
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        for entry in entries:
            try:
                await self._recover_one(entry, now, window)
            except Exception as e:
                log_error(
                    f"[grillo_failure_recovery] recover_one failed for "
                    f"entry {entry.get('id')}: {e}"
                )

    async def _recover_one(
        self, entry: dict[str, Any], now: datetime, window: timedelta
    ) -> None:
        entry_id = entry.get("id")
        if entry_id is None:
            return

        # Already processed in this process lifetime.
        if entry_id in self._processed_ids:
            return

        # Already marked processed by a previous run (persisted in metadata).
        metadata = entry.get("metadata") or {}
        if isinstance(metadata, dict) and metadata.get("processed_by_recovery"):
            self._processed_ids.add(entry_id)
            return

        # Only handle recent failures.
        created_raw = entry.get("created_at")
        created = _coerce_datetime(created_raw)
        if created is not None:
            if now.tzinfo is None and created.tzinfo is not None:
                now = now.replace(tzinfo=created.tzinfo)
            if (now - created) > window:
                # Too old — mark processed so we never revisit it.
                await self._mark_processed(entry_id, entry)
                return

        interface_path = entry.get("interface_path") or ""
        if not interface_path:
            # Cannot route a recovery without a target — mark processed.
            await self._mark_processed(entry_id, entry)
            return

        # Rate-limit per interface_path (one recovery per window).
        if not self._rate_limit_ok(interface_path):
            return

        # Attempt recovery. Regardless of outcome, the finally block marks the
        # failure processed so it leaves the cycle and cannot cause spam.
        try:
            await self._do_recover(entry, interface_path)
        except Exception as e:
            log_error(
                f"[grillo_failure_recovery] recovery attempt failed for "
                f"{interface_path} entry {entry_id}: {e}"
            )
        finally:
            await self._mark_processed(entry_id, entry)
            self._last_recovery[interface_path] = now.timestamp()

    def _rate_limit_ok(self, interface_path: str) -> bool:
        last = self._last_recovery.get(interface_path)
        if last is None:
            return True
        elapsed = datetime.now(timezone.utc).timestamp() - last
        return elapsed >= (self._window_min() * 60)

    # ------------------------------------------------------------------
    # Recovery action
    # ------------------------------------------------------------------
    async def _do_recover(self, entry: dict[str, Any], interface_path: str) -> None:
        from core.chat_history_cache import get_last_message
        from core.interface_path_utils import parse_interface_path

        interface_name, levels = parse_interface_path(interface_path)
        chat_id = levels[0] if levels else None
        thread_id = levels[1] if len(levels) > 1 else None

        # Recover the original user text if the failure recorded it.
        original_text = (entry.get("content_preview") or "").strip()

        # Fallback: recover the last user message from history so we can
        # regenerate a meaningful reply when the preview is missing.
        if not original_text:
            try:
                last = await get_last_message(interface_path)
                if isinstance(last, dict):
                    sender_id = last.get("sender_id")
                    # Only use it if it was a user message (not synth's own).
                    if str(sender_id) not in ("self", "synth", "grillo", ""):
                        original_text = (last.get("text") or "").strip()
            except Exception as e:
                log_debug(f"[grillo_failure_recovery] get_last_message failed: {e}")

        if original_text:
            # Regenerate the proper reply by re-injecting the original user text.
            await self._regenerate_and_send(
                interface_name=interface_name,
                interface_path=interface_path,
                chat_id=chat_id,
                thread_id=thread_id,
                text=original_text,
            )
        else:
            # No original text available: ask for a fresh recovery message.
            prompt = (
                "The previous response failed (LLM timeout/error) and only a "
                "fallback message was delivered. Generate a fresh, helpful message "
                "for this chat to recover the conversation. "
                "Return ONLY the response text (no JSON, no commentary)."
            )
            await self._regenerate_and_send(
                interface_name=interface_name,
                interface_path=interface_path,
                chat_id=chat_id,
                thread_id=thread_id,
                text=prompt,
            )

    async def _regenerate_and_send(
        self,
        *,
        interface_name: str,
        interface_path: str,
        chat_id: Optional[str],
        thread_id: Optional[str],
        text: str,
    ) -> None:
        from core.message_chain import handle_incoming_message

        # Obtain the interface bot instance so the regenerated message is
        # delivered through the correct channel.
        bot = await self._get_bot(interface_name)
        if bot is None:
            log_warning(
                f"[grillo_failure_recovery] no bot for interface {interface_name}; "
                "cannot deliver recovery message"
            )
            return

        from types import SimpleNamespace
        from datetime import datetime as _dt, timezone as _tz

        message = SimpleNamespace()
        message.chat_id = chat_id
        message.message_id = f"grillo_recovery_{interface_path}"
        message.text = text
        message.interface_path = interface_path
        message.thread_id = thread_id
        message.from_user = SimpleNamespace(
            id=-1, username="grillo_recovery", full_name="G.R.I.L.L.O. Recovery"
        )
        message.chat = SimpleNamespace(id=chat_id, type="internal")
        message.date = _dt.now(_tz.utc)

        context = {
            "interface": interface_name,
            "interface_path": interface_path,
            "thread_id": thread_id,
            "grillo_recovery": True,
            # Do not pollute history with the recovery prompt / meta text.
            "skip_history": True,
        }

        try:
            await handle_incoming_message(
                bot,
                message,
                text,
                source="user",
                context=context,
            )
            log_info(
                f"[grillo_failure_recovery] ✅ recovery delivered for {interface_path}"
            )
        except Exception as e:
            log_error(f"[grillo_failure_recovery] handle_incoming_message failed: {e}")

    async def _get_bot(self, interface_name: str):
        try:
            from core.core_initializer import INTERFACE_REGISTRY
            from core.interfaces_registry import get_interface_registry

            registry = get_interface_registry()
            iface = registry.get_interface(interface_name)
            if iface is not None:
                return iface
            # Fallback to core initializer registry.
            return INTERFACE_REGISTRY.get(interface_name)
        except Exception as e:
            log_debug(f"[grillo_failure_recovery] _get_bot failed: {e}")
            return None

    # ------------------------------------------------------------------
    # Mark processed (the anti-loop guarantee)
    # ------------------------------------------------------------------
    async def _mark_processed(self, entry_id: int, entry: dict[str, Any]) -> None:
        self._processed_ids.add(entry_id)
        try:
            from core.llm_failure_log import mark_failure_processed

            await mark_failure_processed(entry_id)
        except Exception:
            # In-memory guard still prevents re-processing this process lifetime.
            pass


def _coerce_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            s = value.replace("Z", "+00:00")
            return datetime.fromisoformat(s)
        except Exception:
            return None
    return None


PLUGIN_CLASS = GrilloLLMFailureRecoveryPlugin
