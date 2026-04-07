"""Base module for Cortex LLM Provider engines."""

from __future__ import annotations

import importlib
import os
import pkgutil
from typing import Optional

from core.logging_utils import log_debug, log_warning
from core.cortex_registry import Capabilities

ENGINE_KIND = "llm_provider"
ENGINE_LABEL = "LLM Provider engines (API / SDK based)"
CAPABILITIES: Optional[Capabilities] = None


def discover_and_register(registry, dev_enabled: bool = False) -> None:
    """Register the LLM Provider cortex kind and its child engines."""
    registry.register_cortex_kind(ENGINE_KIND, ENGINE_LABEL, CAPABILITIES)

    base_path = os.path.dirname(__file__)

    def _register_from_path(path: str, module_prefix: str) -> None:
        for _importer, module_name, is_pkg in pkgutil.iter_modules([path]):
            if is_pkg or module_name.startswith("_") or module_name.endswith("_base"):
                continue
            module_path = f"{module_prefix}.{module_name}"
            try:
                mod = importlib.import_module(module_path)
            except Exception as exc:
                log_warning(
                    f"[llm_provider_base] Failed to import {module_path}: {exc}"
                )
                continue
            if not hasattr(mod, "PLUGIN_CLASS"):
                continue
            label = getattr(mod, "ENGINE_LABEL", None)
            if not label and hasattr(mod, "PLUGIN_CLASS"):
                label = getattr(mod.PLUGIN_CLASS, "engine_label", None)
            registry.register_engine_module(
                module_name,
                module_path,
                cortex=ENGINE_KIND,
                label=label or None,
            )
            log_debug(
                f"[llm_provider_base] Registered engine: {module_name} ({module_path})"
            )

    _register_from_path(base_path, "cortex.llm_provider")

    if dev_enabled:
        dev_path = os.path.join(base_path, "dev")
        if os.path.isdir(dev_path):
            _register_from_path(dev_path, "cortex.llm_provider.dev")
