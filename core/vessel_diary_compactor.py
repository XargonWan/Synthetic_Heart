"""Rift Vessel diary compactor — chunked, LLM-driven end-of-session summary.

At end-of-session the Vessel does not write to the real ``ai_diary`` (that
polluted every non-vessel Fast-Lane prompt with an ever-growing shared daily
row). Instead the session is compacted here, in **chunks**, into a single entry
stored in the dedicated ``vessel_diary`` table.

Two compaction modes live in this module:

* :func:`compact_activity_recap` — the **operational recap** (the current
  end-of-session product). It reads the session's rows from
  ``vessel_activity_log`` and produces a factual, third-person recap of *what
  happened* — coordinates, quantities, world state, actions taken and their
  outcomes — with **no** first-person voice, personality or emotion. This is the
  mode the Rift Vessel Compactor plugin (``plugins/rift_vessel/vessel_compactor``)
  enqueues when a session reaches the ENDED state.
* :func:`compact_session` — the **legacy autobiographical** first-person diary,
  kept for backward compatibility / tests. It is no longer wired into the
  end-of-session path.

Why chunks? A long embodiment session accumulates hundreds of moments
(perceptions, actions, sightings). Feeding them all to the LLM in one shot
overruns the context and fails — exactly the failure this whole change is
fixing. So we:

1. Split the source into chunks (by item-count and char-budget, whichever hits
   first).
2. Summarise each chunk into a short partial.
3. Fold the partials into one coherent entry (recursing if the fold itself is
   still too large).

Everything is best-effort and fail-safe: any LLM error falls back to a
deterministic plain-text join, so session teardown can never break. This module
is deliberately free of any Agent-Lane / Drone dependency (Vessel Fast-Lane
constraint) and runs off the hot path.
"""

from __future__ import annotations

from typing import Any

from core.db import get_conn_ctx
from core.json_utils import extract_json_from_text
from core.logging_utils import log_debug, log_error, log_info, log_warning

__all__ = [
    "compact_session",
    "compact_activity_recap",
    "load_activity_lines",
    "save_vessel_diary",
]

# ``reason`` value stamped on the operational recap rows saved to
# ``vessel_diary`` by :func:`compact_activity_recap`, so the factual recap can be
# distinguished from any other diary entry sharing the table.
ACTIVITY_RECAP_REASON = "activity_recap"

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


# ---------------------------------------------------------------------------
# Operational recap (factual, third-person) — the end-of-session product.
#
# Distinct scope from the autobiographical diary above: this reads the audit
# rows in ``vessel_activity_log`` and produces a *factual* recap of what the
# session actually did (coordinates, quantities, world state, action outcomes),
# with no first-person voice or personality. It is the mode the Rift Vessel
# Compactor plugin enqueues when a session reaches the ENDED state.
# ---------------------------------------------------------------------------


def _stringify_metadata(metadata: Any) -> str:
    """Render an activity-log ``metadata`` value into a compact factual string.

    Only structural, factual key/value pairs are surfaced (coordinates,
    quantities, ids, state) — never interpreted, never keyword-matched. Nested
    values are JSON-encoded. Fully fail-safe: any error yields an empty string.
    """
    if metadata is None:
        return ""
    if isinstance(metadata, (bytes, bytearray)):
        try:
            metadata = metadata.decode("utf-8", errors="replace")
        except Exception:
            return ""
    if isinstance(metadata, str):
        stripped = metadata.strip()
        if not stripped:
            return ""
        try:
            import json as _json

            metadata = _json.loads(stripped)
        except Exception:
            return stripped
    if isinstance(metadata, dict):
        parts: list[str] = []
        for key, value in metadata.items():
            key_s = str(key).strip()
            if not key_s:
                continue
            if isinstance(value, (dict, list)):
                try:
                    import json as _json

                    value_s = _json.dumps(value, ensure_ascii=False, sort_keys=True)
                except Exception:
                    value_s = str(value)
            else:
                value_s = str(value)
            value_s = value_s.strip()
            if not value_s:
                continue
            parts.append(f"{key_s}={value_s}")
        return " ".join(parts)
    return str(metadata).strip()


