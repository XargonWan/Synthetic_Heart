from __future__ import annotations

from typing import List

from core.config_manager import config_registry
from core.logging_utils import log_info, log_warning
from core.transport_layer import extract_json_from_text


display_name = "Recon Language Evaluator"

# UI-exposed switch for enabling language evaluator recon contributions
try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "RECON_LANGUAGE_EVALUATOR_RECON_ENABLED",
        label="Enable Recon Language Evaluator",
        default=True,
        value_type=bool,
        ui_type="bool",
        description="Enable the Recon Language Evaluator plugin (produce language hints for Recon).",
        scope="recon",
        component="recon_language_evaluator",
        hidden=True,
    )
except Exception:
    from core.config_manager import config_registry

    config_registry.get_var(
        "RECON_LANGUAGE_EVALUATOR_RECON_ENABLED",
        True,
        value_type=bool,
        label="Enable Recon Language Evaluator",
        description="Enable the Recon Language Evaluator plugin (produce language hints for Recon).",
        group="recon",
        component="recon_language_evaluator",
        hidden=True,
    )


class ReconLanguageEvaluatorPlugin:
    display_name = display_name
    recon_priority = 7

    def get_supported_actions(self) -> dict:
        return {}

    def get_recon_key(self) -> str:
        return "language_hint"

    def get_recon_instruction(self) -> str:
        return (
            "Detect the primary language of the conversation based ONLY on "
            "the Recent local history and human user messages. "
            "Ignore the global history and any assistant/bot messages when "
            "determining the language. "
            "Weight recent human user messages 3x compared to older history 1x. "
            "If the incoming user message is a system/internal prompt (not natural "
            "conversation), rely on the local chat history instead. "
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
        _raw_llm_text: str | None = None,
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
            from core.config import get_active_cortex_engine
            from core.cortex_registry import get_cortex_registry

            active_cortex = await get_active_cortex_engine()
            registry = get_cortex_registry()
            engine = registry.get_engine(active_cortex) or registry.load_engine(
                active_cortex
            )
        except Exception as e:
            log_warning(f"[recon_lang] Failed to load active Cortex engine: {e}")
            engine = None

        if not engine or not hasattr(engine, "generate_response"):
            return []

        # Build local (interface) history only; global history must not influence
        # language detection per requirements.
        local_lines: list[str] = []
        interface_path = getattr(message, "interface_path", None)
        try:
            if isinstance(context_memory, dict) and interface_path in context_memory:
                raw = list(context_memory.get(interface_path, []))
                for item in raw[-6:]:
                    if isinstance(item, dict):
                        sender = (
                            item.get("sender_name") or item.get("sender") or "unknown"
                        )
                        content = (
                            item.get("text")
                            or item.get("message_text")
                            or item.get("content")
                            or ""
                        )
                        if content:
                            local_lines.append(f"[{sender}] {content}")
        except Exception:
            pass

        try:
            from core.chat_history_cache import load_chat_history

            if interface_path:
                cached = await load_chat_history(interface_path)
                for item in list(cached)[-6:]:
                    sender = item.get("sender_name") or "unknown"
                    content = item.get("text") or ""
                    if content:
                        local_lines.append(f"[{sender}] {content}")
        except Exception:
            pass

        local_text = "\n".join(local_lines) if local_lines else "(none)"

        system_prompt = (
            "This is a Recon prompt, please execute what is requested below:\n"
            "- Detect the primary language of the conversation.\n"
            "- When determining language, treat the incoming message with weight 3, any response message (if present) with weight 1, and the local history with weight 1.\n"
            "Use ONLY the user message and recent history from the same interface path.\n"
            "Do NOT consider global chat or any other interface history when choosing the language.\n"
            'Return ONLY valid JSON: {"language_code":"it"}.\n'
            "Use BCP-47 language codes (e.g., en, it, es, fr)."
        )

        user_prompt = (
            f"User message:\n{text.strip()}\n\n"
            f"Recent interface history:\n{local_text}\n"
        )

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

        # parsed might be None or not contain language_code; don't early-return so
        # we can attempt a history-based fallback when possible.
        language_code = ""
        if isinstance(parsed, dict):
            language_code = str(parsed.get("language_code") or "").strip()

        # If LLM couldn't detect language from the immediate message text,
        # try a history-based fallback using the message's interface_path.
        if not language_code:
            try:
                # determine interface_path from message (supports object or dict)
                interface_path = None
                if message:
                    interface_path = getattr(message, "interface_path", None) or (
                        message.get("interface_path")
                        if isinstance(message, dict)
                        else None
                    )

                if interface_path:
                    # load recent chat history and build a compact prompt
                    from core.chat_history_cache import load_chat_history

                    hist = await load_chat_history(interface_path)
                    hist_items = list(hist)[-6:]
                    hist_lines = []
                    for item in hist_items:
                        txt = item.get("text") or item.get("message_text") or ""
                        sender = (
                            item.get("sender_name") or item.get("sender") or "unknown"
                        )
                        if txt:
                            hist_lines.append(f"[{sender}] {txt}")

                    if hist_lines and engine:
                        history_prompt = "\n".join(hist_lines)
                        user_prompt = (
                            f"Chat history for {interface_path}:\n{history_prompt}\n\n"
                            'Detect the primary language of the conversation and return ONLY valid JSON: {"language_code": "xx"}. '
                            "When determining language, weight the incoming message 3, any response 1, and the history 1."
                        )
                        try:
                            llm_text2 = await engine.generate_response(
                                [
                                    {"role": "system", "content": system_prompt},
                                    {"role": "user", "content": user_prompt},
                                ]
                            )
                            parsed2 = None
                            try:
                                parsed2 = extract_json_from_text(
                                    llm_text2, return_metadata=False
                                )
                            except Exception:
                                parsed2 = None

                            if isinstance(parsed2, dict):
                                language_code = str(
                                    parsed2.get("language_code") or ""
                                ).strip()
                                if language_code:
                                    log_info(
                                        f"[recon_lang] Fallback language detected from history {interface_path}: {language_code}"
                                    )
                        except Exception as e:
                            log_warning(
                                f"[recon_lang] History-based detection failed: {e}"
                            )
            except Exception as e:
                log_warning(f"[recon_lang] History fallback error: {e}")

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
