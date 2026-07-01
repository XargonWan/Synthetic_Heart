import copy
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, Optional, Dict, Union, Tuple
from core.logging_utils import log_debug, log_info, log_warning


_INLINE_MEDIA_KEYS = {
    "image_path",
    "audio_path",
    "video_path",
    "file_path",
    "path",
}
_BASE64_KEYS = {"data", "base64"}
_BASE64_LIST_PATTERN = re.compile(r"(?:^|_)b64(?:$|_)", re.IGNORECASE)


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


def _looks_like_inline_media_value(value: str) -> bool:
    compact = "".join(value.split())
    if not compact:
        return False
    if compact.startswith("data:") and ";base64," in compact:
        return True
    if len(compact) < 256:
        return False
    if not re.fullmatch(r"[A-Za-z0-9+/=_-]+", compact):
        return False
    return True


def _redacted_text(label: str, value: Any) -> str:
    return f"<{label}: {len(str(value))} chars>"


def _redact_multimodal_value(key: str, value: Any) -> Any:
    if key in _BASE64_KEYS and isinstance(value, str):
        return _redacted_text("redacted", value)

    if _BASE64_LIST_PATTERN.search(key):
        if isinstance(value, str):
            return _redacted_text("redacted", value)
        if isinstance(value, list):
            return [
                _redacted_text("redacted", item) if isinstance(item, str) else item
                for item in value
            ]

    if key in _INLINE_MEDIA_KEYS and isinstance(value, str):
        if _looks_like_inline_media_value(value):
            return _redacted_text("redacted-inline-media", value)

    return value


def _redact_multimodal_recursive(obj: Any) -> Any:
    if isinstance(obj, Mapping):
        redacted: dict[Any, Any] = {}
        for key, value in obj.items():
            key_str = str(key)
            maybe_redacted = _redact_multimodal_value(key_str, value)
            if maybe_redacted is not value:
                redacted[key] = maybe_redacted
            else:
                redacted[key] = _redact_multimodal_recursive(value)
        return redacted

    if isinstance(obj, Sequence) and not isinstance(obj, (str, bytes, bytearray)):
        return [_redact_multimodal_recursive(value) for value in obj]

    return obj


def redact_multimodal_for_logging(obj: Any) -> Any:
    """Return a deep-copied log-safe view with heavy multimodal data redacted."""
    if isinstance(obj, str):
        stripped = obj.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                parsed = json.loads(obj)
            except Exception:
                return obj
            return _redact_multimodal_recursive(parsed)
        return obj

    return _redact_multimodal_recursive(copy.deepcopy(obj))


def _repair_premature_string_close(text: str) -> str:
    """Repair JSON where the LLM closes a string value prematurely then continues
    with literal \\n escape sequences and more text outside the string.

    LLM produces (invalid JSON):
        "key": "First paragraph." \\n\\nSecond paragraph.

    After repair (valid JSON):
        "key": "First paragraph.\\n\\nSecond paragraph."

    The \\n sequences remain as JSON string escapes; the premature closing quote
    is removed and a proper one is appended after the continuation text.
    """
    # Match: premature closing " + optional spaces + one or more literal \\n sequences
    # + continuation text up to the next real newline, quote, or closing brace.
    # In the regex, \\n matches the two-char sequence backslash+n in the text string.
    # This pattern can only occur in already-invalid JSON, so false-positive risk is nil.
    pattern = re.compile(r'"( *\\n)+([^\n"]*)')

    def _fix(m: re.Match) -> str:
        # m.group(0) starts with " (premature close), e.g. '" \\n\\nSome text'
        # Drop the leading " and close properly after the continuation content.
        body = m.group(0)[1:].rstrip()
        return body + '"'

    return pattern.sub(_fix, text)


_APOSTROPHE_ESCAPED_TAIL_RE = re.compile(
    r"'((?:,\s*\\\"[A-Za-z_][A-Za-z0-9_]*\\\"\s*:\s*"
    r"(?:\\\"[^\"\\]*\\\"|-?\d+(?:\.\d+)?|true|false|null)\s*)+)"
)


def _repair_apostrophe_closed_escaped_tail(text: str) -> str:
    """Repair JSON where the LLM closes a string value with an apostrophe
    instead of a double quote, then continues with escaped-quote sibling
    keys that were meant to be real JSON object keys, not string content.

    LLM produces (invalid JSON):
        "text": "Some reply!', \\"interface_path\\": \\"telegram_bot/1\\"}

    After repair (valid JSON):
        "text": "Some reply!", "interface_path": "telegram_bot/1"}

    The apostrophe becomes the real closing quote, and the escaped quotes in
    the trailing key/value run are unescaped back into real JSON structure.
    A legitimate JSON string cannot contain an unescaped apostrophe directly
    followed by an escaped-quote ``"key": value`` run like this, so the
    false-positive risk is nil.
    """

    def _fix(m: re.Match) -> str:
        tail = m.group(1)
        return '"' + tail.replace('\\"', '"')

    return _APOSTROPHE_ESCAPED_TAIL_RE.sub(_fix, text)


