# core/component_registry.py
"""Component registration and validation management."""

from typing import Dict, Any, List, Optional
from core.logging_utils import log_debug, log_info, log_warning
from core.validation_registry import get_validation_registry, ValidationRule


class ComponentDescriptor:
    """Describes a component and its validation rules."""

    def __init__(
        self, name: str, component_type: str, actions: Dict[str, Dict[str, Any]] = None
    ):
        self.name = name
        self.component_type = component_type  # "plugin", "interface", "llm_engine"
        self.actions = actions or {}

    @classmethod
    def from_json(
        cls, component_name: str, component_type: str, json_data: Dict[str, Any]
    ):
        """Create ComponentDescriptor from JSON configuration."""
        actions = json_data.get("actions", {})
        return cls(component_name, component_type, actions)

    def register_validation_rules(self):
        """Register validation rules for this component."""
        if not self.actions:
            log_debug(
                f"[ComponentDescriptor] No actions defined for component '{self.name}'"
            )
            return

        validation_registry = get_validation_registry()
        rules = []

        for action_type, action_config in self.actions.items():
            required_fields = action_config.get("required_fields", [])

            if required_fields:
                rule = ValidationRule(
                    action_type=action_type,
                    required_fields=required_fields,
                    component_name=self.name,
                )
                rules.append(rule)
                log_debug(
                    f"[ComponentDescriptor] Created validation rule for '{action_type}' with required fields: {required_fields}"
                )

        if rules:
            validation_registry.register_component_rules(self.name, rules)
            log_info(
                f"[ComponentDescriptor] Registered {len(rules)} validation rules for component '{self.name}'"
            )

    def unregister_validation_rules(self):
        """Unregister validation rules for this component."""
        validation_registry = get_validation_registry()
        validation_registry.unregister_component(self.name)
        log_info(
            f"[ComponentDescriptor] Unregistered validation rules for component '{self.name}'"
        )


class ComponentRegistryManager:
    """Manages component registration and validation rule setup."""

    def __init__(self):
        self._registered_components: Dict[str, ComponentDescriptor] = {}

    def register_component_from_json(
        self, component_name: str, component_type: str, json_config: Dict[str, Any]
    ) -> ComponentDescriptor:
        """Register a component from its JSON configuration.

        Args:
            component_name: Name of the component
            component_type: Type of component ("plugin", "interface", "llm_engine")
            json_config: JSON configuration containing action definitions

        Returns:
            ComponentDescriptor: The registered component descriptor
        """
        log_debug(
            f"[ComponentRegistryManager] Registering component '{component_name}' of type '{component_type}'"
        )

        # Create descriptor from JSON
        descriptor = ComponentDescriptor.from_json(
            component_name, component_type, json_config
        )

        # Register validation rules
        descriptor.register_validation_rules()

        # Store in registry
        self._registered_components[component_name] = descriptor

        log_info(
            f"[ComponentRegistryManager] Successfully registered component '{component_name}'"
        )
        return descriptor

    def unregister_component(self, component_name: str):
        """Unregister a component and remove its validation rules."""
        if component_name not in self._registered_components:
            log_warning(
                f"[ComponentRegistryManager] Component '{component_name}' not found for unregistration"
            )
            return

        log_debug(
            f"[ComponentRegistryManager] Unregistering component '{component_name}'"
        )

        descriptor = self._registered_components[component_name]
        descriptor.unregister_validation_rules()

        del self._registered_components[component_name]

        log_info(
            f"[ComponentRegistryManager] Successfully unregistered component '{component_name}'"
        )

    def get_component(self, component_name: str) -> Optional[ComponentDescriptor]:
        """Get a registered component descriptor."""
        return self._registered_components.get(component_name)

    def get_all_components(self) -> Dict[str, ComponentDescriptor]:
        """Get all registered components."""
        return self._registered_components.copy()

    def list_component_names(self) -> List[str]:
        """Get list of all registered component names."""
        return list(self._registered_components.keys())


# Global component registry manager
_component_registry_manager = ComponentRegistryManager()


def get_component_registry_manager() -> ComponentRegistryManager:
    """Get the global component registry manager."""
    return _component_registry_manager


def register_component_validation(
    component_name: str, component_type: str, json_config: Dict[str, Any]
) -> ComponentDescriptor:
    """Helper function to register component validation rules from JSON config.

    Expected JSON format:
    {
        "actions": {
            "action_type": {
                "required_fields": ["field1", "field2"],
                "description": "Optional description"
            },
            ...
        }
    }
    """
    return _component_registry_manager.register_component_from_json(
        component_name, component_type, json_config
    )


def unregister_component_validation(component_name: str):
    """Helper function to unregister component validation rules."""
    _component_registry_manager.unregister_component(component_name)


__all__ = [
    "ComponentDescriptor",
    "ComponentRegistryManager",
    "get_component_registry_manager",
    "register_component_validation",
    "unregister_component_validation",
]
