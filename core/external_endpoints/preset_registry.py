# core/external_endpoints/preset_registry.py
"""Provider preset registry for guided external-endpoint setup.

Scans the ``providers/`` directory (project root) for JSON preset files and
exposes them via :func:`load_presets`.  Each preset describes a known AI
service provider so the UI can pre-fill the add-endpoint wizard with sensible
defaults while leaving the user free to override any field.

Preset files are intentionally kept outside ``core/`` so they are engine-
agnostic and can be removed individually without affecting the rest of the
system.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Required top-level keys every preset must have.
_REQUIRED_KEYS: frozenset[str] = frozenset(
    {"provider_id", "display_name", "protocol", "base_url"}
)

# Defaults applied when a key is missing from the JSON file.
_DEFAULTS: dict[str, Any] = {
    "description": "",
    "icon": "custom",
    "base_url_locked": False,
    "requires_api_key": False,
    "api_key_placeholder": "",
    "api_key_hint": "",
    "default_capabilities": {
        "cortex": True,
        "vox": False,
        "auris": False,
        "live": False,
    },
    "suggested_name": "",
    "suggested_label": "",
    "extra_config": {},
    # Wizard grouping: "llm" providers render in the main grid, "tts" ones in
    # the dedicated "TTS Endpoints" section below the divider.
    "category": "llm",
    # Provider-specific form fields rendered in step 2 of the add wizard.
    # Each entry: {key, label, type: "text"|"select", options, default,
    # placeholder, hint}. Values are persisted into the endpoint extra_config.
    "extra_fields": [],
    "sort_order": 500,
}


def _providers_dir() -> Path:
    """Return the ``providers/`` directory relative to the project root.

    The project root is inferred as the parent of this file's package tree
    (i.e. two levels up from ``core/external_endpoints/``).
    """
    return Path(__file__).parent.parent.parent / "providers"


def load_presets(providers_dir: Path | None = None) -> list[dict[str, Any]]:
    """Load and return all valid provider presets sorted by ``sort_order``.

    Parameters
    ----------
    providers_dir:
        Override the directory to scan.  Defaults to the project-level
        ``providers/`` folder resolved by :func:`_providers_dir`.

    Returns
    -------
    list[dict]
        Validated preset dicts, sorted ascending by ``sort_order`` then
        ``display_name``.  Invalid files are skipped with a warning.
    """
    directory = providers_dir if providers_dir is not None else _providers_dir()

    if not directory.is_dir():
        logger.warning(
            "[preset_registry] providers/ directory not found at %s — "
            "no presets available",
            directory,
        )
        return []

    presets: list[dict[str, Any]] = []
    for path in sorted(directory.glob("*.json")):
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            logger.warning("[preset_registry] Could not parse %s: %s", path.name, exc)
            continue

        missing = _REQUIRED_KEYS - raw.keys()
        if missing:
            logger.warning(
                "[preset_registry] Skipping %s — missing required keys: %s",
                path.name,
                missing,
            )
            continue

        # Apply defaults for optional fields
        preset: dict[str, Any] = {**_DEFAULTS, **raw}
        # Ensure default_capabilities is always a full dict
        caps: dict[str, bool] = {
            "cortex": False,
            "vox": False,
            "auris": False,
            "live": False,
        }
        caps.update(
            {k: bool(v) for k, v in preset.get("default_capabilities", {}).items()}
        )
        preset["default_capabilities"] = caps

        presets.append(preset)

    presets.sort(key=lambda p: (p["sort_order"], p["display_name"].lower()))
    return presets
