# core/component_auto_registration.py
"""Automatic registration of component validation rules from existing plugins/interfaces."""

from typing import Any, Dict
from core.logging_utils import log_debug, log_info, log_warning
from core.validation_registry import get_validation_registry, ValidationRule
from core.component_registry import get_component_registry_manager


def auto_register_plugin_validation_rules():
    """Automatically register validation rules from existing plugins."""
    try:
        from core.action_parser import _load_action_plugins

        plugins = _load_action_plugins()

        validation_registry = get_validation_registry()
        component_manager = get_component_registry_manager()

        for plugin in plugins:
            plugin_name = getattr(plugin, "__class__", type(plugin)).__name__

            try:
                # Check if plugin has get_supported_actions method
                if hasattr(plugin, "get_supported_actions"):
                    actions = plugin.get_supported_actions()
                    if isinstance(actions, dict):
                        _register_actions_from_dict(
                            plugin_name, "plugin", actions, validation_registry
                        )
                        log_debug(
                            f"[auto_registration] Registered validation rules for plugin '{plugin_name}'"
                        )

                # Also check legacy get_supported_action_types for completeness
                elif hasattr(plugin, "get_supported_action_types"):
                    action_types = plugin.get_supported_action_types()
                    if isinstance(action_types, (list, set, tuple)):
                        # For legacy plugins without detailed action info, just register the action types
                        actions_dict = {
                            action_type: {"required_fields": []}
                            for action_type in action_types
                        }
                        _register_actions_from_dict(
                            plugin_name, "plugin", actions_dict, validation_registry
                        )
                        log_debug(
                            f"[auto_registration] Registered basic validation rules for legacy plugin '{plugin_name}'"
                        )

            except Exception as e:
                log_warning(
                    f"[auto_registration] Error registering plugin '{plugin_name}': {e}"
                )

        log_info(
            f"[auto_registration] Completed auto-registration for {len(plugins)} plugins"
        )

    except Exception as e:
        log_warning(f"[auto_registration] Error during plugin auto-registration: {e}")


def auto_register_interface_validation_rules():
    """Automatically register validation rules from existing interfaces."""
    try:
        from core.core_initializer import INTERFACE_REGISTRY

        validation_registry = get_validation_registry()

        for interface_name, interface in INTERFACE_REGISTRY.items():
            try:
                # Check if interface has get_supported_actions method
                if hasattr(interface, "get_supported_actions"):
                    actions = interface.get_supported_actions()
                    if isinstance(actions, dict):
                        _register_actions_from_dict(
                            interface_name, "interface", actions, validation_registry
                        )
                        log_debug(
                            f"[auto_registration] Registered validation rules for interface '{interface_name}'"
                        )

                # Also check get_supported_action_types
                elif hasattr(interface, "get_supported_action_types"):
                    action_types = interface.get_supported_action_types()
                    if isinstance(action_types, (list, set, tuple)):
                        # For legacy interfaces without detailed action info
                        actions_dict = {
                            action_type: {"required_fields": []}
                            for action_type in action_types
                        }
                        _register_actions_from_dict(
                            interface_name,
                            "interface",
                            actions_dict,
                            validation_registry,
                        )
                        log_debug(
                            f"[auto_registration] Registered basic validation rules for legacy interface '{interface_name}'"
                        )

            except Exception as e:
                log_warning(
                    f"[auto_registration] Error registering interface '{interface_name}': {e}"
                )

        log_info(
            f"[auto_registration] Completed auto-registration for {len(INTERFACE_REGISTRY)} interfaces"
        )

    except Exception as e:
        log_warning(
            f"[auto_registration] Error during interface auto-registration: {e}"
        )


def _register_actions_from_dict(
    component_name: str,
    component_type: str,
    actions: Dict[str, Any],
    validation_registry,
):
    """Helper function to register validation rules from actions dictionary."""
    rules = []

    for action_type, action_config in actions.items():
        required_fields = []

        # Handle different formats of action configuration
        if isinstance(action_config, dict):
            required_fields = action_config.get("required_fields", [])

            # Some plugins might use different field names, try to normalize
            if not required_fields:
                # Check for alternative field names
                required_fields = action_config.get("required", [])

        elif isinstance(action_config, (list, tuple)):
            # Some plugins might return a list of required fields directly
            required_fields = list(action_config)

        # Only create rule if we have required fields
        if required_fields:
            rule = ValidationRule(
                action_type=action_type,
                required_fields=required_fields,
                component_name=component_name,
            )
            rules.append(rule)

    if rules:
        validation_registry.register_component_rules(component_name, rules)


def auto_register_all_components():
    """Auto-register validation rules from all existing components (plugins and interfaces)."""
    log_info("[auto_registration] Starting automatic component validation registration")

    auto_register_plugin_validation_rules()
    auto_register_interface_validation_rules()

    # Log summary
    validation_registry = get_validation_registry()
    registered_components = validation_registry.get_registered_components()
    supported_actions = validation_registry.get_supported_action_types()

    log_info(
        f"[auto_registration] Registration complete: {len(registered_components)} components, {len(supported_actions)} action types"
    )


__all__ = [
    "auto_register_plugin_validation_rules",
    "auto_register_interface_validation_rules",
    "auto_register_all_components",
]
