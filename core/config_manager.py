"""Central configuration registry that unifies environment, database and defaults.

This module exposes a singleton ``config_registry`` which components can use to
declare their configuration variables.  Each variable supports the following
precedence order when resolving its value:

1. Environment variable (strongest). When present it overrides the database
   value, is marked as read-only in the UI and is persisted back to the
   database for visibility.
2. Database value (persisted by the user through the Web UI or API).
3. Hard-coded default defined by the component. Falling back to the default
   emits a warning so operators know persistence failed.

Settings can be registered by the core, interfaces or LLM engines.  The registry
keeps metadata (label, description, component, whether a variable is advanced or
sensitive, etc.) so the Web UI can render a cohesive settings dashboard.

The registry offers synchronous ``get_value`` for modules that need to resolve
configuration during import time and asynchronous ``set_value`` for runtime
updates coming from the API/UI.  Updates trigger registered listeners so
components can reconfigure themselves immediately when possible.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Union

from core.logging_utils import log_debug, log_info, log_warning, log_error


ValueType = Union[type, Callable[[str], Any], str]


class ConfigVar:
    """
    A smart config variable that automatically updates when config changes.

    This class wraps a config key and always returns the current value from
    the registry. No need to manually add listeners or update global variables.

    Usage:
        TOKEN = ConfigVar("MY_TOKEN")

        # Later, use it like a normal variable:
        if TOKEN:  # Calls __bool__
            do_something(str(TOKEN))  # Calls __str__
    """

    def __init__(self, key: str, registry: Optional["ConfigRegistry"] = None):
        self._key = key
        self._registry = registry  # Will be set after registry is created

    def _get_value(self) -> Any:
        """Get current value from registry."""
        if self._registry is None:
            # Lazy import to avoid circular dependency
            from core.config_manager import config_registry

            self._registry = config_registry
        return self._registry.get_value(self._key, "")

    def __str__(self) -> str:
        return str(self._get_value())

    def __repr__(self) -> str:
        return f"ConfigVar({self._key!r}={self._get_value!r})"

    def __bool__(self) -> bool:
        """Allow using in if statements: if TOKEN: ..."""
        value = self._get_value()
        return bool(value) if value is not None else False

    def __int__(self) -> int:
        """Allow int() conversion: timeout = int(TIMEOUT_VAR)"""
        return int(self._get_value())

    def __float__(self) -> float:
        """Allow float() conversion"""
        return float(self._get_value())

    def __neg__(self):
        """Allow unary minus: -CONFIG_VAR"""
        return -self._get_value()

    def __pos__(self):
        """Allow unary plus: +CONFIG_VAR"""
        return +self._get_value()

    def __eq__(self, other) -> bool:
        return self._get_value() == other

    def __or__(self, other) -> Any:
        """Support fallback syntax: TOKEN1 or TOKEN2"""
        value = self._get_value()
        return value if value else other

    @property
    def value(self) -> Any:
        """Explicit property to get the value."""
        return self._get_value()


@dataclass
class ConfigDefinition:
    key: str
    label: str
    description: str
    default: Any
    value_type: ValueType
    group: str
    component: str
    advanced: bool = False
    sensitive: bool = False
    tags: List[str] = field(default_factory=list)
    constraints: Optional[Dict[str, Any]] = None
    getter: Optional[Callable[[], Any]] = None
    setter: Optional[Callable[[Any], None]] = None
    # If True, hide this variable from graphical UI listings (but keep API access)
    hidden: bool = False
    # If True, the config is read-only in the UI (no edits allowed)
    readonly: bool = False

    value: Any = None
    raw_value: Optional[str] = None
    env_override: bool = False
    env_value: Optional[str] = None
    loaded: bool = False
    listeners: List[Callable[[Any], None]] = field(default_factory=list)
    warned_default: bool = False
    # If True, changing this configuration may require reloading the owning
    # component (or the whole core). Default is False to avoid unnecessary
    # reloads from routine config edits.
    needs_component_reload: bool = False


class ConfigRegistry:
    def __init__(self) -> None:
        self._definitions: Dict[str, ConfigDefinition] = {}
        self._load_lock = asyncio.Lock()
        self._pending_env_persists: Dict[
            str, str
        ] = {}  # Buffer for env overrides to persist when DB is ready
        # Buffer for persona-related updates received before PersonaManager is ready
        self._pending_persona_updates: Dict[str, Any] = {}
        # Background task for retrying pending persona DB persists
        self._pending_persona_worker: asyncio.Task | None = None
        # Note: pending persona updates are kept in-memory and retried by the
        # background worker. We intentionally avoid adding a file persistence
        # layer here to reduce complexity and potential I/O surprises.
        # Registry of component reload handlers: maps component name to async callback
        self._reload_handlers: Dict[str, Callable[[], Any]] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def get_value(
        self,
        key: str,
        default: Any,
        *,
        label: Optional[str] = None,
        description: str = "",
        value_type: ValueType = str,
        group: str = "core",
        component: str = "core",
        advanced: bool = False,
        sensitive: bool = False,
        tags: Optional[Iterable[str]] = None,
        constraints: Optional[Dict[str, Any]] = None,
        getter: Optional[Callable[[], Any]] = None,
        setter: Optional[Callable[[Any], None]] = None,
        needs_component_reload: bool = False,
        readonly: bool = False,
        hidden: bool = False,
    ) -> Any:
        """Return the typed value for ``key`` or register it if unknown."""

        definition = self._register_definition(
            key,
            default,
            label=label,
            description=description,
            value_type=value_type,
            group=group,
            component=component,
            advanced=advanced,
            sensitive=sensitive,
            tags=tags,
            needs_component_reload=needs_component_reload,
            readonly=readonly,
            hidden=hidden,
            constraints=constraints,
            getter=getter,
            setter=setter,
        )
        if not definition.loaded:
            self._load_definition_sync(definition)
        return definition.value

    def get_var(
        self,
        key: str,
        default: Any,
        *,
        label: Optional[str] = None,
        description: str = "",
        value_type: ValueType = str,
        group: str = "core",
        component: str = "core",
        advanced: bool = False,
        sensitive: bool = False,
        tags: Optional[Iterable[str]] = None,
        constraints: Optional[Dict[str, Any]] = None,
        getter: Optional[Callable[[], Any]] = None,
        setter: Optional[Callable[[Any], None]] = None,
        needs_component_reload: bool = False,
        readonly: bool = False,
        hidden: bool = False,
    ) -> ConfigVar:
        """
        Return a ConfigVar that auto-updates when the config changes.

        This is the recommended way for interfaces and plugins to access config
        values. The returned ConfigVar automatically reflects database changes
        without requiring manual listeners.

        Example:
            TOKEN = config_registry.get_var("MY_TOKEN", "", label="My Token", ...)

            # Later, the value is always current:
            if TOKEN:  # Automatically checks latest value
                bot = Bot(token=str(TOKEN))
        """
        # First register the config (same as get_value)
        self._register_definition(
            key,
            default,
            label=label,
            description=description,
            value_type=value_type,
            group=group,
            component=component,
            advanced=advanced,
            sensitive=sensitive,
            tags=tags,
            needs_component_reload=needs_component_reload,
            hidden=hidden,
            readonly=readonly,
            constraints=constraints,
            getter=getter,
            setter=setter,
        )

        # Return a ConfigVar that will always fetch the latest value
        return ConfigVar(key, registry=self)

    async def set_value(self, key: str, new_value: Any) -> None:
        """Persist a new value for ``key`` and notify listeners."""

        definition = self._definitions.get(key)
        if definition is None:
            raise KeyError(f"Unknown configuration key: {key}")
        if definition.env_override:
            raise ValueError(
                f"Configuration '{key}' is overridden by environment and cannot be modified."
            )

        # Validate constrained choices before applying setter/persisting
        constraints = definition.constraints or {}
        if constraints and isinstance(constraints, dict) and "choices" in constraints:
            choices = constraints.get("choices") or []
            # Compare using string form to be robust across types
            if str(new_value) not in choices:
                raise ValueError(
                    f"Invalid value for '{key}': {new_value!r}. Allowed values: {choices}"
                )

        # If definition has a setter, use it instead of persisting to DB
        if definition.setter is not None:
            try:
                definition.setter(new_value)
                definition.value = new_value
                definition.raw_value = self._serialize_value(definition, new_value)
                definition.loaded = True

                log_debug(f"[config] Updated '{key}' via setter")

                for callback in list(definition.listeners):
                    try:
                        callback(new_value)
                    except Exception as exc:  # pragma: no cover - listener safety
                        log_warning(f"[config] Listener for '{key}' failed: {exc}")

                # Trigger automatic reload if needed
                await self._trigger_reload_if_needed(definition)
                return
            except Exception as exc:
                log_error(
                    f"[config] Failed to set value for '{key}' using setter: {exc}"
                )
                raise

        serialized = self._serialize_value(definition, new_value)
        typed_value = self._convert_value(definition, serialized)

        await self._persist_to_db(definition.key, serialized)

        definition.value = typed_value
        definition.raw_value = serialized
        definition.loaded = True

        log_debug(f"[config] Updated '{key}' via Web UI/API")

        for callback in list(definition.listeners):
            try:
                callback(typed_value)
            except Exception as exc:  # pragma: no cover - listener safety
                log_warning(f"[config] Listener for '{key}' failed: {exc}")

        # Trigger automatic reload if needed
        await self._trigger_reload_if_needed(definition)

    def add_listener(self, key: str, callback: Callable[[Any], None]) -> None:
        definition = self._definitions.get(key)
        if definition is None:
            raise KeyError(f"Unknown configuration key: {key}")
        definition.listeners.append(callback)

    def register_reload_handler(
        self, component: str, handler: Callable[[], Any]
    ) -> None:
        """Register an async reload handler for a component.

        When a configuration variable with needs_component_reload=True is changed,
        and the variable's component matches this component name, the handler will
        be called automatically.

        Args:
            component: Component name (e.g., "telegram_bot", "discord_bot")
            handler: Async callable that performs the reload (e.g., reload_interface)

        Example:
            config_registry.register_reload_handler("telegram_bot", reload_interface)
        """
        self._reload_handlers[component] = handler
        log_debug(f"[config] Registered reload handler for component '{component}'")

    async def _trigger_reload_if_needed(self, definition: ConfigDefinition) -> None:
        """Trigger component reload if the variable requires it.

        Called after set_value when needs_component_reload is True.
        """
        if not definition.needs_component_reload:
            return

        component = definition.component
        handler = self._reload_handlers.get(component)

        if handler is None:
            log_warning(
                f"[config] No reload handler registered for component '{component}'"
            )
            return

        try:
            log_info(
                f"[config] Triggering automatic reload for component '{component}' (variable: {definition.key})"
            )
            # Call the handler - it could be sync or async
            result = handler()
            if asyncio.iscoroutine(result):
                await result
            log_info(
                f"[config] ✓ Reload handler for '{component}' completed successfully"
            )
        except Exception as exc:
            log_error(f"[config] Reload handler for '{component}' failed: {exc}")
            # Don't re-raise - allow the app to continue even if reload fails

    async def flush_env_overrides_to_db(self) -> None:
        """Persist all buffered env override values to the database.

        This should be called once the database is ready during startup.
        """
        if not self._pending_env_persists:
            log_debug("[config] No env overrides to flush")
            return

        log_info(
            f"[config] Flushing {len(self._pending_env_persists)} env override(s) to database"
        )
        for key, value in list(self._pending_env_persists.items()):
            try:
                log_debug(
                    f"[config] Processing env override '{key}' with value '{value}'"
                )
                # Skip loading current value from DB to avoid potential deadlocks during init
                # Just persist the new value directly
                log_debug(f"[config] About to persist '{key}' to DB")
                await self._persist_to_db(key, value)
                log_debug(f"[config] ✓ Persisted env override '{key}' to DB")
            except Exception as exc:
                log_warning(f"[config] Failed to persist env override '{key}': {exc}")

        self._pending_env_persists.clear()
        log_info("[config] ✓ Env overrides flushed to database")

    def export_definitions(self) -> List[Dict[str, Any]]:
        """Return all registered definitions with current state for the API."""
        exported: List[Dict[str, Any]] = []
        for defn in self._definitions.values():
            if not defn.loaded:
                try:
                    self._load_definition_sync(defn)
                except Exception as exc:
                    log_warning(
                        f"[config] Failed to load '{defn.key}' during export: {exc}"
                    )

            if defn.getter is not None:
                try:
                    defn.value = defn.getter()
                    defn.raw_value = self._serialize_value(defn, defn.value)
                except Exception as exc:
                    log_warning(
                        f"[config] Failed to refresh '{defn.key}' during export: {exc}"
                    )

            exported.append(
                {
                    "key": defn.key,
                    "label": defn.label,
                    "description": defn.description,
                    "value": self._export_value(defn),
                    "default": self._export_default(defn),
                    "group": defn.group,
                    "component": defn.component,
                    "advanced": defn.advanced,
                    "sensitive": defn.sensitive,
                    "env_override": defn.env_override,
                    "value_type": self._type_name(defn.value_type),
                    "needs_component_reload": getattr(
                        defn, "needs_component_reload", False
                    ),
                    "hidden": getattr(defn, "hidden", False),
                    "readonly": getattr(defn, "readonly", False),
                    "tags": list(defn.tags),
                    "constraints": defn.constraints,
                }
            )
        return sorted(
            exported, key=lambda item: (item["group"], item["component"], item["label"])
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _register_definition(
        self,
        key: str,
        default: Any,
        *,
        label: Optional[str],
        description: str,
        value_type: ValueType,
        group: str,
        component: str,
        advanced: bool,
        sensitive: bool,
        tags: Optional[Iterable[str]],
        needs_component_reload: bool = False,
        hidden: bool = False,
        readonly: bool = False,
        constraints: Optional[Dict[str, Any]],
        getter: Optional[Callable[[], Any]] = None,
        setter: Optional[Callable[[Any], None]] = None,
    ) -> ConfigDefinition:
        existing = self._definitions.get(key)
        if existing:
            return existing

        definition = ConfigDefinition(
            key=key,
            label=label or key,
            description=description,
            default=default,
            value_type=value_type,
            group=group,
            component=component,
            advanced=advanced,
            sensitive=sensitive,
            tags=list(tags or []),
            constraints=constraints,
            getter=getter,
            setter=setter,
            needs_component_reload=needs_component_reload,
            hidden=hidden,
            readonly=readonly,
        )
        self._definitions[key] = definition
        log_debug(f"[config] Registered setting '{key}' (component={component})")
        return definition

    def _load_definition_sync(self, definition: ConfigDefinition) -> None:
        """Synchronously ensure ``definition`` is loaded."""

        # If definition has a getter, use it instead of loading from DB/env
        if definition.getter is not None:
            try:
                definition.value = definition.getter()
                definition.raw_value = self._serialize_value(
                    definition, definition.value
                )
                definition.loaded = True
                return
            except Exception as exc:
                print(
                    f"[config] Failed to get value for '{definition.key}' using getter: {exc}",
                    flush=True,
                )
                # Fall back to default
                definition.value = definition.default
                definition.raw_value = self._serialize_value(
                    definition, definition.default
                )
                definition.loaded = True
                return

        # Reset env_override flag at each load - it should only be True if ENV exists NOW
        definition.env_override = False
        definition.env_value = None

        env_value = os.getenv(definition.key)
        # Treat empty env values as "not set" so DB can take precedence.
        # This is important when env files contain placeholders like KEY=.
        if env_value is not None and str(env_value).strip() != "":
            definition.env_override = True
            definition.env_value = env_value
            definition.raw_value = env_value
            definition.value = self._convert_value(definition, env_value)
            definition.loaded = True
            # Buffer env override for later persistence to DB
            self._pending_env_persists[definition.key] = env_value
            return

        raw_value: Optional[str] = None
        if "bootstrap" not in definition.tags:
            try:
                raw_value = self._load_from_db_sync(definition.key)
            except Exception as exc:
                # Use print to avoid circular import with logging_utils during initialization
                print(
                    f"[config] Failed to load '{definition.key}' from DB: {exc}",
                    flush=True,
                )

        if raw_value is not None:
            definition.raw_value = raw_value
            definition.value = self._convert_value(definition, raw_value)
            definition.loaded = True
            return

        if not definition.warned_default:
            # Use print to avoid circular import with logging_utils during initialization
            print(
                f"[config] Using hard-coded default for '{definition.key}' ({definition.default!r})",
                flush=True,
            )
            definition.warned_default = True

        definition.value = definition.default
        definition.raw_value = self._serialize_value(definition, definition.default)
        definition.loaded = True

        # CRITICAL FIX: Don't persist default immediately if we skipped DB load
        # due to running event loop - the value might exist in DB but wasn't loaded yet.
        # Only persist if we're sure DB was checked (no running loop or bootstrap tag)
        try:
            asyncio.get_running_loop()
            # Event loop is running - DB load was skipped, so DON'T persist default yet
            # It will be loaded properly via load_all_from_db() later
            print(
                f"[config] Skipping default persistence for '{definition.key}' (will load from DB async)",
                flush=True,
            )
        except RuntimeError:
            # No event loop - safe to persist default now
            self._persist_background(definition.key, definition.raw_value)

    def _load_from_db_sync(self, key: str) -> Optional[str]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # No event loop running - safe to create one
            return asyncio.run(self._load_from_db(key))
        else:
            # Event loop is running - we cannot block it
            # Skip DB load during sync import phase
            log_debug(
                f"[config] Skipping DB load for '{key}' during async context (will use default)"
            )
            return None

    async def _load_from_db(self, key: str) -> Optional[str]:
        try:
            log_debug(f"[config] About to import from core.db for key '{key}'")
            from core.db import get_conn_ctx, ensure_core_tables

            log_debug(f"[config] Successfully imported from core.db for key '{key}'")
        except ImportError as e:
            # Circular import during initialization - skip DB load
            print(
                f"[config] Skipping DB load for '{key}' during initialization: {e}",
                flush=True,
            )
            return None

        try:
            log_debug(f"[config] About to ensure_core_tables for key '{key}'")
            await ensure_core_tables()
            log_debug(f"[config] ensure_core_tables completed for key '{key}'")
            log_debug(f"[config] About to get_conn_ctx for key '{key}'")
            async with get_conn_ctx() as conn:
                log_debug(f"[config] get_conn_ctx completed for key '{key}'")
                async with conn.cursor() as cur:
                    log_debug(f"[config] About to execute query for key '{key}'")
                    await cur.execute(
                        "SELECT value FROM config WHERE config_key = %s", (key,)
                    )
                    log_debug(f"[config] Query executed for key '{key}'")
                    row = await cur.fetchone()
                    log_debug(f"[config] fetchone completed for key '{key}': {row}")
                    if row:
                        return row[0]

            return None
        except Exception as e:
            # If the error looks like a missing-table/column schema error, try
            # an idempotent ensure_core_tables() and retry the SELECT once.
            msg = str(e) or ""
            is_schema_error = (
                "1146" in msg
                or "doesn't exist" in msg
                or "1054" in msg
                or "Unknown column" in msg
            )
            if is_schema_error:
                try:
                    from core.db import ensure_core_tables

                    log_debug(
                        f"[config] Schema error while loading '{key}': {msg}; running ensure_core_tables() and retrying"
                    )
                    await ensure_core_tables()
                    # retry once
                    async with get_conn_ctx() as conn:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "SELECT value FROM config WHERE config_key = %s", (key,)
                            )
                            row = await cur.fetchone()
                            if row:
                                return row[0]
                except Exception as retry_exc:
                    log_error(
                        f"[config] Retry after ensure_core_tables() failed for key '{key}': {retry_exc}"
                    )
            log_error(f"[config] Error loading from DB for key '{key}': {e}")
            return None

    def _persist_background(self, key: str, value: str) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self._persist_to_db(key, value))
        else:
            loop.create_task(self._persist_to_db(key, value))

    async def _persist_to_db(self, key: str, value: str) -> None:
        # Persisting config values is best-effort. Failures (DB unavailable,
        # pool exhausted, timeouts) should not raise to callers — instead we
        # log a warning and continue so the Web UI/API remains responsive.
        """
        Persist a config key to the DB in a best-effort manner.

        Returns True on success, False on any failure.
        """
        try:
            try:
                from core.db import get_conn_ctx, ensure_core_tables
            except ImportError as e:
                # Circular import during initialization - skip DB persist
                print(
                    f"[config] Skipping DB persist for '{key}' during initialization: {e}",
                    flush=True,
                )
                return False

            log_debug(f"[config] Ensuring core tables before persisting '{key}'")
            await ensure_core_tables()

            log_debug(
                f"[config] Attempting to acquire DB connection to persist '{key}'"
            )
            async with get_conn_ctx() as conn:
                log_debug(
                    f"[config] Acquired DB connection for persisting '{key}': conn_id={id(conn)}"
                )
                try:
                    log_debug(
                        f"[config] Checking existence for key='{key}' before persist"
                    )
                    recreated = False
                    async with conn.cursor() as cur:
                        await cur.execute(
                            "SELECT 1 FROM config WHERE config_key = %s", (key,)
                        )
                        row = await cur.fetchone()
                        if not row:
                            recreated = True

                    log_debug(
                        f"[config] Executing REPLACE for key='{key}' (value_len={len(value) if value else 0})"
                    )
                    try:
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "REPLACE INTO config (config_key, value) VALUES (%s, %s)",
                                (key, value),
                            )
                            await conn.commit()
                        log_debug(f"[config] REPLACE succeeded for key='{key}'")
                        if recreated:
                            log_warning(
                                f"[config] Config key '{key}' was missing from DB and has been recreated with the new value"
                            )
                        return True
                    except Exception:
                        # Some MySQL variants or permissions could reject REPLACE; try robust fallback
                        log_debug(
                            f"[config] REPLACE failed for key='{key}', attempting INSERT ... ON DUPLICATE KEY UPDATE fallback"
                        )
                        async with conn.cursor() as cur:
                            await cur.execute(
                                "INSERT INTO config (config_key, value) VALUES (%s, %s) ON DUPLICATE KEY UPDATE value = VALUES(value)",
                                (key, value),
                            )
                            await conn.commit()
                        log_debug(f"[config] Fallback INSERT succeeded for key='{key}'")
                        if recreated:
                            log_warning(
                                f"[config] Config key '{key}' was missing from DB and has been recreated with the new value (fallback path)"
                            )
                        return True
                except Exception as e:
                    # If schema error (missing table/column), attempt an idempotent
                    # ensure_core_tables() and retry the REPLACE/INSERT once.
                    msg = str(e) or ""
                    is_schema_error = (
                        "1146" in msg
                        or "doesn't exist" in msg
                        or "1054" in msg
                        or "Unknown column" in msg
                    )
                    if is_schema_error:
                        try:
                            from core.db import ensure_core_tables

                            log_debug(
                                f"[config] Schema error persisting '{key}': {msg}; running ensure_core_tables() and retrying persist"
                            )
                            await ensure_core_tables()
                            # retry the same persist steps once
                            recreated = False
                            async with conn.cursor() as cur:
                                await cur.execute(
                                    "SELECT 1 FROM config WHERE config_key = %s", (key,)
                                )
                                row = await cur.fetchone()
                                if not row:
                                    recreated = True

                            try:
                                async with conn.cursor() as cur:
                                    await cur.execute(
                                        "REPLACE INTO config (config_key, value) VALUES (%s, %s)",
                                        (key, value),
                                    )
                                    await conn.commit()
                                log_debug(
                                    f"[config] REPLACE (retry) succeeded for key='{key}'"
                                )
                                if recreated:
                                    log_warning(
                                        f"[config] Config key '{key}' was missing and has been recreated on retry"
                                    )
                                return True
                            except Exception:
                                log_debug(
                                    f"[config] REPLACE (retry) failed for key='{key}', attempting INSERT fallback"
                                )
                                async with conn.cursor() as cur:
                                    await cur.execute(
                                        "INSERT INTO config (config_key, value) VALUES (%s, %s) ON DUPLICATE KEY UPDATE value = VALUES(value)",
                                        (key, value),
                                    )
                                    await conn.commit()
                                log_debug(
                                    f"[config] Fallback INSERT (retry) succeeded for key='{key}'"
                                )
                                if recreated:
                                    log_warning(
                                        f"[config] Config key '{key}' was missing and has been recreated on retry (fallback path)"
                                    )
                                return True
                        except Exception as retry_exc:
                            import traceback

                            tb2 = traceback.format_exc()
                            log_error(
                                f"[config] Retry-after-ensure_core_tables() failed for '{key}': {retry_exc} -- traceback:\n{tb2}"
                            )
                            return False

                    # Non-schema or unrecoverable error: log full traceback and fail gracefully
                    import traceback

                    tb = traceback.format_exc()
                    log_error(
                        f"[config] Failed to persist '{key}' to DB: {e} -- traceback:\n{tb}"
                    )
                    return False
        except Exception as exc:  # pragma: no cover - defensive
            log_error(f"[config] Unexpected error while persisting '{key}': {exc}")
            return False

    def _start_pending_persona_worker(self) -> None:
        """Start a background task to retry persisting pending persona updates."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop — caller is not in async context, skip
            return

        if self._pending_persona_worker and not self._pending_persona_worker.done():
            return

        async def _worker():
            log_info(
                f"[config] Starting pending persona persistence worker (pending={len(self._pending_persona_updates)})"
            )
            try:
                while self._pending_persona_updates:
                    for k, v in list(self._pending_persona_updates.items()):
                        try:
                            serialized = (
                                self._serialize_value(self._definitions.get(k), v)
                                if self._definitions.get(k)
                                else str(v)
                            )
                            ok = await self._persist_to_db(k, serialized)
                            if ok:
                                log_info(
                                    f"[config] Pending persona update persisted: {k}"
                                )
                                try:
                                    del self._pending_persona_updates[k]
                                except KeyError:
                                    pass
                        except Exception as e:
                            log_warning(f"[config] Worker failed persisting '{k}': {e}")
                    # Sleep before next retry
                    await asyncio.sleep(5)
            finally:
                log_info("[config] Pending persona persistence worker exiting")

        self._pending_persona_worker = loop.create_task(_worker())

    # NOTE: file persistence for pending persona updates intentionally removed.
    # If we need durable pending storage in the future we can add a separate
    # durable queue mechanism, but we avoid it for now to keep the system
    # behavior simpler and avoid additional I/O failure modes.

    def _serialize_value(self, definition: ConfigDefinition, value: Any) -> str:
        if value is None:
            return ""
        if definition.value_type is bool:
            return "true" if bool(value) else "false"
        if definition.value_type is int:
            if value == "":
                return ""
            return str(int(value))
        if definition.value_type is float:
            return str(float(value))
        if definition.value_type == "json":
            import json

            return json.dumps(value)
        if callable(definition.value_type) and definition.value_type not in (
            bool,
            int,
            float,
            str,
        ):
            converted = definition.value_type(value)
            return str(converted)
        return str(value)

    def _convert_value(self, definition: ConfigDefinition, raw_value: str) -> Any:
        if definition.key == "SYNTH_ALIASES":
            print(
                f"[DEBUG] _convert_value called for SYNTH_ALIASES: value_type={definition.value_type!r}, raw_value={raw_value!r}",
                flush=True,
            )
        try:
            if definition.value_type is bool:
                return str(raw_value).strip().lower() in {"1", "true", "yes", "on"}
            if definition.value_type is int:
                if raw_value is None or str(raw_value).strip() == "":
                    return definition.default
                return int(raw_value)
            if definition.value_type is float:
                return float(raw_value)
            # Handle explicit JSON type: deserialize string to native object (list/dict)
            if definition.value_type == "json":
                import json

                if not raw_value or raw_value.strip() == "":
                    return definition.default
                return json.loads(raw_value)
            if callable(definition.value_type) and definition.value_type not in (
                bool,
                int,
                float,
                str,
            ):
                return definition.value_type(raw_value)
        except Exception as exc:
            # Use print to avoid circular import with logging_utils during initialization
            print(
                f"[config] Failed to cast '{definition.key}' value '{raw_value}' ({exc}), using default",
                flush=True,
            )
            return definition.default
        if definition.value_type is str:
            if raw_value == "" and definition.default is None:
                return None
            return raw_value
        return raw_value

    def _export_value(self, definition: ConfigDefinition) -> Any:
        if definition.value_type is bool:
            return bool(definition.value)
        if definition.value_type in (int, float):
            return definition.value
        # Preserve JSON/list/dict types so the API can return native arrays/objects
        # (previous behavior returned a stringified representation which broke
        # the Web UI for list-valued settings like SYNTH_ALIASES).
        if definition.value is None:
            return ""
        # If already a native container or explicitly JSON-typed, return as-is
        if definition.value_type == "json" or isinstance(
            definition.value, (list, dict)
        ):
            return definition.value

        # If the stored value is a string that looks like JSON (or a Python
        # literal), try to deserialize it to return native arrays/objects.
        if isinstance(definition.value, str):
            s = definition.value.strip()
            if s.startswith("[") or s.startswith("{"):
                try:
                    import json as _json

                    return _json.loads(s)
                except Exception:
                    try:
                        # Fall back to Python literal eval for strings like "['a','b']"
                        import ast as _ast

                        return _ast.literal_eval(s)
                    except Exception:
                        pass

        return str(definition.value)

    def _export_default(self, definition: ConfigDefinition) -> Any:
        if definition.value_type is bool:
            return bool(definition.default)
        if definition.value_type in (int, float):
            return definition.default
        # Preserve list/dict/json defaults as native types for API consumers
        if definition.default is None:
            return ""
        if definition.value_type == "json" or isinstance(
            definition.default, (list, dict)
        ):
            return definition.default
        return str(definition.default)

    def _type_name(self, value_type: ValueType) -> str:
        if value_type is bool:
            return "bool"
        if value_type is int:
            return "int"
        if value_type is float:
            return "float"
        if value_type == "json":
            return "json"
        return "str"

    async def persist_bootstrap_configs(self) -> None:
        """
        Persist all bootstrap configurations to the database after DB initialization.

        This is called after the database is ready to ensure bootstrap configs
        (like DB_HOST, DB_PORT, etc.) that were loaded from environment variables
        are visible in the UI.
        """
        for definition in self._definitions.values():
            if (
                "bootstrap" in definition.tags
                and definition.env_override
                and definition.loaded
            ):
                try:
                    await self._persist_to_db(definition.key, definition.raw_value)
                    log_debug(
                        f"[config] Persisted bootstrap config '{definition.key}' to DB"
                    )
                except Exception as exc:
                    log_warning(
                        f"[config] Failed to persist bootstrap config '{definition.key}': {exc}"
                    )

    async def load_all_from_db(self) -> None:
        """
        Load all non-bootstrap configurations from the database.

        This is called after DB initialization to load configurations that were
        skipped during module imports (when running inside an async context).

        CRITICAL: This fixes the issue where removing env variables causes configs
        to be lost. When a variable is removed from ENV, this function ensures the
        DB value is loaded instead of using defaults.
        """
        loaded_count = 0
        skipped_count = 0

        # Batch-load all config values in one DB round-trip.
        # This avoids exhausting the DB pool during startup when many components
        # are initializing concurrently.
        config_rows: Dict[str, str] = {}
        try:
            from core.db import get_conn_ctx, ensure_core_tables

            await ensure_core_tables()
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("SELECT config_key, value FROM config")
                    rows = await cur.fetchall()
                    config_rows = {
                        row[0]: row[1] for row in rows if row and len(row) >= 2
                    }
        except Exception as exc:
            log_warning(f"[config] Failed to batch-load config from DB: {exc}")

        for definition in self._definitions.values():
            # Skip bootstrap configs (already loaded from env)
            if "bootstrap" in definition.tags:
                log_debug(f"[config] Skipping '{definition.key}': bootstrap tag")
                skipped_count += 1
                continue

            # Skip if already loaded from environment
            if definition.env_override:
                log_debug(f"[config] Skipping '{definition.key}': env_override=True")
                skipped_count += 1
                continue

            # FORCE reload persona configs from DB (they may have been set to defaults at import time)
            persona_keys = {"SYNTH_NAME", "SYNTH_PROFILE", "SYNTH_ALIASES"}
            if definition.key in persona_keys:
                log_debug(
                    f"[config] FORCE reloading persona config '{definition.key}' from DB"
                )
            # Skip if already properly loaded from DB during sync phase
            # BUT allow reload if it only has default value (to handle ENV removal case)
            elif (
                definition.loaded
                and definition.raw_value is not None
                and definition.raw_value != ""
            ):
                # Check if this is actually a default value that needs DB reload
                default_raw = self._serialize_value(definition, definition.default)
                if definition.raw_value != default_raw:
                    # Has a real value from DB, skip
                    log_debug(
                        f"[config] Skipping '{definition.key}': already has DB value (not default)"
                    )
                    skipped_count += 1
                    continue
                # Has default value, try to load from DB in case it was skipped during async init
                log_debug(
                    f"[config] '{definition.key}' has default value, will try DB reload"
                )

            try:
                raw_value = config_rows.get(definition.key)
                if raw_value is not None:
                    definition.raw_value = raw_value
                    definition.value = self._convert_value(definition, raw_value)
                    definition.loaded = True
                    loaded_count += 1
                    if definition.sensitive:
                        log_debug(
                            f"[config] ✓ Loaded '{definition.key}' from DB: <redacted>"
                        )
                    else:
                        log_debug(f"[config] ✓ Loaded '{definition.key}' from DB")
                else:
                    # Use default value if not in DB. Avoid persisting defaults here to keep
                    # startup light on DB writes; defaults can be persisted later via UI edits.
                    if not definition.loaded:
                        definition.value = definition.default
                        definition.raw_value = self._serialize_value(
                            definition, definition.default
                        )
                        definition.loaded = True
            except Exception as exc:
                log_warning(
                    f"[config] Failed to apply DB value for '{definition.key}': {exc}"
                )

        log_info(
            f"[config] ✓ load_all_from_db completed: loaded={loaded_count}, skipped={skipped_count}, total={len(self._definitions)}"
        )

    def notify_all_listeners(self) -> None:
        """
        Notify all registered listeners with current config values.

        This is called after load_all_from_db() to ensure all components
        receive updated values from the database, not just the defaults
        that were loaded during module import.
        """
        notified_count = 0
        for definition in self._definitions.values():
            if definition.listeners:
                for listener in definition.listeners:
                    try:
                        listener(definition.value)
                        notified_count += 1
                        log_debug(
                            f"[config] Notified listener for '{definition.key}' with value: {definition.value}"
                        )
                    except Exception as exc:
                        log_warning(
                            f"[config] Failed to notify listener for '{definition.key}': {exc}"
                        )

        if notified_count > 0:
            log_info(
                f"[config] ✓ Notified {notified_count} listener(s) with updated config values"
            )


config_registry = ConfigRegistry()
