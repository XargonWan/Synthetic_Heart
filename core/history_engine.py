from __future__ import annotations

import hashlib
import inspect
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence

from core.config_manager import config_registry
from core.logging_utils import log_debug
from core.variables_engine import register_exposed_var

from core.history_types import HistoryContribution, HistoryEntry


register_exposed_var(
    "CONTEXT_VERBOSITY",
    label="Context Verbosity (items)",
    default=10,
    value_type=int,
    ui_type="number",
    description="Global max number of items for history-like context lists.",
    scope="core",
    component="history_engine",
)

register_exposed_var(
    "THOUGHTS_LIMIT",
    label="Thoughts Limit (items)",
    default=5,
    value_type=int,
    ui_type="number",
    description="Max number of thoughts (e.g. grillo/diary thoughts) to include.",
    scope="core",
    component="history_engine",
    advanced=True,
)

register_exposed_var(
    "AI_DIARY_FULL",
    label="AI Diary Full",
    default=1,
    value_type=int,
    ui_type="bool",
    description="If enabled, include full diary entries (within verbosity). If disabled, include thoughts-only.",
    scope="core",
    component="history_engine",
    advanced=True,
)

register_exposed_var(
    "ENABLE_HISTORY_CURRENT_CHAT",
    label="Enable history_current_chat",
    default=1,
    value_type=int,
    ui_type="bool",
    description="Enable current-chat history recap contribution.",
    scope="core",
    component="history_engine",
)

register_exposed_var(
    "ENABLE_HISTORY_RECENT",
    label="Enable history_recent",
    default=1,
    value_type=int,
    ui_type="bool",
    description="Enable recent-history contribution.",
    scope="core",
    component="history_engine",
)

register_exposed_var(
    "UNIFIED_HISTORY",
    label="Unified History",
    default=1,
    value_type=int,
    ui_type="bool",
    description="If enabled, merges all chat streams into the current conversation history (shared brain).",
    scope="core",
    component="history_engine",
    advanced=True,
)

register_exposed_var(
    "ENABLE_AI_DIARY",
    label="Enable AI Diary",
    default=1,
    value_type=int,
    ui_type="bool",
    description="Enable AI diary contributions.",
    scope="core",
    component="history_engine",
    advanced=True,
)

register_exposed_var(
    "ENABLE_MEMORIES",
    label="Enable Memories",
    default=1,
    value_type=int,
    ui_type="bool",
    description="Enable memory search contributions.",
    scope="core",
    component="history_engine",
    advanced=True,
)

register_exposed_var(
    "ENABLE_THOUGHTS",
    label="Enable Thoughts",
    default=1,
    value_type=int,
    ui_type="bool",
    description="Enable thoughts output list.",
    scope="core",
    component="history_engine",
    advanced=True,
)

register_exposed_var(
    "ENABLE_TAGS_PLACEHOLDER",
    label="Enable Tags Placeholder",
    default=1,
    value_type=int,
    ui_type="bool",
    description="Include an empty tags_placeholder list for future multi-step flows.",
    scope="core",
    component="history_engine",
    advanced=True,
)


def _get_int(key: str, default: int) -> int:
    try:
        return int(config_registry.get_value(key, default, value_type=int))
    except Exception:
        try:
            return int(config_registry.get_value(key, default))
        except Exception:
            return default


def _get_bool(key: str, default: bool) -> bool:
    val = _get_int(key, 1 if default else 0)
    return bool(val)


def _format_ts(ts: Any) -> str:
    try:
        if isinstance(ts, str):
            ts = ts.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts)
            return dt.strftime("%d/%m/%y:%H%M")
        if hasattr(ts, "isoformat"):
            # datetime
            return ts.strftime("%d/%m/%y:%H%M")  # type: ignore[attr-defined]
    except Exception:
        pass
    return str(ts or "")


