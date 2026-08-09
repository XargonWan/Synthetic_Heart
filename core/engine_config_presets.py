# core/engine_config_presets.py
"""Named engine-configuration presets.

Provides save/delete/apply semantics for reusable, named ``extra_config``
bundles (optionally carrying a model) that can be swapped onto any external
endpoint from the WebUI Engines tab.  Presets are stored as a JSON list in the
config registry under ``ENGINE_CONFIG_PRESETS``, so no schema migration is
needed and the list survives restarts.

This module is fully optional: when the config registry or the external
endpoint registry is unavailable, every helper fails closed (returns empty
results) and never raises.
"""

from __future__ import annotations

import logging
from typing import Any

from core.logging_utils import log_info, log_warning

logger = logging.getLogger(__name__)

PRESETS_CONFIG_KEY = "ENGINE_CONFIG_PRESETS"

# System keys preserved from an endpoint's existing ``extra_config`` when a
# preset is applied, so bookkeeping the endpoint wizard relies on (e.g. the
# originating provider preset id) is never clobbered by a preset swap.
_PRESERVED_KEYS: tuple[str, ...] = ("provider_id",)


def load_presets() -> list[dict[str, Any]]:
    """Return all saved presets as a list of dicts (never raises)."""
    try:
        from core.config_manager import config_registry

        value = config_registry.get_value(PRESETS_CONFIG_KEY, [])
        if isinstance(value, list):
            return [
                item
                for item in value
                if isinstance(item, dict) and str(item.get("name") or "").strip()
            ]
    except Exception as exc:
        log_warning(f"[engine_config_presets] load failed: {exc}")
    return []


def _find_preset(presets: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    for item in presets:
        if str(item.get("name") or "").strip() == name:
            return item
    return None


async def _store_presets(presets: list[dict[str, Any]]) -> None:
    from core.config_manager import config_registry

    await config_registry.set_value(PRESETS_CONFIG_KEY, presets, require_persist=True)


async def save_preset(
    name: str,
    *,
    model: str | None = None,
    description: str = "",
    extra_config: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create or replace a named preset.

    Args:
        name: Unique preset name (whitespace-stripped, required).
        model: Optional model id applied together with the config.
        description: Optional free-text note shown in the UI.
        extra_config: The ``extra_config`` bundle the preset carries.

    Returns the stored preset dict.  Raises ``ValueError`` for an empty name.
    """
    preset_name = str(name or "").strip()
    if not preset_name:
        raise ValueError("Preset name is required")

    preset: dict[str, Any] = {
        "name": preset_name,
        "model": str(model).strip() if model else "",
        "description": str(description or "").strip(),
        "extra_config": dict(extra_config or {}),
    }

    presets = load_presets()
    existing = _find_preset(presets, preset_name)
    if existing is not None:
        presets = [item for item in presets if item is not existing]
    presets.append(preset)
    await _store_presets(presets)
    log_info(f"[engine_config_presets] Saved preset '{preset_name}'")
    return preset


async def delete_preset(name: str) -> bool:
    """Remove a named preset.  Returns True when a preset was actually removed."""
    preset_name = str(name or "").strip()
    if not preset_name:
        return False
    presets = load_presets()
    remaining = [
        item for item in presets if str(item.get("name") or "").strip() != preset_name
    ]
    if len(remaining) == len(presets):
        return False
    await _store_presets(remaining)
    log_info(f"[engine_config_presets] Deleted preset '{preset_name}'")
    return True


async def apply_preset(
    endpoint_id: int, name: str
) -> tuple[Any | None, dict[str, Any] | None]:
    """Apply a named preset to an external endpoint.

    Replaces the endpoint's ``extra_config`` with the preset's values
    (preserving a small set of system keys) and, when the preset carries a
    model, sets it as the endpoint's default model.  The model is written
    *before* ``update_endpoint`` so its re-sync rebuilds the live bridge from
    a DB row that already carries the new ``default_model`` — the running
    engine swaps to the preset model in the same pass (including models that
    are not in the probed ``available_models`` list).

    Returns ``(updated_endpoint_or_None, applied_preset_or_None)``.  A missing
    preset or endpoint comes back as a ``None`` pair; unexpected errors
    propagate to the caller.
    """
    preset = _find_preset(load_presets(), str(name or "").strip())
    if preset is None:
        return None, None

    try:
        from core.external_endpoints.registry import (
            get_external_endpoint_registry,
        )

        reg = get_external_endpoint_registry()
        ep = await reg.get_endpoint(endpoint_id)
        if ep is None:
            return None, preset

        new_config = dict(preset.get("extra_config") or {})
        existing_config = ep.extra_config or {}
        for key in _PRESERVED_KEYS:
            if key in existing_config:
                new_config.setdefault(key, existing_config[key])

        model = str(preset.get("model") or "").strip()
        if model:
            await reg.set_default_model(endpoint_id, model)
        updated = await reg.update_endpoint(endpoint_id, extra_config=new_config)
        log_info(
            f"[engine_config_presets] Applied preset '{preset.get('name')}' "
            f"to endpoint id={endpoint_id}"
        )
        return updated, preset
    except Exception as exc:
        log_warning(f"[engine_config_presets] apply failed: {exc}")
        raise
