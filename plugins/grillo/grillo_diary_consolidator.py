"""Grillo beat plugin: daily diary consolidation.

This plugin is intended to be called from the G.R.I.L.L.O. beat scheduler.
It checks recent diary days (yesterday and backwards) for entries that still
contain fragments ("---") or that consist of multiple rows, and asks the LLM
to consolidate them into a single coherent daily diary entry.

The beat is designed to run in the background, so the system can clean up
historical diary noise even if users don't interact frequently.
"""

from __future__ import annotations

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
        """Build the consolidation prompt for the most recent unmerged diary day.

        This is called by the Grillo scheduler when selecting a beat of type
        ``diary_consolidation``.
        """
        if not self.enabled:
            return None

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
                            WHERE DATE(timestamp) < CURDATE()
                              AND timestamp >= %s
                            GROUP BY DATE(timestamp)
                        ) t
                        WHERE row_count > 1 OR combined LIKE '%%---%%'
                        ORDER BY day DESC
                        LIMIT 1
                        """,
                        (cutoff,),
                    )
                    row = await cur.fetchone()
        except Exception as e:
            log_error(f"[grillo_diary_consolidator] DB error fetching diary day: {e}")
            return None

        if not row:
            log_debug("[grillo_diary_consolidator] No diary days needing consolidation")
            return None

        day = row.get("day")
        entry_id = row.get("entry_id")
        combined = row.get("combined") or ""
        row_count = int(row.get("row_count") or 0)

        if not combined or ("---" not in combined and row_count <= 1):
            log_debug(
                "[grillo_diary_consolidator] Found diary day but no fragmentation detected"
            )
            return None

        log_info(
            f"[grillo_diary_consolidator] Scheduling consolidation for diary day {day} (entry_id={entry_id}, rows={row_count})"
        )

        import json

        action_payload = {
            "actions": [
                {
                    "type": "update_diary_entry",
                    "payload": {"id": entry_id, "content": "<your merged prose here>"},
                }
            ]
        }

        return (
            "[DIARY CONSOLIDATION — INTERNAL SYSTEM TASK]\n\n"
            "Your personal diary has accumulated multiple entries for the same day. "
            "Rewrite them as a single, coherent first-person diary entry that flows naturally.\n\n"
            "Rules:\n"
            "- Write in first person, as if you are writing in a personal journal.\n"
            "- Remove the '---' separators; only include natural prose.\n"
            "- Preserve the meaning of all fragments, including feelings and details.\n"
            f"- The entry id to update is: {entry_id}\n"
            f"- The diary day is: {day}\n\n"
            "Diary fragments:\n\n"
            f"{combined}\n\n"
            "Respond with ONLY valid JSON (no additional text):\n"
            f"{json.dumps(action_payload)}"
        )

    def get_supported_actions(self) -> dict:
        return {}


PLUGIN_CLASS = GrilloDiaryConsolidatorPlugin
