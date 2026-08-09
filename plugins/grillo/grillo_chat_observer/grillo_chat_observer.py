"""
plugins/grillo/grillo_chat_observer/grillo_chat_observer.py

Periodic Chat Observer beat for G.R.I.L.L.O.: periodically sample the last N chat snippets
and propose them to the synth for processing (propose-only by default). The LLM should
respond with valid JSON actions (include a top-level `safe` boolean on actions when
applicable). The plugin creates an activity log entry and enqueues a low-priority
message for LLM processing using the same pattern as other Grillo beats.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from typing import Any, Dict, List, Optional

from core.core_initializer import register_plugin
from core.logging_utils import log_info, log_debug, log_warning, log_error
from core.config_manager import config_registry
from core.variables_engine import register_exposed_var

from plugins.grillo.common_instructions import (
    GRILLO_INSTRUCTIONS as OBSERVER_INSTRUCTIONS,
    OBSERVER_PROACTIVE_INSTRUCTIONS,
)


register_exposed_var(
    "GRILLO_OBSERVER_STORE_MEMORIES",
    label="Grillo Observer Store Memories",
    default=True,
    value_type=bool,
    ui_type="boolean",
    description="When enabled, observer snippets are stored as passive memories",
    scope="plugins",
    component="grillo_chat_observer",
    tags=["plugin"],
)

register_exposed_var(
    "GRILLO_OBSERVER_SELF_WINDOW",
    label="Grillo Observer Self-Skip Window (s)",
    default=43200,
    value_type=float,
    ui_type="number",
    description="Seconds during which a chat whose last message comes from the synth is ignored when collecting snippets",
    scope="plugins",
    component="grillo_chat_observer",
    advanced=True,
    tags=["plugin"],
)

register_exposed_var(
    "GRILLO_OBSERVER_SELF_COOLDOWN_DAYS",
    label="Grillo Observer Self-Cooldown (days)",
    default=3,
    value_type=int,
    ui_type="number",
    description="Strict anti-spam guard: a conversation whose last message came from the synth is FORBIDDEN for proactive messaging for this many days. Set to 0 to fall back to the finer-grained minutes cooldown (GRILLO_OBSERVER_SELF_COOLDOWN_MINUTES)",
    scope="plugins",
    component="grillo_chat_observer",
    tags=["plugin"],
)

register_exposed_var(
    "GRILLO_OBSERVER_SELF_COOLDOWN_MINUTES",
    label="Grillo Observer Self-Cooldown (minutes)",
    default=45,
    value_type=int,
    ui_type="number",
    description="Fine-grained anti-spam guard used when the days cooldown is 0: after the synth speaks last in a conversation, proactive messaging to it is blocked for this many minutes. Keep it below GRILLO_OBSERVER_INTERVAL or every other run will be skipped",
    scope="plugins",
    component="grillo_chat_observer",
    tags=["plugin"],
)

register_exposed_var(
    "GRILLO_OUTREACH_QUIET_MINUTES",
    label="Grillo Outreach Quiet Window (minutes)",
    default=15,
    value_type=int,
    ui_type="number",
    description="Active-conversation guard: a chat whose last HUMAN message is younger than this is considered mid-conversation and is skipped by proactive outreach for that run",
    scope="plugins",
    component="grillo_chat_observer",
    tags=["plugin"],
)

register_exposed_var(
    "GRILLO_OBSERVER_ACTIVITY_WINDOW_DAYS",
    label="Grillo Observer Activity Window (days)",
    default=14,
    value_type=int,
    ui_type="number",
    description="Anti-dead-chat gate: a conversation is eligible for decay-driven proactivity only if it had genuine human activity within this many days",
    scope="plugins",
    component="grillo_chat_observer",
    tags=["plugin"],
)

# last_run_ts is purely internal; expose but hide it so UI won't show it
register_exposed_var(
    "GRILLO_OBSERVER_LAST_RUN_TS",
    label="Grillo Observer Last Run TS",
    default=0.0,
    value_type=float,
    ui_type="number",
    description="Internal timestamp of the last observer run (UTC). Do not edit unless debugging.",
    scope="plugins",
    component="grillo_chat_observer",
    advanced=True,
    hidden=True,
    tags=["plugin"],
)


class GrilloChatObserverPlugin:
    display_name = "G.R.I.L.L.O. Chat Observer"

    _scheduler_running = False
    _scheduler_task: Optional[asyncio.Task] = None

    def __init__(self):
        self.enabled = config_registry.get_value(
            "GRILLO_OBSERVER_ENABLED",
            True,
            label="Enable Grillo Chat Observer",
            description="Enable periodic chat observation and proposal beat",
            value_type=bool,
            group="grillo",
            component="grillo_chat_observer",
            hidden=True,
        )

        self.interval = int(
            config_registry.get_value(
                "GRILLO_OBSERVER_INTERVAL",
                3600,
                label="Grillo Observer Interval (s)",
                description="Seconds between observer runs (default 3600 = 1 hour)",
                value_type=int,
                group="grillo",
                component="grillo_chat_observer",
            )
        )

        self.samples = int(
            config_registry.get_value(
                "GRILLO_OBSERVER_SAMPLES",
                10,
                label="Grillo Observer Samples",
                description="Number of recent chat snippets to include in the prompt",
                value_type=int,
                group="grillo",
                component="grillo_chat_observer",
            )
        )

        self.propose_only = config_registry.get_value(
            "GRILLO_OBSERVER_PROPOSE_ONLY",
            True,
            label="Grillo Observer Propose Only",
            description="When True, the observer will instruct the LLM to propose actions only (no auto-execution)",
            value_type=bool,
            group="grillo",
            component="grillo_chat_observer",
        )
        self.store_memories = config_registry.get_value(
            "GRILLO_OBSERVER_STORE_MEMORIES",
            True,
            label="Grillo Observer Store Memories",
            description="Store observer snippets as passive memories",
            value_type=bool,
            group="grillo",
            component="grillo_chat_observer",
            advanced=True,
        )
        # How far back (seconds) we honour the "last message was from self" rule.
        # If the most recent message in a conversation comes from the bot and is
        # younger than this window, the chat will be skipped when gathering
        # snippets. This prevents Grillo from endlessly re‑poking a channel that
        # already has an unanswered synthetic question. Default 12h.
        self.self_skip_window = float(
            config_registry.get_value(
                "GRILLO_OBSERVER_SELF_WINDOW",
                43200,
                label="Grillo Observer Self-Skip Window (s)",
                description="Seconds during which a chat whose last message comes from the synth is ignored when collecting snippets",
                value_type=float,
                group="grillo",
                component="grillo_chat_observer",
                advanced=True,
            )
        )
        # Anti-spam self-cooldown: number of days a path is off-limits for
        # proactive messaging if its most recent message came from the synth.
        # When 0, the finer-grained minutes cooldown below applies instead.
        self.self_cooldown_days = int(
            config_registry.get_value(
                "GRILLO_OBSERVER_SELF_COOLDOWN_DAYS",
                3,
                label="Grillo Observer Self-Cooldown (days)",
                description="Strict anti-spam guard: a conversation whose last message came from the synth is forbidden for proactive messaging for this many days. Set to 0 to use the minutes cooldown instead",
                value_type=int,
                group="grillo",
                component="grillo_chat_observer",
            )
        )
        # Fine-grained self-cooldown used when the days guard is disabled (0).
        # Must stay below the observer interval, otherwise consecutive runs
        # will always land inside the cooldown and outreach halves in cadence.
        self.self_cooldown_minutes = int(
            config_registry.get_value(
                "GRILLO_OBSERVER_SELF_COOLDOWN_MINUTES",
                45,
                label="Grillo Observer Self-Cooldown (minutes)",
                description="Fine-grained anti-spam guard used when the days cooldown is 0: after the synth speaks last in a conversation, proactive messaging to it is blocked for this many minutes",
                value_type=int,
                group="grillo",
                component="grillo_chat_observer",
            )
        )
        # Active-conversation guard: if the last HUMAN message in a chat is
        # younger than this, the conversation is considered live and proactive
        # outreach must not butt into it; the next run re-evaluates.
        self.quiet_minutes = int(
            config_registry.get_value(
                "GRILLO_OUTREACH_QUIET_MINUTES",
                15,
                label="Grillo Outreach Quiet Window (minutes)",
                description="Active-conversation guard: a chat whose last human message is younger than this is skipped by proactive outreach for that run",
                value_type=int,
                group="grillo",
                component="grillo_chat_observer",
            )
        )
        # Anti-dead-chat gate: a path is eligible for decay-driven proactivity
        # only if it had genuine human activity within this many days.
        self.activity_window_days = int(
            config_registry.get_value(
                "GRILLO_OBSERVER_ACTIVITY_WINDOW_DAYS",
                14,
                label="Grillo Observer Activity Window (days)",
                description="Anti-dead-chat gate: a conversation is eligible for decay-driven proactivity only if it had genuine human activity within this many days",
                value_type=int,
                group="grillo",
                component="grillo_chat_observer",
            )
        )
        # persistent storage of last-run timestamp - survives restarts
        self._last_run_ts = float(
            config_registry.get_value(
                "GRILLO_OBSERVER_LAST_RUN_TS",
                0.0,
                label="Grillo Observer Last Run TS",
                description="Internal timestamp (UTC) of the last observer run; used to avoid reprocessing history",
                value_type=float,
                group="grillo",
                component="grillo_chat_observer",
                advanced=True,
                hidden=True,
            )
        )

        register_plugin("grillo_chat_observer", self)
        log_info("[grillo_chat_observer] Registered GrilloChatObserverPlugin")

        # Config listeners
        config_registry.add_listener(
            "GRILLO_OBSERVER_ENABLED", lambda v: setattr(self, "enabled", bool(v))
        )
        config_registry.add_listener(
            "GRILLO_OBSERVER_INTERVAL", lambda v: setattr(self, "interval", int(v))
        )
        config_registry.add_listener(
            "GRILLO_OBSERVER_SAMPLES", lambda v: setattr(self, "samples", int(v))
        )
        config_registry.add_listener(
            "GRILLO_OBSERVER_PROPOSE_ONLY",
            lambda v: setattr(self, "propose_only", bool(v)),
        )
        config_registry.add_listener(
            "GRILLO_OBSERVER_STORE_MEMORIES",
            lambda v: setattr(self, "store_memories", bool(v)),
        )
        config_registry.add_listener(
            "GRILLO_OBSERVER_SELF_WINDOW",
            lambda v: setattr(self, "self_skip_window", float(v)),
        )
        config_registry.add_listener(
            "GRILLO_OBSERVER_SELF_COOLDOWN_DAYS",
            lambda v: setattr(self, "self_cooldown_days", int(v)),
        )
        config_registry.add_listener(
            "GRILLO_OBSERVER_SELF_COOLDOWN_MINUTES",
            lambda v: setattr(self, "self_cooldown_minutes", int(v)),
        )
        config_registry.add_listener(
            "GRILLO_OUTREACH_QUIET_MINUTES",
            lambda v: setattr(self, "quiet_minutes", int(v)),
        )
        config_registry.add_listener(
            "GRILLO_OBSERVER_ACTIVITY_WINDOW_DAYS",
            lambda v: setattr(self, "activity_window_days", int(v)),
        )
        config_registry.add_listener(
            "GRILLO_OBSERVER_LAST_RUN_TS",
            lambda v: setattr(self, "_last_run_ts", float(v)),
        )

    def get_supported_action_types(self):
        return []

    def get_supported_actions(self):
        return {}

    async def start(self):
        if not self.enabled:
            log_info("[grillo_chat_observer] Disabled by configuration; not starting")
            return

        if (
            GrilloChatObserverPlugin._scheduler_task
            and not GrilloChatObserverPlugin._scheduler_task.done()
        ):
            log_debug("[grillo_chat_observer] Scheduler already running")
            return

        GrilloChatObserverPlugin._scheduler_running = True
        GrilloChatObserverPlugin._scheduler_task = asyncio.create_task(
            self._observer_loop()
        )
        # Initialize last run timestamp from persisted config (if any). This
        # allows us to survive process restarts without reprocessing the same
        # conversation history. If the stored value is zero (initial launch) we
        # set it to the current time as before.
        try:
            if self._last_run_ts and self._last_run_ts > 0:
                log_debug(
                    f"[grillo_chat_observer] Loaded last_run_ts={self._last_run_ts} from config"
                )
            else:
                self._last_run_ts = float(datetime.now(timezone.utc).timestamp())
                log_debug(
                    f"[grillo_chat_observer] Initialized last_run_ts={self._last_run_ts}"
                )
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
        # Resume from the persisted last-run timestamp instead of always
        # waiting a fresh full interval. Without this, a process restart
        # (e.g. during dev iteration) resets the wait to `self.interval`
        # every time, and if restarts happen more often than the interval,
        # _run_observer() never gets a chance to fire.
        now = datetime.now(timezone.utc).timestamp()
        elapsed = max(0.0, now - (self._last_run_ts or now))
        next_sleep = max(0.0, self.interval - elapsed)
        if elapsed > 0:
            log_debug(
                f"[grillo_chat_observer] Resuming schedule: {elapsed:.0f}s elapsed "
                f"since last_run_ts, sleeping {next_sleep:.0f}s before next check"
            )
        try:
            while GrilloChatObserverPlugin._scheduler_running:
                try:
                    # Sleep for interval but keep cancellable
                    await asyncio.sleep(next_sleep)
                    next_sleep = self.interval
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
                if not getattr(self, "_last_run_ts", 0.0):
                    self._last_run_ts = float(datetime.now(timezone.utc).timestamp())
                    log_debug(
                        "[grillo_chat_observer] last_run_ts uninitialized – initializing and skipping first run"
                    )
                    return

                since_dt = datetime.fromtimestamp(self._last_run_ts, tz=timezone.utc)

                rows = await execute_query(
                    """
                    SELECT COUNT(*) as cnt, MAX(created_at) as max_ts
                    FROM chat_history_cache
                    WHERE created_at > %s
                      AND COALESCE(sender_id, '') NOT IN (%s, %s)
                      AND COALESCE(sender_name, '') NOT IN (%s, %s)
                    """,
                    (since_dt, "self", "synth", "self", "synth"),
                )

                cnt = 0
                max_ts = None
                if rows and len(rows) > 0:
                    r = rows[0]
                    if isinstance(r, dict):
                        cnt = int(r.get("cnt") or 0)
                        max_ts = r.get("max_ts")
                    else:
                        cnt = int(r[0] or 0)
                        max_ts = r[1]

                max_ts_epoch = None
                if isinstance(max_ts, datetime):
                    if max_ts.tzinfo is None:
                        max_ts_epoch = max_ts.replace(tzinfo=timezone.utc).timestamp()
                    else:
                        max_ts_epoch = max_ts.astimezone(timezone.utc).timestamp()
                elif max_ts is not None:
                    try:
                        max_ts_epoch = float(max_ts)
                    except (TypeError, ValueError):
                        max_ts_epoch = None

                if cnt == 0:
                    # No fresh non-self traffic. Rather than going passive, this
                    # is precisely the "vacuum of initiative" the observer is
                    # meant to overcome: proceed on a decay-driven basis so the
                    # synth can be proactive. The anti-dead-chat and
                    # self-cooldown gates in _collect_eligible_targets keep this
                    # from spamming silent or synth-dominated conversations.
                    decay_driven = True
                    log_debug(
                        "[grillo_chat_observer] No new non-self messages since last_run; entering decay-driven proactive mode"
                    )
                else:
                    decay_driven = False
                    log_debug(
                        f"[grillo_chat_observer] Found {cnt} new non-self messages since last_run; proceeding"
                    )

            except Exception as e:
                decay_driven = False
                log_debug(
                    f"[grillo_chat_observer] Direct DB check failed; falling back to checker: {e}"
                )
                # Fallback to non-consuming peek. Even if the checker reports no
                # updates we still proceed in decay-driven mode (the gates below
                # protect against spam), so a silent network no longer blocks
                # proactivity.
                try:
                    from core.chat_update_checker import check_for_updates_once

                    chk = await check_for_updates_once(consume=False)
                    if not chk.get("updated"):
                        decay_driven = True
                        log_debug(
                            "[grillo_chat_observer] No new messages after fallback; entering decay-driven proactive mode"
                        )
                except Exception as e2:
                    decay_driven = True
                    log_debug(
                        f"[grillo_chat_observer] Chat update checker fallback failed; proceeding in decay-driven mode: {e2}"
                    )

            fragments = await self._collect_recent_snippets(self.samples)

            # Per-path metadata for routable, anti-spam-aware proactivity.
            targets = await self._collect_eligible_targets(self.samples)
            eligible_targets = [t for t in targets if t.get("eligible")]

            # In decay-driven mode there is no fresh traffic to react to, so we
            # need at least one eligible target to speak into; otherwise the
            # whole network is either dead or on cooldown and we stay silent.
            if not fragments and not eligible_targets:
                log_info(
                    "[grillo_chat_observer] No fragments and no eligible targets; skipping"
                )
                return
            if decay_driven and not eligible_targets:
                log_info(
                    "[grillo_chat_observer] Decay-driven run but no eligible targets (dead/cooldown/live); skipping"
                )
                return

            if self.store_memories and fragments:
                await self._store_passive_memories(fragments)

            prompt = self._build_observer_prompt(
                fragments, eligible_targets, decay_driven
            )

            # Activity log entry
            activity_log_id = None
            try:
                from plugins.grillo.grillo_impl import GrilloPlugin

                activity_log_id = await GrilloPlugin.create_activity_log(
                    beat_type="observer", prompt_text=prompt
                )
                # Definitive logging: include activity id and short prompt snippet for traceability
                try:
                    snippet = str(prompt).replace("\n", " ")[:200]
                    log_info(
                        f"[grillo_chat_observer] Activity created: GRILLO_ACTIVITY id={activity_log_id} beat=observer propose_only={self.propose_only} prompt_snippet={snippet}"
                    )
                except Exception:
                    # Non-fatal; continue
                    pass
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
                message.from_user = SimpleNamespace(
                    id=-1, username="grillo", full_name="G.R.I.L.L.O."
                )
                message.chat = SimpleNamespace(id=-1, type="internal")
                message.date = datetime.now(timezone.utc)

                context = {
                    "grillo_beat": True,
                    "beat_type": "observer",
                    "activity_log_id": activity_log_id,
                    "grillo_snippets": fragments,
                    "grillo_targets": eligible_targets,
                    "decay_driven": decay_driven,
                    "propose_only": bool(self.propose_only),
                    "include_memories": True,
                }

                await message_queue.enqueue_low_priority(
                    None,
                    message,
                    context_memory=context,
                    interface_id="grillo",
                    original_message=None,
                    priority=message_queue.PRIORITY_BACKGROUND,
                )
                log_info(
                    "[grillo_chat_observer] Observer prompt enqueued for LLM processing"
                )

                # Advance observer last-run to avoid reprocessing the same messages
                try:
                    if max_ts_epoch is not None:
                        self._last_run_ts = max_ts_epoch
                    else:
                        self._last_run_ts = float(
                            datetime.now(timezone.utc).timestamp()
                        )
                    log_debug(
                        f"[grillo_chat_observer] Updated last_run_ts to {self._last_run_ts}"
                    )
                    # persist in config so restart doesn't reset us
                    try:
                        await config_registry.set_value(
                            "GRILLO_OBSERVER_LAST_RUN_TS", self._last_run_ts
                        )
                    except Exception:
                        log_debug(
                            "[grillo_chat_observer] Failed to persist last_run_ts to config"
                        )
                except Exception:
                    pass
            except Exception as e:
                log_error(
                    f"[grillo_chat_observer] Failed to enqueue observer prompt: {e}"
                )
        except Exception as e:
            log_error(f"[grillo_chat_observer] Unexpected error in _run_observer: {e}")

    @staticmethod
    def _is_self_sender(sender: str) -> bool:
        """True when ``sender`` is the synth itself (self/synth/synthetic)."""
        return str(sender or "").strip().lower() in ("self", "synth", "synthetic")

    @staticmethod
    def _is_placeholder_path(interface_path: str) -> bool:
        """True when an interface path contains a placeholder/garbage segment.

        The model must never be handed an unroutable destination. Real
        interface paths are ``<interface>/<chat_id>`` with an optional numeric
        thread id; anything with a placeholder thread segment (literal
        "no thread..."/"not_provided"/"conversation_..."/"dm"/"each_...",
        overflow-length numbers) is not a real routable path and must be
        filtered out of snippets and eligible targets.
        """
        path = str(interface_path or "").strip()
        if not path or "/" not in path:
            return False
        segments = path.split("/")
        last = segments[-1].lower()
        if len(segments) > 2:
            if any(
                token in last
                for token in (
                    "no thread",
                    "not_provided",
                    "not provided",
                    "conversation_",
                    "each_",
                    "indicated",
                    "unknown",
                )
            ):
                return True
            if last == "dm":
                return True
            if len(segments[-1]) > 20:
                return True
        return False

    async def _collect_recent_snippets(self, limit: int) -> List[str]:
        snippets = []
        try:
            from core.chat_history_cache import load_chat_history
            from core.interface_paths import get_recent_interface_paths

            recent = await get_recent_interface_paths(limit * 2)
            for item in recent:
                if len(snippets) >= limit:
                    break
                chat_path = item.get("interface_path")
                if not chat_path:
                    continue
                chat_path = str(chat_path)
                # Never surface chats whose stored path is a placeholder —
                # the model would copy the garbage path into an action.
                if self._is_placeholder_path(chat_path):
                    continue
                # Vessel history is world-scoped and must never be treated as
                # ordinary cross-conversation observer input.  Ended sessions
                # intentionally retain their durable activity/chat rows for
                # the Vessel history UI, so recency alone cannot be an
                # eligibility signal here.
                from core.interface_path_utils import is_vessel_interface_path

                if is_vessel_interface_path(chat_path):
                    continue
                try:
                    messages = await load_chat_history(chat_path)
                    # if the most recent message belongs to the synth and it was
                    # sent less than `self.self_skip_window` seconds ago, ignore
                    # this chat entirely. this does not affect messages already
                    # queued for processing; it only controls what snippets the
                    # observer hands to the LLM.
                    try:
                        if messages:
                            last_msg = messages[-1]
                            if isinstance(last_msg, dict):
                                sender = (
                                    last_msg.get("sender_name")
                                    or last_msg.get("sender_id")
                                    or ""
                                )
                                ts_str = last_msg.get("timestamp") or ""
                                if sender in ("self", "synth") and ts_str:
                                    try:
                                        ts = datetime.fromisoformat(
                                            ts_str.replace("Z", "+00:00")
                                        )
                                        if ts.tzinfo is None:
                                            ts = ts.replace(tzinfo=timezone.utc)
                                        age = (
                                            datetime.now(timezone.utc) - ts
                                        ).total_seconds()
                                        if age < self.self_skip_window:
                                            # skip this chat
                                            continue
                                    except Exception:
                                        pass
                    except Exception:
                        # defensively ignore any parsing errors and continue
                        pass
                    # take up to 2 recent messages per chat — HUMAN-authored
                    # only. Synth's own messages must never be surfaced as
                    # snippets to "naturally reply to": a small model cannot
                    # reliably distinguish its own output from a human turn and
                    # will talk to itself (self-reply spam).
                    taken = 0
                    for msg in reversed(list(messages)):
                        if not isinstance(msg, dict):
                            continue
                        text = msg.get("text")
                        sender = (
                            msg.get("sender_name") or msg.get("sender_id") or "unknown"
                        )
                        if self._is_self_sender(sender):
                            continue
                        timestamp = msg.get("timestamp") or ""
                        # Relative-age annotation. A bare ISO timestamp is
                        # invisible-as-old to a small model, so it treats a
                        # days-old line as the current moment and continues it
                        # (see AGENTS.md §12 staleness note). Tagging each
                        # snippet with how long ago it was said lets the model
                        # judge staleness itself — no hard age gate, so outreach
                        # always has context behind it.
                        msg_ts = self._parse_ts(timestamp)
                        age_seconds: Optional[float] = None
                        if msg_ts is not None:
                            age_seconds = (now - msg_ts).total_seconds()
                        if text:
                            snippet = text.strip()
                            if len(snippet) > 300:
                                snippet = snippet[:300] + "..."
                            age_label = self._humanize_age(age_seconds)
                            snippets.append(
                                f"(chat:{chat_path} | sender:{sender} | {timestamp} | {age_label}) {snippet}"
                            )
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

    async def _collect_eligible_targets(self, limit: int) -> List[Dict[str, Any]]:
        """Build per-path metadata for proactivity decisions (network-agnostic).

        For each recently active conversation returns a dict with:
        - ``interface_path``: the routable path (e.g. ``telegram_bot/123``)
        - ``last_sender``: who sent the most recent message
        - ``last_from_self``: whether the synth spoke last
        - ``age_seconds``: absolute time delta since the last message
        - ``eligible``: True only if there was genuine human (non-self)
          activity within ``activity_window_days`` (anti-dead-chat gate), the
          self-cooldown is not currently active, AND the conversation is not
          live right now (see ``in_active_conversation``).
        - ``cooldown_active``: True when the synth spoke last within the
          cooldown window. The window is ``self_cooldown_days`` when > 0
          (strict legacy behaviour); otherwise ``self_cooldown_minutes``.
        - ``in_active_conversation``: True when a human spoke last within
          ``quiet_minutes`` — the chat is mid-conversation and outreach must
          not interrupt it; the next run re-evaluates.

        The activation-frame prompt uses this to pick a precise
        ``interface_path`` where a void was detected, instead of routing to a
        placeholder. No roles or interface names are hardcoded.
        """
        targets: List[Dict[str, Any]] = []
        try:
            from core.chat_history_cache import load_chat_history
            from core.interface_paths import get_recent_interface_paths

            now = datetime.now(timezone.utc)
            activity_cutoff = now - timedelta(days=self.activity_window_days)
            if self.self_cooldown_days > 0:
                cooldown_cutoff = now - timedelta(days=self.self_cooldown_days)
            else:
                cooldown_cutoff = now - timedelta(minutes=self.self_cooldown_minutes)
            quiet_cutoff = now - timedelta(minutes=self.quiet_minutes)

            recent = await get_recent_interface_paths(limit * 2)
            for item in recent:
                if len(targets) >= limit:
                    break
                chat_path = item.get("interface_path")
                if not chat_path:
                    continue
                chat_path = str(chat_path)
                # Skip placeholder/garbage paths so the model is never offered
                # an unroutable destination.
                if self._is_placeholder_path(chat_path):
                    continue
                from core.interface_path_utils import is_vessel_interface_path

                if is_vessel_interface_path(chat_path):
                    continue
                # Skip live voice paths — audio-only, cannot receive text.
                if "_live_" in chat_path:
                    continue
                try:
                    messages = await load_chat_history(chat_path)
                except Exception:
                    continue
                if not messages:
                    continue

                last_msg = messages[-1] if isinstance(messages[-1], dict) else {}
                last_sender = str(
                    last_msg.get("sender_name")
                    or last_msg.get("sender_id")
                    or "unknown"
                )
                last_from_self = last_sender in ("self", "synth")

                # Age of the most recent message.
                age_seconds: Optional[float] = None
                last_ts = self._parse_ts(last_msg.get("timestamp"))
                if last_ts is not None:
                    age_seconds = (now - last_ts).total_seconds()

                # Anti-spam self-cooldown: synth spoke last within the window.
                cooldown_active = bool(
                    last_from_self
                    and last_ts is not None
                    and last_ts >= cooldown_cutoff
                )

                # Active-conversation guard: a human spoke last and recently —
                # the chat is live, outreach must not interrupt it this run.
                in_active_conversation = bool(
                    not last_from_self
                    and last_ts is not None
                    and last_ts >= quiet_cutoff
                )

                # Anti-dead-chat gate: genuine human activity within window.
                has_recent_human = False
                for msg in reversed(list(messages)):
                    if not isinstance(msg, dict):
                        continue
                    sender = msg.get("sender_name") or msg.get("sender_id") or ""
                    if sender in ("self", "synth", "-1"):
                        continue
                    ts = self._parse_ts(msg.get("timestamp"))
                    if ts is not None and ts >= activity_cutoff:
                        has_recent_human = True
                        break

                eligible = (
                    has_recent_human
                    and not cooldown_active
                    and not in_active_conversation
                )

                targets.append(
                    {
                        "interface_path": chat_path,
                        "last_sender": last_sender,
                        "last_from_self": last_from_self,
                        "age_seconds": age_seconds,
                        "cooldown_active": cooldown_active,
                        "in_active_conversation": in_active_conversation,
                        "has_recent_human": has_recent_human,
                        "eligible": eligible,
                    }
                )
        except Exception as e:
            log_error(f"[grillo_chat_observer] Error collecting targets: {e}")
        return targets

    @staticmethod
    def _humanize_age(age_seconds: Optional[float]) -> str:
        """Render a relative-age label so snippet staleness is visible to the
        LLM (e.g. ``age:just now``, ``age:3h ago``, ``age:2d ago``)."""
        if age_seconds is None:
            return "age:unknown"
        if age_seconds < 90:
            return "age:just now"
        minutes = age_seconds / 60.0
        if minutes < 90:
            return f"age:{int(round(minutes))}m ago"
        hours = age_seconds / 3600.0
        if hours < 48:
            return f"age:{int(round(hours))}h ago"
        days = age_seconds / 86400.0
        return f"age:{int(round(days))}d ago"

    @staticmethod
    def _parse_ts(value: Any) -> Optional[datetime]:
        """Parse a chat_history timestamp into an aware UTC datetime."""
        if value is None:
            return None
        if isinstance(value, datetime):
            return (
                value.replace(tzinfo=timezone.utc)
                if value.tzinfo is None
                else value.astimezone(timezone.utc)
            )
        try:
            ts = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts
        except Exception:
            return None

    async def _store_passive_memories(self, snippets: List[str]) -> None:
        """Persist observer snippets as passive memories when enabled."""
        try:
            from core.db import insert_memory

            tags = json.dumps(["grillo", "observer", "passive"])
            for snippet in snippets:
                try:
                    await insert_memory(
                        content=snippet,
                        author="observer",
                        source="grillo_observer",
                        tags=tags,
                        scope="observer",
                    )
                except Exception as e:
                    log_debug(f"[grillo_chat_observer] Failed to store memory: {e}")
            log_info(
                f"[grillo_chat_observer] Stored {len(snippets)} observer snippets as memories"
            )
        except Exception as e:
            log_warning(f"[grillo_chat_observer] Memory storage failed: {e}")

    def _build_observer_prompt(
        self,
        snippets: List[str],
        targets: Optional[List[Dict[str, Any]]] = None,
        decay_driven: bool = False,
    ) -> str:
        header = (
            "[G.R.I.L.L.O. CHAT OBSERVER] Below are chat snippets from across conversations. "
            "Each snippet is tagged with an 'age:' marker showing how long ago it was said. "
            "Treat older snippets as historical context, NOT as the current moment — do not "
            "continue or reply to a stale line as if it just happened. Analyze and propose any "
            "actions that would be genuinely helpful right now."
        )

        body = "\n\nSnippets:\n"
        if snippets:
            for i, s in enumerate(snippets, 1):
                body += f"{i}. {s}\n"
        else:
            body += "(no fresh snippets — the network is quiet)\n"

        # Render the eligible routing targets so the model can pick a real,
        # precise interface_path instead of hallucinating one.
        targets_block = ""
        if targets:
            targets_block = "\n\nELIGIBLE TARGETS (routable interface_path values you may reach out to):\n"
            for t in targets:
                path = t.get("interface_path", "")
                age = t.get("age_seconds")
                try:
                    age_h = f"{float(age) / 3600.0:.1f}h" if age is not None else "?"
                except Exception:
                    age_h = "?"
                if t.get("cooldown_active"):
                    cd = "ON-COOLDOWN(OFF-LIMITS)"
                elif t.get("in_active_conversation"):
                    cd = "LIVE-CONVERSATION(OFF-LIMITS)"
                else:
                    cd = "ok"
                last = t.get("last_sender") or "?"
                targets_block += f"- interface_path={path} | idle={age_h} | last_sender={last} | cooldown={cd}\n"
        else:
            targets_block = "\n\nELIGIBLE TARGETS: (none currently eligible — do NOT reach out to anyone)\n"

        decay_note = ""
        if decay_driven:
            decay_note = (
                "\n\nNOTE: There is no fresh incoming traffic right now. If — and only if — you have a genuine internal reason, "
                'you may proactively reach out to one of the eligible targets above. Otherwise return {"actions": []}.\n'
            )

        # Ask the LLM to think like a helpful participant: choose which recent message(s) you'd naturally reply to and propose short, human replies.
        propose_clause = (
            "Think like a helpful human reading these snippets: which message(s) would you naturally reply to, and what would you say? "
            "Do NOT propose messages that are conceptually duplicate of what already appears in the snippets. "
            "Do NOT address or mention the WebUI or any system/internal labels (for example: 'webui' or 'system'); write as if speaking directly to the human participant(s) in the conversation."
        )
        if self.propose_only:
            propose_clause += " Suggested actions should be proposals only (do NOT assume automatic execution)."
        propose_clause += (
            " Return ONLY a JSON object with an 'actions' array (see examples below)."
        )

        # Keep the propose clause short and rely on OBSERVER_INSTRUCTIONS for
        # friendly examples and required JSON format, then append the proactive
        # activation-frame instructions.
        prompt = (
            header
            + body
            + targets_block
            + decay_note
            + propose_clause
            + OBSERVER_INSTRUCTIONS
            + OBSERVER_PROACTIVE_INSTRUCTIONS
        )
        return prompt


PLUGIN_CLASS = GrilloChatObserverPlugin
