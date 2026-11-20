from __future__ import annotations

from typing import Any, List

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_info, log_warning

display_name = "Recon Radio Status"

# The radio's chatter is real memory but only matters when the conversation is about
# the radio, so passive recall leaves these sessions out and this plugin asks for
# them back on a radio turn (see ``_radio_memories``). Declared here, by the plugin
# that owns the subject, not in the core.
RADIO_SESSION_PREFIXES: tuple[str, ...] = ("radio_host:", "radio_host_")
try:
    from core.soul.service import register_on_demand_session_prefixes

    register_on_demand_session_prefixes(*RADIO_SESSION_PREFIXES)
except Exception:  # pragma: no cover - SOUL absent
    pass

# UI-exposed switch to enable/disable this recon plugin. The config key is
# derived by the recon dispatcher from this module's basename uppercased
# (core/recon.py:_plugin_key) + "_RECON_ENABLED", so it MUST be named
# RECON_RADIO_STATUS_RECON_ENABLED.
try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "RECON_RADIO_STATUS_RECON_ENABLED",
        label="Enable Recon Radio Status",
        default=True,
        value_type=bool,
        ui_type="bool",
        description=(
            "Inject the live radio status (current/next track, listeners) into "
            "the main prompt when the conversation is about the radio."
        ),
        scope="recon",
        component="recon_radio_status",
        hidden=True,
    )
except Exception:
    config_registry.get_var(
        "RECON_RADIO_STATUS_RECON_ENABLED",
        True,
        value_type=bool,
        label="Enable Recon Radio Status",
        description=(
            "Inject the live radio status (current/next track, listeners) into "
            "the main prompt when the conversation is about the radio."
        ),
        group="recon",
        component="recon_radio_status",
        hidden=True,
    )


class ReconRadioStatusPlugin:
    display_name = display_name
    recon_priority = 5

    def get_supported_actions(self) -> dict:
        return {}

    def get_recon_key(self) -> str:
        return "radio_status"

    def get_recon_instruction(self) -> str:
        return (
            "Determine whether the user's message is about the live radio broadcast "
            "(for example: the song currently playing, what is on air, the station, "
            "the DJ set, what plays next, or the listeners). "
            'If YES, return: {"radio_status": true}. '
            'If it is NOT about the live radio, return: {"radio_status": false}. '
            "Do not base this on any single word; judge the overall intent of the message."
        )

    def _is_relevant(self, data: Any) -> bool:
        """Interpret the recon LLM verdict for the ``radio_status`` key."""
        if isinstance(data, bool):
            return data
        if isinstance(data, dict):
            value = data.get("radio_status")
            if isinstance(value, bool):
                return value
            if isinstance(value, str):
                return value.strip().lower() in ("true", "yes", "1")
        if isinstance(data, str):
            return data.strip().lower() in ("true", "yes", "1")
        return False

    def _find_radio_plugin(self) -> Any | None:
        from core.core_initializer import PLUGIN_REGISTRY

        for plugin in PLUGIN_REGISTRY.values():
            if hasattr(plugin, "get_live_status"):
                return plugin
        return None

    def _format_status(self, status: dict[str, Any]) -> str:
        if not status.get("online"):
            station = status.get("station_name") or "the radio"
            return (
                f"Live radio status: {station} is currently OFFLINE (not broadcasting)."
            )

        lines: list[str] = ["Live radio status:"]
        station = status.get("station_name")
        if station:
            lines.append(f"- Station: {station}")

        title = status.get("current_track_title")
        artist = status.get("current_track_artist")
        if title or artist:
            if title and artist:
                lines.append(f"- Now playing: {artist} - {title}")
            else:
                lines.append(f"- Now playing: {title or artist}")
        else:
            lines.append("- Now playing: unknown (no track data available yet)")

        next_title = status.get("next_track_title")
        next_artist = status.get("next_track_artist")
        if next_title or next_artist:
            if next_title and next_artist:
                lines.append(f"- Up next: {next_artist} - {next_title}")
            else:
                lines.append(f"- Up next: {next_title or next_artist}")

        listeners = status.get("listeners")
        if isinstance(listeners, int):
            lines.append(f"- Listeners: {listeners}")

        schedule = status.get("schedule_description")
        if schedule:
            lines.append(f"- Schedule: {schedule}")

        return "\n".join(lines)

    async def parse_recon_response(
        self,
        data,
        *,
        message=None,
        context_memory=None,
        text: str | None = None,
        tags: List[str] | None = None,
        keywords: List[str] | None = None,
        max_results: int = 5,
        _raw_llm_text: str | None = None,
    ) -> list[dict]:
        """Inject live radio status when the recon LLM flags the topic as relevant."""
        enabled = bool(
            config_registry.get_value(
                "RECON_RADIO_STATUS_RECON_ENABLED", True, value_type=bool
            )
        )
        if not enabled:
            return []

        if not self._is_relevant(data):
            log_debug("[recon_radio_status] LLM did not flag the radio as relevant")
            return []

        radio_plugin = self._find_radio_plugin()
        if radio_plugin is None:
            log_debug("[recon_radio_status] radio_host plugin not found")
            return []

        try:
            status = await radio_plugin.get_live_status()
        except Exception as e:
            log_warning(f"[recon_radio_status] get_live_status failed: {e}")
            return []

        if not status:
            log_debug("[recon_radio_status] radio disabled or not configured")
            return []

        content = self._format_status(status)
        log_info("[recon_radio_status] Injecting live radio status into prompt")

        contributions: list[dict] = [
            {
                "type": "snippet",
                "content": content,
                "source": "recon_radio_status",
                "priority": int(self.recon_priority),
            }
        ]
        contributions.extend(await self._radio_memories(text))
        return contributions

    async def _radio_memories(self, text: str | None) -> list[dict]:
        """What the persona remembers from its own broadcasts, for a radio turn.

        The radio's sessions are left out of passive recall (they would surface in
        unrelated chats), so a conversation about the radio has to ask for them.
        This runs only when the recon verdict above says the turn is about it.
        """
        query = " ".join(str(text or "").split())
        if len(query) < 5:
            return []
        try:
            from core.soul.service import SoulService

            service = SoulService.from_registry()
            if service is None:
                return []
            hits = await service.recall(
                query, session_prefixes=RADIO_SESSION_PREFIXES, limit=3
            )
        except Exception as e:
            log_warning(f"[recon_radio_status] radio memory recall failed: {e}")
            return []
        return [
            {
                "type": "memory",
                "content": {
                    "source": "radio_memory",
                    "id": hit.cell.id,
                    "timestamp": hit.cell.event_timestamp.isoformat(),
                    "snippet": " ".join(hit.cell.episodic_trace.split())[:400],
                    "tags": [],
                },
                "source": "recon_radio_status",
                "priority": int(self.recon_priority),
            }
            for hit in hits
        ]

    async def get_recon_contributions(
        self,
        *,
        message=None,
        context_memory=None,
        text: str | None = None,
        tags: List[str] | None = None,
        keywords: List[str] | None = None,
        max_results: int = 5,
    ) -> list[dict]:
        """Legacy interface — delegate to parse_recon_response."""
        return await self.parse_recon_response(
            data=None,
            message=message,
            context_memory=context_memory,
            text=text,
            tags=tags,
            keywords=keywords,
            max_results=max_results,
            _raw_llm_text=None,
        )


# Auto-register this plugin
PLUGIN_CLASS = ReconRadioStatusPlugin
