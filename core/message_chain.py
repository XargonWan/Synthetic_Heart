# core/message_chain.py
"""Central message chain manager.

This module implements the message loop described by the user:

User -> Interface
Interface -> Message chain

Message chain receives messages (from interfaces or from LLM), tries to extract JSON
and send it to the action parser. If actions are executed the loop ends. If JSON-like
but invalid the message chain will call the corrector middleware (which queries the
active LLM plugin) until corrected JSON is returned or retries are exhausted.

The corrector never sends messages directly to interfaces; it only queries the LLM
via the registered plugin. The message chain marks LLM-origin messages so the
parser will only operate on model outputs.

Return codes:
- ACTIONS_EXECUTED -> actions parsed and executed
- BLOCKED -> message blocked (exhausted retries or explicit ignore)
- FORWARD_AS_TEXT -> not JSON-like; caller may forward plain text to interface
"""

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, Optional

from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.config_manager import config_registry

# Result constants
ACTIONS_EXECUTED = "ACTIONS_EXECUTED"
FORWARD_AS_TEXT = "FORWARD_AS_TEXT"
BLOCKED = "BLOCKED"
LLM_FAILED = "LLM_FAILED"

# Register FAILED_MESSAGE_TEXT configuration
FAILED_MESSAGE_TEXT = config_registry.get_var(
    "FAILED_MESSAGE_TEXT",
    "😵",
    label="Failed Message Text",
    description="Fallback message when LLM fails to respond or correct response.",
    group="core",
    component="core",
)

# Register RESPONSE_TIMEOUT configuration
RESPONSE_TIMEOUT = config_registry.get_var(
    "RESPONSE_TIMEOUT",
    240,
    label="Response Timeout",
    description="Maximum time in seconds to wait for LLM responses before sending fallback message.",
    value_type=int,
    group="core",
    component="core",
)


def get_failed_message_text() -> str:
    """Get the fallback message when LLM fails."""
    fallback = FAILED_MESSAGE_TEXT
    # Ensure we return a string (ConfigVar might be returned)
    if hasattr(fallback, "get_value"):
        fallback = fallback.get_value()
    return str(fallback)


# Map of interface prefixes to their correct message action types
_INTERFACE_TO_MESSAGE_ACTION: Dict[str, str] = {
    "telegram_bot": "message_telegram_bot",
    "discord_bot": "message_discord_bot",
    "synth_webui": "message_synth_webui",
    "matrix_chat": "message_matrix_chat",
    "ollama_serve": "message_ollama_serve",
}


def _auto_inject_interface_path(actions: list, interface_path: Optional[str]) -> list:
    """Auto-inject missing interface_path into message actions.

    LLMs sometimes forget to include interface_path in message actions, causing
    validation failures. This function detects message actions missing interface_path
    and injects it from the context before validation runs.

    Args:
        actions: List of action dicts to process
        interface_path: The interface path from context (e.g., 'telegram_bot/5208932647')

    Returns:
        The same list with interface_path injected in-place where missing
    """
    if not actions or not interface_path:
        return actions

    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = action.get("type") or action.get("action")
        # Only process registered interface message action types (no hard-coded checks)
        if not action_type:
            continue
        try:
            # Import helper from action_parser to determine if this action_type
            # is a user-facing message action according to registered interfaces
            from core.action_parser import _is_interface_message_action

            if not _is_interface_message_action(action_type):
                continue
        except Exception:
            # Fallback: if helper unavailable, skip injection to avoid false positives
            continue

        payload = action.get("payload")
        if not isinstance(payload, dict):
            # Create payload if missing
            payload = {}
            action["payload"] = payload

        # Check if interface_path is missing
        if not payload.get("interface_path") and not payload.get("chat_name"):
            log_info(
                f"[message_chain] 🔧 Auto-injecting interface_path='{interface_path}' into {action_type} action (was missing)"
            )
            payload["interface_path"] = interface_path

    return actions


def _normalize_message_unknown(actions: list, interface_path: Optional[str]) -> list:
    """Normalize 'message_unknown' action types to the correct interface-specific type.

    Some LLMs fabricate action types like 'message_unknown' instead of using the
    correct interface-specific action (e.g., 'message_telegram_bot'). This function
    detects and corrects such actions based on the interface_path.

    Args:
        actions: List of action dicts to normalize
        interface_path: The interface path (e.g., 'telegram_bot/5208932647')

    Returns:
        The same list with any 'message_unknown' types corrected in-place
    """
    if not actions or not interface_path:
        return actions

    # Extract interface prefix from path (e.g., 'telegram_bot' from 'telegram_bot/5208932647')
    interface_prefix = (
        interface_path.split("/")[0] if "/" in interface_path else interface_path
    )
    correct_action_type = _INTERFACE_TO_MESSAGE_ACTION.get(interface_prefix)

    if not correct_action_type:
        return actions

    # Only normalize if the resolved interface-specific action type is actually supported
    try:
        # Import dynamically to avoid circular import at module load
        from core.action_parser import get_supported_action_types

        supported = get_supported_action_types()
    except Exception:
        supported = set()

    # Only proceed with normalization if the target action is registered in the system
    if correct_action_type not in supported:
        log_debug(
            f"[message_chain] Skipping normalization to '{correct_action_type}' because it is not in supported action types"
        )
        return actions

    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = action.get("type") or action.get("action")
        # Normalize 'message_unknown' and similar fabricated types
        if (
            action_type
            and action_type.startswith("message_")
            and action_type not in _INTERFACE_TO_MESSAGE_ACTION.values()
        ):
            old_type = action_type
            # Update whichever key was used
            if "type" in action:
                action["type"] = correct_action_type
            if "action" in action:
                action["action"] = correct_action_type
            log_info(
                f"[message_chain] 🔧 Normalized invalid action type '{old_type}' -> '{correct_action_type}' based on interface_path={interface_path}"
            )

    return actions


