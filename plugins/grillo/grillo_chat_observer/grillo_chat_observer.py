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
    label="Grillo Outbound Duplicate Window (s)",
    default=43200,
    value_type=float,
    ui_type="number",
    description="Window (seconds) in which an identical outbound Grillo message to the same conversation is suppressed as a duplicate. Separately from this, GRILLO_OUTREACH_BLOCK_ON_SELF_LAST decides whether speaking last in a conversation makes it ineligible for proactive outreach",
    scope="plugins",
    component="grillo_chat_observer",
    advanced=True,
    tags=["plugin"],
)

register_exposed_var(
    "GRILLO_OUTREACH_QUIET_MINUTES",
    label="Grillo Outreach Quiet Window (minutes)",
    default=15,
    value_type=int,
    ui_type="number",
    description="Live-conversation guard: a chat whose most recent message (from either the human or the synth) is younger than this is considered mid-conversation and is skipped by proactive outreach for that run",
    scope="plugins",
    component="grillo_chat_observer",
    tags=["plugin"],
)

register_exposed_var(
    "GRILLO_OUTREACH_BLOCK_ON_SELF_LAST",
    label="Block Outreach When Synth Spoke Last",
    default=True,
    value_type=bool,
    ui_type="bool",
    description="Awaiting-reply gate: when the synth's own message is the newest in a conversation, that conversation is off-limits for proactive outreach until the human replies or the window below expires. Turn this off to let outreach reach into a conversation the synth already spoke last in (the behaviour that made the hourly beat nag the same DM). Leave it on unless the gate is measurably silencing every outreach",
    scope="plugins",
    component="grillo_chat_observer",
    tags=["plugin"],
)

