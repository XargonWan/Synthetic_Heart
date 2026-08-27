"""Recon plugin: does this Vessel turn need out-of-world context?

While SyntH is embodied in a Rift Vessel world (Minecraft, …) the prompt is
deliberately scoped *to the world* (see :mod:`core.vessel_focus` and the vessel
focus block in :mod:`core.history_engine`): global chat interfaces, diary,
memories and the rolling "recent chats" block are suppressed so the persona
concentrates on the game.

Sometimes, though, an in-world request genuinely needs "reach outside the game"
— e.g. a player asking SyntH about a Telegram/Discord conversation, the weather,
or anything that lives beyond the world. This recon plugin asks the recon LLM a
single structural yes/no question and records the boolean answer on the context
memory as ``vessel_needs_external_reach``.

Two hard project rules are honoured:

* **Keyword-free.** The decision is delegated to the recon LLM; no trigger words
  or regexes are used here.
* **Gated by the Vessel connection, per turn.** The recon *key* is only injected
  into the combined recon prompt when the current turn is a vessel-focus turn
  (:func:`core.vessel_focus.is_vessel_turn`). This is enforced *before* the
  combined recon call via the optional ``is_recon_eligible`` hook, because
  returning ``[]`` from :meth:`parse_recon_response` would be too late — the key
  would already be part of the single recon LLM call.

For now the boolean is only *stored* on the context memory (future-proofing);
wiring it to actually re-enable out-of-world context is intentionally deferred.
"""

from __future__ import annotations

from typing import Any, List, Optional

from core.config_manager import config_registry
from core.logging_utils import log_debug

display_name = "Recon Vessel External Reach"

# UI-exposed switch to enable/disable this recon plugin.
try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "RECON_VESSEL_REACH_RECON_ENABLED",
        label="Enable Recon Vessel External Reach",
        default=True,
        value_type=bool,
        ui_type="bool",
        description=(
            "While embodied in a Rift Vessel world, ask the recon LLM whether the "
            "current in-world message needs out-of-world context (other chat "
            "interfaces, weather, etc.)."
        ),
        scope="recon",
        component="recon_vessel_reach",
        hidden=True,
    )
except Exception:
    config_registry.get_var(
        "RECON_VESSEL_REACH_RECON_ENABLED",
        True,
        value_type=bool,
        label="Enable Recon Vessel External Reach",
        description=(
            "While embodied in a Rift Vessel world, ask the recon LLM whether the "
            "current in-world message needs out-of-world context (other chat "
            "interfaces, weather, etc.)."
        ),
        group="recon",
        component="recon_vessel_reach",
        hidden=True,
    )


class ReconVesselReachPlugin:
    display_name = display_name
    recon_priority = 4

    def get_supported_actions(self) -> dict:
        return {}

    def get_recon_key(self) -> str:
        return "vessel_needs_external_reach"

    def is_recon_eligible(
        self, message: Any | None = None, context_memory: Any | None = None
    ) -> bool:
        """Only participate in recon during a Vessel embodiment turn.

        Consulted by :func:`core.recon.gather_recon_contributions` *before* the
        combined recon prompt is built, so the key never appears for ordinary
        (non-vessel) turns that may be queued while a session is active.
        """
        try:
            from core.vessel_focus import is_vessel_turn

            return is_vessel_turn(message, context_memory)
        except Exception:
            return False

    def get_recon_instruction(self) -> str:
        return (
            "You are currently embodied inside a virtual/game world. By default "
            "you only perceive this world. Decide whether answering the current "
            "in-world message REQUIRES information from outside the game — for "
            "example other chat interfaces (Telegram, Discord, …), the real-world "
            "weather, or any fact that does not exist inside this world. If the "
            "message can be handled entirely from what happens in the world, no "
            "external reach is needed. The value for this key MUST be an object "
            'with exactly this shape: {"needs_external": true} or '
            '{"needs_external": false}.'
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
        """Record the recon LLM's yes/no answer on the context memory.

        The answer is stored as a plain boolean under
        ``context_memory["vessel_needs_external_reach"]``. No side effects beyond
        that (contribution list is always empty — this plugin injects a decision,
        not a text contribution).
        """
        enabled = bool(
            config_registry.get_value(
                "RECON_VESSEL_REACH_RECON_ENABLED", True, value_type=bool
            )
        )
        if not enabled:
            return []

        needs_external = self._extract_bool(data)

        if isinstance(context_memory, dict):
            context_memory["vessel_needs_external_reach"] = needs_external
            log_debug(
                f"[recon_vessel_reach] vessel_needs_external_reach={needs_external}"
            )

        return []

    @staticmethod
    def _extract_bool(raw: Any) -> bool:
        """Normalise the recon value into a boolean, fail-safe to ``False``.

        Accepts the canonical object form ``{"needs_external": bool}`` as well as
        a bare boolean or a truthy/falsey scalar the recon model might emit.
        """
        try:
            if isinstance(raw, dict):
                val = raw.get("needs_external")
            else:
                val = raw
            if isinstance(val, bool):
                return val
            if isinstance(val, (int, float)):
                return bool(val)
            if isinstance(val, str):
                return val.strip().lower() in ("true", "1", "yes")
        except Exception:
            return False
        return False


PLUGIN_CLASS = ReconVesselReachPlugin
