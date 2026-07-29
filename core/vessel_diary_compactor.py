"""Rift Vessel diary compactor — chunked, LLM-driven lived-experience summary.

At end-of-session the Vessel no longer writes to the real ``ai_diary`` (that
polluted every non-vessel Fast-Lane prompt with an ever-growing shared daily
row). Instead the session's buffered lived experience is compacted here, in
**chunks**, into a single autobiographical entry stored in the dedicated
``vessel_diary`` table.

Why chunks? A long embodiment session accumulates hundreds of buffered moments
(perceptions, actions, sightings). Feeding them all to the LLM in one shot
overruns the context and fails — exactly the failure this whole change is
fixing. So we:

1. Split the buffer into chunks (by item-count and char-budget, whichever hits
   first).
2. Summarise each chunk into a short first-person "lived experience" partial.
3. Fold the partials into one coherent entry (recursing if the fold itself is
   still too large).

Everything is best-effort and fail-safe: any LLM error falls back to a
deterministic plain-text join, so session teardown can never break. This module
is deliberately free of any Agent-Lane / Drone dependency (Vessel Fast-Lane
constraint) and runs off the hot path (launched as a background task by the
session manager).

.. note::
   Whether — and how — to import these ``vessel_diary`` entries into the real
   ``ai_diary`` is a deliberate, **unimplemented** decision (see the TODO in
   :func:`compact_session`). It will be revisited once we can measure how large
   a well-formed vessel diary actually is.
"""

from __future__ import annotations

from typing import Any

from core.db import get_conn_ctx
from core.json_utils import extract_json_from_text
from core.logging_utils import log_debug, log_error, log_info, log_warning

__all__ = [
    "compact_session",
    "save_vessel_diary",
]

# Per-item truncation so one runaway summary cannot dominate a chunk. Mirrors
# the grillo compactor's 1200-char clamp.
_ITEM_MAX_CHARS = 1200

# Config defaults / clamps for chunk sizing. Overridable via the config
# registry keys registered by plugins/rift_vessel/vessel_plugin.py.
_DEFAULT_CHUNK_ITEMS = 40
_MIN_CHUNK_ITEMS = 4
_MAX_CHUNK_ITEMS = 400

_DEFAULT_CHUNK_CHARS = 6000
_MIN_CHUNK_CHARS = 1000
_MAX_CHUNK_CHARS = 40000

# Safety belt against pathological fold recursion.
_MAX_FOLD_DEPTH = 4


def _safe_int(value: Any, default: int, lo: int, hi: int) -> int:
    """Coerce ``value`` to an int clamped to ``[lo, hi]`` (fail-safe)."""
    try:
        n = int(value)
    except Exception:
        return default
    if n < lo:
        return lo
    if n > hi:
        return hi
    return n


def _resolve_chunk_config() -> tuple[int, int]:
    """Return ``(chunk_items, chunk_chars)`` from the config registry (clamped)."""
    try:
        from core.config_manager import config_registry
    except Exception:
        return _DEFAULT_CHUNK_ITEMS, _DEFAULT_CHUNK_CHARS
    items = _safe_int(
        config_registry.get_value("VESSEL_DIARY_CHUNK_ITEMS", _DEFAULT_CHUNK_ITEMS),
        _DEFAULT_CHUNK_ITEMS,
        _MIN_CHUNK_ITEMS,
        _MAX_CHUNK_ITEMS,
    )
    chars = _safe_int(
        config_registry.get_value("VESSEL_DIARY_CHUNK_CHARS", _DEFAULT_CHUNK_CHARS),
        _DEFAULT_CHUNK_CHARS,
        _MIN_CHUNK_CHARS,
        _MAX_CHUNK_CHARS,
    )
    return items, chars


def _buffer_to_lines(buffer: list[dict[str, Any]]) -> list[str]:
    """Render buffered experience items into compact, truncated text lines.

    Each item is ``{event_type, summary, data, at}``; we keep only the
    human-readable ``event_type`` + ``summary`` (the structured ``data`` is for
    the WebUI audit trail, not the autobiographical narrative). Empty summaries
    are skipped.
    """
    lines: list[str] = []
    for item in buffer:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or "").strip()
        if not summary:
            continue
        event_type = str(item.get("event_type") or "").strip()
        text = f"[{event_type}] {summary}" if event_type else summary
        if len(text) > _ITEM_MAX_CHARS:
            text = text[:_ITEM_MAX_CHARS]
        lines.append(text)
    return lines