def _entry_to_text(entry: HistoryEntry) -> str:
    if isinstance(entry, str):
        return entry

    if not isinstance(entry, dict):
        return str(entry)

    # Chat-like message dicts
    text = entry.get("text") or entry.get("message_text") or entry.get("content") or ""
    sender = (
        entry.get("sender_name")
        or entry.get("username")
        or entry.get("sender_id")
        or "Unknown"
    )
    ts = entry.get("timestamp") or entry.get("date") or ""

    # Diary-like dicts
    if "interaction_summary" in entry or "personal_thought" in entry:
        summary = entry.get("interaction_summary") or ""
        thought = entry.get("personal_thought") or ""
        parts = []
        if summary:
            parts.append(f"summary: {summary}")
        if thought:
            parts.append(f"thought: {thought}")
        body = " | ".join(parts) or (text or "")
        return f"[diary {_format_ts(ts)}] {body}".strip()

    safe_text = str(text).replace('"', "'")
    return f'[{_format_ts(ts)}] {sender}: "{safe_text}"'.strip()


def _source_label(
    entry: HistoryEntry, current_interface_path: str | None = None
) -> str | None:
    if not isinstance(entry, dict):
        return None
    entry_path = entry.get("interface_path") or entry.get("source_path")
    if not entry_path:
        return None
    if current_interface_path and entry_path == current_interface_path:
        return None
    return str(entry_path)


def _entry_to_text_with_source(
    entry: HistoryEntry, current_interface_path: str | None = None
) -> str:
    base = _entry_to_text(entry)
    label = _source_label(entry, current_interface_path=current_interface_path)
    if not label:
        return base
    return f"[from {label}] {base}".strip()


def _dedup_key(text: str) -> str:
    normalized = " ".join((text or "").strip().split()).lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


async def _call_with_supported_kwargs(obj: Any, method_name: str, **kwargs):
    fn = getattr(obj, method_name, None)
    if fn is None:
        return None

    try:
        sig = inspect.signature(fn)
        call_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
    except Exception:
        call_kwargs = kwargs

    try:
        result = fn(**call_kwargs)
        if inspect.isawaitable(result):
            return await result
        return result
    except Exception as e:
        log_debug(
            f"[history_engine] {obj.__class__.__name__}.{method_name} failed: {e}"
        )
        return None


