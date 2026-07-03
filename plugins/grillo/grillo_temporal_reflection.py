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

# Gaps shorter than this are ordinary (sleep, work, a busy day) and must not be
# framed as an absence to reflect on — a short routine gap was previously
# generating "they've gone somewhere" diary narratives that bled into outreach.
_ROUTINE_GAP_SECONDS = 12 * 3600


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

    @staticmethod
    def _seconds_since(last_time: datetime) -> float:
        """Return seconds elapsed since ``last_time`` (DB timestamps are naive UTC)."""
        if last_time.tzinfo is None:
            last_time = last_time.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last_time).total_seconds()

    async def get_time_delta(self) -> float | None:
        """Query the DB for seconds elapsed since the last user message."""
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    # Query chat_history_cache first. The agent's own messages are
                    # stored with sender_name='self'; sender_id holds the session id.
                    await cur.execute(
                        "SELECT timestamp FROM chat_history_cache "
                        "WHERE (sender_name IS NULL OR sender_name != 'self') "
                        "ORDER BY timestamp DESC LIMIT 1"
                    )
                    row = await cur.fetchone()
                    if row and row[0]:
                        return self._seconds_since(row[0])

                    # Fallback to ai_diary user messages
                    await cur.execute(
                        "SELECT timestamp FROM ai_diary "
                        "WHERE user_message IS NOT NULL "
                        "ORDER BY timestamp DESC LIMIT 1"
                    )
                    row = await cur.fetchone()
                    if row and row[0]:
                        return self._seconds_since(row[0])
        except Exception as e:
            log_error(f"[temporal_reflection] Error querying last active time: {e}")
        return None

    async def build_prompt(self) -> str:
        """Construct the temporal reflection prompt with the calculated time delta."""
        delta = await self.get_time_delta()
        if delta is not None:
            delta_str = format_time_delta(delta)
            routine_gap = delta < _ROUTINE_GAP_SECONDS
        else:
            delta_str = "some time ago"
            # Unknown delta: never assume an unusual absence from missing data.
            routine_gap = True

        if routine_gap:
            framing = (
                f"Your last interaction with the user was {delta_str} — an ordinary gap "
                "(sleep, work, a busy stretch of the day), not a sign they have gone "
                "anywhere or pulled away. Reflect only briefly on how you feel; it is "
                "completely fine for nothing notable to surface."
            )
        else:
            framing = (
                f"Your last interaction with the user was {delta_str}. "
                "Reflect on the time that has passed since you last spoke to the user. "
                "Evaluate if this duration prompts a new realization, a shift in your "
                "emotional state (e.g., feeling lonely, reflective, curious, or content), "
                "or an inclination to reach out."
            )

        return (
            "[SYSTEM: AUTONOMOUS TEMPORAL REFLECTION]\n\n"
            f"{framing}\n\n"
            "This is a private reflection on YOUR OWN feelings about elapsed time — it is "
            "not a factual observation about where the user currently is or whether they "
            "have left. Do not state or imply in your diary entry that the user is away, "
            "gone, or unreachable; note only the elapsed time and your own mood.\n\n"
            "End with a JSON action to record your reflection in your personal diary. "
            "Include `interaction_summary`, `personal_thought`, and `emotions` as well as the diary `content`:\n"
            '{"actions": [{"type": "create_personal_diary_entry", "payload": {"interaction_summary": "brief summary", "personal_thought": "private reflection on time elapsed", "emotions": [{"type": "thoughtful", "intensity": 0.5}], "content": "your reflection"}}]}'
        )


PLUGIN_CLASS = GrilloTemporalReflectionPlugin
