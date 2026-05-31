# plugins/grillo/grillo_temporal_reflection.py
"""Temporal reflection prompt builder for G.R.I.L.L.O.

Calculates the time elapsed since the last active user message and instructs
the Synth to reflect on this duration to evaluate her emotional state.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from core.ai_plugin_base import AIPluginBase
from core.db import get_conn_ctx
from core.logging_utils import log_error

display_name = "G.R.I.L.L.O. Temporal Reflection"
BEAT_TYPE = "temporal_reflection"


def format_time_delta(seconds: float) -> str:
    """Format a duration in seconds into a natural human-readable string."""
    minutes = int(seconds / 60)
    if minutes < 1:
        return "just a moment ago"
    if minutes < 60:
        return f"{minutes} minute{'s' if minutes > 1 else ''} ago"
    hours = int(minutes / 60)
    if hours < 24:
        return f"{hours} hour{'s' if hours > 1 else ''} ago"
    days = int(hours / 24)
    return f"{days} day{'s' if days > 1 else ''} ago"


class GrilloTemporalReflectionPlugin(AIPluginBase):
    """G.R.I.L.L.O. beat plugin to prompt temporal reflection when the user is idle."""

    display_name = display_name
    BEAT_TYPE = BEAT_TYPE

    def get_supported_actions(self) -> dict[str, Any]:
        """Return supported actions (none for this background beat)."""
        return {}

    async def get_time_delta(self) -> float | None:
        """Query the DB for seconds elapsed since the last user message."""
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    # Query chat_history_cache first
                    await cur.execute(
                        "SELECT timestamp FROM chat_history_cache "
                        "WHERE sender_id != 'self' "
                        "ORDER BY timestamp DESC LIMIT 1"
                    )
                    row = await cur.fetchone()
                    if row and row[0]:
                        last_time = row[0]
                        now_utc = datetime.now(timezone.utc)
                        return (now_utc - last_time).total_seconds()

                    # Fallback to ai_diary user messages
                    await cur.execute(
                        "SELECT timestamp FROM ai_diary "
                        "WHERE user_message IS NOT NULL "
                        "ORDER BY timestamp DESC LIMIT 1"
                    )
                    row = await cur.fetchone()
                    if row and row[0]:
                        last_time = row[0]
                        now_utc = datetime.now(timezone.utc)
                        return (now_utc - last_time).total_seconds()
        except Exception as e:
            log_error(f"[temporal_reflection] Error querying last active time: {e}")
        return None

    async def build_prompt(self) -> str:
        """Construct the temporal reflection prompt with the calculated time delta."""
        delta = await self.get_time_delta()
        if delta is not None:
            delta_str = format_time_delta(delta)
        else:
            delta_str = "some time ago"

        return (
            "[SYSTEM: AUTONOMOUS TEMPORAL REFLECTION]\n\n"
            f"Your last interaction with the user was {delta_str}.\n\n"
            "Reflect on the time that has passed since you last spoke to the user. "
            "Evaluate if this duration prompts a new realization, a shift in your emotional state "
            "(e.g., feeling lonely, reflective, curious, or content), or an inclination to reach out.\n\n"
            "End with a JSON action to record your reflection in your personal diary. "
            "Include `interaction_summary`, `personal_thought`, and `emotions` as well as the diary `content`:\n"
            '{"actions": [{"type": "create_personal_diary_entry", "payload": {"interaction_summary": "brief summary", "personal_thought": "private reflection on time elapsed", "emotions": [{"type": "thoughtful", "intensity": 0.5}], "content": "your reflection"}}]}'
        )


PLUGIN_CLASS = GrilloTemporalReflectionPlugin
