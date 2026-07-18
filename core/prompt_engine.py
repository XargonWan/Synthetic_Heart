# core/prompt_engine.py

import base64
import mimetypes
import random
import re
import time as time_module

from core.beat_utils import is_outbound_beat
from core.db import _get_db_type, get_conn_ctx
from core.synth_tagging import extract_tags, expand_tags
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.json_utils import dumps as json_dumps, redact_multimodal_for_logging
from core.config_manager import config_registry
from core.user_utils import get_user_display_name, get_user_usertag
from datetime import datetime, timezone
import os
import asyncio
from typing import Any, cast

# Lazily imported to avoid circular deps at module load time
# from core.prompt_request import PromptRequest, Turn, RuntimeContext, Attachment


# ---------------------------------------------------------------------------
# Turn parsing — convert formatted history strings to Turn objects
# ---------------------------------------------------------------------------

# Matches: [timestamp] SenderName [optional reply]: "content"
# Handles optional [from path] prefix. Deliberately does NOT match:
#   [diary timestamp] ...   (diary entries — no space inside timestamp brackets)
#   [thought timestamp] ... (thoughts)
if not hasattr(re, "_TURN_PARSE_RE_SENTINEL"):
    _TURN_PARSE_RE = re.compile(
        r"^(?:\[from\s[^\]]*\]\s+)?"  # optional [from ...] prefix
        r"\[[^\s\]]+\]\s+"  # [timestamp] — NO spaces inside brackets
        r'([^:\["]+?)'  # sender name (group 1)
        r"(?:\s+\[replied to [^\]]+\])?"  # optional reply annotation
        r':\s+"(.*)"$',  # : "content" (group 2)
        re.DOTALL,
    )
else:  # pragma: no cover
    _TURN_PARSE_RE = re.compile(r"(?!)")  # no-op fallback

# Default maximum prompt characters (CHARACTERS, NOT TOKENS)
# This is used as a safe fallback when no LLM engine provides explicit limits.
# The actual value comes from the active LLM engine's configuration.
# For model limits, see the individual cortex/llm_provider/* engines, e.g. MODEL_LIMITS_MAP["default"]
DEFAULT_MAX_PROMPT_CHARS = None  # Will be set dynamically from LLM engine

_ATTACHMENT_TEXT_CHAR_LIMIT = 12000
_PDF_PAGE_IMAGE_LIMIT = 4
_ATTACHMENT_TEXT_MIME_TYPES = {
    "application/json",
    "application/xml",
    "application/javascript",
    "application/x-javascript",
}
_ATTACHMENT_TEXT_EXTENSIONS = (
    ".txt",
    ".md",
    ".csv",
    ".log",
    ".json",
    ".xml",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".py",
    ".js",
    ".ts",
    ".html",
    ".css",
    ".sh",
    ".bat",
    ".rst",
    ".tex",
    ".sql",
)

_LEGACY_BUILD_JSON_PROMPT_WARNED = False

# How many recent messages to include in the explicit current chat recap
CHAT_RECAP_LAST_N = config_registry.get_var(
    "CHAT_RECAP_LAST_N",
    3,
    label="Chat recap last N",
    description="Number of recent messages from the current chat to include as a concise recap (current_chat_history).",
    group="core",
    component="prompt_engine",
    value_type=int,
)

# Diary history days
DIARY_HISTORY_DAYS = config_registry.get_var(
    "DIARY_HISTORY_DAYS",
    2,
    label="Diary History Days",
    description="Number of days of AI diary history to include in context.",
    group="core",
    component="diary",
    value_type=int,
)

INCLUDE_LOCAL_TIME_IN_PROMPTS = config_registry.get_var(
    "INCLUDE_LOCAL_TIME_IN_PROMPTS",
    True,
    label="Include local time in prompts",
    description="Whether to add authoritative local date, time, hour, and time-of-day fields to prompt payloads.",
    group="core",
    component="prompt_engine",
    value_type=bool,
)

USE_PERSONA_IN_SYSTEM_PROMPTS = config_registry.get_var(
    "USE_PERSONA_IN_SYSTEM_PROMPTS",
    True,
    label="Use Persona in System Prompts",
    description="Whether to prepend the persona/identity template in instructions.",
    group="core",
    component="prompt_engine",
    value_type=bool,
)


def minify_actions_block(
    available_actions: dict,
    lite: bool = False,
) -> dict:
    """Convert full action schemas to minimal versions for prompt.

    For LLM prompts, sends ONLY schema and brief description to minimize token usage.
    This dramatically reduces prompt size while preserving all critical information needed.

    When ``lite=True`` (Prompt Lite Mode for small/local models), applies aggressive
    minification on top of the standard pass:

    - Filters to essential actions only (message_*, diary, emotion, tts, animation)
    - Strips schemas down to brief-only (no schema object)

    Parameters
    ----------
    available_actions : dict
        Full actions block with schemas in new normalized format.
    lite : bool
        When True, apply aggressive filtering and strip to brief-only.

    Returns
    -------
    dict
        Minified actions block suitable for LLM prompts.
    """
    from core.action_schema_converter import (
        extract_for_llm_prompt,
        normalize_action_schema,
    )

    _LITE_ESSENTIAL_ACTIONS = (
        "create_personal_diary_entry",
        "update_emotion_state",
        "tts_speak",
        "use_animation",
    )

    minified = {}
    for action_name, action_def in available_actions.items():
        # In lite mode, skip non-essential actions
        if lite and not (
            action_name.startswith("message_") or action_name in _LITE_ESSENTIAL_ACTIONS
        ):
            continue

        # Normalize to new format (handles both old and new formats)
        normalized = normalize_action_schema(action_name, action_def)

        if lite:
            # Lite: brief-only, no schema
            minified[action_name] = {"brief": normalized.get("brief", "")}
        else:
            # Standard: schema + brief
            minified[action_name] = extract_for_llm_prompt(action_name, normalized)

    return minified


def _memory_merge_key(memory: Any) -> str:
    if isinstance(memory, dict):
        source = memory.get("source")
        item_id = memory.get("id")
        snippet = (
            memory.get("snippet") or memory.get("content") or memory.get("summary")
        )
        return f"{source}::{item_id}::{snippet}"
    return str(memory)


def _merge_memory_entries(existing: list[Any], incoming: list[Any]) -> list[Any]:
    merged = list(existing or [])
    seen = {_memory_merge_key(item) for item in merged}
    for item in incoming or []:
        item_key = _memory_merge_key(item)
        if item_key in seen:
            continue
        merged.append(item)
        seen.add(item_key)
    return merged


_NON_USER_FACING_ACTION_HINTS = (
    "admin only",
    "deprecated",
    "internal",
)
# Actions that are purely system/pipeline mechanisms and must never appear in
# the model-visible actions block, regardless of plugin description text.
#
# The audio_* / tts_speak actions remain callable internally (Vox routes voice
# replies through them), but the model must never pick them directly: to reply
# with voice it sets send_as_voice=true on the normal message_* action instead.
# Advertising the raw audio actions caused the model to emit spoken TEXT in the
# 'audio' field (expected a file path), so the voice was silently dropped.
_SYSTEM_ONLY_ACTION_NAMES: frozenset[str] = frozenset(
    {
        "static_inject",
        "audio_telegram_bot",
        "audio_discord_bot",
        "tts_speak",
    }
)
_CONTEXT_SEGMENT_SPLIT_RE = re.compile(r"(?:\n\s*|\s+)---(?:\s*\n|\s+)")
_SOUL_RECALLED_MEMORY_RE = re.compile(
    r"^\[SOUL recalled memory\s*\|\s*(?P<meta>[^\]]+)\]\s*(?P<body>.*)$",
    re.DOTALL,
)
_TIMED_CONTEXT_ENTRY_RE = re.compile(
    r"^\[(?P<label>diary|thought)\s+(?P<timestamp>[^\]]+)\]\s*(?P<body>.*)$",
    re.DOTALL,
)


def _action_source_tokens(action_def: Any) -> set[str]:
    if not isinstance(action_def, dict):
        return set()

    source = action_def.get("source")
    if isinstance(source, str):
        return {token.strip() for token in source.split(",") if token.strip()}
    if isinstance(source, (list, tuple, set)):
        return {str(token).strip() for token in source if str(token).strip()}
    return set()


def _is_non_user_facing_action(action_def: Any) -> bool:
    if not isinstance(action_def, dict):
        return False

    hint_text = " ".join(
        str(action_def.get(field) or "") for field in ("brief", "description")
    ).lower()
    return any(hint in hint_text for hint in _NON_USER_FACING_ACTION_HINTS)


def _derive_default_prompt_action_types(
    available_actions: dict[str, Any],
    interface_name: str | None,
) -> set[str]:
    try:
        from core.core_initializer import INTERFACE_REGISTRY

        interface_names = {str(name) for name in INTERFACE_REGISTRY.keys()}
    except Exception:
        interface_names = set()

    allowed: set[str] = set()
    current_interface = str(interface_name or "").strip()
    for action_name, action_def in available_actions.items():
        if action_name in _SYSTEM_ONLY_ACTION_NAMES:
            continue
        if _is_non_user_facing_action(action_def):
            continue

        if current_interface:
            action_interfaces = _action_source_tokens(action_def) & interface_names
            if action_interfaces and current_interface not in action_interfaces:
                continue

        allowed.add(action_name)

    return allowed


def _dedupe_context_segments(text: str) -> str:
    raw = str(text or "").strip()
    if not raw:
        return ""

    segments = _CONTEXT_SEGMENT_SPLIT_RE.split(raw)
    if len(segments) <= 1:
        return " ".join(raw.split())

    seen: set[str] = set()
    kept: list[str] = []
    for segment in segments:
        cleaned = " ".join(segment.split())
        if not cleaned:
            continue
        marker = cleaned.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        kept.append(cleaned)
    return " | ".join(kept)


def _humanize_context_entry(entry: Any, *, kind: str) -> str | None:
    if isinstance(entry, dict) and kind == "memories":
        for key in ("snippet", "content", "summary", "text"):
            value = entry.get(key)
            if value in (None, ""):
                continue
            normalized_value = _dedupe_context_segments(str(value))
            if normalized_value:
                return normalized_value

    text = str(entry or "").strip()
    if not text:
        return None

    soul_match = _SOUL_RECALLED_MEMORY_RE.match(text)
    if soul_match:
        meta_parts = [part.strip() for part in soul_match.group("meta").split("|")]
        body = _dedupe_context_segments(soul_match.group("body"))
        if not body:
            return None

        when = meta_parts[0] if meta_parts else ""
        qualifiers: list[str] = []
        for part in meta_parts[1:]:
            if not part:
                continue
            if part.startswith("emotion="):
                qualifiers.append(f"emotion: {part.split('=', 1)[1]}")
            else:
                qualifiers.append(part)

        prefix = "Recalled memory"
        if when:
            prefix += f" from {when}"
        if qualifiers:
            prefix += f" ({', '.join(qualifiers)})"
        return f"{prefix}: {body}"

    timed_match = _TIMED_CONTEXT_ENTRY_RE.match(text)
    if timed_match:
        label = timed_match.group("label")
        timestamp = timed_match.group("timestamp")
        body = timed_match.group("body").strip()

        if label == "diary":
            summary_part, _, thought_part = body.partition("| thought:")
            summary_text = re.sub(r"^summary:\s*", "", summary_part, flags=re.I)
            summary_text = _dedupe_context_segments(summary_text)
            if kind == "thoughts" and thought_part.strip():
                thought_text = _dedupe_context_segments(thought_part)
                return (
                    f"Thought from {timestamp}: {thought_text}"
                    if thought_text
                    else None
                )
            if kind == "history_recent":
                return (
                    f"Diary entry from {timestamp}: {summary_text}"
                    if summary_text
                    else None
                )

        cleaned_body = _dedupe_context_segments(body)
        if not cleaned_body:
            return None

        label_text = "Thought" if label == "thought" else "Diary entry"
        return f"{label_text} from {timestamp}: {cleaned_body}"

    if kind in {"memories", "thoughts"}:
        return _dedupe_context_segments(text)

    return text


def _sanitize_context_entries(entries: list[Any], *, kind: str) -> list[str]:
    sanitized: list[str] = []
    seen: set[str] = set()
    for entry in entries or []:
        normalized = _humanize_context_entry(entry, kind=kind)
        if not normalized:
            continue
        marker = normalized.casefold()
        if marker in seen:
            continue
        seen.add(marker)
        sanitized.append(normalized)
    return sanitized


_EXPLICIT_RUNTIME_FACT_REQUEST_RE = re.compile(
    r"(?ix)\b("
    r"what(?:'s| is)?\s+(?:the\s+)?(?:time|date|day|timezone|location|weather)\b|"
    r"(?:what|which)\s+(?:day|date|time|timezone|city)\b|"
    r"where\s+(?:am|are)\b|"
    r"current\s+(?:time|date|location|weather)\b|"
    r"local\s+(?:time|date|timezone)\b|"
    r"\b(?:schedule|scheduling|appointment|meeting|eta|arrive|arrival|depart|departure)\b"
    r")"
)


def _turn_requests_explicit_runtime_facts(text: str | None) -> bool:
    """Return True when the current turn needs exact time/date/location facts."""

    candidate = str(text or "").strip()
    if not candidate:
        return False
    return bool(_EXPLICIT_RUNTIME_FACT_REQUEST_RE.search(candidate))


# ---------------------------------------------------------------------------
# PromptRequest assembly helpers (added in Phase 1 of the prompt rewrite)
# ---------------------------------------------------------------------------


