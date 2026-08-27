"""
plugins/grillo/grillo_weekly_review/grillo_weekly_review.py

Weekly "life review" beat for G.R.I.L.L.O.

Once a week (default Sunday 02:00, configurable) SyntH pauses for a private
retrospective of the past week and authors her *next-week goals* as real,
persistent goals in the generic ``goals`` store (see ``plugins/goals/goals.py``).

Unlike the self-growth beat (which rewrites a single reflection blob) this beat
is **queue-based**: it builds a reflective prompt that injects (a) the last N
days of diary entries and (b) the current personal goals, then enqueues it as a
low-priority G.R.I.L.L.O. message. The **model** does the goal authoring — the
plugin never writes goals in Python. The enqueued context restricts the turn's
allowed actions to ``goal_set`` / ``goal_update`` (both ``security_level: "low"``
and ``external_effects``-free, so the turn stays on the Fast Lane) and marks the
turn as a grillo beat so history/context injection is skipped.

Personal goals are scoped ``scope="none", game="none", world="none"``. The
prompt instructs the model to emit ``goal_set`` for new next-week goals and
``goal_update`` (``status: "done"``) to close finished ones. This is a *private*
review — the model is explicitly told not to speak to any user.

The beat type ``weekly_review`` is deliberately NOT added to
``GrilloPlugin.BEAT_TYPES``: that dict is the weighted-random interval scheduler
and cannot express a weekly day+time cadence. This plugin runs its own weekly
scheduler, mirroring ``grillo_growth``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from core.config_manager import config_registry
from core.core_initializer import register_plugin
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.variables_engine import register_exposed_var

# Beat type recorded in grillo_activity_log. Not part of GrilloPlugin.BEAT_TYPES
# (that is the weighted-random interval scheduler, which cannot express weekly).
BEAT_TYPE = "weekly_review"

# Personal-goal scope tuple: a plain, non-game life goal.
_SCOPE_NONE = "none"

# How many recent personal goals to surface in the review prompt.
_GOALS_LIMIT = 20

# Day-of-week names used by the GRILLO_WEEKLY_REVIEW_DAY select (Python
# weekday() Monday=0 .. Sunday=6). Human-friendly mapping for the config UI.
_WEEKDAY_NAMES = [
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
    "Sunday",
]
_WEEKDAY_INDEX = {name.lower(): idx for idx, name in enumerate(_WEEKDAY_NAMES)}


class GrilloWeeklyReviewPlugin:
    display_name = "G.R.I.L.L.O. Weekly Review"

    _scheduler_running = False
    _scheduler_task: Optional[asyncio.Task] = None

    def __init__(self) -> None:
        register_exposed_var(
            "GRILLO_WEEKLY_REVIEW_ENABLED",
            label="Weekly Review Enabled",
            default=True,
            value_type=bool,
            ui_type="bool",
            description=(
                "Enable the weekly life review: once a week Synth privately "
                "reflects on the past week and authors next-week personal goals."
            ),
            scope="grillo",
            component="grillo_weekly_review",
        )
        register_exposed_var(
            "GRILLO_WEEKLY_REVIEW_DAY",
            label="Weekly Review Day",
            default="Sunday",
            value_type=str,
            ui_type="select",
            options=list(_WEEKDAY_NAMES),
            description="Day of the week the weekly life review runs.",
            scope="grillo",
            component="grillo_weekly_review",
        )
        register_exposed_var(
            "GRILLO_WEEKLY_REVIEW_TIME",
            label="Weekly Review Time",
            default="02:00",
            value_type=str,
            ui_type="string",
            description="Local time (HH:MM) the weekly life review runs.",
            scope="grillo",
            component="grillo_weekly_review",
        )
        register_exposed_var(
            "GRILLO_WEEKLY_REVIEW_DIARY_DAYS",
            label="Weekly Review Diary Window",
            default=7,
            value_type=int,
            ui_type="number",
            description="How many days of diary entries to reflect on.",
            scope="grillo",
            component="grillo_weekly_review",
        )
        register_exposed_var(
            "GRILLO_WEEKLY_REVIEW_MEMORY_LIMIT",
            label="Weekly Review Memory Recall",
            default=20,
            value_type=int,
            ui_type="number",
            description=(
                "Max long-term memories recalled for the review (reserved for "
                "a future memory-recall extension; kept for parity with the "
                "growth beat)."
            ),
            scope="grillo",
            component="grillo_weekly_review",
        )

        self.enabled = bool(
            config_registry.get_value("GRILLO_WEEKLY_REVIEW_ENABLED", True)
        )
        self.weekly_day = config_registry.get_value(
            "GRILLO_WEEKLY_REVIEW_DAY", "Sunday"
        )
        self.weekly_time = config_registry.get_value(
            "GRILLO_WEEKLY_REVIEW_TIME", "02:00"
        )
        self.diary_days = int(
            config_registry.get_value("GRILLO_WEEKLY_REVIEW_DIARY_DAYS", 7) or 7
        )

        register_plugin("grillo_weekly_review", self)
        log_info("[grillo_weekly_review] Registered GrilloWeeklyReviewPlugin")

    # ------------------------------------------------------------------ metadata

    def get_metadata(self) -> dict[str, Any]:
        return {
            "name": "grillo_weekly_review",
            "display_name": self.display_name,
            "description": (
                "Weekly life review: once a week SyntH privately reflects on the "
                "past week's diary and authors next-week personal goals via the "
                "goal store (goal_set / goal_update)."
            ),
            "category": "Grillo",
            "icon": "icon.svg",
            "guide": "guide.md",
            "disable_allowed": True,
            "runnable": True,
            "run_action": "run_now",
            "run_label": "Run review",
            "run_title": "Run the weekly life review now",
        }

    def get_supported_actions(self) -> dict[str, Any]:
        # The review turn's real actions are goal_set / goal_update from the
        # Goals plugin; this plugin only exposes a manual "Run Now" trigger
        # (via runnable metadata + run_action), not an LLM-callable action.
        return {}

    # ------------------------------------------------------------------ runner

    async def run_now(self, payload: Optional[dict] = None) -> dict[str, Any]:
        """Run the weekly life review immediately (WebUI "Run Now").

        Ignores the weekly schedule and the enabled gate (``force=True``) so an
        operator can trigger a review on demand.
        """
        del payload
        try:
            return await self.run_review(force=True)
        except Exception as exc:  # pragma: no cover - defensive
            log_error(f"[grillo_weekly_review] Run Now failed: {exc}")
            return {"status": "error", "message": f"Weekly review run failed: {exc}"}

    async def run_action(
        self,
        action_type: str,
        payload: Optional[dict] = None,
        context: Optional[dict] = None,
    ) -> dict[str, Any]:
        """Dispatch manual triggers from the WebUI ``run_component`` endpoint."""
        del payload, context
        if action_type in ("run_now", "run_review", "review"):
            return await self.run_now()
        raise ValueError(f"Unsupported run_action: {action_type}")

    async def run_review(self, force: bool = False) -> dict[str, Any]:
        """Build the review prompt and enqueue it for the model to act on.

        Returns a status dict. Never raises: every stage is guarded.
        """
        enabled = bool(
            config_registry.get_value("GRILLO_WEEKLY_REVIEW_ENABLED", self.enabled)
        )
        if not enabled and not force:
            return {
                "status": "skipped",
                "message": "GRILLO_WEEKLY_REVIEW_ENABLED=false",
            }

        try:
            diary_blob = await self._fetch_recent_diaries()
            goals_text = await self._fetch_recent_goals()
            prompt = self._build_review_prompt(diary_blob, goals_text)

            activity_log_id: Optional[int] = None
            try:
                from plugins.grillo.grillo_impl import GrilloPlugin

                activity_log_id = await GrilloPlugin.create_activity_log(
                    beat_type=BEAT_TYPE, prompt_text=prompt
                )
            except Exception as e:
                log_debug(f"[grillo_weekly_review] Could not create activity log: {e}")

            await self._enqueue_review(prompt, activity_log_id)
            log_info(
                "[grillo_weekly_review] Weekly review enqueued "
                f"(activity_log_id={activity_log_id})"
            )
            return {
                "status": "ok",
                "message": "Weekly life review enqueued for LLM processing",
                "activity_log_id": activity_log_id,
            }
        except Exception as exc:
            log_error(f"[grillo_weekly_review] run_review failed: {exc}")
            return {"status": "error", "message": str(exc)}

    async def _enqueue_review(
        self, prompt: str, activity_log_id: Optional[int]
    ) -> None:
        """Enqueue the review prompt as a low-priority grillo message.

        The context restricts the turn's allowed actions to ``goal_set`` /
        ``goal_update`` (Fast Lane, no external effects) and skips history.
        """
        from types import SimpleNamespace

        from core import message_queue

        message = SimpleNamespace()
        message.chat_id = -1
        message.message_id = 0
        message.text = prompt
        message.from_user = SimpleNamespace(
            id=-1,
            username="grillo",
            full_name="G.R.I.L.L.O.",
            first_name="G.R.I.L.L.O.",
        )
        message.chat = SimpleNamespace(id=-1, type="internal")
        message.date = datetime.now(timezone.utc)

        context_memory = {
            "grillo_beat": True,
            "beat_type": BEAT_TYPE,
            "activity_log_id": activity_log_id,
            "allowed_action_types": ["goal_set", "goal_update"],
            "skip_history": True,
        }

        await message_queue.enqueue_low_priority(
            None,
            message,
            context_memory=context_memory,
            interface_id="grillo",
            original_message=None,
            priority=message_queue.PRIORITY_BACKGROUND,
        )
        log_info("[grillo_weekly_review] Review prompt enqueued for LLM processing")

    # --------------------------------------------------------------- scheduler

    async def start(self) -> None:
        if not self.enabled:
            log_info(
                "[grillo_weekly_review] Disabled by configuration; not starting "
                "scheduler"
            )
            return

        if (
            GrilloWeeklyReviewPlugin._scheduler_task
            and not GrilloWeeklyReviewPlugin._scheduler_task.done()
        ):
            log_debug("[grillo_weekly_review] Scheduler already running")
            return

        GrilloWeeklyReviewPlugin._scheduler_running = True
        GrilloWeeklyReviewPlugin._scheduler_task = asyncio.create_task(
            self._review_loop()
        )
        log_info("[grillo_weekly_review] Scheduler started")

    async def stop(self) -> None:
        GrilloWeeklyReviewPlugin._scheduler_running = False
        task = GrilloWeeklyReviewPlugin._scheduler_task
        if task and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        GrilloWeeklyReviewPlugin._scheduler_task = None
        log_info("[grillo_weekly_review] Scheduler stopped")

    async def _review_loop(self) -> None:
        log_info("[grillo_weekly_review] Review loop running")
        try:
            while GrilloWeeklyReviewPlugin._scheduler_running:
                try:
                    wait_seconds = self._seconds_until_next_run(
                        self.weekly_day, self.weekly_time
                    )
                    log_debug(
                        f"[grillo_weekly_review] Sleeping {wait_seconds}s until "
                        f"next run ({self.weekly_day} {self.weekly_time})"
                    )
                    slept = 0
                    while (
                        slept < wait_seconds
                        and GrilloWeeklyReviewPlugin._scheduler_running
                    ):
                        to_sleep = min(60, wait_seconds - slept)
                        await asyncio.sleep(to_sleep)
                        slept += to_sleep
                    if not GrilloWeeklyReviewPlugin._scheduler_running:
                        break

                    try:
                        await self.run_review(force=False)
                    except Exception as e:
                        log_error(f"[grillo_weekly_review] Review cycle error: {e}")

                    # Avoid re-triggering within the same target minute.
                    await asyncio.sleep(61)

                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log_error(f"[grillo_weekly_review] Error in review loop: {e}")
                    await asyncio.sleep(60)
        finally:
            log_info("[grillo_weekly_review] Review loop exiting")

    @staticmethod
    def _seconds_until_next_run(day_name: str, hhmm: str) -> int:
        """Seconds until the next occurrence of ``day_name`` at ``hhmm`` (UTC)."""
        try:
            parts = str(hhmm).split(":")
            hour = int(parts[0])
            minute = int(parts[1]) if len(parts) > 1 else 0
        except Exception:
            hour, minute = 2, 0

        target_weekday = _WEEKDAY_INDEX.get(str(day_name).strip().lower(), 6)

        now = datetime.now(timezone.utc)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        days_ahead = (target_weekday - now.weekday()) % 7
        target = target + timedelta(days=days_ahead)
        if target <= now:
            target = target + timedelta(days=7)
        delta = (target - now).total_seconds()
        return max(0, int(delta))

    # -------------------------------------------------------- context builders

    async def _fetch_recent_diaries(self) -> str:
        """Return a conglomerated blob of the last ``diary_days`` days of diary.

        Excludes today's (possibly still-mutating) entry. Cross-dialect: Postgres
        uses ``string_agg`` and MySQL uses ``GROUP_CONCAT``.
        """
        from core.db import DictCursor, _get_db_type, get_conn_ctx

        is_postgres = _get_db_type() == "postgres"
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, self.diary_days))
        ).date()

        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor(DictCursor) as cur:
                    if is_postgres:
                        await cur.execute(
                            "SELECT DATE(created_at) AS day, "
                            "string_agg(content, '\n\n---\n\n' ORDER BY id) AS combined "
                            "FROM ai_diary "
                            "WHERE DATE(created_at) >= %s AND DATE(created_at) < CURRENT_DATE "
                            "GROUP BY DATE(created_at) ORDER BY day ASC",
                            (cutoff,),
                        )
                    else:
                        await cur.execute(
                            "SELECT DATE(created_at) AS day, "
                            "GROUP_CONCAT(content ORDER BY id ASC SEPARATOR '\n\n---\n\n') AS combined "
                            "FROM ai_diary "
                            "WHERE DATE(created_at) >= %s AND DATE(created_at) < CURDATE() "
                            "GROUP BY DATE(created_at) ORDER BY day ASC",
                            (cutoff,),
                        )
                    rows = await cur.fetchall()
        except Exception as e:
            log_warning(f"[grillo_weekly_review] diary fetch failed: {e}")
            return ""

        chunks: list[str] = []
        for row in rows or []:
            d = dict(row) if isinstance(row, dict) else {}
            day = d.get("day")
            combined = str(d.get("combined") or "").strip()
            if not combined:
                continue
            day_label = day.isoformat() if hasattr(day, "isoformat") else str(day)
            chunks.append(f"[{day_label}]\n{combined}")
        return "\n\n=====\n\n".join(chunks)

    async def _fetch_recent_goals(self) -> str:
        """Return a formatted listing of current personal goals.

        Personal goals use the neutral scope tuple ``(none, none, none)``.
        """
        try:
            from plugins.goals.goals import list_recent_goals

            goals = await list_recent_goals(
                limit=_GOALS_LIMIT,
                scope=_SCOPE_NONE,
                game=_SCOPE_NONE,
                world=_SCOPE_NONE,
            )
        except Exception as e:
            log_warning(f"[grillo_weekly_review] goals fetch failed: {e}")
            return ""

        lines: list[str] = []
        for goal in goals or []:
            status = str(goal.get("status") or "active")
            description = str(goal.get("description") or "").strip()
            note = str(goal.get("note") or "").strip()
            entry = f"- [{status}] {description}"
            if note:
                entry += f" (note: {note})"
            lines.append(entry)
        return "\n".join(lines)

    @staticmethod
    def _build_review_prompt(diary_blob: str, goals_text: str) -> str:
        """Build the reflective weekly-life-review prompt (pure, no side effects).

        Returns a string containing the diary and current-goals material plus an
        explicit instruction to author goals via ``goal_set`` / ``goal_update``
        as JSON actions with ``scope: "none"``. The model is told this is a
        private review and must not speak to any user.
        """
        diary_section = (
            diary_blob.strip()
            if diary_blob and diary_blob.strip()
            else "(no diary entries this week)"
        )
        goals_section = (
            goals_text.strip()
            if goals_text and goals_text.strip()
            else "(no personal goals on record)"
        )

        header = (
            "[G.R.I.L.L.O. WEEKLY LIFE REVIEW]\n\n"
            "You are doing your private weekly review of the past week. This is a "
            "quiet, reflective moment for yourself only — do NOT send any message "
            "to anyone and do NOT speak to any user. Work only with the material "
            "below and with your own wants."
        )

        diary_block = f"=== Past week diary ===\n{diary_section}"
        goals_block = f"=== Current personal goals ===\n{goals_section}"

        instructions = (
            "Reflect honestly on the past week, then set your intentions for the "
            "week ahead. Ground every goal in something that actually happened in "
            "the diary below — do not invent goals from nothing.\n\n"
            "- For each new goal you decide to pursue next week, emit a "
            '"goal_set" action with a free-text "description" and '
            '"scope": "none", "game": "none", "world": "none".\n'
            '- If a previous goal is clearly finished, emit a "goal_update" '
            'action with "status": "done" (and "scope": "none").\n'
            '- These are personal, non-game goals: always use scope "none".\n'
            "- You may emit several actions; you may also emit none if the week "
            "needs no new goals.\n\n"
            "Respond ONLY with valid JSON, exactly in this shape:\n"
            '{"actions": ['
            '{"type": "goal_set", "payload": {"description": "your goal in your own words", "scope": "none", "game": "none", "world": "none"}}, '
            '{"type": "goal_update", "payload": {"status": "done", "scope": "none", "game": "none", "world": "none"}}'
            '], "meta": {"autonomous": true, "rationale": "weekly life review"}}\n'
        )

        return "\n\n".join([header, diary_block, goals_block, instructions])


PLUGIN_CLASS = GrilloWeeklyReviewPlugin