def _repair_json_string_speech_quotes(raw: str) -> str:
    """Re-escape unescaped speech-marker quotes inside known text-heavy JSON fields.

    LLM roleplay responses frequently embed dialogue in double-quotes inside JSON
    string values (e.g. ``"text": "She said "hello" and "goodbye""``), which
    breaks the JSON parser.  This function scans known text-bearing fields and
    re-escapes any ``"`` that is not a true string closer.

    A ``"`` is a *true closer* when the next non-horizontal-whitespace character
    is ``}``, ``]``, or end-of-string. A ``,`` or newline after ``"`` is only a
    true closer when what follows (after skipping whitespace) is a closing
    bracket or a ``"key":`` pattern — otherwise it's prose punctuation after
    embedded dialogue (e.g. ``"spoken line," she said, "more dialogue"``) and
    the quote is treated as embedded.

    ``\"`` (backslash-quote) where the character following the pair is structural,
    or immediately precedes a ``"key":`` pattern, is treated as a mistakenly-escaped
    closer (the ``\\`` is stripped).

    Real newline / carriage-return / tab characters inside the value are encoded
    as their JSON escape sequences so the result is always valid JSON.
    """

    _FIELD_RE = re.compile(
        r'"(?:text|content|personal_thought|interaction_summary|speech|reply|message_text)"'
        r"\s*:\s*\"",
        re.DOTALL,
    )

    def _is_structural(ch: str) -> bool:
        return ch in ",}]"

    def _looks_like_next_key(s: str, j: int) -> bool:
        """True if ``s[j:]`` starts with a ``"key":`` pattern (next sibling key)."""
        n = len(s)
        if j >= n or s[j] != '"':
            return False
        key_end = j + 1
        while key_end < n and s[key_end] != '"':
            if s[key_end] == "\\":
                key_end += 1
            key_end += 1
        colon = key_end + 1
        while colon < n and s[colon] in " \t":
            colon += 1
        return colon < n and s[colon] == ":"

    def _scan_value(s: str, pos: int) -> tuple[str, int]:
        """Scan the string value starting just after the opening quote.

        Returns ``(repaired_value_with_closing_quote, new_pos)`` where *new_pos*
        is the index immediately after the closing quote that was emitted.
        """
        n = len(s)
        out: list[str] = []
        found_close = False

        while pos < n:
            ch = s[pos]

            if ch == "\\":
                nxt = s[pos + 1] if pos + 1 < n else ""
                if nxt == '"':
                    # \"  — peek past the pair to decide intent.
                    j = pos + 2
                    while j < n and s[j] in " \t":
                        j += 1
                    if j >= n or _is_structural(s[j]):
                        # LLM mistakenly escaped the actual string closer,
                        # followed by JSON punctuation. Drop the backslash;
                        # emit just the closing quote.
                        out.append('"')
                        pos += 2
                        found_close = True
                        break
                    elif _looks_like_next_key(s, j):
                        # Mistakenly-escaped closer immediately followed by the
                        # next sibling key with no separating comma in the
                        # source (e.g. `secret,\" "reply_message_id": ...`).
                        # Drop the backslash and insert the missing comma.
                        out.append('",')
                        pos += 2
                        found_close = True
                        break
                    else:
                        # Legitimate escape — keep verbatim.
                        out.append('\\"')
                        pos += 2
                else:
                    # Other escape sequence (\n, \t, \\, …) — copy both chars.
                    out.append(ch)
                    pos += 1
                    if pos < n:
                        out.append(s[pos])
                        pos += 1

            elif ch == '"':
                # Skip horizontal whitespace to find the next meaningful char.
                j = pos + 1
                while j < n and s[j] in " \t":
                    j += 1

                if j >= n or s[j] in "}]":
                    # True closer: followed by closing brace/bracket or end.
                    out.append('"')
                    pos += 1
                    found_close = True
                    break

                elif s[j] == ",":
                    # Ambiguous: a real JSON separator before the next key, or
                    # just prose punctuation after embedded dialogue (e.g.
                    # `"spoken line," she said, "more dialogue"`). Peek past
                    # the comma for a real "key": pattern or closing bracket.
                    k = j + 1
                    while k < n and s[k] in " \t\n":
                        k += 1
                    if (k >= n or s[k] in "}]") or _looks_like_next_key(s, k):
                        out.append('"')
                        pos += 1
                        found_close = True
                        break
                    else:
                        # Prose comma after embedded dialogue — keep going.
                        out.append('\\"')
                        pos += 1

                elif s[j] == "\n":
                    # Newline after quote — check what the next non-blank line
                    # looks like to decide if this is a true closer.
                    k = j + 1
                    while k < n and s[k] in " \t":
                        k += 1
                    if k >= n or s[k] in "}]":
                        # Next line is a closing brace/bracket → true closer.
                        out.append('"')
                        pos += 1
                        found_close = True
                        break
                    elif _looks_like_next_key(s, k):
                        # Looks like a real key but the source has no comma
                        # between the closing quote and the newline-led next
                        # key → current " is the true closer; insert the
                        # missing comma.
                        out.append('",')
                        pos += 1
                        found_close = True
                        break
                    else:
                        # Quoted speech (or other narrative) on the next line
                        # → embedded quote.
                        out.append('\\"')
                        pos += 1

                elif _looks_like_next_key(s, j):
                    # Same-line case with no comma at all between the closing
                    # quote and the next key (e.g. `"text": "foo" "next_key": ...`).
                    out.append('",')
                    pos += 1
                    found_close = True
                    break

                else:
                    # Followed by non-structural, non-newline → embedded speech quote.
                    out.append('\\"')
                    pos += 1

            elif ch == "\n":
                out.append("\\n")
                pos += 1
            elif ch == "\r":
                out.append("\\r")
                pos += 1
            elif ch == "\t":
                out.append("\\t")
                pos += 1
            else:
                out.append(ch)
                pos += 1

        if not found_close:
            out.append('"')  # add missing closer if end-of-string was reached

        return "".join(out), pos

    result: list[str] = []
    last = 0
    for m in _FIELD_RE.finditer(raw):
        val_start = m.end()
        result.append(raw[last : m.end()])
        repaired_val, new_pos = _scan_value(raw, val_start)
        result.append(repaired_val)
        last = new_pos
    result.append(raw[last:])
    return "".join(result)


