# core/iris_registry.py
"""Registry for Iris vision (image/video understanding) engines.

Mirrors the auris_registry pattern: engines register themselves via
``register_engine`` and are loaded on demand.  The core Iris plugin uses this
registry exclusively — individual engines never need to worry about dispatch
or chain injection.
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, List, Optional, TypedDict

from core.logging_utils import log_debug, log_error, log_info


class IrisCapabilities(TypedDict, total=False):
    """Capability flags an Iris engine may advertise."""

    vision: bool  # Can analyse still images
    video: bool  # Can analyse video frames / short clips
    local: bool  # Runs fully on the local machine (no external API)


class IrisRegistry:
    """Central registry for all Iris vision engines."""

    def __init__(self) -> None:
        self._engine_modules: Dict[str, str] = {}
        self._engine_meta: Dict[str, Dict[str, Any]] = {}
        self._instances: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_engine(
        self,
        name: str,
        module_path: str,
        capabilities: Optional[IrisCapabilities] = None,
        label: str = "",
    ) -> None:
        """Register an Iris engine.

        Args:
            name:         Short unique identifier (e.g. ``"selenium-llm-engine"``).
            module_path:  Dotted import path to the module containing ``ENGINE_CLASS``.
            capabilities: Optional dict of boolean capability flags.
            label:        Human-readable description shown in the WebUI.
        """
        self._engine_modules[name] = module_path
        self._engine_meta[name] = {
            "capabilities": capabilities or {},
            "label": label,
        }
        log_debug(f"[iris_registry] Registered engine '{name}' -> {module_path}")

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_available_engines(self) -> List[str]:
        """Return all registered engine names."""
        return list(self._engine_modules.keys())

    def get_engine_meta(self, name: str) -> Dict[str, Any]:
        return self._engine_meta.get(name, {})

    def find_engine_by_capabilities(self, required: IrisCapabilities) -> Optional[str]:
        """Return the first registered engine satisfying all *required* caps."""
        for name, meta in self._engine_meta.items():
            caps = meta.get("capabilities") or {}
            if all(caps.get(k) for k, v in required.items() if v):
                return name
        return None

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_engine(self, name: str) -> Any:
        """Import the engine module and instantiate ``ENGINE_CLASS``."""
        if name in self._instances:
            return self._instances[name]

        module_path = self._engine_modules.get(name)
        if not module_path:
            raise ValueError(f"[iris_registry] Unknown engine: '{name}'")

        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError as exc:
            log_error(f"[iris_registry] Cannot import '{module_path}': {exc}")
            raise ValueError(
                f"[iris_registry] Invalid engine module: '{name}'"
            ) from exc

        if not hasattr(module, "ENGINE_CLASS"):
            raise ValueError(
                f"[iris_registry] Module '{module_path}' does not define ENGINE_CLASS."
            )

        engine_class = module.ENGINE_CLASS
        instance = engine_class()
        self._instances[name] = instance
        log_info(f"[iris_registry] Loaded engine '{name}' ({engine_class.__name__})")
        return instance

    def register_instance(self, name: str, instance: object, label: str = "") -> None:
        """Register a pre-built engine instance directly (skips module loading)."""
        self._engine_modules[name] = "__direct__"
        self._engine_meta[name] = {"capabilities": {}, "label": label}
        self._instances[name] = instance
        log_info(f"[iris_registry] Registered external instance '{name}'")

    def unregister_engine(self, name: str) -> None:
        """Fully remove an engine from all caches (registered + instances)."""
        self._engine_modules.pop(name, None)
        self._engine_meta.pop(name, None)
        self._instances.pop(name, None)
        log_info(f"[iris_registry] Unregistered engine '{name}'")

    def unload_engine(self, name: str) -> None:
        """Remove a cached engine instance (forces reload on next use)."""
        self._instances.pop(name, None)
        log_debug(f"[iris_registry] Unloaded engine instance '{name}'")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

IRIS_REGISTRY = IrisRegistry()


def register_iris_engine(
    name: str,
    module_path: str,
    capabilities: Optional[IrisCapabilities] = None,
    label: str = "",
) -> None:
    """Convenience helper used by engine modules at import time."""
    IRIS_REGISTRY.register_engine(name, module_path, capabilities, label)
