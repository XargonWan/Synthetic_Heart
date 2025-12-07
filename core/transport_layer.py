# core/transport_layer.py
"""Universal transport layer for all interfaces."""

import json
import re
import asyncio
import contextvars
from typing import Any, Dict, Optional
from types import SimpleNamespace
from core.logging_utils import log_debug, log_warning, log_error, log_info

# Interface-specific utilities are loaded dynamically by interfaces.
# The transport layer provides generic messaging functionality only.

# Store last JSON parsing error details for corrector hints
LAST_JSON_ERROR_INFO: Optional[str] = None

# Track chat_ids for which core initiated a system-message correction request
# Format: {chat_id: timestamp} to allow timeout cleanup
_EXPECTING_SYSTEM_REPLY: dict = {}

def _get_system_reply_timeout():
    """Get the system reply timeout from config, default to 10 minutes."""
    try:
        import os
        timeout = int(os.getenv('AWAIT_RESPONSE_TIMEOUT', '600'))
        return timeout
    except (ValueError, TypeError):
        return 600  # 10 minutes default


def _cleanup_expired_system_replies():
    """Remove expired entries from _EXPECTING_SYSTEM_REPLY."""
    import time
    current_time = time.time()
    timeout = _get_system_reply_timeout()
    expired_keys = [
        chat_id for chat_id, timestamp in _EXPECTING_SYSTEM_REPLY.items()
        if current_time - timestamp > timeout
    ]
    for chat_id in expired_keys:
        del _EXPECTING_SYSTEM_REPLY[chat_id]
        log_debug(f"[transport] Removed expired system reply expectation for chat_id={chat_id} (timeout={timeout}s)")


def _add_system_reply_expectation(chat_id: str):
    """Add a system reply expectation with timestamp."""
    import time
    _cleanup_expired_system_replies()  # Clean up expired ones first
    _EXPECTING_SYSTEM_REPLY[chat_id] = time.time()


def _remove_system_reply_expectation(chat_id: str):
    """Remove a system reply expectation."""
    if chat_id in _EXPECTING_SYSTEM_REPLY:
        del _EXPECTING_SYSTEM_REPLY[chat_id]


def _is_expecting_system_reply(chat_id: str) -> bool:
    """Check if we're expecting a system reply for the given chat_id."""
    _cleanup_expired_system_replies()  # Clean up expired ones first
    return chat_id in _EXPECTING_SYSTEM_REPLY


def _format_json_error(text: str, err: json.JSONDecodeError) -> str:
    """Return a helpful message with context around a JSON error."""
    start = max(0, err.pos - 20)
    end = min(len(text), err.pos + 20)
    snippet = text[start:end]
    pointer = " " * (err.pos - start) + "^"
    return (
        f"{err.msg} at line {err.lineno} column {err.colno}\n"
        f"{snippet}\n{pointer}\n"
        "Tip: escape inner quotes with \\\" and ensure proper commas and brackets."
    )


