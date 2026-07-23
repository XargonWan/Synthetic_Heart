from __future__ import annotations

from typing import List

from core.config_manager import config_registry
from core.logging_utils import log_info, log_warning
from core.transport_layer import extract_json_from_text


display_name = "Recon Tone Evaluator"

# UI-exposed switch for enabling tone evaluator recon contributions
try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "RECON_TONE_EVALUATOR_RECON_ENABLED",
        label="Enable Recon Tone Evaluator",
        default=True,
        value_type=bool,
        ui_type="bool",
        description="Enable the Recon Tone Evaluator plugin (produce tone hints for Recon).",
        scope="recon",
        component="recon",
    )
except Exception:
    from core.config_manager import config_registry

    config_registry.get_var(
        "RECON_TONE_EVALUATOR_RECON_ENABLED",
        True,
        value_type=bool,
        label="Enable Recon Tone Evaluator",
        description="Enable the Recon Tone Evaluator plugin (produce tone hints for Recon).",
        group="recon",
        component="recon",
    )


class ReconToneEvaluatorPlugin:
    display_name = display_name
    recon_priority = 6

    def get_supported_actions(self) -> dict:
        return {}

    def get_recon_key(self) -> str:
        return "tone_hint"

    def get_recon_instruction(self) -> str:
        return (
            "Determine the message tone and the overall conversation tone. "
            'Return as an object: {"message_tone": "...", '
            '"conversation_tone": "...", "sticky": false}.'
        )

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
        if not text or not isinstance(text, str) or not text.strip():
            return []

        enabled = bool(
            config_registry.get_value(
                "RECON_TONE_EVALUATOR_RECON_ENABLED", True, value_type=bool
            )
        )
        if not enabled:
            return []

        if not isinstance(data, dict):
            return []

        message_tone = str(data.get("message_tone") or "").strip()
        conversation_tone = str(data.get("conversation_tone") or "").strip()
        sticky = bool(data.get("sticky", False))

        if not message_tone and not conversation_tone:
            return []

        contrib = {
            "type": "tone_hint",
            "message_tone": message_tone or None,
            "conversation_tone": conversation_tone or None,
            "sticky": sticky,
            "source": "tone_evaluator",
            "priority": int(self.recon_priority),
        }

        log_info("[recon_tone] Added tone_hint contribution")
        return [contrib]

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
        if not text or not isinstance(text, str) or not text.strip():
            return []

        enabled = bool(
            config_registry.get_value(
                "RECON_TONE_EVALUATOR_RECON_ENABLED", True, value_type=bool
            )
        )
        if not enabled:
            return []

        engine = None
        try:
            from core.config import get_active_cortex_engine
            from core.cortex_registry import get_cortex_registry

            active_cortex = await get_active_cortex_engine()
            registry = get_cortex_registry()
            engine = registry.get_engine(active_cortex) or registry.load_engine(
                active_cortex
            )
        except Exception as e:
            log_warning(f"[recon_tone] Failed to load active Cortex engine: {e}")
            engine = None

        if not engine or not hasattr(engine, "generate_response"):
            return []

        system_prompt = (
            "This is a Recon prompt, please execute what is requested below:\n"
            "- Determine the message tone and the overall conversation tone.\n"
            'Return ONLY valid JSON: {"message_tone":"...","conversation_tone":"...","sticky":false}.\n'
            "Use short tone labels like: warm, formal, playful, serious, sarcastic, empathetic, neutral."
        )

        user_prompt = f"User message:\n{text.strip()}\n"

        try:
            llm_text = await engine.generate_response(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
            )
        except Exception as e:
            log_warning(f"[recon_tone] LLM generate_response failed: {e}")
            return []

        parsed = None
        try:
            parsed = extract_json_from_text(llm_text, return_metadata=False)
        except Exception:
            parsed = None

        if not isinstance(parsed, dict):
            return []

        message_tone = str(parsed.get("message_tone") or "").strip()
        conversation_tone = str(parsed.get("conversation_tone") or "").strip()
        sticky = bool(parsed.get("sticky", False))

        if not message_tone and not conversation_tone:
            return []

        contrib = {
            "type": "tone_hint",
            "message_tone": message_tone or None,
            "conversation_tone": conversation_tone or None,
            "sticky": sticky,
            "source": "tone_evaluator",
            "priority": int(self.recon_priority),
        }

        log_info("[recon_tone] Added tone_hint contribution")
        return [contrib]


PLUGIN_CLASS = ReconToneEvaluatorPlugin
