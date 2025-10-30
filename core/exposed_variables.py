"""Engine per le "exposed variables" — variabili di configurazione esposte a UI/API.

Questo modulo offre una API semplice e centralizzata per registrare variabili
esposte dai plugin o dal core. Si appoggia sul `config_registry` esistente per la
persistenza e il caricamento, ma aggiunge metadati UI, validazione e flags
(readonly, dangerous, advanced) in un unico posto.

Design goals:
- Easy registration: register_exposed_var(...) per dichiarare la variabile.
- Consistenza: la variabile viene registrata anche sul `config_registry`.
- Validazione: optional validator (regex/range/choices/callable).
- Flags/UI: label, description, ui_type, scope e flags.
- Safe persistence: set_value usa `config_registry.set_value` (che gestisce
  persona special-case e retry in background).

Nota: il registry non duplica la persistenza DB — delega al `config_registry`.
"""

from typing import Any, Callable, Dict, Optional, Iterable
import re

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_info, log_warning, log_error


class ValidationError(ValueError):
    pass


class ExposedVarDefinition:
    def __init__(
        self,
        key: str,
        label: str,
        default: Any = "",
        value_type: type = str,
        ui_type: str = "string",
        description: str = "",
        scope: str = "global",
        readonly: bool = False,
        dangerous: bool = False,
        advanced: bool = False,
        needs_component_reload: bool = False,
        hidden: bool = False,
        validator: Optional[Dict] | Optional[Callable[[Any], bool]] = None,
        tags: Optional[Iterable[str]] = None,
    ):
        self.key = key
        self.label = label
        self.default = default
        self.value_type = value_type
        self.ui_type = ui_type
        self.description = description
        self.scope = scope
        self.readonly = readonly
        self.dangerous = dangerous
        self.advanced = advanced
        # If True, changing this variable may require reloading the owning
        # component (or more). Default is False to avoid unnecessary reloads.
        self.needs_component_reload = bool(needs_component_reload)
        # If True, the variable should be hidden from graphical UI lists
        # but still available via the API. Default False.
        self.hidden = bool(hidden)
        self.validator = validator
        self.tags = set(tags or [])

    def validate(self, value: Any) -> None:
        # Type check
        if value is None:
            return
        if self.value_type is not None and not callable(self.value_type):
            try:
                # Attempt simple cast for primitives
                _ = self.value_type(value)
            except Exception as e:
                raise ValidationError(f"Value for {self.key} must be {self.value_type}: {e}")

        # Validator can be a dict describing rules or a callable
        if self.validator is None:
            return
        if callable(self.validator):
            try:
                ok = self.validator(value)
            except Exception as e:
                raise ValidationError(f"Validator for {self.key} raised: {e}")
            if not ok:
                raise ValidationError(f"Validator refused value for {self.key}")
            return

        # Dict-based validators
        if isinstance(self.validator, dict):
            v = self.validator
            if 'regex' in v:
                if not re.match(v['regex'], str(value)):
                    raise ValidationError(f"Value for {self.key} does not match pattern")
            if 'min' in v:
                if float(value) < float(v['min']):
                    raise ValidationError(f"Value for {self.key} below min {v['min']}")
            if 'max' in v:
                if float(value) > float(v['max']):
                    raise ValidationError(f"Value for {self.key} above max {v['max']}")
            if 'choices' in v:
                if value not in v['choices']:
                    raise ValidationError(f"Value for {self.key} not in allowed choices")


class ExposedVariableRegistry:
    def __init__(self):
        self._defs: Dict[str, ExposedVarDefinition] = {}

    def register(self, definition: ExposedVarDefinition) -> None:
        """Register a variable and ensure it's present in the config system.

        This will call `config_registry.get_var(...)` which registers the
        definition and exposes it to the rest of the system. If an env override
        exists, `config_registry` will take precedence.
        """
        if definition.key in self._defs:
            log_warning(f"[exposed_vars] Overwriting existing definition for {definition.key}")
        self._defs[definition.key] = definition

        # Register in config_registry so UI and persistence work uniformly.
        try:
            # Map basic ui types to config_registry value_type when reasonable
            value_type = definition.value_type
            tags = list(definition.tags) + ['exposed']
            # Use get_var to register and ensure the config machinery knows about it
            config_registry.get_var(
                definition.key,
                definition.default,
                label=definition.label,
                description=definition.description,
                value_type=value_type,
                group=definition.scope,
                component='exposed',
                advanced=definition.advanced,
                tags=tags,
                needs_component_reload=definition.needs_component_reload,
                hidden=definition.hidden,
            )
            log_info(f"[exposed_vars] Registered exposed var {definition.key}")
        except Exception as e:
            log_error(f"[exposed_vars] Failed to register {definition.key} in config_registry: {e}")

    def get_definition(self, key: str) -> Optional[ExposedVarDefinition]:
        return self._defs.get(key)

    def get_value(self, key: str) -> Any:
        return config_registry.get_value(key)

    async def set_value(self, key: str, value: Any) -> None:
        """Validate and set the exposed variable via the config registry.

        Respects readonly flag and runs validator if present. Uses
        `config_registry.set_value` which provides persistence and persona
        special-casing.
        """
        definition = self._defs.get(key)
        if not definition:
            raise KeyError(f"Unknown exposed variable: {key}")
        if definition.readonly:
            raise PermissionError(f"Exposed variable {key} is read-only")

        # Validate
        try:
            definition.validate(value)
        except ValidationError as ve:
            log_warning(f"[exposed_vars] Validation error for {key}: {ve}")
            raise

        # Delegate to config_registry for persistence and notification
        await config_registry.set_value(key, value)


# Singleton registry
exposed_vars = ExposedVariableRegistry()


def register_exposed_var(
    key: str,
    label: str,
    default: Any = "",
    value_type: type = str,
    ui_type: str = "string",
    description: str = "",
    scope: str = "global",
    readonly: bool = False,
    dangerous: bool = False,
    advanced: bool = False,
    needs_component_reload: bool = False,
    hidden: bool = False,
    validator: Optional[Dict] | Optional[Callable[[Any], bool]] = None,
    tags: Optional[Iterable[str]] = None,
) -> ExposedVarDefinition:
    d = ExposedVarDefinition(
        key=key,
        label=label,
        default=default,
        value_type=value_type,
        ui_type=ui_type,
        description=description,
        scope=scope,
        readonly=readonly,
        dangerous=dangerous,
        advanced=advanced,
        needs_component_reload=needs_component_reload,
        hidden=hidden,
        validator=validator,
        tags=tags,
    )
    exposed_vars.register(d)
    return d
