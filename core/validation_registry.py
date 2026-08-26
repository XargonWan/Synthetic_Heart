# core/validation_registry.py
"""Central registry for component validation rules."""

from typing import Dict, List, Set, Any
from core.logging_utils import log_debug, log_warning


class ValidationRule:
    """Represents a validation rule for an action type."""

    def __init__(
        self,
        action_type: str,
        required_fields: List[str] = None,
        custom_validator: callable = None,
        component_name: str = None,
        one_of_groups: List[List[str]] = None,
    ):
        self.action_type = action_type
        self.required_fields = required_fields or []
        self.custom_validator = custom_validator
        self.component_name = component_name or "unknown"
        # OR groups: within each group at least one field must be present;
        # every group must pass (AND across groups).
        self.one_of_groups = [list(g) for g in (one_of_groups or [])]

    def validate(self, payload: Dict[str, Any]) -> List[str]:
        """Validate payload against this rule. Returns list of error messages."""
        errors = []

        # Check required fields
        for field in self.required_fields:
            if field not in payload:
                errors.append(
                    f"Missing required field '{field}' for action '{self.action_type}'"
                )
            elif payload[field] is None or payload[field] == "":
                errors.append(
                    f"Field '{field}' cannot be empty for action '{self.action_type}'"
                )

        # Check OR groups (at least one field per group must be present)
        for group in self.one_of_groups:
            if not group:
                continue
            if any(
                payload.get(field) is not None and payload.get(field) != ""
                for field in group
            ):
                continue
            errors.append(
                f"At least one of [{', '.join(group)}] is required for action "
                f"'{self.action_type}'"
            )

        # Run custom validator if provided
        if self.custom_validator and callable(self.custom_validator):
            try:
                custom_errors = self.custom_validator(payload)
                if isinstance(custom_errors, list):
                    errors.extend(custom_errors)
                elif isinstance(custom_errors, str):
                    errors.append(custom_errors)
            except Exception as e:
                log_warning(f"Custom validator for {self.action_type} failed: {e}")
                errors.append(
                    f"Custom validation failed for action '{self.action_type}'"
                )

        return errors


