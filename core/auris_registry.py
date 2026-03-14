# core/auris_registry.py
"""Registry for Auris STT (speech-to-text / input) engines.

Mirrors the cortex_registry pattern: engines register themselves via
``register_engine`` and are loaded on demand.  The core Auris plugin uses this
registry exclusively — individual engines never need to worry about dispatch,
chain injection, VAD state, etc.
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, List, Optional, TypedDict

from core.logging_utils import log_debug, log_error, log_info


class AurisCapabilities(TypedDict, total=False):
    """Capability flags an Auris engine may advertise.

    Auris engines are file-based only.  For realtime/VAD engines see
    ``core.live_registry.LiveCapabilities``.
    """

    file_based: bool  # Can transcribe an audio file on-disk
    local: bool  # Runs fully on the local machine (no external API)


class AurisRegistry:
    """Central registry for all Auris STT engines."""

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
        capabilities: Optional[AurisCapabilities] = None,
        label: str = "",
    ) -> None:
        """Register an Auris engine.

        Args:
            name:         Short unique identifier (e.g. ``"gemini"``, ``"whisper"``).
            module_path:  Dotted import path to the module containing ``ENGINE_CLASS``.
            capabilities: Optional dict of boolean capability flags.
            label:        Human-readable description shown in the WebUI.
        """
        self._engine_modules[name] = module_path
        self._engine_meta[name] = {
            "capabilities": capabilities or {},
            "label": label,
        }
        log_debug(f"[auris_registry] Registered engine '{name}' -> {module_path}")

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_available_engines(self) -> List[str]:
        """Return all registered engine names."""
        return list(self._engine_modules.keys())

    def get_engine_meta(self, name: str) -> Dict[str, Any]:
        return self._engine_meta.get(name, {})

    def find_engine_by_capabilities(self, required: AurisCapabilities) -> Optional[str]:
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
            raise ValueError(f"[auris_registry] Unknown engine: '{name}'")

        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError as exc:
            log_error(f"[auris_registry] Cannot import '{module_path}': {exc}")
            raise ValueError(
                f"[auris_registry] Invalid engine module: '{name}'"
            ) from exc

        if not hasattr(module, "ENGINE_CLASS"):
            raise ValueError(
                f"[auris_registry] Module '{module_path}' does not define ENGINE_CLASS."
            )

        engine_class = module.ENGINE_CLASS
        instance = engine_class()
        self._instances[name] = instance
        log_info(f"[auris_registry] Loaded engine '{name}' ({engine_class.__name__})")
        return instance

    def unload_engine(self, name: str) -> None:
        """Remove a cached engine instance (forces reload on next use)."""
        self._instances.pop(name, None)
        log_debug(f"[auris_registry] Unloaded engine instance '{name}'")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

AURIS_REGISTRY = AurisRegistry()


def register_auris_engine(
    name: str,
    module_path: str,
    capabilities: Optional[AurisCapabilities] = None,
    label: str = "",
) -> None:
    """Convenience helper used by engine modules at import time."""
    AURIS_REGISTRY.register_engine(name, module_path, capabilities, label)
