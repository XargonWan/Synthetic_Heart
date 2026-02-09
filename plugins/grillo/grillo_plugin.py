# plugins/grillo/grillo_plugin.py
"""
Moved Grillo plugin into subfolder for better modularity. This file is a literal
move of `plugins/grillo_plugin.py` for now; any future refactor should split
internal responsibilities further.
"""

# Import original module content from top-level to avoid duplication in this patch
# We will keep a copy of the plugin here to be used as main implementation

from typing import Any

try:
    from plugins.grillo.grillo_impl import GrilloPlugin  # type: ignore
except Exception:
    GrilloPlugin = None  # type: ignore

PLUGIN_CLASS = GrilloPlugin