def _activity_row_to_line(row: dict[str, Any]) -> str:
    """Render one ``vessel_activity_log`` row into a compact factual line.

    Format: ``[event_type] summary | key=value key=value`` — the human-readable
    summary plus the structural metadata (coordinates/quantities/state). No
    interpretation, no keyword matching. Truncated to ``_ITEM_MAX_CHARS``.
    """
    event_type = str(row.get("event_type") or "").strip()
    summary = str(row.get("summary") or "").strip()
    meta = _stringify_metadata(row.get("metadata"))
    head = f"[{event_type}] {summary}" if event_type else summary
    line = f"{head} | {meta}" if meta else head
    line = line.strip()
    if len(line) > _ITEM_MAX_CHARS:
        line = line[:_ITEM_MAX_CHARS] + "…"
    return line


async def load_activity_lines(session_id: str) -> list[str]:
    """Load a session's ``vessel_activity_log`` rows as factual text lines.

    Returns the rows for ``session_id`` in chronological order, each rendered by
    :func:`_activity_row_to_line`. Empty (no summary and no metadata) lines are
    skipped. Fully fail-safe — any DB error yields an empty list.
    """
    if not session_id:
        return []
    lines: list[str] = []
    try:
        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT event_type, summary, metadata, created_at "
                    "FROM vessel_activity_log WHERE session_id = %s "
                    "ORDER BY created_at ASC, id ASC",
                    (session_id,),
                )
                rows = await cur.fetchall()
                columns = [c[0] for c in (cur.description or [])]
    except Exception as exc:
        log_error(
            f"[vessel_recap] failed to read vessel_activity_log for {session_id}: {exc}"
        )
        return []
    for raw in rows or []:
        if isinstance(raw, dict):
            row = raw
        else:
            row = dict(zip(columns, raw))
        line = _activity_row_to_line(row)
        if line:
            lines.append(line)
    return lines


def _build_recap_chunk_prompt(
    environment: str,
    lines: list[str],
    part_no: int,
    part_total: int,
) -> dict[str, Any]:
    """Build the prompt to summarise ONE chunk of activity into a factual partial.

    Deliberately persona-free, third-person and operational: it asks for facts
    (positions, quantities, state, actions and their outcomes), never a
    first-person narrative. The chunk is delivered as structured payload data,
    not interpolated free text, so no phrase matching is involved.
    """
    instructions = (
        f"Below is part {part_no} of {part_total} of the operational activity "
        f"log of a single embodiment session in the world '{environment}'. "
        "Write a SHORT, FACTUAL, third-person recap of this part: what actions "
        "were taken and their outcomes, plus concrete state — positions, "
        "quantities, resources, health, notable entities. Report facts only. Do "
        "NOT use first person, do NOT add personality, emotion, or narrative. Do "
        "NOT invent anything not present in the log. Keep it terse.\n"
        'Return ONLY a JSON object: {"partial": "<factual recap>"}.'
    )
    return {
        "input": {
            "type": "vessel_recap_chunk",
            "payload": {"environment": environment, "activity": lines},
        },
        "context": {},
        "instructions": instructions,
    }


def _build_recap_fold_prompt(
    environment: str,
    partials: list[str],
    reason: str,
) -> dict[str, Any]:
    """Build the prompt to fold factual partials into one operational recap."""
    instructions = (
        "Below are several factual, third-person fragments recapping, in order, "
        f"a single embodiment session in the world '{environment}' (the session "
        f"ended: {reason}). Merge them into ONE concise, FACTUAL, third-person "
        "operational recap of the whole session — actions taken and outcomes, "
        "final state, resources gained/lost, and any unresolved goal. Report "
        "facts only. Do NOT use first person, personality, emotion, or "
        "narrative. Do NOT invent anything beyond the fragments.\n"
        'Return ONLY a JSON object: {"entry": "<factual operational recap>"}.'
    )
    return {
        "input": {
            "type": "vessel_recap_fold",
            "payload": {"environment": environment, "fragments": partials},
        },
        "context": {},
        "instructions": instructions,
    }