def _chunk_lines(
    lines: list[str], chunk_items: int, chunk_chars: int
) -> list[list[str]]:
    """Split ``lines`` into chunks bounded by item-count and char-budget.

    A chunk is closed as soon as EITHER limit would be exceeded by the next
    line, whichever comes first. A single oversized line still forms its own
    (one-line) chunk rather than being dropped.
    """
    chunks: list[list[str]] = []
    current: list[str] = []
    current_chars = 0
    for line in lines:
        line_len = len(line) + 1  # +1 for the newline join
        would_overflow_items = len(current) >= chunk_items
        would_overflow_chars = current and (current_chars + line_len) > chunk_chars
        if would_overflow_items or would_overflow_chars:
            chunks.append(current)
            current = []
            current_chars = 0
        current.append(line)
        current_chars += line_len
    if current:
        chunks.append(current)
    return chunks


async def _resolve_engine() -> Any | None:
    """Resolve the vessel-scope Cortex engine (fail-safe).

    Mirrors the grillo compactor's resolution, scoped to ``"vessel"`` so it
    honours ``VESSEL_CORTEX`` (``Default`` = Base Cortex). Returns ``None`` if
    no engine can be loaded.
    """
    try:
        from core.config import get_active_cortex_engine
        from core.cortex_registry import get_cortex_registry
    except Exception as exc:
        log_debug(f"[vessel_diary] cortex imports unavailable: {exc}")
        return None
    try:
        active_cortex = await get_active_cortex_engine(scope="vessel")
    except Exception as exc:
        log_debug(f"[vessel_diary] get_active_cortex_engine failed: {exc}")
        return None
    registry = get_cortex_registry()
    engine = registry.get_engine(active_cortex)
    if engine is None:
        try:
            engine = registry.load_engine(active_cortex)
        except Exception as exc:
            log_error(
                f"[vessel_diary] could not load Cortex engine '{active_cortex}': {exc}"
            )
            return None
    return engine


def _persona_hint() -> tuple[str, str]:
    """Return ``(name, profile)`` for the persona, best-effort."""
    name = "you"
    profile = ""
    try:
        from core.config_manager import config_registry

        name = str(config_registry.get_value("SYNTH_NAME", "you") or "you")
        profile = str(config_registry.get_value("SYNTH_PROFILE", "") or "")
    except Exception:
        pass
    return name, profile


def _build_chunk_prompt(
    name: str,
    profile: str,
    environment: str,
    lines: list[str],
    part_no: int,
    part_total: int,
) -> dict[str, Any]:
    """Build the structured prompt to summarise ONE chunk into a partial.

    First-person, persona-aware, keyword-free. The chunk is delivered as
    structured payload data, not interpolated free text, so no phrase matching
    is involved.
    """
    persona = f"You are {name}." + (f" {profile}" if profile else "")
    instructions = (
        f"{persona}\n"
        f"Below is part {part_no} of {part_total} of the raw log of moments you "
        f"lived while embodied in the world '{environment}'. Write a SHORT, "
        "first-person account of what you experienced in this part — what you "
        "saw, did, and felt — as if remembering it. Do NOT list events "
        "mechanically; narrate naturally. Do NOT invent anything not present in "
        "the moments. Keep it to a few sentences.\n"
        'Return ONLY a JSON object: {"partial": "<your first-person account>"}.'
    )
    return {
        "input": {
            "type": "vessel_diary_chunk",
            "payload": {"environment": environment, "moments": lines},
        },
        "context": {},
        "instructions": instructions,
    }


def _build_fold_prompt(
    name: str,
    profile: str,
    environment: str,
    partials: list[str],
    reason: str,
) -> dict[str, Any]:
    """Build the structured prompt to fold partials into one coherent entry."""
    persona = f"You are {name}." + (f" {profile}" if profile else "")
    instructions = (
        f"{persona}\n"
        "Below are several first-person fragments recalling, in order, a single "
        f"session you spent embodied in the world '{environment}' (the session "
        f"ended: {reason}). Weave them into ONE coherent, first-person diary "
        "entry — a lived-experience memory, natural and flowing, not a list. Do "
        "NOT invent anything beyond the fragments. Keep it concise.\n"
        'Return ONLY a JSON object: {"entry": "<your first-person diary entry>"}.'
    )
    return {
        "input": {
            "type": "vessel_diary_fold",
            "payload": {"environment": environment, "fragments": partials},
        },
        "context": {},
        "instructions": instructions,
    }


async def _generate_json(engine: Any, prompt: dict[str, Any], key: str) -> str | None:
    """Call the engine and extract ``key`` from its JSON response (fail-safe)."""
    try:
        raw = await engine.generate_response(prompt)
    except Exception as exc:
        log_warning(f"[vessel_diary] generate_response failed: {exc}")
        return None
    parsed = extract_json_from_text(raw)
    if isinstance(parsed, dict) and parsed.get(key):
        value = str(parsed.get(key)).strip()
        if value:
            return value
    log_debug(f"[vessel_diary] no '{key}' in LLM JSON response")
    return None


