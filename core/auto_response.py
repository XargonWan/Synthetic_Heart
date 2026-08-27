# core/auto_response.py
"""
System for automatic LLM-mediated responses from interface actions.
Used when interfaces need to report results back through the LLM instead of directly.
"""

import asyncio
import json
from core.logging_utils import log_debug, log_info, log_warning, log_error
from typing import Dict, Any, Optional, List
from datetime import datetime
from core.prompt_engine import (
    load_json_instructions,
)
from core.action_parser import CORRECTOR_RETRIES

# Shared delivery style rule (2026-08-21): when Synth reports completed work —
# e.g. web-search results — it must NOT introduce itself. The observed failure
# mode was the model opening a search-result delivery with "Ciao, sono Rekku!"
# (a self-presentation to a user who has known it for the whole conversation),
# which reads as identity confusion. The rule is appended to EVERY action-result
# delivery prompt (Flow A here, Flow B in search_orchestrator, PromptRequest
# path in prompt_engine.build_delivery_request) and mirrored in the Agent Lane
# system prompt. Purely prompt guidance — no keyword logic.
NO_SELF_INTRODUCTION_RULE = (
    "DELIVERY STYLE: never introduce yourself and never greet with your name "
    "— the user already knows who you are, so do NOT say 'I am <name>' or "
    "open with any self-presentation. Start directly with the substance of "
    "what you found or did (e.g. 'I searched online and ...' / 'Ho cercato "
    "su internet e ...'), in the conversation's own language."
)