async def send_llm_fallback_message(
    bot, message: SimpleNamespace, failure_reason: str, context: dict = None
) -> str:
    """Send fallback message when LLM fails and log the failure reason.

    Args:
        bot: The bot instance
        message: The message object (may have interface_path attribute)
        failure_reason: Description of why the LLM failed
        context: Optional context dict that may contain interface_path
    """
    fallback_text = get_failed_message_text()
    # Ensure fallback_text is a string (ConfigVar might be returned)
    if hasattr(fallback_text, "get_value"):
        fallback_text = fallback_text.get_value()
    fallback_text = str(fallback_text)
    chat_id = getattr(message, "chat_id", None)
    # Preserve thread_id when available so the fallback message is routed to the
    # same message thread and not defaulted to 0
    thread_id = getattr(message, "thread_id", None)
    if not thread_id and context:
        thread_id = context.get("thread_id")

    # Extract interface_path from message or context - CRITICAL for routing to correct interface
    interface_path = getattr(message, "interface_path", None)
    if not interface_path and context:
        interface_path = context.get("interface_path")

    # Debug: trace all available routing info
    log_debug(
        f"[message_chain] FALLBACK ROUTING DEBUG: message.interface_path={getattr(message, 'interface_path', None)}, "
        f"context.interface_path={context.get('interface_path') if context else None}, "
        f"resolved_interface_path={interface_path}, chat_id={chat_id}, thread_id={thread_id}"
    )

    # Log detailed error
    log_error(
        f"[message_chain] LLM FAILURE - Chat: {chat_id}, Interface: {interface_path}, Thread: {thread_id}, Reason: {failure_reason}"
    )
    log_error(f"[message_chain] Sending fallback message: '{fallback_text}'")

    # Send fallback message through transport layer
    try:
        from core.transport_layer import universal_send

        # Get the send_message method from the bot (interface)
        if bot and hasattr(bot, "send_message"):
            try:
                # First attempt: prefer interface-friendly args (message_thread_id is commonly used)
                await universal_send(
                    bot.send_message,
                    chat_id,
                    text=fallback_text,
                    interface_path=interface_path,
                    thread_id=thread_id,
                    is_llm_response=True,  # Mark as LLM response so interface handles normally
                )
            except TypeError as te:
                # Some bots (or test fakes) don't accept 'message_thread_id'.
                # Retry without mapping thread id to message_thread_id.
                try:
                    log_warning(f"[message_chain] send_message TypeError, retrying without message_thread_id: {te}")
                    await universal_send(
                        bot.send_message,
                        chat_id,
                        text=fallback_text,
                        interface_path=interface_path,
                        is_llm_response=True,
                    )
                except Exception as e:
                    # If retry fails, surface the error but continue gracefully
                    log_error(f"[message_chain] Failed to send fallback message after retry: {e} (original: {te})")
        else:
            log_warning(
                "[message_chain] Bot does not have send_message method, cannot send fallback"
            )
        log_debug(
            f"[message_chain] Fallback message sent to chat {chat_id} via interface_path {interface_path} thread_id={thread_id}"
        )
        return fallback_text
    except Exception as e:
        log_error(f"[message_chain] Failed to send fallback message: {e}")
        return fallback_text


