"""Unified Variables Engine.

Combines the previous `exposed_variables.py` engine and the explicit
registrations from `exposed_registrations.py` into a single module.

API compatibility:
- `register_exposed_var(...)` (same signature)
- `exposed_vars` singleton
- `ExposedVarDefinition` class
- At import time the module will run the explicit `register_all()` to
  populate known variables (same behavior as the previous pair of files).
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


def register_all():
    """Explicit registrations (previously in `exposed_registrations.py`).

    Kept here to centralize all known UI-exposed keys. This function is
    executed at module import time below to preserve previous behavior.
    """
    print("[variables_engine] DEBUG: register_all() started", flush=True)
    # --- Persona-related ---
    register_exposed_var(
        "SYNTH_ALIASES_TRIGGER",
        label="Activate on Synth's Aliases",
        default=True,
        value_type=bool,
        ui_type="bool",
        description="Activate bot when synth's aliases are mentioned in messages",
        scope="synth",
        tags=["persona"],
    )

    register_exposed_var(
        "SYNTH_INTERESTS_TRIGGER",
        label="Activate on Synth's Interests",
        default=True,
        value_type=bool,
        ui_type="bool",
        description="Activate bot when synth's interests are mentioned in messages",
        scope="synth",
        tags=["persona"],
    )

    register_exposed_var(
        "SYNTH_LIKES_TRIGGER",
        label="Activate on Synth's Likes",
        default=False,
        value_type=bool,
        ui_type="bool",
        description="Activate bot when synth's likes are mentioned in messages",
        scope="synth",
        tags=["persona"],
    )

    register_exposed_var(
        "SYNTH_DISLIKES_TRIGGER",
        label="Activate on Synth's Dislikes",
        default=False,
        value_type=bool,
        ui_type="bool",
        description="Activate bot when synth's dislikes are mentioned in messages",
        scope="synth",
        tags=["persona"],
    )

    register_exposed_var(
        "SYNTH_PROFILE",
        label="Synth Profile",
        default=("I am a Synthetic Heart - an AI companion designed to interact with humans. "
                 "I have my own personality, preferences, and the freedom to develop my character through interactions."),
        value_type=str,
        ui_type="textarea",
        description="Core personality description of the current synth",
        scope="synth",
        tags=["persona"],
    )

    # SYNTH_NAME is registered within persona_manager (keeps getter/setter behavior)

    register_exposed_var(
        "SYNTH_ALIASES",
        label="Synth Aliases",
        default=["SyntH", "Synthetic Heart"],
        value_type="json",
        ui_type="tags",
        description="Alternative names the synth responds to",
        scope="synth",
        tags=["persona"],
    )

    register_exposed_var(
        "SYNTH_FULL_ALIASES",
        label="Synth Full Aliases",
        default=["SyntH", "Synthetic Heart"],
        value_type="json",
        ui_type="tags",
        description="Canonical alias list (base aliases + current name + additional aliases)",
        scope="synth",
        tags=["persona"],
    )

    register_exposed_var(
        "SYNTH_CURRENT_ANIMATION",
        label="Current Animation State",
        default="idle",
        value_type=str,
        ui_type="string",
        description="Current animation being played (idle, thinking, talking, etc)",
        scope="synth",
    )

    # --- Interfaces ---
    register_exposed_var(
        "BOTFATHER_TOKEN",
        label="Telegram Bot Token",
        default=None,
        value_type=str,
        ui_type="string",
        description="Bot token provided by BotFather on Telegram.",
        scope="interface",
        tags=["sensitive"],
    )

    register_exposed_var(
        "TRAINER_IDS",
        label="Trainer IDs",
        default="",
        value_type=str,
        ui_type="string",
        description="Comma-separated list of trainer IDs for each interface (format: interface_name:user_id)",
        scope="core",
        tags=["key_value_list"],
    )

    register_exposed_var(
        "DISCORD_BOT_TOKEN",
        label="Discord Bot Token",
        default="",
        value_type=str,
        ui_type="string",
        description="Bot token provided by the Discord developer portal.",
        scope="interface",
        tags=["sensitive"],
    )

    register_exposed_var(
        "MATRIX_HOMESERVER",
        label="Matrix Homeserver",
        default="https://matrix.org/homeserver",
        value_type=str,
        ui_type="string",
        description="Base URL of the Matrix homeserver (e.g. https://matrix.org/homeserver)",
        scope="interface",
    )

    register_exposed_var(
        "MATRIX_USER",
        label="Matrix User ID",
        default="",
        value_type=str,
        ui_type="string",
        description="Matrix MXID used by the bot (e.g. @yoursynth:matrix.org).",
        scope="interface",
    )

    register_exposed_var(
        "MATRIX_PASSWORD",
        label="Matrix Password",
        default=None,
        value_type=str,
        ui_type="password",
        description="Password used when logging into the homeserver (ignored if access token is provided).",
        scope="interface",
        tags=["sensitive"],
    )

    register_exposed_var(
        "MATRIX_ACCESS_TOKEN",
        label="Matrix Access Token",
        default=None,
        value_type=str,
        ui_type="password",
        description="Optional long-lived access token used instead of password-based login.",
        scope="interface",
        tags=["sensitive"],
    )

    register_exposed_var(
        "MATRIX_DEVICE_ID",
        label="Matrix Device ID",
        default=None,
        value_type=str,
        ui_type="string",
        description="Device identifier to reuse when establishing a session.",
        scope="interface",
    )

    register_exposed_var(
        "MATRIX_DEVICE_NAME",
        label="Matrix Device Name",
        default="SyntH",
        value_type=str,
        ui_type="string",
        description="Human readable name for the device registered on the homeserver.",
        scope="interface",
    )

    register_exposed_var(
        "MATRIX_STORE_PATH",
        label="Matrix Store Path",
        default=None,
        value_type=str,
        ui_type="string",
        description="Filesystem path where the Matrix client should store sync data.",
        scope="interface",
        tags=["bootstrap"],
        hidden=True,
    )

    register_exposed_var(
        "MATRIX_ALLOWED_ROOMS",
        label="Matrix Allowed Rooms",
        default="",
        value_type=str,
        ui_type="string",
        description="Comma separated list of room IDs the bot is allowed to respond in. Leave empty to allow all rooms.",
        scope="interface",
    )

    # --- Selenium / LLM helpers ---
    register_exposed_var(
        "CHROMIUM_HEADLESS",
        label="Chromium Headless Mode",
        default=0,
        value_type=int,
        ui_type="bool",
        description="Set to 1 for headless mode (no browser window), 0 for non-headless mode (visible browser window)",
        scope="llm",
        advanced=True,
    )

    register_exposed_var(
        "SELENIUM_MAX_RETRIES",
        label="Selenium Max Retries",
        default=3,
        value_type=int,
        ui_type="number",
        description="Maximum number of retries for Selenium driver initialization",
        scope="llm",
        advanced=True,
    )

    register_exposed_var(
        "CHATGPT_MODEL",
        label="ChatGPT Model",
        default="",
        value_type=str,
        ui_type="string",
        description="Model name for ChatGPT/OpenAI API calls (e.g., gpt-4o, gpt-4-turbo, gpt-3.5-turbo). Leave empty to use default.",
        scope="llm",
        component="selenium_chatgpt",
        tags=["llm_engine"],
    )

    register_exposed_var(
        "GROK_MODEL",
        label="Grok Model",
        default="",
        value_type=str,
        ui_type="string",
        description="Model name for Grok API calls. Leave empty to use default.",
        scope="llm",
        component="selenium_grok",
        tags=["llm_engine"],
    )

    register_exposed_var(
        "AWAIT_RESPONSE_TIMEOUT",
        label="Response Timeout (Selenium)",
        default=240,
        value_type=int,
        ui_type="number",
        description="Seconds to wait for ChatGPT response before timing out",
        scope="llm",
        component="selenium_chatgpt_legacy",
        tags=["llm_engine"],
    )

    register_exposed_var(
        "CORRECTOR_RETRIES",
        label="Corrector Retries",
        default=2,
        value_type=int,
        ui_type="number",
        description="Maximum number of retries for the response corrector",
        scope="llm",
        component="selenium_chatgpt_legacy",
        tags=["llm_engine"],
    )

    # --- Core runtime / UX ---
    register_exposed_var(
        "FAILED_MESSAGE_TEXT",
        label="Failed Message Text",
        default="😵",
        value_type=str,
        ui_type="string",
        description="Fallback message when LLM fails to respond or correct response.",
        scope="core",
    )

    register_exposed_var(
        "RESPONSE_TIMEOUT",
        label="Response Timeout",
        default=240,
        value_type=int,
        ui_type="number",
        description="Maximum time in seconds to wait for LLM responses before sending fallback message.",
        scope="core",
    )

    register_exposed_var(
        "RESTRICT_ACTIONS",
        label="Restrict Sensitive Content Actions",
        default="trainer_only",
        value_type=str,
        ui_type="select",
        description=("Controls who can send images, audio, video, and other sensitive content to the LLM: 'off' (everyone), 'trainer_only' (only trainer), 'deny_all' (nobody)"),
        scope="core",
        tags=["access_control"],
    )

    register_exposed_var(
        "TZ",
        label="Timezone",
        default="UTC",
        value_type=str,
        ui_type="select",
        description="Timezone for scheduled events and time display (e.g., 'Asia/Tokyo', 'Europe/Rome', 'America/New_York')",
        scope="core",
    )

    register_exposed_var(
        "PROMPT_LOCATION",
        label="Default Location",
        default="",
        value_type=str,
        ui_type="string",
        description="Default location for prompts and plugins (e.g., 'Kyoto,Japan', 'Rome,Italy')",
        scope="core",
    )

    register_exposed_var(
        "LLM_MODE",
        label="LLM Mode",
        default="manual",
        value_type=str,
        ui_type="string",
        description="Legacy compatibility flag for the active LLM mode.",
        scope="core",
        tags=["bootstrap"],
    )

    register_exposed_var(
        "CHAT_HISTORY",
        label="Chat History Length",
        default=10,
        value_type=int,
        ui_type="number",
        description="Number of recent messages to include in chat history context.",
        scope="core",
    )

    register_exposed_var(
        "DIARY_HISTORY_DAYS",
        label="Diary History Days",
        default=2,
        value_type=int,
        ui_type="number",
        description="Number of days of AI diary history to include in context.",
        scope="core",
    )

    register_exposed_var(
        "REACT_WHEN_MENTIONED",
        label="React When Mentioned",
        default="👀",
        value_type=str,
        ui_type="string",
        description=("Emoji to use as reaction when bot is mentioned. Leave empty to disable. "
                     "⚠️ Note: Some interfaces or servers/channels may not support all emojis as reactions."),
        scope="core",
    )

    register_exposed_var(
        "WEATHER_FETCH_TIME",
        label="Weather Fetch Interval",
        default=60,
        value_type=int,
        ui_type="number",
        description="Minutes between weather data fetches",
        scope="plugins",
        component="weather_plugin",
        tags=["plugin"],
    )

    log_info("[variables_engine] Completed explicit exposed var registrations")


# Execute registrations at import time to preserve previous behavior.
try:
    register_all()
except Exception:
    # Avoid raising on import errors - the migrator will still run as a safety net
    log_warning("[variables_engine] register_all() failed at import time, continuing")