def extract_json_from_text(text: str, return_metadata: bool = False) -> Optional[Dict]:
    """Extract the first valid JSON object or array from text.
    
    This function is smart enough to extract JSON even when LLMs (like Gemini) 
    add extra text before or after the JSON structure. It scans the entire text
    looking for valid JSON objects or arrays, ignoring any surrounding text.
    
    Args:
        text: The text to parse
        return_metadata: If True, returns (json_obj, metadata_dict) tuple
                        If False, returns just json_obj (backward compatible)
    
    Returns:
        If return_metadata=False: JSON object or None
        If return_metadata=True: (JSON object or None, metadata dict)
        
    Metadata dict contains:
        - 'had_errors': bool - True if parsing encountered errors
        - 'error_count': int - Number of parsing errors encountered
        - 'unparsed_content': str - Content that couldn't be parsed (if any)
        - 'recovered': bool - True if JSON was recovered after errors
        - 'had_extra_text': bool - True if text was found before or after JSON
        - 'prefix': str - Text before JSON (if any)
        - 'suffix': str - Text after JSON (if any)
    """
    metadata = {
        'had_errors': False,
        'error_count': 0,
        'unparsed_content': '',
        'recovered': False,
        'had_extra_text': False,
        'prefix': '',
        'suffix': ''
    }
    
    if not text:
        return (None, metadata) if return_metadata else None
    
    # Try to clean up common markdown/formatting issues
    cleaned_text = text.strip()
    
    # Remove markdown code blocks if present
    if cleaned_text.startswith('```json'):
        cleaned_text = cleaned_text[7:]  # Remove ```json
        if cleaned_text.endswith('```'):
            cleaned_text = cleaned_text[:-3]  # Remove ```
        cleaned_text = cleaned_text.strip()
    elif cleaned_text.startswith('```'):
        cleaned_text = cleaned_text[3:]  # Remove ```
        if cleaned_text.endswith('```'):
            cleaned_text = cleaned_text[:-3]  # Remove ```
        cleaned_text = cleaned_text.strip()
    
    # Also try original text in case cleaning broke something
    texts_to_try = [cleaned_text, text.strip()]
    
    decoder = json.JSONDecoder()
    found_json = None
    best_extra_chars = float('inf')  # Track the JSON with least extra content

    for text_variant in texts_to_try:
        log_debug(f"[extract_json_from_text] Trying text variant (length: {len(text_variant)})")

        # First pass: look for clean JSON (no extra text)
        object_start_indices = [i for i, char in enumerate(text_variant) if char == '{']
        array_start_indices = [i for i, char in enumerate(text_variant) if char == '[']
        all_start_indices = sorted(set(object_start_indices + array_start_indices))

        for start in all_start_indices:
            try:
                obj, obj_end = decoder.raw_decode(text_variant[start:])
                obj_end += start
                prefix = text_variant[:start].strip()
                suffix = text_variant[obj_end:].strip()

                # Calculate total extra characters
                extra_chars = len(prefix) + len(suffix)

                # Skip if prefix looks like explanatory text (common LLM patterns)
                if prefix and len(prefix.split()) <= 3:  # Skip prefixes like "json", "here is", etc.
                    prefix_lower = prefix.lower().strip()
                    if prefix_lower in ['json', 'here', 'output', 'response', 'result', 'answer'] or \
                       prefix_lower.startswith(('here is', 'the json', 'json:', 'output:')):
                        log_debug(f"[extract_json_from_text] Skipping JSON with explanatory prefix: '{prefix}'")
                        continue

                # Prefer clean JSON (no extra text)
                if extra_chars == 0:
                    log_debug(f"[extract_json_from_text] ✅ Found clean JSON: {type(obj)}")
                    found_json = obj
                    metadata['had_extra_text'] = False
                    break

                # If we have extra text, keep track of the one with least extra content
                elif extra_chars < best_extra_chars:
                    best_extra_chars = extra_chars
                    found_json = obj
                    metadata['had_extra_text'] = True
                    metadata['prefix'] = prefix
                    metadata['suffix'] = suffix
                    metadata['prefix_length'] = len(prefix)
                    metadata['suffix_length'] = len(suffix)
                    log_debug(f"[extract_json_from_text] Found JSON with {extra_chars} extra chars (best so far)")

            except json.JSONDecodeError as e:
                # Save helpful error info (used by the corrector middleware)
                try:
                    err_msg = _format_json_error(text_variant, e)
                except Exception:
                    err_msg = str(e)

                try:
                    global LAST_JSON_ERROR_INFO
                    LAST_JSON_ERROR_INFO = err_msg
                except Exception:
                    pass

                metadata.setdefault('error_messages', []).append(err_msg)
                log_debug(f"[extract_json_from_text] JSON decode error at position {start}: {e}")
                metadata['had_errors'] = True
                metadata['error_count'] += 1
                continue

        if found_json:
            break
        
        if found_json:
            break
    
    if not found_json:
        log_debug("[extract_json_from_text] No valid JSON found in text")
        log_debug(f"[extract_json_from_text] Text content (first 500 chars): {text[:500]}")
        log_debug(f"[extract_json_from_text] Text content (last 500 chars): {text[-500:]}")
        return (None, metadata) if return_metadata else None

    # Log results based on what we found
    if metadata.get('had_extra_text', False):
        prefix_len = metadata.get('prefix_length', 0)
        suffix_len = metadata.get('suffix_length', 0)
        log_info(f"[extract_json_from_text] ✅ Extracted JSON with {prefix_len + suffix_len} extra chars (prefix: {prefix_len}, suffix: {suffix_len})")
    else:
        log_debug(f"[extract_json_from_text] ✅ Found clean JSON: {type(found_json)}")

    # If we had errors but found JSON, it means we recovered from corruption
    if metadata['had_errors'] and found_json:
        metadata['recovered'] = True
        log_warning(f"[extract_json_from_text] ⚠️ JSON recovered after {metadata['error_count']} parsing errors - may be incomplete")

        # Attempt to recover missing/partially-corrupted actions that the
        # JSON decoder couldn't parse due to poor quoting inside string values
        try:
            # Normalize: if the decoder returned a single action dict instead of
            # a top-level object containing 'actions', convert it into an actions list
            if isinstance(found_json, dict) and 'actions' not in found_json and 'type' in found_json:
                # Make top-level actions list containing the single decoded action
                found_json = {'actions': [found_json]}

            # Only attempt targeted recovery when we have an actions array
            if isinstance(found_json, dict) and 'actions' in found_json and isinstance(found_json['actions'], list):
                _attempt_recover_actions_from_text(text, found_json, metadata)
        except Exception as e:
            log_debug(f"[extract_json_from_text] Action recovery failed: {e}")

    return (found_json, metadata) if return_metadata else found_json


