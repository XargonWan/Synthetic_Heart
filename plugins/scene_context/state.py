"""Bounded scene patches. Relations always name both actors explicitly."""

from __future__ import annotations

import math
from copy import deepcopy

FIELDS = frozenset(
    {
        "location",
        "self.position",
        "human.position",
        "relation",
        "activity",
        "self.activity",
    }
)
MAX_AGE_SECONDS = 6 * 3600
SOURCES = frozenset({"human", "assistant"})
SCALAR_MAX_CHARS = {"human": 240, "assistant": 120}
PREDICATE_MAX_CHARS = 80
ACTORS = frozenset({"self", "human"})
# Quotes shorter than this must match the whole source message exactly.
MIN_PARTIAL_QUOTE_CHARS = 8
MIN_FULL_QUOTE_CHARS = 3


def _finite_time(value) -> bool:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _normalise_text(value: str) -> str:
    return " ".join(value.split())


def _clean_relation(value) -> dict | None:
    if not isinstance(value, dict):
        return None
    subject = value.get("subject")
    obj = value.get("object")
    predicate = value.get("predicate")
    if not isinstance(subject, str) or not isinstance(obj, str):
        return None
    if subject not in ACTORS or obj not in ACTORS or subject == obj:
        return None
    if not isinstance(predicate, str):
        return None
    predicate = _normalise_text(predicate)
    if not 1 <= len(predicate) <= PREDICATE_MAX_CHARS:
        return None
    return {"subject": subject, "predicate": predicate, "object": obj}


def _clean_scalar(value, source: str) -> str | None:
    if not isinstance(value, str):
        return None
    value = _normalise_text(value)
    if not 1 <= len(value) <= SCALAR_MAX_CHARS.get(source, 0):
        return None
    return value


def _clean_value(field: str, value, source: str):
    """Normalised value for ``field``, or None when the shape is invalid."""
    if field == "relation":
        return _clean_relation(value)
    return _clean_scalar(value, source)


def _valid_entry(field: str, entry) -> dict | None:
    if not isinstance(entry, dict):
        return None
    source = entry.get("source")
    updated_at = entry.get("updated_at")
    if (
        not isinstance(source, str)
        or source not in SOURCES
        or not _finite_time(updated_at)
    ):
        return None
    value = _clean_value(field, entry.get("value"), source)
    if value is None:
        return None
    return {"value": value, "updated_at": updated_at, "source": source}


def _evidence_supported(quote, source_text: str) -> bool:
    """Exact, word-bounded support for a quote in the current source message.

    Short quotes are accepted only when they are the entire message, so
    "bed" or "in bed" survive but fragments like "on" inside "hold on" do not.
    """
    if not isinstance(quote, str) or not isinstance(source_text, str):
        return False
    quote = quote.strip()
    if not quote:
        return False
    if source_text.strip() == quote:
        return len(quote) >= MIN_FULL_QUOTE_CHARS
    if len(quote) < MIN_PARTIAL_QUOTE_CHARS:
        return False
    start = source_text.find(quote)
    while start != -1:
        end = start + len(quote)
        before_ok = not (
            quote[0].isalnum() and start > 0 and source_text[start - 1].isalnum()
        )
        after_ok = not (
            quote[-1].isalnum()
            and end < len(source_text)
            and source_text[end].isalnum()
        )
        if before_ok and after_ok:
            return True
        start = source_text.find(quote, start + 1)
    return False


def active_fields(scene: dict, *, now: float, max_age: float = MAX_AGE_SECONDS) -> dict:
    """Live, shape-valid entries. Invalid fields are dropped one by one."""
    if not isinstance(scene, dict):
        return {}
    result = {}
    for key, raw in scene.items():
        if key not in FIELDS:
            continue
        entry = _valid_entry(key, raw)
        if entry is None:
            continue
        if 0 <= now - entry["updated_at"] < max_age:
            result[key] = deepcopy(entry)
    return result


def apply_patch(
    scene: dict,
    patch: dict,
    *,
    source_text: str,
    phase: str,
    now: float,
    max_age: float = MAX_AGE_SECONDS,
    protected_fields: set[str] | None = None,
) -> dict:
    """Apply only evidence-backed changes; absent fields retain their own age."""
    result = active_fields(scene, now=now, max_age=max_age)
    if not isinstance(patch, dict):
        return result
    if not isinstance(phase, str) or phase not in SOURCES:
        return result
    if (
        phase == "human"
        and patch.get("reset") is True
        and _evidence_supported(patch.get("reset_evidence"), source_text)
    ):
        result = {}
    changes = patch.get("changes")
    if not isinstance(changes, list):
        return result
    for change in changes[: len(FIELDS)]:
        if not isinstance(change, dict) or "value" not in change:
            continue
        field = change.get("field")
        if not isinstance(field, str) or field not in FIELDS:
            continue
        if phase == "assistant" and (
            field not in {"self.position", "self.activity", "relation"}
            or field in (protected_fields or set())
        ):
            continue
        if not _evidence_supported(change.get("evidence"), source_text):
            continue
        value = change.get("value")
        if value is None:
            if field == "relation" and phase == "assistant":
                existing = result.get("relation", {}).get("value", {})
                if existing.get("subject") != "self":
                    continue
            result.pop(field, None)
            continue
        value = _clean_value(field, value, phase)
        if value is None:
            continue
        if field == "relation" and phase == "assistant" and value["subject"] != "self":
            continue
        result[field] = {"value": value, "updated_at": now, "source": phase}
    return result


def render_scene(
    scene: dict, *, now: float, name: str = "Synth", max_age: float = MAX_AGE_SECONDS
) -> str:
    """Readable, directional state, never an invented default setting."""
    scene = active_fields(scene, now=now, max_age=max_age)
    labels = {"self": f"You ({name})", "human": "Human"}
    lines = []
    for field, entry in scene.items():
        value = entry["value"]
        if field == "relation":
            lines.append(
                f"Relation: {labels[value['subject']]} {value['predicate']} {labels[value['object']]}"
            )
        else:
            label = {
                "location": "Shared location",
                "self.position": labels["self"],
                "human.position": labels["human"],
                "activity": "Activity",
                "self.activity": "Your activity",
            }[field]
            lines.append(f"{label}: {value}")
    return "\n".join(lines)