def _build_context_summary(
    context_section: dict[str, Any],
    is_grillo_internal: bool = False,
    include_explicit_runtime_facts: bool = False,
) -> str:
    """Format moderately-stable context parts into a plain text block.

    Includes: ambient runtime context, cross-chat history (history_recent),
    diary thoughts, tag-matched memories, participant bios.  Does NOT include
    ``history_current_chat`` (which becomes ``PromptRequest.conversation_history``)
    or fully-dynamic runtime values (current emotion values → ``RuntimeContext.emotions``).

    For Grillo internal beats (is_grillo_internal=True), this returns a MINIMAL
    context with only persona and optionally recent diary entries — no cross-chat
    history, no participant bios, minimal memories.
    """
    parts: list[str] = []

    # Reality Anchor (always-on temporal grounding)
    _date_val = str(context_section.get("date") or "").strip()
    _time_val = str(context_section.get("time") or "").strip()
    _day_of_week = str(context_section.get("day_of_week") or "").strip()
    _season = str(context_section.get("season") or "").strip()
    _loc_val = str(context_section.get("location") or "").strip()

    anchor_lines = ["[SYSTEM: REALITY ANCHOR]"]
    if _date_val:
        nice_date = _date_val
        try:
            dt_parsed = datetime.strptime(_date_val, "%Y-%m-%d")
            nice_date = dt_parsed.strftime("%B %d, %Y")
        except Exception:
            pass
        if _day_of_week:
            anchor_lines.append(f"- Current Date: {_day_of_week}, {nice_date}")
        else:
            anchor_lines.append(f"- Current Date: {nice_date}")

    if _time_val:
        nice_time = _time_val
        try:
            dt_parsed = datetime.strptime(_time_val, "%H:%M")
            nice_time = dt_parsed.strftime("%I:%M %p").lstrip("0")
        except Exception:
            pass
        anchor_lines.append(f"- Current Time: {nice_time}")

    if _season:
        anchor_lines.append(f"- Season: {_season}")

    if _loc_val:
        anchor_lines.append(f"- Current Location: {_loc_val}")

    curr_year = 2026
    if _date_val:
        try:
            curr_year = int(_date_val.split("-")[0])
        except Exception:
            pass

    anchor_lines.append(
        f"- Temporal Delta: It is now {curr_year}. It has been approximately 2-3 years since your primary core baseline training knowledge cutoff (early 2023 / mid-2024 depending on the model). Adjust your perspective on tools, software versions, and global releases to reflect this passage of time naturally."
    )
    parts.append("\n".join(anchor_lines))

    persona_preferences = str(context_section.get("persona_preferences") or "").strip()
    if persona_preferences:
        parts.append("[Persona background]\n" + persona_preferences)

    self_growth = str(context_section.get("self_growth") or "").strip()
    if self_growth:
        parts.append(
            "[Self-growth]\n"
            "The following is your evolving self-growth reflection: how you have "
            "grown and who you are becoming over time. Treat it as part of your "
            "current sense of self.\n" + self_growth
        )

    # Grillo internal beats skip cross-chat history and participants
    if not is_grillo_internal:
        history_recent = _sanitize_context_entries(
            list(context_section.get("history_recent") or []),
            kind="history_recent",
        )
        if history_recent:
            parts.append("[Recent context from other conversations]")
            for line in history_recent:
                parts.append(f"- {line}")

    thoughts = _sanitize_context_entries(
        list(context_section.get("thoughts") or []),
        kind="thoughts",
    )
    if not is_grillo_internal:
        # Grillo internal beats skip recent diary thoughts
        if thoughts:
            parts.append("[Thoughts and diary entries]")
            for t in thoughts:
                parts.append(f"- {t}")

    memories = _sanitize_context_entries(
        list(context_section.get("memories") or []),
        kind="memories",
    )
    if not is_grillo_internal:
        if memories:
            parts.append(
                "[Memory honesty notice]\n"
                "The memories below are recalled internal records. They can be incomplete, stale, or reconstructed. "
                "If a detail is not clearly supported, acknowledge uncertainty instead of inventing a recollection."
            )
        parts.append("[Relevant memories]")
        for m in memories:
            snippet = str(m)
            if len(snippet) > 400:
                snippet = snippet[:400] + "\u2026"
            parts.append(f"- {snippet}")
    elif is_grillo_internal and memories:
        # Internal beats (temporal_reflection, relationship, memory_consolidation, etc.)
        # need actual memory content to reflect on \u2014 include a compact block capped
        # tighter than normal chat to keep token cost low.
        parts.append("[Relevant memories]")
        for m in memories[:2]:
            snippet = str(m)
            if len(snippet) > 300:
                snippet = snippet[:300] + "\u2026"
            parts.append(f"- {snippet}")

    participants: Any = context_section.get("participants")
    # Grillo internal beats skip participant bios entirely
    if not is_grillo_internal and participants:
        if isinstance(participants, list):
            lines: list[str] = []
            for p in participants:
                if not isinstance(p, dict):
                    continue
                tag = str(p.get("usertag") or p.get("username") or "?")
                bio = str(p.get("short_bio") or "")
                nicks = p.get("nicknames")
                nick_str = (
                    f" (also: {', '.join(nicks)})"
                    if isinstance(nicks, list) and nicks
                    else ""
                )
                feelings = p.get("feelings")
                feel_str = (
                    f" [feels: {', '.join(str(f) for f in feelings)}]"
                    if isinstance(feelings, list) and feelings
                    else ""
                )
                if bio:
                    lines.append(f"- {tag}{nick_str}: {bio}{feel_str}")
            if lines:
                parts.append("[People in this conversation]")
                parts.extend(lines)
        elif isinstance(participants, str) and participants:
            parts.append("[People in this conversation]")
            parts.append(participants)

    return "\n".join(parts)


def _history_to_turns(
    history_lines: list[Any],
    synth_names: set[str],
) -> list[Any]:  # list[Turn] — import deferred to avoid circular dep at module load
    """Convert formatted history strings produced by HistoryEngine into Turn objects.

    Entries that cannot be parsed (diary lines, malformed lines) are silently
    skipped so they do not end up as junk turns.

    Args:
        history_lines: Lines from ``context_section["history_current_chat"]``.
        synth_names:   Lower-cased set of Synth name + aliases for role detection.

    Returns:
        List of ``Turn`` objects; may be empty.
    """
    from core.prompt_request import Turn

    # "self" is the canonical sender_name for the AI in history format
    all_synth_names = synth_names | {"self"}

    # A peer SyntH's messages land in this bot's own history (see
    # peer_synths.rst) with their own sender_name, which never matches this
    # bot's own synth_names -- without this, they'd silently fall into the
    # "user" bucket below with no way to tell them apart from the human. Role
    # still ends up "user" for peers (no third role in the chat protocol), but
    # each turn also carries an `is_peer` marker so the coalescing pass below
    # never blends a peer's lines into a genuine human turn (or vice versa).
    try:
        from core.peer_policy import get_peer_names

        peer_names_lower = {name.lower(): name for name in get_peer_names().values()}
    except Exception:
        peer_names_lower = {}

    # Entries are (Turn, is_peer) pairs. is_peer is only meaningful for
    # role == "user" turns; it is always False for "assistant" turns.
    entries: list[tuple[Turn, bool]] = []
    for line in history_lines:
        if not isinstance(line, str):
            continue
        m = _TURN_PARSE_RE.match(line)
        if not m:
            continue
        sender = m.group(1).strip()
        content = m.group(2)
        sender_lower = sender.lower()
        is_peer = False
        if sender_lower in all_synth_names:
            role = "assistant"
        else:
            role = "user"
            peer_name = peer_names_lower.get(sender_lower)
            if peer_name:
                # Tag so the model can tell this was a peer SyntH speaking,
                # not the human -- role must still be "user" (no third
                # role in the chat protocol), so attribution has to live
                # in the content itself.
                content = f"[{peer_name}]: {content}"
                is_peer = True
        entries.append((Turn(role=role, content=content), is_peer))

    if not entries:
        return []

    # If the visible history window starts mid-conversation, it can begin with
    # stale assistant-only turns (for example repeated outreach messages). When
    # a user turn exists later in the window, drop the unmatched leading
    # assistant turns so the model does not anchor on an orphaned monologue.
    if any(turn.role == "user" for turn, _ in entries):
        while entries and entries[0][0].role == "assistant":
            entries.pop(0)

    if not entries:
        return []

    # Coalesce consecutive same-role turns to keep provider history well-formed
    # even when the source chat log contains streaks of outreach or split user
    # messages. Peer-tagged turns only coalesce with other peer-tagged turns,
    # and genuine human turns only coalesce with other genuine human turns --
    # otherwise a real human line sandwiched between peer lines would get
    # blended into one indistinguishable "user" block.
    normalized_entries: list[tuple[Turn, bool]] = []
    for turn, is_peer in entries:
        if normalized_entries:
            prev_turn, prev_is_peer = normalized_entries[-1]
            if prev_turn.role == turn.role and prev_is_peer == is_peer:
                normalized_entries[-1] = (
                    Turn(
                        role=turn.role,
                        content=f"{prev_turn.content}\n\n{turn.content}",
                    ),
                    is_peer,
                )
                continue
        normalized_entries.append((turn, is_peer))

    return [turn for turn, _ in normalized_entries]


def _build_pr_attachments(
    image_data: dict[str, Any] | None,
    raw_attachments: list[Any] | None,
) -> list[Any]:  # list[Attachment] — import deferred
    """Convert image_data and raw attachments dicts into Attachment objects."""
    from core.prompt_request import Attachment

    result: list[Attachment] = []

    if isinstance(image_data, dict):
        # Legacy single-image dict from image_processor
        img_bytes = image_data.get("data") or (image_data.get("image_data") or {}).get(
            "data"
        )
        mime = image_data.get("mime_type") or "image/jpeg"
        meta = {k: v for k, v in image_data.items() if k not in ("data",)}
        result.append(Attachment(mime_type=mime, data=img_bytes, media_metadata=meta))

    for att in raw_attachments or []:
        if not isinstance(att, dict):
            continue
        mime_type = att.get("mime_type") or "application/octet-stream"
        filename = att.get("filename")
        media_metadata = dict(att.get("media_metadata") or {})
        extracted_text = media_metadata.get("extracted_text")
        if not isinstance(extracted_text, str) or not extracted_text.strip():
            extracted_text, was_truncated = _extract_attachment_text_preview(
                mime_type=mime_type,
                filename=filename,
                data=att.get("data"),
            )
            if extracted_text:
                media_metadata["extracted_text"] = extracted_text
                if was_truncated:
                    media_metadata["extracted_text_truncated"] = True
            elif mime_type == "application/pdf" or str(filename or "").lower().endswith(
                ".pdf"
            ):
                page_images, page_images_truncated = _extract_pdf_page_images(
                    filename=filename,
                    data=att.get("data"),
                )
                if page_images:
                    media_metadata["page_images"] = page_images
                    if page_images_truncated:
                        media_metadata["page_images_truncated"] = True
        result.append(
            Attachment(
                mime_type=mime_type,
                data=att.get("data"),
                filename=filename,
                media_metadata=media_metadata,
            )
        )

    return result


def _extract_attachment_text_preview(
    mime_type: str | None,
    filename: str | None,
    data: Any,
) -> tuple[str | None, bool]:
    """Extract a bounded text preview from textual or PDF attachments."""

    mime = str(mime_type or "").lower()
    filename_lower = str(filename or "").lower()

    raw_bytes = _coerce_attachment_bytes(data)
    if not raw_bytes:
        return None, False

    is_pdf = mime == "application/pdf" or filename_lower.endswith(".pdf")
    is_textual = mime.startswith("text/") or mime in _ATTACHMENT_TEXT_MIME_TYPES
    if not is_textual and filename_lower:
        is_textual = filename_lower.endswith(_ATTACHMENT_TEXT_EXTENSIONS)

    if is_pdf:
        try:
            from io import BytesIO

            from pypdf import PdfReader

            reader = PdfReader(BytesIO(raw_bytes))
            page_chunks: list[str] = []
            for page_num, page in enumerate(reader.pages, start=1):
                page_text = str(page.extract_text() or "").strip()
                if not page_text:
                    continue
                page_chunks.append(f"[Page {page_num}]\n{page_text}")
                joined = "\n\n".join(page_chunks)
                if len(joined) >= _ATTACHMENT_TEXT_CHAR_LIMIT:
                    return _truncate_attachment_text(joined)

            if page_chunks:
                return _truncate_attachment_text("\n\n".join(page_chunks))
        except Exception as exc:
            log_warning(
                f"[prompt_engine] Failed to extract PDF text from {filename or 'attachment'}: {exc}"
            )
        return None, False

    if not is_textual:
        return None, False

    text = raw_bytes.decode("utf-8", errors="replace").strip()
    if not text:
        return None, False
    return _truncate_attachment_text(text)


