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
- LLM_FAILED -> LLM produced invalid output and all corrector attempts were exhausted

Note: the LLM must always reply with valid JSON actions. Plain text from the LLM
is treated as a correctable error; the corrector will request valid JSON format.
"""

import asyncio
from types import SimpleNamespace
from typing import Any, Dict, Optional, cast

from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.config_manager import config_registry
from core.beat_utils import is_outbound_beat

# Result constants
ACTIONS_EXECUTED = "ACTIONS_EXECUTED"
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
    2100,
    label="Response Timeout",
    description=(
        "Maximum time in seconds to wait for LLM responses before sending a "
        "fallback message. Must stay above LLM_GENERATION_TIMEOUT_SEC so a slow "
        "generation is not cut off by this outer guard."
    ),
    value_type=int,
    group="core",
    component="core",
)


def get_failed_message_text() -> str:
    """Get the fallback message when LLM fails."""
    fallback = FAILED_MESSAGE_TEXT
    # Ensure we return a string (ConfigVar might be returned)
    get_value = getattr(fallback, "get_value", None)
    if callable(get_value):
        fallback = get_value()
    return str(fallback)


# Map of interface prefixes to their correct message action types
_INTERFACE_TO_MESSAGE_ACTION: Dict[str, str] = {
    "telegram_bot": "message_telegram_bot",
    "discord_bot": "message_discord_bot",
    "synth_webui": "message_synth_webui",
    "matrix_chat": "message_matrix_chat",
    "ollama_serve": "message_ollama_serve",
    # radio_host: speaking on-air is the equivalent of "sending a message"
    "radio_host": "radio_speak",
}

_ACTION_TYPE_ALIASES: Dict[str, str] = {
    "diary": "create_personal_diary_entry",
    "diary_entry": "create_personal_diary_entry",
}

# Keys that are part of the action envelope and must NOT be swept into payload
# when gathering flat fields.
_ACTION_SYSTEM_KEYS = frozenset(
    {
        "type",
        "payload",
        "meta",
        # Alternative type/payload key names (already handled above, just exclude)
        "function",
        "name",
        "plugin",
        "action",
        "command",
        "method",
        "arguments",
        "parameters",
        "args",
        "schema",
        "input",
    }
)


def _normalize_message_payload_text(actions: list) -> list:
    """Promote legacy message payload text aliases to payload.text.

    LLMs occasionally emit message-like actions with keys such as ``body`` or
    ``content`` instead of the canonical ``text`` field required by validation.
    Normalize those aliases in-place before validation/correction so valid reply
    intents do not trigger an unnecessary corrective round-trip.

    Args:
        actions: List of action dicts to normalize.

    Returns:
        The same list with legacy text aliases copied into ``payload.text``.
    """
    if not actions:
        return actions

    for action in actions:
        if not isinstance(action, dict):
            continue

        action_type = action.get("type") or action.get("action")
        if not isinstance(action_type, str):
            continue
        if action_type not in (
            "message",
            "send_message",
        ) and not action_type.startswith("message_"):
            continue

        payload = action.get("payload")
        if not isinstance(payload, dict):
            continue

        existing_text = payload.get("text")
        if isinstance(existing_text, str) and existing_text.strip():
            continue

        for legacy_key in ("body", "content", "message", "value"):
            legacy_value = payload.get(legacy_key)
            if isinstance(legacy_value, str) and legacy_value.strip():
                payload["text"] = legacy_value
                log_debug(
                    f"[message_chain] Normalized payload.{legacy_key} -> payload.text for {action_type}"
                )
                break

    return actions


def _normalize_action_type_aliases(actions: list) -> list:
    """Rewrite legacy action aliases to their canonical registered names."""

    if not actions:
        return actions

    for action in actions:
        if not isinstance(action, dict):
            continue

        action_type = action.get("type") or action.get("action")
        if not isinstance(action_type, str):
            continue

        if action_type.startswith("default_api:"):
            action_type = action_type.split("default_api:", 1)[1]
            action["type"] = action_type
            if "action" in action:
                action["action"] = action_type

        canonical_type = _ACTION_TYPE_ALIASES.get(action_type)
        if not canonical_type or canonical_type == action_type:
            continue

        action["type"] = canonical_type
        log_debug(
            f"[message_chain] Normalized action type alias: {action_type} -> {canonical_type}"
        )

    return actions


def _normalize_diary_payload_fields(actions: list) -> list:
    """Promote legacy diary payload keys to canonical create_personal_diary_entry fields."""

    if not actions:
        return actions

    legacy_field_map = {
        "entry": "interaction_summary",
        "summary": "interaction_summary",
        "thought": "personal_thought",
    }

    diary_action: dict[str, Any] | None = None
    for action in actions:
        if not isinstance(action, dict):
            continue

        action_type = action.get("type") or action.get("action")
        if action_type != "create_personal_diary_entry":
            continue

        payload = action.get("payload")
        if isinstance(payload, dict):
            diary_action = action
            break

    normalized_actions = []

    for action in actions:
        if not isinstance(action, dict):
            normalized_actions.append(action)
            continue

        action_type = action.get("type") or action.get("action")
        if action_type == "thought":
            payload = action.get("payload")
            diary_payload = diary_action.get("payload") if diary_action else None
            if isinstance(payload, dict) and isinstance(diary_payload, dict):
                thought_value = payload.get("personal_thought") or payload.get(
                    "thought"
                )
                existing_thought = diary_payload.get("personal_thought")
                if isinstance(thought_value, str) and thought_value.strip():
                    if isinstance(existing_thought, str) and existing_thought.strip():
                        if thought_value.strip() not in existing_thought:
                            diary_payload["personal_thought"] = (
                                f"{existing_thought}\n\n{thought_value}"
                            )
                    else:
                        diary_payload["personal_thought"] = thought_value
                if not diary_payload.get("timestamp") and payload.get("timestamp"):
                    diary_payload["timestamp"] = payload["timestamp"]
                log_debug(
                    "[message_chain] Folded legacy thought action into create_personal_diary_entry"
                )
                continue

            normalized_actions.append(action)
            continue

        if action_type != "create_personal_diary_entry":
            normalized_actions.append(action)
            continue

        payload = action.get("payload")
        if not isinstance(payload, dict):
            normalized_actions.append(action)
            continue

        for legacy_field, canonical_field in legacy_field_map.items():
            canonical_value = payload.get(canonical_field)
            if isinstance(canonical_value, str) and canonical_value.strip():
                continue

            legacy_value = payload.get(legacy_field)
            if not isinstance(legacy_value, str) or not legacy_value.strip():
                continue

            payload[canonical_field] = legacy_value
            log_debug(
                "[message_chain] Normalized diary payload field: "
                f"{legacy_field} -> {canonical_field}"
            )

        normalized_actions.append(action)

    return normalized_actions


def _auto_inject_interface_path(actions: list, interface_path: Optional[str]) -> list:
    """Auto-inject missing interface_path into message actions.

    LLMs sometimes forget to include interface_path in message actions, causing
    validation failures. This function detects message actions missing interface_path
    and injects it from the context before validation runs.

    Args:
        actions: List of action dicts to process
        interface_path: The interface path from context (e.g., 'telegram_bot/5551234567')

    Returns:
        The same list with interface_path injected in-place where missing
    """
    if not actions or not interface_path:
        return actions

    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = (
            action.get("type")
            or action.get("action")
            or action.get("command")
            or action.get("method")
        )
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
        interface_path: The interface path (e.g., 'telegram_bot/5551234567')

    Returns:
        The same list with any 'message_unknown' types corrected in-place
    """
    if not actions or not interface_path:
        return actions

    # Extract interface prefix from path (e.g., 'telegram_bot' from 'telegram_bot/5551234567')
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
            and isinstance(action_type, str)
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
    bot,
    message: SimpleNamespace,
    failure_reason: str,
    context: dict[str, Any] | None = None,
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
    fallback_get_value = getattr(fallback_text, "get_value", None)
    if callable(fallback_get_value):
        fallback_text = fallback_get_value()
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

    async def _record_failure_event(
        stage: str,
        reason_text: str,
        *,
        failure_code: str | None = None,
        extra_metadata: dict[str, Any] | None = None,
    ) -> None:
        if stage == "llm_fallback" and getattr(message, "_llm_failure_logged", False):
            return

        try:
            from core.llm_failure_log import build_failure_entry, record_failure_entry

            correction_context = getattr(message, "correction_context", None)
            last_action_result = getattr(message, "last_action_result", None)
            metadata: dict[str, Any] = {}
            if isinstance(extra_metadata, dict):
                metadata.update(extra_metadata)
            if isinstance(last_action_result, dict) and last_action_result:
                metadata["last_action_result"] = last_action_result
            if context:
                for key in (
                    "source",
                    "interface_name",
                    "payload_thread_id",
                    "allowed_action_types",
                ):
                    if key in context:
                        metadata[key] = context.get(key)

            original_text = None
            if context:
                original_text = context.get("original_text")
            if not isinstance(original_text, str):
                original_text = getattr(message, "original_text", None)
            if not isinstance(original_text, str):
                original_text = None

            entry = build_failure_entry(
                reason=reason_text,
                stage=stage,
                interface_path=interface_path,
                chat_id=chat_id,
                thread_id=thread_id,
                engine=(context or {}).get("engine")
                or (context or {}).get("cortex_engine"),
                model=(context or {}).get("model")
                or (context or {}).get("cortex_model"),
                message_id=getattr(message, "event_id", None)
                or getattr(message, "message_id", None),
                content_preview=original_text[:500]
                if isinstance(original_text, str)
                else None,
                correction_context=correction_context
                if isinstance(correction_context, dict)
                else None,
                metadata=metadata,
                failure_code=failure_code,
            )
            await record_failure_entry(entry)
            if stage == "llm_fallback":
                message._llm_failure_logged = True
        except Exception as exc:
            log_warning(f"[message_chain] Failed to persist failure log entry: {exc}")

    await _record_failure_event("llm_fallback", failure_reason)

    # Clear transient avatar face state so upstream outages do not leave the
    # persona stuck with stale failure-adjacent expressions on reconnect.
    try:
        from core.animation_handler import get_karada_state_server

        karada = get_karada_state_server()
        if karada is not None:
            await karada.push_face_expression(None, 0)
            await karada.clear_face_values()
    except Exception as face_exc:
        log_warning(
            f"[message_chain] Failed to clear face state on fallback: {face_exc}"
        )

    # Send fallback message through transport layer
    try:
        from core.transport_layer import universal_send

        # Get the send_message method from the bot (interface)
        if bot and hasattr(bot, "send_message"):
            try:
                # First attempt: prefer interface-friendly args (message_thread_id is commonly used)
                # skip_history=True: fallback messages must NOT be stored in chat history — they
                # would pollute the LLM context and make future responses progressively slower.
                await universal_send(
                    bot.send_message,
                    chat_id,
                    text=fallback_text,
                    interface_path=interface_path,
                    thread_id=thread_id,
                    is_llm_response=True,  # Mark as LLM response so interface handles normally
                    skip_history=True,  # Do NOT store fallback in chat history
                )
            except TypeError as te:
                # Some bots (or test fakes) don't accept 'message_thread_id'.
                # Retry without mapping thread id to message_thread_id.
                try:
                    log_warning(
                        f"[message_chain] send_message TypeError, retrying without message_thread_id: {te}"
                    )
                    await universal_send(
                        bot.send_message,
                        chat_id,
                        text=fallback_text,
                        interface_path=interface_path,
                        is_llm_response=True,
                        skip_history=True,  # Do NOT store fallback in chat history
                    )
                except Exception as e:
                    # If retry fails, surface the error but continue gracefully
                    log_error(
                        f"[message_chain] Failed to send fallback message after retry: {e} (original: {te})"
                    )
        else:
            await _record_failure_event(
                "delivery",
                "Fallback delivery skipped: bot does not have send_message method",
                failure_code="delivery_failed",
                extra_metadata={"fallback_text": fallback_text},
            )
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
    from datetime import datetime, timezone

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
        message.date = datetime.now(timezone.utc)

    # Default context
    ctx = context or {}
    ctx["message"] = message
    ctx["original_text"] = (
        text  # Track original text in context, not on message (for consistency with immutable Telegram Message objects)
    )
    if not ctx.get("original_user_message"):
        try:
            ctx["original_user_message"] = getattr(message, "text", "") or ""
        except Exception:
            ctx["original_user_message"] = ""

    # Mark cortex-origin in context (not on message object, as Telegram
    # Message objects are immutable). We normalise on "from_cortex" internally.
    is_cortex_origin = True if source == "llm" else ctx.get("from_cortex", False)
    # also honor explicit cortex/ai flags in context (legacy support)
    if not is_cortex_origin:
        is_cortex_origin = bool(ctx.get("from_cortex") or ctx.get("from_ai"))
    ctx["from_cortex"] = is_cortex_origin

    # Preserve raw LLM response text for Debrief/intent-recovery plugins
    if is_cortex_origin:
        try:
            ctx["llm_response_text"] = text or ""
        except Exception:
            ctx["llm_response_text"] = ""

    # Also set on message object if possible (for corrector_orchestrator and action_parser detection)
    try:
        if hasattr(message, "__dict__") or isinstance(message, type({})):
            # keep legacy attr for compatibility but core uses from_cortex
            message.from_cortex = is_cortex_origin
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

    # Propagate is_voice_input from message attributes (set by WebUI when input
    # was transcribed from audio, so TTS auto-inject fires for voice-originated requests)
    if (
        hasattr(message, "is_voice_input")
        and message.is_voice_input
        and not ctx.get("is_voice_input")
    ):
        ctx["is_voice_input"] = True

    log_debug(
        f"[message_chain] Context preserved: interface_path={ctx.get('interface_path')}, chat_id={ctx.get('chat_id')}, interface={ctx.get('interface')}"
    )

    # Process LLM messages for emotional state updates
    if ctx.get("from_cortex", False) or source == "llm":
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
                        asyncio.create_task(
                            run_action(corrector_action, ctx, bot, message)
                        )
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

        # Remove internal emotion tags from the LLM text once they have been
        # processed so downstream actions and interfaces only see clean text.
        try:
            from plugins.emotion_manager import strip_emotion_tags

            cleaned_text = strip_emotion_tags(text)
            if cleaned_text != text:
                log_debug("[message_chain] Stripped emotion tags from LLM-origin text")
                text = cleaned_text
        except Exception:
            pass

    log_info("[message_chain] 📋 Starting action extraction loop...")
    # Retry/tried set to avoid loops
    tried_texts = set()
    attempt = 0
    max_retries = ctx.get("max_retries", int(CORRECTOR_RETRIES))

    # Check for action result delivery context - these responses should have minimal retries
    # to prevent cascading loops when processing action outputs (e.g., memory_search results)
    # (value read later if needed)
    try:
        system_message = ctx.get("system_message", {})
        if isinstance(system_message, dict):
            # note: we only read is_action_result when necessary later, ignore
            # for now to keep lint happy
            _ = system_message.get("is_action_result_delivery", False)
            custom_max = system_message.get("max_correction_attempts")
            if custom_max is not None and isinstance(custom_max, int):
                max_retries = min(max_retries, custom_max)
                log_info(
                    f"[message_chain] Action result delivery detected - limiting retries to {max_retries}"
                )
    except Exception:
        pass

    # Track whether any actions have successfully executed during the loop.
    # If at least one action ran, we consider the iteration partially successful and
    # avoid sending a generic fallback message later on. This matches the
    # requirement that "LLM failure" should only be emitted when the entire
    # iteration failed (or there was a technical error with no reply at all).
    actions_executed_during_loop = False

    while True:
        log_info(
            f"[message_chain] 🔄 LOOP: attempt={attempt} source={source} chat={getattr(message, 'chat_id', None)} text_len={len(text) if text else 0}"
        )

        # Determine interface flags early
        user_facing_interfaces = [
            "discord_bot",
            "telegram_bot",
            "synth_webui",
            "matrix_chat",
            "ollama_serve",
        ]
        interface_path = ctx.get("interface_path") or ""
        chat_id = ctx.get("chat_id")

        is_user_facing = interface_path and any(
            interface_path.startswith(f"{iface}/") for iface in user_facing_interfaces
        )

        is_internal_chat = chat_id == -1 or chat_id == "-1" or str(chat_id) == "-1"

        is_grillo_internal = ctx.get("grillo_beat", False) and not is_outbound_beat(
            ctx.get("beat_type")
        )

        # Prompt-scoped turns (e.g. vision_describe, delivery prompts) restrict the
        # LLM to specific action types; if no message_* type is allowed, the LLM
        # cannot reply to the user and we must not demand one.
        _allowed_types = ctx.get("allowed_action_types")
        is_scoped_non_message = (
            isinstance(_allowed_types, (list, tuple, set))
            and len(_allowed_types) > 0
            and not any(
                isinstance(t, str) and t.startswith("message_") for t in _allowed_types
            )
        )

        actions = None

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

            # Pre-normalize: unwrap single-element arrays wrapping a
            # standard dict response — [{"actions": [...]}, ...],
            # [{"tool_calls": [...]}, ...], or [{"text": "...", ...}].
            # Collapse to the inner dict so the existing dict-branch
            # handles everything (meta, feelings, normalization, etc.).
            if (
                isinstance(parsed, list)
                and len(parsed) == 1
                and isinstance(parsed[0], dict)
                and (
                    "actions" in parsed[0]
                    or "tool_calls" in parsed[0]
                    or "text" in parsed[0]
                )
            ):
                _wrapper = parsed[0]
                _wkey = (
                    "actions"
                    if "actions" in _wrapper
                    else ("tool_calls" if "tool_calls" in _wrapper else "text")
                )
                log_info(
                    f'[message_chain] \U0001f504 Unwrapping [{{"{_wkey}": ...}}] '
                    "single-element list wrapper \u2192 dict"
                )
                parsed = _wrapper

            # Recognise 'tool_calls' as a synonym for 'actions'.  Gemini
            # sometimes outputs {"tool_calls": [{"name": ..., "arguments": ...}]}
            # instead of {"actions": [{"type": ..., "payload": ...}]}.
            if (
                isinstance(parsed, dict)
                and "tool_calls" in parsed
                and "actions" not in parsed
            ):
                tc = parsed["tool_calls"]
                if isinstance(tc, list):
                    log_info(
                        f"[message_chain] \U0001f504 Normalizing top-level 'tool_calls' "
                        f"({len(tc)} item(s)) \u2192 'actions'"
                    )
                    parsed["actions"] = tc
                    del parsed["tool_calls"]

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
                else:
                    # Recover a root-level type+payload action that the LLM placed
                    # outside the actions array, e.g.:
                    #   {"actions": [...], "type": "message_telegram_bot", "payload": {...}}
                    # gemma-uncensored and similar models occasionally emit this
                    # malformed structure where the last action leaks to root.
                    # Folding it in here avoids the corrector firing for every
                    # message that ends this way, which would produce duplicate
                    # user-visible messages via the deliver_to_llm chain.
                    _root_type = parsed.get("type")
                    _root_payload = parsed.get("payload")
                    if (
                        isinstance(_root_type, str)
                        and _root_type not in ("actions", "recovery_actions")
                        and isinstance(_root_payload, dict)
                    ):
                        actions.append({"type": _root_type, "payload": _root_payload})
                        log_info(
                            f"[message_chain] \U0001f504 Recovered root-level action "
                            f"'{_root_type}' outside actions array → appended"
                        )

                    # Normalize OpenAI tool calling format (name/parameters) to type/payload
                    for i, act in enumerate(actions):
                        if isinstance(act, dict):
                            act = cast(dict[str, Any], act)
                            # Normalize legacy single-key action objects inside the
                            # actions array, e.g. {"create_personal_diary_entry": {...}}
                            # to the canonical {"type": ..., "payload": ...} format.
                            if (
                                "type" not in act
                                and "action" not in act
                                and "name" not in act
                                and len(act) == 1
                            ):
                                action_name, action_payload = next(iter(act.items()))
                                if isinstance(action_name, str):
                                    normalized_payload = (
                                        action_payload
                                        if isinstance(action_payload, dict)
                                        else {"value": action_payload}
                                    )
                                    actions[i] = {
                                        "type": action_name,
                                        "payload": normalized_payload,
                                    }
                                    act = cast(dict[str, Any], actions[i])
                                    log_info(
                                        "[message_chain] 🔄 Normalized legacy single-key action object inside actions array: "
                                        f"{action_name}"
                                    )
                            # Normalize alternative keys for action type if 'type' is missing
                            if "type" not in act:
                                # Prioritize 'function' or 'name' (OpenAI/Gemini style) then 'plugin'
                                _t = (
                                    act.get("function")
                                    or act.get("name")
                                    or act.get("plugin")
                                    or act.get("action")
                                    or act.get("command")
                                    or act.get("method")
                                )
                                if _t:
                                    act["type"] = _t
                                    log_debug(
                                        f"[message_chain] Normalizing action type: mapped to '{_t}'"
                                    )

                            # Normalize alternative keys for payload if 'payload' is missing
                            if "payload" not in act:
                                # Prioritize 'arguments' (OpenAI) or 'parameters' (Gemini) then 'args'/'schema'/'input'
                                _p = (
                                    act.get("arguments")
                                    or act.get("parameters")
                                    or act.get("args")
                                    or act.get("schema")
                                    or act.get("input")
                                )
                                if _p:
                                    act["payload"] = _p
                                    log_debug(
                                        f"[message_chain] Normalizing action payload for {act.get('type')}"
                                    )

                            # Fallback: gather flat action fields into payload
                            if "payload" not in act and "type" in act:
                                _extra = {
                                    k: v
                                    for k, v in act.items()
                                    if k not in _ACTION_SYSTEM_KEYS
                                }
                                if _extra:
                                    for k in _extra:
                                        del act[k]
                                    act["payload"] = _extra
                                    log_debug(
                                        f"[message_chain] Gathered {len(_extra)} flat fields into payload for {act.get('type')}"
                                    )
                            elif "payload" in act and "type" in act:
                                # Merge any action-level keys that belong inside
                                # payload but were placed at the action level by
                                # the LLM (e.g. interface_path, chat_name,
                                # reply_to_message_id for message_* actions).
                                _orphaned = {
                                    k: v
                                    for k, v in act.items()
                                    if k not in _ACTION_SYSTEM_KEYS
                                }
                                if _orphaned:
                                    for k in _orphaned:
                                        del act[k]
                                    for k, v in _orphaned.items():
                                        if k not in act["payload"]:
                                            act["payload"][k] = v
                                    log_debug(
                                        f"[message_chain] Merged {len(_orphaned)} orphaned action-level"
                                        f" key(s) into payload for {act.get('type')}: {list(_orphaned)}"
                                    )
            elif isinstance(parsed, list):
                actions = parsed
                # Normalize OpenAI tool calling format for bare list responses
                for i, act in enumerate(actions):
                    if isinstance(act, dict):
                        act = cast(dict[str, Any], act)
                        # Normalize type
                        if "type" not in act:
                            _t = (
                                act.get("function")
                                or act.get("name")
                                or act.get("plugin")
                                or act.get("action")
                                or act.get("command")
                                or act.get("method")
                            )
                            if _t:
                                act["type"] = _t
                                log_debug(
                                    f"[message_chain] Normalizing bare list action type: mapped to '{_t}'"
                                )
                        # Normalize payload
                        if "payload" not in act:
                            _p = (
                                act.get("arguments")
                                or act.get("parameters")
                                or act.get("args")
                                or act.get("schema")
                                or act.get("input")
                            )
                            if _p:
                                act["payload"] = _p
                                log_debug(
                                    f"[message_chain] Normalizing bare list action payload for {act.get('type')}"
                                )

                        # Fallback: gather flat action fields into payload
                        if "payload" not in act and "type" in act:
                            _extra = {
                                k: v
                                for k, v in act.items()
                                if k not in _ACTION_SYSTEM_KEYS
                            }
                            if _extra:
                                for k in _extra:
                                    del act[k]
                                act["payload"] = _extra
                                log_debug(
                                    f"[message_chain] Gathered {len(_extra)} flat fields into payload for {act.get('type')}"
                                )
                        elif "payload" in act and "type" in act:
                            # Merge any action-level keys that belong inside
                            # payload but were placed at the action level by
                            # the LLM (e.g. interface_path, chat_name,
                            # reply_to_message_id for message_* actions).
                            _orphaned = {
                                k: v
                                for k, v in act.items()
                                if k not in _ACTION_SYSTEM_KEYS
                            }
                            if _orphaned:
                                for k in _orphaned:
                                    del act[k]
                                for k, v in _orphaned.items():
                                    if k not in act["payload"]:
                                        act["payload"][k] = v
                                log_debug(
                                    f"[message_chain] Merged {len(_orphaned)} orphaned action-level key(s) into"
                                    f" payload for {act.get('type')}: {list(_orphaned)}"
                                )
            elif isinstance(parsed, dict) and (
                "type" in parsed
                or "name" in parsed
                or "function" in parsed
                or "plugin" in parsed
                or "action" in parsed
                or "command" in parsed
                or "method" in parsed
            ):
                # Normalize singe-action dict (type/name/function/plugin/command)
                if "type" not in parsed:
                    parsed["type"] = (
                        parsed.get("function")
                        or parsed.get("name")
                        or parsed.get("plugin")
                        or parsed.get("action")
                        or parsed.get("command")
                        or parsed.get("method")
                    )
                if "payload" not in parsed:
                    parsed["payload"] = (
                        parsed.get("arguments")
                        or parsed.get("parameters")
                        or parsed.get("args")
                        or parsed.get("schema")
                        or parsed.get("input")
                    )
                # Fallback: gather flat action fields into payload
                if not parsed.get("payload") and "type" in parsed:
                    _extra = {
                        k: v for k, v in parsed.items() if k not in _ACTION_SYSTEM_KEYS
                    }
                    if _extra:
                        for k in _extra:
                            del parsed[k]
                        parsed["payload"] = _extra
                        log_debug(
                            f"[message_chain] Gathered {len(_extra)} flat fields into payload for {parsed.get('type')}"
                        )
                elif (
                    isinstance((_pl := parsed.get("payload")), dict)
                    and "type" in parsed
                ):
                    _orphaned = {
                        k: v for k, v in parsed.items() if k not in _ACTION_SYSTEM_KEYS
                    }
                    if _orphaned:
                        for k in _orphaned:
                            del parsed[k]
                        for k, v in _orphaned.items():
                            if k not in _pl:
                                _pl[k] = v
                        log_debug(
                            f"[message_chain] Merged {len(_orphaned)} orphaned action-level key(s) into"
                            f" payload for {parsed.get('type')}: {list(_orphaned)}"
                        )
                if "payload" not in parsed:
                    parsed["payload"] = {}
                actions = [parsed]
                log_debug(
                    f"[message_chain] Normalized single-action dict: {parsed.get('type')}"
                )
            elif isinstance(parsed, dict) and "recovery_actions" in parsed:
                # Debrief plugins sometimes return {"recovery_actions": [{"action_type": ..., "payload": ...}]}
                # Normalize this to the standard {"actions": [{"type": ..., "payload": ...}]} format.
                raw_recovery = parsed.get("recovery_actions")
                if isinstance(raw_recovery, list):
                    normalized_recovery = []
                    for item in raw_recovery:
                        if not isinstance(item, dict):
                            continue
                        # action_type → type (debrief uses action_type, core uses type)
                        atype = (
                            item.get("action_type")
                            or item.get("type")
                            or item.get("action")
                        )
                        if not atype:
                            continue
                        normalized_recovery.append(
                            {"type": atype, "payload": item.get("payload", {})}
                        )
                    if normalized_recovery:
                        log_info(
                            f"[message_chain] 🔄 Normalizing recovery_actions → actions "
                            f"({len(normalized_recovery)} item(s)): "
                            f"{[a['type'] for a in normalized_recovery]}"
                        )
                        actions = normalized_recovery
                    else:
                        log_warning(
                            "[message_chain] recovery_actions list was empty or malformed - triggering corrector"
                        )
                        parsed = None
                else:
                    log_warning(
                        "[message_chain] recovery_actions field is not a list - triggering corrector"
                    )
                    parsed = None
            elif (
                isinstance(parsed, dict)
                and "action" in parsed
                and isinstance(parsed["action"], str)
            ):
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
            elif (
                isinstance(parsed, dict)
                and "text" in parsed
                and "interface_path" in parsed
            ):
                # Bare message payload recovered from corrupted JSON — the
                # LLM produced the correct payload but the actions wrapper
                # was lost during JSON extraction.  Infer the action type
                # from the interface_path prefix.
                _ipath = str(parsed.get("interface_path", ""))
                _iface = _ipath.split("/")[0] if "/" in _ipath else _ipath
                _inferred_type = f"message_{_iface}" if _iface else None
                if _inferred_type:
                    log_info(
                        f"[message_chain] 🔄 Wrapping bare message payload "
                        f"as {_inferred_type} (recovered from corrupted JSON)"
                    )
                    actions = [{"type": _inferred_type, "payload": parsed}]
                else:
                    log_warning(
                        f"[message_chain] Bare payload has interface_path "
                        f"but no inferrable action type: {_ipath}"
                    )
                    parsed = None
            elif isinstance(parsed, dict) and "text" in parsed:
                # Text-only response without actions — e.g.
                # {"text": "...", "meta": {"autonomous": true}}
                # The LLM returned a plain message with optional metadata
                # but omitted the actions wrapper entirely.  Infer the
                # correct message action from the context interface_path.
                _ctx_ipath = ctx.get("interface_path", "") if ctx else ""
                _ctx_iface = (
                    _ctx_ipath.split("/")[0] if "/" in _ctx_ipath else _ctx_ipath
                )
                _inferred_type = _INTERFACE_TO_MESSAGE_ACTION.get(_ctx_iface)
                if _inferred_type:
                    _text_content = parsed.get("text", "")
                    _payload: dict[str, Any] = {
                        "text": _text_content,
                        "interface_path": _ctx_ipath,
                    }
                    _msg_action: dict[str, Any] = {
                        "type": _inferred_type,
                        "payload": _payload,
                    }
                    # Propagate top-level meta (e.g. autonomous flag) to the action
                    _top_meta = parsed.get("meta")
                    if isinstance(_top_meta, dict):
                        _msg_action["meta"] = _top_meta
                    actions = [_msg_action]
                    log_info(
                        f"[message_chain] 🔄 Wrapping text-only response "
                        f"as {_inferred_type} (no actions array, inferred "
                        f"from context interface_path={_ctx_ipath})"
                    )
                else:
                    log_warning(
                        f"[message_chain] Text-only response but cannot "
                        f"infer message action from interface_path: "
                        f"{_ctx_ipath!r}"
                    )
                    parsed = None
            elif (
                isinstance(parsed, dict)
                and not any(
                    k in ["actions", "recovery_actions", "type", "text"]
                    for k in parsed.keys()
                )
                and not isinstance(parsed.get("action"), str)
            ):
                # The LLM may have returned a dictionary mapping action types to payloads
                # e.g., {"message_telegram_bot": {...}, "create_personal_diary_entry": {...}}
                # Or it may have returned {"action": {"command": "...", ...}}
                log_info("[message_chain] 🔄 Normalizing action-key dictionary format")
                actions = []
                for k, v in parsed.items():
                    if isinstance(v, dict):
                        # Handle {"action": {"command": "update_emotion_from_tags", ...}}
                        if k == "action" and ("command" in v or "type" in v):
                            atype = v.get("command") or v.get("type")
                            actions.append({"type": str(atype), "payload": v})
                        else:
                            actions.append({"type": k, "payload": v})
                    else:
                        actions.append({"type": k, "payload": {}})
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
                is_from_cortex = source == "llm" or getattr(
                    message, "from_cortex", False
                )
                if is_from_cortex and isinstance(parsed, dict):
                    try:
                        from core.validation_registry import get_validation_registry

                        allowed_metadata = (
                            get_validation_registry().get_response_metadata_keys()
                            or set()
                        )
                        extra_keys = [
                            k
                            for k in parsed.keys()
                            if k != "actions"
                            and k not in allowed_metadata
                            and not k.startswith("meta.")
                        ]
                        if extra_keys:
                            synthetic_actions = []
                            for key in extra_keys:
                                value = parsed.get(key)
                                # When a string value is stored under a message-like
                                # root key (e.g. {"message": "hello"}) the LLM
                                # probably intended a text reply.  Map it to
                                # {"text": ...} so that message_synth_webui and
                                # other send_message implementations can find it.
                                if isinstance(value, dict):
                                    payload = value
                                elif key in (
                                    "message",
                                    "text",
                                    "reply",
                                    "response",
                                ) and isinstance(value, str):
                                    payload = {"text": value}
                                else:
                                    payload = {"value": value}
                                # Skip synthetic message actions when the same text is
                                # already present in an explicit message action — the LLM
                                # commonly echoes the reply in a top-level "message" key in
                                # addition to the proper actions list, which creates a
                                # duplicate that fails validation (no interface_path) and
                                # triggers an unnecessary correction loop.
                                if key in (
                                    "message",
                                    "text",
                                    "reply",
                                    "response",
                                ) and isinstance(value, str):

                                    def _matches_existing_message(action: Any) -> bool:
                                        if not isinstance(action, dict):
                                            return False
                                        action_type = action.get("type")
                                        if not isinstance(action_type, str):
                                            return False
                                        if not action_type.startswith("message_"):
                                            return False
                                        payload = action.get("payload")
                                        if not isinstance(payload, dict):
                                            return False
                                        return payload.get("text") == value

                                    already_present = any(
                                        _matches_existing_message(a) for a in actions
                                    )
                                    if already_present:
                                        log_debug(
                                            f"[message_chain] Skipping synthetic '{key}' action — text already present in explicit message action"
                                        )
                                        continue
                                # Resolve the correct action type and inject
                                # interface_path for message-like synthetic actions
                                # so they pass validation without a correction loop.
                                synthetic_type = key
                                ctx_ipath = ctx.get("interface_path") if ctx else None
                                if key in (
                                    "message",
                                    "text",
                                    "reply",
                                    "response",
                                ) and isinstance(value, str):
                                    if ctx_ipath:
                                        iface_prefix = str(ctx_ipath).split("/")[0]
                                        resolved = _INTERFACE_TO_MESSAGE_ACTION.get(
                                            iface_prefix
                                        )
                                        if resolved:
                                            synthetic_type = resolved
                                        if "interface_path" not in payload:
                                            payload["interface_path"] = str(ctx_ipath)
                                synthetic_actions.append(
                                    {"type": synthetic_type, "payload": payload}
                                )

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
                actions = _normalize_action_type_aliases(actions)
                actions = _normalize_diary_payload_fields(actions)
                actions = _normalize_message_payload_text(actions)
                # Auto-inject interface_path into message actions that are missing it
                # This prevents validation failures and avoids costly LLM correction calls
                actions = _auto_inject_interface_path(actions, ctx_interface_path)

                # --- New: Validate action types early and trigger corrector for unsupported types ---
                try:
                    from core.action_parser import get_supported_action_types

                    supported_action_types = get_supported_action_types() or set()
                except Exception as e:
                    log_warning(
                        f"[message_chain] Could not load supported action types: {e}"
                    )
                    supported_action_types = set()

                # Only enforce this for LLM-originated responses
                if is_from_cortex and isinstance(actions, list):
                    try:
                        scoped_actions = ctx.get("allowed_action_types") or ctx.get(
                            "allowed_actions"
                        )
                        if isinstance(scoped_actions, (list, set, tuple)):
                            supported_action_types.update(
                                str(action_type)
                                for action_type in scoped_actions
                                if action_type
                            )
                    except Exception as e:
                        log_debug(
                            "[message_chain] Failed to merge scoped allowed action "
                            f"types into runtime supported set: {e}"
                        )

                    # quick mapping: some models may output a generic "message"
                    # or plain "text" / "reply" / "response" action when they
                    # really intend to send text to the current interface.
                    # Convert it to a concrete type based on the context path
                    # to avoid unnecessary corrector loops.
                    _GENERIC_MSG_TYPES = (
                        "message",
                        "send_message",
                        "message_send",  # some LLMs swap the word order
                        "text",
                        "reply",
                        "response",
                    )
                    if ctx_interface_path:
                        interface_prefix = (
                            ctx_interface_path.split("/")[0]
                            if "/" in ctx_interface_path
                            else ctx_interface_path
                        )
                        resolved_message_type = _INTERFACE_TO_MESSAGE_ACTION.get(
                            interface_prefix, "message_synth_webui"
                        )
                        rewrote_generic_message_action = False
                        for act in actions:
                            if (
                                isinstance(act, dict)
                                and act.get("type") in _GENERIC_MSG_TYPES
                            ):
                                act["type"] = resolved_message_type
                                rewrote_generic_message_action = True

                                payload = act.get("payload")
                                if not isinstance(payload, dict):
                                    payload = {}
                                    act["payload"] = payload

                                if (
                                    not isinstance(payload.get("text"), str)
                                    or not payload.get("text", "").strip()
                                ):
                                    for legacy_key in (
                                        "text",
                                        "body",
                                        "content",
                                        "message",
                                        "value",
                                    ):
                                        legacy_value = act.get(legacy_key)
                                        if (
                                            isinstance(legacy_value, str)
                                            and legacy_value.strip()
                                        ):
                                            payload["text"] = legacy_value
                                            break

                        if rewrote_generic_message_action:
                            actions = _normalize_message_payload_text(actions)
                            actions = _auto_inject_interface_path(
                                actions, ctx_interface_path
                            )

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
                                    "errors": [
                                        f"Unsupported type '{atype}' - no plugin or interface found to handle it"
                                    ],
                                }
                            )

                    if unsupported:
                        bad_indices = frozenset(u["index"] for u in unsupported)
                        remaining_valid = [
                            act
                            for idx, act in enumerate(actions)
                            if idx not in bad_indices
                        ]

                        if remaining_valid:
                            # Some valid actions remain — drop only the unrecognised
                            # ones so the response can still be delivered without
                            # triggering (and likely exhausting) the corrector.
                            log_warning(
                                f"[message_chain] 🚨 Dropping {len(unsupported)} unsupported action "
                                f"type(s): {[u['action'].get('type') or u['action'].get('action') for u in unsupported]} "
                                "— continuing with valid actions"
                            )
                            actions = remaining_valid
                        else:
                            # All actions are unsupported — request correction
                            log_warning(
                                f"[message_chain] 🚨 Detected unsupported action types from LLM: {[u['action'].get('type') or u['action'].get('action') for u in unsupported]} - requesting correction"
                            )
                            # Attach correction context and force correction path
                            correction_context = {
                                "successful_actions": [],
                                "successful_types": [],
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
                                valid_feelings.keys(),
                                key=lambda emotion_name: valid_feelings[emotion_name],
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
                if source == "llm" or getattr(message, "from_cortex", False):
                    has_user_response = False
                    has_tts = False
                    # Set when the LLM emits an action that delivers user-visible
                    # output on its own (a self-replying plugin action). Tracked
                    # separately from has_user_response so it suppresses the
                    # missing-reply corrector without affecting message/TTS handling.
                    has_user_output_action = False
                    user_message_action = None
                    # Determine current set of message action types from config (dynamic)
                    current_message_action_types = []
                    try:
                        from core.config_manager import config_registry

                        MESSAGE_ACTION_TYPES = config_registry.get_var(
                            "MESSAGE_ACTION_TYPES",
                            [],
                            label="Message action types",
                            description=(
                                "Action types treated as outbound user-visible messages (used for response detection and logging)."
                            ),
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

                    # Action types that deliver user-visible output on their own
                    # (self-replying plugin actions, e.g. a plugin that calls
                    # bot.send_message inside execute_action). They satisfy the
                    # "did the user get a reply this turn?" check so the missing-reply
                    # corrector does not fire when the LLM responds with only such an
                    # action. Engine-agnostic and unrelated to any endpoint grammar —
                    # purely about whether the user receives output. Contributors add
                    # their own self-replying actions here via config; fetch-only
                    # actions that need a follow-up reply (e.g. recall_last_dream) must
                    # NOT be listed.
                    current_user_output_action_types = []
                    try:
                        from core.config_manager import config_registry

                        USER_OUTPUT_ACTION_TYPES = config_registry.get_var(
                            "USER_OUTPUT_ACTION_TYPES",
                            ["get_recent_chats"],
                            label="User-output action types",
                            description=(
                                "Action types that deliver user-visible output on their own "
                                "(self-replying plugin actions). Counted as a user reply so the "
                                "missing-reply corrector does not fire when the LLM responds with "
                                "only such an action."
                            ),
                            group="core",
                            component="message_chain",
                        )
                        current_user_output_action_types = (
                            list(USER_OUTPUT_ACTION_TYPES.value)
                            if hasattr(USER_OUTPUT_ACTION_TYPES, "value")
                            else list(USER_OUTPUT_ACTION_TYPES)
                        )
                    except Exception:
                        current_user_output_action_types = []

                    if isinstance(actions, list):
                        actions = cast(list[dict[str, Any]], actions)
                        # ========================================
                        # STRIP TTS FROM AUTONOMOUS MESSAGES
                        # ========================================
                        # Check if any message action is autonomous (Grillo outreach, dreams, etc.)
                        # If so, remove any tts_speak actions - they shouldn't be spoken
                        has_autonomous_message = False
                        for action in actions:
                            if isinstance(action, dict):
                                action_meta = action.get("meta")
                                if not isinstance(action_meta, dict):
                                    action_meta = {}
                                action_name = action.get("action") or action.get("type")
                                # Check if this is an autonomous message action
                                if action_meta.get("autonomous", False) is True:
                                    if (
                                        action_name
                                        and isinstance(action_name, str)
                                        and action_name.startswith("message_")
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
                                    removed_payload = removed.get("payload")
                                    if not isinstance(removed_payload, dict):
                                        removed_payload = {}
                                    log_debug(
                                        f"[message_chain] 🔇 Stripped TTS from autonomous message: {removed_payload.get('text', '')[:40]}..."
                                    )

                        for action in actions:
                            # Support both 'action' and 'type' keys
                            action_name = None
                            if isinstance(action, dict):
                                action_name = action.get("action") or action.get("type")
                            if action_name == "tts_speak":
                                has_tts = True
                            if action_name in current_message_action_types or (
                                isinstance(action_name, str)
                                and action_name.startswith("message_")
                            ):
                                has_user_response = True
                                if not user_message_action:
                                    user_message_action = action
                                # break
                            if action_name in current_user_output_action_types:
                                has_user_output_action = True

                    # ------------------------------------------------------------------
                    # LLM-CHOSEN VOICE REPLY (text turn): merge into a single voice note
                    # ------------------------------------------------------------------
                    # When the LLM itself decided to answer with `tts_speak` AND also
                    # emitted a text-only message_* action for the same reply, we don't
                    # want the user to receive both a text bubble and a separate audio
                    # note. Mirror the voice-input behaviour: drop the standalone text
                    # message and fold its text into the tts_speak as the caption.
                    #
                    # This lets the synth reply with voice on a *typed* request (e.g.
                    # "answer me with a voice message") or whenever it chooses to speak,
                    # driven purely by the LLM's action choice (no keyword detection).
                    # Skipped for: WebUI (keeps its text bubble for the tts-play handler),
                    # autonomous/internal turns (already stripped above), and voice-input
                    # turns (handled by the auto-inject path below).
                    if (
                        has_tts
                        and has_user_response
                        and user_message_action
                        and isinstance(actions, list)
                    ):
                        _merge_iface_prefix = (ctx.get("interface_path") or "").split(
                            "/"
                        )[0]
                        _merge_is_voice_input = bool(ctx.get("is_voice_input", False))
                        _merge_request_tts = bool(
                            context
                            and isinstance(context, dict)
                            and context.get("request_tts")
                        )
                        # Only merge for LLM-driven voice replies on non-voice turns.
                        # Voice-originated turns are merged by the auto-inject path, and
                        # WebUI intentionally keeps the separate text bubble.
                        if (
                            _merge_iface_prefix != "synth_webui"
                            and not _merge_is_voice_input
                            and not _merge_request_tts
                        ):
                            # Find the LLM-emitted tts_speak that is NOT auto-injected.
                            _llm_tts_action = None
                            for _a in actions:
                                if not isinstance(_a, dict):
                                    continue
                                if (_a.get("action") or _a.get("type")) != "tts_speak":
                                    continue
                                _a_payload = _a.get("payload")
                                if not isinstance(_a_payload, dict):
                                    _a_payload = {}
                                if _a_payload.get("__auto_injected"):
                                    continue
                                _llm_tts_action = _a
                                break

                            if _llm_tts_action is not None:
                                _tts_payload = _llm_tts_action.get("payload")
                                if not isinstance(_tts_payload, dict):
                                    _tts_payload = {}
                                    _llm_tts_action["payload"] = _tts_payload
                                _spoken_text = str(
                                    _tts_payload.get("text") or ""
                                ).strip()
                                # Prefer the spoken text as caption; fall back to the
                                # text-only message body if tts_speak carried no text.
                                _msg_payload = (
                                    user_message_action.get("payload")
                                    if isinstance(user_message_action, dict)
                                    else {}
                                )
                                if not isinstance(_msg_payload, dict):
                                    _msg_payload = {}
                                _msg_text = str(
                                    _msg_payload.get("text")
                                    or _msg_payload.get("content")
                                    or _msg_payload.get("message")
                                    or ""
                                ).strip()
                                _caption = _spoken_text or _msg_text
                                if _caption:
                                    if not _tts_payload.get("text"):
                                        _tts_payload["text"] = _caption
                                    _tts_payload["__merged_text"] = _caption
                                    log_info(
                                        "[message_chain] \U0001f3a4 LLM voice reply on text turn: "
                                        f"merging text+audio into a single voice message for: {_caption[:30]}..."
                                    )
                                    # Remove standalone message_* actions; the voice
                                    # message (audio + caption) replaces them.
                                    actions[:] = [
                                        a
                                        for a in actions
                                        if isinstance(a, dict)
                                        and (a.get("action") or a.get("type"))
                                        not in current_message_action_types
                                    ]

                    # Auto-inject TTS if there's a user response but no tts_speak
                    # Only for actual user-facing interfaces (not internal like grillo)
                    if (
                        has_user_response
                        and not has_tts
                        and user_message_action
                        and isinstance(actions, list)
                    ):
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
                        # e.g., "telegram_bot/5551234567" not just "telegram_bot"
                        is_user_facing = interface_path and any(
                            interface_path.startswith(f"{iface}/")
                            for iface in user_facing_interfaces
                        )

                        # Check if this is an internal/system message
                        # chat_id == -1 indicates internal system messages (Grillo beats, etc.)
                        is_internal_chat = (
                            chat_id == -1 or chat_id == "-1" or str(chat_id) == "-1"
                        )

                        # Check if this is a Grillo internal beat (not outbound)
                        # Only internal Grillo beats (self_reflection, curiosity, etc.) skip TTS
                        # Outbound beats (observer) ARE user-facing and SHOULD get TTS
                        is_grillo_internal = ctx.get(
                            "grillo_beat", False
                        ) and not is_outbound_beat(ctx.get("beat_type"))

                        # Check for autonomous messages (Grillo outreach, dreams, etc.)
                        # These are system-initiated, not user-response, so they shouldn't get TTS
                        action_meta = (
                            user_message_action.get("meta")
                            if isinstance(user_message_action, dict)
                            else {}
                        )
                        if not isinstance(action_meta, dict):
                            action_meta = {}
                        is_autonomous = action_meta.get("autonomous", False) is True

                        # Check for system startup/internal message patterns
                        payload = (
                            user_message_action.get("payload")
                            if isinstance(user_message_action, dict)
                            else {}
                        )
                        if not isinstance(payload, dict):
                            payload = {}
                        text_to_speak = (
                            payload.get("text")
                            or payload.get("content")
                            or payload.get("message")
                            or ""
                        )
                        # If the LLM returned plain text rather than structured JSON,
                        # there will be no user_message_action and text_to_speak will
                        # be empty.  In cases where the interface explicitly asked for
                        # speech (e.g. voice note, request_tts flag) we fall back to
                        # speaking the raw text output so the user still hears audio.
                        if not text_to_speak and ctx.get("request_tts") and text:
                            text_to_speak = text
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
                        # When the LLM explicitly set send_as_voice=true on the
                        # message_* action, the audio is routed centrally in
                        # action_parser (VoxPlugin.speak) and the plain text is
                        # suppressed there. Auto-injecting tts_speak here as well
                        # would produce a second spoken message, so skip it.
                        _explicit_send_as_voice = bool(payload.get("send_as_voice"))

                        # Skip for: internal grillo beats, internal chats, system messages, autonomous messages, already-executed TTS,
                        # an explicit send_as_voice request (handled in action_parser),
                        # or if the request_tts flag/feature is off
                        should_skip_tts = (
                            is_grillo_internal
                            or is_internal_chat
                            or is_system_message
                            or is_autonomous
                            or tts_already_executed
                            or _explicit_send_as_voice
                        )
                        # honor explicit request flag passed via context or wrapped message
                        _explicit_tts_request = bool(
                            context
                            and isinstance(context, dict)
                            and context.get("request_tts")
                        )
                        if context and isinstance(context, dict):
                            if context.get("request_tts") is False:
                                should_skip_tts = True

                        # Determine whether Vox/TTS is effectively enabled.  The new
                        # canonical switch is the active engine name: if the selected
                        # engine is non-empty and not "disabled", we consider TTS on.
                        # For backwards compatibility we also honour the old boolean
                        # flags/endpoints, but they no longer drive the feature state.
                        tts_raw = ""
                        try:
                            from core.config_manager import config_registry

                            active = str(
                                config_registry.get_value(
                                    "ACTIVE_VOX_ENGINE",
                                    "",
                                    value_type=str,
                                    group="plugins",
                                    component="vox_plugin",
                                )
                            )
                            vox_enabled = bool(active and active != "disabled")

                            # legacy fallback
                            if not vox_enabled:
                                tts_raw = str(
                                    config_registry.get_value(
                                        "TTS_ENDPOINTS",
                                        "",
                                        value_type=str,
                                        group="plugins",
                                        component="tts_lipsync",
                                    )
                                    or ""
                                )
                                vox_enabled = bool(
                                    config_registry.get_value(
                                        "TTS_ENABLED",
                                        False,
                                        value_type=bool,
                                        group="plugins",
                                        component="tts_lipsync",
                                    )
                                    or tts_raw
                                )

                            if not vox_enabled and not _explicit_tts_request:
                                should_skip_tts = True
                                log_debug(
                                    "[message_chain] Skipping TTS auto-inject because Vox engine is disabled"
                                )
                            elif not vox_enabled and _explicit_tts_request:
                                log_debug(
                                    "[message_chain] Vox engine disabled but request_tts=True — "
                                    "allowing TTS attempt (VoxPlugin will fall back to text if needed)"
                                )

                        except Exception as e:
                            log_debug(
                                f"[message_chain] Error checking Vox/TTS config: {e}"
                            )

                        # For Telegram/Discord, only auto-inject TTS for voice-originated
                        # messages. WebUI, Matrix and Ollama always get TTS when VOX is active.
                        _is_voice_input = bool(ctx.get("is_voice_input", False))
                        _iface_tts_prefix = (ctx.get("interface_path") or "").split(
                            "/"
                        )[0]
                        _voice_only_tts_ifaces = {"telegram_bot", "discord_bot"}
                        tts_allowed = (
                            _iface_tts_prefix not in _voice_only_tts_ifaces
                        ) or _is_voice_input

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
                        # auto-inject only for voice-originated messages or when
                        # the LLM explicitly asked for TTS.  previously the
                        # ``tts_allowed`` flag would permit any text response from
                        # WebUI/Matrix/other non-voice interfaces which caused the
                        # synth to speak even though the incoming message was not
                        # audio.  the requirement is that non-audio inputs should not
                        # trigger spoken replies.
                        elif (is_user_facing and tts_allowed and _is_voice_input) or (
                            context and context.get("request_tts")
                        ):
                            # With the new strategy we honor explicit TTS requests even when
                            # they come from non-WebUI interfaces (voice note, etc.).
                            if (
                                text_to_speak
                                and isinstance(text_to_speak, str)
                                and len(text_to_speak.strip()) > 0
                            ):
                                # Determine whether this is a voice-originated request.
                                # For voice inputs we want to send a SINGLE audio+caption
                                # message instead of separate text then audio.
                                # Exception: synth_webui always keeps the text bubble so the
                                # tts-play handler has a bubble to annotate; audio is overlaid.
                                is_voice_response = (
                                    _is_voice_input
                                    or bool(context and context.get("request_tts"))
                                ) and _iface_tts_prefix != "synth_webui"

                                if is_voice_response:
                                    # Voice response strategy:
                                    #   • Remove the standalone message_* action so text
                                    #     is NOT sent as a separate message.
                                    #   • Inject tts_speak with __merged_text so the text
                                    #     becomes the audio caption (Telegram) or is sent
                                    #     immediately after the audio (other interfaces).
                                    #   • Do NOT set __auto_injected — if TTS fails the
                                    #     fallback will send text so the user is not left
                                    #     with zero response.
                                    log_info(
                                        f"[message_chain] 🎤 Voice response: replacing text-only message with audio+caption for: {text_to_speak[:30]}..."
                                    )
                                    # Remove all message_* actions; audio+caption replaces them
                                    actions[:] = [
                                        a
                                        for a in actions
                                        if isinstance(a, dict)
                                        and (a.get("action") or a.get("type"))
                                        not in current_message_action_types
                                    ]
                                    tts_action = {
                                        "type": "tts_speak",
                                        "payload": {
                                            "text": text_to_speak,
                                            # __merged_text becomes the caption on Telegram
                                            # and the follow-up text on other interfaces.
                                            "__merged_text": text_to_speak,
                                            "emotion": payload.get("emotion")
                                            if isinstance(payload, dict)
                                            else None,
                                        },
                                    }
                                else:
                                    # Non-voice: inject TTS alongside the existing message_*
                                    # action.  Set __auto_injected so VoxPlugin suppresses
                                    # the text fallback — text was already sent by message_*.
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
                                            # Flag: auto-injected by message_chain (not emitted
                                            # by the LLM). VoxPlugin uses this to suppress the
                                            # text fallback when TTS fails — text was already
                                            # sent by the message_*_bot action.
                                            "__auto_injected": True,
                                        },
                                    }
                                actions.append(tts_action)
                                # Update has_tts flag since we just added it
                                has_tts = True
                                # clear request flag so we don't duplicate later
                                if context and isinstance(context, dict):
                                    context.pop("request_tts", None)
                        else:
                            log_debug(
                                f"[message_chain] Skipping TTS auto-inject for non-user-facing interface: {interface_path}"
                            )

                    # at this point we may not have computed the
                    # various interface flags (they're defined only in
                    # the branch above). ensure they exist so the
                    # warning logic doesn't crash when has_user_response
                    # is False.
                    if "interface_path" not in locals():
                        interface_path = ctx.get("interface_path") or ""
                    if "is_user_facing" not in locals():
                        is_user_facing = False
                    if "is_grillo_internal" not in locals():
                        is_grillo_internal = False
                    if "is_internal_chat" not in locals():
                        is_internal_chat = False

                    if not has_user_response:
                        if (
                            is_user_facing
                            and not is_grillo_internal
                            and not is_internal_chat
                        ):
                            log_warning(
                                f"[message_chain] ⚠️ LLM generated no outbound message action for user-facing interface '{interface_path}' — user will receive no reply"
                            )
                        else:
                            log_debug(
                                "[message_chain] LLM chose not to send user message (diary/internal only)"
                            )
                    else:
                        # Check if the originating interface has a corresponding action
                        iface_prefix = (interface_path or "").split("/")[0]
                        expected_action = _INTERFACE_TO_MESSAGE_ACTION.get(iface_prefix)
                        action_types_in_response = {
                            a.get("type") or a.get("action")
                            for a in (actions or [])
                            if isinstance(a, dict)
                        }
                        if (
                            expected_action
                            and expected_action not in action_types_in_response
                            and is_user_facing
                            and not is_grillo_internal
                            and not is_internal_chat
                        ):
                            log_warning(
                                f"[message_chain] ⚠️ LLM replied but not to the originating interface '{iface_prefix}' "
                                f"(expected '{expected_action}', got {action_types_in_response & set(_INTERFACE_TO_MESSAGE_ACTION.values())}) "
                                f"— user may not receive reply on {iface_prefix}"
                            )
                        else:
                            log_debug("[message_chain] LLM will send message to user")

                        # CRITICAL FIX: Merge text into TTS when both are present
                        # This ensures text+audio are sent in the SAME message and
                        # prevents a separate duplicate text reply.  We no longer
                        # gate on plugin availability since the payload can still
                        # fall back to merged_text if TTS fails.
                        if has_user_response and has_tts and isinstance(actions, list):
                            # Find all message actions carrying text and any TTS actions
                            message_actions_to_remove = []
                            tts_actions = []

                            for idx, action in enumerate(actions):
                                if not isinstance(action, dict):
                                    continue
                                action_type = action.get("action") or action.get("type")

                                if action_type == "tts_speak":
                                    tts_actions.append(action)
                                elif action_type in current_message_action_types:
                                    msg_payload = action.get("payload", {})
                                    msg_text = msg_payload.get("text")
                                    # record interface_path or chat_name for diagnostics
                                    msg_ipath = msg_payload.get(
                                        "interface_path"
                                    ) or msg_payload.get("chat_name")

                                    if msg_text:
                                        message_actions_to_remove.append(
                                            (idx, msg_text, msg_ipath)
                                        )

                            # Merge text into each TTS payload and then drop the
                            # standalone message actions.
                            if message_actions_to_remove and tts_actions:
                                log_info(
                                    f"[message_chain] 🔗 Merging {len(message_actions_to_remove)} message action(s) into TTS to send text+audio together"
                                )

                                for tts_action in tts_actions:
                                    tts_payload = tts_action.get("payload", {})
                                    if not isinstance(tts_payload, dict):
                                        continue

                                    for (
                                        idx,
                                        msg_text,
                                        msg_ipath,
                                    ) in message_actions_to_remove:
                                        if "__merged_text" not in tts_payload:
                                            tts_payload["__merged_text"] = msg_text
                                            log_info(
                                                f"[message_chain] ✅ Merged text into tts_speak: '{msg_text[:50]}...'"
                                            )
                                            break

                                # Remove the standalone message actions
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
                        # filter out previously-successful types when retrying
                        if attempt > 0:
                            cc = getattr(message, "correction_context", None) or {}
                            successful_types = cc.get("successful_types", [])
                            if successful_types and isinstance(actions, list):
                                filtered = []
                                for act in actions:
                                    atype = None
                                    if isinstance(act, dict):
                                        atype = act.get("type") or act.get("action")
                                    if atype in successful_types:
                                        log_debug(
                                            f"[message_chain] Removing previously successful action type '{atype}' from retry payload"
                                        )
                                    else:
                                        filtered.append(act)
                                actions = filtered
                        result = await run_actions(actions, ctx, bot, message)
                        processed = result.get("processed", [])
                        failed = result.get("failed_actions", [])
                        errors = result.get("errors", [])
                        # A non-empty action_outputs means run_actions already
                        # enqueued an LLM delivery follow-up (terminal output, or a
                        # deliver_to_llm fetch action like recall_last_dream). That
                        # follow-up *is* the user's reply, so the missing-reply
                        # corrector must not also fire — otherwise both produce a
                        # message and the user gets a double reply.
                        delivered_to_llm = bool(result.get("action_outputs"))

                        # remember if we actually ran anything
                        if processed:
                            actions_executed_during_loop = True

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

                        recovered_with_extra_text = bool(
                            metadata.get("recovered", False)
                            and (
                                metadata.get("had_extra_text", False)
                                or metadata.get("prefix_length", 0)
                                or metadata.get("suffix_length", 0)
                                or metadata.get("unparsed_content", "")
                                or metadata.get("recovery_attempts", 0)
                            )
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
                            source == "llm" or getattr(message, "from_cortex", False)
                        ):
                            # Some actions failed or JSON was corrupted - request selective correction.
                            # If recovery left extra trailing content, we may have silently dropped
                            # additional actions after executing the ones we could salvage.
                            if len(failed) > 0:
                                log_warning(
                                    f"[message_chain] {len(failed)} actions failed, requesting correction for missing/invalid actions"
                                )
                            elif recovered_with_extra_text:
                                extra_chars = int(
                                    metadata.get("prefix_length", 0)
                                ) + int(metadata.get("suffix_length", 0))
                                log_warning(
                                    "[message_chain] Recovered LLM JSON still had extra trailing content "
                                    f"({extra_chars} chars, {metadata.get('error_count', 0)} parse errors); "
                                    "requesting correction for dropped actions"
                                )
                            else:
                                log_warning(
                                    "[message_chain] JSON recovery requires correction despite no execution failures"
                                )

                            # Check if user response is required but missing.
                            missing_user_reply = False
                            if (
                                is_user_facing
                                and not is_grillo_internal
                                and not is_internal_chat
                                and not is_scoped_non_message
                                and not has_user_response
                                and not has_user_output_action
                                and not delivered_to_llm
                            ):
                                missing_user_reply = True

                            errors_list = list(errors)
                            if missing_user_reply:
                                errors_list.append(
                                    "CHAT REPLY REQUIRED: The user is waiting for a reply in this active conversation turn. "
                                    f"You MUST include a message action targeting the originating interface '{interface_path.split('/')[0]}' "
                                    "(e.g., 'message_telegram_bot') to reply to the user. Internal actions like diary entries "
                                    "and emotion updates do NOT substitute for replying."
                                )

                            # Build correction context with info about what succeeded and what failed
                            correction_context = {
                                "successful_actions": processed,
                                "successful_types": [
                                    (a.get("type") or a.get("action"))
                                    for a in processed
                                    if isinstance(a, dict)
                                ],
                                "failed_actions": failed,
                                "errors": errors_list,
                                "had_json_errors": metadata.get("recovered", False),
                                "original_text": text,
                            }

                            # Store this in the message for the corrector to use
                            if hasattr(message, "__dict__"):
                                message.correction_context = correction_context

                            # Set parsed = None to trigger correction path
                            # But keep the successful actions already executed
                            if (
                                len(failed) > 0
                                or recovered_with_extra_text
                                or missing_user_reply
                            ):
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
                            # All actions succeeded, but check if user response is required and missing
                            if (
                                is_user_facing
                                and not is_grillo_internal
                                and not is_internal_chat
                                and not is_scoped_non_message
                                and not has_user_response
                                and not has_user_output_action
                                and not delivered_to_llm
                            ):
                                log_warning(
                                    f"[message_chain] ⚠️ LLM generated no outbound message action for user-facing interface '{interface_path}' — triggering corrector for missing reply"
                                )
                                correction_context = {
                                    "successful_actions": processed,
                                    "successful_types": [
                                        (a.get("type") or a.get("action"))
                                        for a in processed
                                        if isinstance(a, dict)
                                    ],
                                    "failed_actions": [],
                                    "errors": [
                                        "CHAT REPLY REQUIRED: The user is waiting for a reply in this active conversation turn. "
                                        f"You MUST include a message action targeting the originating interface '{interface_path.split('/')[0]}' "
                                        "(e.g., 'message_telegram_bot') to reply to the user. Internal actions like diary entries "
                                        "and emotion updates do NOT substitute for replying."
                                    ],
                                    "had_json_errors": False,
                                    "original_text": text,
                                }
                                if hasattr(message, "__dict__"):
                                    message.correction_context = correction_context
                                parsed = None
                            else:
                                log_info(
                                    "[message_chain] Actions executed successfully - loop interrupted"
                                )
                                return ACTIONS_EXECUTED

                    except Exception as e:
                        log_warning(f"[message_chain] Failed to run actions: {e}")
                        # On a hard exception during action execution we treat it as a
                        # technical failure. If nothing has run yet, propagate an LLM
                        # failure so the interface can show a fallback message. If some
                        # actions already succeeded we simply report ACTIONS_EXECUTED so
                        # the user isn't spammed with unrelated error texts.
                        if not actions_executed_during_loop:
                            failure_reason = f"Action execution exception: {e}"
                            try:
                                await send_llm_fallback_message(
                                    bot, message, failure_reason, context=ctx
                                )
                            except Exception:
                                pass
                            return LLM_FAILED
                        else:
                            return ACTIONS_EXECUTED

        # Not parsed. Only attempt correction for LLM-origin messages.
        # Non-LLM messages (interface/system source) are blocked without correction.
        # Plain text from the LLM is treated as a correctable error: the corrector
        # will request a valid JSON response from the model.
        if source == "llm":
            log_debug(
                "[message_chain] LLM returned non-JSON output; activating corrector to request JSON format"
            )
        if source != "llm" and not getattr(message, "from_cortex", False):
            log_debug("[message_chain] Non-LLM source; no correction needed")
            return BLOCKED

        # Additional check: if this is already a system error message from corrector, don't re-correct
        if "system_message" in (text or "") and "error" in (text or ""):
            log_debug(
                "[message_chain] Detected system error message from corrector; preventing re-correction loop"
            )
            return BLOCKED

        # Check if the LLM returned a clear server-side error (not fixable by correction).
        # In these cases the corrector would also fail since it hits the same engine, so
        # skip directly to the fallback to avoid wasting minutes of retry time.
        _SERVER_ERROR_MARKERS = (
            "logprobs not supported",
            "internal server error",
            "service unavailable",
            "bad gateway",
            "gateway timeout",
            "503 service unavailable",
            "502 bad gateway",
            "504 gateway timeout",
        )
        if any(marker in (text or "").lower() for marker in _SERVER_ERROR_MARKERS):
            log_warning(
                "[message_chain] LLM returned server-side error; skipping correction loop "
                f"(error: {(text or '')[:200]})"
            )
            if not actions_executed_during_loop:
                await send_llm_fallback_message(
                    bot, message, f"LLM engine error: {(text or '')[:200]}", context=ctx
                )
                return LLM_FAILED
            return ACTIONS_EXECUTED

        attempt += 1
        if attempt > max_retries:
            failure_reason = (
                f"Exhausted {max_retries} correction attempts for invalid JSON"
            )
            if not actions_executed_during_loop:
                log_warning(
                    f"[message_chain] {failure_reason}; sending fallback message"
                )
                await send_llm_fallback_message(
                    bot, message, failure_reason, context=ctx
                )
                return LLM_FAILED
            else:
                log_warning(
                    f"[message_chain] {failure_reason} but {len(actions or [])} action(s) already executed; skipping fallback"
                )
                return ACTIONS_EXECUTED

        if text in tried_texts:
            failure_reason = "Correction loop detected - same text repeated"
            if not actions_executed_during_loop:
                log_warning(
                    f"[message_chain] {failure_reason}; sending fallback message"
                )
                await send_llm_fallback_message(
                    bot, message, failure_reason, context=ctx
                )
                return LLM_FAILED
            else:
                log_warning(
                    f"[message_chain] {failure_reason} but some actions already executed; skipping fallback"
                )
                return ACTIONS_EXECUTED

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
            # Only send fallback if no actions have succeeded; otherwise suppress
            # in accordance with the new policy that at least one delivered
            # message prevents an LLM failure notification.
            if not actions_executed_during_loop:
                await send_llm_fallback_message(
                    bot, message, failure_reason, context=ctx
                )
                return LLM_FAILED
            else:
                log_warning(
                    "[message_chain] Corrector exception but actions already executed; skipping fallback"
                )
                return ACTIONS_EXECUTED

        if not corrected:
            log_debug("[message_chain] Corrector returned no correction this attempt")
            # Check if we're approaching max retries to avoid infinite waiting
            if attempt >= max_retries - 1:
                failure_reason = (
                    f"Corrector returned no correction after {attempt} attempts"
                )
                if not actions_executed_during_loop:
                    log_warning(
                        f"[message_chain] {failure_reason}; sending fallback message"
                    )
                    await send_llm_fallback_message(
                        bot, message, failure_reason, context=ctx
                    )
                    return LLM_FAILED
                else:
                    log_warning(
                        f"[message_chain] {failure_reason} but some actions already executed; skipping fallback"
                    )
                    return ACTIONS_EXECUTED
            # On no-correction, loop and let retry counter enforce blocking
            await asyncio.sleep(0.5)
            continue

        # Accept corrected text and treat it as LLM-origin for next iteration
        log_debug("[message_chain] Received corrected text from LLM; retrying parse")
        text = corrected
        source = "llm"
        ctx["original_text"] = text  # Track in context instead of on message object
        ctx["from_cortex"] = True  # Track in context instead of on message object
        # loop continues


# Backwards-compatible alias
handle_message = handle_incoming_message
