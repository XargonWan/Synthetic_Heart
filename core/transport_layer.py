# core/transport_layer.py
"""Universal transport layer for all interfaces."""

import json
import re
import asyncio
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


# Helpers for sanitizing and extracting JSON from noisy LLM outputs
def _remove_control_chars(s: str) -> str:
    """Remove non-printable control characters that commonly break JSON parsing.
    Keep common whitespace (\n, \r, \t)."""
    # Remove C0 control chars except tab/newline/carriage return (0x09,0x0a,0x0d)
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)


def _strip_stacktraces_and_addresses(s: str) -> str:
    """Remove obvious stacktrace blocks and lines containing raw addresses to reduce noise.
    This is conservative: we remove blocks starting with 'Traceback' or 'Stacktrace' and
    isolated lines containing '0x' hex addresses which commonly appear in native traces.
    """
    # Remove common Python Traceback blocks
    s2 = re.sub(r"(?is)traceback.*?(?=\n\s*\n|$)", "", s)

    # Remove generic 'Stacktrace:' blocks
    s2 = re.sub(r"(?is)stacktrace:.*?(?=\n\s*\n|$)", "", s2)

    # Remove long blocks that look like hex-address stacks (lines with 0x..)
    s2 = re.sub(r"(?m)^.*0x[0-9a-fA-F]+.*\n?", "", s2)

    return s2


def _find_json_substrings(s: str, max_window: int = 20000):
    """Find candidate JSON substrings by scanning for balanced braces/brackets.
    Returns generator of (start, end, substring).
    The scan is bounded to avoid pathological worst-cases."""
    n = len(s)
    for i, ch in enumerate(s):
        if ch not in "{[":
            continue
        depth = 0
        start = i
        # Limit scan window
        end_limit = min(n, i + max_window)
        for j in range(i, end_limit):
            cj = s[j]
            if cj == ch:
                depth += 1
            elif (ch == "{" and cj == "}") or (ch == "[" and cj == "]"):
                depth -= 1
                if depth == 0:
                    yield (start, j + 1, s[start : j + 1])
                    break


def _get_system_reply_timeout():
    """Get the system reply timeout from config, default to 10 minutes."""
    try:
        # Prefer config_registry so UI/API changes are respected at runtime
        from core.config_manager import config_registry

        timeout = int(config_registry.get_value("AWAIT_RESPONSE_TIMEOUT", 600))
        return timeout
    except (ValueError, TypeError):
        return 600  # 10 minutes default


def _cleanup_expired_system_replies():
    """Remove expired entries from _EXPECTING_SYSTEM_REPLY."""
    import time

    current_time = time.time()
    timeout = _get_system_reply_timeout()
    expired_keys = [
        chat_id
        for chat_id, timestamp in _EXPECTING_SYSTEM_REPLY.items()
        if current_time - timestamp > timeout
    ]
    for chat_id in expired_keys:
        del _EXPECTING_SYSTEM_REPLY[chat_id]
        log_debug(
            f"[transport] Removed expired system reply expectation for chat_id={chat_id} (timeout={timeout}s)"
        )


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
        'Tip: escape inner quotes with \\" and ensure proper commas and brackets.'
    )


