"""Recon plugin: which of the optional actions does this turn need in full?

A chat prompt used to carry the full JSON schema of every action in scope, more than
half of the prompt (see :mod:`core.action_tiers`). With ``PROMPT_ACTIONS_TIERED`` the
core actions keep their full schema and the rest are sent compact, so the model
needs a way to say which of those it wants spelled out this turn. This plugin asks
the shared Recon call: it is shown a one-line catalogue of the optional actions and
answers with the ones the conversation may need.

Keyword-free: the choice is the Recon model's, made from the conversation; nothing
here matches words. If the answer is missing or unusable the plugin returns no
verdict at all, and the prompt engine then sends the whole catalogue in full, so a
failed Recon call can only cost tokens, never an action.
"""

from __future__ import annotations

from typing import Any, List, Optional

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_info

display_name = "Recon Action Needs"

try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "RECON_ACTION_NEEDS_RECON_ENABLED",
        label="Enable Recon Action Needs",
        default=True,
        value_type=bool,
        ui_type="bool",
        description=(
            "Let the Recon call choose which optional actions get their full schema "
            "in the main prompt (see Tier the action catalogue)."
        ),
        scope="recon",
        component="recon_action_needs",
        hidden=True,
    )
except Exception:
    config_registry.get_var(
        "RECON_ACTION_NEEDS_RECON_ENABLED",
        True,
        value_type=bool,
        label="Enable Recon Action Needs",
        description=(
            "Let the Recon call choose which optional actions get their full schema "
            "in the main prompt (see Tier the action catalogue)."
        ),
        group="recon",
        component="recon_action_needs",
        hidden=True,
    )


def _available_actions() -> dict[str, Any]:
    """The registered actions, as the prompt engine reads them.

    ``actions_block`` lives on the ``core_initializer`` *instance*; reading it off
    the module returned nothing, which made this plugin decline every turn.
    """
    from core.core_initializer import core_initializer

    block = getattr(core_initializer, "actions_block", None) or {}
    actions = block.get("available_actions", {}) if isinstance(block, dict) else {}
    return actions if isinstance(actions, dict) else {}


def _system_only_names() -> set[str]:
    try:
        from core.prompt_engine import _SYSTEM_ONLY_ACTION_NAMES

        return set(_SYSTEM_ONLY_ACTION_NAMES)
    except Exception:
        return set()


class ReconActionNeedsPlugin:
    display_name = display_name
    recon_priority = 3

    def get_supported_actions(self) -> dict:
        return {}

    def get_recon_key(self) -> str:
        return "action_needs"

    def is_recon_eligible(
        self, message: Any | None = None, context_memory: Any | None = None
    ) -> bool:
        """Only take part when the catalogue is tiered and there is something to pick.

        Checked before the combined Recon prompt is built, so with tiering off the
        key (and its catalogue) never adds to the Recon call.
        """
        try:
            from core.action_tiers import optional_action_briefs, tiering_enabled

            if not tiering_enabled():
                return False
            return bool(
                optional_action_briefs(_available_actions(), skip=_system_only_names())
            )
        except Exception:
            return False

    def get_recon_instruction(
        self, message: Any | None = None, context_memory: Any | None = None
    ) -> str:
        from core.action_tiers import optional_action_briefs

        briefs = optional_action_briefs(_available_actions(), skip=_system_only_names())
        lines = "\n".join(
            f"  - {name}: {brief}" for name, brief in sorted(briefs.items())
        )
        return (
            "Decide which of the optional actions below the assistant may USE in "
            "its next reply, judging from the whole conversation and what the "
            "message asks for or implies (not from single words). An action left "
            "out cannot be used this turn, while one listed by mistake only costs "
            "a few tokens, so include every action the message could reasonably "
            "call for, whether it is about the user or about the assistant itself "
            "(its own goals, reminders, profile notes, preferences, ...); when in "
            "doubt, include it. List none only when the message clearly needs "
            "nothing beyond a normal reply. Messaging, the diary, emotions and "
            "animations are always available and must not be listed. "
            'Return an object of exactly this shape: {"actions": ["action_name", ...]} '
            "(an empty list when none is needed).\nOptional actions:\n" + lines
        )

    async def parse_recon_response(
        self,
        data: Any,
        *,
        message: Any | None = None,
        context_memory: Any | None = None,
        text: Optional[str] = None,
        tags: Optional[List[str]] = None,
        keywords: Optional[List[str]] = None,
        max_results: int = 5,
        _raw_llm_text: Optional[str] = None,
    ) -> list[dict]:
        enabled = bool(
            config_registry.get_value(
                "RECON_ACTION_NEEDS_RECON_ENABLED", True, value_type=bool
            )
        )
        if not enabled:
            return []

        names = self._extract_names(data)
        if names is None:
            # No usable answer: give no verdict, so the full catalogue is sent.
            log_debug("[recon_action_needs] no usable verdict; catalogue stays full")
            return []

        known = set(_available_actions())
        chosen = sorted(n for n in names if n in known)
        log_info(f"[recon_action_needs] optional actions for this turn: {chosen}")
        return [
            {
                "type": "action_hint",
                "content": "actions: " + (", ".join(chosen) or "none"),
                "names": chosen,
                "source": "recon_action_needs",
                "priority": int(self.recon_priority),
            }
        ]

    @staticmethod
    def _extract_names(data: Any) -> list[str] | None:
        """The action names in the answer, or ``None`` when there is no answer."""
        if isinstance(data, dict):
            data = data.get("actions")
        if data is None:
            return None
        if isinstance(data, str):
            data = [part.strip() for part in data.split(",")]
        if not isinstance(data, list):
            return None
        return [str(item).strip() for item in data if str(item).strip()]


PLUGIN_CLASS = ReconActionNeedsPlugin