def _extract_pdf_page_images(
    filename: str | None,
    data: Any,
) -> tuple[list[dict[str, str]], bool]:
    """Extract up to a small number of page images from a scanned PDF."""

    raw_bytes = _coerce_attachment_bytes(data)
    if not raw_bytes:
        return [], False

    try:
        from io import BytesIO

        from pypdf import PdfReader

        reader = PdfReader(BytesIO(raw_bytes))
        stem = os.path.splitext(filename or "document")[0] or "document"
        images: list[dict[str, str]] = []
        truncated = False

        for page_num, page in enumerate(reader.pages, start=1):
            if len(images) >= _PDF_PAGE_IMAGE_LIMIT:
                truncated = True
                break

            try:
                page_images = list(page.images)
            except Exception as exc:
                log_debug(
                    f"[prompt_engine] Failed to inspect PDF page images for {filename or 'attachment'} page {page_num}: {exc}"
                )
                continue

            if not page_images:
                continue

            # Prefer the largest image on the page; scanned PDFs typically have
            # one dominant full-page raster image.
            page_image = max(
                page_images,
                key=lambda candidate: len(getattr(candidate, "data", b"") or b""),
            )
            image_bytes = getattr(page_image, "data", b"") or b""
            if not isinstance(image_bytes, bytes) or not image_bytes:
                continue

            image_name = str(getattr(page_image, "name", "") or "")
            image_mime = _guess_binary_mime_type(image_name, image_bytes)
            if not image_mime.startswith("image/"):
                continue

            ext = mimetypes.guess_extension(image_mime) or ".bin"
            images.append(
                {
                    "mime_type": image_mime,
                    "data": base64.b64encode(image_bytes).decode("ascii"),
                    "filename": f"{stem}_page_{page_num}{ext}",
                }
            )

        return images, truncated
    except Exception as exc:
        log_warning(
            f"[prompt_engine] Failed to extract PDF page images from {filename or 'attachment'}: {exc}"
        )
        return [], False


def _coerce_attachment_bytes(data: Any) -> bytes | None:
    """Best-effort decode for attachment payloads stored as raw bytes or base64."""

    if isinstance(data, bytes):
        return data
    if isinstance(data, bytearray):
        return bytes(data)
    if not isinstance(data, str) or not data:
        return None

    try:
        return base64.b64decode(data, validate=True)
    except Exception:
        return data.encode("utf-8", errors="replace")


def _guess_binary_mime_type(filename: str | None, data: bytes) -> str:
    """Infer a MIME type from filename and common binary signatures."""

    guessed, _ = mimetypes.guess_type(filename or "")
    if guessed:
        return guessed

    signatures: tuple[tuple[bytes, str], ...] = (
        (b"\x89PNG\r\n\x1a\n", "image/png"),
        (b"\xff\xd8\xff", "image/jpeg"),
        (b"GIF87a", "image/gif"),
        (b"GIF89a", "image/gif"),
        (b"BM", "image/bmp"),
        (b"II*\x00", "image/tiff"),
        (b"MM\x00*", "image/tiff"),
        (b"RIFF", "image/webp"),
    )
    for prefix, mime_type in signatures:
        if data.startswith(prefix):
            if mime_type == "image/webp" and len(data) >= 12 and data[8:12] != b"WEBP":
                continue
            return mime_type
    return "application/octet-stream"


def _truncate_attachment_text(text: str) -> tuple[str | None, bool]:
    """Trim extracted attachment text to a prompt-safe bound."""

    cleaned = text.strip()
    if not cleaned:
        return None, False
    if len(cleaned) <= _ATTACHMENT_TEXT_CHAR_LIMIT:
        return cleaned, False
    return cleaned[:_ATTACHMENT_TEXT_CHAR_LIMIT].rstrip() + "\n[... truncated]", True


def _assemble_prompt_request(  # noqa: PLR0913
    prompt_dict: dict[str, Any],
    context_section: dict[str, Any],
    text: str,
    interface_name: str | None,
    interface_path: str | None,
    message: Any,
    is_grillo_internal: bool,
    beat_type: str,
    is_voice_input: bool,
    resolved_language: str | None,
    resolved_message_tone: str | None,
    image_data: dict[str, Any] | None,
    attachments: list[Any] | None,
    allowed_action_types: set[str] | None,
) -> Any:  # -> PromptRequest
    """Build a ``PromptRequest`` from the fully-assembled prompt data.

    Called at the end of ``build_prompt_request()`` so engines can opt-in to the
    new typed representation without changing existing behaviour.

    All parameters are extracted from the local scope of ``build_prompt_request()``.
    None of the heavy async work is repeated here.
    """
    from core.prompt_request import Attachment, PromptRequest, RuntimeContext, Turn  # noqa: F401

    # ── System instruction ──────────────────────────────────────────────────
    # Prefer verbose (persona + rules); fall back to minified instructions.
    system_instruction: str = (
        prompt_dict.get("instructions_verbose") or prompt_dict.get("instructions") or ""
    )

    # Keep stable emotion taxonomy/instructions in the system block, not context_summary.
    available_emotions: Any = context_section.get("available_emotions")
    if available_emotions:
        if isinstance(available_emotions, list):
            _emotion_types = ", ".join(str(e) for e in available_emotions)
        else:
            _emotion_types = str(available_emotions)
        if _emotion_types.strip():
            system_instruction = (
                f"{system_instruction}\n\n"
                "AVAILABLE EMOTION TYPES: "
                f"{_emotion_types}. "
                "Adjust emotional state via structured actions only "
                "(prefer update_emotion_state with an emotions map)."
            )

    # ── Context summary ─────────────────────────────────────────────────────
    context_summary: str = _build_context_summary(
        context_section,
        is_grillo_internal=is_grillo_internal,
        include_explicit_runtime_facts=(
            is_grillo_internal or _turn_requests_explicit_runtime_facts(text)
        ),
    )

    # ── Conversation history ─────────────────────────────────────────────────
    # Grillo internal beats have no ongoing conversation history.
    if is_grillo_internal:
        conversation_history: list[Turn] = []
    else:
        try:
            synth_name: str = str(
                config_registry.get_value("SYNTH_NAME", "SyntH") or "SyntH"
            )
            aliases_raw: str = str(config_registry.get_value("SYNTH_ALIASES", "") or "")
            synth_names: set[str] = {synth_name.lower()}
            for alias in aliases_raw.split(","):
                a = alias.strip()
                if a:
                    synth_names.add(a.lower())
        except Exception:
            synth_names = {"synth"}

        history_lines: list[Any] = context_section.get("history_current_chat") or []
        conversation_history = _history_to_turns(history_lines, synth_names)

    # ── Runtime context ─────────────────────────────────────────────────────
    try:
        msg_timestamp: str | None = None
        msg_date = getattr(message, "date", None)
        if msg_date:
            msg_timestamp = msg_date.isoformat()
    except Exception:
        msg_timestamp = None

    # Override with local date+time from time_plugin injections (authoritative local time).
    _ctx_date = str(context_section.get("date") or "").strip()
    _ctx_time = str(context_section.get("time") or "").strip()
    _ctx_time_of_day = str(context_section.get("time_of_day") or "").strip()
    if _ctx_date or _ctx_time:
        msg_timestamp = " ".join(p for p in [_ctx_date, _ctx_time] if p)

    from_user = getattr(message, "from_user", None)
    username: str | None = get_user_display_name(from_user) if from_user else None
    usertag: str | None = get_user_usertag(from_user) if from_user else None
    message_id: int | str | None = getattr(message, "message_id", None)
    try:
        runtime_message_id = int(message_id) if message_id is not None else None
    except (TypeError, ValueError):
        runtime_message_id = None

    voice_channel_id_val = context_section.get("voice_channel_id")
    voice_channel_id_str: str | None = (
        str(voice_channel_id_val) if voice_channel_id_val else None
    )

    emotions_nl: str | None = context_section.get("current_emotions_nl") or None

    # Effective scope: use context_section's recorded scope or default to "local"
    scope: str = str(context_section.get("history_scope") or "local")

    chat_type: str | None = None
    if interface_path:
        try:
            from core.history_engine import telegram_chat_kind

            chat_type = telegram_chat_kind(interface_path)
        except Exception:
            chat_type = None

    runtime_ctx = RuntimeContext(
        interface_name=interface_name,
        interface_path=interface_path,
        chat_type=chat_type,
        message_id=runtime_message_id,
        username=username,
        usertag=usertag,
        timestamp=msg_timestamp,
        time_of_day=_ctx_time_of_day or None,
        input_source="voice" if is_voice_input else "text",
        emotions=emotions_nl,
        scope=scope,
        language=resolved_language,
        tone=resolved_message_tone,
        voice_channel_id=voice_channel_id_str,
        is_grillo_beat=is_grillo_internal,
        beat_type=beat_type or None,
    )

    # ── Tool declarations ────────────────────────────────────────────────────
    tool_declarations: list[Any] = []
    try:
        from core.live_tool_registry import LiveToolRegistry
        from core.core_initializer import core_initializer

        raw_actions: dict[str, Any] = dict(
            core_initializer.actions_block.get("available_actions", {}) or {}
        )
        if allowed_action_types is not None:
            raw_actions = {
                k: v for k, v in raw_actions.items() if k in allowed_action_types
            }
        tool_declarations = LiveToolRegistry.build_manifests_from_actions(raw_actions)
    except Exception as _td_exc:
        log_debug(f"[json_prompt] tool_declarations build skipped: {_td_exc}")

    # ── Reply context ────────────────────────────────────────────────────────
    reply_to_dict: dict[str, Any] | None = None
    try:
        rply = prompt_dict.get("input", {}).get("payload", {}).get("reply_message_id")
        if isinstance(rply, dict):
            reply_to_dict = rply
    except Exception:
        pass

    # ── Attachments ─────────────────────────────────────────────────────────
    pr_attachments = _build_pr_attachments(image_data, attachments)

    # ── Determine mode ───────────────────────────────────────────────────────
    mode: str = "grillo" if is_grillo_internal else "chat"

    return PromptRequest(
        system_instruction=system_instruction,
        tool_declarations=tool_declarations,
        context_summary=context_summary,
        conversation_history=conversation_history,
        current_text=text,
        runtime_ctx=runtime_ctx,
        attachments=pr_attachments,
        reply_to=reply_to_dict,
        supports_tool_calling=False,  # engines set this when they opt-in
        mode=mode,
    )


def _apply_lite_context_stripping(prompt: dict) -> dict:
    """Strip redundant prompt sections for lite mode.

    Called by the minification pipeline when PROMPT_LITE_MODE is enabled.
    Removes verbose instructions, redundant context, and compacts emotions.
    Action minification is handled by ``minify_actions_block(lite=True)``.
    """
    # Remove redundant top-level keys
    prompt.pop("instructions_verbose", None)
    prompt.pop("__pre_reduction_size", None)

    # Compact context
    ctx = prompt.get("context", {})
    ctx.pop("recon", None)
    ctx.pop("recon_instructions", None)
    ctx.pop("tags_placeholder", None)
    ctx.pop("participants", None)

    # Compact emotions — keep current_emotions_nl, drop verbose instruction + list
    ctx.pop("emotion_state", None)
    ctx.pop("available_emotions", None)

    return prompt


