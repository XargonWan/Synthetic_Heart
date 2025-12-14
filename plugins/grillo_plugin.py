"""Backward-compatible wrapper for the Grillo plugin implementation.

This file exposes `PLUGIN_CLASS` pointing at the real implementation in `plugins/grillo/grillo_impl.py`.
"""

try:
    from plugins.grillo.grillo_impl import GrilloPlugin  # type: ignore
except Exception:  # pragma: no cover
    GrilloPlugin = None  # type: ignore

PLUGIN_CLASS = GrilloPlugin