class ValidationRegistry:
    """Central registry for component validation rules."""

    def __init__(self):
        self._rules: Dict[str, List[ValidationRule]] = {}
        self._registered_components: Set[str] = set()
        # Common LLM response metadata keys that should be silently
        # ignored rather than treated as actions (e.g. "meta", "message").
        self._response_metadata_keys: Set[str] = {
            "meta",
            "meta.autonomous",
            "metadata",
            "rationale",
            "thoughtSignature",
            # Model introspection / chain-of-thought keys — never actions
            "thoughts",
            "thought",
            "thinking",
            "reasoning",
            "internal_thoughts",
            "chain_of_thought",
            # Extra structural keys some models emit alongside actions
            "brief",
            "source",
        }
        self._response_metadata_by_component: Dict[str, Set[str]] = {}
        # Alias mapping: alias_name -> resolver(payload) -> Optional[canonical_action_type]
        # Resolvers are callables that receive the action payload and return a
        # canonical action_type (string) or None if they cannot resolve.
        self._aliases: Dict[str, Any] = {}

    def register_action_alias(self, alias_name: str, resolver: callable):
        """Register a resolver for a legacy alias action name.

        The resolver should accept a single arg (payload dict) and return a
        canonical action type (str) or None.
        """
        if not callable(resolver):
            raise ValueError("resolver must be callable")
        log_debug(f"[ValidationRegistry] Registering alias resolver for '{alias_name}'")
        self._aliases[alias_name] = resolver

    def resolve_action_alias(self, alias_name: str, payload: dict) -> Any:
        """Attempt to resolve an alias name using a registered resolver.

        Returns the canonical action type (str) or None if unresolved.
        """
        resolver = self._aliases.get(alias_name)
        if not resolver:
            return None
        try:
            return resolver(payload or {})
        except Exception as e:
            log_warning(
                f"[ValidationRegistry] Alias resolver for '{alias_name}' failed: {e}"
            )
            return None

    def register_component_rules(
        self, component_name: str, rules: List[ValidationRule]
    ):
        """Register validation rules for a component."""
        log_debug(
            f"[ValidationRegistry] Registering {len(rules)} rules for component '{component_name}'"
        )

        self._registered_components.add(component_name)

        for rule in rules:
            rule.component_name = component_name
            action_type = rule.action_type

            if action_type not in self._rules:
                self._rules[action_type] = []

            self._rules[action_type].append(rule)
            log_debug(
                f"[ValidationRegistry] Registered rule for action '{action_type}' from component '{component_name}'"
            )

    def unregister_component(self, component_name: str):
        """Remove all rules for a component."""
        if component_name not in self._registered_components:
            return

        log_debug(f"[ValidationRegistry] Unregistering component '{component_name}'")

        # Remove all rules from this component
        for action_type in list(self._rules.keys()):
            self._rules[action_type] = [
                rule
                for rule in self._rules[action_type]
                if rule.component_name != component_name
            ]
            # Remove empty action types
            if not self._rules[action_type]:
                del self._rules[action_type]

        self._registered_components.discard(component_name)

        # Remove response metadata keys registered by this component
        if component_name in self._response_metadata_by_component:
            keys = self._response_metadata_by_component.pop(component_name, set())
            self._response_metadata_keys.difference_update(keys)

    def get_validation_rules(self, action_type: str) -> List[ValidationRule]:
        """Get all validation rules for an action type."""
        return self._rules.get(action_type, [])

    def validate_action_payload(
        self, action_type: str, payload: Dict[str, Any]
    ) -> List[str]:
        """Validate payload against all registered rules for the action type."""
        errors = []
        rules = self.get_validation_rules(action_type)

        for rule in rules:
            rule_errors = rule.validate(payload)
            errors.extend(rule_errors)

        return errors

    def get_supported_action_types(self) -> Set[str]:
        """Get all action types that have validation rules."""
        return set(self._rules.keys())

    def get_registered_components(self) -> Set[str]:
        """Get all registered component names."""
        return self._registered_components.copy()

    def register_response_metadata_keys(
        self, component_name: str, keys: List[str]
    ) -> None:
        """Register allowed top-level response metadata keys for a component.

        These keys are permitted in LLM JSON responses alongside "actions" and
        will not be treated as invalid actions.
        """
        if not keys:
            return
        normalized = {str(k) for k in keys if str(k).strip()}
        if not normalized:
            return

        existing = self._response_metadata_by_component.get(component_name, set())
        existing.update(normalized)
        self._response_metadata_by_component[component_name] = existing
        self._response_metadata_keys.update(normalized)

        log_debug(
            f"[ValidationRegistry] Registered response metadata keys for '{component_name}': {sorted(normalized)}"
        )

    def get_response_metadata_keys(self) -> Set[str]:
        """Return the union of allowed top-level response metadata keys."""
        return set(self._response_metadata_keys)

    def clear(self):
        """Clear all registered rules (for testing)."""
        self._rules.clear()
        self._registered_components.clear()
        self._response_metadata_keys.clear()
        self._response_metadata_by_component.clear()


# Global validation registry instance
_validation_registry = ValidationRegistry()


def get_validation_registry() -> ValidationRegistry:
    """Get the global validation registry instance."""
    return _validation_registry


def register_component_validation_rules(
    component_name: str, action_rules: Dict[str, Dict[str, Any]]
):
    """Helper function to register validation rules from component JSON configuration.

    Args:
        component_name: Name of the component
        action_rules: Dictionary mapping action_type to rule configuration
                     Example: {
                         "send_message": {
                             "required_fields": ["text", "chat_id"],
                             "custom_validator": some_function
                         }
                     }
    """
    rules = []

    for action_type, rule_config in action_rules.items():
        required_fields = rule_config.get("required_fields", [])
        custom_validator = rule_config.get("custom_validator")
        one_of_groups = rule_config.get("one_of") or rule_config.get("one_of_groups")

        rule = ValidationRule(
            action_type=action_type,
            required_fields=required_fields,
            custom_validator=custom_validator,
            component_name=component_name,
            one_of_groups=one_of_groups,
        )
        rules.append(rule)

    _validation_registry.register_component_rules(component_name, rules)


# Register a default alias resolver for legacy 'message_send' that delegates
# to concrete interface actions. This lives in the registry (not in the
# action parser) so it's configurable and centralized.

# NOTE: We intentionally do NOT register a default resolver for legacy
# action names like 'message_send'. Converting unknown action names silently
# would hide potential errors and prevent the corrector from asking the LLM
# to fix action names/parameters. If projects need special aliasing behavior,
# they should register alias resolvers explicitly via
# `get_validation_registry().register_action_alias(alias_name, resolver)`.

# Example (for documentation or plugin initialization):
#    get_validation_registry().register_action_alias('message_send', my_resolver)


__all__ = [
    "ValidationRule",
    "ValidationRegistry",
    "get_validation_registry",
    "register_component_validation_rules",
]