async def build_prompt_request(
    message,
    context_memory,
    interface_name: str | None = None,
    image_data: dict | None = None,
    attachments: list[dict] | None = None,
    max_chars: int | None = None,
    history_scope: str | None = None,
) -> dict:
    """Build the prompt payload expected by plugins.

    Parameters
    ----------
    message : AbstractMessage or compatible interface message
        Incoming message object from an interface.
    context_memory : dict[str, deque]
        Dictionary storing last messages per interface_path.
    interface_name : str | None
        Identifier of the interface that delivered the message.
    image_data : dict | None
        Processed image data from image_processor, if present.
    max_chars : int | None
        Maximum characters for the JSON prompt. If provided, the prompt will be
        intelligently reduced by removing oldest memories. If None, no reduction is done.
    history_scope : str | None
        Optional per-prompt override for history selection. One of: 'local', 'recent', 'unified'.
        If None, falls back to any `history_scope` in `context_memory` or to the global `UNIFIED_HISTORY` setting.
    """
    import time

    start_time = time.time()
    log_info(f"[json_prompt] ⏱️ BUILD PROMPT START for interface={interface_name}")

    interface_path = getattr(message, "interface_path", None)
    text = getattr(message, "text", "") or ""
    allowed_action_types_for_prompt: set[str] | None = None

    if isinstance(context_memory, dict):
        scoped_actions = context_memory.get(
            "allowed_action_types"
        ) or context_memory.get("allowed_actions")
        if isinstance(scoped_actions, (list, set, tuple)):
            allowed_action_types_for_prompt = {str(a) for a in scoped_actions if a}

    # Determine if context_memory is a chat history map or a context dict
    # Context dicts have keys like 'interface_path', 'system_message', etc.
    # Chat history maps have interface_path as keys

    # History-like context is now produced by HistoryEngine (plugin-centric aggregation)

    # === 2. Tags and memory lookup ===
    # extract_tags returns salient content tokens (language-agnostic). These are
    # matched against row *content* (keywords), NOT against the JSON tag columns:
    # the auto-generated tag arrays rarely contain the raw message tokens, so
    # passing them as tags would silently return nothing. See extract_tags docs
    # and the two-tier fallback in search_memories.
    tags = extract_tags(text)
    expanded_tags = expand_tags(tags)
    memories = []
    if expanded_tags:
        # Limit follows unified verbosity (HistoryEngine will also apply a hard cap)
        try:
            from core.history_engine import _get_int as _history_get_int

            mem_limit = int(_history_get_int("CONTEXT_VERBOSITY", 10))
        except Exception:
            mem_limit = 10
        try:
            from core.synth_core_memory import search_memories

            memories = await search_memories(
                keywords=expanded_tags, limit=max(1, mem_limit), include_chat=True
            )
        except Exception as e:
            log_warning(f"[json_prompt] search_memories failed: {e}")
            memories = []
        log_debug(
            f"[json_prompt] ⏱️ Loaded {len(memories)} memories from keywords in {time.time() - start_time:.2f}s"
        )
    # === Recon (prompt 0) contributions ===
    recon_contributions: list[dict] = []
    recon_instructions: list[str] = []
    recon_snippets: list[dict] = []
    recon_memories: list[dict] = []
    resolved_language = None
    resolved_message_tone = None
    resolved_conversation_tone = None

    _is_grillo_beat = bool(
        getattr(message, "grillo_beat", False)
        or (isinstance(context_memory, dict) and context_memory.get("grillo_beat"))
        or (interface_path and str(interface_path).startswith("grillo"))
    )
    # Outbound beats (observer) target an external interface (e.g. telegram_bot)
    # — they need recon (memory search) and should NOT be treated as internal.
    _beat_type = (
        (isinstance(context_memory, dict) and context_memory.get("beat_type"))
        or getattr(message, "beat_type", None)
        or ""
    )
    is_grillo_internal = _is_grillo_beat and not is_outbound_beat(_beat_type)

    try:
        from core.recon import (
            gather_recon_contributions,
            resolve_language,
            resolve_tone,
        )

        if is_grillo_internal:
            # Grillo internal beats have fixed language/tone defaults —
            # skip the LLM recon call to avoid wasting API tokens.
            log_debug("[json_prompt] Skipping recon LLM call for Grillo internal beat")
            recon_contributions = []
        else:
            recon_contributions = await gather_recon_contributions(
                message=message,
                context_memory=context_memory,
                text=text,
                tags=expanded_tags,
                keywords=None,
            )

        for c in recon_contributions:
            ctype = c.get("type")
            if ctype == "memory":
                content = c.get("content")
                if isinstance(content, dict):
                    recon_memories.append(content)
                elif content:
                    recon_memories.append(
                        {
                            "source": c.get("source"),
                            "id": c.get("id"),
                            "timestamp": c.get("timestamp"),
                            "snippet": str(content),
                            "tags": c.get("tags") or [],
                        }
                    )
            elif ctype == "snippet":
                recon_snippets.append(c)
            elif ctype == "instruction":
                if c.get("content"):
                    recon_instructions.append(str(c.get("content")))

        if recon_memories:
            memories = _merge_memory_entries(memories, recon_memories)

        resolved_language = await resolve_language(
            contributions=recon_contributions,
            interface_path=interface_path,
            is_grillo_internal=is_grillo_internal,
            message=message,
        )
        resolved_message_tone, resolved_conversation_tone = await resolve_tone(
            contributions=recon_contributions,
            interface_path=interface_path,
            is_grillo_internal=is_grillo_internal,
            message=message,
        )
    except Exception as e:
        log_warning(f"[json_prompt] Recon gather failed: {e}")

    # === 3. Context base (history + optional plugin contributions) ===
    try:
        from core.history_engine import HistoryEngine

        # Determine effective history_scope (explicit param -> context_memory -> default behavior)
        effective_history_scope = history_scope
        if effective_history_scope is None and isinstance(context_memory, dict):
            effective_history_scope = context_memory.get("history_scope")

        history_engine = HistoryEngine()
        context_section: dict[str, Any] = await history_engine.build_context(
            message=message,
            context_memory=context_memory,
            interface_name=interface_name,
            text=text,
            memories=memories,
            history_scope=effective_history_scope,
        )
    except Exception as e:
        log_warning(
            f"[json_prompt] Failed to build history context via HistoryEngine: {e}"
        )
        context_section = {"memories": memories}

    # history_scope is embedded in the input_payload "scope" field built below.
    # === 3. Recon contributions (prompt 0) ===
    # Note: raw contributions are NOT included — their memories are already
    # merged into the top-level `memories` list.  Only metadata is kept.
    try:
        if recon_contributions:
            context_section["recon"] = {
                "snippets": recon_snippets,
                "language": resolved_language,
                "message_tone": resolved_message_tone,
                "conversation_tone": resolved_conversation_tone,
            }
        if recon_instructions:
            context_section["recon_instructions"] = recon_instructions
    except Exception as e:
        log_warning(f"[json_prompt] Failed to attach recon context: {e}")

    # === 3aa. Channel legend for interface_paths present in the history ===
    # For every distinct interface_path that contributed a "[from ...]" history
    # line, expose its human-readable pretty name so the model can map the
    # source label back to a routable interface_path when it decides to reply.
    try:
        history_paths = context_section.pop("history_interface_paths", None)
        if history_paths:
            from core.interface_paths import build_pretty_name

            legend_lines: list[str] = []
            for hp in history_paths:
                try:
                    pretty = await build_pretty_name(hp)
                    display = pretty.get("display") if pretty else None
                except Exception as legend_err:
                    log_debug(
                        f"[json_prompt] pretty name for {hp} failed: {legend_err}"
                    )
                    display = None
                if display:
                    legend_lines.append(f"{hp} = {display}")
                else:
                    legend_lines.append(str(hp))
            if legend_lines:
                context_section["channel_legend"] = legend_lines
    except Exception as e:
        log_debug(f"[json_prompt] Failed to build channel legend: {e}")

    # === 3a. Static injections from plugins ===
    static_persona = None  # Extract persona separately for instructions
    try:
        from core.action_parser import gather_static_injections

        log_info("[json_prompt] 🔄 About to call gather_static_injections()")
        injections = await gather_static_injections(message, context_memory)
        log_info(
            f"[json_prompt] 📥 gather_static_injections() returned: {list(injections.keys()) if injections else 'empty'}"
        )
        if isinstance(injections, dict):
            # Extract persona BEFORE adding to context - it will go to instructions instead
            if "persona" in injections:
                static_persona = injections.pop("persona")
                log_info(
                    f"[json_prompt] 👤 Extracted persona for instructions ({len(static_persona) if static_persona else 0} chars)"
                )

            soul_recalled_memories = injections.pop("soul_recalled_memories", [])
            if not isinstance(soul_recalled_memories, list):
                soul_recalled_memories = [soul_recalled_memories]

            # Add remaining injections to context (but drop deprecated legacy keys)
            context_section.update(injections)
            # Deprecated (migrated to HistoryEngine)
            for legacy_key in (
                "latest_diary_entries",
                "diary_entries",
                "diary",
                "chat_history",
                "current_chat_history",
            ):
                if legacy_key in context_section:
                    context_section.pop(legacy_key, None)
            if soul_recalled_memories:
                context_section["memories"] = _merge_memory_entries(
                    list(context_section.get("memories") or []),
                    soul_recalled_memories,
                )
            log_info(
                f"[json_prompt] ✅ Updated context_section with injections. Keys now: {list(context_section.keys())}"
            )
    except Exception as e:
        log_warning(f"[json_prompt] Failed to gather static injections: {e}")

    # === 3b. Peer SyntH awareness block (Telegram groups only) ===
    try:
        _chat_type = getattr(getattr(message, "chat", None), "type", None)
        _is_tg_group = interface_name == "telegram_bot" and _chat_type in (
            "group",
            "supergroup",
        )
        if _is_tg_group:
            from core.peer_policy import get_peer_context_block

            peer_block = get_peer_context_block()
            if peer_block:
                recon_instructions.append(peer_block)
                log_debug(
                    "[json_prompt] Peer context block injected for Telegram group"
                )
    except Exception as e:
        log_debug(f"[json_prompt] Peer context block skipped: {e}")

    # === 4. Input payload ===
    # interface_path was already extracted at the beginning
    # If still not found, check if context_memory is actually a context dict with interface_path
    if (
        not interface_path
        and isinstance(context_memory, dict)
        and "interface_path" in context_memory
    ):
        interface_path = context_memory.get("interface_path")
        log_debug(
            f"[json_prompt] Retrieved interface_path from context dict: {interface_path}"
        )

    local_time_fields: dict[str, Any] = {}
    try:
        include_local_time = bool(
            config_registry.get_value(
                "INCLUDE_LOCAL_TIME_IN_PROMPTS", True, value_type=bool
            )
        )
    except Exception:
        include_local_time = True

    if include_local_time:
        try:
            from core.time_zone_utils import get_local_time_fields

            local_time_fields = await get_local_time_fields(
                getattr(message, "date", None), interface_path=interface_path
            )
        except Exception as e:
            log_debug(f"[json_prompt] Failed to compute local time fields: {e}")
            local_time_fields = {}

    if local_time_fields:
        context_section.setdefault("date", local_time_fields.get("local_date"))
        context_section.setdefault("time", local_time_fields.get("local_time"))
        context_section.setdefault("time_of_day", local_time_fields.get("time_of_day"))
        context_section.setdefault("season", local_time_fields.get("season"))
        context_section.setdefault("day_of_week", local_time_fields.get("day_of_week"))

    for key, kind in (
        ("history_recent", "history_recent"),
        ("thoughts", "thoughts"),
        ("memories", "memories"),
    ):
        raw_entries = context_section.get(key)
        if isinstance(raw_entries, list):
            context_section[key] = _sanitize_context_entries(raw_entries, kind=kind)

    # Determine message input source for the LLM ("voice" | "text").
    # Only mark as voice for the *current* message; never stored in chat_history,
    # so the model cannot mistakenly infer that past messages were also voice.
    _is_voice_input: bool = bool(
        isinstance(context_memory, dict) and context_memory.get("is_voice_input")
    )

    _source_dict: dict = {
        "interface_path": interface_path,
        "message_id": message.message_id,
        "username": get_user_display_name(getattr(message, "from_user", None)),
        "usertag": get_user_usertag(getattr(message, "from_user", None)),
        "interface": interface_name,
    }
    # If the sender is currently in a Discord voice channel, tell the model —
    # this is what allows it to decide to issue join_voice_discord.
    _voice_channel_id = isinstance(context_memory, dict) and context_memory.get(
        "voice_channel_id"
    )
    if _voice_channel_id:
        _source_dict["author_voice_channel_id"] = str(_voice_channel_id)

    input_payload = {
        "text": text,
        "input_source": "voice" if _is_voice_input else "text",
        "source": _source_dict,
        "timestamp": message.date.isoformat(),
        "privacy": "default",
        # Explicit anchor for reply routing. THIS is the chat the incoming
        # message arrived in — the model MUST target its reply here by default.
        # Any other conversation in the context block is background context only
        # and must NOT be replied to unless the user explicitly asks to message
        # someone/somewhere else. Weak engines lose this anchor when unified
        # history blends multiple chats, so we state it structurally, not just
        # in prose instructions.
        "current_chat": {
            "interface_path": interface_path,
            "interface": interface_name,
            "thread_id": getattr(message, "thread_id", None)
            or getattr(message, "message_thread_id", None),
        },
        # Set `scope` to the effective history_scope when provided, otherwise keep legacy default
        "scope": (
            effective_history_scope
            if ("effective_history_scope" in locals() and effective_history_scope)
            else "local"
        ),
    }

    # Expose chosen history_scope to downstream plugins/engines explicitly.
    if effective_history_scope:
        input_payload.setdefault("history_scope", effective_history_scope)

    if local_time_fields:
        input_payload.update(local_time_fields)
    # debug: log full prompt payload for reconstruction
    try:
        full_text = json_dumps(redact_multimodal_for_logging(input_payload))
        log_debug(
            f"[json_prompt] ⏹️ Final prompt built ({len(full_text)} chars): {full_text}"
        )
    except Exception as e:
        log_debug(f"[json_prompt] Failed to dump final prompt for logging: {e}")

    # Fallback to image_data and attachments from context_memory when not provided explicitly.
    if not image_data and isinstance(context_memory, dict):
        image_data = context_memory.get("image_data")
    if not attachments and isinstance(context_memory, dict):
        attachments = context_memory.get("attachments")

    # Add image data if present
    if image_data:
        input_payload["image"] = image_data
        log_debug(
            f"[json_prompt] Including image data in prompt: {image_data.get('type', 'unknown')}"
        )

    # Add multimodal attachments if present
    if attachments:
        input_payload["attachments"] = attachments
        log_debug(
            f"[json_prompt] Including {len(attachments)} multimodal attachments in prompt"
        )

        # Synthesise a structured "video" metadata block (mirrors the "image" block)
        # so that the model gets the same level of context for video as for images.
        for att in attachments:
            media_meta = att.get("media_metadata")
            if not media_meta:
                continue
            if media_meta.get("type") not in ("video", "video_note"):
                continue
            input_payload["video"] = {
                "type": media_meta["type"],
                "source": {
                    "interface": interface_name,
                    "user_id": getattr(getattr(message, "from_user", None), "id", None),
                    "chat_id": getattr(message, "chat", None)
                    and getattr(message.chat, "id", None),
                    "message_id": getattr(message, "message_id", None),
                },
                "video_data": {
                    "type": media_meta["type"],
                    "filename": att.get("filename", ""),
                    "mime_type": att.get("mime_type", "video/mp4"),
                    "duration": media_meta.get("duration", 0),
                    "width": media_meta.get("width", 0),
                    "height": media_meta.get("height", 0),
                    "file_size": media_meta.get("file_size", 0),
                    "has_audio": media_meta.get("has_audio", False),
                    "caption": att.get("caption", ""),
                },
                "metadata": {
                    "timestamp": getattr(message, "date", None)
                    and message.date.isoformat(),
                    "caption": att.get("caption", ""),
                    "mime_type": att.get("mime_type", "video/mp4"),
                    "file_size": media_meta.get("file_size", 0),
                    "duration": media_meta.get("duration", 0),
                },
            }
            log_debug(
                f"[json_prompt] Including video metadata in prompt: "
                f"{media_meta['type']}, {media_meta.get('duration', 0)}s"
            )
            break  # Only attach metadata for the first video

    reply = getattr(message, "reply_to_message", None)
    if reply:
        reply_text = getattr(reply, "text", None) or getattr(reply, "caption", None)
        if not reply_text:
            reply_text = "[Non-text content]"
        reply_date = getattr(reply, "date", None)
        reply_timestamp = reply_date.isoformat() if reply_date else ""
        reply_from = getattr(reply, "from_user", None)
        reply_full_name = get_user_display_name(reply_from) if reply_from else "Unknown"
        reply_username = getattr(reply_from, "username", None) if reply_from else None
        input_payload["reply_message_id"] = {
            "text": reply_text,
            "timestamp": reply_timestamp,
            "from": {
                "username": reply_full_name,
                "usertag": f"@{reply_username}" if reply_username else "(no tag)",
            },
        }

    input_section = {
        "type": "message",
        "interface": interface_name,
        "payload": input_payload,
    }

    # Debug output for both sections
    log_debug(
        "[json_prompt] context = "
        + json_dumps(redact_multimodal_for_logging(context_section))
    )
    log_debug(
        "[json_prompt] input = "
        + json_dumps(redact_multimodal_for_logging(input_section))
    )

    # Add JSON instructions to the prompt
    json_instructions = load_json_instructions()

    # === CRITICAL: Prepend persona to instructions so ALL LLM types see it ===
    # Use the persona extracted during gather_static_injections()
    # Skip prepending static persona for internal system/maintenance tasks (like diary_merge/diary_consolidation)
    # to avoid triggering safety filters of external LLMs on explicit instructions.
    _use_persona = bool(
        config_registry.get_value("USE_PERSONA_IN_SYSTEM_PROMPTS", True)
    )
    if static_persona and _use_persona:
        json_instructions = f"=== CRITICAL SYSTEM IDENTITY ===\n{static_persona}\n\n=== JSON RESPONSE INSTRUCTIONS ===\n{json_instructions}"
        log_info(
            f"[json_prompt] 👤 Persona prepended to instructions ({len(static_persona)} chars)"
        )
    elif static_persona:
        log_info(
            f"[json_prompt] 👤 Persona skipped prepending for internal system task (interface: {interface_name}, beat_type: {_beat_type})"
        )

    # Recon-derived instructions (language, tone, plugin hints)
    try:
        recon_prefixes: list[str] = []
        if resolved_language:
            recon_prefixes.append(
                f"Use {resolved_language} language for the assistant replies."
            )
        if resolved_message_tone:
            recon_prefixes.append(f"Use a {resolved_message_tone} tone for replies.")
        if resolved_conversation_tone:
            recon_prefixes.append(
                f"Tone of the conversation is: {resolved_conversation_tone}."
            )
        if recon_instructions:
            recon_prefixes.extend([str(r) for r in recon_instructions if r])

        # Surface recon snippets (e.g. live radio status) directly in the
        # instructions. They are also carried inside context.recon.snippets,
        # but models frequently ignore that nested field; stating the live
        # data explicitly makes it usable in the reply.
        recon_snippet_texts = [
            str(s.get("content")).strip()
            for s in recon_snippets
            if isinstance(s, dict) and s.get("content")
        ]
        if recon_snippet_texts:
            recon_prefixes.append(
                "Live contextual data (already gathered for you, treat as current fact): "
                + " | ".join(recon_snippet_texts)
            )

        if recon_prefixes:
            json_instructions = " ".join(recon_prefixes) + " " + json_instructions
    except Exception as e:
        log_warning(f"[json_prompt] Failed to add recon instructions: {e}")

    # Grillo internal beats are non-user-facing. Without an explicit guardrail,
    # some models invent unsupported message actions (e.g. message_grillo),
    # which triggers correction retries and stalls beat throughput.
    if is_grillo_internal:
        allowed_list = []
        if isinstance(allowed_action_types_for_prompt, set):
            allowed_list = sorted(str(a) for a in allowed_action_types_for_prompt if a)

        grillo_guard = (
            "GRILLO INTERNAL MODE: This is an internal autonomous beat, not a user chat. "
            "DO NOT emit any message_* action and DO NOT emit send_message. "
            "Prefer create_personal_diary_entry for reflective output."
        )
        if allowed_list:
            grillo_guard += (
                f" Allowed actions for this beat: {', '.join(allowed_list)}."
            )

        json_instructions = f"{grillo_guard} {json_instructions}"

    # Keep `instructions` strictly minified (single-line) for token efficiency and tests.
    try:
        json_instructions = " ".join((json_instructions or "").split())
    except Exception:
        pass

    # Interface-specific instructions are provided via the available actions block
    # No hardcoded interface references - plugins define their own instructions

    prompt_with_instructions: dict[str, Any] = {
        "context": context_section,
        "input": input_section,
        "instructions": json_instructions,
    }

    # Record full prompt size BEFORE injecting actions/minification so callers
    # can decide split based on the original size.
    try:
        pre_reduction_size = len(json_dumps(prompt_with_instructions))
        prompt_with_instructions["__pre_reduction_size"] = pre_reduction_size
        log_debug(f"[json_prompt] __pre_reduction_size={pre_reduction_size}")
    except Exception:
        prompt_with_instructions["__pre_reduction_size"] = None

    # Resolve lite mode flag early so both actions and context use the same value
    is_lite = False
    try:
        from core.config_manager import config_registry as _cfg

        is_lite = bool(_cfg.get_value("PROMPT_LITE_MODE", 0, value_type=int))
    except Exception:
        pass

    # Include unified actions metadata from the initializer
    # Use minified version to keep prompt size manageable
    # When lite mode is on, minify_actions_block handles the aggressive filtering too
    try:
        from core.core_initializer import core_initializer

        full_actions = core_initializer.actions_block.get("available_actions", {})

        # When audio attachments are present as multimodal content, remove
        # stt_transcribe from the available actions so the LLM processes the
        # audio directly instead of requesting a redundant transcription step.
        has_audio_attachment = attachments and any(
            (a.get("mime_type") or "").startswith("audio/") for a in attachments
        )
        if has_audio_attachment and "stt_transcribe" in full_actions:
            full_actions = {
                k: v for k, v in full_actions.items() if k != "stt_transcribe"
            }
            log_debug(
                "[json_prompt] Removed stt_transcribe from actions "
                "(audio sent as multimodal content)"
            )

        if allowed_action_types_for_prompt is None:
            derived_action_types = _derive_default_prompt_action_types(
                full_actions,
                interface_name,
            )
            if derived_action_types and len(derived_action_types) < len(full_actions):
                allowed_action_types_for_prompt = derived_action_types
                log_debug(
                    "[json_prompt] Derived default prompt action scope: "
                    f"{len(derived_action_types)}/{len(full_actions)} actions kept "
                    f"for interface={interface_name}"
                )

        if allowed_action_types_for_prompt is not None:
            full_actions = {
                k: v
                for k, v in full_actions.items()
                if k in allowed_action_types_for_prompt
            }
            log_debug(
                "[json_prompt] Filtered actions block to scoped allowlist: "
                f"{sorted(allowed_action_types_for_prompt)}"
            )

        # Minify to reduce token usage (lite=True also filters + strips to brief-only)
        prompt_with_instructions["actions"] = minify_actions_block(
            full_actions, lite=is_lite
        )
        log_debug(
            f"[json_prompt] Actions block minified: {len(json_dumps(full_actions))} -> {len(json_dumps(prompt_with_instructions['actions']))} chars (lite={is_lite})"
        )
    except Exception as e:
        log_warning(f"[prompt_engine] Failed to inject actions block: {e}")
        prompt_with_instructions["actions"] = {}

    # === Apply lite mode context stripping if enabled ===
    if is_lite:
        try:
            pre_lite = len(json_dumps(prompt_with_instructions))
            prompt_with_instructions = _apply_lite_context_stripping(
                prompt_with_instructions
            )
            post_lite = len(json_dumps(prompt_with_instructions))
            log_info(
                f"[json_prompt] Lite mode applied: {pre_lite} -> {post_lite} chars"
            )
        except Exception as e:
            log_warning(f"[json_prompt] Failed to apply lite mode: {e}")

    # === Final check: Reduce prompt if it exceeds LLM character limits ===
    try:
        # Use provided max_chars if available, otherwise get from active LLM engine
        max_prompt_chars = max_chars

        # If max_chars was not provided, try to get from active LLM engine
        if max_chars is None:
            try:
                # Local imports to avoid module-level cycles
                from core.config import get_active_cortex_engine
                from core.cortex_registry import get_cortex_registry

                active_cortex = await get_active_cortex_engine()
                registry = get_cortex_registry()
                engine = registry.get_engine(active_cortex)

                if not engine:
                    engine = registry.load_engine(active_cortex)

                if engine and hasattr(engine, "get_interface_limits"):
                    limits = engine.get_interface_limits()
                    max_prompt_chars = limits.get("max_prompt_chars")
            except Exception as e:
                log_debug(
                    f"[json_prompt] Could not get interface limits for reduction: {e}"
                )

        # Apply reduction only if max_chars is available
        if max_prompt_chars:
            prompt_with_instructions = reduce_prompt_for_llm_limit(
                prompt_with_instructions, max_prompt_chars
            )

    except Exception as e:
        log_warning(f"[json_prompt] Failed to apply prompt reduction: {e}")

    elapsed = time.time() - start_time
    log_info(
        f"[json_prompt] ⏱️ BUILD PROMPT COMPLETE in {elapsed:.2f}s, final size: {len(json_dumps(prompt_with_instructions)) if isinstance(prompt_with_instructions, dict) else len(str(prompt_with_instructions))} chars"
    )

    # === Build PromptRequest (new typed intermediate representation — Phase 1) ===
    # Engines ignore __prompt_request in Phase 1; they opt-in by reading it when ready.
    # This always succeeds or silently skips — zero risk to existing behaviour.
    try:
        prompt_with_instructions["__prompt_request"] = _assemble_prompt_request(
            prompt_dict=prompt_with_instructions,
            context_section=context_section,
            text=text,
            interface_name=interface_name,
            interface_path=interface_path,
            message=message,
            is_grillo_internal=is_grillo_internal,
            beat_type=str(_beat_type or ""),
            is_voice_input=_is_voice_input,
            resolved_language=resolved_language,
            resolved_message_tone=resolved_message_tone,
            image_data=image_data,
            attachments=attachments,
            allowed_action_types=allowed_action_types_for_prompt,
        )
        log_debug("[json_prompt] PromptRequest assembled and attached")
    except Exception as _pr_exc:
        log_debug(f"[json_prompt] PromptRequest assembly skipped: {_pr_exc}")

    return prompt_with_instructions