def _attempt_recover_actions_from_text(original_text: str, found_json: dict, metadata: dict):
    """Targeted recovery for common LLM output corruption patterns.

    Current heuristic:
      - If the recovered JSON has an 'actions' list but some action types are
        missing (commonly message_telegram_bot), try to find those action
        blocks in the raw original_text and reconstruct a safe action dict
        by extracting the 'text' value and escaping internal quotes.

    This is intentionally conservative (only reconstructs small, well-known
    message actions) to avoid introducing invalid actions from garbage.

    Mutates found_json in-place when successful and updates metadata.
    """
    try:
        text = original_text or ''
        # Collect types already present
        existing = set()
        try:
            for a in found_json.get('actions', []):
                if isinstance(a, dict) and 'type' in a:
                    existing.add(a['type'])
        except Exception:
            existing = set()

        # Candidate action types we try to recover if missing
        candidates = ['message_telegram_bot', 'message_synth_webui', 'message_ollama_serve', 'message_discord_bot']

        for candidate in candidates:
            if candidate in existing:
                continue

            # Find occurrences of the candidate type in the original text
            for m in re.finditer(rf'"type"\s*:\s*"{re.escape(candidate)}"', text):
                # Locate the payload region after this occurrence
                start_pos = m.start()
                payload_idx = text.find('"payload"', start_pos)
                if payload_idx == -1:
                    continue

                # Find where payload object starts
                brace_idx = text.find('{', payload_idx)
                if brace_idx == -1:
                    continue

                # Try to extract a reasonable slice for the payload by searching
                # for the next '}' that likely terminates the payload object.
                # Use a conservative search window to avoid expensive scans.
                window = text[brace_idx:brace_idx + 8000]  # a large but bounded window

                # Attempt to find the closing brace for this payload using simple balance
                depth = 0
                end_idx = None
                for i, ch in enumerate(window):
                    if ch == '{':
                        depth += 1
                    elif ch == '}':
                        depth -= 1
                        if depth == 0:
                            end_idx = brace_idx + i + 1
                            break

                if end_idx is None:
                    # fallback: try to locate a following known key which likely marks end
                    next_key_pos = len(text)
                    for k in ('"type"', '"actions"', '\n\n'):
                        kp = text.find(k, brace_idx + 1)
                        if kp != -1:
                            next_key_pos = min(next_key_pos, kp)
                    end_idx = next_key_pos

                payload_text = text[brace_idx:end_idx]

                # Attempt to extract 'text' field from payload_text using a heuristic
                text_field_match = re.search(r'"text"\s*:\s*"', payload_text)
                if not text_field_match:
                    # Nothing to recover here
                    continue

                # position of actual text value start
                val_start = text_field_match.end()

                # Find a conservative marker to end the text value: look for common next keys
                markers = ['"interface_path"', '"reply_to_message_id"', '"chat_name"', '"reply_to_message_id"', '"send_in"', '"send_at"']
                marker_pos = None
                search_base = brace_idx + val_start
                for mk in markers:
                    pos = text.find(mk, search_base)
                    if pos != -1:
                        marker_pos = pos
                        break

                if marker_pos is None:
                    # fallback: look for the end of payload object
                    marker_pos = end_idx

                raw_value = text[brace_idx + val_start:marker_pos]

                # Trim trailing characters that may belong to JSON punctuation (like ",)
                raw_value = raw_value.rstrip(' ,\n\r\t}')

                # Remove a possible leading quote or stray characters
                if raw_value.startswith('"'):
                    raw_value = raw_value[1:]
                if raw_value.endswith('"'):
                    raw_value = raw_value[:-1]

                recovered_text = raw_value.strip()

                if not recovered_text:
                    continue

                # Escape double quotes inside recovered_text to make it safe for JSON
                safe_text = recovered_text.replace('"', '\\"')

                # Try to extract interface_path similarly (optional)
                interface_path = None
                ip_match = re.search(r'"interface_path"\s*:\s*"([^"]*)"', payload_text)
                if ip_match:
                    interface_path = ip_match.group(1)

                # Build recovered action and append
                recovered_action = {
                    'type': candidate,
                    'payload': {
                        'text': safe_text
                    }
                }
                if interface_path:
                    recovered_action['payload']['interface_path'] = interface_path

                # Append recovered action only if not present already
                found_json.setdefault('actions', [])
                found_json['actions'].append(recovered_action)
                metadata['recovered'] = True
                metadata['recovery_attempts'] = metadata.get('recovery_attempts', 0) + 1
                log_info(f"[extract_json_from_text] Recovered missing action {candidate} (heuristic)")
                # Break after first recovery for this candidate to avoid duplicates
                break
    except Exception as e:
        log_debug(f"[attempt_recover] Recovery routine error: {e}")




