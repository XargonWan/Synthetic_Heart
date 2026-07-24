# core/ai_plugin_base.py

from __future__ import annotations

from typing import TYPE_CHECKING, List


if TYPE_CHECKING:  # pragma: no cover
    from core.history_types import HistoryContribution


class AIPluginBase:
    """
    Base interface for every AI engine.
    Each plugin (OpenAI, Claude, Manual, etc.) may implement the desired methods.
    """

    supports_prompt_request = False

    async def handle_incoming_message(self, bot, message, prompt):
        """Process a message using a pre-built prompt."""
        raise NotImplementedError("handle_incoming_message not implemented")

    def get_target(self, trainer_message_id):
        """Return the trainer of a training message."""
        return None  # Default: does nothing

    def clear(self, trainer_message_id):
        """Remove proxy references once consumed."""
        pass  # Default: does nothing

    async def generate_response(self, messages):
        """Send messages to the LLM engine and receive the response."""
        raise NotImplementedError("generate_response not implemented")

    def get_supported_models(self) -> list[str]:
        """Optional. Return the list of available models."""
        return []

    def get_rate_limit(self):
        return (80, 10800, 0.5)

    def set_notify_fn(self, notify_fn):
        """Optional: dynamically update the notification function."""
        self.notify_fn = notify_fn

    def get_supported_action_types(self) -> list[str]:
        """Return custom action types handled by this plugin."""
        return []

    def is_enabled(self) -> bool:
        """Return True when this plugin should expose actions in the prompt."""
        return True

    def get_metadata(self) -> dict:
        """Return declarative plugin metadata for the WebUI and docs pipeline.

        See :meth:`core.plugin_base.PluginBase.get_metadata` for the full list
        of supported keys. The default implementation derives values
        reflectively so existing engines keep working without changes.
        """
        return {
            "name": self.get_display_name(),
            "display_name": self.get_display_name(),
            "description": self._reflective_description(),
        }

    def get_display_name(self) -> str:
        """Return a human-friendly name for this plugin."""
        for attr in ("display_name", "friendly_name", "name"):
            value = getattr(self, attr, None)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return self.__class__.__name__

    def _reflective_description(self) -> str:
        """Best-effort description derived from attributes/docstring."""
        candidate = getattr(self, "description", None)
        if isinstance(candidate, str) and candidate.strip():
            return " ".join(candidate.split())
        doc = (self.__class__.__doc__ or "").strip()
        if doc:
            return " ".join(doc.split())
        return ""

    async def handle_custom_action(self, action_type: str, payload: dict):
        """Handle a plugin-defined custom action."""
        raise NotImplementedError("handle_custom_action not implemented")

    async def execute_action(self, action: dict, context: dict, bot, original_message):
        """Bridge method to call handle_custom_action from action_parser.

        Default implementation extracts action type and payload.
        """
        action_type = action.get("type")
        if not isinstance(action_type, str):
            action_type = ""
        payload = action.get("payload", {})
        if not isinstance(payload, dict):
            payload = {}
        return await self.handle_custom_action(action_type, payload)

    @staticmethod
    def get_supported_actions() -> dict:
        """Return schema information for supported actions."""
        return {}

    def get_prompt_instructions(self, action_name: str) -> dict:
        """Return prompt instructions for the given action."""
        return {}

    def get_history_contributions(self, **kwargs) -> List["HistoryContribution"]:
        """Optional: provide history contributions for prompt context.

        Engines may choose to contribute context (e.g., model notes). The core
        `HistoryEngine` will handle toggles, ordering, limiting, and dedup.
        """

        return []