class HistoryEngine:
    """Collects, deduplicates, and formats history-like prompt context."""

    async def build_context(
        self,
        message: Any,
        context_memory: Any,
        interface_name: Optional[str] = None,
        text: str = "",
        memories: Optional[Sequence[HistoryEntry]] = None,
    ) -> Dict[str, Any]:
        verbosity = max(0, _get_int("CONTEXT_VERBOSITY", 10))
        thoughts_limit = max(0, _get_int("THOUGHTS_LIMIT", 5))

        enable_current = _get_bool("ENABLE_HISTORY_CURRENT_CHAT", True)
        enable_recent = _get_bool("ENABLE_HISTORY_RECENT", True)
        enable_diary = _get_bool("ENABLE_AI_DIARY", True)
        enable_memories = _get_bool("ENABLE_MEMORIES", True)
        enable_thoughts = _get_bool("ENABLE_THOUGHTS", True)
        enable_tags_placeholder = _get_bool("ENABLE_TAGS_PLACEHOLDER", True)
        diary_full = _get_bool("AI_DIARY_FULL", True)
        unified_mode = _get_bool("UNIFIED_HISTORY", True)

        interface_path = getattr(message, "interface_path", None)
        if not interface_path:
            try:
                # Common runtime shape: message is a dict similar to prompt `input`
                if isinstance(message, dict):
                    interface_path = (
                        (message.get("payload") or {}).get("source") or {}
                    ).get("interface_path")
                # Or message has a `payload` attribute
                elif hasattr(message, "payload"):
                    payload = getattr(message, "payload", None) or {}
                    if isinstance(payload, dict):
                        interface_path = (payload.get("source") or {}).get(
                            "interface_path"
                        )
            except Exception:
                interface_path = None

        # Additional fallback: plugin_instance passes `context_memory['interface_path']`
        if not interface_path and isinstance(context_memory, dict):
            try:
                interface_path = context_memory.get("interface_path")
            except Exception:
                interface_path = None

        # `context_memory` can be either:
        # - the centralized chat map: { interface_path: deque([...]) }
        # - an interface/user context dict (e.g. Telegram user_data)
        # - a mixed dict (chat map + metadata like 'interface_path')
        # We normalize to a chat-map view where possible.
        chat_map: Optional[dict] = (
            context_memory if isinstance(context_memory, dict) else None
        )
        if interface_path:
            try:
                current_val = (
                    chat_map.get(interface_path) if isinstance(chat_map, dict) else None
                )
                has_current = isinstance(current_val, (list, tuple)) or hasattr(
                    current_val, "__iter__"
                )
            except Exception:
                has_current = False

            if not has_current:
                # Fall back to centralized context manager when the caller passes a context dict.
                try:
                    from core.chat_context_manager import (
                        get_context_memory as _get_global_context_memory,
                    )

                    global_map = _get_global_context_memory()
                    if isinstance(global_map, dict) and interface_path in global_map:
                        chat_map = global_map
                except Exception:
                    pass

        history_current_chat: List[str] = []
        history_recent: List[str] = []
        thoughts: List[str] = []

        seen_history: set[str] = set()

        # --- Core contributions ---
        if enable_current and interface_path:
            try:
                msgs: List[dict] = []
                # Prefer in-memory first (if context_memory is a chat map)
                if isinstance(chat_map, dict) and interface_path in chat_map:
                    try:
                        msgs = list(chat_map.get(interface_path, []))
                    except Exception:
                        msgs = []

                # Fall back to persisted cache if not enough
                if verbosity > 0 and len(msgs) < verbosity:
                    try:
                        from core.chat_history_cache import (
                            load_chat_history as cache_load,
                        )

                        cached = await cache_load(interface_path)
                        combined = list(msgs) + list(cached)
                        msgs = combined[-verbosity:]
                    except Exception as e:
                        log_debug(
                            f"[history_engine] Could not load cached messages for current chat: {e}"
                        )

                for m in msgs[-verbosity:] if verbosity > 0 else []:
                    line = _entry_to_text_with_source(
                        m, current_interface_path=interface_path
                    )
                    k = _dedup_key(line)
                    if k in seen_history:
                        continue
                    history_current_chat.append(line)
                    seen_history.add(k)
            except Exception as e:
                log_debug(f"[history_engine] Failed building history_current_chat: {e}")

        if unified_mode:
            try:
                unified_candidates: List[dict] = []

                try:
                    from core.chat_history_cache import load_global_chat_history

                    db_history = await load_global_chat_history(
                        limit=verbosity * 5 if verbosity > 0 else 100
                    )
                    unified_candidates.extend(list(db_history))
                except Exception as db_e:
                    log_debug(
                        f"[history_engine] Failed to load global chat history: {db_e}"
                    )

                if isinstance(chat_map, dict):
                    for k, q in chat_map.items():
                        # Skip metadata keys
                        if isinstance(k, str) and k in (
                            "interface_path",
                            "chat_id",
                            "thread_id",
                            "system_message",
                            "user_id",
                            "username",
                        ):
                            continue

                        # Skip invalid types
                        if isinstance(q, (str, dict)):
                            continue

                        if isinstance(q, (list, tuple)) or hasattr(q, "__iter__"):
                            unified_candidates.extend(list(q))

                def _uni_sort_key(m: Any) -> float:
                    if not isinstance(m, dict):
                        return 0.0
                    ts = m.get("timestamp") or m.get("date")
                    if isinstance(ts, str):
                        try:
                            normalized_ts = ts.replace("Z", "+00:00")
                            dt = datetime.fromisoformat(normalized_ts)
                            return dt.timestamp()
                        except Exception:
                            return 0.0
                    if hasattr(ts, "timestamp"):
                        try:
                            return float(ts.timestamp())
                        except Exception:
                            return 0.0
                    return 0.0

                if unified_candidates:
                    unified_candidates.sort(key=_uni_sort_key)

                    # Filter out internal system messages (e.g. Grillo tags, Pattern Analysis)
                    # These typically appear on '.../-1' as self-monologues.
                    # We preserve actual chat on '/-1' (valid WebUI sessions).
                    def _is_internal_noise(m: dict) -> bool:
                        path = str(m.get("interface_path", "") or "")
                        if not path.endswith("/-1"):
                            return False

                        txt = m.get("text") or m.get("message_text") or ""
                        if not txt:
                            return False

                        # Heuristics for non-chat system monologues
                        if "G.R.I.L.L.O." in txt:
                            return True
                        if txt.startswith("Pattern analysis"):
                            return True
                        if txt.startswith("Reflecting on"):
                            return True
                        # Grillo memory consolidation / diary reflection patterns
                        if txt.startswith("Recent cycles"):
                            return True
                        if txt.startswith("Analysis of recent"):
                            return True
                        if txt.startswith("Recent interaction patterns"):
                            return True
                        if txt.startswith("Relationship Reflection:"):
                            return True
                        # Messages from 'self' on /-1 are almost always system monologues
                        sender = str(m.get("sender_name", "") or "")
                        if sender.lower() == "self":
                            return True
                        return False

                    unified_candidates = [
                        m for m in unified_candidates if not _is_internal_noise(m)
                    ]

                history_current_chat = []
                seen_history = set()

                # Filter out the current inbound message to avoid duplication
                # with input.payload.text in the prompt
                input_text = (text or "").strip()

                for m in unified_candidates[-verbosity:] if verbosity > 0 else []:
                    # Skip if this entry's text matches the current input
                    entry_text = (
                        m.get("text") or m.get("message_text") or ""
                    ).strip() if isinstance(m, dict) else ""
                    if input_text and entry_text == input_text:
                        continue

                    line = _entry_to_text_with_source(
                        m, current_interface_path=interface_path
                    )
                    k = _dedup_key(line)
                    if k in seen_history:
                        continue
                    history_current_chat.append(line)
                    seen_history.add(k)
            except Exception as e:
                log_debug(f"[history_engine] Failed building UNIFIED history: {e}")

        if enable_recent and isinstance(chat_map, dict) and not unified_mode:
            try:
                # "Recent" is global: messages from other chats/threads, excluding the current one.
                # We best-effort sort by timestamp when possible.
                candidates: List[dict] = []
                for ip, q in list(chat_map.items()):
                    if interface_path and ip == interface_path:
                        continue
                    try:
                        # Ignore metadata keys (e.g. 'interface_path') that are not chat deques.
                        if isinstance(ip, str) and ip in (
                            "interface_path",
                            "chat_id",
                            "thread_id",
                            "system_message",
                        ):
                            continue
                        if not (isinstance(q, (list, tuple)) or hasattr(q, "__iter__")):
                            continue
                        for m in list(q or []):
                            if isinstance(m, dict):
                                candidates.append(m)
                    except Exception:
                        continue

                def _sort_key(m: dict) -> float:
                    ts = m.get("timestamp") or m.get("date")
                    if isinstance(ts, str):
                        try:
                            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                            return dt.timestamp()
                        except Exception:
                            return 0.0
                    if hasattr(ts, "timestamp"):
                        try:
                            return float(ts.timestamp())
                        except Exception:
                            return 0.0
                    return 0.0

                if candidates:
                    candidates.sort(key=_sort_key)
                for m in candidates[-verbosity:] if verbosity > 0 else []:
                    line = _entry_to_text_with_source(
                        m, current_interface_path=interface_path
                    )
                    k = _dedup_key(line)
                    if k in seen_history:
                        continue
                    history_recent.append(line)
                    seen_history.add(k)
            except Exception as e:
                log_debug(f"[history_engine] Failed building history_recent: {e}")

        # --- Plugin contributions ---
        contributions: List[HistoryContribution] = []
        try:
            from core.core_initializer import PLUGIN_REGISTRY

            plugins = (
                list(PLUGIN_REGISTRY.values())
                if isinstance(PLUGIN_REGISTRY, dict)
                else []
            )
        except Exception:
            plugins = []

        for plugin in plugins:
            if plugin is None:
                continue
            contribs = await _call_with_supported_kwargs(
                plugin,
                "get_history_contributions",
                message=message,
                context_memory=context_memory,
                interface_name=interface_name,
                interface_path=interface_path,
                text=text,
            )
            if isinstance(contribs, list):
                contributions.extend(
                    [c for c in contribs if isinstance(c, HistoryContribution)]
                )

        contributions.sort(key=lambda c: int(getattr(c, "priority", 0)), reverse=True)

        for c in contributions:
            enabled_var = getattr(c, "enabled_var", None)
            if enabled_var and not _get_bool(enabled_var, True):
                continue

            # Central gating for diary
            if c.name == "ai_diary" and not enable_diary:
                continue

            target = c.target or (
                "history_recent" if c.name != "thoughts" else "thoughts"
            )
            max_items = c.max_items

            if target in ("history_current_chat", "history_recent"):
                for raw in list(c.entries)[
                    : max_items or (verbosity if verbosity > 0 else None)
                ]:
                    line = _entry_to_text_with_source(
                        raw, current_interface_path=interface_path
                    )
                    k = _dedup_key(line)
                    if k in seen_history:
                        continue
                    if target == "history_current_chat":
                        history_current_chat.append(line)
                    else:
                        # Special: diary can be thoughts-only
                        if c.name == "ai_diary" and not diary_full:
                            continue
                        history_recent.append(line)
                    seen_history.add(k)

            elif target == "thoughts":
                if not enable_thoughts:
                    continue

                for raw in list(c.entries)[: max_items or thoughts_limit]:
                    t = _entry_to_text(raw)
                    if t:
                        thoughts.append(t)

        # Extract personal_thought from diary contributions for the thoughts array.
        # This runs regardless of diary_full — personal thoughts are always valuable
        # context for the LLM, separate from the full diary entries in history_recent.
        if enable_thoughts and enable_diary:
            diary_entries: List[dict[str, Any]] = []
            for c in contributions:
                if c.name != "ai_diary":
                    continue
                for raw in c.entries:
                    if isinstance(raw, dict):
                        diary_entries.append(raw)
            for entry in diary_entries:
                thought = entry.get("personal_thought")
                if thought:
                    ts = _format_ts(entry.get("timestamp", ""))
                    thoughts.append(f"[thought {ts}] {thought}".strip())
                if len(thoughts) >= thoughts_limit:
                    break

        # Core-provided memories (already searched by prompt engine)
        out_memories: List[Any] = []
        if enable_memories and memories:
            out_memories = list(memories)[:verbosity] if verbosity > 0 else []

        # Final per-target limits
        if verbosity > 0:
            history_current_chat = history_current_chat[-verbosity:]
            history_recent = history_recent[-verbosity:]
        if thoughts_limit > 0:
            thoughts = thoughts[-thoughts_limit:]

        context: Dict[str, Any] = {
            "history_current_chat": history_current_chat,
            "history_recent": history_recent,
            "thoughts": thoughts,
        }

        if enable_memories:
            context["memories"] = out_memories

        if enable_tags_placeholder:
            context["tags_placeholder"] = []

        return context