def extract_json_from_text(text: str, return_metadata: bool = False) -> Optional[Dict]:
    """Extract the first valid JSON object or array from text.

    This function is smart enough to extract JSON even when LLMs (like Gemini)
                            # For all other interfaces, once valid JSON has been recovered,
                            # return the normalized JSON immediately instead of falling
                            # through into another retry cycle.
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
        "had_errors": False,
        "error_count": 0,
        "unparsed_content": "",
        "recovered": False,
        "had_extra_text": False,
        "prefix": "",
        "suffix": "",
    }

    if not text:
        return (None, metadata) if return_metadata else None

    # Try to clean up common markdown/formatting issues
    cleaned_text = text.strip()

    # Remove markdown code blocks if present
    if cleaned_text.startswith("```json"):
        cleaned_text = cleaned_text[7:]  # Remove ```json
        if cleaned_text.endswith("```"):
            cleaned_text = cleaned_text[:-3]  # Remove ```
        cleaned_text = cleaned_text.strip()
    elif cleaned_text.startswith("```"):
        cleaned_text = cleaned_text[3:]  # Remove ```
        if cleaned_text.endswith("```"):
            cleaned_text = cleaned_text[:-3]  # Remove ```
        cleaned_text = cleaned_text.strip()

    # Also try original text in case cleaning broke something
    sanitized = _remove_control_chars(text)
    stripped_noise = _strip_stacktraces_and_addresses(text)
    texts_to_try = [
        cleaned_text,
        stripped_noise.strip(),
        sanitized.strip(),
        text.strip(),
    ]

    decoder = json.JSONDecoder()
    found_json = None
    best_extra_chars = float("inf")  # Track the JSON with least extra content

    for text_variant in texts_to_try:
        log_debug(
            f"[extract_json_from_text] Trying text variant (length: {len(text_variant)})"
        )

        # First pass: look for clean JSON (no extra text)
        object_start_indices = [i for i, char in enumerate(text_variant) if char == "{"]
        array_start_indices = [i for i, char in enumerate(text_variant) if char == "["]
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
                if (
                    prefix and len(prefix.split()) <= 3
                ):  # Skip prefixes like "json", "here is", etc.
                    prefix_lower = prefix.lower().strip()
                    if prefix_lower in [
                        "json",
                        "here",
                        "output",
                        "response",
                        "result",
                        "answer",
                    ] or prefix_lower.startswith(
                        ("here is", "the json", "json:", "output:")
                    ):
                        log_debug(
                            f"[extract_json_from_text] Skipping JSON with explanatory prefix: '{prefix}'"
                        )
                        continue

                # Prefer clean JSON (no extra text)
                if extra_chars == 0:
                    log_debug(
                        f"[extract_json_from_text] ✅ Found clean JSON: {type(obj)}"
                    )
                    found_json = obj
                    metadata["had_extra_text"] = False
                    break

                # If we have extra text, keep track of the one with least extra content
                elif extra_chars < best_extra_chars:
                    best_extra_chars = extra_chars
                    found_json = obj
                    metadata["had_extra_text"] = True
                    metadata["prefix"] = prefix
                    metadata["suffix"] = suffix
                    metadata["prefix_length"] = len(prefix)
                    metadata["suffix_length"] = len(suffix)
                    log_debug(
                        f"[extract_json_from_text] Found JSON with {extra_chars} extra chars (best so far)"
                    )

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

                metadata.setdefault("error_messages", []).append(err_msg)
                log_debug(
                    f"[extract_json_from_text] JSON decode error at position {start}: {e}"
                )
                metadata["had_errors"] = True
                metadata["error_count"] += 1
                continue

        if found_json:
            break

        if found_json:
            break

    if not found_json:
        # As a last resort, try to find balanced JSON substrings in noise-reduced variants.
        tried_substrings = 0
        for candidate_source in (stripped_noise, sanitized, text):
            if not candidate_source:
                continue
            for start, end, substr in _find_json_substrings(candidate_source):
                tried_substrings += 1
                try:
                    obj = json.loads(substr)
                    # We found a valid JSON substring — accept it
                    found_json = obj
                    metadata["had_extra_text"] = True
                    metadata["prefix"] = candidate_source[:start].strip()
                    metadata["suffix"] = candidate_source[end:].strip()
                    metadata["prefix_length"] = len(metadata["prefix"])
                    metadata["suffix_length"] = len(metadata["suffix"])
                    metadata["noise_removed"] = tried_substrings > 0
                    log_info(
                        f"[extract_json_from_text] ✅ Extracted JSON from substring of sanitized text (start={start}, len={len(substr)})"
                    )
                    break
                except Exception:  # JSON still invalid — continue
                    metadata["had_errors"] = True
                    metadata["error_count"] += 1
                    continue
            if found_json:
                break

        if not found_json:
            log_debug("[extract_json_from_text] No valid JSON found in text")
            log_debug(
                f"[extract_json_from_text] Text content (first 500 chars): {text[:500]}"
            )
            log_debug(
                f"[extract_json_from_text] Text content (last 500 chars): {text[-500:]}"
            )
            return (None, metadata) if return_metadata else None

    # Log results based on what we found
    if metadata.get("had_extra_text", False):
        prefix_len = metadata.get("prefix_length", 0)
        suffix_len = metadata.get("suffix_length", 0)
        log_info(
            f"[extract_json_from_text] ✅ Extracted JSON with {prefix_len + suffix_len} extra chars (prefix: {prefix_len}, suffix: {suffix_len})"
        )
    else:
        log_debug(f"[extract_json_from_text] ✅ Found clean JSON: {type(found_json)}")

    # If we had errors but found JSON, it means we recovered from corruption
    if metadata["had_errors"] and found_json:
        metadata["recovered"] = True
        log_warning(
            f"[extract_json_from_text] ⚠️ JSON recovered after {metadata['error_count']} parsing errors - may be incomplete"
        )

        # Attempt to recover missing/partially-corrupted actions that the
        # JSON decoder couldn't parse due to poor quoting inside string values
        try:
            # Normalize: if the decoder returned a single action dict instead of
            # a top-level object containing 'actions', convert it into an actions list
            if (
                isinstance(found_json, dict)
                and "actions" not in found_json
                and "type" in found_json
            ):
                # Make top-level actions list containing the single decoded action
                found_json = {"actions": [found_json]}

            # Only attempt targeted recovery when we have an actions array
            if (
                isinstance(found_json, dict)
                and "actions" in found_json
                and isinstance(found_json["actions"], list)
            ):
                _attempt_recover_actions_from_text(text, found_json, metadata)
        except Exception as e:
            log_debug(f"[extract_json_from_text] Action recovery failed: {e}")

    return (found_json, metadata) if return_metadata else found_json


def _attempt_recover_actions_from_text(
    original_text: str, found_json: dict, metadata: dict
):
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
        text = original_text or ""
        # Collect types already present
        existing = set()
        try:
            for a in found_json.get("actions", []):
                if isinstance(a, dict) and "type" in a:
                    existing.add(a["type"])
        except Exception:
            existing = set()

        # Candidate action types we try to recover if missing (dynamic)
        candidates = []
        try:
            from core.core_initializer import core_initializer

            available_actions = core_initializer.actions_block.get(
                "available_actions", {}
            )
            candidates = [
                action_type
                for action_type in available_actions.keys()
                if isinstance(action_type, str) and action_type.startswith("message_")
            ]
        except Exception:
            candidates = []

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
                brace_idx = text.find("{", payload_idx)
                if brace_idx == -1:
                    continue

                # Try to extract a reasonable slice for the payload by searching
                # for the next '}' that likely terminates the payload object.
                # Use a conservative search window to avoid expensive scans.
                window = text[
                    brace_idx : brace_idx + 8001
                ]  # a large but bounded window

                # Attempt to find the closing brace for this payload using simple balance
                depth = 0
                end_idx = None
                for i, ch in enumerate(window):
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            end_idx = brace_idx + i + 1
                            break

                if end_idx is None:
                    # fallback: try to locate a following known key which likely marks end
                    next_key_pos = len(text)
                    for k in ('"type"', '"actions"', "\n\n"):
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
                markers = [
                    '"interface_path"',
                    '"reply_to_message_id"',
                    '"chat_name"',
                    '"reply_to_message_id"',
                    '"send_in"',
                    '"send_at"',
                ]
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

                raw_value = text[brace_idx + val_start : marker_pos]

                # Trim trailing characters that may belong to JSON punctuation (like ",)
                raw_value = raw_value.rstrip(" ,\n\r\t}")

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
                recovered_action = {"type": candidate, "payload": {"text": safe_text}}
                if interface_path:
                    recovered_action["payload"]["interface_path"] = interface_path

                # Append recovered action only if not present already
                found_json.setdefault("actions", [])
                found_json["actions"].append(recovered_action)
                metadata["recovered"] = True
                metadata["recovery_attempts"] = metadata.get("recovery_attempts", 0) + 1
                log_info(
                    f"[extract_json_from_text] Recovered missing action {candidate} (heuristic)"
                )
                # Break after first recovery for this candidate to avoid duplicates
                break
    except Exception as e:
        log_debug(f"[attempt_recover] Recovery routine error: {e}")


async def _grillo_fire_and_forget(
    bot, message, original_user_message: str, llm_reply: str, context: dict
):
    """Background helper that calls the GrilloActionChecker and either auto-executes
    or persists proposed actions depending on configuration and policy.

    This helper is intentionally defensive: failures here must not affect the
    main LLM->interface flow.
    """
    try:
        log_info(
            f"[grillo] Checker started for chat_id={getattr(message, 'chat_id', None)} chain_result={context.get('chain_result') if context else 'unknown'}"
        )
        # Prefer plugin-provided checker via PLUGIN_REGISTRY to avoid importing plugin internals from core
        from core.core_initializer import PLUGIN_REGISTRY

        grillo_plugin = None
        try:
            if isinstance(PLUGIN_REGISTRY, dict):
                grillo_plugin = PLUGIN_REGISTRY.get(
                    "grillo_plugin"
                ) or PLUGIN_REGISTRY.get("grillo_impl")
        except Exception:
            grillo_plugin = None

        if grillo_plugin and hasattr(grillo_plugin, "suggest_actions_from_reply"):
            actions = await grillo_plugin.suggest_actions_from_reply(
                llm_reply, original_user_message, context or {}, message
            )
        else:
            # Fallback to legacy checker module if plugin not available
            try:
                from plugins.grillo.grillo_action_checker import GrilloActionChecker

                checker = GrilloActionChecker()
                actions = await checker.inspect_reply_and_suggest_actions(
                    llm_reply, original_user_message, context or {}, message
                )
            except Exception as e:
                log_debug(f"[grillo] Checker raised exception: {e}")
                return
    except Exception as e:
        log_debug(f"[grillo] Checker raised exception: {e}")
        return

    if not actions or not isinstance(actions, list) or len(actions) == 0:
        log_info("[grillo] Checker completed: no suggested actions")
        return

    log_info(f"[grillo] Checker returned {len(actions)} suggested action(s)")

    # Determine auto-exec policy
    try:
        from core.config_manager import config_registry

        # If this invocation came from a Grillo beat (observer or other beats),
        # force auto-execution so beats are active by default (the action parser
        # still enforces policy/autonomy checks).
        if context and isinstance(context, dict) and context.get("grillo_beat"):
            auto_exec = True
            log_info(
                f"[grillo] Force auto_exec because grillo_beat present (beat_type={context.get('beat_type')})"
            )
        else:
            auto_exec = bool(
                config_registry.get_value(
                    "GRILLO_AUTO_GENERATE_ACTIONS",
                    False,
                    value_type=bool,
                    group="grillo",
                    component="grillo",
                )
            )
    except Exception:
        auto_exec = False

    from core.core_initializer import PLUGIN_REGISTRY

    grillo_plugin = None
    try:
        if isinstance(PLUGIN_REGISTRY, dict):
            grillo_plugin = PLUGIN_REGISTRY.get("grillo_plugin") or PLUGIN_REGISTRY.get(
                "grillo_impl"
            )
    except Exception:
        grillo_plugin = None

    if auto_exec:
        try:
            # Quarantine heuristic-recovered actions: do not auto-execute them
            quarantined_actions = [
                a for a in actions if a.get("metadata", {}).get("heuristic_recovery")
            ]
            actions_to_exec = [
                a
                for a in actions
                if not a.get("metadata", {}).get("heuristic_recovery")
            ]

            if quarantined_actions:
                log_info(
                    f"[grillo] Quarantined {len(quarantined_actions)} heuristic-recovered action(s); persisting as proposals"
                )
                if grillo_plugin and hasattr(grillo_plugin, "create_activity_log"):
                    try:
                        activity_id_q = await grillo_plugin.create_activity_log(
                            beat_type="grillo_quarantined",
                            prompt_text=str(
                                {"user": original_user_message, "llm_reply": llm_reply}
                            ),
                            metadata={"quarantined_actions": quarantined_actions},
                        )
                        log_info(
                            f"[grillo] Persisted quarantined actions activity_id={activity_id_q}"
                        )
                    except Exception as e:
                        log_debug(
                            f"[grillo] Failed to persist quarantined actions: {e}"
                        )

            # Filter out action types already present in the original LLM reply.
            # The checker's role is to ADD missing actions, not re-execute ones the LLM
            # already generated (those are handled by the main message_chain flow).
            # Without this filter, a Grillo beat can double-send to Telegram:
            # (1) checker auto-executes suggested message_telegram_bot, then
            # (2) the main correction loop also executes the same action.
            try:
                original_json, _ = extract_json_from_text(
                    llm_reply, return_metadata=True
                )
                if original_json and isinstance(original_json, dict):
                    original_actions = original_json.get("actions", [])
                    original_types = {
                        a.get("type") or a.get("action")
                        for a in original_actions
                        if isinstance(a, dict)
                    }
                    if original_types:
                        before_count = len(actions_to_exec)
                        actions_to_exec = [
                            a
                            for a in actions_to_exec
                            if (a.get("type") or a.get("action")) not in original_types
                        ]
                        skipped = before_count - len(actions_to_exec)
                        if skipped:
                            log_info(
                                f"[grillo] Skipped {skipped} checker-suggested action(s) already present in original LLM reply (prevents double-send): {original_types}"
                            )
            except Exception as e:
                log_debug(
                    f"[grillo] Error filtering duplicate actions from LLM reply: {e}"
                )

            # If no remaining actions to auto-execute, return early
            if not actions_to_exec:
                log_info(
                    "[grillo] No actions to auto-execute after quarantining; returning"
                )
                return

            # Run actions through the canonical action parser so policy/autonomy
            # checks are applied consistently.
            from core import action_parser

            run_ctx = dict(context or {})
            run_ctx["grillo_auto"] = True
            result = await action_parser.run_actions(
                actions_to_exec, run_ctx, bot, message
            )
            log_info(
                f"[grillo] Auto-executed {len(actions_to_exec)} action(s); result: {result}"
            )
            if grillo_plugin and hasattr(grillo_plugin, "create_activity_log"):
                try:
                    activity_id = await grillo_plugin.create_activity_log(
                        beat_type="grillo_auto_exec",
                        prompt_text=str(
                            {"user": original_user_message, "llm_reply": llm_reply}
                        ),
                        metadata={
                            "suggested_actions": actions_to_exec,
                            "result": result,
                        },
                    )
                    log_info(f"[grillo] Logged auto-exec activity id={activity_id}")
                except Exception as e:
                    log_debug(
                        f"[grillo] create_activity_log failed after auto-exec: {e}"
                    )

            # Persist action-level execution results
            if (
                grillo_plugin
                and hasattr(grillo_plugin, "create_action_exec")
                and activity_id
            ):
                try:
                    processed = (
                        result.get("processed", []) if isinstance(result, dict) else []
                    )
                    failed = (
                        result.get("failed_actions", [])
                        if isinstance(result, dict)
                        else []
                    )
                    db_count = 0
                    fallback_count = 0
                    for idx, a in enumerate(actions_to_exec):
                        try:
                            if any(
                                (a == p)
                                or (
                                    p.get("type") == a.get("type")
                                    and p.get("payload") == a.get("payload")
                                )
                                for p in processed
                            ):
                                res = await grillo_plugin.create_action_exec(
                                    activity_log_id=activity_id,
                                    action_index=idx,
                                    action_type=a.get("type"),
                                    payload=a.get("payload"),
                                    status="processed",
                                    result=result,
                                )
                                if res:
                                    db_count += 1
                                else:
                                    fallback_count += 1
                            else:
                                # Try to match by index in failed list
                                fail_entry = next(
                                    (f for f in failed if f.get("index") == idx), None
                                )
                                if fail_entry:
                                    err = (
                                        "; ".join(fail_entry.get("errors", []))
                                        if isinstance(
                                            fail_entry.get("errors", []), list
                                        )
                                        else str(fail_entry.get("errors"))
                                    )
                                    res = await grillo_plugin.create_action_exec(
                                        activity_log_id=activity_id,
                                        action_index=idx,
                                        action_type=a.get("type"),
                                        payload=a.get("payload"),
                                        status="failed",
                                        error_text=err,
                                        result=fail_entry,
                                    )
                                    if res:
                                        db_count += 1
                                    else:
                                        fallback_count += 1
                                else:
                                    # Unknown outcome: mark processed with available result
                                    res = await grillo_plugin.create_action_exec(
                                        activity_log_id=activity_id,
                                        action_index=idx,
                                        action_type=a.get("type"),
                                        payload=a.get("payload"),
                                        status="processed",
                                        result=result,
                                    )
                                    if res:
                                        db_count += 1
                                    else:
                                        fallback_count += 1
                        except Exception as e:
                            log_debug(
                                f"[grillo] create_action_exec failed after auto-exec per-action: {e}"
                            )
                    log_info(
                        f"[grillo] Recorded action_execs for activity_id={activity_id}: db={db_count}, fallback={fallback_count}"
                    )
                except Exception as e:
                    log_debug(
                        f"[grillo] create_action_exec failed after auto-exec: {e}"
                    )
        except Exception as e:
            log_warning(f"[grillo] Auto-execution failed: {e}")
            if grillo_plugin and hasattr(grillo_plugin, "create_activity_log"):
                try:
                    await grillo_plugin.create_activity_log(
                        beat_type="grillo_auto_exec_failure",
                        prompt_text=str(
                            {
                                "user": original_user_message,
                                "llm_reply": llm_reply,
                                "error": str(e),
                            }
                        ),
                        metadata={"suggested_actions": actions},
                    )
                except Exception:
                    pass
    else:
        # Persist proposal for human review
        try:
            if grillo_plugin and hasattr(grillo_plugin, "create_activity_log"):
                try:
                    activity_id = await grillo_plugin.create_activity_log(
                        beat_type="grillo_proposal",
                        prompt_text=str(
                            {"user": original_user_message, "llm_reply": llm_reply}
                        ),
                        metadata={"suggested_actions": actions},
                    )
                    if activity_id:
                        log_info(
                            f"[grillo] Persisted suggested actions to grillo_activity_log id={activity_id}"
                        )
                    else:
                        log_warning(
                            "[grillo] create_activity_log returned None while persisting proposal (activity not recorded)"
                        )
                except Exception as e:
                    log_debug(
                        f"[grillo] create_activity_log failed while persisting proposal: {e}"
                    )
        except Exception as e:
            log_warning(
                f"[grillo] Failed to create activity log for suggested actions: {e}"
            )

        # Persist suggested actions as pending (best-effort)
        try:
            if (
                grillo_plugin
                and hasattr(grillo_plugin, "create_action_exec")
                and activity_id
            ):
                try:
                    pending_count = 0
                    fallback_count = 0
                    for idx, a in enumerate(actions):
                        res_id = await grillo_plugin.create_action_exec(
                            activity_log_id=activity_id,
                            action_index=idx,
                            action_type=a.get("type", "unknown"),
                            payload=a.get("payload"),
                            status="pending",
                        )
                        if res_id:
                            pending_count += 1
                        else:
                            fallback_count += 1
                    log_info(
                        f"[grillo] Persisted {pending_count} action_exec(s) to DB for activity_id={activity_id}, {fallback_count} to fallback"
                    )
                except Exception as e:
                    log_debug(
                        f"[grillo] create_action_exec failed while persisting proposal: {e}"
                    )
            elif grillo_plugin and not activity_id:
                # DB didn't record activity; write a fallback activity and store action execs there
                try:
                    activity_obj = {
                        "beat_type": "grillo_proposal",
                        "prompt_text": str(
                            {"user": original_user_message, "llm_reply": llm_reply}
                        ),
                        "response_text": None,
                        "metadata": {"suggested_actions": actions},
                    }
                    fallback_activity_id = await (
                        grillo_plugin._fallback_write_activity(activity_obj)
                        if hasattr(grillo_plugin, "_fallback_write_activity")
                        else None
                    )
                    written = 0
                    for idx, a in enumerate(actions):
                        exec_obj = {
                            "activity_log_id": fallback_activity_id,
                            "action_index": idx,
                            "action_type": a.get("type", "unknown"),
                            "payload": a.get("payload"),
                            "status": "pending",
                            "error_text": None,
                            "result": None,
                            "created_at": None,
                        }
                        if hasattr(grillo_plugin, "_fallback_write_action_exec"):
                            await grillo_plugin._fallback_write_action_exec(exec_obj)
                        written += 1
                    log_info(
                        f"[grillo] Persisted {written} action_exec(s) to fallback for fallback_activity_id={fallback_activity_id}"
                    )
                except Exception as e:
                    log_debug(f"[grillo] fallback write for proposal failed: {e}")
        except Exception as e:
            log_debug(
                f"[grillo] create_action_exec outer failed while persisting proposal: {e}"
            )


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

    # Normalize mojibake in plain text before any downstream processing
    try:
        if isinstance(text, str) and text:
            from core.text_utils import try_recover_mojibake

            recovered_text = try_recover_mojibake(text)
            if recovered_text and recovered_text != text:
                log_debug("[transport] Recovered mojibake in outbound text")
                text = recovered_text
    except Exception:
        # Non-fatal: keep original text if recovery fails
        pass

    # Strip emotion tags like {arousal 10, devotion 10} from outbound text.
    # The LLM embeds these for internal emotion state tracking but they
    # must never leak into user-visible messages.
    try:
        if isinstance(text, str) and text and "{" in text:
            from plugins.emotion_manager import strip_emotion_tags

            cleaned = strip_emotion_tags(text)
            if cleaned != text:
                log_debug("[transport] Stripped emotion tags from outbound text")
                text = cleaned
    except Exception:
        pass

    # Diagnostic: log interface function and runtime send parameters
    try:
        bot_self = getattr(interface_send_func, "__self__", None)
        log_debug(
            f"[transport] universal_send called: interface_send_func={interface_send_func} bot_self={bot_self} args={args} kwargs_keys={list(kwargs.keys())}"
        )
    except Exception as _:
        log_debug(
            "[transport] universal_send diagnostic logging failed to inspect interface_send_func"
        )

    # Map thread_id to message_thread_id for Telegram API compatibility
    if "thread_id" in kwargs:
        thread_id = kwargs.pop("thread_id")
        # Convert thread_id to int if it's a string (for Telegram API compatibility)
        if isinstance(thread_id, str) and thread_id.isdigit():
            thread_id = int(thread_id)
        kwargs["message_thread_id"] = thread_id

    # Save LLM response to chat history (before JSON parsing)
    # Extract interface_path for saving to chat history
    interface_path = kwargs.get("interface_path")
    log_debug(
        f"[transport] Checking for chat history save: interface_path={interface_path}, text_len={len(text) if text else 0}"
    )

    if interface_path and text:
        try:
            from core.chat_context_manager import add_message_to_context
            from datetime import datetime

            # Extract JSON and metadata to decide what (if any) human-readable
            # companion text should be saved alongside LLM-originated JSON.
            json_payload, json_meta = extract_json_from_text(text, return_metadata=True)
            log_debug(
                f"[transport] JSON data extracted: {json_payload is not None} metadata: {bool(json_meta)}"
            )

            # If JSON present and metadata indicates extra text (prefix/suffix), use those.
            if json_payload is None:
                text_content = text
            else:
                if json_meta and (json_meta.get("prefix") or json_meta.get("suffix")):
                    # Compose companion text from prefix+suffix
                    parts = []
                    if json_meta.get("prefix"):
                        parts.append(json_meta.get("prefix").strip())
                    if json_meta.get("suffix"):
                        parts.append(json_meta.get("suffix").strip())
                    text_content = "\n".join(parts).strip()
                else:
                    # Pure JSON only — nothing to save as user-visible content
                    text_content = ""
            log_debug(
                f"[transport] Text content for history: {len(text_content)} chars (was {len(text)})"
            )

            if text_content:  # Only save if there's actual message content
                log_info(
                    f"[transport] 📝 Saving LLM response to chat history: {interface_path}"
                )
                await add_message_to_context(
                    interface_path=interface_path,
                    message_text=text_content,
                    sender_name="self",
                    sender_id="synth",
                    timestamp=datetime.now().isoformat(),
                )
                log_info(
                    f"[transport] ✅ LLM response saved to chat history for {interface_path}"
                )
            else:
                log_debug("[transport] No text content to save (only JSON)")
        except Exception as e:
            log_error(
                f"[transport] ❌ Failed to save LLM response to chat history: {e}"
            )
            import traceback

            log_debug(f"[transport] Traceback: {traceback.format_exc()}")

    # Filter out internal parameters that should not be passed to interface send functions
    excluded_params = {
        "event_id",
        "interface",
        "is_llm_response",
        "context",
        "error_retry_policy",
        "interface_path",
        "skip_history",
    }
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
            log_debug("[flow] transport.detects_json -> will attempt run_actions")
            # Handle various JSON structures: nested 'actions' list, bare list, or single action dict
            actions = []
            if isinstance(json_data, dict) and "actions" in json_data:
                actions = json_data["actions"]
                if not isinstance(actions, list):
                    actions = [actions] if actions else []
            elif isinstance(json_data, list):
                actions = json_data
            elif isinstance(json_data, dict):
                # Single bare action (possibly using alternative keys)
                if any(
                    k in json_data
                    for k in ["type", "function", "name", "plugin", "action"]
                ):
                    actions = [json_data]

            if not actions:
                log_debug(f"[transport] No actions found in JSON: {json_data}")
                return await interface_send_func(*args, text=text, **kwargs)

            # Normalize all actions to the standard (type, payload) format
            log_debug(f"[transport] Normalizing {len(actions)} actions before dispatch")
            for act in actions:
                if isinstance(act, dict):
                    # Normalize type
                    if "type" not in act:
                        act["type"] = (
                            act.get("function")
                            or act.get("name")
                            or act.get("plugin")
                            or act.get("action")
                        )
                    # Normalize payload
                    if "payload" not in act:
                        # Extract arguments/parameters if they exist
                        act["payload"] = (
                            act.get("arguments")
                            or act.get("parameters")
                            or act.get("args")
                        )

            bot = getattr(interface_send_func, "__self__", None) or (
                args[0] if args and hasattr(args[0], "send_message") else None
            )

            if not bot:
                log_warning(
                    "[transport] Could not extract bot instance for action parsing"
                )
                return await interface_send_func(*args, text=text, **kwargs)

            # Create message context for actions
            message = SimpleNamespace()
            chat_id_value = kwargs.get("chat_id") or (args[0] if args else None)

            # Accept int or numeric string chat IDs (some interfaces provide strings)
            if chat_id_value is None or not isinstance(chat_id_value, (int, str)):
                log_warning(
                    f"[transport] Invalid chat_id for action processing: {chat_id_value}"
                )
                return await interface_send_func(*args, text=text, **kwargs)

            # Coerce numeric string to int when possible for internal consistency
            if (
                isinstance(chat_id_value, str)
                and chat_id_value.strip().lstrip("-").isdigit()
            ):
                try:
                    chat_id_value = int(chat_id_value)
                except Exception:
                    # Keep as string if coercion fails
                    pass

            message.chat_id = chat_id_value
            message.text = ""
            # Mark this message as coming from the AI/cortex layer so corrective
            # flows trigger. Keep both legacy and new flags to be safe.
            message.from_cortex = True
            message.original_text = text
            message.thread_id = kwargs.get("thread_id")
            from datetime import datetime

            message.date = datetime.utcnow()

            # Use centralized action system for all action types.  The action
            # parser is optional, so import it lazily and fall back to sending
            # plain text if it's unavailable.
            try:
                from core.action_parser import run_actions  # type: ignore
            except (
                Exception
            ) as e:  # pragma: no cover - executed when action_parser missing
                log_warning(f"[transport] action parser unavailable: {e}")
                return await interface_send_func(*args, text=text, **kwargs)

            # Determine current interface from bot
            current_interface = "unknown"  # default fallback
            if hasattr(bot, "get_interface_id"):
                try:
                    current_interface = bot.get_interface_id()
                except Exception as e:
                    log_debug(f"[transport] Could not get interface_id from bot: {e}")

            context = {
                "interface": current_interface,
                "original_chat_id": chat_id_value,
                "original_thread_id": kwargs.get("thread_id"),
                "original_text": text[:500]
                if text
                else "",  # Include preview of original text
                "thread_defaults": {"default": None},
            }  # Add more context as needed
            # Mark context to indicate source is LLM (used by selective corrector)
            context["from_cortex"] = True

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
                    log_debug(
                        f"[transport] Skipping cross-interface action {action_type} (current: {current_interface})"
                    )

            if cross_interface_actions:
                log_info(
                    f"[transport] Filtered {len(cross_interface_actions)} cross-interface actions, processing {len(current_interface_actions)} for {current_interface}"
                )

            # Only process actions for current interface
            if current_interface_actions:
                # Diagnostic: list action types and payload summaries
                try:
                    types_list = [a.get("type") for a in current_interface_actions]
                    log_debug(
                        f"[transport] Actions for {current_interface}: {types_list}"
                    )
                    for a in current_interface_actions:
                        log_debug(
                            f"[transport] Action detail: type={a.get('type')} payload_keys={list((a.get('payload') or {}).keys())}"
                        )
                except Exception:
                    pass
                try:
                    result = await run_actions(
                        current_interface_actions, context, bot, message
                    )
                    processed_actions = (
                        result.get("processed", [])
                        if isinstance(result, dict)
                        else current_interface_actions
                    )
                    log_debug(
                        f"[transport] run_actions result: processed={len(processed_actions)} failed={len(result.get('failed_actions', [])) if isinstance(result, dict) else 0}"
                    )
                except Exception as e:
                    log_warning(f"[transport] Failed to process actions: {e}")
                    processed_actions = []
                finally:
                    log_debug(
                        f"[flow] transport.after_run_actions -> processed_count={len(processed_actions)}"
                    )

                log_info(
                    f"[transport] Processed {len(processed_actions)} unique JSON actions via plugin system"
                )

                # After processing actions, check if there's text outside the JSON that should be sent
                if json_data and (json_data.get("prefix") or json_data.get("suffix")):
                    companion_text = ""

                    # Combine prefix and suffix, removing duplicate content
                    if json_data.get("prefix"):
                        companion_text = json_data.get("prefix", "").strip()

                    if json_data.get("suffix"):
                        suffix_text = json_data.get("suffix", "").strip()
                        if companion_text:
                            companion_text += "\n" + suffix_text
                        else:
                            companion_text = suffix_text

                    if companion_text:
                        log_info(
                            f"[transport] Sending companion text alongside JSON actions: {len(companion_text)} chars"
                        )
                        # Send the companion text as a separate message after the actions
                        await interface_send_func(*args, text=companion_text, **kwargs)

                return
            else:
                # No actions for current interface, send as plain text
                log_debug(
                    f"[transport] No actions for current interface {current_interface}, sending as plain text"
                )
                return await interface_send_func(*args, text=text, **kwargs)

        except Exception as e:
            log_warning(f"[transport] Failed to process JSON actions: {e}")

    # Non-JSON plain text — forward directly. Corrector is only run in llm_to_interface.
    # NOTE: If LLM output contains no JSON and no actions, it will be forwarded as plain text.
    # The LLM instructions are clear that ANY TEXT OUTSIDE JSON WILL BE DISCARDED,
    # so LLMs should only output JSON. If they output plain text anyway, it still gets sent,
    # but companion text from JSON extraction does NOT get sent in this path.
    if text and not text.startswith(("[ERROR]", "[WARNING]", "[INFO]", "[DEBUG]")):
        log_debug(
            f"[flow] transport.non_json -> forwarding plain text (no JSON detected, no corrector triggered) chat_id={kwargs.get('chat_id')}"
        )
        return await interface_send_func(*args, text=text, **kwargs)

    # Send as normal text
    log_debug(
        f"[flow] transport.forward_plain -> calling interface_send_func for chat_id={kwargs.get('chat_id')}"
    )
    return await interface_send_func(*args, text=text, **kwargs)


# Re-entry guard removed: rely solely on the corrector retry counter to avoid loops


def _get_attempted_action_full_description(
    error_text: str,
    allowed_action_types: Optional[set[str]] = None,
) -> Optional[Dict[str, Any]]:
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
        action_type: Optional[str] = None

        try:
            parsed_json, _ = extract_json_from_text(error_text, return_metadata=True)
        except Exception:
            parsed_json = None

        def _extract_action_type(candidate: Any) -> Optional[str]:
            if isinstance(candidate, dict):
                candidate_type = (
                    candidate.get("type")
                    or candidate.get("action")
                    or candidate.get("function")
                    or candidate.get("name")
                    or candidate.get("plugin")
                )
                if isinstance(candidate_type, str) and candidate_type:
                    return candidate_type
                if len(candidate) == 1:
                    only_key, only_value = next(iter(candidate.items()))
                    if isinstance(only_key, str) and isinstance(only_value, dict):
                        return only_key
            return None

        if isinstance(parsed_json, dict):
            raw_actions = parsed_json.get("actions")
            if isinstance(raw_actions, list):
                for raw_action in raw_actions:
                    action_type = _extract_action_type(raw_action)
                    if action_type:
                        break
            if not action_type:
                action_type = _extract_action_type(parsed_json)
        elif isinstance(parsed_json, list):
            for raw_action in parsed_json:
                action_type = _extract_action_type(raw_action)
                if action_type:
                    break

        if not action_type:
            # Fallback to regex for partially malformed JSON.
            import re

            # Look for "type", "action", etc. key-value pairs. We use a more specific regex
            # to avoid matching nested fields (like emotion types inside an action payload).
            type_match = re.search(
                r'"(?:type|action|function|name|plugin)"\s*:\s*"([^"]+)"', error_text
            )
            if type_match:
                action_type = type_match.group(1)

        if not action_type:
            log_debug(
                "[_get_attempted_action] Could not extract action type from error text"
            )
            return None
        log_debug(
            f"[_get_attempted_action] Identified attempted action type: {action_type}"
        )

        # Enforce allowed action scope (if provided)
        if allowed_action_types is not None and action_type not in allowed_action_types:
            log_debug(
                f"[_get_attempted_action] Action type '{action_type}' not in allowed scope; skipping"
            )
            return None

        # Now get the FULL description from core_initializer (not minified)
        try:
            from core.core_initializer import core_initializer

            full_actions = core_initializer.actions_block.get("available_actions", {})

            if action_type in full_actions:
                full_description = full_actions[action_type]
                log_debug(
                    f"[_get_attempted_action] Found full description for {action_type}"
                )
                return {
                    "action_type": action_type,
                    "full_description": full_description,
                }
            else:
                # Action type isn't recognised. Instead of returning None and
                # silently dropping the attempted-action hint we craft a
                # minimal description that tells the corrector the action was
                # invalid and provides the list of supported actions.  This
                # helps the LLM understand that it should not invent new action
                # names.
                log_debug(
                    f"[_get_attempted_action] Action type '{action_type}' not found in available actions; providing fallback info"
                )
                # Instead of enumerating all types (which could be large and
                # change as plugins load), give a generic remark.  The action
                # registry is dynamic and the corrector already has access to the
                # current set of supported types, so listing them here is both
                # unnecessary and brittle.
                note = (
                    f"Action '{action_type}' is not a supported action type. "
                    "Available actions are provided dynamically by the active "
                    "plugins; please choose one of those."
                )
                return {
                    "action_type": action_type,
                    "full_description": {"description": note},
                }
        except Exception as e:
            log_debug(
                f"[_get_attempted_action] Could not load full action description: {e}"
            )
            return None

    except Exception as e:
        log_debug(f"[_get_attempted_action] Error analyzing attempted action: {e}")
        return None


async def run_corrector_middleware(
    text: str, bot=None, context: dict = None, chat_id=None, thread_id=None
) -> str:
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
        from types import SimpleNamespace
    except Exception as e:
        try:
            log_warning(f"[corrector_middleware] Import error: {e}")
        except Exception:
            pass
        return None

    # CORRECTOR_RETRIES may be a ConfigVar; ensure we have an int
    max_retries = int(getattr(action_parser, "CORRECTOR_RETRIES", 2))

    # Extract message from context if available
    message = context.get("message") if context else None

    # Normalize allowed action types from context (scope-aware correction)
    allowed_action_types = None
    if context:
        scoped_actions = context.get("allowed_action_types") or context.get(
            "allowed_actions"
        )
        if isinstance(scoped_actions, (list, set, tuple)):
            allowed_action_types = {str(a) for a in scoped_actions if a}

    # Check if we have selective correction context (some actions succeeded, some failed)
    correction_context = (
        getattr(message, "correction_context", None) if message else None
    )

    # If already valid JSON WITHOUT errors, nothing to do
    try:
        json_obj, metadata = extract_json_from_text(text, return_metadata=True)
        # If we have a JSON object, prefer it even if there was extra surrounding
        # noise (e.g., stacktraces) as long as we recovered or there were no parsing
        # errors that leave us without a valid JSON structure.
        if (
            json_obj
            and not correction_context
            and (
                not metadata.get("had_errors", False)
                or metadata.get("recovered", False)
                or metadata.get("had_extra_text", False)
            )
        ):
            log_debug(
                "[corrector_middleware] Input contains valid JSON (possibly with extra noise); skipping correction"
            )
            return text
        elif json_obj and metadata.get("had_errors", False):
            log_debug(
                f"[corrector_middleware] JSON recovered with {metadata.get('error_count', 0)} errors; proceeding with correction"
            )
    except Exception:
        pass

    # Don't correct system messages or messages that clearly don't need correction
    if text and (
        "system_message" in text
        or text.startswith(("[ERROR]", "[WARNING]", "[INFO]", "[DEBUG]"))
    ):
        log_debug(
            "[corrector_middleware] Text appears to be system message; skipping correction to prevent loops"
        )
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
                llm_plugin = getattr(plugin_instance, "get_plugin", lambda: None)()
                if llm_plugin is None:
                    llm_plugin = getattr(plugin_instance, "plugin", None)

            # Log plugin discovery
            try:
                if llm_plugin is None:
                    log_debug(
                        f"[corrector_middleware] attempt {attempt}: no active LLM plugin found"
                    )
                else:
                    log_debug(
                        f"[corrector_middleware] attempt {attempt}: using LLM plugin {getattr(llm_plugin, '__class__', llm_plugin)}"
                    )
            except Exception:
                pass

            if not llm_plugin or not hasattr(llm_plugin, "handle_incoming_message"):
                log_warning(
                    f"[corrector_middleware] No active LLM plugin available (attempt {attempt}/{max_retries})"
                )
                # no plugin available — stop trying and indicate failure (do NOT send to interface)
                log_debug(
                    "[corrector_middleware] No LLM available; blocking and returning None"
                )
                return None

            # Build a correction payload similar to action_parser.corrector
            # NOTE: We do NOT include full_json_instructions here because:
            # 1. It's HUGE (all available actions with full descriptions)
            # 2. Corrector only needs the format template, not all actions
            # 3. LLM just needs to fix the JSON structure, not learn new actions
            attempted_action_info = None

            # Try to identify which action was attempted and include its full description
            try:
                attempted_action_info = _get_attempted_action_full_description(
                    text,
                    allowed_action_types=allowed_action_types,
                )
                if attempted_action_info:
                    log_info(
                        f"[corrector_middleware] Including full description for attempted action: {attempted_action_info['action_type']}"
                    )
            except Exception as e:
                log_debug(
                    f"[corrector_middleware] Could not extract attempted action info: {e}"
                )

            # Use the thread_id from the original conversation if available
            # Avoid defaulting to 0. If there's no thread_id, leave it as None so
            # downstream code doesn't accidentally route to thread 0.
            payload_thread_id = thread_id if thread_id is not None else None
            log_debug(
                f"[corrector_middleware] Using payload_thread_id={payload_thread_id} for corrector request"
            )

            # Extract the original user message text from context if available
            # This is CRUCIAL: when the corrector retries, the LLM needs to know what the
            # original user question was, not just that "your JSON was invalid". Without this,
            # the LLM might respond to the correction instructions themselves instead of
            # answering the user's original question.
            # Example: User asks "SyntH are you there?" -> LLM fails -> Corrector sends correction
            #          With original_user_message, LLM knows to respond "Yup, here I am ♡"
            #          Without it, LLM might respond "Acknowledged, JSON was valid"
            original_user_message = ""
            if message:
                original_user_message = getattr(message, "text", "") or ""
            if (
                not original_user_message
                and context
                and context.get("original_user_message") is not None
            ):
                original_user_message = context.get("original_user_message") or ""

            # Build correction message based on whether we have selective correction context
            if correction_context:
                # Selective correction: tell LLM what succeeded and what needs fixing
                successful = correction_context.get("successful_actions", [])
                failed = correction_context.get("failed_actions", [])

                correction_message_text = (
                    f"PARTIAL SUCCESS - Some actions completed, others failed.\n\n"
                    f"✅ Successfully executed {len(successful)} action(s):\n"
                )
                for action in successful:
                    action_type = action.get("type", "unknown")
                    correction_message_text += f"  - {action_type}\n"

                correction_message_text += (
                    f"\n❌ Failed {len(failed)} action(s) that need correction:\n"
                )
                for failed_item in failed:
                    action = failed_item.get("action", {})
                    action_errors = failed_item.get("errors", [])
                    action_type = action.get("type", "unknown")
                    correction_message_text += (
                        f"  - {action_type}: {', '.join(action_errors)}\n"
                    )

                if allowed_action_types:
                    correction_message_text += f"\nAllowed action types for this scope: {', '.join(sorted(allowed_action_types))}\n"
                correction_message_text += (
                    "\nRequirements:\n"
                    "1. Respond with ONLY valid JSON\n"
                    "2. Include ONLY the missing/failed actions - do NOT repeat successful ones\n"
                    "3. Fix validation errors in the failed actions\n"
                    "4. Ensure you've created ALL actions from the user's original request\n"
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
                if allowed_action_types:
                    correction_message_text += f"\nAllowed action types for this scope: {', '.join(sorted(allowed_action_types))}"

            # Extract the originating interface so the LLM engine can route
            # the corrected response back to the correct interface instead of
            # falling back to synth_webui.
            originating_interface: str | None = (
                context.get("interface") if context else None
            )

            correction_payload = {
                "system_message": {
                    "type": "error",
                    "message": correction_message_text,
                    "your_reply": text,
                    "original_user_message": original_user_message,
                    "chat_id": chat_id,
                    "thread_id": payload_thread_id,
                    "target_interface": originating_interface,
                }
            }

            # If we identified an action attempt, include its complete (non-minified) description
            if attempted_action_info:
                correction_payload["system_message"]["attempted_action"] = (
                    attempted_action_info["action_type"]
                )
                correction_payload["system_message"]["action_full_schema"] = (
                    attempted_action_info["full_description"]
                )
                log_debug(
                    f"[corrector_middleware] Added full action schema for {attempted_action_info['action_type']}"
                )

            # Add required format examples — use concrete interface name when known
            iface_label = originating_interface or "<interface>"
            correction_payload["system_message"]["required_format"] = {
                "actions": [
                    {
                        "type": f"message_{iface_label}",
                        "payload": {
                            "text": "Your message content here (optional - only if you want to reply to user)",
                            "interface_path": f"{iface_label}/{chat_id or '<chat_id>'}/{payload_thread_id if payload_thread_id is not None else ''}",
                        },
                    }
                ]
            }
            correction_payload["system_message"]["strict_requirements"] = [
                "MUST start with { and end with }",
                "MUST contain 'actions' array",
                "NO text outside JSON structure",
                "NO markdown formatting (e.g., no ```json blocks)",
                "NO explanations outside JSON",
                'Emotions MUST be provided in the \'feelings\' metadata object, NEVER as top-level actions (e.g., do NOT use {"type": "happy"})',
            ]
            correction_prompt = json.dumps(correction_payload, ensure_ascii=False)

            # Construct a lightweight message object expected by plugins
            # CRITICAL: Preserve interface_path from original message/context so error messages route correctly
            correction_message = SimpleNamespace()
            correction_message.chat_id = chat_id or getattr(bot, "chat_id", None) or -1
            correction_message.text = correction_prompt
            correction_message.thread_id = (
                payload_thread_id  # Use thread_id from original conversation
            )
            correction_message.date = None
            correction_message.from_user = None
            correction_message.chat = SimpleNamespace(
                id=correction_message.chat_id, type="private"
            )
            # Extract interface_path from original message or context
            correction_message.interface_path = None
            if message and hasattr(message, "interface_path"):
                correction_message.interface_path = message.interface_path
            elif context and "interface_path" in context:
                correction_message.interface_path = context["interface_path"]

            scope_label = "unknown"
            if context and isinstance(context.get("action_scope"), str):
                scope_label = context.get("action_scope")
            log_debug(
                f"[corrector_middleware] Requesting correction from LLM (attempt {attempt}/{max_retries}) - chat_id={correction_message.chat_id}, thread_id={correction_message.thread_id}, scope={scope_label}"
            )

            # Mark that we are expecting a system reply for this chat_id so llm_to_interface can
            # consume it without forwarding and avoid re-entry loops.
            try:
                if correction_message.chat_id is not None:
                    _add_system_reply_expectation(str(correction_message.chat_id))
            except Exception:
                pass

            # Call plugin directly and await returned value when available
            try:
                corrected = await llm_plugin.handle_incoming_message(
                    bot, correction_message, correction_prompt
                )
            except TypeError:
                # Some plugins expect different signature; try fallback
                corrected = await llm_plugin.handle_incoming_message(
                    bot, correction_message, correction_prompt
                )
            # Normalize plugin output to string; some engines may erroneously
            # return integers or other types which would crash subsequent
            # len() calls.  Coerce and log a warning so author can fix the
            # engine implementation.
            if corrected is None:
                corrected = ""
            elif not isinstance(corrected, str):
                try:
                    log_warning(
                        f"[corrector_middleware] LLM plugin returned non-str {type(corrected)}; coercing to str"
                    )
                except Exception:
                    pass
                corrected = str(corrected)

            # Log corrected result type/length
            try:
                log_debug(
                    f"[corrector_middleware] LLM returned type={type(corrected)} len={(len(corrected) if isinstance(corrected, str) else 'N/A')}"
                )
            except Exception:
                pass

            if corrected and isinstance(corrected, str):
                log_debug(
                    f"[corrector_middleware] LLM returned text len={len(corrected)}"
                )
                # If corrected contains valid JSON, return it (do not echo to chat)
                try:
                    parsed_json, _ = extract_json_from_text(
                        corrected, return_metadata=True
                    )
                    if parsed_json:
                        # Special-case enforcement for ollama_serve: if the original
                        # interface was `ollama_serve`, make sure any `message_*`
                        # actions are routed back to `message_ollama_serve` and
                        # their payload.interface_path points to `ollama_serve/...`.
                        if originating_interface == "ollama_serve" and isinstance(
                            parsed_json, dict
                        ):
                            rewritten = False
                            actions = parsed_json.get("actions", [])
                            if isinstance(actions, list):
                                for act in actions:
                                    if not isinstance(act, dict):
                                        continue
                                    a_type = act.get("type", "")
                                    # Rewrite any message_* target to message_ollama_serve
                                    if (
                                        a_type.startswith("message_")
                                        and a_type != "message_ollama_serve"
                                    ):
                                        act["type"] = "message_ollama_serve"
                                        rewritten = True

                                    # Ensure payload.interface_path is an ollama_serve path
                                    payload = act.get("payload") or {}
                                    if isinstance(payload, dict):
                                        iface_path = payload.get("interface_path", "")
                                        if not iface_path.startswith("ollama_serve"):
                                            id_for_iface = chat_id or (
                                                context.get("chat_id")
                                                if context
                                                else None
                                            )
                                            payload["interface_path"] = (
                                                f"ollama_serve/{id_for_iface or '<chat_id>'}"
                                            )
                                            rewritten = True

                                        if act.get("type") == "message_ollama_serve":
                                            conversation_id = payload.get(
                                                "conversation_id"
                                            )
                                            if conversation_id is None:
                                                conversation_id = (
                                                    context.get("conversation_id")
                                                    if context
                                                    else None
                                                )
                                            payload.setdefault(
                                                "target",
                                                chat_id
                                                or conversation_id
                                                or id_for_iface
                                                or "<chat_id>",
                                            )
                                            payload.setdefault(
                                                "conversation_id",
                                                conversation_id,
                                            )
                                            act["payload"] = payload
                                            rewritten = True
                            if rewritten:
                                # Serialize back to JSON string before returning
                                corrected = json.dumps(parsed_json, ensure_ascii=False)
                                log_info(
                                    "[corrector_middleware] Rewrote corrected actions to 'message_ollama_serve' for originating interface 'ollama_serve'"
                                )
                                # Return rewritten JSON immediately for ollama_serve origin
                                return corrected

                        corrected = json.dumps(parsed_json, ensure_ascii=False)
                        log_info(
                            "[corrector_middleware] Valid JSON correction recovered successfully"
                        )
                        return corrected

                except Exception as e:
                    log_warning(
                        f"[corrector_middleware] Exception while processing corrected JSON: {e}"
                    )
                    log_debug(getattr(e, "__traceback__", str(e)))

                # Not valid JSON yet — DO NOT send LLM suggestion to chat (policy)
                log_debug(
                    "[corrector_middleware] LLM suggestion is not valid JSON; will retry without echoing to interface"
                )

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
    log_warning(
        f"[corrector_middleware] Exhausted {max_retries} attempts without valid JSON; blocking message for chat_id={chat_id}"
    )
    if llm_plugin and hasattr(llm_plugin, "_send_error_message") and message:
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
        log_debug(
            f"[chain] interface_to_llm called -> send_to_llm_func={send_to_llm_func} kwargs_keys={list(kwargs.keys())}"
        )
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
    # guard against malicious or buggy plugins returning non-string objects
    if not isinstance(text, str):
        try:
            log_warning(
                f"[llm_to_interface] LLM returned non-str {type(text)}; coercing to str"
            )
        except Exception:
            pass
        text = str(text)

    # Extract chat_id for logging
    chat_id = kwargs.get("chat_id")
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
        log_debug(
            f"[chain] llm_to_interface called -> interface_send_func={interface_send_func}"
        )
    except Exception:
        pass

    # Mark this forwarding attempt explicitly as an LLM response so downstream
    # components and the orchestrator can detect origin without changes to every
    # LLM engine. Engines that already set this can override it.
    try:
        kwargs.setdefault("is_llm_response", True)
    except Exception:
        pass

    # Normalize mojibake/unescaped characters on every LLM output before we
    # do anything else.  This recovers cases where UTF-8 bytes have been
    # mis-decoded as latin1 (the garbled example the user saw).
    try:
        from core.text_utils import normalize_for_outbound

        norm = normalize_for_outbound(text)
        if norm and norm != text:
            log_debug("[llm_to_interface] Normalized LLM text (mojibake/unescape)")
            text = norm
    except Exception:
        pass

    # Strip internal emotion tags from every incoming LLM message so the text
    # remains clean for downstream processors and interface outputs.
    try:
        from plugins.emotion_manager import strip_emotion_tags

        cleaned = strip_emotion_tags(text)
        if cleaned != text:
            log_debug("[llm_to_interface] Stripped emotion tags from LLM text")
            text = cleaned
    except Exception:
        pass

    # Suppress empty/whitespace LLM replies early — centralize handling here
    if kwargs.get("is_llm_response", False) and (not text or not text.strip()):
        log_debug(
            "[transport] Empty LLM response received; skipping corrector and not forwarding"
        )
        return None

    # --- the following section was previously maintained as a commented-out
    # reference.  We now re-enable it so llm_to_interface can drive the
    # corrector, message_chain, and grillo checker just as before. ---

    # If the LLM sent a JSON-like payload that looks like a correction/system message,
    # handle it via the parser orchestrator to avoid echoing it back into the interfaces
    try:
        json_payload = None
        json_metadata = None
        if text and text.strip():
            try:
                json_payload, json_metadata = extract_json_from_text(
                    text, return_metadata=True
                )
            except Exception:
                json_payload = None
                json_metadata = None

        # Check if JSON was corrupted during parsing
        is_corrupted = json_metadata and (
            json_metadata.get("recovered", False)
            or json_metadata.get("unparsed_content", "")
        )

        if is_corrupted:
            log_warning(
                "[llm_to_interface] 🔧 Corrupted JSON detected - activating corrector to regenerate damaged actions"
            )
            log_debug(
                f"[llm_to_interface] Unparsed content ({len(json_metadata.get('unparsed_content', ''))} chars): {json_metadata.get('unparsed_content', '')[:200]}..."
            )

        # Check if LLM returned plain text with NO JSON at all (violates LLM instructions)
        is_plain_text_only = not json_payload and text and text.strip()
        if is_plain_text_only:
            log_warning(
                "[llm_to_interface] ⚠️ LLM returned plain text with NO JSON - violates instructions! Activating corrector to request JSON format"
            )

        # Detect correction/system payloads (top-level "system_message") OR corrupted JSON OR plain text only
        if (
            (isinstance(json_payload, dict) and "system_message" in json_payload)
            or is_corrupted
            or is_plain_text_only
        ):
            try:
                # Determine bot instance from args if present
                bot = args[0] if args and len(args) > 0 else None

                # Attempt to determine chat_id from kwargs or positional args
                chat_id = kwargs.get("chat_id")
                if chat_id is None and len(args) > 1:
                    maybe = args[1]
                    if isinstance(maybe, (int, str)):
                        chat_id = maybe

                # Sanitize chat_id: accept int or numeric string; set None otherwise
                try:
                    if isinstance(chat_id, str):
                        s = chat_id.strip()
                        if s == "" or not s.lstrip("-").isdigit():
                            # invalid/empty string -> normalize to None
                            log_debug(
                                f"[llm_to_interface] Sanitizing invalid chat_id '{chat_id}' -> None"
                            )
                            chat_id = None
                        else:
                            chat_id = int(s)
                    elif isinstance(chat_id, int):
                        pass
                    else:
                        chat_id = None
                except Exception:
                    log_debug(
                        f"[llm_to_interface] Could not coerce chat_id '{chat_id}' to int; setting to None"
                    )
                    chat_id = None

                # Build lightweight message and context objects for orchestrator
                from types import SimpleNamespace
                from datetime import datetime

                message = SimpleNamespace()
                message.chat_id = chat_id
                message.text = ""
                message.original_text = text
                message.thread_id = kwargs.get("thread_id")
                # Mark this message as originating from the AI/cortex layer so the
                # orchestrator will process it.  The `is_llm_response` flag is also
                # set earlier, but we propagate a concrete attribute here for
                # downstream code.
                message.from_cortex = True
                message.date = datetime.utcnow()

                current_interface = kwargs.get("interface") or (
                    getattr(bot, "get_interface_id", lambda: "unknown")()
                    if bot
                    else "unknown"
                )
                corrector_context = {
                    "interface": current_interface,
                    "original_chat_id": chat_id,
                    "original_thread_id": kwargs.get("thread_id"),
                    "original_text": text[:500] if text else "",
                    "from_cortex": True,
                }

                # If JSON is corrupted, extract already-completed actions from the recovered JSON
                completed_actions = []
                if is_corrupted and json_payload:
                    log_info(
                        "[llm_to_interface] Extracting completed actions from corrupted JSON"
                    )
                    # Extract actions from recovered JSON
                    if isinstance(json_payload, dict) and "actions" in json_payload:
                        recovered_actions = json_payload.get("actions", [])
                        if isinstance(recovered_actions, list):
                            completed_actions = [
                                a.get("type")
                                for a in recovered_actions
                                if isinstance(a, dict) and "type" in a
                            ]
                            log_info(
                                f"[llm_to_interface] Found {len(completed_actions)} actions in recovered JSON: {completed_actions}"
                            )

                # Delegate to the parser orchestrator (will call corrector middleware as needed)
                from core import action_parser

                orchestrator_result = await action_parser.corrector_orchestrator(
                    text,
                    corrector_context,
                    bot,
                    message,
                    completed_actions=completed_actions if is_corrupted else None,
                    force_correction=is_plain_text_only,  # Force corrector to run for plain text
                )

                if orchestrator_result is True:
                    log_debug(
                        "[llm_to_interface] corrector_orchestrator executed actions; not forwarding text"
                    )
                    return None
                elif orchestrator_result is False:
                    log_warning(
                        "[llm_to_interface] corrector_orchestrator blocked message; not forwarding"
                    )
                    return None
                else:
                    log_debug(
                        "[llm_to_interface] corrector_orchestrator returned None; forwarding as usual"
                    )

            except Exception as e:
                import traceback

                log_warning(f"[llm_to_interface] Orchestrator handling failed: {e}")
                log_debug(f"[llm_to_interface] Traceback: {traceback.format_exc()}")
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
        source = "llm" if kwargs.get("is_llm_response", False) else "interface"
        # Build a message object compatible with message_chain
        from types import SimpleNamespace
        from datetime import datetime

        message = SimpleNamespace()
        # Try to extract chat_id from kwargs/args
        chat_id = kwargs.get("chat_id")
        if chat_id is None and args:
            maybe = args[1] if len(args) > 1 else None
            chat_id = maybe if isinstance(maybe, (int, str)) else None
        message.chat_id = chat_id
        message.text = ""
        message.original_text = text
        message.thread_id = kwargs.get("thread_id")
        message.date = datetime.utcnow()
        # If this chat_id is marked as expecting a system/LLM reply from the
        # corrector middleware, consume it here and do not re-enter the message
        # chain. This avoids infinite correction/forwarding loops.
        # However, only consume if the message actually looks like a correction response.
        try:
            if chat_id is not None and _is_expecting_system_reply(str(chat_id)):
                # Check if this is actually a correction response by looking for typical patterns
                is_correction_response = isinstance(text, str) and (
                    "system_message" in text
                    or text.strip().startswith('{"actions":')
                    or "correction" in text.lower()
                    or "corrected" in text.lower()
                )

                if is_correction_response:
                    _remove_system_reply_expectation(str(chat_id))
                    log_debug(
                        f"[llm_to_interface] Consuming expected system reply for chat_id={chat_id}; not forwarding to message_chain"
                    )
                    return None
                else:
                    # This doesn't look like a correction response, might be a normal message
                    # Log a warning and let it through
                    log_warning(
                        f"[llm_to_interface] Expected system reply for chat_id={chat_id} but message doesn't look like correction. Allowing through."
                    )
                    _remove_system_reply_expectation(
                        str(chat_id)
                    )  # Clear the expectation anyway
        except Exception:
            pass
        # Pass through to message chain
        result = await handle_incoming_message(
            bot=getattr(interface_send_func, "__self__", None) or None,
            message=message,
            text=text,
            source=source,
            context=kwargs.get("context", None),
            **kwargs,
        )

        # At this point the message_chain has completed. If this was an LLM plain-text
        # reply (no JSON was present originally) we optionally run the Grillo checker in
        # the background to detect and propose missing actions.
        try:
            # Ensure we only trigger for LLM-origin responses (plain-text or JSON)
            # but skip system/correction payloads (they are handled by corrector).
            if kwargs.get("is_llm_response", False) and not (
                isinstance(json_payload, dict) and "system_message" in json_payload
            ):
                # Avoid double-invoking Grillo on the same message
                already_checked = False
                try:
                    already_checked = getattr(message, "grillo_checked", False)
                except Exception:
                    already_checked = False

                if not already_checked:
                    # Respect config that allows async vs sync execution for tests
                    try:
                        from core.config_manager import config_registry

                        async_check = bool(
                            config_registry.get_value(
                                "GRILLO_ACTION_CHECK_ASYNC",
                                True,
                                value_type=bool,
                                group="grillo",
                                component="grillo",
                            )
                        )
                    except Exception:
                        async_check = True

                    # Build minimal context to pass through
                    checker_context = kwargs.get("context") or {}
                    checker_context = dict(checker_context)
                    # Attach last_action_result from message if present
                    if hasattr(message, "last_action_result"):
                        checker_context["last_action_result"] = getattr(
                            message, "last_action_result"
                        )
                    # Attach the final chain result (ACTIONS_EXECUTED / BLOCKED / LLM_FAILED / etc.)
                    try:
                        checker_context["chain_result"] = result
                    except Exception:
                        pass

                    # Get an original_user_message when available in context
                    original_user_message = (
                        checker_context.get("original_user_message")
                        or checker_context.get("original_text")
                        or ""
                    )

                    if async_check:
                        try:
                            log_info(
                                f"[grillo] Scheduling checker for chat_id={getattr(message, 'chat_id', None)} chain_result={result}"
                            )
                            asyncio.create_task(
                                _grillo_fire_and_forget(
                                    getattr(interface_send_func, "__self__", None)
                                    or None,
                                    message,
                                    original_user_message,
                                    text,
                                    checker_context,
                                )
                            )
                            # Mark as scheduled
                            try:
                                setattr(message, "grillo_checked", True)
                            except Exception:
                                pass
                        except Exception as e:
                            log_debug(
                                "[llm_to_interface] Failed to schedule Grillo checker task: "
                                + str(e)
                            )
                    else:
                        # Synchronous for testing convenience
                        try:
                            await _grillo_fire_and_forget(
                                getattr(interface_send_func, "__self__", None) or None,
                                message,
                                original_user_message,
                                text,
                                checker_context,
                            )
                            try:
                                setattr(message, "grillo_checked", True)
                            except Exception:
                                pass
                        except Exception:
                            log_debug(
                                "[llm_to_interface] Grillo checker synchronous call failed"
                            )
        except Exception:
            log_debug(
                "[llm_to_interface] Grillo checker invocation encountered an error"
            )

        if result == "ACTIONS_EXECUTED" or result == "BLOCKED":
            # Orchestrator handled or blocked: nothing to forward
            return None
        elif result == "LLM_FAILED":
            # LLM failed and fallback message was already sent: nothing more to forward
            return None
        # Else forward as usual
        return await universal_send(interface_send_func, *args, text=text, **kwargs)
    except Exception as e:
        log_warning(f"[transport] message_chain delegation failed: {e}")
        return await universal_send(interface_send_func, *args, text=text, **kwargs)


async def notify_corrector_of_system_message(
    text: str,
    bot,
    chat_id: int | str | None = None,
    thread_id: int | None = None,
    interface: str = "telegram",
):
    """Send a manually-generated system message into the corrector.

    This is used by interfaces (currently only Telegram) when they need to
    trigger the correction loop for error notifications or invalid user
    input.  Unlike normal LLM-origin messages, these are *not* coming from the
    AI stack, so we explicitly mark ``from_cortex=False`` to prevent the
    orchestrator from re-processing them as model output.

    Args:
        text: The text to pass through the corrector (usually a JSON payload).
        bot: Bot instance (may be None for tests).
        chat_id: Optional chat identifier associated with the message.
        thread_id: Optional thread identifier.
        interface: Name of originating interface for logging/context.
    """
    from types import SimpleNamespace
    from datetime import datetime

    try:
        msg = SimpleNamespace()
        msg.chat_id = chat_id
        msg.text = ""
        msg.original_text = text
        msg.thread_id = thread_id
        msg.date = datetime.utcnow()
        msg.from_cortex = False
    except Exception:
        msg = None

    context = {"interface": interface}
    if chat_id is not None:
        context["original_chat_id"] = chat_id
    if thread_id is not None:
        context["original_thread_id"] = thread_id

    try:
        from core import action_parser

        await action_parser.corrector_orchestrator(text, context, bot, msg)
    except Exception as e:
        log_debug(f"[transport] notify_corrector_of_system_message failed: {e}")
    return
    try:
        json_payload = None
        json_metadata = None
        if text and text.strip():
            try:
                json_payload, json_metadata = extract_json_from_text(
                    text, return_metadata=True
                )
            except Exception:
                json_payload = None
                json_metadata = None

        # Check if JSON was corrupted during parsing
        is_corrupted = json_metadata and (
            json_metadata.get("recovered", False)
            or json_metadata.get("unparsed_content", "")
        )

        if is_corrupted:
            log_warning(
                "[llm_to_interface] 🔧 Corrupted JSON detected - activating corrector to regenerate damaged actions"
            )
            log_debug(
                f"[llm_to_interface] Unparsed content ({len(json_metadata.get('unparsed_content', ''))} chars): {json_metadata.get('unparsed_content', '')[:200]}..."
            )

        # Check if LLM returned plain text with NO JSON at all (violates LLM instructions)
        is_plain_text_only = not json_payload and text and text.strip()
        if is_plain_text_only:
            log_warning(
                "[llm_to_interface] ⚠️ LLM returned plain text with NO JSON - violates instructions! Activating corrector to request JSON format"
            )

        # Detect correction/system payloads (top-level "system_message") OR corrupted JSON OR plain text only
        if (
            (isinstance(json_payload, dict) and "system_message" in json_payload)
            or is_corrupted
            or is_plain_text_only
        ):
            try:
                # Determine bot instance from args if present
                bot = args[0] if args and len(args) > 0 else None

                # Attempt to determine chat_id from kwargs or positional args
                chat_id = kwargs.get("chat_id")
                if chat_id is None and len(args) > 1:
                    maybe = args[1]
                    if isinstance(maybe, (int, str)):
                        chat_id = maybe

                # Sanitize chat_id: accept int or numeric string; set None otherwise
                try:
                    if isinstance(chat_id, str):
                        s = chat_id.strip()
                        if s == "" or not s.lstrip("-").isdigit():
                            # invalid/empty string -> normalize to None
                            log_debug(
                                f"[llm_to_interface] Sanitizing invalid chat_id '{chat_id}' -> None"
                            )
                            chat_id = None
                        else:
                            chat_id = int(s)
                    elif isinstance(chat_id, int):
                        pass
                    else:
                        chat_id = None
                except Exception:
                    log_debug(
                        f"[llm_to_interface] Could not coerce chat_id '{chat_id}' to int; setting to None"
                    )
                    chat_id = None

                # Build lightweight message and context objects for orchestrator
                from types import SimpleNamespace
                from datetime import datetime

                message = SimpleNamespace()
                message.chat_id = chat_id
                message.text = ""
                message.original_text = text
                message.thread_id = kwargs.get("thread_id")
                # Mark this message as originating from the AI/cortex layer so the
                # orchestrator will process it.  The `is_llm_response` flag is also
                # set earlier, but we propagate a concrete attribute here for
                # downstream code.
                message.from_cortex = True
                message.date = datetime.utcnow()

                current_interface = kwargs.get("interface") or (
                    getattr(bot, "get_interface_id", lambda: "unknown")()
                    if bot
                    else "unknown"
                )
                corrector_context = {
                    "interface": current_interface,
                    "original_chat_id": chat_id,
                    "original_thread_id": kwargs.get("thread_id"),
                    "original_text": text[:500] if text else "",
                    "from_cortex": True,
                }

                # If JSON is corrupted, extract already-completed actions from the recovered JSON
                completed_actions = []
                if is_corrupted and json_payload:
                    log_info(
                        "[llm_to_interface] Extracting completed actions from corrupted JSON"
                    )
                    # Extract actions from recovered JSON
                    if isinstance(json_payload, dict) and "actions" in json_payload:
                        recovered_actions = json_payload.get("actions", [])
                        if isinstance(recovered_actions, list):
                            completed_actions = [
                                a.get("type")
                                for a in recovered_actions
                                if isinstance(a, dict) and "type" in a
                            ]
                            log_info(
                                f"[llm_to_interface] Found {len(completed_actions)} actions in recovered JSON: {completed_actions}"
                            )

                # Delegate to the parser orchestrator (will call corrector middleware as needed)
                from core import action_parser

                orchestrator_result = await action_parser.corrector_orchestrator(
                    text,
                    corrector_context,
                    bot,
                    message,
                    completed_actions=completed_actions if is_corrupted else None,
                    force_correction=is_plain_text_only,  # Force corrector to run for plain text
                )

                if orchestrator_result is True:
                    log_debug(
                        "[llm_to_interface] corrector_orchestrator executed actions; not forwarding text"
                    )
                    return None
                elif orchestrator_result is False:
                    log_warning(
                        "[llm_to_interface] corrector_orchestrator blocked message; not forwarding"
                    )
                    return None
                else:
                    log_debug(
                        "[llm_to_interface] corrector_orchestrator returned None; forwarding as usual"
                    )

            except Exception as e:
                import traceback

                log_warning(f"[llm_to_interface] Orchestrator handling failed: {e}")
                log_debug(f"[llm_to_interface] Traceback: {traceback.format_exc()}")
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
        source = "llm" if kwargs.get("is_llm_response", False) else "interface"
        # Build a message object compatible with message_chain
        from types import SimpleNamespace
        from datetime import datetime

        message = SimpleNamespace()
        # Try to extract chat_id from kwargs/args
        chat_id = kwargs.get("chat_id")
        if chat_id is None and args:
            maybe = args[1] if len(args) > 1 else None
            chat_id = maybe if isinstance(maybe, (int, str)) else None
        message.chat_id = chat_id
        message.text = ""
        message.original_text = text
        message.thread_id = kwargs.get("thread_id")
        message.date = datetime.utcnow()
        # If this chat_id is marked as expecting a system/LLM reply from the
        # corrector middleware, consume it here and do not re-enter the message
        # chain. This avoids infinite correction/forwarding loops.
        # However, only consume if the message actually looks like a correction response.
        try:
            if chat_id is not None and _is_expecting_system_reply(str(chat_id)):
                # Check if this is actually a correction response by looking for typical patterns
                is_correction_response = isinstance(text, str) and (
                    "system_message" in text
                    or text.strip().startswith('{"actions":')
                    or "correction" in text.lower()
                    or "corrected" in text.lower()
                )

                if is_correction_response:
                    _remove_system_reply_expectation(str(chat_id))
                    log_debug(
                        f"[llm_to_interface] Consuming expected system reply for chat_id={chat_id}; not forwarding to message_chain"
                    )
                    return None
                else:
                    # This doesn't look like a correction response, might be a normal message
                    # Log a warning and let it through
                    log_warning(
                        f"[llm_to_interface] Expected system reply for chat_id={chat_id} but message doesn't look like correction. Allowing through."
                    )
                    _remove_system_reply_expectation(
                        str(chat_id)
                    )  # Clear the expectation anyway
        except Exception:
            pass
        # Pass through to message chain
        result = await handle_incoming_message(
            bot=getattr(interface_send_func, "__self__", None) or None,
            message=message,
            text=text,
            source=source,
            context=kwargs.get("context", None),
            **kwargs,
        )

        # At this point the message_chain has completed. If this was an LLM plain-text
        # reply (no JSON was present originally) we optionally run the Grillo checker in
        # the background to detect and propose missing actions.
        try:
            # Ensure we only trigger for LLM-origin responses (plain-text or JSON)
            # but skip system/correction payloads (they are handled by corrector).
            if kwargs.get("is_llm_response", False) and not (
                isinstance(json_payload, dict) and "system_message" in json_payload
            ):
                # Avoid double-invoking Grillo on the same message
                already_checked = False
                try:
                    already_checked = getattr(message, "grillo_checked", False)
                except Exception:
                    already_checked = False

                if not already_checked:
                    # Respect config that allows async vs sync execution for tests
                    try:
                        from core.config_manager import config_registry

                        async_check = bool(
                            config_registry.get_value(
                                "GRILLO_ACTION_CHECK_ASYNC",
                                True,
                                value_type=bool,
                                group="grillo",
                                component="grillo",
                            )
                        )
                    except Exception:
                        async_check = True

                    # Build minimal context to pass through
                    checker_context = kwargs.get("context") or {}
                    checker_context = dict(checker_context)
                    # Attach last_action_result from message if present
                    if hasattr(message, "last_action_result"):
                        checker_context["last_action_result"] = getattr(
                            message, "last_action_result"
                        )
                    # Attach the final chain result (ACTIONS_EXECUTED / FORWARD_AS_TEXT / etc.)
                    try:
                        checker_context["chain_result"] = result
                    except Exception:
                        pass

                    # Get an original_user_message when available in context
                    original_user_message = (
                        checker_context.get("original_user_message")
                        or checker_context.get("original_text")
                        or ""
                    )

                    if async_check:
                        try:
                            log_info(
                                f"[grillo] Scheduling checker for chat_id={getattr(message, 'chat_id', None)} chain_result={result}"
                            )
                            asyncio.create_task(
                                _grillo_fire_and_forget(
                                    getattr(interface_send_func, "__self__", None)
                                    or None,
                                    message,
                                    original_user_message,
                                    text,
                                    checker_context,
                                )
                            )
                            # Mark as scheduled
                            try:
                                setattr(message, "grillo_checked", True)
                            except Exception:
                                pass
                        except Exception as e:
                            log_debug(
                                "[llm_to_interface] Failed to schedule Grillo checker task: "
                                + str(e)
                            )
                    else:
                        # Synchronous for testing convenience
                        try:
                            await _grillo_fire_and_forget(
                                getattr(interface_send_func, "__self__", None) or None,
                                message,
                                original_user_message,
                                text,
                                checker_context,
                            )
                            try:
                                setattr(message, "grillo_checked", True)
                            except Exception:
                                pass
                        except Exception:
                            log_debug(
                                "[llm_to_interface] Grillo checker synchronous call failed"
                            )
        except Exception:
            log_debug(
                "[llm_to_interface] Grillo checker invocation encountered an error"
            )

        if result == "ACTIONS_EXECUTED" or result == "BLOCKED":
            # Orchestrator handled or blocked: nothing to forward
            return None
        elif result == "LLM_FAILED":
            # LLM failed and fallback message was already sent: nothing more to forward
            return None
        # Else forward as usual
        return await universal_send(interface_send_func, *args, text=text, **kwargs)
    except Exception as e:
        log_warning(f"[transport] message_chain delegation failed: {e}")
        return await universal_send(interface_send_func, *args, text=text, **kwargs)
