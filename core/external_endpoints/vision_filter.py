"""Which models the Iris model selector lists.

A model belongs in the selector when the endpoint declares vision for it (the
``vision`` skill in its capabilities) or when it passed the image probe: a photo
sent through the Iris path came back as a description. A model that has neither is
not offered: that is how a speech or embedding model, which carries no vision
skill and cannot answer a photo, stops appearing next to the real ones.

The probe outcome lives next to the capabilities, in the model's metadata entry:
``{"vision_probe": {"ok": bool, "elapsed_ms": int, "at": iso-8601}}``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

KEY = "vision_probe"


def has_vision_skill(meta: dict[str, Any] | None) -> bool:
    """True when the endpoint declares vision for this model."""
    caps = (meta or {}).get("capabilities")
    return bool(isinstance(caps, dict) and caps.get("vision"))


def passed_vision_probe(meta: dict[str, Any] | None) -> bool:
    """True when the last image probe of this model produced a description."""
    probe = (meta or {}).get(KEY)
    return bool(isinstance(probe, dict) and probe.get("ok"))


def is_listed_for_iris(meta: dict[str, Any] | None) -> bool:
    """True when the Iris model selector should offer this model."""
    return has_vision_skill(meta) or passed_vision_probe(meta)


def meta_by_id(models_metadata: list[dict[str, Any]] | None) -> dict[str, dict]:
    """Index a metadata list by model id."""
    return {
        str(m.get("id") or m.get("model_id")): m
        for m in (models_metadata or [])
        if isinstance(m, dict) and (m.get("id") or m.get("model_id"))
    }


def models_to_probe(
    available_models: list[str], models_metadata: list[dict[str, Any]] | None
) -> list[str]:
    """Models with no vision skill: the ones the selector hides until a probe passes."""
    index = meta_by_id(models_metadata)
    return [m for m in available_models if not has_vision_skill(index.get(m))]


def record_probe(
    models_metadata: list[dict[str, Any]] | None,
    model_id: str,
    *,
    ok: bool,
    elapsed_ms: int,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return ``models_metadata`` with the probe outcome stored on ``model_id``.

    The entry is created when the endpoint reported no metadata for the model.
    """
    stamp = (now or datetime.now(timezone.utc)).isoformat()
    result: list[dict[str, Any]] = []
    found = False
    for entry in models_metadata or []:
        if isinstance(entry, dict) and str(entry.get("id") or "") == model_id:
            entry = {**entry, KEY: {"ok": ok, "elapsed_ms": elapsed_ms, "at": stamp}}
            found = True
        result.append(entry)
    if not found:
        result.append(
            {
                "id": model_id,
                "capabilities": {},
                KEY: {"ok": ok, "elapsed_ms": elapsed_ms, "at": stamp},
            }
        )
    return result


def carry_over_probes(
    new_metadata: list[dict[str, Any]] | None,
    old_metadata: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Keep the stored probe outcomes when the endpoint's models are re-listed.

    A re-probe of the endpoint rebuilds the metadata from the provider's answer,
    which knows nothing about our image probes; without this each refresh would
    erase them and the selector would lose every model that only passed the probe.
    """
    old = meta_by_id(old_metadata)
    merged: list[dict[str, Any]] = []
    for entry in new_metadata or []:
        previous = (
            old.get(str(entry.get("id") or "")) if isinstance(entry, dict) else None
        )
        if previous and KEY in previous and KEY not in entry:
            entry = {**entry, KEY: previous[KEY]}
        merged.append(entry)
    return merged
