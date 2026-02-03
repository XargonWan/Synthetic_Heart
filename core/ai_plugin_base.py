# core/ai_plugin_base.py

from __future__ import annotations

from typing import TYPE_CHECKING, List

from core.prompt_engine import build_prompt

if TYPE_CHECKING:  # pragma: no cover
    from core.history_types import HistoryContribution


class AIPluginBase:
    """
    Base interface for every AI engine.
    Each plugin (OpenAI, Claude, Manual, etc.) may implement the desired methods.
    """

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

    # --- Agent hooks (optional) ---
    def supports_agent(self) -> bool:
        """Return True if this engine/plugin supports agentic extensions.

        Default: False. Engines that support agent flows should override this.
        """
        return False

    def attach_agent(self, agent_plugin) -> None:
        """Attach an Agent plugin instance to this engine (optional)."""
        # Default: store reference and set attribute for convenience
        try:
            setattr(self, "_agent_plugin", agent_plugin)
            setattr(self, "agent_enabled", True)
        except Exception:
            pass

    def detach_agent(self, agent_plugin) -> None:
        """Detach a previously attached Agent plugin instance (optional)."""
        try:
            if hasattr(self, "_agent_plugin"):
                delattr(self, "_agent_plugin")
            setattr(self, "agent_enabled", False)
        except Exception:
            pass

    def agent_prepare_prompt(self, context: dict | None = None) -> dict | None:
        """Optional hook: provide extra prompt preamble or structured context for agent loops."""
        return None

    def agent_execute(self, action_dict: dict, context: dict | None = None) -> dict:
        """Optional engine-level execution helper for agentic actions.

        Default implementation returns an unsupported payload so callers can fall back.
        """
        return {"status": "unsupported", "reason": "engine does not implement agent_execute"}

    async def handle_custom_action(self, action_type: str, payload: dict):
        """Handle a plugin-defined custom action."""
        raise NotImplementedError("handle_custom_action not implemented")

    async def execute_action(self, action: dict, context: dict, bot, original_message):
        """Bridge method to call handle_custom_action from action_parser.

        Default implementation extracts action type and payload.
        """
        action_type = action.get("type")
        payload = action.get("payload", {})
        return await self.handle_custom_action(action_type, payload)

    @staticmethod
    def get_supported_actions() -> dict:
        """Return schema information for supported actions."""
        return {}

    def get_prompt_instructions(self, action_name: str) -> dict:
        """Return prompt instructions for the given action."""
        return {}

    def get_history_contributions(self, **kwargs) -> List['HistoryContribution']:
        """Optional: provide history contributions for prompt context.

        Engines may choose to contribute context (e.g., model notes). The core
        `HistoryEngine` will handle toggles, ordering, limiting, and dedup.
        """

        return []
