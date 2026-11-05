"""Guarded access to optional plugins from core and interface code.

Removing a plugin must never break the system (AGENTS.md), so core/interface
modules reach optional plugins through these helpers instead of importing them
directly: a missing or broken plugin degrades to "feature off".
"""

from __future__ import annotations


def is_vox_enabled() -> bool:
    """True when the Vox plugin is installed and enabled; False otherwise."""
    try:
        from plugins.vox_plugin import is_vox_enabled as _is_vox_enabled

        return bool(_is_vox_enabled())
    except Exception:
        return False