register_exposed_var(
    "GRILLO_OUTREACH_SELF_LAST_WINDOW_MINUTES",
    label="Outreach Self-Last Window (minutes)",
    default=720,
    value_type=int,
    ui_type="number",
    description="How long a conversation stays off-limits after the synth spoke last in it, in minutes (default 720 = 12 h). Only used while Block Outreach When Synth Spoke Last is on. Set it to 0 to hold a conversation off-limits indefinitely until the human replies, or to a small value to release it quickly. Keep it above the observer interval if you do not want every run to find nothing eligible",
    scope="plugins",
    component="grillo_chat_observer",
    advanced=True,
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
        # Live-conversation guard: a chat whose most recent message (from the
        # human OR from the synth) is younger than this is a conversation
        # happening right now, and outreach must not butt into it; the next run
        # re-evaluates.
        #
        # This is the *other* of the two gates. The second is the awaiting-reply
        # gate below (``block_on_self_last``): this one defers outreach to a
        # live exchange, that one keeps the beat out of a thread whose last word
        # was the synth's own. The beat owns the cadence
        # (GRILLO_OBSERVER_INTERVAL); together these two decide which threads a
        # run may speak into.
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
        # Awaiting-reply gate. A synth that answers everything is the newest
        # speaker in every chat it takes part in, so with this off the hourly
        # beat re-offered the same DM every run and nagged it ("still coming
        # tonight?" -> "hurry home!" -> "did you get home okay?" — five
        # consecutive observer beats). It was removed once on the grounds that
        # it matched every real conversation and made outreach structurally
        # impossible, which is true of a gate with no window and no toggle —
        # hence the window and the toggle here. Default ON, because an
        # unanswered conversation is the case where re-asking is nagging, and
        # the human replying releases the hold immediately.
        self.block_on_self_last = config_registry.get_value(
            "GRILLO_OUTREACH_BLOCK_ON_SELF_LAST",
            True,
            label="Block Outreach When Synth Spoke Last",
            description="Awaiting-reply gate: a conversation whose newest message is the synth's own is off-limits for proactive outreach until the human replies",
            value_type=bool,
            group="grillo",
            component="grillo_chat_observer",
        )
        self.self_last_window_minutes = int(
            config_registry.get_value(
                "GRILLO_OUTREACH_SELF_LAST_WINDOW_MINUTES",
                720,
                label="Outreach Self-Last Window (minutes)",
                description="How long a conversation stays off-limits after the synth spoke last, in minutes; 0 means until the human replies",
                value_type=int,
                group="grillo",
                component="grillo_chat_observer",
                advanced=True,
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
            "GRILLO_OUTREACH_QUIET_MINUTES",
            lambda v: setattr(self, "quiet_minutes", int(v)),
        )
        config_registry.add_listener(
            "GRILLO_OUTREACH_BLOCK_ON_SELF_LAST",
            lambda v: setattr(self, "block_on_self_last", bool(v)),
        )
        config_registry.add_listener(
            "GRILLO_OUTREACH_SELF_LAST_WINDOW_MINUTES",
            lambda v: setattr(self, "self_last_window_minutes", int(v)),
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

                # Freshness is a WALL-CLOCK question, not a cursor question.
                # "Newer than last_run" stops meaning "live" the moment the
                # process is away for longer than one cadence: the newest message
                # can be hours old and still newer than the cursor, and calling
                # that fresh traffic drops the proactive note further down (and
                # leaves the header's "do not reply to a stale line" in force),
                # so a run that comes back after an outage answers nothing and
                # outreach silently stops happening.
                now_ts = datetime.now(timezone.utc).timestamp()
                freshness_window = float(max(60, int(self.interval)))
                newest_is_recent = (
                    max_ts_epoch is None or (now_ts - max_ts_epoch) <= freshness_window
                )
                if cnt == 0 or not newest_is_recent:
                    # No live non-self traffic. Rather than going passive, this
                    # is precisely the "vacuum of initiative" the observer is
                    # meant to overcome: proceed on a decay-driven basis so the
                    # synth can be proactive. The anti-dead-chat and
                    # self-cooldown gates in _collect_eligible_targets keep this
                    # from spamming silent or synth-dominated conversations.
                    decay_driven = True
                    if max_ts_epoch is not None:
                        log_debug(
                            f"[grillo_chat_observer] {cnt} new non-self message(s) "
                            f"since last_run but the newest is "
                            f"{(now_ts - max_ts_epoch) / 3600.0:.1f}h old (older than one "
                            "cadence); entering decay-driven proactive mode"
                        )
                    else:
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

            fragments, own_lines = await self._collect_recent_snippets(self.samples)

            # Per-path metadata for routable, anti-spam-aware proactivity.
            targets = await self._collect_eligible_targets(self.samples)
            eligible_targets = [t for t in targets if t.get("eligible")]

            # The conversation that was active MOST RECENTLY is the natural place
            # to speak. When it is live the person is right there, so reaching
            # into a different chat is outreach drifting away from them rather
            # than toward them (live 2026-09-20 11:07: the direct message was
            # live, so the run reached into a group chat instead). Proactive mode
            # only: the react path below still answers fresh traffic normally, and
            # the next run re-evaluates. Structural only — the same recency
            # ordering and live flag this metadata already carries, never text.
            newest_target = min(
                (t for t in targets if t.get("age_seconds") is not None),
                key=lambda t: t.get("age_seconds") or 0.0,
                default=None,
            )
            if decay_driven and newest_target is not None:
                if newest_target.get("in_active_conversation"):
                    log_info(
                        "[grillo_chat_observer] Newest conversation "
                        f"{newest_target.get('interface_path')} is live; staying "
                        "silent instead of reaching out elsewhere"
                    )
                    eligible_targets = []

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
                    "[grillo_chat_observer] Decay-driven run but no eligible targets "
                    "(dead/live/awaiting-reply); skipping"
                )
                return

            if self.store_memories and fragments:
                await self._store_passive_memories(fragments)

            prompt = self._build_observer_prompt(
                fragments, eligible_targets, decay_driven, own_lines=own_lines
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

    @staticmethod
    def _last_used_datetime(value: Any) -> Optional[datetime]:
        """Parse an ``interface_paths.last_used`` value (datetime or ISO string)."""
        if value is None:
            return None
        if isinstance(value, datetime):
            dt = value
        else:
            text = str(value).strip()
            if not text:
                return None
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except Exception:
                return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    @classmethod
    def _render_own_line(cls, chat_path: str, text: str, timestamp: Any) -> str:
        """Render one of the synth's own lines as CONTEXT, never as a target.

        Same tag shape as every other snippet, plus the two things a small model
        gets wrong about its own output: that the line is its own, and that it is
        not something to answer.
        """
        snippet = " ".join(str(text or "").split())
        if len(snippet) > 300:
            snippet = snippet[:300] + "..."
        age_label = cls._relative_age_label(timestamp)
        return (
            f"(chat:{chat_path} | sender:self | {age_label} | your own line, "
            f"not a reply target) {snippet}"
        )

    @classmethod
    def _render_answered_line(
        cls, chat_path: str, sender: str, text: Any, timestamp: Any
    ) -> str:
        """Render a line the synth has ALREADY answered as CONTEXT, not a target.

        Same tag shape as every other snippet, plus the fact a small model gets
        wrong about a conversation it just handled: that this line is already
        answered, so there is nothing left to reply to here. Whether a chat
        counts as answered is decided structurally by the caller, from message
        timestamps (the synth's newest line is newer than the newest line from
        the other person) together with the live-conversation window — never
        from message text.

        Live 2026-09-25 04:31: the DM had been answered two minutes earlier, the
        beat was still handed the human's line as a reply target, and it re-sent
        the synth's own previous reply verbatim into Telegram. Lines rendered
        here never enter ``snippets``/``grillo_snippets``, so a reply aimed at
        them is dropped as misrouted instead of being delivered twice.
        """
        snippet = " ".join(str(text or "").split())
        if len(snippet) > 300:
            snippet = snippet[:300] + "..."
        age_label = cls._relative_age_label(timestamp)
        return (
            f"(chat:{chat_path} | sender:{sender} | {age_label} | you already "
            f"answered this, not a reply target) {snippet}"
        )

    async def _collect_recent_snippets(self, limit: int) -> tuple[List[str], List[str]]:
        """Collect ``(snippets from other people, context lines)``.

        The two lists are deliberately separate. ``snippets`` is what the beat may
        answer and what ``grillo_snippets`` carries into the routing guard; the
        second is context only — what each conversation already holds, from the
        synth's side and from lines it has already answered — and is rendered
        into the prompt without ever widening the set of reachable chats.

        A chat counts as *already answered* when the synth's own newest line is
        newer than the newest line from anyone else there, and as *live* while
        any message in it is younger than ``GRILLO_OUTREACH_QUIET_MINUTES``. In
        that combination — the conversation is happening right now and the synth
        has already replied to it — the other person's lines are context, not
        reply targets: the synth has nothing pending there, and the live-guard
        that keeps proactive outreach out of a live chat applies to snippet
        replies too.
        """
        snippets: List[str] = []
        own_lines: List[str] = []
        try:
            from core.chat_history_cache import load_chat_history
            from core.interface_paths import get_recent_interface_paths

            now = datetime.now(timezone.utc)
            # Same anti-dead-chat gate the target list applies: a conversation
            # nobody has touched within GRILLO_OBSERVER_ACTIVITY_WINDOW_DAYS
            # contributes no context. Without it the snippet pool was "the N most
            # recently used paths" with no cutoff at all, so whenever the live
            # conversations did not fill the limit the observer's context was
            # padded with lines from chats dead for weeks — 40-day-old WebUI
            # entries and 77-day-old roleplay from a chat that no longer exists
            # were observed in a live outreach prompt, which reads as a broken
            # memory context (and invites the model to reply to ancient lines).
            activity_cutoff = now - timedelta(days=self.activity_window_days)
            # Same live-conversation window the eligible-target builder uses: any
            # message younger than this means the chat is being spoken in right
            # now.
            quiet_cutoff = now - timedelta(minutes=self.quiet_minutes)

            recent = await get_recent_interface_paths(limit * 2)
            for item in recent:
                if len(snippets) >= limit:
                    break
                chat_path = item.get("interface_path")
                if not chat_path:
                    continue
                chat_path = str(chat_path)
                last_used = self._last_used_datetime(
                    item.get("last_used") if isinstance(item, dict) else None
                )
                if last_used is not None and last_used < activity_cutoff:
                    continue
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
                    # No chat-level "the synth spoke last, so ignore the whole
                    # chat" rule: for a responsive synth that matches every
                    # conversation, which left the observer with no live
                    # context to reason about. Synth's own lines are still
                    # filtered out per message below, so self-reply spam stays
                    # impossible while the human's lines stay visible.
                    # take up to 2 recent messages per chat — HUMAN-authored
                    # only. Synth's own messages must never be surfaced as
                    # snippets to "naturally reply to": a small model cannot
                    # reliably distinguish its own output from a human turn and
                    # will talk to itself (self-reply spam).
                    taken = 0
                    own_line: Optional[str] = None
                    # Other people's renderable lines for this chat, kept until
                    # the timestamps say whether the chat is answered.
                    chat_lines: List[tuple[str, Any, str]] = []
                    newest_other_ts: Optional[datetime] = None
                    newest_self_ts: Optional[datetime] = None
                    newest_any_ts: Optional[datetime] = None
                    for msg in reversed(list(messages)):
                        if not isinstance(msg, dict):
                            continue
                        text = msg.get("text")
                        sender = (
                            msg.get("sender_name") or msg.get("sender_id") or "unknown"
                        )
                        timestamp = msg.get("timestamp") or ""
                        msg_ts = self._parse_ts(timestamp)
                        if msg_ts is not None and (
                            newest_any_ts is None or msg_ts > newest_any_ts
                        ):
                            newest_any_ts = msg_ts
                        if self._is_self_sender(sender):
                            # The synth's OWN most recent line in this chat is
                            # kept, but as context: a beat handed only the
                            # human's first-person lines carries on in the
                            # human's voice (live 2026-09-23, trace 3499288d:
                            # the DM outreach addressed the human as "wife",
                            # while the same turn's diary wrote "him"). One per
                            # chat, and it never enters ``grillo_snippets`` —
                            # that list is what the routing guard turns into
                            # reachable paths, and a chat the human never spoke
                            # in must not become reachable through the synth's
                            # own line.
                            if msg_ts is not None and (
                                newest_self_ts is None or msg_ts > newest_self_ts
                            ):
                                newest_self_ts = msg_ts
                            if own_line is None and text:
                                own_line = self._render_own_line(
                                    chat_path, text, timestamp
                                )
                            continue
                        if text:
                            if msg_ts is not None and (
                                newest_other_ts is None or msg_ts > newest_other_ts
                            ):
                                newest_other_ts = msg_ts
                            chat_lines.append((sender, timestamp, text.strip()))
                            taken += 1
                        if taken >= 2 or len(snippets) >= limit:
                            break
                    if chat_lines:
                        # Structural decision, from timestamps only: the synth
                        # has the last word here (``answered``) while the chat is
                        # still being spoken in (``live``) => there is nothing
                        # pending to answer, so these lines are context and never
                        # reply targets. An answered but idle chat keeps its
                        # human lines as replyable snippets: reaching out there
                        # later with something new is the beat's purpose, and a
                        # repeat of the synth's own last line is caught at
                        # delivery (``GRILLO_DUP_SIMILARITY_THRESHOLD``).
                        answered = newest_self_ts is not None and (
                            newest_other_ts is None or newest_self_ts > newest_other_ts
                        )
                        live = (
                            newest_any_ts is not None and newest_any_ts > quiet_cutoff
                        )
                        for sender, timestamp, raw_text in chat_lines:
                            if answered and live and len(own_lines) < limit:
                                own_lines.append(
                                    self._render_answered_line(
                                        chat_path, sender, raw_text, timestamp
                                    )
                                )
                                continue
                            # Relative-age annotation. A bare ISO timestamp is
                            # invisible-as-old to a small model, so it treats a
                            # days-old line as the current moment and continues it
                            # (see AGENTS.md §12 staleness note). Tagging each
                            # snippet with how long ago it was said lets the model
                            # judge staleness itself — no hard age gate, so outreach
                            # always has context behind it.
                            snippet = raw_text
                            if len(snippet) > 300:
                                snippet = snippet[:300] + "..."
                            age_label = self._relative_age_label(timestamp)
                            snippets.append(
                                f"(chat:{chat_path} | sender:{sender} | {age_label}) {snippet}"
                            )
                    if own_line and len(own_lines) < limit:
                        own_lines.append(own_line)
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
                return out, own_lines
            return [], own_lines
        except Exception as e:
            log_error(f"[grillo_chat_observer] Error collecting snippets: {e}")
            return [], []

    async def _collect_eligible_targets(self, limit: int) -> List[Dict[str, Any]]:
        """Build per-path metadata for proactivity decisions (network-agnostic).

        For each recently active conversation returns a dict with:
        - ``interface_path``: the routable path (e.g. ``telegram_bot/123``)
        - ``last_sender``: who sent the most recent message
        - ``last_from_self``: whether the synth spoke last
        - ``age_seconds``: absolute time delta since the last message
        - ``eligible``: True only if there was genuine human (non-self)
          activity within ``activity_window_days`` (anti-dead-chat gate), the
          conversation is not live right now (see ``in_active_conversation``),
          and the awaiting-reply gate is not holding it (see
          ``awaiting_reply``).
        - ``in_active_conversation``: True when ANY message (from either the
          human or the synth) arrived within ``quiet_minutes`` — the chat is
          mid-conversation and outreach must not interrupt it; the next run
          re-evaluates.
        - ``awaiting_reply``: True when the synth spoke last within
          ``self_last_window_minutes`` and the gate is enabled — the person has
          not answered yet, so the conversation waits instead of being re-asked.
          Always False when ``GRILLO_OUTREACH_BLOCK_ON_SELF_LAST`` is off.

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

                # Live-conversation guard: any message, from the human or from
                # the synth, younger than ``quiet_minutes`` means the
                # conversation is happening right now and proactive outreach
                # must not interrupt it. The awaiting-reply gate below is
                # separate: that one is about a thread whose last word was the
                # synth's, this one is about a thread still being spoken in.
                # Structural sender/timestamp metadata only, never keyword
                # logic.
                in_active_conversation = bool(
                    last_ts is not None and last_ts >= quiet_cutoff
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

                # Awaiting-reply gate (GRILLO_OUTREACH_BLOCK_ON_SELF_LAST, on by
                # default): when the synth's own line is the newest in the chat,
                # the human has simply not answered yet — they are not gone.
                # Re-offering it on a timer produced the live nag cycle
                # ("still coming tonight?" -> "hurry home!" -> "did you get
                # home okay?" into a DM the synth already dominated), so the
                # hold is released the moment the human replies and otherwise
                # expires after ``self_last_window_minutes`` (0 = wait for the
                # reply indefinitely).
                #
                # This is the gate that was removed in c2b71ead, on the grounds
                # that a responsive synth is the newest speaker in every chat and
                # so the guard matched every real conversation. That diagnosis
                # was right about the windowless version and wrong about the
                # behaviour: the fix is a bounded window plus a toggle, not
                # absence. Set GRILLO_OUTREACH_BLOCK_ON_SELF_LAST to False to get
                # the pre-gate behaviour back (every idle chat eligible,
                # synth-dominated threads included). Structural sender/timestamp
                # metadata only, never keyword logic.
                awaiting_reply = bool(
                    self.block_on_self_last
                    and last_from_self
                    and last_ts is not None
                    and (
                        self.self_last_window_minutes <= 0
                        or (now - last_ts).total_seconds()
                        < self.self_last_window_minutes * 60
                    )
                )

                eligible = (
                    has_recent_human
                    and not in_active_conversation
                    and not awaiting_reply
                )

                targets.append(
                    {
                        "interface_path": chat_path,
                        "last_sender": last_sender,
                        "last_from_self": last_from_self,
                        "age_seconds": age_seconds,
                        "in_active_conversation": in_active_conversation,
                        "awaiting_reply": awaiting_reply,
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

    @staticmethod
    def _relative_age_label(value: Any) -> str:
        """Compact relative age (e.g. ``2.9h``, ``3d``) for a snippet timestamp.

        Snippets used to carry the raw ISO timestamp, which a small model does
        not translate into "hours ago" — so an hours-old thread read as live and
        outreach replied mid-intimacy as if it were happening now (CHANGELOG
        2026-07-05 staleness issue). A relative label keeps the temporal
        distance model-visible. Fail-safe: returns ``"?"`` on any parse error.
        """
        ts = GrilloChatObserverPlugin._parse_ts(value)
        if ts is None:
            return "?"
        try:
            age_s = (datetime.now(timezone.utc) - ts).total_seconds()
            if age_s < 3600:
                return f"{max(1, int(age_s // 60))}m"
            if age_s < 86400:
                return f"{int(age_s // 3600)}h"
            return f"{int(age_s // 86400)}d"
        except Exception:
            return "?"

    # How many recent observer rows are compared against when deciding whether a
    # snippet has already been stored. A snippet stays in the collection window for
    # hours, so the same lines come back beat after beat; a few hundred rows cover
    # every conversation the observer can currently see.
    _DEDUPE_LOOKBACK_ROWS = 500

    # A snippet younger than this is still the head of a conversation being
    # spoken in right now: the very next prompt carries that same line as the
    # live message, so remembering it writes a copy of the current turn into the
    # store and the recall path serves it back beside the original. The model
    # then reads one message as the person repeating themselves (live
    # 2026-09-28 22:13: the human sent "I'm fine, your belly and ur so warm"
    # once; the observer stored it 8s later and the outreach that followed wrote
    # "'Fine' again. Second time in four minutes, husband"). Skipped, not
    # dropped: the line stays in the collection window for hours, so a later
    # beat stores it once it is genuinely history.
    _FRESH_SNIPPET_SKIP_SEC = 300

    @staticmethod
    def _snippet_age_seconds(snippet: str) -> Optional[float]:
        """Age in seconds the snippet's own age marker reports, or ``None``.

        Snippets are rendered ``(chat:<path> | sender:<who> | <age>) body`` with
        a compact age (``12m``, ``3h``, ``2d``; ``just now`` when younger than a
        minute). Fail-safe: an unrecognised marker returns ``None`` so the
        caller treats the line as old and stores it, never the reverse.
        """
        text = (snippet or "").strip()
        head, sep, _body = text.partition(") ")
        if not sep or not head.startswith("("):
            return None
        parts = [p.strip() for p in head[1:].split("|")]
        if len(parts) < 3:
            return None
        label = parts[2].casefold()
        if label.startswith("age:"):
            label = label[4:]
        if label.endswith(" ago"):
            label = label[:-4]
        label = label.strip()
        if label in ("just now", "now"):
            return 0.0
        if len(label) >= 2 and label[:-1].isdigit():
            factor = {"m": 60.0, "h": 3600.0, "d": 86400.0}.get(label[-1])
            if factor is not None:
                return float(int(label[:-1])) * factor
        return None

    @staticmethod
    def _snippet_identity(snippet: str) -> str:
        """Identity of a snippet for dedupe, with the volatile age marker removed.

        A snippet is rendered as ``(chat:<path> | sender:<who> | <age>[ | <flags>]) body``
        and the age is recomputed on every run, so the same line arrives looking new
        each beat (``26m``, then ``3h``, then ``5h``). Dropping that one field keeps the
        identity stable across runs while the path, the sender, any flag and the body
        still have to match. Fail-safe: anything unparseable is compared as it stands.
        """
        text = (snippet or "").strip()
        head, sep, body = text.partition(") ")
        if not sep or not head.startswith("("):
            return text
        parts = [p.strip() for p in head[1:].split("|")]
        if len(parts) >= 3:
            parts = [parts[0], parts[1], *parts[3:]]
        return "(" + " | ".join(parts) + ") " + body.strip()

    async def _load_stored_snippet_identities(self) -> set:
        """Identities of the observer rows already in ``memories`` (recent window)."""
        from core.db import get_conn_ctx

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT content FROM memories WHERE scope = %s ORDER BY id DESC LIMIT %s",
                    ("observer", self._DEDUPE_LOOKBACK_ROWS),
                )
                fetched = await cur.fetchall()

        out = set()
        for r in fetched or []:
            content = r.get("content") if isinstance(r, dict) else (r[0] if r else None)
            if content:
                out.add(self._snippet_identity(str(content)))
        return out

    async def _store_passive_memories(self, snippets: List[str]) -> None:
        """Persist observer snippets as passive memories, once each.

        A beat re-reads the same conversations every run, so a snippet stays in the
        window for hours and the age marker it carries is rewritten each time. Storing
        without comparing wrote a fresh copy of the same line every beat (measured
        2026-09-27: 511 observer rows in two days, one sentence stored seven times, and
        those rows are in the table recall searches), so the same line could reach a
        prompt several times over. The age marker is stripped before comparison and a
        snippet that is already stored is skipped.

        A snippet younger than ``_FRESH_SNIPPET_SKIP_SEC`` is skipped too: it is
        still the head of a live conversation and the next prompt carries it as the
        live message, so remembering it here is what put one message into a prompt
        twice.
        """
        try:
            from core.db import insert_memory

            try:
                stored = await self._load_stored_snippet_identities()
            except Exception as e:
                # Without the comparison every snippet looks new, which is exactly the
                # duplication this guard exists to stop: skip the store for this run
                # rather than write copies, and say so loudly enough to be found.
                log_warning(
                    "[grillo_chat_observer] could not read stored observer memories "
                    f"({e}); skipping this run's memory store"
                )
                return

            tags = json.dumps(["grillo", "observer", "passive"])
            written = 0
            skipped = 0
            fresh = 0
            for snippet in snippets:
                try:
                    age_seconds = self._snippet_age_seconds(snippet)
                    if (
                        age_seconds is not None
                        and age_seconds < self._FRESH_SNIPPET_SKIP_SEC
                    ):
                        fresh += 1
                        continue
                    identity = self._snippet_identity(snippet)
                    if identity in stored:
                        skipped += 1
                        continue
                    await insert_memory(
                        content=snippet,
                        author="observer",
                        source="grillo_observer",
                        tags=tags,
                        scope="observer",
                    )
                    stored.add(identity)
                    written += 1
                except Exception as e:
                    log_debug(f"[grillo_chat_observer] Failed to store memory: {e}")
            log_info(
                f"[grillo_chat_observer] Stored {written} observer snippet(s) as memories "
                f"({skipped} already stored, {fresh} still live, skipped)"
            )
        except Exception as e:
            log_warning(f"[grillo_chat_observer] Memory storage failed: {e}")

    def _build_observer_prompt(
        self,
        snippets: List[str],
        targets: Optional[List[Dict[str, Any]]] = None,
        decay_driven: bool = False,
        own_lines: Optional[List[str]] = None,
    ) -> str:
        own_lines = list(own_lines or [])
        header = (
            "[G.R.I.L.L.O. CHAT OBSERVER] Below are chat snippets from across conversations. "
            "Each snippet is tagged with an 'age:' marker showing how long ago it was said. "
            "Treat older snippets as historical context, NOT as the current moment — do not "
            "continue or reply to a stale line as if it just happened. Analyze and propose any "
            "actions that would be genuinely helpful right now. "
            "A snippet whose sender is `self` is YOUR OWN earlier line in that chat: it is there "
            "so you can see where the conversation stands, it is what YOU said (not the other "
            "person's words), and it is never a message to reply to. "
            "A snippet tagged `you already answered this` is the other person's line in a chat "
            "you have ALREADY replied to just now: it is context for where that conversation "
            "stands, not something to answer again — the conversation is up to date."
        )

        body = "\n\nSnippets:\n"
        if snippets or own_lines:
            for i, s in enumerate(list(snippets) + own_lines, 1):
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
                if t.get("in_active_conversation"):
                    # A message landed moments ago (from either side): the
                    # conversation is live and outreach must not derail it.
                    cd = "LIVE-CONVERSATION(OFF-LIMITS — a message arrived moments ago; do not interrupt)"
                elif t.get("awaiting_reply"):
                    # The synth spoke last and the human has not replied yet.
                    # They are simply away, not gone, so asking again whether
                    # they are coming back is nagging, not initiative.
                    cd = "AWAITING-REPLY(OFF-LIMITS — you spoke last; the human has not replied yet)"
                else:
                    cd = "ok"
                last = t.get("last_sender") or "?"
                targets_block += f"- interface_path={path} | idle={age_h} | last_sender={last} | cooldown={cd}\n"
        else:
            targets_block = "\n\nELIGIBLE TARGETS: (none currently eligible — do NOT reach out to anyone)\n"

        decay_note = ""
        if decay_driven:
            decay_note = (
                "\n\nNOTE: There is no fresh incoming traffic right now — that is what this run is for. "
                "Reach out to one of the eligible targets above (skipping any marked LIVE-CONVERSATION or AWAITING-REPLY): say something new to someone who is not live, "
                "grounded in what was last said there or in something you are actually carrying. Do not repeat a recent message and do not open with a canned line. "
                'Return {"actions": []} only if every listed target is off-limits, or if you genuinely have nothing that is not a repeat.\n'
            )

        # Ask the LLM to think like a helpful participant: choose which recent message(s) you'd naturally reply to and propose short, human replies.
        propose_clause = (
            "Think like a helpful human reading these snippets: which message(s) would you naturally reply to, and what would you say? "
            "Do NOT propose messages that are conceptually duplicate of what already appears in the snippets. "
            "Answer as yourself, in your own voice: a snippet from someone else is what you reply to, and you never write that person's lines for them. "
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
