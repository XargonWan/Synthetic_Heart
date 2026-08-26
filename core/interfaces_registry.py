# core/interfaces_registry.py

"""
Registry to manage active interfaces without hardcoded dependencies.
"""

from typing import Dict, Any, Optional
from core.logging_utils import log_debug


class InterfaceRegistry:
    """Central registry for all active interfaces."""

    def __init__(self):
        self._interfaces: Dict[str, Any] = {}
        self._interface_configs: Dict[str, Dict[str, Any]] = {}
        self._trainer_ids: Dict[str, int | str] = {}
        self._interface_capabilities: Dict[str, frozenset[str]] = {}

    def register_interface(
        self,
        name: str,
        interface_instance: Any,
        config: Optional[Dict[str, Any]] = None,
        capabilities: Optional[frozenset[str]] = None,
    ):
        """Register a new interface.

        ``capabilities`` is the structural capability token set advertised by
        the interface (see ``core.interface_capabilities``). When omitted it
        is derived lazily via :meth:`get_interface_capabilities`.
        """
        self._interfaces[name] = interface_instance
        if config:
            self._interface_configs[name] = config
        if capabilities is not None:
            self._interface_capabilities[name] = frozenset(capabilities)
        log_debug(f"[interfaces_registry] Registered interface: {name}")

    def register_interface_capabilities(
        self, name: str, capabilities: frozenset[str]
    ) -> None:
        """Store (or overwrite) the capability set for a registered interface."""
        self._interface_capabilities[name] = frozenset(capabilities)
        log_debug(
            f"[interfaces_registry] Stored capabilities for {name}: "
            f"{sorted(self._interface_capabilities[name])}"
        )

    def get_interface_capabilities(self, name: str) -> frozenset[str]:
        """Return the capability tokens for ``name``; empty set when unknown."""
        return self._interface_capabilities.get(name, frozenset())

    def unregister_interface(self, name: str):
        """Remove an interface from the registry."""
        if name in self._interfaces:
            del self._interfaces[name]
        if name in self._interface_configs:
            del self._interface_configs[name]
        if name in self._trainer_ids:
            del self._trainer_ids[name]
        if name in self._interface_capabilities:
            del self._interface_capabilities[name]
        log_debug(f"[interfaces_registry] Unregistered interface: {name}")

    def get_interface(self, name: str) -> Optional[Any]:
        """Get a specific interface."""
        return self._interfaces.get(name)

    def get_all_interfaces(self) -> Dict[str, Any]:
        """Get all registered interfaces."""
        return self._interfaces.copy()

    def get_interface_names(self) -> list[str]:
        """Get names of all registered interfaces."""
        return list(self._interfaces.keys())

    def set_trainer_id(
        self,
        interface_name: str,
        trainer_id: int | str | list[int | str],
    ):
        """Set the trainer ID for a specific interface.

        Historically this stored a single integer ID; it is now permitted to
        supply a string username (eg. "alice#1234") or a list containing any
        combination of integers and strings.  The registry will normalise lists
        of values and the matching logic in :meth:`is_trainer` will compare the
        provided user identifier against all entries, returning ``True`` if any
        element matches.
        """
        # Normalise lists/tuples so storage is simple and comparison easier.
        if isinstance(trainer_id, (list, tuple)):
            # convert each to str so we can compare universally later
            normalized = [str(x).strip() for x in trainer_id if x is not None]
            self._trainer_ids[interface_name] = normalized
        else:
            # leave scalars as-is; they may be int or str
            self._trainer_ids[interface_name] = trainer_id
        log_debug(
            f"[interfaces_registry] Set trainer ID {trainer_id} for interface {interface_name}"
        )

    def replace_trainer_ids(self, mapping: Dict[str, int | str]) -> None:
        """Replace all trainer IDs with the provided mapping."""
        self._trainer_ids = dict(mapping)
        log_debug(
            f"[interfaces_registry] Replaced trainer IDs ({len(self._trainer_ids)} entries)"
        )

    def get_trainer_id(
        self, interface_name: str
    ) -> Optional[int | str | list[int | str]]:
        """Get the trainer ID for a specific interface.

        The value returned may be:

        * ``None`` if no trainer has been set.
        * An ``int`` (traditional numeric ID).
        * A ``str`` username.
        * A ``list`` containing a mix of ``int``/``str`` identifiers when multiple
          entries were configured.
        """
        return self._trainer_ids.get(interface_name)

    def is_trainer(self, interface_name: str, user_id: int | str) -> bool:
        """Check if a user_id is the trainer for a specific interface.

        Comparison is permissive: the stored trainer identifier(s) may be
        numeric or string, and the passed ``user_id`` will be coerced to string
        for comparison.  If multiple trainers were configured (stored as a
        list), the method returns ``True`` if *any* element matches.
        """
        trainer_id = self.get_trainer_id(interface_name)
        if trainer_id is None:
            return False

        user_str = str(user_id).strip()
        if isinstance(trainer_id, (list, tuple)):
            for t in trainer_id:
                if user_str == str(t).strip():
                    return True
            return False
        # scalar case
        return user_str == str(trainer_id).strip()

    def get_default_interface(self) -> Optional[str]:
        """Get the name of the first available interface (fallback to webui if any)."""
        names = self.get_interface_names()
        if not names:
            return None
        # Prefer webui if available, otherwise return first
        if "webui" in names:
            return "webui"
        return names[0]

    def get_default_interface_or_error(self) -> str:
        """Get default interface or raise error if none available."""
        interface = self.get_default_interface()
        if interface is None:
            raise ValueError("No interfaces are currently registered")
        return interface


# Global registry instance
_interface_registry = InterfaceRegistry()


def get_interface_registry() -> InterfaceRegistry:
    """Get the global instance of the interfaces registry."""
    return _interface_registry
