# core/vessel_registry.py
"""Registry for Rift Vessel connectors (game-world embodiment layers).

A *Rift Vessel* lets SyntH — the persistent cognitive entity — act through and
perceive an external interactive environment (a "world"): Minecraft, Skyrim,
VRChat, HyTale, and future systems. Each world is provided by a pluggable
**connector** that subclasses :class:`plugins.rift_vessel.vessel_base.VesselConnectorBase`.

This registry mirrors the ``iris_registry`` / ``auris_registry`` pattern:
connectors register themselves via :func:`register_vessel_connector` at import
time and are loaded on demand. The core Vessel plugin/interface uses this
registry exclusively — individual connectors never worry about dispatch or
message-chain injection.

Golden rule (AGENTS.md): removing any connector must not break the rest of the
system. Unknown/failed connectors are logged and skipped.
"""

from __future__ import annotations

import importlib
from typing import Any, Dict, List, Optional, TypedDict

from core.logging_utils import log_debug, log_error, log_info


class VesselCapabilities(TypedDict, total=False):
    """Capability flags a Vessel connector may advertise."""

    movement: bool  # Can move the embodiment within the world
    chat: bool  # Can send/receive in-world chat
    perception: bool  # Emits perception events (proximity, damage, sight, ...)
    interaction: bool  # Can use/interact with world objects
    local: bool  # Runs fully on the local machine (no external service)


class VesselRegistry:
    """Central registry for all Rift Vessel connectors."""

    def __init__(self) -> None:
        self._connector_modules: Dict[str, str] = {}
        self._connector_meta: Dict[str, Dict[str, Any]] = {}
        self._instances: Dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Registration
    # ------------------------------------------------------------------

    def register_connector(
        self,
        name: str,
        module_path: str,
        capabilities: Optional[VesselCapabilities] = None,
        label: str = "",
    ) -> None:
        """Register a Vessel connector.

        Args:
            name:         Short unique identifier (e.g. ``"minecraft"``).
            module_path:  Dotted import path to the module containing
                          ``CONNECTOR_CLASS``.
            capabilities: Optional dict of boolean capability flags.
            label:        Human-readable description shown in the WebUI.
        """
        self._connector_modules[name] = module_path
        self._connector_meta[name] = {
            "capabilities": capabilities or {},
            "label": label,
        }
        log_debug(f"[vessel_registry] Registered connector '{name}' -> {module_path}")

    # ------------------------------------------------------------------
    # Query helpers
    # ------------------------------------------------------------------

    def get_available_connectors(self) -> List[str]:
        """Return all registered connector names."""
        return list(self._connector_modules.keys())

    def get_connector_meta(self, name: str) -> Dict[str, Any]:
        return self._connector_meta.get(name, {})

    def find_connector_by_capabilities(
        self, required: VesselCapabilities
    ) -> Optional[str]:
        """Return the first registered connector satisfying all *required* caps."""
        for name, meta in self._connector_meta.items():
            caps = meta.get("capabilities") or {}
            if all(caps.get(k) for k, v in required.items() if v):
                return name
        return None

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load_connector(self, name: str) -> Any:
        """Import the connector module and instantiate ``CONNECTOR_CLASS``."""
        if name in self._instances:
            return self._instances[name]

        module_path = self._connector_modules.get(name)
        if not module_path:
            raise ValueError(f"[vessel_registry] Unknown connector: '{name}'")

        try:
            module = importlib.import_module(module_path)
        except ModuleNotFoundError as exc:
            log_error(f"[vessel_registry] Cannot import '{module_path}': {exc}")
            raise ValueError(
                f"[vessel_registry] Invalid connector module: '{name}'"
            ) from exc

        if not hasattr(module, "CONNECTOR_CLASS"):
            raise ValueError(
                f"[vessel_registry] Module '{module_path}' does not define "
                "CONNECTOR_CLASS."
            )

        connector_class = module.CONNECTOR_CLASS
        instance = connector_class()
        self._instances[name] = instance
        log_info(
            f"[vessel_registry] Loaded connector '{name}' ({connector_class.__name__})"
        )
        return instance

    def register_instance(
        self,
        name: str,
        instance: object,
        label: str = "",
        capabilities: Optional[VesselCapabilities] = None,
    ) -> None:
        """Register a pre-built connector instance directly (skips module loading)."""
        self._connector_modules[name] = "__direct__"
        self._connector_meta[name] = {
            "capabilities": capabilities or {},
            "label": label,
        }
        self._instances[name] = instance
        log_info(f"[vessel_registry] Registered external instance '{name}'")

    def unregister_connector(self, name: str) -> None:
        """Fully remove a connector from all caches (registered + instances)."""
        self._connector_modules.pop(name, None)
        self._connector_meta.pop(name, None)
        self._instances.pop(name, None)
        log_info(f"[vessel_registry] Unregistered connector '{name}'")

    def get_instance(self, name: str) -> Any | None:
        """Return the pre-built connector instance for *name*, or ``None``."""
        return self._instances.get(name)

    def unload_connector(self, name: str) -> None:
        """Remove a cached connector instance (forces reload on next use)."""
        self._instances.pop(name, None)
        log_debug(f"[vessel_registry] Unloaded connector instance '{name}'")


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

VESSEL_REGISTRY = VesselRegistry()


def register_vessel_connector(
    name: str,
    module_path: str,
    capabilities: Optional[VesselCapabilities] = None,
    label: str = "",
) -> None:
    """Convenience helper used by connector modules at import time."""
    VESSEL_REGISTRY.register_connector(name, module_path, capabilities, label)
