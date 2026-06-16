"""Base module for Cortex Agent engines."""

from __future__ import annotations

import importlib
import os
import pkgutil
from typing import Optional

from core.logging_utils import log_debug, log_warning
from core.cortex_registry import Capabilities

ENGINE_KIND = "agent"
ENGINE_LABEL = "Agent engines"
CAPABILITIES: Optional[Capabilities] = None


def discover_and_register(registry, dev_enabled: bool = False) -> None:
    """Register the Agent cortex kind and its child engines."""
    registry.register_cortex_kind(ENGINE_KIND, ENGINE_LABEL, CAPABILITIES)

    base_path = os.path.dirname(__file__)

    for _importer, module_name, is_pkg in pkgutil.iter_modules([base_path]):
        if is_pkg or module_name.startswith("_") or module_name.endswith("_base"):
            continue
        module_path = f"engines.agent.{module_name}"
        try:
            mod = importlib.import_module(module_path)
        except Exception as exc:
            log_warning(f"[agent_base] Failed to import {module_path}: {exc}")
            continue
        if not hasattr(mod, "PLUGIN_CLASS"):
            continue
        label = getattr(mod, "ENGINE_LABEL", None)
        registry.register_engine_module(
            module_name,
            module_path,
            cortex=ENGINE_KIND,
            label=label or None,
        )
        log_debug(f"[agent_base] Registered engine: {module_name} ({module_path})")
