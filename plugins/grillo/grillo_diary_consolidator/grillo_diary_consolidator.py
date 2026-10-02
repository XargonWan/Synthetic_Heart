"""Grillo beat plugin: daily diary consolidation.

This plugin is intended to be called from the G.R.I.L.L.O. beat scheduler.
It checks diary days for entries that still contain fragments ("---") or that
consist of multiple rows, and asks the LLM to consolidate them into a single
coherent daily diary entry.  Completed days are handled first; today is offered
only once it has grown past the chunk limit, so a day that keeps growing is
merged in parts during the day instead of becoming one oversized call
overnight.

Each invocation processes a single day, and a day longer than
``GRILLO_DIARY_CONSOLIDATE_CHUNK_CHARS`` is sent in parts (earliest fragments
first).  This guarantees the days immediately preceding today are cleaned up
first (highest priority) and keeps each consolidation prompt small enough for
the LLM to complete reliably.  Over successive runs every historical day is
eventually cleaned up.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Optional

from core.core_initializer import register_plugin
from core.config_manager import config_registry
from core.db import DictCursor, get_conn_ctx
from core.logging_utils import log_debug, log_info, log_error

# Default cap on how many times the same unresolved diary day may be
# re-offered to the LLM before this plugin gives up on it. Without this, a
# day whose update_diary_entry action never actually executes (e.g. the LLM
# fixates on an unrelated/invalid action instead) is indistinguishable from
# "never attempted" and gets re-selected by _find_unmerged_days forever, once
# per beat cycle, indefinitely.
DEFAULT_MAX_CONSOLIDATION_ATTEMPTS = 5

# The fragment separator the diary uses between the pieces of one day. It is
# what ``_find_unmerged_days`` joins rows with and what marks a day as "not yet
# merged", so a partially merged day stays eligible for a later run.
FRAGMENT_SEPARATOR = "\n\n---\n\n"

# Default ceiling on how much of one day goes into a single consolidation
# prompt. A day's diary row grows all day long: the day that broke on
# 2026-09-24 02:22 reached ~130,000 characters (it is 5,383 once merged) and the
# whole day went into one call, which built a 143,375-character prompt: over the
# 100,000-character prompt limit, past the reducer (which may not touch
# ``input``), and 138,476 characters of it was the protected system + current
# turn, so the downstream bridge could not trim it either ("the budget is
# unreachable"). 25,000 keeps the serialized prompt near 75-90k (the dict
# measures roughly three times the text an engine receives) and the rendered
# prompt near 35k, inside the bridge's own 50,000-character budget.
#
# Merging the earliest fragments first keeps every prompt bounded and lets the
# day shrink as it goes: each part is merged together with the prose already
# produced for that day, so the next part is cheaper than the one before it and
# the day still reads as one continuous page.
DEFAULT_CONSOLIDATION_CHUNK_CHARS = 25000


class GrilloDiaryConsolidatorPlugin:
    display_name = "G.R.I.L.L.O. Diary Consolidation"

    # This must match the beat type used by the main Grillo scheduler.
    BEAT_TYPE = "diary_consolidation"

    # Days to consolidate per invocation. Kept at 1 so each run targets the
    # single most recent unconsolidated day *before* today: this prioritises
    # the days immediately preceding today and keeps the prompt small enough
    # for the LLM to complete reliably (large multi-day prompts were failing
    # to produce any output).
    MAX_DAYS_PER_RUN = 1

    def __init__(self):
        self.enabled = config_registry.get_value(
            "GRILLO_DIARY_CONSOLIDATE_ENABLED",
            True,
            label="Enable Grillo diary consolidation",
            description=(
                "When enabled, Grillo will periodically scan recent diary days "
                "and ask the LLM to consolidate fragmented diary entries."
            ),
            value_type=bool,
            group="grillo",
            component="grillo_diary_consolidator",
            hidden=True,
        )
        self.lookback_days = int(
            config_registry.get_value(
                "GRILLO_DIARY_CONSOLIDATE_LOOKBACK_DAYS",
                14,
                label="Diary consolidation lookback (days)",
                description=(
                    "How many days back to scan for diary drafts that need "
                    "consolidation."
                ),
                value_type=int,
                group="grillo",
                component="grillo_diary_consolidator",
            )
        )

        # Register ourselves so the core recognizes this plugin.
        register_plugin("grillo_diary_consolidator", self)
        log_info(
            "[grillo_diary_consolidator] Registered Grillo Diary Consolidation plugin"
        )

        # Listen for config changes
        def _update_enabled(val):
            try:
                self.enabled = bool(val)
                log_info(f"[grillo_diary_consolidator] enabled set to {self.enabled}")
            except Exception:
                pass

        config_registry.add_listener(
            "GRILLO_DIARY_CONSOLIDATE_ENABLED", _update_enabled
        )

        def _update_lookback(val):
            try:
                self.lookback_days = int(val)
                log_info(
                    f"[grillo_diary_consolidator] lookback_days set to {self.lookback_days}"
                )
            except Exception:
                pass

        config_registry.add_listener(
            "GRILLO_DIARY_CONSOLIDATE_LOOKBACK_DAYS",
            _update_lookback,
        )

        self.max_consolidation_attempts = int(
            config_registry.get_value(
                "GRILLO_DIARY_CONSOLIDATE_MAX_ATTEMPTS",
                DEFAULT_MAX_CONSOLIDATION_ATTEMPTS,
                label="Diary consolidation max attempts per day",
                description=(
                    "How many times the same unresolved diary day may be "
                    "re-offered to the LLM before Grillo gives up on it "
                    "(logged loudly) instead of retrying forever."
                ),
                value_type=int,
                group="grillo",
                component="grillo_diary_consolidator",
            )
        )

        def _update_max_attempts(val):
            try:
                self.max_consolidation_attempts = int(val)
                log_info(
                    "[grillo_diary_consolidator] max_consolidation_attempts set to "
                    f"{self.max_consolidation_attempts}"
                )
            except Exception:
                pass

        config_registry.add_listener(
            "GRILLO_DIARY_CONSOLIDATE_MAX_ATTEMPTS",
            _update_max_attempts,
        )

        self.chunk_chars = int(
            config_registry.get_value(
                "GRILLO_DIARY_CONSOLIDATE_CHUNK_CHARS",
                DEFAULT_CONSOLIDATION_CHUNK_CHARS,
                label="Diary consolidation chunk size (characters)",
                description=(
                    "How much of a single diary day may go into one "
                    "consolidation prompt. A day longer than this is merged in "
                    "parts, earliest fragments first, each part into the last "
                    "row of that part (the fragments after it are left for the "
                    "next run). 0 disables chunking and sends whole days."
                ),
                value_type=int,
                group="grillo",
                component="grillo_diary_consolidator",
            )
        )

        def _update_chunk_chars(val):
            try:
                self.chunk_chars = int(val)
                log_info(
                    f"[grillo_diary_consolidator] chunk_chars set to {self.chunk_chars}"
                )
            except Exception:
                pass

        config_registry.add_listener(
            "GRILLO_DIARY_CONSOLIDATE_CHUNK_CHARS",
            _update_chunk_chars,
        )

        # In-process attempt tracking, keyed by diary ``day``. Resets on
        # restart, which is an acceptable/conservative reset (a fresh process
        # gets a clean slate rather than needing extra DB schema to persist
        # counts).
        self._consolidation_attempt_counts: dict = {}
        self._consolidation_exhausted_days: set = set()
        # The size of the day's text at the previous offer, so a merge that
        # actually landed (the day got shorter) counts as progress rather than
        # as another failed attempt.
        self._consolidation_last_sizes: dict = {}

        # Handed to the executor with the next enqueued beat: it tells the diary
        # write how much of the day this merge covers, so a part-merge keeps the
        # fragments after that offset. Cleared after every use.
        self.pending_beat_context: Optional[dict] = None

    async def build_prompt(self) -> Optional[str]:
        """Build a consolidation prompt for the most recent unmerged diary day(s).

        Scans up to ``MAX_DAYS_PER_RUN`` days from newest to oldest, *excluding
        today*, and produces a single prompt asking the LLM to consolidate
        them.  Each day gets its own ``update_diary_entry`` action in the
        response JSON.  The newest completed day (typically yesterday) is
        always processed first.
        """
        if not self.enabled:
            return None

        # Fetch a wider pool than MAX_DAYS_PER_RUN so that, once some days
        # have been given up on (see _record_attempts_and_filter), there is
        # still room to surface the next-oldest candidate instead of only
        # ever re-checking the same permanently-stuck top day.
        pool_size = self.MAX_DAYS_PER_RUN + len(self._consolidation_exhausted_days)
        candidates = await self._find_unmerged_days(pool_size)
        if not candidates:
            return None

        eligible = [
            c
            for c in candidates
            if c[0] not in self._consolidation_exhausted_days
            and self._is_eligible_day(c)
        ]
        days = eligible[: self.MAX_DAYS_PER_RUN]
        if not days:
            return None

        days = self._record_attempts_and_filter(days)
        if not days:
            return None

        return await self._build_multi_day_prompt(days)

    def _is_eligible_day(self, candidate: tuple) -> bool:
        """Whether a candidate day may be consolidated right now.

        A completed day is always eligible. Today is still being written, so it
        is only offered once it has already grown past the chunk limit: at that
        point merging its earliest fragments into one keeps the row (and every
        prompt that reads it) bounded, which is cheaper than the single
        oversized call it would otherwise become overnight. Below the limit
        today is left alone exactly as before.
        """
        day = candidate[0]
        if day != date.today():
            return True
        limit = self.chunk_chars
        if limit <= 0:
            return False
        return len(candidate[2] or "") > limit

    def _record_attempts_and_filter(self, days: list) -> list:
        """Record an attempt for each day about to be offered to the LLM.

        ``_find_unmerged_days`` selects purely from live ``ai_diary`` content,
        so a day whose ``update_diary_entry`` fix never actually executes
        (e.g. the LLM/engine emits an unrelated or invalid action instead) is
        indistinguishable from "never attempted" and would otherwise be
        re-offered on every beat cycle forever. This caps it at
        ``max_consolidation_attempts`` and gives up loudly (logged, not
        silent) so a permanently-stuck day is visible instead of burning an
        LLM call indefinitely.
        """
        kept = []
        for entry in days:
            day = entry[0]
            size = len(entry[2] or "")
            previous_size = self._consolidation_last_sizes.get(day)
            if previous_size is not None and size < previous_size:
                # The previous offer actually merged something: the day's text
                # got shorter. That is progress, not another failed attempt, so
                # the counter restarts and a day being merged in parts is never
                # given up on halfway through.
                self._consolidation_attempt_counts[day] = 0
            self._consolidation_last_sizes[day] = size
            count = self._consolidation_attempt_counts.get(day, 0) + 1
            self._consolidation_attempt_counts[day] = count
            if count > self.max_consolidation_attempts:
                self._consolidation_exhausted_days.add(day)
                log_error(
                    f"[grillo_diary_consolidator] Giving up on day {day} after "
                    f"{count - 1} failed consolidation attempt(s) (exceeded "
                    "GRILLO_DIARY_CONSOLIDATE_MAX_ATTEMPTS="
                    f"{self.max_consolidation_attempts}); it will not be "
                    "re-offered automatically. Needs manual review of "
                    "ai_diary for that day."
                )
                continue
            kept.append(entry)
        return kept

    async def _find_unmerged_days(self, max_days: int) -> list:
        """Return up to *max_days* unconsolidated diary days (newest first).

        Each element is a tuple ``(day, entry_id, combined, row_count)``.
        Completed days come first, newest first, so the days immediately
        preceding today are consolidated with the highest priority; today is
        last in the queue (it is still being written, and the caller only offers
        it once it has grown past the chunk limit).  Only returns days whose
        content still contains the ``---`` fragment separator OR have more than
        one row.
        """
        cutoff = date.today() - timedelta(days=self.lookback_days)
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor(DictCursor) as cur:
                    await cur.execute(
                        """
                        SELECT day, entry_id, combined, row_count FROM (
                            SELECT
                                DATE(created_at) AS day,
                                MAX(id) AS entry_id,
                                GROUP_CONCAT(content ORDER BY id ASC SEPARATOR '\n\n---\n\n') AS combined,
                                COUNT(*) AS row_count
                            FROM ai_diary
                            WHERE DATE(created_at) >= %s
                              AND DATE(created_at) <= CURDATE()
                            GROUP BY DATE(created_at)
                        ) t
                        WHERE row_count > 1 OR combined LIKE '%%---%%'
                        ORDER BY (day = CURDATE()) ASC, day DESC
                        LIMIT %s
                        """,
                        (cutoff, max_days),
                    )
                    rows = await cur.fetchall()
        except Exception as e:
            log_error(f"[grillo_diary_consolidator] DB error fetching diary days: {e}")
            return []

        results = []
        for row in rows:
            day = row.get("day")
            entry_id = row.get("entry_id")
            combined = row.get("combined") or ""
            row_count = int(row.get("row_count") or 0)

            if not combined or ("---" not in combined and row_count <= 1):
                continue

            results.append((day, entry_id, combined, row_count))

        if results:
            log_info(
                f"[grillo_diary_consolidator] Found {len(results)} unmerged day(s): "
                + ", ".join(str(d[0]) for d in results)
            )
        else:
            log_debug("[grillo_diary_consolidator] No diary days needing consolidation")

        return results

    async def _build_multi_day_prompt(self, days: list) -> str:
        """Build a single prompt asking the LLM to consolidate multiple days.

        Each day gets its own ``update_diary_entry`` action in the response.
        A day longer than ``chunk_chars`` is sent in parts: only its earliest
        fragments go into this prompt, and the rest is preserved untouched by
        the diary write (see ``_split_day_text`` and the
        ``diary_merge_preserve_from`` context key).
        """
        self.pending_beat_context = None
        actions = []
        sections = []
        partial_days = 0
        for day, entry_id, combined, row_count in days:
            text, cut = self._split_day_text(combined)
            partial = cut < len(combined)
            if partial:
                partial_days += 1
                parts = self._count_parts(combined)
                self.pending_beat_context = {"diary_merge_preserve_from": cut}
                log_info(
                    f"[grillo_diary_consolidator] Day {day} is {len(combined)} "
                    f"characters: consolidating PART 1 of {parts} "
                    f"({len(text)} chars sent, {len(combined) - cut} kept for a "
                    "later run)"
                )
            else:
                log_info(
                    f"[grillo_diary_consolidator] Including day {day} "
                    f"(entry_id={entry_id}, {row_count} rows)"
                )
            actions.append(
                {
                    "type": "update_diary_entry",
                    "payload": {
                        "id": entry_id,
                        "content": "<your merged prose here>",
                    },
                }
            )
            header = f"--- Day: {day} (entry id: {entry_id}) ---"
            if partial:
                header += " [PART 1: earliest fragments only]"
            sections.append(f"{header}\n\n{text}")

        part_rule = ""
        if partial_days:
            part_rule = (
                "- A day marked PART 1 is given to you in pieces: merge ONLY the "
                "fragments shown for it. Its later fragments are preserved "
                "untouched and are merged in a later run, so do not write about "
                "them, do not summarise the whole day, and never repeat content "
                "you were not shown.\n"
            )

        prompt = (
            "[DIARY CONSOLIDATION — INTERNAL SYSTEM TASK]\n\n"
            "Below are diary fragments from multiple days. For EACH day, "
            "transform the fragments into a single flowing diary page. "
            "Eliminate duplicates, group related topics together, and write in "
            "natural first-person diary style. Preserve important events, "
            "conversations, and reflections while making the text read like "
            "something written at the end of the day.\n\n"
            "Rules:\n"
            "- Write in first person, as if you are writing in a personal journal.\n"
            "- Re-voice the fragments as lived feeling — do NOT summarise or analyse them, "
            "and never describe this as a task, a 'synthesis', or a 'process'.\n"
            "- Write flowing first-person prose (no bullet lists, no '---' separators).\n"
            "- Preserve every meaningful detail from all fragments.\n"
            "- Remove exact duplicates; keep nuance and emotional context.\n"
            "- Group related topics together into coherent paragraphs.\n"
            "- End each day with an emotional reflection or thought.\n"
            "- You MUST produce ONE update_diary_entry action per day.\n"
            f"{part_rule}\n"
            "Diary fragments:\n\n"
            f"{chr(10).join(sections)}\n\n"
            "Respond with ONLY valid JSON (no additional text):\n"
            f"{json.dumps({'actions': actions})}"
        )

        return prompt

    def _split_day_text(self, combined: str) -> tuple:
        """Return ``(text_to_send, offset_of_the_preserved_remainder)``.

        A day's fragments live inside one row, separated by
        ``FRAGMENT_SEPARATOR``, so the split happens on a fragment boundary: the
        last separator that still fits inside ``chunk_chars``. The caller sends
        only the text before it and passes the offset on, so the diary write
        keeps everything from that offset onward. A merged part is always
        shorter than the fragments it replaces, so the next part is cheaper than
        this one. ``chunk_chars <= 0`` disables splitting entirely.
        """
        text = combined or ""
        limit = self.chunk_chars
        if limit <= 0 or len(text) <= limit:
            return text, len(text)
        window = text[:limit]
        boundary = window.rfind(FRAGMENT_SEPARATOR)
        cut = boundary if boundary > 0 else limit
        return text[:cut], cut

    def _count_parts(self, combined: str) -> int:
        """How many runs a day of this size will take (for the prompt note)."""
        text = combined or ""
        count = 0
        while text:
            _piece, cut = self._split_day_text(text)
            count += 1
            if cut >= len(text):
                break
            text = text[cut:]
        return count

    def get_supported_actions(self) -> dict:
        return {}

    async def run_now(self, payload: Optional[dict] = None) -> dict:
        """Enqueue a diary consolidation beat immediately (WebUI "Run Now").

        Builds the consolidation prompt using the unchanged day-selection logic
        (completed days first, newest first; today only once it has grown past
        the chunk limit) and enqueues it as a ``diary_consolidation`` beat via
        the official Grillo low-priority queue API. Returns a status dict
        reporting the queue priority so the WebUI can display "scheduled with
        priority X".
        """
        if not self.enabled:
            return {
                "status": "disabled",
                "message": "Diary consolidation is disabled in configuration.",
            }

        prompt = await self.build_prompt()
        if not prompt:
            return {
                "status": "empty",
                "message": ("No unconsolidated diary days found to process right now."),
            }

        try:
            from core.core_initializer import PLUGIN_REGISTRY

            grillo = None
            for candidate in ("grillo_plugin", "grillo_impl"):
                inst = PLUGIN_REGISTRY.get(candidate)
                if inst is not None and hasattr(inst, "_enqueue_with_low_priority"):
                    grillo = inst
                    break
            if grillo is None:
                log_error(
                    "[grillo_diary_consolidator] Grillo scheduler plugin unavailable; "
                    "cannot enqueue diary consolidation beat."
                )
                return {
                    "status": "error",
                    "message": "Grillo scheduler is not available.",
                }

            await grillo._enqueue_with_low_priority(prompt, self.BEAT_TYPE)
        except Exception as exc:  # pragma: no cover - defensive
            log_error(
                f"[grillo_diary_consolidator] Failed to enqueue diary consolidation: {exc}"
            )
            return {
                "status": "error",
                "message": f"Failed to enqueue diary consolidation: {exc}",
            }

        # Background beats are enqueued in the low band (see
        # core/message_queue.py PRIORITY_LOW). Report it so the WebUI can show
        # "scheduled with priority X".
        from core.message_queue import PRIORITY_LOW

        log_info(
            f"[grillo_diary_consolidator] Diary consolidation beat scheduled "
            f"with priority {PRIORITY_LOW} (Run Now)"
        )
        return {
            "status": "scheduled",
            "priority": PRIORITY_LOW,
            "beat_type": self.BEAT_TYPE,
            "message": f"Diary consolidation scheduled with priority {PRIORITY_LOW}.",
        }


PLUGIN_CLASS = GrilloDiaryConsolidatorPlugin
