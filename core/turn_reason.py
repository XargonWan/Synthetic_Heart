"""What drove a turn ("why did I say that"): a compact structural summary.

``build_reason_summary`` is a pure, side-effect-free helper. ``core/prompt_engine``
attaches its result to the prompt dict under ``__reason_trail`` (popped before the engine
sees it), and the turn's card (``core/turn_cards``) shows it as the *memories* and
*emotions* sections, so the old Reason Trail log, its table and its WebUI tab are gone.

Design rules (per AGENTS.md): **structural**, never semantic. The recorded fields are the
already-resolved context of a turn (memory source/id, diary source ids, dominant emotion,
the active vessel goal, the beat type, the effective history scope); there is no keyword
or intent detection anywhere in this module.
"""

from __future__ import annotations

from typing import Any


def build_reason_summary(
    memories: Any,
    diary_entries: Any,
    emotion: Any,
    beat_type: Any,
    history_scope: Any,
    goal: Any,
) -> dict[str, Any]:
    """Build a compact structural summary of what drove a turn.

    Pure and side-effect-free: reads only the passed values and returns a plain
    JSON-safe dict. None of the inputs are interpreted semantically — this is a
    structural redaction of already-resolved context fields.
    """
    return {
        "memories": _compact_memories(memories),
        "diary_sources": _compact_diary_sources(diary_entries),
        "emotion": _compact_emotion(emotion),
        "goal": _compact_goal(goal),
        "beat_type": str(beat_type).strip() if beat_type else None,
        "history_scope": str(history_scope).strip() if history_scope else None,
    }


def _compact_memories(memories: Any) -> list[dict[str, Any]]:
    if not isinstance(memories, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for mem in memories:
        if not isinstance(mem, dict):
            continue
        item: dict[str, Any] = {}
        for key in ("source", "id"):
            if mem.get(key) is not None:
                item[key] = mem[key]
        snippet = mem.get("snippet") or mem.get("content") or mem.get("text")
        if snippet:
            item["snippet"] = str(snippet)[:200]
        if item:
            result.append(item)
    return result


def _compact_diary_sources(diary_entries: Any) -> list[dict[str, Any]]:
    if not isinstance(diary_entries, (list, tuple)):
        return []
    result: list[dict[str, Any]] = []
    for entry in diary_entries:
        if not isinstance(entry, dict):
            continue
        item: dict[str, Any] = {}
        for key in ("id", "created_at", "interface", "chat_id"):
            if entry.get(key) is not None:
                item[key] = entry[key]
        if item:
            result.append(item)
    return result


def _compact_emotion(emotion: Any) -> str | None:
    if emotion is None:
        return None
    if isinstance(emotion, dict) and emotion:
        parts = [f"{name}:{value}" for name, value in emotion.items()]
        joined = ", ".join(parts).strip()
        return joined[:255] if joined else None
    text = str(emotion).strip()
    return text[:255] if text else None


def _compact_goal(goal: Any) -> dict[str, Any] | None:
    if not isinstance(goal, dict):
        return None
    item: dict[str, Any] = {}
    for key in (
        "id",
        "description",
        "note",
        "target_kind",
        "target_name",
        "status",
        "current_step",
    ):
        if goal.get(key) is not None:
            item[key] = goal[key]
    return item or None


# ---------------------------------------------------------------------------
# Sanitization + normalization
# ---------------------------------------------------------------------------