async def _fold_recap_partials(
    engine: Any,
    environment: str,
    partials: list[str],
    reason: str,
    chunk_chars: int,
    depth: int = 0,
) -> str:
    """Fold factual partials into one recap, recursing if still oversized."""
    if not partials:
        return ""
    if len(partials) == 1:
        return partials[0]

    joined_len = sum(len(p) for p in partials)
    if joined_len > chunk_chars and depth < _MAX_FOLD_DEPTH:
        group_partials = _chunk_lines(partials, len(partials), chunk_chars)
        reduced: list[str] = []
        for group in group_partials:
            if len(group) == 1:
                reduced.append(group[0])
                continue
            prompt = _build_recap_fold_prompt(environment, group, reason)
            folded = await _generate_json(engine, prompt, "entry")
            reduced.append(folded if folded else "\n".join(group))
        return await _fold_recap_partials(
            engine, environment, reduced, reason, chunk_chars, depth + 1
        )

    prompt = _build_recap_fold_prompt(environment, partials, reason)
    folded = await _generate_json(engine, prompt, "entry")
    return folded if folded else "\n\n".join(partials)


def _recap_fallback(environment: str, lines: list[str], reason: str) -> str:
    """Deterministic plain-text recap used when the LLM is unavailable."""
    body = "\n".join(f"- {line}" for line in lines)
    return f"Operational recap of {environment} (session ended: {reason}).\n{body}"


async def compact_activity_recap(
    session_id: str,
    environment: str,
    interface_path: str | None,
    reason: str,
) -> int | None:
    """Compact a session's ``vessel_activity_log`` into one operational recap.

    Reads the session's audit rows, summarises them in chunks into a factual,
    third-person recap (no first person / personality), folds the partials into
    one entry and saves it to ``vessel_diary`` with
    ``reason = ACTIVITY_RECAP_REASON``. Returns the new ``vessel_diary`` id, or
    ``None`` if there was nothing to recap. Never raises — on any LLM failure it
    falls back to a deterministic plain-text join.
    """
    lines = await load_activity_lines(session_id)
    if not lines:
        log_debug(f"[vessel_recap] no activity to recap for session {session_id}")
        return None

    chunk_items, chunk_chars = _resolve_chunk_config()
    chunks = _chunk_lines(lines, chunk_items, chunk_chars)

    engine = await _resolve_engine()
    if engine is None:
        log_warning(
            "[vessel_recap] no Cortex engine available; using plain-text fallback"
        )
        summary = _recap_fallback(environment, lines, reason)
    else:
        partials: list[str] = []
        part_total = len(chunks)
        for idx, chunk in enumerate(chunks, start=1):
            prompt = _build_recap_chunk_prompt(environment, chunk, idx, part_total)
            partial = await _generate_json(engine, prompt, "partial")
            partials.append(partial if partial else "\n".join(chunk))
        summary = await _fold_recap_partials(
            engine, environment, partials, reason, chunk_chars
        )
        if not summary:
            summary = _recap_fallback(environment, lines, reason)

    entry_id = await save_vessel_diary(
        session_id=session_id,
        environment=environment,
        interface_path=interface_path,
        summary=summary,
        moments_count=len(lines),
        reason=ACTIVITY_RECAP_REASON,
    )
    log_info(
        f"[vessel_recap] compacted session {session_id} "
        f"({len(lines)} activity rows) into vessel_diary #{entry_id}"
    )
    return entry_id