async def universal_send(interface_send_func, *args, text: str = None, **kwargs):
    """
    Universal send function that intercepts JSON actions and parses them.
    
    Args:
        interface_send_func: The actual send function of the interface (e.g., bot.send_message)
        *args: Positional arguments for the interface send function
        text: The text to send (required parameter)
        **kwargs: Additional keyword arguments for the interface send function
    """
    if text is None:
        text = ""

    # Diagnostic: log interface function and runtime send parameters
    try:
        bot_self = getattr(interface_send_func, '__self__', None)
        log_debug(f"[transport] universal_send called: interface_send_func={interface_send_func} bot_self={bot_self} args={args} kwargs_keys={list(kwargs.keys())}")
    except Exception as _:
        log_debug("[transport] universal_send diagnostic logging failed to inspect interface_send_func")

    # Map thread_id to message_thread_id for Telegram API compatibility
    if 'thread_id' in kwargs:
        thread_id = kwargs.pop('thread_id')
        # Convert thread_id to int if it's a string (for Telegram API compatibility)
        if isinstance(thread_id, str) and thread_id.isdigit():
            thread_id = int(thread_id)
        kwargs['message_thread_id'] = thread_id

    # Save LLM response to chat history (before JSON parsing)
    # Extract interface_path for saving to chat history
    interface_path = kwargs.get('interface_path')
    log_debug(f"[transport] Checking for chat history save: interface_path={interface_path}, text_len={len(text) if text else 0}")
    
    if interface_path and text:
        try:
            from core.chat_context_manager import add_message_to_context
            from datetime import datetime

            # Extract JSON and metadata to decide what (if any) human-readable
            # companion text should be saved alongside LLM-originated JSON.
            json_payload, json_meta = extract_json_from_text(text, return_metadata=True)
            log_debug(f"[transport] JSON data extracted: {json_payload is not None} metadata: {bool(json_meta)}")

            # If JSON present and metadata indicates extra text (prefix/suffix), use those.
            if json_payload is None:
                text_content = text
            else:
                if json_meta and (json_meta.get('prefix') or json_meta.get('suffix')):
                    # Compose companion text from prefix+suffix
                    parts = []
                    if json_meta.get('prefix'):
                        parts.append(json_meta.get('prefix').strip())
                    if json_meta.get('suffix'):
                        parts.append(json_meta.get('suffix').strip())
                    text_content = "\n".join(parts).strip()
                else:
                    # Pure JSON only — nothing to save as user-visible content
                    text_content = ""
            log_debug(f"[transport] Text content for history: {len(text_content)} chars (was {len(text)})")
            
            if text_content:  # Only save if there's actual message content
                log_info(f"[transport] 📝 Saving LLM response to chat history: {interface_path}")
                await add_message_to_context(
                    interface_path=interface_path,
                    message_text=text_content,
                    sender_name="self",
                    sender_id="synth",
                    timestamp=datetime.now().isoformat()
                )
                log_info(f"[transport] ✅ LLM response saved to chat history for {interface_path}")
            else:
                log_debug(f"[transport] No text content to save (only JSON)")
        except Exception as e:
            log_error(f"[transport] ❌ Failed to save LLM response to chat history: {e}")
            import traceback
            log_debug(f"[transport] Traceback: {traceback.format_exc()}")

    # Filter out internal parameters that should not be passed to interface send functions
    excluded_params = {'event_id', 'interface', 'is_llm_response', 'context', 'error_retry_policy', 'interface_path'}
    for param in excluded_params:
        kwargs.pop(param, None)

    # Log LLM response for debugging
    if text:
        preview = text[:200] + "..." if len(text) > 200 else text
        log_info(f"[transport] 🤖 LLM Response: {preview}")
    # Flow trace: record that transport received LLM output
    try:
        log_debug(f"[flow] transport.received -> text_len={len(text)}")
    except Exception:
        pass

    # Extract JSON from text
    json_data = extract_json_from_text(text)
    if json_data:
        log_debug(f"[transport] Detected JSON data, parsing: {json_data}")
        try:
            log_debug(f"[flow] transport.detects_json -> will attempt run_actions")
            # Handle new nested actions format
            if isinstance(json_data, dict) and "actions" in json_data:
                actions = json_data["actions"]
                if not isinstance(actions, list):
                    log_warning("[transport] actions field must be a list")
                    return await interface_send_func(*args, text=text, **kwargs)
            # Fallback to legacy array format
            elif isinstance(json_data, list):
                actions = json_data
            # Fallback to legacy single action format
            elif isinstance(json_data, dict) and "type" in json_data:
                actions = [json_data]
            else:
                log_warning(f"[transport] Unrecognized JSON structure: {json_data}")
                return await interface_send_func(*args, text=text, **kwargs)

            bot = getattr(interface_send_func, '__self__', None) or (
                args[0] if args and hasattr(args[0], 'send_message') else None
            )

            if not bot:
                log_warning("[transport] Could not extract bot instance for action parsing")
                return await interface_send_func(*args, text=text, **kwargs)

            # Create message context for actions
            message = SimpleNamespace()
            chat_id_value = kwargs.get('chat_id') or (args[0] if args else None)

            # Accept int or numeric string chat IDs (some interfaces provide strings)
            if chat_id_value is None or not isinstance(chat_id_value, (int, str)):
                log_warning(f"[transport] Invalid chat_id for action processing: {chat_id_value}")
                return await interface_send_func(*args, text=text, **kwargs)

            # Coerce numeric string to int when possible for internal consistency
            if isinstance(chat_id_value, str) and chat_id_value.strip().lstrip('-').isdigit():
                try:
                    chat_id_value = int(chat_id_value)
                except Exception:
                    # Keep as string if coercion fails
                    pass

            message.chat_id = chat_id_value
            message.text = ""
            message.original_text = text
            message.thread_id = kwargs.get('thread_id')
            from datetime import datetime
            message.date = datetime.utcnow()

            # Use centralized action system for all action types.  The action
            # parser is optional, so import it lazily and fall back to sending
            # plain text if it's unavailable.
            try:
                from core.action_parser import run_actions  # type: ignore
            except Exception as e:  # pragma: no cover - executed when action_parser missing
                log_warning(f"[transport] action parser unavailable: {e}")
                return await interface_send_func(*args, text=text, **kwargs)

            # Determine current interface from bot
            current_interface = "unknown"  # default fallback
            if hasattr(bot, 'get_interface_id'):
                try:
                    current_interface = bot.get_interface_id()
                except Exception as e:
                    log_debug(f"[transport] Could not get interface_id from bot: {e}")
            
            context = {
                "interface": current_interface,
                "original_chat_id": chat_id_value,
                "original_thread_id": kwargs.get('thread_id'),
                "original_text": text[:500] if text else "",  # Include preview of original text
                "thread_defaults": {
                    "default": None
                }
            }  # Add more context as needed

            # Filter actions to only include those for the current interface
            current_interface_actions = []
            cross_interface_actions = []
            
            for action in actions:
                action_type = action.get("type", "")
                # Check if action belongs to current interface
                if action_type.endswith(f"_{current_interface}"):
                    current_interface_actions.append(action)
                else:
                    cross_interface_actions.append(action)
                    log_debug(f"[transport] Skipping cross-interface action {action_type} (current: {current_interface})")
            
            if cross_interface_actions:
                log_info(f"[transport] Filtered {len(cross_interface_actions)} cross-interface actions, processing {len(current_interface_actions)} for {current_interface}")
            
            # Only process actions for current interface
            if current_interface_actions:
                try:
                    result = await run_actions(current_interface_actions, context, bot, message)
                    processed_actions = result.get("processed", []) if isinstance(result, dict) else current_interface_actions
                except Exception as e:
                    log_warning(f"[transport] Failed to process actions: {e}")
                    processed_actions = []
                finally:
                    log_debug(f"[flow] transport.after_run_actions -> processed_count={len(processed_actions)}")

                log_info(
                    f"[transport] Processed {len(processed_actions)} unique JSON actions via plugin system"
                )
                
                # After processing actions, check if there's text outside the JSON that should be sent
                if json_metadata and (json_metadata.get('prefix') or json_metadata.get('suffix')):
                    companion_text = ""
                    
                    # Combine prefix and suffix, removing duplicate content
                    if json_metadata.get('prefix'):
                        companion_text = json_metadata.get('prefix', '').strip()
                    
                    if json_metadata.get('suffix'):
                        suffix_text = json_metadata.get('suffix', '').strip()
                        if companion_text:
                            companion_text += "\n" + suffix_text
                        else:
                            companion_text = suffix_text
                    
                    if companion_text:
                        log_info(f"[transport] Sending companion text alongside JSON actions: {len(companion_text)} chars")
                        # Send the companion text as a separate message after the actions
                        await interface_send_func(*args, text=companion_text, **kwargs)
                
                return
            else:
                # No actions for current interface, send as plain text
                log_debug(f"[transport] No actions for current interface {current_interface}, sending as plain text")
                return await interface_send_func(*args, text=text, **kwargs)

        except Exception as e:
            log_warning(f"[transport] Failed to process JSON actions: {e}")

    # Non-JSON plain text — forward directly. Corrector is only run in llm_to_interface.
    # NOTE: If LLM output contains no JSON and no actions, it will be forwarded as plain text.
    # The LLM instructions are clear that ANY TEXT OUTSIDE JSON WILL BE DISCARDED,
    # so LLMs should only output JSON. If they output plain text anyway, it still gets sent,
    # but companion text from JSON extraction does NOT get sent in this path.
    if text and not text.startswith(("[ERROR]", "[WARNING]", "[INFO]", "[DEBUG]")):
        log_debug(f"[flow] transport.non_json -> forwarding plain text (no JSON detected, no corrector triggered) chat_id={kwargs.get('chat_id')}")
        return await interface_send_func(*args, text=text, **kwargs)

    # Send as normal text
    log_debug(f"[flow] transport.forward_plain -> calling interface_send_func for chat_id={kwargs.get('chat_id')}")
    return await interface_send_func(*args, text=text, **kwargs)