async def _fold_partials(
    engine: Any,
    name: str,
    profile: str,
    environment: str,
    partials: list[str],
    reason: str,
    chunk_chars: int,
    depth: int = 0,
) -> str:
    """Fold partials into one entry, recursing if the fold is still oversized."""
    if not partials:
        return ""
    if len(partials) == 1:
        return partials[0]

    joined_len = sum(len(p) for p in partials)
    if joined_len > chunk_chars and depth < _MAX_FOLD_DEPTH:
        # Too big to fold in one shot: fold in groups first (recurse).
        group_partials = _chunk_lines(partials, len(partials), chunk_chars)
        reduced: list[str] = []
        for group in group_partials:
            if len(group) == 1:
                reduced.append(group[0])
                continue
            prompt = _build_fold_prompt(name, profile, environment, group, reason)
            folded = await _generate_json(engine, prompt, "entry")
            reduced.append(folded if folded else "\n".join(group))
        return await _fold_partials(
            engine,
            name,
            profile,
            environment,
            reduced,
            reason,
            chunk_chars,
            depth + 1,
        )

    prompt = _build_fold_prompt(name, profile, environment, partials, reason)
    folded = await _generate_json(engine, prompt, "entry")
    return folded if folded else "\n\n".join(partials)


def _fallback_entry(environment: str, lines: list[str], reason: str) -> str:
    """Deterministic plain-text entry used when the LLM is unavailable."""
    body = "\n".join(f"- {line}" for line in lines)
    return f"Lived experience in {environment} (session ended: {reason}).\n{body}"


async def compact_session(
    session_id: str,
    environment: str,
    interface_path: str | None,
    buffer: list[dict[str, Any]],
    reason: str,
) -> str | None:
    """Compact a session's buffered lived experience into one diary entry.

    Returns the compacted first-person entry text, or ``None`` if the buffer was
    empty. Never raises — on any LLM failure it falls back to a deterministic
    plain-text join so session teardown is never blocked.

    .. todo::
       Decide whether/how to import the returned entry into the real
       ``ai_diary``. Deferred until we can measure typical entry size. For now
       the entry lives ONLY in ``vessel_diary`` (see :func:`save_vessel_diary`).
    """
    lines = _buffer_to_lines(buffer)
    if not lines:
        return None

    chunk_items, chunk_chars = _resolve_chunk_config()
    chunks = _chunk_lines(lines, chunk_items, chunk_chars)
    name, profile = _persona_hint()

    engine = await _resolve_engine()
    if engine is None:
        log_warning(
            "[vessel_diary] no Cortex engine available; using plain-text fallback"
        )
        return _fallback_entry(environment, lines, reason)

    partials: list[str] = []
    part_total = len(chunks)
    for idx, chunk in enumerate(chunks, start=1):
        prompt = _build_chunk_prompt(name, profile, environment, chunk, idx, part_total)
        partial = await _generate_json(engine, prompt, "partial")
        # On a per-chunk failure, degrade to the raw chunk text rather than
        # losing that slice of experience entirely.
        partials.append(partial if partial else "\n".join(chunk))

    entry = await _fold_partials(
        engine, name, profile, environment, partials, reason, chunk_chars
    )
    if not entry:
        return _fallback_entry(environment, lines, reason)
    log_info(
        f"[vessel_diary] compacted session {session_id} "
        f"({len(buffer)} moments, {part_total} chunks) into {len(entry)} chars"
    )
    return entry


async def save_vessel_diary(
    session_id: str,
    environment: str,
    interface_path: str | None,
    summary: str,
    moments_count: int,
    reason: str,
) -> int | None:
    """Insert one compacted entry into ``vessel_diary``. Returns its id or None."""
    if not summary:
        return None
    params = (
        session_id,
        interface_path,
        environment,
        summary,
        moments_count,
        reason,
    )
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                new_id: int | None = None
                try:
                    # Postgres path: RETURNING id yields the new row id.
                    await cur.execute(
                        "INSERT INTO vessel_diary "
                        "(session_id, interface_path, environment, summary, "
                        "moments_count, reason) VALUES (%s, %s, %s, %s, %s, %s) "
                        "RETURNING id",
                        params,
                    )
                    row = await cur.fetchone()
                    if row is not None:
                        new_id = int(row[0])
                except Exception:
                    # MariaDB / drivers without RETURNING support.
                    await cur.execute(
                        "INSERT INTO vessel_diary "
                        "(session_id, interface_path, environment, summary, "
                        "moments_count, reason) VALUES (%s, %s, %s, %s, %s, %s)",
                        params,
                    )
                    last = getattr(cur, "lastrowid", None)
                    new_id = int(last) if last else None
                await conn.commit()
                return new_id
    except Exception as exc:
        log_error(f"[vessel_diary] failed to save vessel_diary entry: {exc}")
        return None