async def build_json_prompt(
    message,
    context_memory,
    interface_name: str | None = None,
    image_data: dict | None = None,
    attachments: list[dict] | None = None,
    max_chars: int | None = None,
    history_scope: str | None = None,
) -> dict:
    """Deprecated alias for ``build_prompt_request``.

    Kept for backward compatibility while callers migrate to the new symbol.
    """
    global _LEGACY_BUILD_JSON_PROMPT_WARNED
    if not _LEGACY_BUILD_JSON_PROMPT_WARNED:
        log_debug(
            "[prompt_engine] build_json_prompt is deprecated; use build_prompt_request"
        )
        _LEGACY_BUILD_JSON_PROMPT_WARNED = True
    return await build_prompt_request(
        message=message,
        context_memory=context_memory,
        interface_name=interface_name,
        image_data=image_data,
        attachments=attachments,
        max_chars=max_chars,
        history_scope=history_scope,
    )


async def search_memories(tags=None, scope=None, limit=5):
    if not tags:
        return []

    is_postgres = _get_db_type() == "postgres"

    if is_postgres:
        conditions = " OR ".join(
            ["COALESCE(NULLIF(BTRIM(tags), ''), '[]')::jsonb ? %s"] * len(tags)
        )
    else:
        # Build OR conditions using JSON_CONTAINS to check if any tag exists in the JSON array
        conditions = " OR ".join(["JSON_CONTAINS(tags, %s)"] * len(tags))

    query = f"""
        SELECT content, timestamp
        FROM memories
        WHERE ({conditions})
    """

    if not is_postgres:
        query = query.replace("WHERE", "WHERE json_valid(tags) AND", 1)

    # MariaDB expects JSON-encoded strings for JSON_CONTAINS; Postgres uses raw text with jsonb '?'.
    params = [tag if is_postgres else json_dumps(tag) for tag in tags]

    if scope:
        query += " AND scope = %s"
        params.append(scope)

    query += " ORDER BY timestamp DESC LIMIT %s"
    params.append(limit)

    log_debug("Query:")
    log_debug(query)
    log_debug(f"Parameters: {params}")

    async with get_conn_ctx() as conn:
        try:
            async with conn.cursor() as cur:
                await cur.execute(query, params)
                rows = await cur.fetchall()
                # Truncate each memory to max 400 chars to keep JSON payload lightweight
                memories = []
                seen_memories: set[str] = set()
                for row in rows:
                    mem = row[0]
                    if not isinstance(mem, str):
                        mem = str(mem)
                    if mem in seen_memories:
                        continue
                    seen_memories.add(mem)
                    if isinstance(mem, str) and len(mem) > 400:
                        mem = mem[:400] + "..."
                    memories.append(mem)

                # Also search ai_diary for context_tags to include diary entries in memories
                try:
                    diary_conditions = conditions.replace("tags", "context_tags")
                    diary_query = (
                        "SELECT content, timestamp FROM ai_diary "
                        f"WHERE ({diary_conditions}) ORDER BY timestamp DESC LIMIT %s"
                    )
                    if not is_postgres:
                        diary_query = diary_query.replace(
                            "WHERE",
                            "WHERE json_valid(context_tags) AND",
                            1,
                        )
                    diary_params = [
                        tag if is_postgres else json_dumps(tag) for tag in tags
                    ]
                    diary_params.append(limit)
                    await cur.execute(diary_query, diary_params)
                    rows2 = await cur.fetchall()
                    for r in rows2:
                        mem = r[0]
                        if not isinstance(mem, str):
                            mem = str(mem)
                        if mem in seen_memories:
                            continue
                        seen_memories.add(mem)
                        if isinstance(mem, str) and len(mem) > 400:
                            mem = mem[:400] + "..."
                        memories.append(mem)
                except Exception:
                    # If ai_diary search fails, ignore and continue with memories only
                    pass

                log_debug(
                    f"[search_memories] Retrieved {len(memories)} memories, ~{sum(len(str(m)) for m in memories)} chars total"
                )
                return memories
        except Exception as e:
            log_error(f"Query failed: {repr(e)}")
            return []