# Re-entry guard removed: rely solely on the corrector retry counter to avoid loops

def _get_attempted_action_full_description(error_text: str) -> Optional[Dict[str, Any]]:
    """Extract which action was attempted and return its FULL description.
    
    When the corrector is triggered due to JSON parsing errors, this function:
    1. Tries to parse the partial/corrupted JSON to identify the action type attempted
    2. Looks up the FULL (non-minified) action description from the core_initializer
    3. Returns the full description so the corrector can provide complete details
    
    Args:
        error_text: The malformed JSON text that the LLM tried to produce
        
    Returns:
        Dict with keys:
            - 'action_type': str - The action type that was attempted (e.g., 'message_telegram_bot')
            - 'full_description': dict - The complete, non-minified action schema with all details
        Or None if we can't identify an action
    """
    try:
        # Try to extract partial JSON to find action type
        import re
        
        # Look for "type": "..." or "action": "..." patterns
        type_match = re.search(r'"type"\s*:\s*"([^"]+)"', error_text)
        if not type_match:
            type_match = re.search(r'"action"\s*:\s*"([^"]+)"', error_text)
        
        if not type_match:
            log_debug("[_get_attempted_action] Could not extract action type from error text")
            return None
        
        action_type = type_match.group(1)
        log_debug(f"[_get_attempted_action] Identified attempted action type: {action_type}")
        
        # Now get the FULL description from core_initializer (not minified)
        try:
            from core.core_initializer import core_initializer
            
            full_actions = core_initializer.actions_block.get("available_actions", {})
            
            if action_type in full_actions:
                full_description = full_actions[action_type]
                log_debug(f"[_get_attempted_action] Found full description for {action_type}")
                return {
                    'action_type': action_type,
                    'full_description': full_description
                }
            else:
                log_debug(f"[_get_attempted_action] Action type '{action_type}' not found in available actions")
                return None
        except Exception as e:
            log_debug(f"[_get_attempted_action] Could not load full action description: {e}")
            return None
            
    except Exception as e:
        log_debug(f"[_get_attempted_action] Error analyzing attempted action: {e}")
        return None