async def handle_incoming_message(
    bot,
    message: Optional[SimpleNamespace],
    text: str,
    *,
    source: str = "interface",
    context: Optional[Dict[str, Any]] = None,
    **kwargs,
):
    """Main entry point for the message chain.

    Parameters
    - bot: interface bot instance
    - message: SimpleNamespace-like message object (may be None)
    - text: incoming text to process
    - source: 'interface'|'user'|'llm' - origin of the text
    - context: optional context dict to pass to action parser
    - kwargs: additional metadata (e.g., thread_id)

    Returns one of the constants above.
    """
    # Local imports to avoid circular dependencies
    from core.transport_layer import extract_json_from_text, run_corrector_middleware
    from core.action_parser import run_actions, CORRECTOR_RETRIES
    from types import SimpleNamespace
    from datetime import datetime

    # Extract interface_path early for debug tracing
    _entry_interface_path = kwargs.get("interface_path") or (
        getattr(message, "interface_path", None) if message else None
    )
    _entry_chat_id = kwargs.get(
        "chat_id", getattr(message, "chat_id", "unknown") if message else "unknown"
    )
    _entry_thread_id = kwargs.get(
        "thread_id", getattr(message, "thread_id", None) if message else None
    )
    log_info(
        f"[message_chain] 🔄 ENTRY: source={source} text_len={len(text) if text else 0} chat_id={_entry_chat_id} interface_path={_entry_interface_path} thread_id={_entry_thread_id}"
    )
    log_debug(
        f"[message_chain] ENTRY CONTEXT: kwargs_keys={list(kwargs.keys())}, context_keys={list((context or {}).keys())}, message_type={type(message).__name__ if message else None}"
    )

    # Trace LLM→INTERFACE flow
    if source == "llm":
        log_info(
            "[message_chain] 📥 LLM→INTERFACE: Processing LLM response via message_chain (will apply llm_to_interface transport standards)"
        )

    if message is None:
        message = SimpleNamespace()
        message.chat_id = kwargs.get("chat_id")
        message.text = ""
        message.interface_path = kwargs.get("interface_path")
        message.date = datetime.utcnow()

    # Default context
    ctx = context or {}
    ctx["message"] = message
    ctx["original_text"] = (
        text  # Track original text in context, not on message (for consistency with immutable Telegram Message objects)
    )

    # Mark LLM-origin in context (not on message object, as Telegram Message objects are immutable)
    is_from_llm = True if source == "llm" else ctx.get("from_llm", False)
    ctx["from_llm"] = is_from_llm

    # Also set on message object if possible (for corrector_orchestrator and action_parser detection)
    try:
        if hasattr(message, "__dict__") or isinstance(message, type({})):
            message.from_llm = is_from_llm
    except (AttributeError, TypeError):
        pass  # Message object is immutable (Telegram Message); use ctx instead

    # Preserve chat_id and interface_path in context to avoid losing them during processing
    # Check message attributes, kwargs, and original context (in that priority order)
    if hasattr(message, "chat_id") and message.chat_id:
        ctx["chat_id"] = message.chat_id
    elif kwargs.get("chat_id") and not ctx.get("chat_id"):
        ctx["chat_id"] = kwargs["chat_id"]

    # interface_path is CRITICAL for routing - check all sources
    if hasattr(message, "interface_path") and message.interface_path:
        ctx["interface_path"] = message.interface_path
    elif kwargs.get("interface_path") and not ctx.get("interface_path"):
        ctx["interface_path"] = kwargs["interface_path"]
    # If still not set, try to build it from interface + chat_id
    if not ctx.get("interface_path"):
        interface_name = ctx.get("interface") or kwargs.get("interface")
        chat_id = ctx.get("chat_id")
        if interface_name and chat_id:
            ctx["interface_path"] = f"{interface_name}/{chat_id}"
            log_debug(
                f"[message_chain] Built interface_path from interface+chat_id: {ctx['interface_path']}"
            )

    log_debug(
        f"[message_chain] Context preserved: interface_path={ctx.get('interface_path')}, chat_id={ctx.get('chat_id')}, interface={ctx.get('interface')}"
    )

    # Process LLM messages for emotional state updates
    if ctx.get("from_llm", False) or source == "llm":
        log_info("[message_chain] 🎭 Starting emotion processing for LLM message...")
        try:
            from core.persona_manager import get_persona_manager

            persona_manager = get_persona_manager()
            if persona_manager:
                persona_manager.process_llm_message_for_emotions(text)
                log_info("[message_chain] ✅ Emotion processing completed successfully")

                # Check if there were invalid emotions - trigger corrector if so
                corrector_msg = persona_manager.get_emotion_validation_corrector()
                if corrector_msg:
                    log_warning(
                        "[message_chain] 🚨 Invalid emotions detected - triggering corrector"
                    )
                    # Trigger corrector with emotion validation message
                    from core.action_parser import run_action

                    try:
                        # Build corrector action
                        corrector_action = {
                            "type": "send_corrector_message",
                            "payload": {
                                "correction_type": "invalid_emotions",
                                "message": corrector_msg,
                                "interface_path": ctx.get("interface_path"),
                                "chat_id": ctx.get("chat_id"),
                            },
                        }

                        # Try to send corrector message
                        asyncio.create_task(run_action(corrector_action, message))
                        log_info(
                            "[message_chain] ✓ Corrector action scheduled for invalid emotions"
                        )
                    except Exception as ce:
                        log_warning(f"[message_chain] Could not send corrector: {ce}")
            else:
                log_warning("[message_chain] ⚠️ Persona manager not available")
        except Exception as e:
            log_error(f"[message_chain] ❌ Error processing LLM emotions: {e}")
            import traceback

            log_error(f"[message_chain] Traceback: {traceback.format_exc()}")

    log_info("[message_chain] 📋 Starting action extraction loop...")
    # Retry/tried set to avoid loops
    tried_texts = set()
    attempt = 0
    max_retries = ctx.get("max_retries", int(CORRECTOR_RETRIES))

    # Check for action result delivery context - these responses should have minimal retries
    # to prevent cascading loops when processing action outputs (e.g., memory_search results)
    is_action_result = False
    try:
        system_message = ctx.get("system_message", {})
        if isinstance(system_message, dict):
            is_action_result = system_message.get("is_action_result_delivery", False)
            custom_max = system_message.get("max_correction_attempts")
            if custom_max is not None and isinstance(custom_max, int):
                max_retries = min(max_retries, custom_max)
                log_info(
                    f"[message_chain] Action result delivery detected - limiting retries to {max_retries}"
                )
    except Exception:
        pass

    while True:
        log_info(
            f"[message_chain] 🔄 LOOP: attempt={attempt} source={source} chat={getattr(message, 'chat_id', None)} text_len={len(text) if text else 0}"
        )

        # Quick JSON extraction with metadata to detect corruption
        parsed = None
        metadata = {}
        try:
            log_info("[message_chain] Attempting to extract JSON from text...")
            parsed, metadata = extract_json_from_text(text, return_metadata=True)
            log_info(
                f"[message_chain] JSON extraction completed: parsed={parsed is not None} recovered={metadata.get('recovered')}"
            )
        except Exception as e:
            log_error(f"[message_chain] extract_json EXCEPTION: {e}")
            import traceback

            log_error(f"[message_chain] Traceback: {traceback.format_exc()}")

        # Check if JSON was recovered from corruption - may still have valid actions
        if parsed is not None and metadata.get("recovered"):
            log_warning(
                f"[message_chain] JSON recovered from corruption (errors: {metadata.get('error_count', 0)}, "
                f"unparsed: {len(metadata.get('unparsed_content', ''))} chars) - will execute valid actions and correct failures"
            )
            # Don't set parsed = None here - try to execute valid actions first

        if parsed is not None:
            # System messages are produced by the core/system and should NEVER be processed
            # This prevents loops caused by system messages being re-evaluated
            if isinstance(parsed, dict) and "system_message" in parsed:
                sm = parsed.get("system_message") or {}
                sm_type = sm.get("type") if isinstance(sm, dict) else None
                log_info(
                    f"[message_chain] Blocking system_message type={sm_type} (system-origin payload) - system messages must not enter the processing loop"
                )
                return BLOCKED

            # Build actions list
            if isinstance(parsed, dict) and "actions" in parsed:
                actions = (
                    parsed["actions"] if isinstance(parsed["actions"], list) else None
                )
                if actions is None:
                    log_warning(
                        "[message_chain] actions field must be a list - triggering corrector"
                    )
                    # Don't return here - let corrector fix it
                    parsed = None  # Force correction path
            elif isinstance(parsed, list):
                actions = parsed
            elif isinstance(parsed, dict) and "type" in parsed:
                actions = [parsed]
            elif isinstance(parsed, dict) and "action" in parsed:
                # Normalize Gemini-style {"action": "...", "action_input"|"content": "..."} to standard format
                # This handles LLMs that output the older single-action format
                # Gemini sometimes uses "action_input", sometimes "content", sometimes "text"
                action_type = parsed.get("action")
                action_input = (
                    parsed.get("action_input")
                    or parsed.get("content")
                    or parsed.get("text")
                    or parsed.get("message")
                )
                log_info(
                    f"[message_chain] 🔄 Normalizing Gemini-style action format: {action_type}"
                )
                # Convert action_input to payload with 'text' key if it's a string
                if isinstance(action_input, str):
                    normalized_action = {
                        "type": action_type,
                        "payload": {"text": action_input},
                    }
                elif isinstance(action_input, dict):
                    normalized_action = {"type": action_type, "payload": action_input}
                else:
                    # Fallback: collect any remaining keys as payload (excluding 'action')
                    fallback_payload = {
                        k: v for k, v in parsed.items() if k != "action"
                    }
                    normalized_action = {
                        "type": action_type,
                        "payload": fallback_payload,
                    }
                    log_warning(
                        f"[message_chain] ⚠️ No recognized text field in Gemini action, using fallback payload: {list(fallback_payload.keys())}"
                    )
                actions = [normalized_action]
            else:
                log_warning(
                    f"[message_chain] Unrecognized JSON structure: {parsed} - triggering corrector"
                )
                # Don't return here - let corrector fix it
                parsed = None  # Force correction path

            # Normalize any 'message_unknown' or other fabricated message types
            # to the correct interface-specific action type before execution
            if actions:
                # --- New: treat unregistered top-level JSON keys as invalid actions (registry-driven) ---
                # Some LLMs return a response object like:
                # {"actions": [...], "message": "...", "feelings": {...}}
                # We already allow certain metadata keys (e.g. "feelings") via the validation registry.
                # Any other top-level key is treated as an invalid action type so the corrector
                # can regenerate the response using only registered actions.
                is_from_llm = source == "llm" or getattr(message, "from_llm", False)
                if is_from_llm and isinstance(parsed, dict):
                    try:
                        from core.validation_registry import get_validation_registry

                        allowed_metadata = (
                            get_validation_registry().get_response_metadata_keys() or set()
                        )
                        extra_keys = [
                            k
                            for k in parsed.keys()
                            if k != "actions" and k not in allowed_metadata
                        ]
                        if extra_keys:
                            synthetic_actions = []
                            for key in extra_keys:
                                value = parsed.get(key)
                                payload = (
                                    value
                                    if isinstance(value, dict)
                                    else {"value": value}
                                )
                                synthetic_actions.append({"type": key, "payload": payload})

                            actions.extend(synthetic_actions)
                            log_info(
                                f"[message_chain] Added {len(synthetic_actions)} synthetic action(s) for unregistered top-level key(s): {', '.join(extra_keys)}"
                            )
                    except Exception as e:
                        log_debug(
                            f"[message_chain] Failed to process top-level metadata keys for correction: {e}"
                        )

                ctx_interface_path = ctx.get("interface_path") if ctx else None
                actions = _normalize_message_unknown(actions, ctx_interface_path)
                # Auto-inject interface_path into message actions that are missing it
                # This prevents validation failures and avoids costly LLM correction calls
                actions = _auto_inject_interface_path(actions, ctx_interface_path)

                # --- New: Validate action types early and trigger corrector for unsupported types ---
                try:
                    from core.action_parser import get_supported_action_types

                    supported_action_types = get_supported_action_types() or set()
                except Exception as e:
                    log_warning(f"[message_chain] Could not load supported action types: {e}")
                    supported_action_types = set()

                # Only enforce this for LLM-originated responses
                if is_from_llm and isinstance(actions, list):
                    unsupported = []
                    for idx, act in enumerate(actions):
                        if not isinstance(act, dict):
                            continue
                        atype = act.get("type") or act.get("action")
                        if not atype or atype not in supported_action_types:
                            unsupported.append(
                                {
                                    "index": idx,
                                    "action": act,
                                    "errors": [f"Unsupported type '{atype}' - no plugin or interface found to handle it"],
                                }
                            )

                    if unsupported:
                        log_warning(
                            f"[message_chain] 🚨 Detected unsupported action types from LLM: {[u['action'].get('type') or u['action'].get('action') for u in unsupported]} - requesting correction"
                        )
                        # Attach correction context and force correction path
                        correction_context = {
                            "successful_actions": [],
                            "failed_actions": unsupported,
                            "had_json_errors": False,
                            "original_text": text,
                        }
                        try:
                            if hasattr(message, "__dict__"):
                                message.correction_context = correction_context
                        except Exception:
                            pass

                        parsed = None  # Trigger the corrector loop below

            # Synthera Emotion Forwarding: copy dominant feeling into tts_speak payload
            if (
                parsed is not None
                and isinstance(parsed, dict)
                and "feelings" in parsed
                and actions
            ):
                try:
                    feelings = parsed.get("feelings")
                    if isinstance(feelings, dict) and feelings:
                        valid_feelings = {
                            k: float(v)
                            for k, v in feelings.items()
                            if isinstance(v, (int, float))
                        }
                        if valid_feelings:
                            dominant_emotion = max(
                                valid_feelings, key=valid_feelings.get
                            )
                            if valid_feelings[dominant_emotion] > 0:
                                log_debug(
                                    f"[message_chain] 🎭 Found dominant emotion in metadata: {dominant_emotion} ({valid_feelings[dominant_emotion]})"
                                )
                                for action in actions:
                                    atype = action.get("type") or action.get("action")
                                    if atype == "tts_speak":
                                        payload = action.get("payload")
                                        if isinstance(
                                            payload, dict
                                        ) and not payload.get("emotion"):
                                            payload["emotion"] = dominant_emotion
                                            log_info(
                                                f"[message_chain] 💉 Auto-injected emotion '{dominant_emotion}' into tts_speak payload"
                                            )
                except Exception as e:
                    log_warning(f"[message_chain] Failed to auto-forward emotions: {e}")

            # Only execute actions if we have valid ones
            if parsed is not None:
                # Note: LLM decides freely whether to respond to user or not
                # If no message_telegram_bot action is included, user simply receives nothing
                # Log for debugging purposes
                if source == "llm" or getattr(message, "from_llm", False):
                    has_user_response = False
                    has_tts = False
                    user_message_action = None
                    # Determine current set of message action types from config (dynamic)
                    current_message_action_types = []
                    try:
                        from core.config_manager import config_registry

                        MESSAGE_ACTION_TYPES = config_registry.get_var(
                            "MESSAGE_ACTION_TYPES",
                            [],
                            label="Message action types",
                            description="List of action types considered as outbound user messages.",
                            group="core",
                            component="message_chain",
                        )
                        current_message_action_types = (
                            list(MESSAGE_ACTION_TYPES.value)
                            if hasattr(MESSAGE_ACTION_TYPES, "value")
                            else list(MESSAGE_ACTION_TYPES)
                        )
                    except Exception:
                        current_message_action_types = []

                    # If not configured, infer from available action schemas
                    if not current_message_action_types:
                        try:
                            from core.core_initializer import core_initializer

                            available_actions = core_initializer.actions_block.get(
                                "available_actions", {}
                            )
                            current_message_action_types = [
                                action_type
                                for action_type in available_actions.keys()
                                if isinstance(action_type, str)
                                and action_type.startswith("message_")
                            ]
                        except Exception:
                            current_message_action_types = []

                    if isinstance(actions, list):
                        # ========================================
                        # STRIP TTS FROM AUTONOMOUS MESSAGES
                        # ========================================
                        # Check if any message action is autonomous (Grillo outreach, dreams, etc.)
                        # If so, remove any tts_speak actions - they shouldn't be spoken
                        has_autonomous_message = False
                        for action in actions:
                            if isinstance(action, dict):
                                action_meta = action.get("meta", {})
                                action_name = action.get("action") or action.get("type")
                                # Check if this is an autonomous message action
                                if action_meta.get("autonomous", False) is True:
                                    if action_name and action_name.startswith(
                                        "message_"
                                    ):
                                        has_autonomous_message = True
                                        break

                        if has_autonomous_message:
                            # Remove any tts_speak actions from autonomous message responses
                            tts_to_remove = []
                            for i, action in enumerate(actions):
                                if isinstance(action, dict):
                                    action_name = action.get("action") or action.get(
                                        "type"
                                    )
                                    if action_name == "tts_speak":
                                        tts_to_remove.append(i)

                            if tts_to_remove:
                                for i in reversed(tts_to_remove):
                                    removed = actions.pop(i)
                                    log_debug(
                                        f"[message_chain] 🔇 Stripped TTS from autonomous message: {removed.get('payload', {}).get('text', '')[:40]}..."
                                    )

                        for action in actions:
                            # Support both 'action' and 'type' keys
                            action_name = None
                            if isinstance(action, dict):
                                action_name = action.get("action") or action.get("type")
                            if action_name == "tts_speak":
                                has_tts = True
                            if action_name in current_message_action_types:
                                has_user_response = True
                                if not user_message_action:
                                    user_message_action = action
                                # break

                    # Auto-inject TTS if there's a user response but no tts_speak
                    # Only for actual user-facing interfaces (not internal like grillo)
                    if has_user_response and not has_tts and user_message_action:
                        # Check if this is for a user-facing interface
                        user_facing_interfaces = [
                            "discord_bot",
                            "telegram_bot",
                            "synth_webui",
                            "matrix_chat",
                            "ollama_serve",
                        ]
                        interface_path = ctx.get("interface_path") or ""
                        chat_id = ctx.get("chat_id")

                        # interface_path must have a chat_id suffix to be user-facing
                        # e.g., "telegram_bot/5208932647" not just "telegram_bot"
                        is_user_facing = interface_path and any(
                            interface_path.startswith(f"{iface}/")
                            for iface in user_facing_interfaces
                        )

                        # Check if this is an internal/system message
                        # chat_id == -1 indicates internal system messages (Grillo beats, etc.)
                        is_internal_chat = (
                            chat_id == -1 or chat_id == "-1" or str(chat_id) == "-1"
                        )

                        # Check if this is a Grillo internal beat (not outreach)
                        # Only internal Grillo beats (self_reflection, curiosity, etc.) skip TTS
                        # Outreach beats ARE user-facing and SHOULD get TTS
                        is_grillo_internal = ctx.get("grillo_beat", False) and ctx.get(
                            "beat_type"
                        ) not in ("outreach", None)

                        # Check for autonomous messages (Grillo outreach, dreams, etc.)
                        # These are system-initiated, not user-response, so they shouldn't get TTS
                        action_meta = (
                            user_message_action.get("meta", {})
                            if isinstance(user_message_action, dict)
                            else {}
                        )
                        is_autonomous = action_meta.get("autonomous", False) is True

                        # Check for system startup/internal message patterns
                        payload = (
                            user_message_action.get("payload", {})
                            if isinstance(user_message_action, dict)
                            else {}
                        )
                        text_to_speak = (
                            payload.get("text")
                            or payload.get("content")
                            or payload.get("message")
                            or ""
                        )
                        # Patterns that indicate internal/system messages that shouldn't get TTS
                        internal_message_patterns = [
                            "Synthetic Heart AI online",
                            "Action schema and system instructions",
                            "Analysis of recent memory logs",
                            "I have completed the analysis of memory logs",
                            "I have successfully processed",
                            "operational instruction",
                            "memory consolidation",
                            "tag elaboration",
                            "system initialization",
                            "configuration updated",
                        ]
                        is_system_message = any(
                            pattern.lower() in text_to_speak.lower()
                            for pattern in internal_message_patterns
                        )

                        # Check if TTS was already executed in a previous correction attempt
                        # This prevents double-TTS when correction flow runs
                        tts_already_executed = False
                        correction_ctx = getattr(message, "correction_context", None)
                        if correction_ctx:
                            # Check if successful_actions (list) or successful_types (list) contains tts_speak
                            successful_actions = correction_ctx.get(
                                "successful_actions", []
                            )
                            if isinstance(successful_actions, list):
                                for action in successful_actions:
                                    if (
                                        isinstance(action, dict)
                                        and action.get("type") == "tts_speak"
                                    ):
                                        tts_already_executed = True
                                        break
                            # Also check successful_types which is populated by action_parser
                            successful_types = correction_ctx.get(
                                "successful_types", []
                            )
                            if (
                                isinstance(successful_types, list)
                                and "tts_speak" in successful_types
                            ):
                                tts_already_executed = True

                        # Determine if we should skip TTS
                        # Skip for: internal grillo beats, internal chats, system messages, autonomous messages, AND already-executed TTS
                        should_skip_tts = (
                            is_grillo_internal
                            or is_internal_chat
                            or is_system_message
                            or is_autonomous
                            or tts_already_executed
                        )

                        # Additional check: if TTS endpoints are not configured, skip auto-inject
                        try:
                            from core.config_manager import config_registry

                            tts_raw = config_registry.get_value(
                                "TTS_ENDPOINTS",
                                "",
                                value_type=str,
                                group="plugins",
                                component="tts_lipsync",
                            )
                            tts_enabled = config_registry.get_value(
                                "TTS_ENABLED",
                                False,
                                value_type=bool,
                                group="plugins",
                                component="tts_lipsync",
                            )

                            if not tts_raw:
                                should_skip_tts = True
                                log_debug(
                                    "[message_chain] Skipping TTS auto-inject because TTS_ENDPOINTS is not configured"
                                )

                            # If TTS is explicitly disabled via WebUI, skip injection
                            if not bool(tts_enabled):
                                should_skip_tts = True
                                log_debug(
                                    "[message_chain] Skipping TTS auto-inject because TTS_ENABLED is False"
                                )
                        except Exception as e:
                            log_debug(f"[message_chain] Error checking TTS config: {e}")

                        if should_skip_tts:
                            skip_reason = []
                            if is_grillo_internal:
                                skip_reason.append(
                                    f"grillo_beat={ctx.get('beat_type')}"
                                )
                            if is_internal_chat:
                                skip_reason.append(f"internal_chat_id={chat_id}")
                            if is_system_message:
                                skip_reason.append("system_message_pattern")
                            if is_autonomous:
                                skip_reason.append("autonomous_message")
                            if tts_already_executed:
                                skip_reason.append("tts_already_executed_in_correction")
                            if not tts_raw:
                                skip_reason.append("tts_not_configured")
                            log_debug(
                                f"[message_chain] Skipping TTS auto-inject: {', '.join(skip_reason)}"
                            )
                        elif is_user_facing:
                            if (
                                text_to_speak
                                and isinstance(text_to_speak, str)
                                and len(text_to_speak.strip()) > 0
                            ):
                                log_info(
                                    f"[message_chain] 🗣️ Auto-injecting 'tts_speak' action for message: {text_to_speak[:30]}..."
                                )
                                tts_action = {
                                    "type": "tts_speak",
                                    "payload": {
                                        "text": text_to_speak,
                                        "emotion": payload.get("emotion")
                                        if isinstance(payload, dict)
                                        else None,
                                    },
                                }
                                actions.append(tts_action)
                                # Update has_tts flag since we just added it
                                has_tts = True
                        else:
                            log_debug(
                                f"[message_chain] Skipping TTS auto-inject for non-user-facing interface: {interface_path}"
                            )

                    if not has_user_response:
                        log_debug(
                            "[message_chain] LLM chose not to send user message (diary/internal only)"
                        )
                    else:
                        log_debug("[message_chain] LLM will send message to user")

                        # CRITICAL FIX: Merge text into TTS when both are present
                        # This ensures text+audio are sent in the SAME message
                        # SAFETY: Only do this if TTS plugin is actually loaded
                        if has_user_response and has_tts and isinstance(actions, list):
                            # Check if TTS plugin is loaded and active
                            tts_plugin_available = False
                            try:
                                from core.core_initializer import core_initializer

                                available_actions = core_initializer.actions_block.get(
                                    "available_actions", {}
                                )
                                # TTS plugin is available if tts_speak is in available actions
                                tts_plugin_available = "tts_speak" in available_actions
                                log_debug(
                                    f"[message_chain] TTS plugin availability check: {tts_plugin_available}"
                                )
                            except Exception as e:
                                log_warning(
                                    f"[message_chain] Could not verify TTS plugin availability: {e}"
                                )
                                tts_plugin_available = False

                            # Only merge and remove message actions if TTS plugin is confirmed available
                            if tts_plugin_available:
                                # Find message and TTS actions
                                message_actions_to_remove = []
                                tts_actions = []

                                for idx, action in enumerate(actions):
                                    if not isinstance(action, dict):
                                        continue
                                    action_type = action.get("action") or action.get(
                                        "type"
                                    )

                                    if action_type == "tts_speak":
                                        tts_actions.append(action)
                                    elif action_type in current_message_action_types:
                                        # Check if this message action has the same interface_path as TTS
                                        msg_payload = action.get("payload", {})
                                        msg_interface_path = msg_payload.get(
                                            "interface_path"
                                        )
                                        msg_text = msg_payload.get("text")

                                        if msg_text and msg_interface_path:
                                            message_actions_to_remove.append(
                                                (idx, msg_text, msg_interface_path)
                                            )

                                # Merge text into TTS payloads that match interface_path
                                if message_actions_to_remove and tts_actions:
                                    log_info(
                                        f"[message_chain] 🔗 Merging {len(message_actions_to_remove)} message action(s) into TTS to send text+audio together"
                                    )

                                    for tts_action in tts_actions:
                                        tts_payload = tts_action.get("payload", {})
                                        if not isinstance(tts_payload, dict):
                                            continue

                                        # Find matching message text for this TTS (same or compatible interface)
                                        for (
                                            idx,
                                            msg_text,
                                            msg_ipath,
                                        ) in message_actions_to_remove:
                                            # Merge the text into TTS payload
                                            if "__merged_text" not in tts_payload:
                                                tts_payload["__merged_text"] = msg_text
                                                log_info(
                                                    f"[message_chain] ✅ Merged text into tts_speak: '{msg_text[:50]}...'"
                                                )
                                                break

                                    # Remove the standalone message actions (they'll be sent with TTS)
                                    # Remove in reverse order to avoid index shifting
                                    for idx, _, _ in sorted(
                                        message_actions_to_remove, reverse=True
                                    ):
                                        removed_action = actions.pop(idx)
                                        log_info(
                                            f"[message_chain] 🗑️ Removed duplicate message action (will be sent with TTS): {removed_action.get('type')}"
                                        )
                            else:
                                log_info(
                                    "[message_chain] ⚠️ TTS plugin not available - keeping separate message and TTS actions (text sent separately)"
                                )

                # Execute actions regardless of whether response is included
                if parsed is not None:
                    try:
                        log_debug(
                            f"[message_chain] EXECUTING ACTIONS: count={len(actions) if actions else 0}, interface_path={ctx.get('interface_path')}, chat_id={ctx.get('chat_id')}, action_types={[a.get('type') or a.get('action') for a in (actions or []) if isinstance(a, dict)]}"
                        )
                        result = await run_actions(actions, ctx, bot, message)
                        processed = result.get("processed", [])
                        failed = result.get("failed_actions", [])
                        errors = result.get("errors", [])

                        log_info(
                            f"[message_chain] Actions result: {len(processed)} successful, {len(failed)} failed"
                        )

                        # If we had corruption recovery or validation failures, check if correction is needed
                        # But SKIP correction for "unfixable" errors (policy restrictions like whitelist/suggest mode)
                        # These can't be fixed by the LLM - they're system configuration issues
                        fixable_failures = [
                            f for f in failed if not f.get("unfixable", False)
                        ]
                        unfixable_failures = [
                            f for f in failed if f.get("unfixable", False)
                        ]

                        if unfixable_failures:
                            unfixable_types = [
                                f.get("action", {}).get("type", "?")
                                for f in unfixable_failures
                            ]
                            log_info(
                                f"[message_chain] Skipping correction for {len(unfixable_failures)} unfixable policy errors: {unfixable_types}"
                            )

                        needs_correction = len(fixable_failures) > 0 or metadata.get(
                            "recovered", False
                        )

                        # Attach last action result to message/context so downstream hooks
                        # (e.g. Grillo action checker) can inspect what happened.
                        last_action_result = {
                            "processed": processed,
                            "failed": failed,
                            "errors": errors,
                        }
                        try:
                            ctx["last_action_result"] = last_action_result
                            if hasattr(message, "__dict__"):
                                message.last_action_result = last_action_result
                        except Exception:
                            pass

                        if needs_correction and (
                            source == "llm" or getattr(message, "from_llm", False)
                        ):
                            # Some actions failed or JSON was corrupted - request selective correction
                            log_warning(
                                f"[message_chain] {len(failed)} actions failed, requesting correction for missing/invalid actions"
                            )

                            # Build correction context with info about what succeeded and what failed
                            correction_context = {
                                "successful_actions": processed,
                                "failed_actions": failed,
                                "errors": errors,
                                "had_json_errors": metadata.get("recovered", False),
                                "original_text": text,
                            }

                            # Store this in the message for the corrector to use
                            if hasattr(message, "__dict__"):
                                message.correction_context = correction_context

                            # Set parsed = None to trigger correction path
                            # But keep the successful actions already executed
                            if len(failed) > 0:
                                parsed = (
                                    None  # This will trigger the correction loop below
                                )
                            else:
                                # All actions succeeded despite recovery - we're done
                                log_info(
                                    "[message_chain] All actions executed successfully despite JSON recovery"
                                )
                                return ACTIONS_EXECUTED
                        else:
                            # All actions succeeded
                            log_info(
                                "[message_chain] Actions executed successfully - loop interrupted"
                            )
                            return ACTIONS_EXECUTED

                    except Exception as e:
                        log_warning(f"[message_chain] Failed to run actions: {e}")
                        # If action execution fails, don't continue with correction loop
                        # This prevents cascading failures and loops
                        return BLOCKED

        # Not parsed. If it's from LLM, always attempt correction regardless of braces
        # If it's non-LLM source, don't attempt correction
        # IMPORTANT: Only attempt correction for LLM messages that failed JSON parsing
        # Non-LLM messages and messages that don't require correction should be blocked
        if source != "llm" and not getattr(message, "from_llm", False):
            log_debug("[message_chain] Non-LLM source; no correction needed")
            return BLOCKED

        # Additional check: if this is already a system error message from corrector, don't re-correct
        if "system_message" in (text or "") and "error" in (text or ""):
            log_debug(
                "[message_chain] Detected system error message from corrector; preventing re-correction loop"
            )
            return BLOCKED

        attempt += 1
        if attempt > max_retries:
            failure_reason = (
                f"Exhausted {max_retries} correction attempts for invalid JSON"
            )
            log_warning(f"[message_chain] {failure_reason}; sending fallback message")
            await send_llm_fallback_message(bot, message, failure_reason, context=ctx)
            return LLM_FAILED

        if text in tried_texts:
            failure_reason = "Correction loop detected - same text repeated"
            log_warning(f"[message_chain] {failure_reason}; sending fallback message")
            await send_llm_fallback_message(bot, message, failure_reason, context=ctx)
            return LLM_FAILED

        tried_texts.add(text)

        # Request correction from LLM via transport-layer middleware
        try:
            log_info(
                f"[message_chain] Calling corrector middleware for attempt={attempt}..."
            )
            log_debug(
                f"[message_chain] CORRECTOR CONTEXT: interface_path={ctx.get('interface_path')}, chat_id={getattr(message, 'chat_id', None)}, text_preview={text[:200] if text else ''}"
            )
            corrected = await run_corrector_middleware(
                text,
                bot=bot,
                context=ctx,
                chat_id=getattr(message, "chat_id", None),
                thread_id=getattr(message, "thread_id", None),
            )
            log_info(
                f"[message_chain] Corrector returned: corrected={corrected is not None} len={len(corrected) if corrected else 0}"
            )
        except Exception as e:
            failure_reason = f"Corrector middleware exception: {str(e)}"
            log_error(f"[message_chain] {failure_reason}")
            import traceback

            log_error(f"[message_chain] Traceback: {traceback.format_exc()}")
            await send_llm_fallback_message(bot, message, failure_reason, context=ctx)
            return LLM_FAILED

        if not corrected:
            log_debug("[message_chain] Corrector returned no correction this attempt")
            # Check if we're approaching max retries to avoid infinite waiting
            if attempt >= max_retries - 1:
                failure_reason = (
                    f"Corrector returned no correction after {attempt} attempts"
                )
                log_warning(
                    f"[message_chain] {failure_reason}; sending fallback message"
                )
                await send_llm_fallback_message(
                    bot, message, failure_reason, context=ctx
                )
                return LLM_FAILED
            # On no-correction, loop and let retry counter enforce blocking
            await asyncio.sleep(0.5)
            continue

        # Accept corrected text and treat it as LLM-origin for next iteration
        log_debug("[message_chain] Received corrected text from LLM; retrying parse")
        text = corrected
        source = "llm"
        ctx["original_text"] = text  # Track in context instead of on message object
        ctx["from_llm"] = True  # Track in context instead of on message object
        # loop continues


# Backwards-compatible alias
handle_message = handle_incoming_message