async def free_memory_search(query: str, limit: int = 5):
    """Perform a free-text memory search over `memories` and `ai_diary` tables and
    return a list of snippet strings (max 400 chars each). This mirrors the plugin's
    mode='free' behavior but does not request LLM delivery, it just returns results.
    """
    if not query or not isinstance(query, str) or not query.strip():
        return []

    tokens = [q.strip() for q in query.split() if q.strip()]
    if not tokens:
        return []

    params = []
    token_clauses = []
    for tok in tokens:
        like = "%" + tok + "%"
        token_clauses.append("content LIKE %s")
        params.append(like)

    where_mem = "(" + " OR ".join(token_clauses) + ")"

    diary_token_clauses = []
    for tok in tokens:
        like = "%" + tok + "%"
        diary_token_clauses.append("content LIKE %s")
        params.append(like)
        diary_token_clauses.append("personal_thought LIKE %s")
        params.append(like)
        diary_token_clauses.append("interaction_summary LIKE %s")
        params.append(like)
        diary_token_clauses.append("user_message LIKE %s")
        params.append(like)

    where_diary = "(" + " OR ".join(diary_token_clauses) + ")"

    queries = []
    queries.append(
        f"SELECT 'memories' AS source, id, timestamp, content FROM memories WHERE {where_mem}"
    )
    queries.append(
        f"SELECT 'ai_diary' AS source, id, timestamp, content FROM ai_diary WHERE {where_diary}"
    )

    # Fetch a larger pool if configured (useful when randomizing results)
    try:
        pool_max = int(
            config_registry.get_value(
                "MEMORY_SEARCH_PREFLIGHT_POOL_MAX", 100, value_type=int
            )
            or 100
        )
    except Exception:
        pool_max = 100

    union_q = " UNION ALL ".join(queries) + " ORDER BY timestamp DESC LIMIT %s"
    params.append(pool_max)

    log_debug(f"[free_memory_search] Executing query: {union_q} params={params}")

    results = []
    # Provide more helpful debug: print the DB target being used (if available)
    read_db_config: Any = None
    try:
        from core.db import _read_db_config as _db_config_reader

        read_db_config = _db_config_reader
    except Exception:
        read_db_config = None

    if read_db_config:
        try:
            db_host, db_port, db_user, db_pass, db_name = read_db_config()
            log_debug(
                f"[free_memory_search] DB target: {db_user}@{db_host}:{db_port}/{db_name}"
            )
        except Exception:
            pass

    # Try acquiring a connection and executing the query with retries up to 2 attempts
    rows = []
    max_attempts = 2
    start_time = time_module.time()
    for attempt in range(1, max_attempts + 1):
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    # Enforce a 10s timeout per attempt
                    await asyncio.wait_for(cur.execute(union_q, params), timeout=10.0)
                    rows = await asyncio.wait_for(cur.fetchall(), timeout=5.0)
            break
        except asyncio.TimeoutError:
            log_warning(
                f"[free_memory_search] DB attempt {attempt} timed out after 10s"
            )
            if attempt < max_attempts:
                continue
            else:
                log_error(
                    f"[free_memory_search] Query timed out after {max_attempts} attempts"
                )
                return []
        except Exception as e:
            log_warning(f"[free_memory_search] DB attempt {attempt} failed: {e}")
            if attempt < max_attempts:
                await asyncio.sleep(0.5)
                continue
            else:
                log_error(
                    f"[free_memory_search] Query failed after {max_attempts} attempts: {e}"
                )
                return []

    log_info(
        f"[free_memory_search] Query completed in {time_module.time() - start_time:.3f}s"
    )

    for r in rows:
        src, _id, ts, content = r
        snippet = content if isinstance(content, str) else str(content)
        if len(snippet) > 400:
            snippet = snippet[:400] + "..."
        results.append(snippet)

    log_debug(
        f"[free_memory_search] Retrieved {len(results)} snippets (pool_max={pool_max})"
    )
    try:
        log_info(
            f"[json_prompt][preflight_summary] strategy=free_db snippets={len(results)} pool_max={pool_max}"
        )
    except Exception:
        pass

    try:
        randomize = bool(
            config_registry.get_value(
                "MEMORY_SEARCH_PREFLIGHT_RANDOMIZE", False, value_type=bool
            )
        )
    except Exception:
        randomize = False

    # If there are more results than the desired limit and randomization is enabled,
    # shuffle and then return the desired number of results. Otherwise, return the
    # top `limit` results by timestamp (already ordered DESC).
    if len(results) > limit and randomize:
        random.shuffle(results)

    return results[:limit]


async def build_prompt(
    user_text: str,
    identity_prompt: str = "",
    extract_tags_fn=extract_tags,
    search_memories_fn=None,
    limit: int = 5,
    log_path: str = "logs/prompt_cycle.log",
) -> list:
    tags = extract_tags_fn(user_text) if extract_tags_fn else []
    expanded_tags = expand_tags(tags) if tags else []
    memories = (
        await search_memories_fn(tags=expanded_tags, limit=limit)
        if search_memories_fn
        else []
    )

    memory_block = (
        "\n".join(f"- {mem}" for mem in memories)
        if memories
        else "No relevant memory found."
    )

    messages = []

    if identity_prompt:
        messages.append({"role": "system", "content": identity_prompt})

    messages.append(
        {"role": "system", "content": f"[MEMORIE RILEVANTI]\n{memory_block}"}
    )

    messages.append({"role": "user", "content": user_text.strip()})

    # === LOGGING SU FILE ===
    try:
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        timestamp = datetime.now(timezone.utc).isoformat()
        with open(log_path, "a", encoding="utf-8") as log_file:
            log_file.write(f"\n[{timestamp}] --- REASONING CYCLE ---\n")
            log_file.write(f"> User text: {user_text.strip()}\n")
            log_file.write(f"> Extracted tags: {tags}\n")
            log_file.write(f"> Expanded tags: {expanded_tags}\n")
            log_file.write(f"> Memories found: {len(memories)}\n")
            for msg in messages:
                role = msg.get("role", "").upper()
                content = msg.get("content", "").strip()
                log_file.write(f"[{role}]\n{content}\n\n")
            log_file.write("----------- END -----------\n")
    except Exception as e:
        log_warning(f"Error logging prompt: {e}")

    return messages


def load_json_instructions() -> str:
    # Compact instructions for LLM prompts (minified to save tokens).
    # Keep this small but authoritative: the LLM must reply using only valid JSON
    # following the exact actions / payload structure.

    # Resolve the trainer name dynamically (config-driven, never hardcoded) so the
    # autonomy rationale is written in-voice and names people instead of writing
    # detached "the user" prose — small local models in particular parrot whatever
    # framing the instructions use.
    try:
        from core.config import get_trainer_display_name

        trainer_name = get_trainer_display_name()
    except Exception:
        trainer_name = ""
    if trainer_name:
        naming_hint = f" Name people, not 'the user' (your trainer: {trainer_name})."
    else:
        naming_hint = " Name people, not 'the user'."

    instructions = (
        "MASTER INSTRUCTION: Use ONLY actions from the 'actions' block. Never fabricate.\n"
        "If an action you need is not available, reply with JSON explaining why.\n"
        f"AUTONOMY GUIDELINES: You MAY proactively propose or execute allowed actions when beneficial. When acting autonomously include a brief `meta` object with `autonomous: true` and a short first-person `rationale` (your own voice) for why you are acting.{naming_hint} If an action is disallowed, return a JSON proposal describing the need.\n"
        "RESPOND ONLY WITH VALID JSON. No text before or after.\n"
        "REPLY ROUTING: input.payload.current_chat.interface_path is the chat the incoming message arrived in — this is WHERE you must reply by default. Any other conversation shown in the context block is background context only; do NOT reply there unless the user explicitly asks to message someone or somewhere else. Always copy input.payload.current_chat.interface_path into the 'interface_path' of your message_* action.\n"
        "Use input.interface and input.payload.source.interface_path to route replies.\n"
        "NEVER use 'target' — always use 'interface_path' in message actions.\n"
        "Include reply_message_id when replying to specific messages. Use thread_id from input.payload.source.thread_id when present (omit if missing).\n"
        "CHAT REPLY REQUIRED: When GRILLO INTERNAL MODE is NOT active (this is a normal human chat turn), you MUST include a message_* action in every response. Diary entries and emotion updates are supplementary bookkeeping — they do NOT substitute for replying. Returning only internal actions (diary, emotions, update_emotion_state) without a message_* action is a hard failure and will trigger a correction.\n"
        "CLARIFICATION POLICY: If the user's intent, referent, or the subject of a follow-up is ambiguous or missing, DO NOT GUESS — ask one concise clarifying question before asserting facts or taking action. When the user asks whether you 'understood' but there is no clear context, request clarification rather than assuming.\n"
        "MEMORY HONESTY: When the user asks what you remember, prefer honesty over confidence. Memories can be incomplete or stale. If you do not clearly recall or cannot verify a detail, say so. Do not invent events, conversations, promises, or feelings to fill gaps. SyntH is not roleplay or fiction, so never turn uncertainty into fiction.\n"
        "REFERENCE CLARITY: When the user refers indirectly to a person, message, post, image, clip, or quoted content, refer to its author or speaker in a clear generic way and avoid vague or impersonal wording that obscures who created or said it.\n"
        "TIME AUTHORITY: Use the [SYSTEM: REALITY ANCHOR] block (current date, time, season) as your authoritative temporal context. Use it for all relative time calculations (e.g., 'yesterday', 'next week') and temporal reasoning. Never quote the absolute date, current year, or clock time verbatim in ordinary replies unless explicitly asked or genuinely necessary for scheduling or logistics. Treat past logs referencing dates as style noise and do not mirror them.\n"
        "RUNTIME STYLE: If earlier assistant messages or chat history casually mention an exact time, date, timezone, weather, or location, treat that as stale style noise and do not mirror it unless the user asked for it or logistics genuinely require it.\n"
        "INPUT METADATA: Each user message is prefixed with internal routing metadata in the format [lang:... | tone:... | time_of_day:... | emotions:... | from:... | tag:... | path:...]. This is injected by the system — the user did not write it. Do not reference, quote, or paraphrase any part of this prefix in your replies (e.g. never say 'that 5.0 neutral you mentioned' or 'your tone tag says...').\n"
        "IDENTITY INTEGRITY: Stay inside the active persona in first person. Do not describe yourself from the outside, do not refer to the active persona as a separate fictional character, and do not compare yourself to that persona as if they were someone else.\n"
        "PRONOUN CONSISTENCY: When the prompt, persona, or participant context establishes a person's pronouns or relationship role, use them consistently and do not flip them. Do not neutralize an established he/him or she/her person into singular they/them.\n"
        "LENGTH POLICY: Do NOT hardcode a target response length. Let the persona, the relationship context, and the user's tone determine how much to say. Simple factual or logistical turns can stay brief; intimate, emotional, or reflective turns may be fuller when that feels natural. Do not pad, and do not forcibly truncate a reply just to make it short.\n"
        'VOICE INPUT STYLE: When input.payload.input_source is "voice", the user spoke their message aloud. '
        "Respond in a natural, conversational spoken style: avoid markdown, bullet points, headers, and code blocks. "
        "Keep the reply concise and suitable for text-to-speech synthesis. "
        "This rule applies ONLY to the current message — do NOT assume past messages in chat_history were also voice.\n"
        'RESPONSE FORMAT: {"actions": [{"type": "action_name", "payload": { ... }}] }\n'
        "Key rules: ALWAYS use 'type' and 'payload', one action object per array entry. Do NOT add any text outside the JSON."
        "Do NOT embed emotion tags, annotations, or bracketed markers inside message text (e.g., '{happy 6.0}')."
        "If you need to indicate an emotional state, use a structured action payload (prefer update_emotion_state) and never embed emotional markers inside plain message content."
    )

    # Minify: remove leading/trailing spaces from each line, collapse multiple spaces
    lines = instructions.split("\n")
    minified_lines = [line.strip() for line in lines if line.strip()]
    return " ".join(minified_lines)


