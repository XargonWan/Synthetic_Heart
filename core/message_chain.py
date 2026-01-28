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
import os
from types import SimpleNamespace
from typing import Any, Dict, Optional

from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.config_manager import config_registry

# Result constants
ACTIONS_EXECUTED = 'ACTIONS_EXECUTED'
FORWARD_AS_TEXT = 'FORWARD_AS_TEXT'
BLOCKED = 'BLOCKED'
LLM_FAILED = 'LLM_FAILED'

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
    if hasattr(fallback, 'get_value'):
        fallback = fallback.get_value()
    return str(fallback)

async def send_llm_fallback_message(bot, message: SimpleNamespace, failure_reason: str, context: dict = None) -> str:
    """Send fallback message when LLM fails and log the failure reason.
    
    Args:
        bot: The bot instance
        message: The message object (may have interface_path attribute)
        failure_reason: Description of why the LLM failed
        context: Optional context dict that may contain interface_path
    """
    fallback_text = get_failed_message_text()
    # Ensure fallback_text is a string (ConfigVar might be returned)
    if hasattr(fallback_text, 'get_value'):
        fallback_text = fallback_text.get_value()
    fallback_text = str(fallback_text)
    chat_id = getattr(message, 'chat_id', None)
    # Preserve thread_id when available so the fallback message is routed to the
    # same message thread and not defaulted to 0
    thread_id = getattr(message, 'thread_id', None)
    if not thread_id and context:
        thread_id = context.get('thread_id')
    
    # Extract interface_path from message or context - CRITICAL for routing to correct interface
    interface_path = getattr(message, 'interface_path', None)
    if not interface_path and context:
        interface_path = context.get('interface_path')
    
    # Log detailed error
    log_error(f"[message_chain] LLM FAILURE - Chat: {chat_id}, Interface: {interface_path}, Thread: {thread_id}, Reason: {failure_reason}")
    log_error(f"[message_chain] Sending fallback message: '{fallback_text}'")
    
    # Send fallback message through transport layer
    try:
        from core.transport_layer import universal_send
        # Get the send_message method from the bot (interface)
        if bot and hasattr(bot, 'send_message'):
            await universal_send(
                bot.send_message,
                chat_id,
                text=fallback_text,
                interface_path=interface_path,
                thread_id=thread_id,
                is_llm_response=True  # Mark as LLM response so interface handles normally
            )
        else:
            log_warning(f"[message_chain] Bot does not have send_message method, cannot send fallback")
        log_debug(f"[message_chain] Fallback message sent to chat {chat_id} via interface_path {interface_path} thread_id={thread_id}")
        return fallback_text
    except Exception as e:
        log_error(f"[message_chain] Failed to send fallback message: {e}")
        return fallback_text


