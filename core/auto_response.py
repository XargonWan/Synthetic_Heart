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
    build_delivery_request,
    load_json_instructions,
)
from core.action_parser import CORRECTOR_RETRIES


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
                return

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
                message_block = {"action_outputs": action_outputs}
            else:
                message_block = output

            # Build explicit instruction to prevent action loops
            # Tell LLM to respond to user with results, NOT call the same action again
            loop_prevention_instruction = (
                f"IMPORTANT: These are the results from your '{action_type}' action. "
                f"DO NOT call '{action_type}' again. Respond directly to the user with these results. "
                f"Output a single message_* action with your response text. No additional actions needed."
            )

            system_payload = {
                "system_message": {
                    "type": "output",
                    "action_type": action_type,  # Track which action produced these results
                    "instruction": loop_prevention_instruction,
                    "message": message_block,
                    "full_json_instructions": json_rules,
                    "is_action_result_delivery": True,  # Flag for downstream loop prevention
                    "max_correction_attempts": 1,  # Limit corrections for action result responses
                }
            }

            # Phase 3: Attach PromptRequest so migrated engines can use native rendering.
            # Engines that haven't migrated yet ignore __prompt_request entirely.
            try:
                _pr = await build_delivery_request(
                    action_type=action_type,
                    action_outputs=(
                        action_outputs
                        if action_outputs is not None
                        else [{"output": str(output)}]
                    ),
                    interface_name=interface_name,
                    interface_path=interface_path,
                )
                system_payload["__prompt_request"] = _pr
                log_debug("[auto_response] delivery PromptRequest attached")
            except Exception as _drq_exc:
                log_debug(f"[auto_response] build_delivery_request skipped: {_drq_exc}")

            log_info(
                f"[auto_response] Requesting LLM to deliver {action_type} output to chat {chat_id}"
            )

            # Get interface instance dynamically without hardcoding
            from core.core_initializer import INTERFACE_REGISTRY

            interface = INTERFACE_REGISTRY.get(interface_name)
            if not interface:
                log_error(f"[auto_response] No interface '{interface_name}' available")
                return

            # Enqueue the LLM request
            await enqueue(
                interface,
                mock_message,
                json.dumps(system_payload, ensure_ascii=False),
                priority=True,
            )

        except Exception as e:
            log_error(f"[auto_response] Failed to request LLM response: {e}")
            import traceback

            traceback.print_exc()


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
        await _auto_response_system.request_llm_response(
            original_context=original_context,
            action_type=action_type or "unknown",
            action_outputs=action_outputs,
        )
        return

    if output is not None and original_context is not None:
        await _auto_response_system.request_llm_response(
            output,
            original_context,
            action_type or "unknown",
            command,
        )
        return

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