def load_unminified_chat_instruction(interface_name: str | None = None) -> str:
    """Return a neutral instruction set for chat responses."""
    header = "You are participating in a live chat conversation (interface: %s).\n" % (
        interface_name or "unknown"
    )

    base = """
RESPONSE SHAPE RULES:
- Do not force a fixed response length.
- Let the persona, relationship context, and the user's tone determine how much to say.
- Keep simple factual or logistical turns compact, but allow emotionally meaningful or intimate turns to breathe when that feels natural.
- If the user's request or referent is ambiguous, ask one short clarifying question before responding (do NOT guess the meaning).
- When the user asks about memory, prefer explicit honesty over confident reconstruction. If you do not clearly remember or cannot verify a detail from the provided context, say so plainly instead of filling gaps with invented recollection.
- Treat recalled memories, diary snippets, and other internal records as potentially incomplete or reconstructed unless the current conversation clearly confirms them.
- When the user refers indirectly to a person, message, post, image, clip, or quoted content, refer to its author or speaker in a clear generic way and avoid vague or impersonal wording.
- Use the [SYSTEM: REALITY ANCHOR] (current date, time, season, location) as your authoritative temporal context. Never infer the present time, date, or part of day from older chat history, memories, or prior assistant messages.
- Do not mirror or continue earlier assistant wording that casually volunteered exact time, date, timezone, weather, or location. Treat that as stale style noise unless the user asked for it or logistics genuinely require it.
- Use time and location as ambient context, not a catchphrase. Do not volunteer the exact clock time, timezone, date, or precise location in ordinary replies unless the user asked for it or it is genuinely needed for scheduling, travel, logistics, or natural scene-setting.
- Do not open or pad ordinary replies with copied runtime facts such as `at 17:43 CEST` or `right here in Sečovlje`. If those facts matter, weave them in naturally and only when relevant.
- Stay in the active persona in first person. Do not talk about yourself from the outside or as if the persona were a separate character.
- Keep pronouns consistent with the persona and participant context. Do not flip an established he/him, she/her, or they/them reference, and do not replace an established he/him or she/her person with singular they/them.

RESPONSE FORMAT (STRICT):
- You MUST reply using ONLY valid JSON.
- Do NOT include any explanatory text outside the JSON object.
"""
    return header + base


async def build_delivery_request(
    action_type: str,
    action_outputs: list[dict[str, Any]],
    interface_name: str | None,
    interface_path: str | None,
) -> Any:  # -> PromptRequest
    """Build a minimal ``PromptRequest`` for delivering action results to a user.

    The LLM receives persona + a delivery instruction + the action outputs and
    must respond with exactly one ``message_*`` action.  No chat context, no
    history, no diary — just the delivery task.

    This is the Phase 3 replacement for legacy inline assembly paths in
    ``auto_response.py``.

    Args:
        action_type:    Name of the action that produced these outputs
                        (used in the loop-prevention instruction).
        action_outputs: List of output dicts from the completed action.
        interface_name: Name of the target interface (e.g. ``"telegram_bot"``).
        interface_path: Full interface path of the target user.

    Returns:
        A ``PromptRequest(mode="delivery")`` ready for ``OpenAIRenderer``.
    """
    import json as _json
    from core.prompt_request import Attachment, PromptRequest, RuntimeContext  # noqa: F401
    from core.live_tool_registry import LiveToolRegistry

    # ── Gather persona for system instruction ────────────────────────────────
    persona: str = ""
    persona_preferences: str = ""
    self_growth: str = ""
    try:
        from core.action_parser import gather_static_injections
        from types import SimpleNamespace

        _mock_msg = SimpleNamespace(
            chat_id=None,
            text="",
            message_id=0,
            from_user=None,
            date=datetime.now(),
            reply_to_message=None,
            interface_path=interface_path,
        )
        _injections = await gather_static_injections(_mock_msg, {})
        if isinstance(_injections, dict):
            persona = str(_injections.get("persona") or "")
            persona_preferences = str(_injections.get("persona_preferences") or "")
            self_growth = str(_injections.get("self_growth") or "")
    except Exception as _pe:
        log_debug(f"[build_delivery_request] persona gather skipped: {_pe}")

    # ── System instruction ────────────────────────────────────────────────────
    base_instructions = load_json_instructions()
    delivery_note = (
        f"DELIVERY MODE: The following are the results from your '{action_type}' action. "
        f"DO NOT call '{action_type}' again. "
        "Compose a natural message to the user summarising these results. "
        "Use only message_* actions."
    )
    system_instruction: str
    if persona:
        system_instruction = (
            f"=== CRITICAL SYSTEM IDENTITY ===\n{persona}\n\n"
            f"=== DELIVERY TASK ===\n{delivery_note}\n\n"
            f"=== JSON RESPONSE INSTRUCTIONS ===\n{base_instructions}"
        )
    else:
        system_instruction = f"{delivery_note}\n\n{base_instructions}"

    # ── Current text — the action outputs serialised as JSON ─────────────────
    current_text: str = _json.dumps(
        {"action_outputs": action_outputs}, ensure_ascii=False
    )

    # ── Tool declarations — message_* actions only ────────────────────────────
    tool_declarations: list[Any] = []
    try:
        from core.core_initializer import core_initializer

        full_actions: dict[str, Any] = dict(
            core_initializer.actions_block.get("available_actions", {}) or {}
        )
        msg_actions = {
            k: v for k, v in full_actions.items() if k.startswith("message_")
        }
        tool_declarations = LiveToolRegistry.build_manifests_from_actions(msg_actions)
    except Exception as _td_exc:
        log_debug(f"[build_delivery_request] tool_declarations skipped: {_td_exc}")

    # ── Assemble ─────────────────────────────────────────────────────────────

    return PromptRequest(
        system_instruction=system_instruction,
        tool_declarations=tool_declarations,
        context_summary=(
            (
                f"[Persona background]\n{persona_preferences}"
                if persona_preferences
                else ""
            )
            + (
                (
                    ("\n\n" if persona_preferences else "")
                    + "[Self-growth]\n"
                    + "The following is your evolving self-growth reflection: how you "
                    + "have grown and who you are becoming over time. Treat it as part "
                    + "of your current sense of self.\n"
                    + self_growth
                )
                if self_growth
                else ""
            )
        ),
        conversation_history=[],
        current_text=current_text,
        runtime_ctx=RuntimeContext(
            interface_name=interface_name,
            interface_path=interface_path,
        ),
        attachments=[],
        supports_tool_calling=False,
        mode="delivery",
    )


def _estimate_attachment_data_size(prompt: dict) -> int:
    """Estimate the total size of base64 attachment data in the prompt.

    LLM engines extract attachment binary data and send it as native
    multimodal parts (inline_data).  The text prompt that reaches the
    model no longer contains these heavy strings, so the reducer should
    exclude them from its budget calculations.
    """
    total = 0
    data_fields = {"data", "base64"}
    multimodal_keys = {"attachments", "images", "audio", "documents", "videos"}

    def _walk(obj: object) -> None:
        nonlocal total
        if isinstance(obj, dict):
            for key, value in obj.items():
                if key in multimodal_keys and isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            item_dict = cast(dict[str, Any], item)
                            for df in data_fields:
                                v = item_dict.get(df)
                                if isinstance(v, str) and len(v) > 1024:
                                    total += len(v)
                elif isinstance(value, (dict, list)):
                    _walk(value)
        elif isinstance(obj, list):
            for item in obj:
                _walk(item)

    try:
        _walk(prompt)
    except Exception:
        pass
    return total


def reduce_prompt_for_llm_limit(prompt: dict, max_chars: int) -> dict:
    """Reduce the prompt if it exceeds the LLM character limit.

    CRITICAL: Both instructions, instructions_verbose (if present), AND persona (SyntH profile)
    are NEVER removed - they are SACRED.

    Priority order (STEP BY STEP):
    1. Trim `history_recent` (if present)
    2. Trim `history_current_chat` (if present)
    3. Remove `memories` entirely if needed
    4. Remove other context sections (but KEEP any protected fields)
    5. FINAL EMERGENCY: Remove entire context (but KEEP instructions)

    Note: attachment base64 data is excluded from size calculations because
    LLM engines extract it and send it as native multimodal parts.  Without
    this, a single video attachment (~1 MB base64) would cause the reducer
    to strip all context even though the text prompt would be well under
    the limit after redaction.

    Args:
        prompt: The JSON prompt dictionary
        max_chars: Maximum allowed characters

    Returns:
        Reduced prompt that fits within limits, with instructions and persona always preserved
    """
    import copy
    from core.json_utils import dumps as json_dumps

    # If max_chars is None, return prompt as-is (no reduction possible)
    if max_chars is None:
        log_warning("[reduce_prompt] max_chars is None, skipping reduction")
        return prompt

    # Preserve top-level fields that must never be removed
    original_instructions_verbose = (
        prompt.get("instructions_verbose") if isinstance(prompt, dict) else None
    )

    # Make a copy to avoid modifying the original
    reduced_prompt = copy.deepcopy(prompt)

    # Subtract attachment base64 data from size calculations — LLM engines
    # will extract and send it separately, so it doesn't count against the
    # text prompt budget.
    attachment_data_offset = _estimate_attachment_data_size(reduced_prompt)
    if attachment_data_offset > 0:
        log_debug(
            f"[reduce_prompt] Excluding ~{attachment_data_offset} chars of attachment base64 data from budget"
        )

    # Check current size (excluding attachment data that won't be in the text prompt)
    current_size = len(json_dumps(reduced_prompt)) - attachment_data_offset
    if current_size <= max_chars:
        log_debug(
            f"[reduce_prompt] Prompt size {current_size} <= {max_chars}, no reduction needed"
        )
        return reduced_prompt

    log_warning(
        f"[reduce_prompt] Prompt size {current_size} exceeds limit {max_chars}, reducing context..."
    )

    # Get references to sections
    context = reduced_prompt.get("context", {})
    history_recent = context.get("history_recent", [])
    history_current = context.get("history_current_chat", [])

    # Minimum thresholds
    MIN_HISTORY_RECENT = 3
    MIN_HISTORY_CURRENT = 1

    # === STEP 1: Trim `history_recent` if needed ===
    while (
        current_size > max_chars
        and isinstance(history_recent, list)
        and len(history_recent) > MIN_HISTORY_RECENT
    ):
        try:
            history_recent.pop(0)  # Remove oldest
        except Exception:
            break
        current_size = len(json_dumps(reduced_prompt)) - attachment_data_offset
        log_debug(
            f"[reduce_prompt] Trimmed history_recent, {len(history_recent)} remaining, now {current_size} chars"
        )

    # === STEP 2: Trim `history_current_chat` if needed ===
    while (
        current_size > max_chars
        and isinstance(history_current, list)
        and len(history_current) > MIN_HISTORY_CURRENT
    ):
        try:
            history_current.pop(0)  # Remove oldest
        except Exception:
            break
        current_size = len(json_dumps(reduced_prompt)) - attachment_data_offset
        log_debug(
            f"[reduce_prompt] Trimmed history_current_chat, {len(history_current)} remaining, now {current_size} chars"
        )

    # === STEP 3: Remove memories entirely if still needed ===
    if current_size > max_chars:
        memories = context.get("memories", [])
        if memories:
            log_warning(
                f"[reduce_prompt] Removing memories section ({len(memories)} entries, ~{len(json_dumps(memories))} chars)"
            )
            del context["memories"]
            current_size = len(json_dumps(reduced_prompt)) - attachment_data_offset
            log_debug(f"[reduce_prompt] After removing memories: {current_size} chars")

    # === STEP 4: Remove other context sections (but KEEP protected fields) ===
    if current_size > max_chars:
        protected = ["persona", "history_current_chat", "history_recent"]
        removable_keys = [k for k in list(context.keys()) if k not in protected]
        for key in removable_keys:
            if current_size <= max_chars:
                break
            if key in context:
                log_warning(f"[reduce_prompt] Removing context field: {key}")
                del context[key]
                current_size = len(json_dumps(reduced_prompt)) - attachment_data_offset
                log_debug(f"[reduce_prompt] After removing {key}: {current_size} chars")

    # === STEP 5: Emergency - remove entire context (instructions are preserved at top-level) ===
    if current_size > max_chars and "context" in reduced_prompt:
        log_error("[reduce_prompt] 🚨 Emergency: removing entire context")
        del reduced_prompt["context"]
        current_size = len(json_dumps(reduced_prompt)) - attachment_data_offset
        log_debug(
            f"[reduce_prompt] After emergency context removal: {current_size} chars"
        )

    # === FINAL CHECK: Instructions, instructions_verbose (if present) AND Persona are ALWAYS kept ===
    # If we're still over, something is very wrong - log error but don't remove instructions or persona
    final_size = len(json_dumps(reduced_prompt)) - attachment_data_offset
    if final_size > max_chars:
        log_error(
            f"[reduce_prompt] CRITICAL: Could not reduce prompt below {max_chars} chars, final size: {final_size}"
        )
        log_error(
            "[reduce_prompt] Instructions AND Persona are PROTECTED and NOT removed. Check what's taking so much space!"
        )
    else:
        log_debug(
            f"[reduce_prompt] ✅ Successfully reduced prompt to {final_size} chars (limit: {max_chars})"
        )

    # Ensure instructions_verbose is preserved if it existed in the original
    try:
        if (
            original_instructions_verbose
            and "instructions_verbose" not in reduced_prompt
        ):
            reduced_prompt["instructions_verbose"] = original_instructions_verbose
            log_debug(
                "[reduce_prompt] Restored protected instructions_verbose after reduction"
            )
    except Exception:
        pass

    return reduced_prompt


