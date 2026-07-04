"""Grillo beat plugin: daily diary consolidation.

This plugin is intended to be called from the G.R.I.L.L.O. beat scheduler.
It checks recent diary days (including today) for entries that still contain
fragments ("---") or that consist of multiple rows, and asks the LLM to
consolidate them into a single coherent daily diary entry.

Each invocation processes up to 3 days: today + the 2 most recent
unconsolidated days going backward.  Over successive runs every historical
day is eventually cleaned up.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Optional

from core.core_initializer import register_plugin
from core.config_manager import config_registry
from core.db import DictCursor, get_conn_ctx
from core.logging_utils import log_debug, log_info, log_error


class GrilloDiaryConsolidatorPlugin:
    display_name = "G.R.I.L.L.O. Diary Consolidation"

    # This must match the beat type used by the main Grillo scheduler.
    BEAT_TYPE = "diary_consolidation"

    # Max days to consolidate per invocation (today + 2 older = 3)
    MAX_DAYS_PER_RUN = 3

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

    async def build_prompt(self) -> Optional[str]:
        """Build a consolidation prompt for the most recent unmerged diary day(s).

        Scans up to ``MAX_DAYS_PER_RUN`` days from newest to oldest (including
        today) and produces a single prompt asking the LLM to consolidate all
        of them.  Each day gets its own ``update_diary_entry`` action in the
        response JSON.
        """
        if not self.enabled:
            return None

        days = await self._find_unmerged_days(self.MAX_DAYS_PER_RUN)
        if not days:
            return None

        return self._build_multi_day_prompt(days)

    async def _find_unmerged_days(self, max_days: int) -> list:
        """Return up to *max_days* unconsolidated diary days (newest first).

        Each element is a tuple ``(day, entry_id, combined, row_count)``.
        Includes today.  Only returns days whose content still contains the
        ``---`` fragment separator OR have more than one row.
        """
        cutoff = date.today() - timedelta(days=self.lookback_days)
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor(DictCursor) as cur:
                    await cur.execute(
                        """
                        SELECT day, entry_id, combined, row_count FROM (
                            SELECT
                                DATE(timestamp) AS day,
                                MAX(id) AS entry_id,
                                GROUP_CONCAT(content ORDER BY id ASC SEPARATOR '\n\n---\n\n') AS combined,
                                COUNT(*) AS row_count
                            FROM ai_diary
                            WHERE DATE(timestamp) >= %s
                              AND DATE(timestamp) <= CURDATE()
                            GROUP BY DATE(timestamp)
                        ) t
                        WHERE row_count > 1 OR combined LIKE '%%---%%'
                        ORDER BY day DESC
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

    def _build_multi_day_prompt(self, days: list) -> str:
        """Build a single prompt asking the LLM to consolidate multiple days.

        Each day gets its own ``update_diary_entry`` action in the response.
        """
        actions = []
        sections = []
        for day, entry_id, combined, row_count in days:
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
            sections.append(f"--- Day: {day} (entry id: {entry_id}) ---\n\n{combined}")

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
            "- You MUST produce ONE update_diary_entry action per day.\n\n"
            "Diary fragments:\n\n"
            f"{chr(10).join(sections)}\n\n"
            "Respond with ONLY valid JSON (no additional text):\n"
            f"{json.dumps({'actions': actions})}"
        )

        return prompt

    def get_supported_actions(self) -> dict:
        return {}


PLUGIN_CLASS = GrilloDiaryConsolidatorPlugin