async def run_corrector_middleware(text: str, bot=None, context: dict = None, chat_id=None, thread_id=None) -> str:
    """Attempt to obtain a corrected LLM output that contains valid JSON actions.

    Strategy:
    - If the text already contains valid JSON, return it immediately.
    - Otherwise, attempt up to CORRECTOR_RETRIES to ask the active LLM plugin to produce
      a corrected reply. If the plugin returns a reply that contains valid JSON, return it.
    - If no correction succeeds, return None to indicate blocking.

    This function is intentionally best-effort and non-blocking for the system: if no
    active LLM plugin is available it will log and return None.
    
    Args:
        text: The text to correct
        bot: The bot instance
        context: Context dictionary
        chat_id: The chat ID for the correction
        thread_id: The thread ID to maintain conversation continuity (optional)
    """
    try:
        # Avoid circular imports at module import time
        from core import action_parser
        from core.logging_utils import log_debug, log_info, log_warning
        from core.prompt_engine import build_full_json_instructions
        from types import SimpleNamespace
    except Exception as e:
        try:
            log_warning(f"[corrector_middleware] Import error: {e}")
        except Exception:
            pass
        return None

    max_retries = getattr(action_parser, 'CORRECTOR_RETRIES', 2)

    # Extract message from context if available
    message = context.get('message') if context else None
    
    # Check if we have selective correction context (some actions succeeded, some failed)
    correction_context = getattr(message, 'correction_context', None) if message else None
    
    # If already valid JSON WITHOUT errors, nothing to do
    try:
        json_obj, metadata = extract_json_from_text(text, return_metadata=True)
        if json_obj and not metadata.get('had_errors', False) and not correction_context:
            log_debug("[corrector_middleware] Input contains clean JSON; skipping correction")
            return text
        elif json_obj and metadata.get('had_errors', False):
            log_debug(f"[corrector_middleware] JSON recovered with {metadata.get('error_count', 0)} errors; proceeding with correction")
    except Exception:
        pass

    # Don't correct system messages or messages that clearly don't need correction
    if text and ("system_message" in text or text.startswith(("[ERROR]", "[WARNING]", "[INFO]", "[DEBUG]"))):
        log_debug("[corrector_middleware] Text appears to be system message; skipping correction to prevent loops")
        return None

    last_error_hint = LAST_JSON_ERROR_INFO or "Invalid or missing JSON"

    llm_plugin = None  # Store the plugin for later use

    for attempt in range(1, max_retries + 1):
        try:
            # Try to get active plugin instance
            try:
                import core.plugin_instance as plugin_instance
            except Exception:
                plugin_instance = None

            llm_plugin = None
            if plugin_instance is not None:
                # plugin_instance may export get_plugin() or plugin attribute
                llm_plugin = getattr(plugin_instance, 'get_plugin', lambda: None)()
                if llm_plugin is None:
                    llm_plugin = getattr(plugin_instance, 'plugin', None)

            # Log plugin discovery
            try:
                if llm_plugin is None:
                    log_debug(f"[corrector_middleware] attempt {attempt}: no active LLM plugin found")
                else:
                    log_debug(f"[corrector_middleware] attempt {attempt}: using LLM plugin {getattr(llm_plugin, '__class__', llm_plugin)}")
            except Exception:
                pass

            if not llm_plugin or not hasattr(llm_plugin, 'handle_incoming_message'):
                log_warning(f"[corrector_middleware] No active LLM plugin available (attempt {attempt}/{max_retries})")
                # no plugin available — stop trying and indicate failure (do NOT send to interface)
                log_debug("[corrector_middleware] No LLM available; blocking and returning None")
                return None

            # Build a correction payload similar to action_parser.corrector
            # NOTE: We do NOT include full_json_instructions here because:
            # 1. It's HUGE (all available actions with full descriptions)
            # 2. Corrector only needs the format template, not all actions
            # 3. LLM just needs to fix the JSON structure, not learn new actions
            attempted_action_info = None
            
            # Try to identify which action was attempted and include its full description
            try:
                attempted_action_info = _get_attempted_action_full_description(text)
                if attempted_action_info:
                    log_info(f"[corrector_middleware] Including full description for attempted action: {attempted_action_info['action_type']}")
            except Exception as e:
                log_debug(f"[corrector_middleware] Could not extract attempted action info: {e}")

            # Use the thread_id from the original conversation if available
            # The interface (e.g., telegram_bot) should have already normalized message_thread_id to thread_id
            payload_thread_id = thread_id if thread_id is not None else 0

            # Extract the original user message text from context if available
            # This is CRUCIAL: when the corrector retries, the LLM needs to know what the
            # original user question was, not just that "your JSON was invalid". Without this,
            # the LLM might respond to the correction instructions themselves instead of
            # answering the user's original question.
            # Example: User asks "Rekku ci sei?" -> LLM fails -> Corrector sends correction
            #          With original_user_message, LLM knows to respond "Sì, ci sono ♡"
            #          Without it, LLM might respond "Acknowledged, JSON was valid"
            original_user_message = ""
            if message:
                original_user_message = getattr(message, 'text', "")
            
            # Build correction message based on whether we have selective correction context
            if correction_context:
                # Selective correction: tell LLM what succeeded and what needs fixing
                successful = correction_context.get('successful_actions', [])
                failed = correction_context.get('failed_actions', [])
                errors = correction_context.get('errors', [])
                
                correction_message_text = (
                    f"PARTIAL SUCCESS - Some actions completed, others failed.\n\n"
                    f"✅ Successfully executed {len(successful)} action(s):\n"
                )
                for action in successful:
                    action_type = action.get('type', 'unknown')
                    correction_message_text += f"  - {action_type}\n"
                
                correction_message_text += (
                    f"\n❌ Failed {len(failed)} action(s) that need correction:\n"
                )
                for failed_item in failed:
                    action = failed_item.get('action', {})
                    action_errors = failed_item.get('errors', [])
                    action_type = action.get('type', 'unknown')
                    correction_message_text += f"  - {action_type}: {', '.join(action_errors)}\n"
                
                correction_message_text += (
                    f"\nRequirements:\n"
                    f"1. Respond with ONLY valid JSON\n"
                    f"2. Include ONLY the missing/failed actions - do NOT repeat successful ones\n"
                    f"3. Fix validation errors in the failed actions\n"
                    f"4. Ensure you've created ALL actions from the user's original request\n"
                )
            else:
                # Full correction: complete JSON failure
                correction_message_text = (
                    f"CRITICAL ERROR: Your previous response was not valid JSON or incomplete. "
                    f"Requirements:\n"
                    f"1. Respond with ONLY valid JSON (no explanations)\n"
                    f"2. Complete ALL actions requested by the user in their original message\n"
                    f"3. Fix any JSON syntax errors\n"
                    f"Error details: {last_error_hint}"
                )
            
            correction_payload = {
                "system_message": {
                    "type": "error",
                    "message": correction_message_text,
                    "your_reply": text,
                    "original_user_message": original_user_message,
                    "chat_id": chat_id,
                    "thread_id": payload_thread_id,
                }
            }
            
            # If we identified an action attempt, include its complete (non-minified) description
            if attempted_action_info:
                correction_payload["system_message"]["attempted_action"] = attempted_action_info["action_type"]
                correction_payload["system_message"]["action_full_schema"] = attempted_action_info["full_description"]
                log_debug(f"[corrector_middleware] Added full action schema for {attempted_action_info['action_type']}")
            
            # Add required format examples
            correction_payload["system_message"]["required_format"] = {
                "actions": [
                    {
                        "type": "message_telegram_bot",
                        "payload": {
                            "text": "Your message content here (optional - only if you want to reply to user)",
                            "interface_path": f"telegram_bot/{chat_id or '-1003098886330'}/{payload_thread_id or 0}"
                        }
                    }
                ]
            }
            correction_payload["system_message"]["strict_requirements"] = [
                "MUST start with { and end with }",
                "MUST contain 'actions' array",
                "NO text outside JSON structure",
                "NO markdown formatting",
                "NO explanations outside JSON"
            ]
            correction_prompt = json.dumps(correction_payload, ensure_ascii=False)

            # Construct a lightweight message object expected by plugins
            # CRITICAL: Preserve interface_path from original message/context so error messages route correctly
            correction_message = SimpleNamespace()
            correction_message.chat_id = chat_id or getattr(bot, 'chat_id', None) or -1
            correction_message.text = correction_prompt
            correction_message.thread_id = payload_thread_id  # Use thread_id from original conversation
            correction_message.date = None
            correction_message.from_user = None
            correction_message.chat = SimpleNamespace(id=correction_message.chat_id, type='private')
            # Extract interface_path from original message or context
            correction_message.interface_path = None
            if message and hasattr(message, 'interface_path'):
                correction_message.interface_path = message.interface_path
            elif context and 'interface_path' in context:
                correction_message.interface_path = context['interface_path']

            log_debug(f"[corrector_middleware] Requesting correction from LLM (attempt {attempt}/{max_retries}) - chat_id={correction_message.chat_id}, thread_id={correction_message.thread_id}")

            # Mark that we are expecting a system reply for this chat_id so llm_to_interface can
            # consume it without forwarding and avoid re-entry loops.
            try:
                if correction_message.chat_id is not None:
                    _add_system_reply_expectation(str(correction_message.chat_id))
            except Exception:
                pass

            # Call plugin directly and await returned value when available
            try:
                corrected = await llm_plugin.handle_incoming_message(bot, correction_message, correction_prompt)
            except TypeError:
                # Some plugins expect different signature; try fallback
                corrected = await llm_plugin.handle_incoming_message(bot, correction_message, correction_prompt)

            # Log corrected result type/length
            try:
                log_debug(f"[corrector_middleware] LLM returned type={type(corrected)} len={(len(corrected) if isinstance(corrected, str) else 'N/A')}")
            except Exception:
                pass

            if corrected and isinstance(corrected, str):
                log_debug(f"[corrector_middleware] LLM returned text len={len(corrected)}")
                # If corrected contains valid JSON, return it (do not echo to chat)
                try:
                    if extract_json_from_text(corrected):
                        log_info("[corrector_middleware] Received corrected JSON from LLM")
                        return corrected
                except Exception:
                    pass

                # Not valid JSON yet — DO NOT send LLM suggestion to chat (policy)
                log_debug("[corrector_middleware] LLM suggestion is not valid JSON; will retry without echoing to interface")

                # Update text with LLM reply and retry
                text = corrected

            # small backoff between attempts
            await asyncio.sleep(1)

        except Exception as e:
            try:
                log_warning(f"[corrector_middleware] attempt {attempt} failed: {e}")
            except Exception:
                pass
            await asyncio.sleep(1)

    # Exhausted retries — send error message if possible
    log_warning(f"[corrector_middleware] Exhausted {max_retries} attempts without valid JSON; blocking message for chat_id={chat_id}")
    if llm_plugin and hasattr(llm_plugin, '_send_error_message') and message:
        try:
            await llm_plugin._send_error_message(bot, message)
        except Exception as e:
            log_warning(f"[corrector_middleware] Failed to send error message: {e}")
    # Cleanup expectation for this chat in case it wasn't consumed
    try:
        if chat_id is not None:
            _remove_system_reply_expectation(str(chat_id))
    except Exception:
        pass
    return None