def _repair_inline_premature_string_close(text: str) -> str:
    """Repair JSON where the LLM prematurely closes a string with " then continues
    inline with space + text, potentially spanning multiple real newlines.

    LLM produces (invalid JSON):
        "text": "Mm-mmph!" I gasp, scratching at your thighs...
                                                 (more lines)
        "next_key": value

    After repair (valid JSON):
        "text": "Mm-mmph! I gasp, scratching at your thighs...\\n(more lines)"
        "next_key": value

    Real newlines in the continuation are encoded as \\n JSON escape sequences.
    A trailing comma before the next property line is stripped.
    """
    # Match:
    #   "           premature closing quote
    #   ( [A-Za-z]  space + letter starts the inline continuation (not a JSON token)
    #   [^"]*? )    rest of continuation, non-greedy, no embedded quotes
    #   (,?)        capture optional trailing comma (must be restored after the fixed string)
    #   [ \t]*      trailing whitespace
    #   (?=\n[ \t]*["}])  lookahead: newline before next JSON key or closing brace
    pattern = re.compile(
        r'"( [A-Za-z][^"]*?)(,?)[ \t]*(?=\n[ \t]*["}])',
        re.DOTALL,
    )

    def _fix(m: re.Match) -> str:
        continuation = m.group(1)
        trailing_comma = m.group(2)  # "," or ""
        # Encode real newlines as JSON \\n escape sequences
        encoded = continuation.replace("\n", "\\n")
        # Escape any stray embedded double-quotes
        encoded = encoded.replace('"', '\\"')
        # Restore the comma so the next sibling key stays valid JSON
        return encoded.rstrip() + '"' + trailing_comma

    return pattern.sub(_fix, text)


def extract_json_from_text(
    text: str, return_metadata: bool = False
) -> Union[Optional[Dict], Tuple[Optional[Dict], Dict[str, Any]]]:
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

    # Apply targeted repairs for premature string close patterns produced by LLMs.
    # Pass 1: literal \\n sequences outside the string  ("value." \\n\\nmore)
    # Pass 2: apostrophe used as a string closer, followed by an escaped-quote
    #         run of sibling keys that belong outside the string
    # Pass 3: re-escape unescaped speech quotes inside text-heavy fields
    repaired_text = _repair_premature_string_close(cleaned_text)
    repaired_text = _repair_apostrophe_closed_escaped_tail(repaired_text)
    repaired_text = _repair_json_string_speech_quotes(repaired_text)
    if repaired_text != cleaned_text:
        log_debug("[extract_json_from_text] Applied premature string close repair")
        texts_to_try = [repaired_text, cleaned_text, text.strip()]
    else:
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
        prefix_len = int(metadata.get("prefix_length", 0))
        suffix_len = int(metadata.get("suffix_length", 0))
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
