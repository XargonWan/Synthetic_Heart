import json
from collections.abc import Mapping, Sequence
from typing import Optional, Dict
from core.logging_utils import log_debug, log_info, log_warning


def custom_json_encoder(obj):
    """Fallback encoder that converts objects to dictionaries or strings."""
    if hasattr(obj, "__dict__"):
        return obj.__dict__
    return str(obj)


def dumps(data, **kwargs):
    """Serialize ``data`` to JSON using the custom encoder."""
    kwargs.setdefault("ensure_ascii", False)
    return json.dumps(data, default=custom_json_encoder, **kwargs)


def sanitize_for_json(obj):
    """Recursively convert objects into JSON-serializable structures."""
    if isinstance(obj, Mapping):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, Sequence) and not isinstance(obj, (str, bytes, bytearray)):
        return [sanitize_for_json(v) for v in obj]
    try:
        json.dumps(obj)
        return obj
    except TypeError:
        if hasattr(obj, "__dict__"):
            return sanitize_for_json(obj.__dict__)
        return str(obj)


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
    """
    metadata = {
        "had_errors": False,
        "error_count": 0,
        "unparsed_content": "",
        "recovered": False,
        "had_extra_text": False,
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
    texts_to_try = [cleaned_text, text.strip()]

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
                    metadata["prefix_length"] = len(prefix)
                    metadata["suffix_length"] = len(suffix)
                    log_debug(
                        f"[extract_json_from_text] Found JSON with {extra_chars} extra chars (best so far)"
                    )

            except json.JSONDecodeError as e:
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

    return (found_json, metadata) if return_metadata else found_json