async def interface_to_llm(send_to_llm_func, *args, text: str = None, **kwargs):
    """Entry point for messages going from an interface/plugin TO the LLM.

    This is a thin wrapper that records direction and forwards the call to the
    LLM-facing send function provided by a plugin. It does NOT run the
    corrector.
    """
    try:
        log_debug(f"[chain] interface_to_llm called -> send_to_llm_func={send_to_llm_func} kwargs_keys={list(kwargs.keys())}")
    except Exception:
        pass
    return await send_to_llm_func(*args, text=text, **kwargs)


async def llm_to_interface(interface_send_func, *args, text: str = None, **kwargs):
    """Entry point for messages coming FROM the LLM TO an interface.

    Responsibilities:
    - Ensure a single LLM->interface path for all model outputs (centralized diagnostics)
    - Run the corrector middleware (with retries) only for LLM outputs that are non-empty
    - Forward the (possibly corrected) text into the transport via `universal_send`
    """
    if text is None:
        text = ""

    # Extract chat_id for logging
    chat_id = kwargs.get('chat_id')
    if chat_id is None and args:
        maybe = args[1] if len(args) > 1 else None
        chat_id = maybe if isinstance(maybe, (int, str)) else None

    try:
        log_debug(f"[llm_to_interface] Delivering message to chat_id={chat_id}")
        log_debug(f"[llm_to_interface] Message: {text}")
    except Exception:
        pass

    # Clean up expired system reply expectations
    _cleanup_expired_system_replies()

    try:
        log_debug(f"[chain] llm_to_interface called -> interface_send_func={interface_send_func}")
    except Exception:
        pass

    # Mark this forwarding attempt explicitly as an LLM response so downstream
    # components and the orchestrator can detect origin without changes to every
    # LLM engine. Engines that already set this can override it.
    try:
        kwargs.setdefault('is_llm_response', True)
    except Exception:
        pass

    # Suppress empty/whitespace LLM replies early — centralize handling here
    if kwargs.get('is_llm_response', False) and (not text or not text.strip()):
        log_debug("[transport] Empty LLM response received; skipping corrector and not forwarding")
        return None

    # If the LLM sent a JSON-like payload that looks like a correction/system message,
    # handle it via the parser orchestrator to avoid echoing it back into the interfaces
    try:
        json_payload = None
        json_metadata = None
        if text and text.strip():
            try:
                json_payload, json_metadata = extract_json_from_text(text, return_metadata=True)
            except Exception:
                json_payload = None
                json_metadata = None

        # Check if JSON was corrupted during parsing
        is_corrupted = json_metadata and (json_metadata.get('recovered', False) or json_metadata.get('unparsed_content', ''))
        
        if is_corrupted:
            log_warning(f"[llm_to_interface] 🔧 Corrupted JSON detected - activating corrector to regenerate damaged actions")
            log_debug(f"[llm_to_interface] Unparsed content ({len(json_metadata.get('unparsed_content', ''))} chars): {json_metadata.get('unparsed_content', '')[:200]}...")
        
        # Check if LLM returned plain text with NO JSON at all (violates LLM instructions)
        is_plain_text_only = (not json_payload and text and text.strip())
        if is_plain_text_only:
            log_warning(f"[llm_to_interface] ⚠️ LLM returned plain text with NO JSON - violates instructions! Activating corrector to request JSON format")
        
        # Detect correction/system payloads (top-level "system_message") OR corrupted JSON OR plain text only
        if (isinstance(json_payload, dict) and 'system_message' in json_payload) or is_corrupted or is_plain_text_only:
            try:
                # Determine bot instance from args if present
                bot = args[0] if args and len(args) > 0 else None

                # Attempt to determine chat_id from kwargs or positional args
                chat_id = kwargs.get('chat_id')
                if chat_id is None and len(args) > 1:
                    maybe = args[1]
                    if isinstance(maybe, (int, str)):
                        chat_id = maybe

                # Build lightweight message and context objects for orchestrator
                from types import SimpleNamespace
                from datetime import datetime

                message = SimpleNamespace()
                message.chat_id = chat_id
                message.text = ""
                message.original_text = text
                message.thread_id = kwargs.get('thread_id')
                # Mark this message as originating from the LLM so the orchestrator
                # will process it. This complements the is_llm_response flag.
                message.from_llm = True
                message.date = datetime.utcnow()

                current_interface = kwargs.get('interface') or (getattr(bot, 'get_interface_id', lambda: 'unknown')() if bot else 'unknown')
                corrector_context = {
                    'interface': current_interface,
                    'original_chat_id': chat_id,
                    'original_thread_id': kwargs.get('thread_id'),
                    'original_text': text[:500] if text else ''
                }
                
                # If JSON is corrupted, extract already-completed actions from the recovered JSON
                completed_actions = []
                if is_corrupted and json_payload:
                    log_info("[llm_to_interface] Extracting completed actions from corrupted JSON")
                    # Extract actions from recovered JSON
                    if isinstance(json_payload, dict) and "actions" in json_payload:
                        recovered_actions = json_payload.get("actions", [])
                        if isinstance(recovered_actions, list):
                            completed_actions = [a.get('type') for a in recovered_actions if isinstance(a, dict) and 'type' in a]
                            log_info(f"[llm_to_interface] Found {len(completed_actions)} actions in recovered JSON: {completed_actions}")

                # Delegate to the parser orchestrator (will call corrector middleware as needed)
                from core import action_parser
                orchestrator_result = await action_parser.corrector_orchestrator(
                    text, 
                    corrector_context, 
                    bot, 
                    message,
                    completed_actions=completed_actions if is_corrupted else None,
                    force_correction=is_plain_text_only  # Force corrector to run for plain text
                )

                if orchestrator_result is True:
                    log_debug('[llm_to_interface] corrector_orchestrator executed actions; not forwarding text')
                    return None
                elif orchestrator_result is False:
                    log_warning('[llm_to_interface] corrector_orchestrator blocked message; not forwarding')
                    return None
                else:
                    log_debug('[llm_to_interface] corrector_orchestrator returned None; forwarding as usual')

            except Exception as e:
                log_warning(f"[llm_to_interface] Orchestrator handling failed: {e}")
                # On failure, avoid forwarding suspicious payload to interface
                return None

    except Exception:
        # Defensive: any unexpected issue parsing LLM output should not crash forwarding
        pass

    # Forward into the neutral universal send which will parse JSON/actions
    # Delegate to central message chain for unified handling of LLM-origin and interface-origin messages
    try:
        from core.message_chain import handle_incoming_message
        # Determine source: if this path was called from llm_to_interface we set is_llm_response
        source = 'llm' if kwargs.get('is_llm_response', False) else 'interface'
        # Build a message object compatible with message_chain
        from types import SimpleNamespace
        from datetime import datetime
        message = SimpleNamespace()
        # Try to extract chat_id from kwargs/args
        chat_id = kwargs.get('chat_id')
        if chat_id is None and args:
            maybe = args[1] if len(args) > 1 else None
            chat_id = maybe if isinstance(maybe, (int, str)) else None
        message.chat_id = chat_id
        message.text = ""
        message.original_text = text
        message.thread_id = kwargs.get('thread_id')
        message.date = datetime.utcnow()
        # If this chat_id is marked as expecting a system/LLM reply from the
        # corrector middleware, consume it here and do not re-enter the message
        # chain. This avoids infinite correction/forwarding loops.
        # However, only consume if the message actually looks like a correction response.
        try:
            if chat_id is not None and _is_expecting_system_reply(str(chat_id)):
                # Check if this is actually a correction response by looking for typical patterns
                is_correction_response = (
                    isinstance(text, str) and (
                        "system_message" in text or
                        text.strip().startswith('{"actions":') or
                        "correction" in text.lower() or
                        "corrected" in text.lower()
                    )
                )
                
                if is_correction_response:
                    _remove_system_reply_expectation(str(chat_id))
                    log_debug(f"[llm_to_interface] Consuming expected system reply for chat_id={chat_id}; not forwarding to message_chain")
                    return None
                else:
                    # This doesn't look like a correction response, might be a normal message
                    # Log a warning and let it through
                    log_warning(f"[llm_to_interface] Expected system reply for chat_id={chat_id} but message doesn't look like correction. Allowing through.")
                    _remove_system_reply_expectation(str(chat_id))  # Clear the expectation anyway
        except Exception:
            pass
        # Pass through to message chain
        result = await handle_incoming_message(bot=getattr(interface_send_func, '__self__', None) or None, message=message, text=text, source=source, context=kwargs.get('context', None), **kwargs)
        if result == 'ACTIONS_EXECUTED' or result == 'BLOCKED':
            # Orchestrator handled or blocked: nothing to forward
            return None
        elif result == 'LLM_FAILED':
            # LLM failed and fallback message was already sent: nothing more to forward
            return None
        # Else forward as usual
        return await universal_send(interface_send_func, *args, text=text, **kwargs)
    except Exception as e:
        log_warning(f"[transport] message_chain delegation failed: {e}")
        return await universal_send(interface_send_func, *args, text=text, **kwargs)