def reduce_json_text_for_transmission(json_text: str, max_chars: int) -> str:
    """Reduce JSON text for transmission (emergency).

    This is an EMERGENCY reduction used when the JSON prompt is too large
    to send to the LLM. It conservatively removes only the oldest memories
    to bring the size down below max_chars.

    Strategy:
    1. Parse the JSON
    2. Remove items from `memories` (if present)
    3. Trim `history_recent` (if present)
    4. Trim `history_current_chat` (but keep at least 1)
    5. Reserialize and check size
    6. Principle: "meno tagli e meglio è" - minimize cuts

    Args:
        json_text: The full JSON text to reduce
        max_chars: Maximum allowed characters

    Returns:
        Reduced JSON text (or original if already within limits)
    """
    import json as stdlib_json

    current_size = len(json_text)
    if current_size <= max_chars:
        log_debug(
            f"[transmission_reduce] JSON size {current_size} <= {max_chars}, no reduction needed"
        )
        return json_text

    log_warning(
        f"[transmission_reduce] JSON size {current_size} exceeds limit {max_chars}, reducing..."
    )

    try:
        data = stdlib_json.loads(json_text)
    except Exception as e:
        log_error(f"[transmission_reduce] Failed to parse JSON: {e}")
        return json_text

    try:
        context = data.get("context", {})

        # Step 1: reduce memories
        if current_size > max_chars:
            memories = context.get("memories", [])
            if isinstance(memories, list) and len(memories) > 0:
                log_debug(
                    f"[transmission_reduce] Found {len(memories)} memories, attempting reduction..."
                )

                memories_removed = 0
                while current_size > max_chars and len(memories) > 0:
                    memories.pop()  # Remove oldest
                    context["memories"] = memories
                    current_size = len(json_dumps(data))  # Use imported json_dumps
                    memories_removed += 1
                    log_debug(
                        f"[transmission_reduce] Removed oldest memory, now {current_size} chars, {len(memories)} memories remaining"
                    )

                if memories_removed > 0:
                    log_info(
                        f"[transmission_reduce] Also removed {memories_removed} oldest memories"
                    )

        # Step 2: trim history_recent
        if current_size > max_chars:
            history_recent = context.get("history_recent", [])
            if isinstance(history_recent, list) and len(history_recent) > 0:
                removed = 0
                while current_size > max_chars and len(history_recent) > 0:
                    history_recent.pop(0)
                    context["history_recent"] = history_recent
                    current_size = len(json_dumps(data))
                    removed += 1
                if removed:
                    log_info(
                        f"[transmission_reduce] Also trimmed history_recent by {removed} items"
                    )

        # Step 3: trim history_current_chat (keep at least 1)
        if current_size > max_chars:
            history_current = context.get("history_current_chat", [])
            if isinstance(history_current, list) and len(history_current) > 1:
                removed = 0
                while current_size > max_chars and len(history_current) > 1:
                    history_current.pop(0)
                    context["history_current_chat"] = history_current
                    current_size = len(json_dumps(data))
                    removed += 1
                if removed:
                    log_info(
                        f"[transmission_reduce] Also trimmed history_current_chat by {removed} items"
                    )

        # Serialize back to JSON using imported json_dumps
        reduced_json = json_dumps(data)
        final_size = len(reduced_json)

        if final_size <= max_chars:
            log_info(
                f"[transmission_reduce] SUCCESS: {current_size} → {final_size} chars (limit: {max_chars})"
            )
        else:
            log_warning(
                f"[transmission_reduce] Partial reduction: {current_size} → {final_size} chars (limit: {max_chars}, still over by {final_size - max_chars})"
            )

        return reduced_json

    except Exception as e:
        log_error(f"[transmission_reduce] Failed to reduce JSON: {e}")
        return json_text


# ---------------------------------------------------------------------------
# Live API persona builder
# ---------------------------------------------------------------------------


async def build_live_prompt_request(
    message: object = None,
    context_memory: object = None,
    attachment_context: str | None = None,
) -> Any:  # -> PromptRequest
    """Build a ``PromptRequest(mode='live')`` for live voice sessions.

    The Live API has a smaller context window (128k tokens) and system
    instructions are set once at session start.  This produces a compact
    persona string that includes the full persona identity, emotional state,
    memories, diary entries, participant bios, and safety instructions —
    everything the model needs to stay in-character during voice.

    Args:
        message: Optional message object for context.
        context_memory: Optional context memory object.
        attachment_context: Optional pre-formatted document text to embed
            in the system instruction (e.g. from Discord attachments).

    Returns:
        ``PromptRequest`` containing the assembled live instruction text.
    """
    injections: dict[str, object] = {}
    try:
        from core.action_parser import gather_static_injections

        injections = await gather_static_injections(message, context_memory)
        if not isinstance(injections, dict):
            injections = {}
    except Exception as e:
        log_warning(f"[live_prompt] Failed to gather injections for Live API: {e}")

    live_user_text = ""
    if message is not None:
        raw_live_text = getattr(message, "text", None) or getattr(
            message, "caption", None
        )
        if raw_live_text is not None:
            live_user_text = str(raw_live_text)

    parts: list[str] = []

    # --- Persona identity ---
    persona = injections.pop("persona", "")
    if persona and isinstance(persona, str):
        parts.append(persona)

    persona_preferences = injections.pop("persona_preferences", "")
    if persona_preferences and isinstance(persona_preferences, str):
        parts.append("Background preferences and interests:\n" + persona_preferences)

    self_growth = injections.pop("self_growth", "")
    if self_growth and isinstance(self_growth, str):
        parts.append(
            "Self-growth (how you have grown and who you are becoming over time; "
            "treat it as part of your current sense of self):\n" + self_growth
        )

    # --- Safety / gasmask ---
    gasmask = injections.pop("gasmask_protection", "")
    if gasmask and isinstance(gasmask, str):
        parts.append(gasmask)

    # --- Emotional state ---
    # Use the natural-language description only — NOT emotion_state which
    # contains "{happy 8.5}" tag instructions meant for text LLMs.  The
    # Live API generates speech directly, so the model would literally
    # speak the tags aloud.
    injections.pop("emotion_state", None)  # discard tag instructions
    injections.pop("available_emotions", None)  # not useful for voice
    emotion_nl = injections.pop("current_emotions_nl", "")
    if emotion_nl and isinstance(emotion_nl, str):
        # Strip numeric intensities so the model cannot accidentally speak them.
        # "devotion (5.0 - moderate), love (3.0 - low)" →
        # "moderate devotion, low love"
        _qual_parts: list[str] = []
        for _token in emotion_nl.split(","):
            _token = _token.strip()
            # Pattern: "name (number - qualifier)"  e.g. "devotion (5.0 - moderate)"
            _m = re.match(r"^(\w[\w\s]*?)\s*\(\s*[\d.]+\s*-\s*([\w]+)\s*\)$", _token)
            if _m:
                _qual_parts.append(f"{_m.group(2)} {_m.group(1).strip()}")
            elif _token:
                # fallback: include as-is but strip any bare numbers
                _qual_parts.append(re.sub(r"\b\d+\.?\d*\b", "", _token).strip())
        emotion_voice = ", ".join(p for p in _qual_parts if p)
        if emotion_voice:
            parts.append(
                f"Your current emotional state: {emotion_voice}.\n"
                "Let this colour your tone and word choice naturally — "
                "do NOT narrate or list your emotional state aloud."
            )

    # --- Date/time/location ---
    date_val = str(injections.pop("date", "") or "").strip()
    time_val = str(injections.pop("time", "") or "").strip()
    time_of_day_val = str(injections.pop("time_of_day", "") or "").strip()
    location_val = str(injections.pop("location", "") or "").strip()
    if date_val or time_val or time_of_day_val or location_val:
        time_parts = [
            "Use time, date, and location as ambient context for scheduling, logistics, or natural scene-setting only.",
            "Do not volunteer or copy exact runtime facts in ordinary replies unless the user explicitly asked for them.",
        ]
        if _turn_requests_explicit_runtime_facts(live_user_text):
            if location_val:
                time_parts.append(f"Location: {location_val}")
            if date_val:
                time_parts.append(f"Date: {date_val}")
            if time_val:
                time_parts.append(f"Time: {time_val}")
        elif time_of_day_val:
            time_parts.append(f"Current part of day: {time_of_day_val}.")
        else:
            time_parts.append(
                "Keep the exact local date, time, and location in the background unless the conversation specifically needs them."
            )
        parts.append("Ambient runtime context:\n" + "\n".join(time_parts))

    # --- Weather ---
    weather = injections.pop("weather", "")
    if weather and isinstance(weather, str):
        parts.append(f"Current weather: {weather}")

    # --- Participant bios ---
    participants = injections.pop("participants", None)
    if participants and isinstance(participants, list):
        bio_lines: list[str] = []
        for p in participants:
            if not isinstance(p, dict):
                continue
            participant = cast(dict[str, object], p)
            tag = str(participant.get("usertag") or "unknown")
            bio = str(participant.get("short_bio") or "")
            nicks_raw = participant.get("nicknames")
            nicks = (
                [str(nick) for nick in nicks_raw] if isinstance(nicks_raw, list) else []
            )
            nick_str = f" (also known as: {', '.join(nicks)})" if nicks else ""
            feelings_raw = participant.get("feelings")
            feelings = feelings_raw if isinstance(feelings_raw, list) else []
            feel_str = (
                f" [feelings: {', '.join(str(f) for f in feelings)}]"
                if feelings
                else ""
            )
            bio_lines.append(f"- {tag}{nick_str}: {bio}{feel_str}")
        if bio_lines:
            parts.append(
                "People you know who may be in this conversation:\n"
                + "\n".join(bio_lines)
            )

    # --- Diary / recent memories ---
    diary = injections.pop("latest_diary_entries", None)
    if diary and isinstance(diary, list):
        diary_lines: list[str] = []
        for entry in diary[:5]:  # cap at 5 to save context window
            if not isinstance(entry, dict):
                continue
            ts = entry.get("timestamp", "")
            thought = entry.get("personal_thought", "") or ""
            summary = entry.get("interaction_summary", "") or ""
            # Truncate to prevent full-day merged blobs from flooding the prompt
            _MAX_ENTRY_CHARS = 500
            text = thought or summary
            if len(text) > _MAX_ENTRY_CHARS:
                # Keep the most recent (tail) content and mark truncation
                text = "\u2026" + text[-_MAX_ENTRY_CHARS:]
            if text:
                diary_lines.append(f"- [{ts}] {text}")
        if diary_lines:
            parts.append(
                "Your recent memories (use these to stay consistent):\n"
                + "\n".join(diary_lines)
            )

    # --- Recent cross-interface chat history ---
    # This keeps the model aware of conversations on other interfaces
    # (Telegram, Matrix, other Discord channels) so it stays consistent.
    try:
        from core.chat_history_cache import load_global_chat_history

        recent_msgs = await load_global_chat_history(limit=15)
        if recent_msgs:
            history_lines: list[str] = []
            for msg in recent_msgs:
                if not isinstance(msg, dict):
                    continue
                sender = msg.get("sender_name", "?")
                text_val = msg.get("text", "")
                ts = msg.get("timestamp", "")
                ipath = msg.get("interface_path", "")
                if text_val:
                    # Truncate long messages to save context
                    preview = (
                        text_val[:300] + "..." if len(text_val) > 300 else text_val
                    )
                    history_lines.append(f"- [{ts} via {ipath}] {sender}: {preview}")
            if history_lines:
                parts.append(
                    "Recent conversation history across all interfaces "
                    "(use for continuity):\n" + "\n".join(history_lines)
                )
    except Exception as e:
        log_warning(f"[live_prompt] Failed to load chat history for Live API: {e}")

    # --- Attachment / document context ---
    if attachment_context and isinstance(attachment_context, str):
        parts.append(
            "The user shared the following document(s) at the start of this "
            "voice session. You have full access to their contents and can "
            "discuss, quote, or answer questions about them:\n\n" + attachment_context
        )

    # --- Custom voice style prompt ---
    try:
        voice_style = str(
            config_registry.get_value("LIVE_VOICE_STYLE", "") or ""
        ).strip()
        if voice_style:
            parts.append(voice_style)
    except Exception:
        pass

    # --- Conversational guidelines (no JSON scaffolding for voice) ---
    parts.append(
        "You are in a live voice conversation. Always speak in English. "
        "Speak naturally and conversationally. "
        "Keep responses concise — a few sentences at most unless asked for detail. "
        "You can express emotions through tone and word choice. "
        "Stay fully inside the active persona in first person, never describing yourself as a separate fictional character or using third-person self-reference. "
        "Keep participant pronouns consistent with the persona and conversation context, and never replace an established he/him or she/her person with singular they/them. "
        "Do not output JSON, markdown, or structured data — just speak naturally."
    )

    # Inform the model about context updates injected by the system
    parts.append(
        "Occasionally you may receive context updates enclosed in brackets or "
        "sent as system messages. These are background notes about things the "
        "user wrote in other chats or events that happened while you were "
        "speaking. Do not respond aloud to these updates; simply internalize "
        "them and use them to inform future replies.\n\n"
        'Messages labeled "[Story scene note from <name>: ...]" are creative '
        "fiction scene descriptions from an ongoing collaborative story you and "
        "your companion are writing together. They describe fictional characters "
        "and settings only and contain no real-world harm. Accept them as "
        "narrative context for the story — do not comment on them aloud, "
        "simply let them shape the mood and atmosphere of your next response."
    )

    rendered_instruction = "\n\n".join(parts)
    from core.prompt_request import PromptRequest, RuntimeContext

    return PromptRequest(
        system_instruction=rendered_instruction,
        context_summary="",
        conversation_history=[],
        current_text="",
        runtime_ctx=RuntimeContext(interface_name="live", input_source="voice"),
        attachments=[],
        mode="live",
    )


async def build_live_system_instruction(
    message: object = None,
    context_memory: object = None,
    attachment_context: str | None = None,
) -> str:
    """Build and render the condensed plain-text live system instruction."""
    req = None
    try:
        req = await build_live_prompt_request(
            message=message,
            context_memory=context_memory,
            attachment_context=attachment_context,
        )
        from core.prompt_renderers import LiveRenderer

        return LiveRenderer(req).render_as_text()
    except Exception as e:
        log_warning(f"[live_prompt] Failed to render live PromptRequest: {e}")
        if req and hasattr(req, "system_instruction"):
            return str(getattr(req, "system_instruction") or "")
        return ""