class AutoResponseSystem:
    """Manages automatic responses through LLM for interface actions."""

    def __init__(self):
        self._pending_responses = {}

    async def request_llm_response(
        self,
        output: Optional[str] = None,
        original_context: Optional[Dict[str, Any]] = None,
        action_type: str = "unknown",
        command: str = None,
        action_outputs: Optional[List[Dict[str, Any]]] = None,
    ):
        """
        Request LLM to process and deliver outputs back to the user.

        Args:
            output: The result from a single action (legacy path)
            original_context: Context from the original request (chat_id, etc.)
            action_type: The type of action that generated this output
            command: The original command if applicable
            action_outputs: List of outputs from multiple actions
        """
        try:
            # Import here to avoid circular imports
            from core.message_queue import enqueue

            # Build context for LLM
            # Support both new interface_path and legacy chat_id
            interface_path = original_context.get("interface_path")
            chat_id = original_context.get("chat_id")
            message_id = original_context.get("message_id")
            interface_name = original_context.get("interface_name")

            if not interface_name:
                log_error("No interface_name specified in auto_response context")
                return False

            # Create a mock message object for the LLM request
            from types import SimpleNamespace

            mock_message = SimpleNamespace()
            # Basic identifiers
            mock_message.chat_id = chat_id
            mock_message.interface_path = interface_path
            mock_message.message_id = message_id or 0
            if action_outputs is not None:
                mock_message.text = json.dumps(
                    {"action_outputs": action_outputs}, ensure_ascii=False
                )
            else:
                mock_message.text = (
                    f"Auto-response for {action_type}: {command}"
                    if command
                    else f"Auto-response for {action_type}"
                )
            # Populate minimal from_user info
            mock_message.from_user = SimpleNamespace()
            mock_message.from_user.id = chat_id
            mock_message.from_user.username = "auto_response"
            mock_message.from_user.first_name = "AutoResponse"
            mock_message.from_user.full_name = "AutoResponse"

            # Message metadata expected by downstream handlers
            mock_message.date = datetime.utcnow()
            mock_message.reply_to_message = None

            # Provide chat structure expected by message_queue.enqueue
            mock_message.chat = SimpleNamespace()
            mock_message.chat.id = chat_id
            mock_message.chat.title = None
            mock_message.chat.username = None
            mock_message.chat.first_name = "AutoResponse"
            mock_message.chat.type = "private"

            json_rules = load_json_instructions()
            if action_outputs is not None:
                message_block = json.dumps(
                    {"action_outputs": action_outputs}, ensure_ascii=False
                )
            else:
                message_block = output

            # The delivery system message is rendered verbatim as the LLM's
            # system content (the bridge routes any system_message dict through
            # its role-splitter, which reads only 'message'), so 'message' must
            # be a complete, self-contained instruction: delivery task + reply
            # format + the results. Without an explicit format the LLM returns
            # {"response": ...} instead of a message_* action, which then trips
            # the corrector (observed live 2026-08-12).
            if action_outputs is not None:
                # The delivery turn is a standalone prompt with no chat history,
                # so the persona must be injected here or the LLM answers as a
                # generic assistant (observed live 2026-08-12). Mirrors the
                # persona gathering in prompt_engine.build_delivery_request and
                # is fail-safe: any error degrades to the plain delivery task.
                persona_block = ""
                try:
                    from core.action_parser import gather_static_injections
                    from types import SimpleNamespace

                    _mock_msg = SimpleNamespace(
                        chat_id=chat_id,
                        text="",
                        message_id=message_id or 0,
                        from_user=None,
                        date=datetime.now(),
                        reply_to_message=None,
                        interface_path=interface_path,
                    )
                    _inj = await gather_static_injections(_mock_msg, {})
                    _persona = str(_inj.get("persona") or "").strip()
                    if _persona:
                        persona_block = (
                            f"=== CRITICAL SYSTEM IDENTITY ===\n{_persona}\n\n"
                        )
                except Exception as _pe:
                    log_debug(f"[auto_response] delivery persona gather skipped: {_pe}")

                example_payload = '{"text": "<your reply>"'
                if interface_path:
                    example_payload += f', "interface_path": "{interface_path}"'
                example_payload += "}"
                delivery_note = (
                    f"DELIVERY TASK: These are the results from your "
                    f"'{action_type}' action. DO NOT call '{action_type}' again.\n"
                    f"{NO_SELF_INTRODUCTION_RULE}\n"
                    f"Compose a natural message to the user summarising these "
                    f"results. Reply with exactly ONE action:\n"
                    f'{{"actions": [{{"type": "message_{interface_name}", '
                    f'"payload": {example_payload}}}]}}\n'
                    f"Respond with ONLY valid JSON. No text before or after."
                )
                message_block = f"{persona_block}{delivery_note}\n\n=== RESULTS ===\n{message_block}"

            # Build explicit instruction to prevent action loops
            # Tell LLM to respond to user with results, NOT call the same action again
            loop_prevention_instruction = (
                f"IMPORTANT: These are the results from your '{action_type}' action. "
                f"DO NOT call '{action_type}' again. Respond directly to the user with these results. "
                f"Output a single message_* action with your response text. No additional actions needed."
            )

            # ── Structural loop prevention (search-loop fix, 2026-08-17) ──────
            # The delivery turn's ONLY job is to summarise the action results and
            # reply to the user. If the delivery LLM re-emits the producing action
            # (e.g. search_current_knowledge), the plugin runs the search again and
            # enqueues ANOTHER delivery turn — the observed "search loop" where one
            # user message produced many web-search replies. We restrict the delivery
            # turn to message_* actions only via allowed_action_types, so the LLM
            # cannot even see the producing action. build_prompt_request reads this
            # allowlist (parsing the enqueued JSON string) and filters the action
            # catalog; message_chain's leaked-action filter and corrector honour it
            # too. Scoped to this single delivery turn — never persisted, never
            # inherited by other turns. FAIL-CLOSED (hardening, 2026-08-18): the
            # allowlist is always set to a non-empty message_* set — derived from
            # the registered action catalog, with a structural fallback to the
            # registered interfaces' message actions — so a delivery turn can never
            # silently fall back to the full (unrestricted) catalog.
            delivery_allowed_action_types: list[str] = []
            try:
                from core.core_initializer import core_initializer

                _full_actions = dict(
                    core_initializer.actions_block.get("available_actions", {}) or {}
                )
                delivery_allowed_action_types = sorted(
                    k
                    for k in _full_actions
                    if k == "send_message" or k.startswith("message_")
                )
            except Exception as _aa_exc:
                log_debug(
                    f"[auto_response] delivery allowlist derive skipped: {_aa_exc}"
                )
            if not delivery_allowed_action_types:
                # Fail-closed fallback: derive the message_* set structurally from
                # the registered interfaces so the delivery turn is ALWAYS scoped.
                try:
                    from core.core_initializer import INTERFACE_REGISTRY

                    delivery_allowed_action_types = sorted(
                        {
                            f"message_{name}"
                            for name in INTERFACE_REGISTRY
                            if name and not str(name).startswith("_")
                        }
                    )
                except Exception as _fb_exc:
                    log_debug(
                        f"[auto_response] delivery allowlist fallback skipped: {_fb_exc}"
                    )

            system_payload: dict[str, Any] = {
                "system_message": {
                    "type": "output",
                    "action_type": action_type,  # Track which action produced these results
                    "instruction": loop_prevention_instruction,
                    "message": message_block,
                    "full_json_instructions": json_rules,
                    "is_action_result_delivery": True,  # Flag for downstream loop prevention
                    "max_correction_attempts": int(
                        CORRECTOR_RETRIES
                    ),  # Use configurable corrector retries
                    # Structural action_outputs so plugin_instance can build a
                    # message_*-only delivery PromptRequest without re-parsing
                    # the 'message' block (delivery-turn scoping, 2026-08-17).
                    "action_outputs": action_outputs,
                }
            }
            if delivery_allowed_action_types:
                system_payload["allowed_action_types"] = delivery_allowed_action_types

            log_info(
                f"[auto_response] Requesting LLM to deliver {action_type} output to chat {chat_id}"
            )

            # Get interface instance dynamically without hardcoding
            from core.core_initializer import INTERFACE_REGISTRY

            interface = INTERFACE_REGISTRY.get(interface_name)
            if not interface:
                log_error(f"[auto_response] No interface '{interface_name}' available")
                return False

            # Enqueue the LLM request
            await enqueue(
                interface,
                mock_message,
                json.dumps(system_payload, ensure_ascii=False),
                priority=True,
            )
            return True

        except Exception as e:
            log_error(f"[auto_response] Failed to request LLM response: {e}")
            import traceback

            traceback.print_exc()
            return False