async def handle_incoming_message(bot, message: Optional[SimpleNamespace], text: str, *, source: str = "interface", context: Optional[Dict[str, Any]] = None, **kwargs):
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

    log_info(f"[message_chain] 🔄 ENTRY: source={source} text_len={len(text) if text else 0} chat_id={kwargs.get('chat_id', getattr(message, 'chat_id', 'unknown')) if message else kwargs.get('chat_id')}")
    
    # Trace LLM→INTERFACE flow
    if source == "llm":
        log_info(f"[message_chain] 📥 LLM→INTERFACE: Processing LLM response via message_chain (will apply llm_to_interface transport standards)")

    if message is None:
        message = SimpleNamespace()
        message.chat_id = kwargs.get('chat_id')
        message.text = ""
        message.interface_path = kwargs.get('interface_path')
        message.date = datetime.utcnow()

    # Default context
    ctx = context or {}
    ctx['message'] = message
    ctx['original_text'] = text  # Track original text in context, not on message (for consistency with immutable Telegram Message objects)
    
    # Mark LLM-origin in context (not on message object, as Telegram Message objects are immutable)
    is_from_llm = True if source == 'llm' else ctx.get('from_llm', False)
    ctx['from_llm'] = is_from_llm
    
    # Also set on message object if possible (for corrector_orchestrator and action_parser detection)
    try:
        if hasattr(message, '__dict__') or isinstance(message, type({})):
            message.from_llm = is_from_llm
    except (AttributeError, TypeError):
        pass  # Message object is immutable (Telegram Message); use ctx instead
    
    # Preserve chat_id and interface_path in context to avoid losing them during processing
    if hasattr(message, 'chat_id'):
        ctx['chat_id'] = message.chat_id
    if hasattr(message, 'interface_path'):
        ctx['interface_path'] = message.interface_path

    # Process LLM messages for emotional state updates
    if ctx.get('from_llm', False) or source == 'llm':
        log_info(f"[message_chain] 🎭 Starting emotion processing for LLM message...")
        try:
            from core.persona_manager import get_persona_manager
            persona_manager = get_persona_manager()
            if persona_manager:
                persona_manager.process_llm_message_for_emotions(text)
                log_info(f"[message_chain] ✅ Emotion processing completed successfully")
                
                # Check if there were invalid emotions - trigger corrector if so
                corrector_msg = persona_manager.get_emotion_validation_corrector()
                if corrector_msg:
                    log_warning(f"[message_chain] 🚨 Invalid emotions detected - triggering corrector")
                    # Trigger corrector with emotion validation message
                    from core.corrector_utils import get_action_description_for_corrector
                    from core.action_parser import run_action
                    
                    try:
                        # Build corrector action
                        corrector_action = {
                            "type": "send_corrector_message",
                            "payload": {
                                "correction_type": "invalid_emotions",
                                "message": corrector_msg,
                                "interface_path": ctx.get('interface_path'),
                                "chat_id": ctx.get('chat_id')
                            }
                        }
                        
                        # Try to send corrector message
                        asyncio.create_task(run_action(corrector_action, message))
                        log_info(f"[message_chain] ✓ Corrector action scheduled for invalid emotions")
                    except Exception as ce:
                        log_warning(f"[message_chain] Could not send corrector: {ce}")
            else:
                log_warning(f"[message_chain] ⚠️ Persona manager not available")
        except Exception as e:
            log_error(f"[message_chain] ❌ Error processing LLM emotions: {e}")
            import traceback
            log_error(f"[message_chain] Traceback: {traceback.format_exc()}")

    log_info(f"[message_chain] 📋 Starting action extraction loop...")
    # Retry/tried set to avoid loops
    tried_texts = set()
    attempt = 0
    max_retries = ctx.get('max_retries', int(CORRECTOR_RETRIES))

    while True:
        log_info(
            f"[message_chain] 🔄 LOOP: attempt={attempt} source={source} chat={getattr(message,'chat_id',None)} text_len={len(text) if text else 0}"
        )

        # Quick JSON extraction with metadata to detect corruption
        parsed = None
        metadata = {}
        try:
            log_info(f"[message_chain] Attempting to extract JSON from text...")
            parsed, metadata = extract_json_from_text(text, return_metadata=True)
            log_info(f"[message_chain] JSON extraction completed: parsed={parsed is not None} recovered={metadata.get('recovered')}")
        except Exception as e:
            log_error(f"[message_chain] extract_json EXCEPTION: {e}")
            import traceback
            log_error(f"[message_chain] Traceback: {traceback.format_exc()}")

        # Check if JSON was recovered from corruption - may still have valid actions
        if parsed is not None and metadata.get('recovered'):
            log_warning(
                f"[message_chain] JSON recovered from corruption (errors: {metadata.get('error_count', 0)}, "
                f"unparsed: {len(metadata.get('unparsed_content', ''))} chars) - will execute valid actions and correct failures"
            )
            # Don't set parsed = None here - try to execute valid actions first

        if parsed is not None:
            # System messages are produced by the core/system and should NEVER be processed
            # This prevents loops caused by system messages being re-evaluated
            if isinstance(parsed, dict) and 'system_message' in parsed:
                sm = parsed.get('system_message') or {}
                sm_type = sm.get('type') if isinstance(sm, dict) else None
                log_info(
                    f"[message_chain] Blocking system_message type={sm_type} (system-origin payload) - system messages must not enter the processing loop"
                )
                return BLOCKED

            # Build actions list
            if isinstance(parsed, dict) and 'actions' in parsed:
                actions = parsed['actions'] if isinstance(parsed['actions'], list) else None
                if actions is None:
                    log_warning('[message_chain] actions field must be a list - triggering corrector')
                    # Don't return here - let corrector fix it
                    parsed = None  # Force correction path
            elif isinstance(parsed, list):
                actions = parsed
            elif isinstance(parsed, dict) and 'type' in parsed:
                actions = [parsed]
            else:
                log_warning(f"[message_chain] Unrecognized JSON structure: {parsed} - triggering corrector")
                # Don't return here - let corrector fix it
                parsed = None  # Force correction path

            # Only execute actions if we have valid ones
            if parsed is not None:
                # Note: LLM decides freely whether to respond to user or not
                # If no message_telegram_bot action is included, user simply receives nothing
                # Log for debugging purposes
                if source == "llm" or getattr(message, "from_llm", False):
                    has_user_response = False
                    # Determine current set of message action types from config (dynamic)
                    try:
                        from core.config_manager import config_registry
                        MESSAGE_ACTION_TYPES = config_registry.get_var(
                            "MESSAGE_ACTION_TYPES",
                            ["message_telegram_bot", "message_discord_bot", "message_ollama_serve", "message_synth_webui"],
                            label="Message action types",
                            description="List of action types considered as outbound user messages.",
                            group="core",
                            component="message_chain",
                        )
                        current_message_action_types = list(MESSAGE_ACTION_TYPES.value) if hasattr(MESSAGE_ACTION_TYPES, 'value') else list(MESSAGE_ACTION_TYPES)
                    except Exception:
                        current_message_action_types = ["message_telegram_bot", "message_discord_bot", "message_ollama_serve", "message_synth_webui"]

                    if isinstance(actions, list):
                        for action in actions:
                            # Support both 'action' and 'type' keys
                            action_name = None
                            if isinstance(action, dict):
                                action_name = action.get('action') or action.get('type')
                            if action_name in current_message_action_types:
                                has_user_response = True
                                break

                    if not has_user_response:
                        log_debug('[message_chain] LLM chose not to send user message (diary/internal only)')
                    else:
                        log_debug('[message_chain] LLM will send message to user')
                
                # Execute actions regardless of whether response is included
                if parsed is not None:
                    try:
                        result = await run_actions(actions, ctx, bot, message)
                        processed = result.get('processed', [])
                        failed = result.get('failed_actions', [])
                        errors = result.get('errors', [])
                        
                        log_info(f'[message_chain] Actions result: {len(processed)} successful, {len(failed)} failed')
                        
                        # If we had corruption recovery or validation failures, check if correction is needed
                        needs_correction = len(failed) > 0 or metadata.get('recovered', False)
                        
                        # Attach last action result to message/context so downstream hooks
                        # (e.g. Grillo action checker) can inspect what happened.
                        last_action_result = {
                            'processed': processed,
                            'failed': failed,
                            'errors': errors
                        }
                        try:
                            ctx['last_action_result'] = last_action_result
                            if hasattr(message, '__dict__'):
                                message.last_action_result = last_action_result
                        except Exception:
                            pass

                        if needs_correction and (source == "llm" or getattr(message, "from_llm", False)):
                            # Some actions failed or JSON was corrupted - request selective correction
                            log_warning(f'[message_chain] {len(failed)} actions failed, requesting correction for missing/invalid actions')
                            
                            # Build correction context with info about what succeeded and what failed
                            correction_context = {
                                'successful_actions': processed,
                                'failed_actions': failed,
                                'errors': errors,
                                'had_json_errors': metadata.get('recovered', False),
                                'original_text': text
                            }
                            
                            # Store this in the message for the corrector to use
                            if hasattr(message, '__dict__'):
                                message.correction_context = correction_context
                            
                            # Set parsed = None to trigger correction path
                            # But keep the successful actions already executed
                            if len(failed) > 0:
                                parsed = None  # This will trigger the correction loop below
                            else:
                                # All actions succeeded despite recovery - we're done
                                log_info('[message_chain] All actions executed successfully despite JSON recovery')
                                return ACTIONS_EXECUTED
                        else:
                            # All actions succeeded
                            log_info('[message_chain] Actions executed successfully - loop interrupted')
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
        if "system_message" in (text or '') and "error" in (text or ''):
            log_debug("[message_chain] Detected system error message from corrector; preventing re-correction loop")
            return BLOCKED

        attempt += 1
        if attempt > max_retries:
            failure_reason = f"Exhausted {max_retries} correction attempts for invalid JSON"
            log_warning(f"[message_chain] {failure_reason}; sending fallback message")
            await send_llm_fallback_message(bot, message, failure_reason, context=ctx)
            return LLM_FAILED

        if text in tried_texts:
            failure_reason = "Correction loop detected - same text repeated"
            log_warning(f'[message_chain] {failure_reason}; sending fallback message')
            await send_llm_fallback_message(bot, message, failure_reason, context=ctx)
            return LLM_FAILED

        tried_texts.add(text)

        # Request correction from LLM via transport-layer middleware
        try:
            log_info(f"[message_chain] Calling corrector middleware for attempt={attempt}...")
            corrected = await run_corrector_middleware(text, bot=bot, context=ctx, chat_id=getattr(message, 'chat_id', None), thread_id=getattr(message, 'thread_id', None))
            log_info(f"[message_chain] Corrector returned: corrected={corrected is not None} len={len(corrected) if corrected else 0}")
        except Exception as e:
            failure_reason = f"Corrector middleware exception: {str(e)}"
            log_error(f"[message_chain] {failure_reason}")
            import traceback
            log_error(f"[message_chain] Traceback: {traceback.format_exc()}")
            await send_llm_fallback_message(bot, message, failure_reason, context=ctx)
            return LLM_FAILED

        if not corrected:
            log_debug('[message_chain] Corrector returned no correction this attempt')
            # Check if we're approaching max retries to avoid infinite waiting
            if attempt >= max_retries - 1:
                failure_reason = f"Corrector returned no correction after {attempt} attempts"
                log_warning(f"[message_chain] {failure_reason}; sending fallback message")
                await send_llm_fallback_message(bot, message, failure_reason, context=ctx)
                return LLM_FAILED
            # On no-correction, loop and let retry counter enforce blocking
            await asyncio.sleep(0.5)
            continue

        # Accept corrected text and treat it as LLM-origin for next iteration
        log_debug('[message_chain] Received corrected text from LLM; retrying parse')
        text = corrected
        source = 'llm'
        ctx['original_text'] = text  # Track in context instead of on message object
        ctx['from_llm'] = True  # Track in context instead of on message object
        # loop continues


# Backwards-compatible alias
handle_message = handle_incoming_message
