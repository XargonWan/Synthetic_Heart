"""
plugins/grillo/grillo_action_checker.py

GrilloActionChecker: rileva frasi di "promessa" nelle risposte dell'LLM e chiede
al backend LLM di restituire SOLTANTO JSON con le azioni che concretizzano la promessa.

Importante: il prompt chiede di *generare* azioni che implementino la richiesta ("implement this instruction"),
non di valutare se l'assistente abbia effettivamente detto qualcosa.
"""

from types import SimpleNamespace
from typing import Optional, List, Dict, Any

from core.logging_utils import log_debug, log_info, log_warning
from core.transport_layer import extract_json_from_text


class GrilloActionChecker:
    """Utility class used by Grillo to inspect plain-text LLM replies and
    request JSON actions when the reply contains a promise like "ti avviso"
    or "I'll remind".
    """

    async def inspect_reply_and_suggest_actions(
        self, llm_reply: str, original_user_message: str, context: dict, message: Any
    ) -> Optional[List[Dict[str, Any]]]:
        """Ask the LLM to compare the original user message and the synth reply
        and, if the synth promised an action but did not produce it, return a
        JSON object containing the actions to implement the promise.

        IMPORTANT: This method does NOT attempt to detect promises via heuristics;
        it simply asks the LLM to judge the pair (original_user_message, llm_reply)
        and to respond with ONLY JSON of the form {"actions": [...]}.

        Args:
            llm_reply: the full text returned by the LLM
            original_user_message: the user's original message text
            context: transport/message context
            message: message-like object for preserving chat/thread metadata

        Returns:
            A list of action dicts, an empty list (if LLM determines no actions),
            or None on error.
        """
        try:
            if not llm_reply or not llm_reply.strip():
                return None

            log_info(
                f"[grillo.checker] Invoked for chat_id={getattr(message, 'chat_id', None)} reply_len={len(llm_reply)}"
            )

            # Avoid calling the LLM if we've already checked this message
            try:
                if getattr(message, "grillo_checked", False):
                    log_debug(
                        "[grillo.checker] Message already checked by Grillo; skipping"
                    )
                    return None
            except Exception:
                pass

            # Build a concise prompt that includes both the user's message and
            # the assistant's reply. We explicitly instruct the LLM to return
            # ONLY a JSON object. If no actions are needed, it must return
            # {"actions": []}. Also include a compact list of relevant available
            # actions and their required/optional fields so the model can generate
            # valid actions in correct shape.
            actions_snippet = self._build_available_actions_snippet()

            # Include execution metadata to help the LLM decide whether actions are missing
            chain_result = context.get("chain_result") if context else None
            last_action_result = getattr(message, "last_action_result", None)

            prompt = (
                f'User message: "{(original_user_message or "").strip()}"\n'
                f'Assistant reply: "{llm_reply.strip()}"\n\n'
                f"Execution summary:\n  chain_result: {chain_result}\n  last_action_result: {last_action_result}\n\n"
                f"Relevant actions:\n{actions_snippet}\n"
                "Task: Compare the user's message, the assistant's reply, and the execution summary. "
                "If the assistant promised to perform an action but did not create "
                "the corresponding actions, RETURN ONLY a JSON object with an array "
                "called 'actions' that implements what was promised. If there is nothing "
                'to do, RETURN {"actions": []}. Do NOT include any text outside the JSON. '
                "Do NOT create diary entries, thoughts, or unrelated logs—only actionable items."
            )

            # Build a lightweight message so plugins keep context (chat/thread)
            msg = SimpleNamespace()
            msg.chat_id = getattr(message, "chat_id", None)
            msg.text = llm_reply
            msg.thread_id = getattr(message, "thread_id", None)

            try:
                import core.plugin_instance as plugin_instance
            except Exception as e:
                log_warning(f"[grillo.checker] cannot import plugin_instance: {e}")
                return None

            try:
                suggested = await plugin_instance.handle_incoming_message(
                    None, msg, prompt
                )
            except Exception as e:
                log_warning(f"[grillo.checker] LLM plugin call failed: {e}")
                return None

            # Mark/check that we've asked for actions based on available schemas
            try:
                if hasattr(message, "__dict__"):
                    message.grillo_prompt_included_actions = True
                    message.grillo_checked = True
            except Exception:
                pass

            if not suggested or not isinstance(suggested, str):
                log_debug("[grillo.checker] LLM returned no textual suggestion")
                return None

            json_obj = None
            try:
                json_obj = extract_json_from_text(suggested)
            except Exception as e:
                log_warning(f"[grillo.checker] extract_json_from_text failed: {e}")

            if (
                not json_obj
                or not isinstance(json_obj, dict)
                or "actions" not in json_obj
            ):
                log_debug("[grillo.checker] No 'actions' array found in LLM suggestion")
                return None

            actions = json_obj.get("actions")
            if not isinstance(actions, list):
                log_debug("[grillo.checker] 'actions' is not a list")
                return None

            # Return the list (can be empty)
            log_info(f"[grillo.checker] LLM suggested {len(actions)} action(s)")
            return actions

        except Exception as e:
            log_warning(f"[grillo.checker] Unexpected error: {e}")
            return None

    def _build_available_actions_snippet(self) -> str:
        """Build a compact snippet describing relevant available actions and
        their required/optional fields. Limit size to avoid huge prompts.

        Returns a multi-line string suitable for inclusion in the prompt.
        """
        try:
            from core.core_initializer import core_initializer

            actions = core_initializer.actions_block.get("available_actions", {})
        except Exception:
            actions = {}

        # Select relevant actions by simple heuristics (message/schedule/event/send)
        relevant = []
        for atype, desc in actions.items():
            low = atype.lower()
            if any(
                k in low
                for k in (
                    "schedule",
                    "event",
                    "message",
                    "send",
                    "notify",
                    "calendar",
                    "remind",
                )
            ):
                relevant.append((atype, desc))

        # Limit number of actions included
        relevant = relevant[:10]

        lines = []
        for atype, desc in relevant:
            try:
                schema = desc.get("schema", {})
                required = (
                    schema.get("required", [])
                    if isinstance(schema.get("required", []), list)
                    else []
                )
                props = (
                    list(schema.get("properties", {}).keys())
                    if schema.get("properties")
                    else []
                )
                optional = [p for p in props if p not in required]
                lines.append(f"- {atype}: required={required}, optional={optional}")
            except Exception:
                lines.append(f"- {atype}: (schema unavailable)")

        if not lines:
            return "(no compact action schemas available)"
        return "\n".join(lines)