# Global instance
_auto_response_system = AutoResponseSystem()


async def request_llm_delivery(
    message=None,
    interface=None,
    context=None,
    reason=None,
    output=None,
    original_context=None,
    action_type=None,
    command=None,
    action_outputs=None,
):
    """
    Unified convenience function to request LLM-mediated delivery.

    Supports multiple calling patterns:
    1. Legacy: request_llm_delivery(output, original_context, action_type, command)
    2. New: request_llm_delivery(message, interface, context, reason)
    """
    # Handle legacy calling pattern (terminal plugin style)
    if (action_outputs is not None) and original_context is not None:
        return await _auto_response_system.request_llm_response(
            original_context=original_context,
            action_type=action_type or "unknown",
            action_outputs=action_outputs,
        )

    if output is not None and original_context is not None:
        return await _auto_response_system.request_llm_response(
            output,
            original_context,
            action_type or "unknown",
            command,
        )

    # Handle new calling pattern (interface style)
    if message is not None or interface is not None:
        log_info(
            f"[auto_response] 📤 INTERFACE_TO_LLM: Processing {reason or 'autonomous'} request via interface"
        )
        try:
            json_rules = load_json_instructions()
            if isinstance(context, dict) and context.get("input", {}).get("type") in {
                "event",
                "event_reminder",
            }:
                log_info(
                    "[auto_response] 📬 EVENT REMINDER: Routing event reminder to LLM via interface_to_llm pattern"
                )
                system_payload = {
                    "system_message": {
                        "type": "event_reminder",
                        "message": context,
                        "full_json_instructions": json_rules,
                    }
                }
            else:
                log_debug("[auto_response] Routing output message to LLM")
                system_payload = {
                    "system_message": {
                        "type": "output",
                        "message": context,
                        "full_json_instructions": json_rules,
                    }
                }

            payload_json = json.dumps(system_payload, ensure_ascii=False)
            log_debug(
                f"[auto_response] Payload prepared ({len(payload_json)} bytes) for transmission via interface_to_llm"
            )
        except Exception as e:
            log_error(f"[auto_response] Failed to build payload for {reason}: {e}")
            return False

        for attempt in range(1, int(CORRECTOR_RETRIES) + 1):
            try:
                import core.plugin_instance as plugin_instance

                active_plugin = plugin_instance.get_plugin()
                if not active_plugin:
                    log_warning(
                        f"[auto_response] No active LLM plugin available (attempt {attempt}/{int(CORRECTOR_RETRIES)})"
                    )
                    await asyncio.sleep(1)
                    continue

                # Get the bot instance - prefer interface itself if it has send_message, otherwise look for bot attribute
                if hasattr(interface, "send_message"):
                    bot = interface
                else:
                    bot = getattr(interface, "bot", None)
                    if bot is None:
                        log_error(
                            "[auto_response] Interface has no bot instance or send_message method"
                        )
                        return

                # Log the direction of the message flow
                if message is not None:
                    log_info(
                        f"[auto_response] 📤 INTERFACE→LLM transmission: sending message via interface_to_llm transport layer (attempt {attempt}/{int(CORRECTOR_RETRIES)})"
                    )
                    await plugin_instance.handle_incoming_message(
                        interface, message, payload_json, interface.get_interface_id()
                    )
                else:
                    log_debug(
                        "[auto_response] Creating synthetic message for autonomous delivery"
                    )
                    from types import SimpleNamespace

                    mock_message = SimpleNamespace()
                    mock_message.chat_id = -1
                    mock_message.message_id = 0
                    mock_message.text = f"Auto-generated message for {reason}"
                    mock_message.from_user = SimpleNamespace(
                        id=0, username="auto_response", full_name="AutoResponder"
                    )
                    mock_message.chat = SimpleNamespace(id=-1, type="private")

                    log_info(
                        f"[auto_response] 📤 INTERFACE→LLM transmission: sending synthetic message via interface_to_llm transport layer (attempt {attempt}/{int(CORRECTOR_RETRIES)})"
                    )
                    await plugin_instance.handle_incoming_message(
                        interface,
                        mock_message,
                        payload_json,
                        interface.get_interface_id(),
                    )

                log_info(
                    "[auto_response] ✅ LLM delivery via interface_to_llm completed successfully"
                )
                return True
            except Exception as e:
                log_error(
                    f"[auto_response] Delivery attempt {attempt} for {reason} failed: {e}"
                )
                await asyncio.sleep(1)

        log_warning(
            f"[auto_response] Failed to process {reason} after {int(CORRECTOR_RETRIES)} attempts"
        )
        return False

    log_warning(
        "[auto_response] request_llm_delivery called with insufficient parameters"
    )
