# core/live_registry.py
"""Registry for Live bidirectional-streaming engines.

Live engines handle *real-time* audio sessions where input (microphone / PCM
chunks) and output (TTS audio / transcript events) travel over the same
persistent session.  Examples: Gemini Live, Harmony Live.

Unlike Auris (file → text) and Vox (text → file), Live engines are
bidirectional and session-based.  They belong exclusively to this registry —
they do **not** register with Auris or Vox.

Mirrors the ``cortex_registry`` pattern.
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, List, Optional, TypedDict

from core.logging_utils import log_debug, log_error, log_info


class LiveCapabilities(TypedDict, total=False):
    """Capability flags a Live engine may advertise."""

    input: bool  # Accepts audio chunks and emits transcript events
    output: bool  # Accepts text and emits TTS audio events
    vad: bool  # Has built-in voice-activity detection
    local: bool  # Runs fully on-device (no external API)


class LiveRegistry:
    """Central registry for all Live streaming engines."""

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
        capabilities: Optional[LiveCapabilities] = None,
        label: str = "",
    ) -> None:
        """Register a Live engine.

        Args:
            name:         Short unique identifier (e.g. ``"gemini_live"``, ``"harmony_live"``).  
            module_path:  Dotted import path to the module containing ``ENGINE_CLASS``.
            capabilities: Optional dict of boolean capability flags.
            label:        Human-readable description shown in the WebUI.
        """
        self._engine_modules[name] = module_path
        self._engine_meta[name] = {
            "capabilities": capabilities or {},
            "label": label,
        }
        log_debug(f"[live_registry] Registered engine '{name}' -> {module_path}")

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_available_engines(self) -> List[str]:
        """Return all registered engine names."""
        return list(self._engine_modules.keys())

    def get_engine_meta(self, name: str) -> Dict[str, Any]:
        return self._engine_meta.get(name, {})

    def find_engine_by_capabilities(self, required: LiveCapabilities) -> Optional[str]:
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
            raise ValueError(f"[live_registry] Unknown engine: '{name}'")

        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError as exc:
            log_error(f"[live_registry] Cannot import '{module_path}': {exc}")
            raise ValueError(
                f"[live_registry] Invalid engine module: '{name}'"
            ) from exc

        engine_class = getattr(module, "ENGINE_CLASS", None)
        if engine_class is None:
            raise ValueError(
                f"[live_registry] Module '{module_path}' has no ENGINE_CLASS"
            )

        instance = engine_class()
        instance.setup()
        self._instances[name] = instance
        log_info(f"[live_registry] Loaded and instantiated engine '{name}'")
        return instance


# ---------------------------------------------------------------------------
# Module-level singleton + convenience helper
# ---------------------------------------------------------------------------

LIVE_REGISTRY = LiveRegistry()


def register_live_engine(
    name: str,
    module_path: str,
    capabilities: Optional[LiveCapabilities] = None,
    label: str = "",
) -> None:
    """Module-level convenience wrapper for ``LIVE_REGISTRY.register_engine``."""
    LIVE_REGISTRY.register_engine(name, module_path, capabilities, label)
