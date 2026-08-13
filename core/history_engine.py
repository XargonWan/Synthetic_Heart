from __future__ import annotations

import hashlib
import inspect
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

from core.config_manager import config_registry
from core.interface_path_utils import is_vessel_history_entry, is_vessel_interface_path
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
    label="Enable Current Chat History",
    default=1,
    value_type=int,
    ui_type="bool",
    description="Include a recap of the active chat in prompt context.",
    scope="core",
    component="history_engine",
)

register_exposed_var(
    "ENABLE_HISTORY_RECENT",
    label="Enable Recent Chats History",
    default=1,
    value_type=int,
    ui_type="bool",
    description="Include a recap of recent chats outside the current one.",
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
    "VESSEL_PERCEPTION_CONTEXT_CAP",
    label="Vessel Perception Context Cap",
    default=3,
    value_type=int,
    description=(
        "During Rift Vessel embodiment, the max number of Synth's own recent "
        "autonomous perceptions (sightings/movement/will beats) kept in the "
        "current-chat context. Conversational lines (player chats + Synth's "
        "replies) are always kept. Prevents the perception stream from drowning "
        "a reactive player turn."
    ),
    scope="core",
    component="history_engine",
    advanced=True,
)

register_exposed_var(
    "VESSEL_PERCEPTION_COMPACT_MAX",
    label="Vessel Perception Compact Max (items)",
    default=20,
    value_type=int,
    description=(
        "During Rift Vessel embodiment, near-identical autonomous perceptions "
        "(e.g. repeated 'took damage' / 'collected 1 sand') are collapsed into a "
        "single line with an occurrence count and summed quantity — e.g. "
        "'took damage (×5)', 'collected sand (×5, total 23)'. This is the max "
        "number of such compacted lines kept in the current-chat context. "
        "Structural digit-masking only, never keyword matching (multi-language "
        "safe)."
    ),
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

register_exposed_var(
    "PROMPT_LITE_MODE",
    label="Prompt Lite Mode",
    default=0,
    value_type=int,
    ui_type="bool",
    description="Aggressively minimize prompt size for small/local models. Reduces history, strips actions, removes redundant context.",
    scope="core",
    component="history_engine",
)

register_exposed_var(
    "LITE_MODE_HISTORY_LIMIT",
    label="Lite Mode History Limit (items)",
    default=3,
    value_type=int,
    ui_type="number",
    description="Max number of recent chat history / recap items to inject while Prompt Lite Mode is on. Overrides Context Verbosity while lite mode is active.",
    scope="core",
    component="history_engine",
)

register_exposed_var(
    "HISTORY_AGE_MARKER_MINUTES",
    label="History Age Marker (minutes)",
    default=10,
    value_type=int,
    ui_type="number",
    description="Conversation-history turns older than this many minutes are annotated with a relative-age marker (e.g. '[3 hours earlier]') so the model can see how stale past turns are. Prevents outreach/beats from grounding in an hours-old thread as if it were live. Set 0 to disable.",
    scope="core",
    component="history_engine",
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
    """Format a timestamp for display in chat history.

    Converts to the server's local timezone so history entries align with the
    ``time`` context field that the time plugin injects (which also uses local time).
    """
    try:
        from core.time_zone_utils import utc_to_local

        if isinstance(ts, str):
            ts = ts.replace("Z", "+00:00")
            dt = datetime.fromisoformat(ts)
            if dt.tzinfo is not None:
                dt = utc_to_local(dt)
            return dt.strftime("%d/%m/%y:%H%M")
        if hasattr(ts, "isoformat"):
            # datetime object
            if getattr(ts, "tzinfo", None) is not None:
                ts = utc_to_local(ts)
            return ts.strftime("%d/%m/%y:%H%M")
    except Exception:
        pass
    return str(ts or "")


def _relative_age_marker(ts: Any, now: datetime | None = None) -> str:
    """Return a compact relative-age annotation (e.g. ``[3 hours earlier]``).

    Used to make temporal distance model-visible in conversation history: an
    absolute timestamp like ``[09/08/26:0218]`` is hard for a small model to
    translate into "3 hours ago", so beats/outreach treat an hours-old thread
    as live (CHANGELOG 2026-07-05 staleness issue). The marker is only emitted
    for entries older than ``HISTORY_AGE_MARKER_MINUTES`` (default 10); fresh
    entries get ``""`` so live conversation stays uncluttered. ``0`` disables.

    Args:
        ts:  Raw timestamp (ISO string or datetime) as stored in the cache.
        now: Optional "now" reference for deterministic tests; defaults to UTC.

    Returns:
        A bracketed marker like ``"[3 hours earlier]"``, or ``""`` when the
        entry is fresh, the timestamp is unusable, or markers are disabled.
    """
    threshold_min = _get_int("HISTORY_AGE_MARKER_MINUTES", 10)
    if threshold_min <= 0:
        return ""
    try:
        if isinstance(ts, str):
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        elif hasattr(ts, "isoformat"):
            dt = ts
        else:
            return ""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = now or datetime.now(timezone.utc)
        age_s = (now - dt).total_seconds()
        if age_s < threshold_min * 60:
            return ""
        if age_s < 3600:
            minutes = max(1, int(age_s // 60))
            unit = "minute" if minutes == 1 else "minutes"
            return f"[{minutes} {unit} earlier]"
        if age_s < 86400:
            hours = max(1, int(age_s // 3600))
            unit = "hour" if hours == 1 else "hours"
            return f"[{hours} {unit} earlier]"
        days = max(1, int(age_s // 86400))
        unit = "day" if days == 1 else "days"
        return f"[{days} {unit} earlier]"
    except Exception:
        return ""


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
    ts = entry.get("timestamp") or entry.get("date") or entry.get("created_at") or ""

    # Diary-like dicts: inject only the interaction summary, never the raw
    # personal thought.
    if "interaction_summary" in entry or "personal_thought" in entry:
        summary = entry.get("interaction_summary") or ""
        body = f"summary: {summary}" if summary else (text or "")
        return f"[diary {_format_ts(ts)}] {body}".strip()

    # Reply context annotation (40-char truncation for LLM readability)
    reply_suffix = ""
    meta = entry.get("metadata")
    if isinstance(meta, dict):
        reply_to = meta.get("reply_to")
        if isinstance(reply_to, dict):
            reply_sender = reply_to.get("sender_name") or "Unknown"
            reply_text = str(reply_to.get("text") or "")
            if len(reply_text) > 40:
                reply_text = reply_text[:40] + "\u2026"
            reply_text_safe = reply_text.replace('"', "'")
            reply_suffix = f' [replied to {reply_sender}: "{reply_text_safe}"]'

    safe_text = str(text).replace('"', "'")
    age_marker = _relative_age_marker(ts)
    if age_marker:
        safe_text = f"{age_marker} {safe_text}"
    return f'[{_format_ts(ts)}] {sender}{reply_suffix}: "{safe_text}"'.strip()


def telegram_chat_kind(path: str) -> str | None:
    """Return ``"group"`` or ``"dm"`` for a telegram_bot interface_path.

    Telegram chat IDs are negative for groups/supergroups and positive for
    private chats -- a reliable signal that's already on every interface_path,
    with no need to track chat titles (nothing in the codebase populates
    those). Returns ``None`` for non-Telegram interfaces or an unparseable
    chat id, where this convention doesn't apply.
    """
    parts = path.split("/")
    if len(parts) >= 2 and parts[0] == "telegram_bot":
        try:
            chat_id = int(parts[1])
        except ValueError:
            return None
        return "group" if chat_id < 0 else "dm"
    return None


def _friendly_interface_label(path: str) -> str:
    """Best-effort human-readable label for a cross-chat source path.

    Falls back to the raw path when :func:`telegram_chat_kind` can't
    classify it (non-Telegram interfaces, unparseable chat id).
    """
    kind = telegram_chat_kind(path)
    if kind == "group":
        return "the group chat"
    if kind == "dm":
        return "your DM"
    return path


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
    pretty = entry.get("interface_path_pretty")
    if pretty:
        return str(pretty)
    return _friendly_interface_label(str(entry_path))


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


# Matches a run of digits (optionally with a decimal part). Used only to derive
# a language-agnostic *shape* for a history line by masking the variable numeric
# parts (coordinates, quantities); it never inspects any word.
_NUMBER_RE = re.compile(r"\d+(?:\.\d+)?")


def _compact_similar_lines(lines: Sequence[str], max_items: int) -> List[str]:
    """Collapse near-identical history lines, with counts and summed quantities.

    A game world emits highly repetitive perceptions ("took damage",
    "collected 1 sand", "collected 1 sand", …). Kept verbatim they waste prompt
    budget and drown the useful signal. This helper groups lines by their
    *structural shape* — the line with every numeric run masked out — so lines
    that differ only in coordinates/quantities merge into one. Detection is
    purely structural (digit masking), never keyword/language matching, so it is
    multi-language safe.

    For each group we append a ``(×N)`` occurrence count when it repeated, and
    when every member carries exactly one number we also sum those numbers and
    surface the total (e.g. ``collected sand (×5, total 23)``). First-appearance
    order is preserved and the result is capped at ``max_items`` (keeping the
    most recent groups when the cap bites).

    Args:
        lines: History lines in chronological (insertion) order.
        max_items: Maximum number of compacted lines to return (<=0 → no cap).

    Returns:
        The compacted, order-preserving list of lines.
    """
    order: List[str] = []
    samples: Dict[str, str] = {}
    counts: Dict[str, int] = {}
    single_number: Dict[str, bool] = {}
    totals: Dict[str, float] = {}
    for raw in lines:
        line = (raw or "").strip()
        if not line:
            continue
        nums = _NUMBER_RE.findall(line)
        shape = _NUMBER_RE.sub("#", line)
        key = " ".join(shape.split()).lower()
        if key not in counts:
            samples[key] = line
            counts[key] = 0
            single_number[key] = len(nums) == 1
            totals[key] = 0.0
            order.append(key)
        counts[key] += 1
        # Only maintain a running sum while every seen member has exactly one
        # number; a single non-conforming member disables the total for safety.
        if len(nums) == 1 and single_number[key]:
            try:
                totals[key] += float(nums[0])
            except (TypeError, ValueError):
                single_number[key] = False
        elif len(nums) != 1:
            single_number[key] = False

    if max_items > 0 and len(order) > max_items:
        order = order[-max_items:]

    compacted: List[str] = []
    for key in order:
        text = samples[key]
        count = counts[key]
        if count <= 1:
            compacted.append(text)
            continue
        if single_number[key]:
            total = float(totals[key])
            total_str = str(int(total)) if total.is_integer() else f"{total:g}"
            compacted.append(f"{text} (×{count}, total {total_str})")
        else:
            compacted.append(f"{text} (×{count})")
    return compacted


def _is_vessel_autonomous_perception(entry: HistoryEntry) -> bool:
    """Return True for one of Synth's *own* synthetic vessel perceptions.

    A game world streams autonomous perceptions (sightings, movement, will
    beats, …) far faster than a slow LLM consumes them, so an unbounded stream
    of them floods the current-chat history and frames every reactive turn as
    solitary wandering — burying a player's actual question. Those perceptions
    are tagged at persistence time (``interface/vessel_interface.py``) with
    ``metadata.vessel_perception``. A real in-world player chat is deliberately
    left untagged. Detection is purely structural (a persisted flag), never
    keyword matching (project rule: multi-language safe).
    """
    if not isinstance(entry, dict):
        return False
    metadata = entry.get("metadata")
    return bool(isinstance(metadata, dict) and metadata.get("vessel_perception"))


def _is_vessel_beat_perception(entry: HistoryEntry) -> bool:
    """Return True for one of Synth's *own* autonomous vessel **beat** turns.

    Will beats and action beats are enqueued as perceptions (so they never
    evict player chat from the conversational deque), but their persisted text
    is the *first-person self-instruction prompt* — e.g. "this is a private
    moment, no one is addressing you, do NOT speak, return no ``say`` action".
    That framing is meant only for the beat's own solitary cognition turn; if it
    leaks into a **reactive** player-chat turn's context it directly suppresses
    the reply (the model obeys "do NOT speak" instead of answering the player).

    Unlike genuine world-grounding perceptions (sightings, movement, damage,
    status) — which are legitimate ambient context — beat perceptions must never
    be merged back into a reactive turn. Detection is purely structural: the
    persisted ``metadata.vessel_event_type`` ends with ``_beat``
    (``will_beat`` / ``action_beat`` / any future ``*_beat``). Never keyword
    matching on the text (project rule: multi-language safe).
    """
    if not isinstance(entry, dict):
        return False
    metadata = entry.get("metadata")
    if not isinstance(metadata, dict):
        return False
    event_type = metadata.get("vessel_event_type")
    return bool(isinstance(event_type, str) and event_type.endswith("_beat"))


def _is_ignored_prompt_history_entry(entry: HistoryEntry) -> bool:
    if not isinstance(entry, dict):
        return False

    metadata = entry.get("metadata")
    if isinstance(metadata, dict) and metadata.get("skip_history"):
        return True

    sender = str(entry.get("sender_name") or entry.get("username") or "").lower()
    text = str(
        entry.get("text") or entry.get("message_text") or entry.get("content") or ""
    )
    # Chat-like entries with NO text (media without a caption, empty placeholder
    # rows) carry zero signal for the model and would render as a blank
    # '[ts] Sender: ""' line, which `_history_to_turns` converts into an
    # empty-content user/assistant turn in the provider messages array
    # (observed as blank blocks in Langfuse traces). Skip them so the prompt
    # only ever contains lines that say something. Diary-like dicts
    # (interaction_summary/personal_thought) are exempt — they are rendered by
    # the diary branch of `_entry_to_text`, never as chat lines.
    if not text.strip() and not (
        entry.get("interaction_summary") or entry.get("personal_thought")
    ):
        return True
    if sender != "self":
        return False

    return text.startswith(
        (
            "✅ Cortex engine dynamically updated to `",
            "✅ Cortex engine override for grillo updated to `",
            "✅ Cortex engine override for trainer updated to `",
            "❌ Failed to switch Cortex to `",
        )
    )


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
        history_scope: str | None = None,
    ) -> Dict[str, Any]:
        # Internal beats (diary consolidation, grillo, etc.) set skip_history=True
        # in context_memory to avoid loading irrelevant chat history into the prompt.
        if isinstance(context_memory, dict) and context_memory.get("skip_history"):
            log_debug(
                "[history_engine] skip_history flag set — returning empty context"
            )
            return {"history_current_chat": [], "history_recent": [], "thoughts": []}

        lite_mode = _get_bool("PROMPT_LITE_MODE", False)

        verbosity = max(0, _get_int("CONTEXT_VERBOSITY", 10))
        thoughts_limit = max(0, _get_int("THOUGHTS_LIMIT", 5))

        # In lite mode, the dedicated lite-mode limit is authoritative -- it's
        # the WebUI-exposed dial right next to the Lite Mode toggle, and
        # min()-ing it against the general CONTEXT_VERBOSITY dial meant raising
        # it above CONTEXT_VERBOSITY silently had no effect (the two dials
        # look independent in the UI but weren't).
        if lite_mode:
            verbosity = max(0, _get_int("LITE_MODE_HISTORY_LIMIT", 3))
            thoughts_limit = min(thoughts_limit, 2)

        enable_current = _get_bool("ENABLE_HISTORY_CURRENT_CHAT", True)
        enable_recent = _get_bool("ENABLE_HISTORY_RECENT", True)
        enable_diary = _get_bool("ENABLE_AI_DIARY", True)
        enable_memories = _get_bool("ENABLE_MEMORIES", True)
        enable_thoughts = _get_bool("ENABLE_THOUGHTS", True)
        enable_tags_placeholder = _get_bool("ENABLE_TAGS_PLACEHOLDER", True)
        diary_full = _get_bool("AI_DIARY_FULL", True)
        unified_mode = _get_bool("UNIFIED_HISTORY", True)

        # Allow callers to pass a per-prompt history_scope via argument or context_memory dict
        if history_scope is None and isinstance(context_memory, dict):
            try:
                history_scope = context_memory.get("history_scope")
            except Exception:
                history_scope = None

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

        # === RIFT VESSEL FOCUS: scope context to the world during embodiment ===
        # When the current turn originates from a Vessel embodiment (SyntH is
        # "in the world"), it concentrates there like a real person: it must NOT
        # pull in the whole cross-interface history / global diary / global
        # memory. We detect vessel focus purely from routing metadata — the
        # source interface_path (``vessel/...``), the message's chat type
        # (``vessel``) or an explicit ``vessel_focus`` context flag — never from
        # message text (project rule: no keyword logic). On focus we force
        # ``unified_mode = False`` so only the local vessel history is kept, and
        # we suppress the global diary/memory blocks below. Fully guarded: any
        # failure leaves the normal context path untouched.
        from core.vessel_focus import is_vessel_turn

        vessel_focus = is_vessel_turn(message, context_memory, interface_path)
        if vessel_focus:
            unified_mode = False
            enable_diary = False
            enable_memories = False
            # The rolling cross-interface "recent chats" block is global noise
            # during embodiment: SyntH concentrates on the world, so we also
            # suppress it here (Bug B — it otherwise polluted the vessel prompt
            # via the enable_recent path below).
            enable_recent = False
            log_debug(
                "[history_engine] Vessel focus active — scoping context to the "
                "world (unified history + global diary/memory/recent suppressed)"
            )

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
        # Distinct interface_paths that actually contributed history lines. Used
        # by the prompt engine to render a compact pretty-name legend so the
        # model can map each "[from ...]" source back to a routable path.
        used_interface_paths: set[str] = set()

        def _note_path(entry: Any) -> None:
            if not isinstance(entry, dict):
                return
            p = entry.get("interface_path") or entry.get("source_path")
            if p:
                used_interface_paths.add(str(p))

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

                        cached = await cache_load(
                            interface_path,
                            match_chat_level=(
                                not vessel_focus
                                and not is_vessel_interface_path(interface_path)
                            ),
                        )
                        combined = list(msgs) + list(cached)
                        msgs = combined[-verbosity:]
                    except Exception as e:
                        log_debug(
                            f"[history_engine] Could not load cached messages for current chat: {e}"
                        )

                # If this is a live voice interface and live-history syncing is enabled,
                # include global cross-interface history as well so the prompt sees both
                # the local voice/chat stream and any unrelated messages from the
                # same guild/user context.  This mirrors the behaviour of the normal
                # prompt path and guards against stale data if the periodic sync task
                # missed something.
                if interface_path and interface_path.startswith("discord_live_"):
                    try:
                        from core.config_manager import config_registry as _cfg

                        if _cfg.get_value(
                            "LIVE_SYNC_CHAT_HISTORY", True, value_type=bool
                        ):
                            from core.chat_history_cache import load_global_chat_history

                            global_hist = await load_global_chat_history(
                                limit=verbosity * 5 if verbosity > 0 else 100
                            )
                            if vessel_focus:
                                global_hist = []
                            else:
                                global_hist = [
                                    m
                                    for m in global_hist
                                    if not is_vessel_history_entry(m)
                                ]
                            # merge and sort by timestamp so chronology is preserved
                            combined = list(msgs) + list(global_hist)

                            def _sort_key(m: Any) -> str:
                                return str(m.get("timestamp", ""))

                            combined.sort(key=_sort_key)
                            msgs = combined
                    except Exception as _e:
                        log_debug(f"[history_engine] live merge skipped: {_e}")

                window = msgs[-verbosity:] if verbosity > 0 else []

                # Vessel focus: merge conversation with a *bounded* number of
                # Synth's own autonomous perceptions. Perceptions live in a
                # SEPARATE in-memory ring buffer (see chat_context_manager) so a
                # burst of world perceptions (e.g. repeated environmental damage
                # while drowning) can never evict a player's chat from the
                # bounded conversational deque. A world emits
                # sightings/movement/will-beats far faster than a slow LLM
                # consumes them; left unbounded they dominate the current-chat
                # context and frame every turn as solitary wandering, so the
                # model ignores the player's actual question and re-emits a stock
                # reflective line. We keep ALL conversational lines (player chats
                # + Synth's own replies) and only the most recent
                # ``VESSEL_PERCEPTION_CONTEXT_CAP`` perceptions for ambient
                # grounding. Structural (persisted metadata flag), never keyword.
                compacted_perception_lines: List[str] = []
                if vessel_focus:
                    # Drop any stray perceptions still in the conversational
                    # window (older messages persisted before the split) so they
                    # do not double-count against the conversational budget.
                    window = [
                        m for m in window if not _is_vessel_autonomous_perception(m)
                    ]
                    # Compact-then-cap. A world emits highly repetitive
                    # perceptions ("took damage", "collected 1 sand", …). Rather
                    # than keeping only a tiny raw tail (the old
                    # ``VESSEL_PERCEPTION_CONTEXT_CAP`` = 3 grounding lines), we
                    # read the *whole* recent ring, collapse near-identical lines
                    # into counted/summed rollups (e.g. "took damage (×5)",
                    # "collected sand (×5, total 23)"), then cap the number of
                    # distinct rollups at ``VESSEL_PERCEPTION_COMPACT_MAX``
                    # (default 20). This keeps far richer ambient grounding in a
                    # much smaller prompt budget. Compaction is structural
                    # (digit-masking), never keyword matching (multi-language
                    # safe).
                    compact_max = max(0, _get_int("VESSEL_PERCEPTION_COMPACT_MAX", 20))
                    if compact_max > 0:
                        try:
                            from core.chat_context_manager import (
                                get_perception_memory as _get_perception_memory,
                            )

                            pmap = _get_perception_memory()
                            pbuf = (
                                pmap.get(interface_path)
                                if isinstance(pmap, dict)
                                else None
                            )
                            if pbuf:
                                # Keep only genuine world-grounding perceptions
                                # (sightings/movement/damage/status). Exclude
                                # will/action **beat** turns: their persisted
                                # text is a solitary self-instruction ("do NOT
                                # speak, return no say action") that suppresses
                                # the reply when re-injected into a reactive
                                # player-chat turn. Structural filter on the
                                # persisted event type, never keyword matching.
                                grounding = [
                                    m for m in pbuf if not _is_vessel_beat_perception(m)
                                ]
                                perception_texts = [
                                    _entry_to_text_with_source(
                                        m, current_interface_path=interface_path
                                    )
                                    for m in grounding
                                ]
                                compacted_perception_lines = _compact_similar_lines(
                                    perception_texts, compact_max
                                )
                        except Exception as _pe:
                            log_debug(
                                f"[history_engine] Could not read vessel perceptions: {_pe}"
                            )
                    # Order: ambient grounding perceptions FIRST, then the
                    # conversation. Perceptions are background scene-setting, not
                    # part of the conversational thread, so they must never be
                    # the *last* line the model reads: on a reactive player-chat
                    # turn the player's question is the last conversational entry
                    # and MUST stay last, or a weak embodiment model continues
                    # its own autonomous pattern (mining/observe) instead of
                    # answering. The compacted perception lines are emitted ahead
                    # of the conversation ``window`` below.

                # Emit the compacted ambient perceptions first (already plain
                # text), then the conversational window.
                for pline in compacted_perception_lines:
                    k = _dedup_key(pline)
                    if k in seen_history:
                        continue
                    history_current_chat.append(pline)
                    seen_history.add(k)

                for m in window:
                    if _is_ignored_prompt_history_entry(m):
                        continue
                    line = _entry_to_text_with_source(
                        m, current_interface_path=interface_path
                    )
                    k = _dedup_key(line)
                    if k in seen_history:
                        continue
                    history_current_chat.append(line)
                    _note_path(m)
                    seen_history.add(k)
            except Exception as e:
                log_debug(f"[history_engine] Failed building history_current_chat: {e}")

        if unified_mode:
            try:
                unified_candidates: List[dict] = []

                for m in msgs if enable_current and interface_path else []:
                    if not isinstance(m, dict):
                        continue
                    if not m.get("interface_path") and not m.get("source_path"):
                        unified_candidates.append(
                            {**m, "interface_path": interface_path}
                        )
                    else:
                        unified_candidates.append(m)

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

                # Vessel conversations are a private embodiment context, not
                # ordinary cross-interface memory.  They remain available on a
                # Vessel turn through the local path, but must not enter a
                # non-Vessel unified prompt merely because their rows are
                # recent in the durable cache.
                if not vessel_focus:
                    unified_candidates = [
                        m for m in unified_candidates if not is_vessel_history_entry(m)
                    ]
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
                            for m in list(q):
                                if not isinstance(m, dict):
                                    unified_candidates.append(m)
                                    continue

                                if (
                                    isinstance(k, str)
                                    and k
                                    and not m.get("interface_path")
                                    and not m.get("source_path")
                                ):
                                    if not vessel_focus and is_vessel_interface_path(k):
                                        continue
                                    unified_candidates.append(
                                        {**m, "interface_path": k}
                                    )
                                else:
                                    if not vessel_focus and (
                                        is_vessel_interface_path(k)
                                        or is_vessel_history_entry(m)
                                    ):
                                        continue
                                    unified_candidates.append(m)

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
                    # Queste tipicamente appaiono su '.../-1' come monologhi di sistema.
                    def _is_internal_noise(m: dict) -> bool:
                        if _is_ignored_prompt_history_entry(m):
                            return True

                        path = str(m.get("interface_path", "") or "")
                        if not path.endswith("/-1"):
                            return False

                        txt = m.get("text") or m.get("message_text") or ""
                        if not txt:
                            return False

                        # Euristiche per monologhi di sistema non chat
                        if "G.R.I.L.L.O." in txt:
                            return True
                        if txt.startswith("Pattern analysis"):
                            return True
                        if txt.startswith("Reflecting on"):
                            return True
                        if txt.startswith("Recent cycles"):
                            return True
                        if txt.startswith("Analysis of recent"):
                            return True
                        if txt.startswith("Recent interaction patterns"):
                            return True
                        if txt.startswith("Relationship Reflection:"):
                            return True
                        sender = str(m.get("sender_name", "") or "")
                        if sender.lower() == "self":
                            return True
                        return False

                    unified_candidates = [
                        m for m in unified_candidates if not _is_internal_noise(m)
                    ]

                # Build separate lists for local vs other chat entries while preserving
                # the original dedup behavior used for the unified view.
                local_lines: List[str] = []
                other_lines: List[str] = []
                seen_history = set()

                # Filter out the current inbound message to avoid duplication
                input_text = (text or "").strip()

                # LOG: mostra quanti messaggi e da quali path
                log_debug(
                    f"[history_engine] Unified candidates totali: {len(unified_candidates)}"
                )
                path_counter = {}
                for m in unified_candidates:
                    ipath = m.get("interface_path")
                    if ipath:
                        path_counter[ipath] = path_counter.get(ipath, 0) + 1
                log_debug(
                    f"[history_engine] Messaggi per interface_path: {path_counter}"
                )

                for m in unified_candidates:
                    # Skip if this entry's text matches the current input
                    entry_text = (
                        (m.get("text") or m.get("message_text") or "").strip()
                        if isinstance(m, dict)
                        else ""
                    )
                    if input_text and entry_text == input_text:
                        continue

                    line = _entry_to_text_with_source(
                        m, current_interface_path=interface_path
                    )
                    k = _dedup_key(line)
                    if k in seen_history:
                        continue

                    # Determine origin: prefer explicit interface_path/source_path fields
                    entry_path = m.get("interface_path") or m.get("source_path") or ""
                    if (
                        entry_path
                        and interface_path
                        and str(entry_path) == str(interface_path)
                    ):
                        local_lines.append(line)
                    else:
                        other_lines.append(line)

                    _note_path(m)
                    seen_history.add(k)

                if verbosity > 0:
                    local_lines = local_lines[-verbosity:]
                    other_lines = other_lines[-verbosity:]

                log_debug(
                    f"[history_engine] Messaggi globali passati al prompt: {len(local_lines) + len(other_lines)} (locali: {len(local_lines)}, altri: {len(other_lines)})"
                )

                # Keep the active chat stream isolated from cross-chat context.
                # Older unrelated messages belong in `history_recent`, not inside
                # `history_current_chat`, otherwise the model can anchor on stale
                # external context as if it were part of the active thread.
                history_current_chat = local_lines
                # Ensure `history_recent` (global) contains ONLY other chats
                history_recent = other_lines
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
                                if not vessel_focus and (
                                    is_vessel_interface_path(ip)
                                    or is_vessel_history_entry(m)
                                ):
                                    continue
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
                    if _is_ignored_prompt_history_entry(m):
                        continue
                    line = _entry_to_text_with_source(
                        m, current_interface_path=interface_path
                    )
                    k = _dedup_key(line)
                    if k in seen_history:
                        continue
                    history_recent.append(line)
                    _note_path(m)
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
                    if not vessel_focus and is_vessel_history_entry(raw):
                        continue
                    if _is_ignored_prompt_history_entry(raw):
                        continue
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
                    _note_path(raw)
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
                    # Cap thought length — the merged daily blob can be 30k+ chars.
                    # We keep the most recent 800 chars (tail) since entries are
                    # appended chronologically and the latest thought is at the end.
                    _MAX_THOUGHT = 800
                    if len(thought) > _MAX_THOUGHT:
                        thought = "\u2026" + thought[-_MAX_THOUGHT:]
                    thoughts.append(f"[thought {ts}] {thought}".strip())
                if len(thoughts) >= thoughts_limit:
                    break

        # Core-provided memories (already searched by prompt engine)
        out_memories: List[Any] = []
        if enable_memories and memories:
            mem_limit = min(verbosity, 2) if lite_mode else verbosity
            out_memories = list(memories)[:mem_limit] if mem_limit > 0 else []

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

        # NOTE: local_history / global_history used to be unconditional aliases
        # of history_current_chat / history_recent.  They are *always* identical
        # to the canonical keys, so including them just doubles token usage.
        # The ``history_scope`` field below already tells the LLM which stream
        # is primary — no alias keys are needed.

        # Echo the requested history_scope (if any) so downstream systems can
        # treat one stream as 'primary' while still seeing the other.
        try:
            if history_scope:
                context["history_scope"] = history_scope
        except Exception:
            pass

        if enable_memories:
            context["memories"] = out_memories

        if enable_tags_placeholder and not lite_mode:
            context["tags_placeholder"] = []

        # Distinct interface_paths present in the assembled history (excluding the
        # current chat, which the model already knows). The prompt engine turns
        # these into a compact pretty-name legend.
        if interface_path:
            used_interface_paths.discard(str(interface_path))
        if used_interface_paths:
            context["history_interface_paths"] = sorted(used_interface_paths)

        return context
