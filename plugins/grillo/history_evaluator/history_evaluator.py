"""
plugins/grillo/history_evaluator/history_evaluator.py

History Evaluator plugin for G.R.I.L.L.O.:
Provides evaluate_history(interface_path, entries) to format the last N chat
messages into a concise 'spunto di riflessione' that Grillo can include into prompts.

This plugin intentionally is non-LLM and implemented as a standard PluginBase,
so it is registered under `plugins/` and available via the core plugin registry.
"""

from typing import Optional

from core.plugin_base import PluginBase
from core.logging_utils import log_error
from core.config_manager import config_registry

display_name = "G.R.I.L.L.O. History Evaluator"


def _truncate(text: str, max_chars: int = 240) -> str:
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


class HistoryEvaluatorPlugin(PluginBase):
    display_name = display_name

    def __init__(self, config: Optional[dict] = None):
        super().__init__(config=config)
        try:
            self.default_entries = int(
                config_registry.get_value(
                    "HISTORY_EVALUATOR_DEFAULT_ENTRIES",
                    10,
                    value_type=int,
                    group="grillo",
                    component="history_evaluator",
                )
            )
        except Exception:
            self.default_entries = 10

    def get_supported_actions(self) -> dict:
        return {}

    def get_metadata(self) -> dict:
        """Return declarative plugin metadata (see PluginBase.get_metadata)."""
        return {
            "name": "grillo.history_evaluator",
            "display_name": "History Evaluator",
            "description": "Utility plugin to create short reflective prompts from recent history.",
            "category": "Grillo",
            "version": "0.1",
        }

    async def evaluate_history(
        self, interface_path: str, entries: int | None = None
    ) -> str:
        entries = entries or self.default_entries
        try:
            from core.chat_history_cache import load_chat_history

            hist = await load_chat_history(interface_path)
            if not hist:
                return (
                    "[History Evaluator] No recent messages available to evaluate. "
                    "Try again later or increase the history cache settings."
                )

            last = list(hist)[-entries:]

            lines = ["[History Evaluator] Recent messages for quick reflection:\n"]
            for i, item in enumerate(last, 1):
                sender = item.get("sender_name") or item.get("sender_id") or "unknown"
                text = item.get("text") or ""
                text = _truncate(text, max_chars=300)
                lines.append(f"{i}. {sender}: {text}")

            lines.append("")
            lines.append(
                "Please consider the above recent messages and suggest 3 concise reflections or observations "
                "that SyntH should think about (each 1-2 short sentences). End with a short JSON action suggestion:"
            )
            lines.append(
                '{"actions": [{"type": "create_personal_diary_entry", "payload": {"content": "<your reflection>", "personal_thought": "<insight>"}}]}'
            )

            return "\n".join(lines)
        except Exception as e:
            log_error(
                f"[history_evaluator] Failed to evaluate history for {interface_path}: {e}"
            )
            return "[History Evaluator] Error while retrieving history."


PLUGIN_CLASS = HistoryEvaluatorPlugin
