# core/interfaces_registry.py

"""
Registry to manage active interfaces without hardcoded dependencies.
"""

from typing import Dict, Any, Optional, Callable
from core.logging_utils import log_debug, log_info, log_warning, log_error

class InterfaceRegistry:
    """Central registry for all active interfaces."""
    
    def __init__(self):
        self._interfaces: Dict[str, Any] = {}
        self._interface_configs: Dict[str, Dict[str, Any]] = {}
        self._trainer_ids: Dict[str, int] = {}
        
    def register_interface(self, name: str, interface_instance: Any, config: Optional[Dict[str, Any]] = None):
        """Register a new interface."""
        self._interfaces[name] = interface_instance
        if config:
            self._interface_configs[name] = config
        log_debug(f"[interfaces_registry] Registered interface: {name}")
    
    def unregister_interface(self, name: str):
        """Remove an interface from the registry."""
        if name in self._interfaces:
            del self._interfaces[name]
        if name in self._interface_configs:
            del self._interface_configs[name]
        if name in self._trainer_ids:
            del self._trainer_ids[name]
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
    
    def set_trainer_id(self, interface_name: str, trainer_id: int):
        """Set the trainer ID for a specific interface."""
        self._trainer_ids[interface_name] = trainer_id
        log_debug(f"[interfaces_registry] Set trainer ID {trainer_id} for interface {interface_name}")
    
    def get_trainer_id(self, interface_name: str) -> Optional[int]:
        """Get the trainer ID for a specific interface."""
        return self._trainer_ids.get(interface_name)
    
    def is_trainer(self, interface_name: str, user_id: int | str) -> bool:
        """Check if a user_id is the trainer for a specific interface."""
        trainer_id = self.get_trainer_id(interface_name)
        if trainer_id is None:
            return False

        return str(user_id).strip() == str(trainer_id).strip()
    
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
