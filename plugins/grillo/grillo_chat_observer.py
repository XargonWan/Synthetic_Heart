"""
plugins/grillo/grillo_chat_observer.py

Periodic Chat Observer beat for G.R.I.L.L.O.: periodically sample the last N chat snippets
and propose them to the synth for processing (propose-only by default). The LLM should
respond with valid JSON actions (include a top-level `safe` boolean on actions when
applicable). The plugin creates an activity log entry and enqueues a low-priority
message for LLM processing using the same pattern as other Grillo beats.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import List, Optional

from core.core_initializer import register_plugin
from core.logging_utils import log_info, log_debug, log_warning, log_error
from core.config_manager import config_registry

from plugins.grillo.common_instructions import GRILLO_INSTRUCTIONS as OBSERVER_INSTRUCTIONS


class GrilloChatObserverPlugin:
    display_name = "G.R.I.L.L.O. Chat Observer"

    _scheduler_running = False
    _scheduler_task: Optional[asyncio.Task] = None

    def __init__(self):
        self.enabled = config_registry.get_value(
            "GRILLO_OBSERVER_ENABLED", True,
            label="Enable Grillo Chat Observer",
            description="Enable periodic chat observation and proposal beat",
            value_type=bool,
            group="grillo",
            component="grillo_chat_observer",
        )

        self.interval = int(config_registry.get_value(
            "GRILLO_OBSERVER_INTERVAL", 3600,
            label="Grillo Observer Interval (s)",
            description="Seconds between observer runs (default 3600 = 1 hour)",
            value_type=int,
            group="grillo",
            component="grillo_chat_observer",
        ))

        self.samples = int(config_registry.get_value(
            "GRILLO_OBSERVER_SAMPLES", 10,
            label="Grillo Observer Samples",
            description="Number of recent chat snippets to include in the prompt",
            value_type=int,
            group="grillo",
            component="grillo_chat_observer",
        ))

        self.propose_only = config_registry.get_value(
            "GRILLO_OBSERVER_PROPOSE_ONLY", True,
            label="Grillo Observer Propose Only",
            description="When True, the observer will instruct the LLM to propose actions only (no auto-execution)",
            value_type=bool,
            group="grillo",
            component="grillo_chat_observer",
        )

        register_plugin("grillo_chat_observer", self)
        log_info("[grillo_chat_observer] Registered GrilloChatObserverPlugin")

        # Track last run timestamp for observer to avoid missing messages even
        # if the global checker has already consumed them. This is initialized
        # on start() to the current time to avoid acting on historic messages.
        self._last_run_ts: float = 0.0

        # Config listeners
        config_registry.add_listener("GRILLO_OBSERVER_ENABLED", lambda v: setattr(self, "enabled", bool(v)))
        config_registry.add_listener("GRILLO_OBSERVER_INTERVAL", lambda v: setattr(self, "interval", int(v)))
        config_registry.add_listener("GRILLO_OBSERVER_SAMPLES", lambda v: setattr(self, "samples", int(v)))
        config_registry.add_listener("GRILLO_OBSERVER_PROPOSE_ONLY", lambda v: setattr(self, "propose_only", bool(v)))

    def get_supported_action_types(self):
        return []

    def get_supported_actions(self):
        return {}

    async def start(self):
        if not self.enabled:
            log_info("[grillo_chat_observer] Disabled by configuration; not starting")
            return

        if GrilloChatObserverPlugin._scheduler_task and not GrilloChatObserverPlugin._scheduler_task.done():
            log_debug("[grillo_chat_observer] Scheduler already running")
            return

        GrilloChatObserverPlugin._scheduler_running = True
        GrilloChatObserverPlugin._scheduler_task = asyncio.create_task(self._observer_loop())
        # Initialize last run timestamp to now so we don't process old history
        try:
            self._last_run_ts = float(datetime.utcnow().timestamp())
            log_debug(f"[grillo_chat_observer] Initialized last_run_ts={self._last_run_ts}")
        except Exception:
            pass
        log_info("[grillo_chat_observer] Scheduler started")

    async def stop(self):
        GrilloChatObserverPlugin._scheduler_running = False
        task = GrilloChatObserverPlugin._scheduler_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        GrilloChatObserverPlugin._scheduler_task = None
        log_info("[grillo_chat_observer] Scheduler stopped")

    async def _observer_loop(self):
        log_info("[grillo_chat_observer] Observer loop running")
        try:
            while GrilloChatObserverPlugin._scheduler_running:
                try:
                    # Sleep for interval but keep cancellable
                    await asyncio.sleep(self.interval)
                    if not GrilloChatObserverPlugin._scheduler_running:
                        break

                    await self._run_observer()
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log_error(f"[grillo_chat_observer] Error in observer loop: {e}")
                    await asyncio.sleep(10)
        finally:
            log_info("[grillo_chat_observer] Observer loop exiting")

    async def _run_observer(self):
        try:
            if not self.enabled:
                log_debug("[grillo_chat_observer] Skipping run because disabled")
                return

            # Only run observer if there are new non-self messages since the
            # observer's last run. We query the chat_history_cache directly to
            # avoid races with the global checker (which may consume updates).
            try:
                from core.db import execute_query
                # Ensure last_run_ts initialized
                if not getattr(self, '_last_run_ts', 0.0):
                    self._last_run_ts = float(datetime.utcnow().timestamp())
                    log_debug("[grillo_chat_observer] last_run_ts uninitialized – initializing and skipping first run")
                    return

                rows = await execute_query(
                    """
                    SELECT COUNT(*) as cnt, MAX(UNIX_TIMESTAMP(timestamp)) as max_ts
                    FROM chat_history_cache
                    WHERE UNIX_TIMESTAMP(timestamp) > %s
                      AND COALESCE(sender_id, '') NOT IN (%s, %s)
                      AND COALESCE(sender_name, '') NOT IN (%s, %s)
                    """,
                    (self._last_run_ts, 'self', 'synth', 'self', 'synth'),
                )

                cnt = 0
                max_ts = None
                if rows and len(rows) > 0:
                    r = rows[0]
                    if isinstance(r, dict):
                        cnt = int(r.get('cnt') or 0)
                        max_ts = r.get('max_ts')
                    else:
                        cnt = int(r[0] or 0)
                        max_ts = r[1]

                if cnt == 0:
                    log_debug('[grillo_chat_observer] No new non-self messages since last_run; skipping')
                    return
                else:
                    log_debug(f"[grillo_chat_observer] Found {cnt} new non-self messages since last_run; proceeding")

            except Exception as e:
                log_debug(f"[grillo_chat_observer] Direct DB check failed; falling back to checker: {e}")
                # Fallback to non-consuming peek
                try:
                    from core.chat_update_checker import check_for_updates_once
                    chk = await check_for_updates_once(consume=False)
                    if not chk.get('updated'):
                        log_debug("[grillo_chat_observer] No new messages after fallback; skipping observer run")
                        return
                except Exception as e2:
                    log_debug(f"[grillo_chat_observer] Chat update checker fallback failed; proceeding: {e2}")

            fragments = await self._collect_recent_snippets(self.samples)
            if not fragments:
                log_debug("[grillo_chat_observer] No fragments found; skipping")
                return

            prompt = self._build_observer_prompt(fragments)

            # Activity log entry
            activity_log_id = None
            try:
                from plugins.grillo.grillo_impl import GrilloPlugin
                activity_log_id = await GrilloPlugin.create_activity_log(beat_type="observer", prompt_text=prompt)
            except Exception as e:
                log_debug(f"[grillo_chat_observer] Could not create activity log: {e}")

            # Enqueue as low-priority grillo message
            try:
                from types import SimpleNamespace
                from core import message_queue

                message = SimpleNamespace()
                message.chat_id = -1
                message.message_id = 0
                message.text = prompt
                message.from_user = SimpleNamespace(id=-1, username="grillo", full_name="G.R.I.L.L.O.")
                message.chat = SimpleNamespace(id=-1, type="internal")
                message.date = datetime.utcnow()

                context = {
                    "grillo_beat": True,
                    "beat_type": "observer",
                    "activity_log_id": activity_log_id,
                    "grillo_snippets": fragments,
                    "propose_only": bool(self.propose_only),
                    "include_memories": True,
                }

                await message_queue.enqueue_low_priority(None, message, context_memory=context, interface_id='grillo', original_message=None)
                log_info("[grillo_chat_observer] Observer prompt enqueued for LLM processing")

                # Advance observer last-run to avoid reprocessing the same messages
                try:
                    if max_ts:
                        self._last_run_ts = float(max_ts)
                    else:
                        self._last_run_ts = float(datetime.utcnow().timestamp())
                    log_debug(f"[grillo_chat_observer] Updated last_run_ts to {self._last_run_ts}")
                except Exception:
                    pass
            except Exception as e:
                log_error(f"[grillo_chat_observer] Failed to enqueue observer prompt: {e}")
        except Exception as e:
            log_error(f"[grillo_chat_observer] Unexpected error in _run_observer: {e}")

    async def _collect_recent_snippets(self, limit: int) -> List[str]:
        snippets = []
        try:
            import core.recent_chats as recent_chats
            from core.chat_history_cache import load_chat_history

            last = await recent_chats.get_last_active_chats_verbose(limit * 2)
            for chat_id, name in last:
                if len(snippets) >= limit:
                    break
                chat_path = recent_chats.get_chat_path(chat_id) or f"telegram_bot/{chat_id}"
                try:
                    messages = await load_chat_history(chat_path)
                    # take up to 2 recent messages per chat
                    taken = 0
                    for msg in reversed(list(messages)):
                        if not isinstance(msg, dict):
                            continue
                        text = msg.get('text')
                        sender = msg.get('sender_name') or msg.get('sender_id') or "unknown"
                        timestamp = msg.get('timestamp') or ""
                        if text:
                            snippet = text.strip()
                            if len(snippet) > 300:
                                snippet = snippet[:300] + "..."
                            snippets.append(f"(chat:{chat_path} | sender:{sender} | {timestamp}) {snippet}")
                            taken += 1
                        if taken >= 2 or len(snippets) >= limit:
                            break
                except Exception:
                    continue

            # deduplicate and trim to limit
            if snippets:
                out = []
                seen = set()
                for s in snippets:
                    if s in seen:
                        continue
                    seen.add(s)
                    out.append(s)
                    if len(out) >= limit:
                        break
                return out
            return []
        except Exception as e:
            log_error(f"[grillo_chat_observer] Error collecting snippets: {e}")
            return []

    def _build_observer_prompt(self, snippets: List[str]) -> str:
        header = "[G.R.I.L.L.O. CHAT OBSERVER] Below are recent chat snippets from across conversations. Analyze and propose any actions that would be helpful."

        body = "\n\nSnippets:\n"
        for i, s in enumerate(snippets, 1):
            body += f"{i}. {s}\n"

        propose_clause = "RESPOND WITH VALID JSON: a single object with an 'actions' array."
        if self.propose_only:
            propose_clause += " The actions should be proposals only; do NOT assume automatic execution."
        propose_clause += " Include a top-level boolean 'safe' on each action indicating if you consider it safe to auto-execute."
        # Additional clarity (kept intentionally concise - see OBSERVER_INSTRUCTIONS for full requirements)

        # Use configured synth name in examples to avoid hardcoding 'G.R.I.L.L.O.'
        synth_name = str(config_registry.get_var("SYNTH_NAME", "SyntH"))

        # Keep the propose clause short and rely on OBSERVER_INSTRUCTIONS for detailed bullets/examples
        propose_clause += " See 'Instructions' section below for examples and required JSON format."

        # Use the module-level instructions block so it's easy to maintain and test
        return header + body + propose_clause + OBSERVER_INSTRUCTIONS


PLUGIN_CLASS = GrilloChatObserverPlugin
