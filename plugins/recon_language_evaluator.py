from __future__ import annotations

from typing import List

from core.config_manager import config_registry
from core.logging_utils import log_info, log_warning
from core.transport_layer import extract_json_from_text


display_name = "Recon Language Evaluator"


class ReconLanguageEvaluatorPlugin:
    display_name = display_name
    recon_priority = 7

    def get_supported_actions(self) -> dict:
        return {}

    def get_recon_key(self) -> str:
        return "language_hint"

    def get_recon_instruction(self) -> str:
        return (
            "Detect the primary language of the conversation. "
            'Return as an object: {"language_code": "it"}.'
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
    ) -> list[dict]:
        if not text or not isinstance(text, str) or not text.strip():
            return []

        enabled = bool(
            config_registry.get_value(
                "RECON_LANGUAGE_EVALUATOR_RECON_ENABLED", True, value_type=bool
            )
        )
        if not enabled:
            return []

        if not isinstance(data, dict):
            return []

        language_code = str(data.get("language_code") or "").strip()
        if not language_code:
            return []

        contrib = {
            "type": "language_hint",
            "language_code": language_code,
            "source": "language_evaluator",
            "priority": int(self.recon_priority),
        }

        log_info("[recon_lang] Added language_hint contribution")
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
                "RECON_LANGUAGE_EVALUATOR_RECON_ENABLED", True, value_type=bool
            )
        )
        if not enabled:
            return []

        engine = None
        try:
            from core.config import get_active_llm
            from core.llm_registry import get_llm_registry

            active_llm = await get_active_llm()
            registry = get_llm_registry()
            engine = registry.get_engine(active_llm) or registry.load_engine(active_llm)
        except Exception as e:
            log_warning(f"[recon_lang] Failed to load active LLM: {e}")
            engine = None

        if not engine or not hasattr(engine, "generate_response"):
            return []

        system_prompt = (
            "This is a Recon prompt, please execute what is requested below:\n"
            "- Detect the primary language of the conversation.\n"
            'Return ONLY valid JSON: {"language_code":"it"}.\n'
            "Use BCP-47 language codes (e.g., en, it, es, fr)."
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
            log_warning(f"[recon_lang] LLM generate_response failed: {e}")
            return []

        parsed = None
        try:
            parsed = extract_json_from_text(llm_text, return_metadata=False)
        except Exception:
            parsed = None

        if not isinstance(parsed, dict):
            return []

        language_code = str(parsed.get("language_code") or "").strip()
        if not language_code:
            return []

        contrib = {
            "type": "language_hint",
            "language_code": language_code,
            "source": "language_evaluator",
            "priority": int(self.recon_priority),
        }

        log_info("[recon_lang] Added language_hint contribution")
        return [contrib]


PLUGIN_CLASS = ReconLanguageEvaluatorPlugin
