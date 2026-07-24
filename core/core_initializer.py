# core/core_initializer.py

import os
import importlib
import inspect
import asyncio
import json
import threading
from pathlib import Path
from typing import Any
from core.logging_utils import log_info, log_error, log_warning, log_debug
from core.config import list_available_cortex_engines
from dataclasses import dataclass, field
from typing import List, Dict, Optional
from enum import Enum

# Import exposed variables EARLY to ensure correct type registrations
# before any circular import chains can cause persona_manager to load prematurely
try:
    import core.variables_engine  # noqa: F401
except Exception as e:
    log_warning(
        f"[core_initializer] Failed to import variables_engine at module level: {e}"
    )


class ComponentStatus(Enum):
    """Status of a system component."""

    LOADING = "loading"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass
class ComponentInfo:
    """Information about a system component."""

    name: str
    type: str  # "plugin", "interface", "cortex", "core"
    status: ComponentStatus = ComponentStatus.LOADING
    actions: List[str] = field(default_factory=list)
    error: str = ""
    details: str = ""
    # WebUI plugin-section metadata (Phase A-cat / B)
    module_name: str = ""  # dotted module path, e.g. "plugins.grillo.grillo_dream"
    dir_path: str = ""  # on-disk directory of the component (for icon/guide lookup)
    category: str = (
        ""  # macro-category: Core/Interfaces/Grillo/Vessels/Agent/Recon/Various
    )


# Curated set of plugins that must never be disabled at runtime (message chain
# critical). Kept intentionally minimal; everything else is disable-able.
CORE_PLUGIN_SHORT_NAMES: frozenset[str] = frozenset({"message_plugin"})

# Curated set of interfaces that must never be disabled at runtime. Every I/O
# adapter (Telegram, Discord, Matrix, OpenAI API) is safely toggle-able. The
# WebUI is protected because it is the very surface used to manage toggles —
# disabling it would lock the operator out (self-lockout) until the next manual
# config edit + restart.
CORE_INTERFACE_NAMES: frozenset[str] = frozenset({"synth_webui"})


def derive_plugin_category(
    module_name: str, dir_path: str, declared: str | None = None
) -> str:
    """Return the macro-category for a plugin.

    Order of precedence: an explicit ``declared`` category (from
    ``get_metadata``) always wins; otherwise the category is auto-derived from
    the module path / on-disk location. This is deterministic and language
    independent (no keyword matching on user content).
    """
    if declared and isinstance(declared, str) and declared.strip():
        return declared.strip()

    parts = (module_name or "").split(".")
    lowered = [p.lower() for p in parts]

    # Location-based rules.
    if "interface" in lowered or "interface_dev" in lowered:
        return "Interfaces"
    if "grillo" in lowered:
        return "Grillo"
    if "vessels" in lowered:
        return "Vessels"

    short_name = parts[-1] if parts else ""
    if short_name in CORE_PLUGIN_SHORT_NAMES:
        return "Core"

    # Recon plugins follow the structural `recon_*` module-name convention.
    if short_name.startswith("recon_"):
        return "Recon"

    return "Various"


class CoreInitializer:
    """Centralizes the initialization of all synth components."""

    def __init__(self):
        self.loaded_plugins = []
        self.active_interfaces = []
        self.active_cortex = None
        self.startup_errors = []
        self.actions_block = {"available_actions": {}}
        self.interface_actions = {}
        self._summary_displayed = False  # Flag to prevent duplicate summaries
        self._building_actions_block = False  # Flag to prevent infinite rebuild loops
        self._initial_initialization = (
            False  # Flag to indicate we're in initial startup phase
        )
        self._background_tasks = set()

        # Component tracking system
        self.components: Dict[str, ComponentInfo] = {}
        self.initialization_completed = False

        # Runtime flag for dev components (NOT persistent, resets on restart)
        self._enable_dev_components = False
        self._trainer_listener_registered = False
        self._agentic_runtime_bootstrapped = False

    def enable_dev_components(self, enabled: bool = True):
        """Enable or disable dev components discovery. NOT persistent across restarts."""
        self._enable_dev_components = enabled
        log_info(
            f"[core_initializer] Dev components {'enabled' if enabled else 'disabled'} (runtime only)"
        )

    def are_dev_components_enabled(self) -> bool:
        """Check if dev components are currently enabled."""
        return self._enable_dev_components

    def _evaluate_cortex_health(self, plugin: Any) -> tuple[bool, str]:
        """Return (ok, error_message) for a Cortex engine health check."""
        if plugin is None:
            return False, "Cortex engine instance is missing"

        if hasattr(plugin, "get_health_status"):
            try:
                result = plugin.get_health_status()
                if isinstance(result, tuple) and len(result) >= 2:
                    return bool(result[0]), str(result[1] or "")
                if isinstance(result, dict):
                    ok = bool(result.get("ok", True))
                    error = str(result.get("error") or result.get("message") or "")
                    return ok, error
                return bool(result), ""
            except Exception as exc:
                return False, f"Health check failed: {exc}"

        return True, ""

    async def _recover_interrupted_agent_tasks(self) -> None:
        """Reconcile agentic turns left ``running`` by a previous process.

        Agent Lane turns run detached from the message-chain consumer, so a
        container restart (or crash) mid-turn leaves their ``agent_tasks`` row
        stuck as ``running`` / ``paused`` forever. On startup we mark those
        orphans as ``failed`` with a clear stop reason.

        We deliberately do NOT auto-resume them: an interrupted turn may have
        already executed tool calls with external side effects, so blindly
        replaying it could duplicate those effects. Surfacing the interruption
        (and letting the user re-ask) is the safe, non-destructive choice.
        """
        try:
            from core.db import get_conn_ctx
        except Exception as exc:
            log_debug(
                f"[core_initializer] Agent recovery skipped (db unavailable): {exc}"
            )
            return

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    UPDATE agent_tasks
                    SET status = 'failed',
                        output = %s
                    WHERE status IN ('running', 'paused')
                    """,
                    (json.dumps({"stop_reason": "interrupted_by_restart"}),),
                )
                affected = getattr(cur, "rowcount", 0) or 0
                commit_fn = getattr(conn, "commit", None)
                if callable(commit_fn):
                    res = commit_fn()
                    if asyncio.iscoroutine(res):
                        await res

        if affected:
            log_info(
                f"[core_initializer] Recovered {affected} interrupted agent "
                "task(s) left running by a previous process (marked failed)."
            )
        else:
            log_debug("[core_initializer] No interrupted agent tasks to recover.")

    async def initialize_all(self, notify_fn=None):
        """Initialize all synth components in the correct order."""
        log_info("🚀 Initializing synth core components...")

        # Set flag to prevent plugin auto-registration from triggering refreshes
        self._initial_initialization = True
        log_debug(
            "[core_initializer] Set _initial_initialization=True to prevent auto-refresh loops"
        )

        try:
            # Don't reset loaded_plugins as they may have been registered during import
            # Only reset interface state for fresh initialization
            self.interface_actions = {}
            self.actions_block = {"available_actions": {}}

            log_debug(
                f"[core_initializer] Starting with {len(self.loaded_plugins)} pre-registered plugins: {self.loaded_plugins}"
            )

            # 0. Initialize registries
            await self._initialize_registries()

            # 0.5. Pre-load BASE_CORTEX from database before loading the engine
            # This ensures we load the correct Cortex engine that was saved by the user
            log_debug("[core_initializer] Pre-loading BASE_CORTEX from database...")
            try:
                from core.config_manager import config_registry

                # Force load BASE_CORTEX from DB if it exists
                definition = config_registry._definitions.get("BASE_CORTEX")
                if definition:
                    from core.db import ensure_core_tables

                    await ensure_core_tables()
                    raw_value = await config_registry._load_from_db("BASE_CORTEX")
                    if raw_value:
                        definition.raw_value = raw_value
                        definition.value = config_registry._convert_value(
                            definition, raw_value
                        )
                        definition.loaded = True
                        log_info(
                            f"[core_initializer] ✅ Pre-loaded BASE_CORTEX from DB: {raw_value}"
                        )
                    else:
                        log_debug(
                            "[core_initializer] BASE_CORTEX not found in DB, using default"
                        )
                else:
                    log_debug(
                        "[core_initializer] BASE_CORTEX definition not found in registry"
                    )
            except Exception as preload_exc:
                log_warning(
                    f"[core_initializer] Failed to pre-load BASE_CORTEX: {preload_exc}"
                )

            # 0.6. Register external endpoints BEFORE loading the cortex engine so that
            # ext_* engines are present in CortexRegistry._engines when load_plugin runs.
            try:
                from core.external_endpoints.registry import (
                    get_external_endpoint_registry,
                )

                await get_external_endpoint_registry().register_all_enabled()
                log_debug(
                    "[core_initializer] ✅ external endpoints pre-registered (before cortex load)"
                )
            except Exception as e:
                log_warning(
                    f"[core_initializer] external endpoint pre-registration failed: {e}"
                )

            # 1. Load Cortex engine
            await self._load_cortex_engine(notify_fn)

            # 1.5. Flush env overrides to DB now that Cortex is loaded (avoids connection deadlocks)
            try:
                log_debug("[core_initializer] About to flush env overrides to database")
                from core.config_manager import config_registry

                await config_registry.flush_env_overrides_to_db()
                log_debug(
                    "[core_initializer] Env overrides flushed to database successfully"
                )
            except Exception as flush_exc:
                log_warning(
                    f"[core_initializer] Failed to flush env overrides: {flush_exc}"
                )

            # 2. Load generic plugins (this may load additional plugins)
            self._load_plugins()

            # 2.1 Ensure plugin-managed DB tables exist (preflight)
            try:
                from core.db import ensure_plugin_tables

                await ensure_plugin_tables()
                log_debug("[core_initializer] ✅ ensure_plugin_tables() completed")
            except Exception as e:
                log_warning(f"[core_initializer] ensure_plugin_tables failed: {e}")

            # 2.2. Re-sync external endpoints (idempotent; catches any endpoints registered
            # by plugins during step 2 that were not present at step 0.6).
            try:
                from core.external_endpoints.registry import (
                    get_external_endpoint_registry,
                )

                await get_external_endpoint_registry().register_all_enabled()
                log_debug("[core_initializer] ✅ external endpoints re-synced")
            except Exception as e:
                log_warning(f"[core_initializer] external endpoint re-sync failed: {e}")

            # 2.5. Auto-register validation rules from loaded components
            log_debug(
                "[core_initializer] 🔍 About to call _register_component_validation_rules()"
            )
            self._register_component_validation_rules()
            log_debug(
                "[core_initializer] ✅ _register_component_validation_rules() completed"
            )

            # 2.5.5. Load all configurations from DB BEFORE starting async plugins.
            # This ensures plugins start with correct DB values, not hardcoded defaults.
            # The weather plugin's daily report flag is a prime example: if the weather
            # loop starts before DB configs are loaded, it uses the default (False) and
            # the daily report never fires even though the user set it to True in the UI.
            log_info(
                "[core_initializer] Loading configurations from DB (pre-plugin-start)..."
            )
            try:
                from core.config_manager import config_registry

                await config_registry.load_all_from_db()
                log_info(
                    "[core_initializer] ✅ Configurations loaded from DB (pre-plugin-start)"
                )

                # Notify all listeners so components can update their instance variables
                # before their async loops start running.
                log_info(
                    "[core_initializer] Notifying all config listeners (pre-plugin-start)..."
                )
                config_registry.notify_all_listeners()
                log_info(
                    "[core_initializer] ✅ All config listeners notified (pre-plugin-start)"
                )
            except Exception as load_exc:
                log_warning(
                    f"[core_initializer] Failed to load configurations from DB (pre-plugin-start): {load_exc}"
                )

            # 2.6. Start any async plugins that were deferred due to no running event loop
            try:
                await self.start_pending_async_plugins()
                log_debug("[core_initializer] start_pending_async_plugins completed")
            except Exception as e:
                log_warning(
                    f"[core_initializer] start_pending_async_plugins failed: {e}"
                )

            # 3. Load core actions if not already loaded
            log_debug("[core_initializer] 🔍 About to call _ensure_core_actions()")
            self._ensure_core_actions()
            log_debug("[core_initializer] ✅ _ensure_core_actions() completed")

            # 4. Initialize core persona manager BEFORE loading DB configs
            # This ensures persona variables (SYNTH_NAME, SYNTH_PROFILE, SYNTH_ALIASES) are registered first
            # Ensure core DB tables exist before attempting to load persona
            from core.db import ensure_core_tables

            try:
                await ensure_core_tables()
                log_debug(
                    "[core_initializer] ensure_core_tables() completed before persona init"
                )
            except Exception as _e:
                log_warning(f"[core_initializer] ensure_core_tables() failed: {_e}")

            # 4.1. Initialize centralized chat context manager
            log_debug(
                "[core_initializer] Initializing centralized chat context manager..."
            )
            try:
                from core.chat_context_manager import initialize_context_manager

                await initialize_context_manager()
                log_info("[core_initializer] ✅ Chat context manager initialized")
            except Exception as e:
                log_warning(
                    f"[core_initializer] Failed to initialize chat context manager: {e}"
                )

            log_debug(
                "[core_initializer] 🔍 About to call _initialize_persona_manager()"
            )
            try:
                await self._initialize_persona_manager()
                log_debug(
                    "[core_initializer] ✅ _initialize_persona_manager() completed"
                )
            except Exception as e:
                log_warning(
                    f"[core_initializer] Persona manager async init failed: {e}"
                )

            # 4.4. Auto-discover and import interface modules BEFORE loading DB configs
            # Interface modules register their config variables at import time (via config_registry.get_var).
            # If we load configs from DB before importing interfaces, interface settings like BOTFATHER_TOKEN
            # won't be registered yet and therefore won't be loaded.
            log_info(
                "[core_initializer] Discovering interface modules (pre-config load)..."
            )
            try:
                self._discover_interfaces()
                log_info("[core_initializer] ✅ Interface module discovery completed")
            except Exception as e:
                log_error(
                    f"[core_initializer] Error in _discover_interfaces (pre-config load): {e}"
                )
                self.startup_errors.append(
                    f"Interface discovery (pre-config load) failed: {e}"
                )

            # 4.5. Eagerly import core modules that register config vars at module level.
            # These are imported lazily during chat turns (after load_all_from_db has
            # already run), so their keys would never be in _definitions at bulk-load
            # time and would always fall back to defaults.  Importing here ensures vars
            # like PROMPT_LITE_MODE are registered before the DB sweep below.
            for _early_mod in ("core.history_engine", "core.chat_attention"):
                try:
                    import importlib

                    importlib.import_module(_early_mod)
                except Exception as _e:
                    log_warning(
                        f"[core_initializer] Early import of '{_early_mod}' failed: {_e}"
                    )

            # 4.5.1. Eagerly register agentic-runtime config keys.
            # These keys are only ever read via config_registry.get_var(...) INSIDE
            # functions on the chat path (core.agent_router.classify, the gate in
            # core.message_chain, plugins.recon_agent_intent), never at module import
            # time. That means they are not in _definitions when load_all_from_db()
            # runs below, so their DB value (e.g. AGENTIC_ROUTING_ENABLED=true) is
            # never loaded and they permanently fall back to their code default —
            # the Fast/Agent router would stay disabled even when enabled in the DB.
            # Registering them here ensures the DB sweep populates them. Same class
            # of bug as BOTFATHER_TOKEN (see FIXED_ISSUES.md).
            try:
                from core.config_manager import config_registry as _cfg_reg

                _cfg_reg.get_var(
                    "AGENTIC_ROUTING_ENABLED",
                    False,
                    value_type=bool,
                    label="Enable Agentic Routing",
                    description=(
                        "Enable the deterministic Fast/Agent router. When on, "
                        "turns that need tools or multiple steps are escalated "
                        "to the bounded Agent lane; otherwise every turn uses "
                        "the Fast lane."
                    ),
                    group="agent",
                    component="agent",
                )
                # AGENT_ENABLED (the user-facing on/off toggle) is registered at
                # module import time by plugins.agent_plugin, but the plugin is
                # loaded AFTER load_all_from_db() runs — so its DB value would
                # never be swept in and get_var(...) on the chat path would
                # permanently fall back to the code default (True), keeping the
                # Agent Lane engaged even when the user switched the agent OFF.
                # Register it eagerly here so the DB sweep populates it.
                _cfg_reg.get_var(
                    "AGENT_ENABLED",
                    True,
                    value_type=bool,
                    component="agent",
                )
                _cfg_reg.get_var(
                    "AGENT_MAX_ITERATIONS",
                    30,
                    value_type=int,
                    label="Agent Max Iterations",
                    description="Hard cap on the Agent reasoning-loop iterations per turn.",
                    group="agent",
                    component="agent",
                    advanced=True,
                )
                _cfg_reg.get_var(
                    "AGENT_TURN_TIMEOUT_SEC",
                    120,
                    value_type=int,
                    label="Agent Turn Timeout (s)",
                    description="Wall-clock budget in seconds for a single Agent turn.",
                    group="agent",
                    component="agent",
                    advanced=True,
                )
                _cfg_reg.get_var(
                    "DRONE_MAX_ITERATIONS",
                    3,
                    value_type=int,
                    label="Drone Max Iterations",
                    description="Hard cap on a Drone sub-agent's reasoning-loop iterations.",
                    group="agent",
                    component="agent",
                    advanced=True,
                )
                _cfg_reg.get_var(
                    "DRONE_TURN_TIMEOUT_SEC",
                    90,
                    value_type=int,
                    label="Drone Turn Timeout (s)",
                    description="Wall-clock budget in seconds for a single Drone turn.",
                    group="agent",
                    component="agent",
                    advanced=True,
                )
                log_debug(
                    "[core_initializer] Eagerly registered agentic-runtime config keys"
                )
            except Exception as _e:
                log_warning(
                    f"[core_initializer] Failed to eagerly register agentic config keys: {_e}"
                )

            # 3.5. Load all configurations from DB AFTER persona manager initialization
            # This ensures SYNTH_NAME, SYNTH_PROFILE, SYNTH_ALIASES have been registered and can be loaded from DB
            log_info("[core_initializer] Loading all configurations from database...")
            try:
                from core.config_manager import config_registry

                # Reset loaded flag for persona configs so they're reloaded from DB
                for persona_key in ["SYNTH_NAME", "SYNTH_PROFILE", "SYNTH_ALIASES"]:
                    if persona_key in config_registry._definitions:
                        config_registry._definitions[persona_key].loaded = False
                        log_debug(
                            f"[core_initializer] Reset loaded flag for {persona_key}"
                        )

                await config_registry.load_all_from_db()
                log_info(
                    "[core_initializer] ✅ All configurations loaded from database"
                )

                # Notify all listeners so components can update their global variables
                log_info("[core_initializer] Notifying all config listeners...")
                config_registry.notify_all_listeners()
                log_info("[core_initializer] ✅ All config listeners notified")

                # Apply trainer IDs from DB and keep registry in sync with updates.
                self._configure_trainer_ids()
                if not self._trainer_listener_registered:

                    def _refresh_trainer_ids(_value):
                        try:
                            self._configure_trainer_ids()
                        except Exception as exc:
                            log_warning(
                                f"[core_initializer] Failed to refresh trainer IDs: {exc}"
                            )

                    try:
                        config_registry.add_listener(
                            "TRAINER_IDS", _refresh_trainer_ids
                        )
                        self._trainer_listener_registered = True
                    except Exception as exc:
                        log_warning(
                            f"[core_initializer] Failed to register TRAINER_IDS listener: {exc}"
                        )

                # CRITICAL: Reload persona after config values are updated from DB
                # This ensures SYNTH_NAME, SYNTH_PROFILE, etc. are correct in the persona object
                log_info(
                    "[core_initializer] Reloading persona with updated config values..."
                )
                try:
                    from core.persona_manager import get_persona_manager

                    persona_mgr = get_persona_manager()
                    if persona_mgr:
                        await persona_mgr.reload_persona_from_config()
                        log_info(
                            "[core_initializer] ✅ Persona reloaded with updated config values"
                        )
                    else:
                        log_warning(
                            "[core_initializer] Persona manager not available for reload"
                        )
                except Exception as persona_reload_exc:
                    log_warning(
                        f"[core_initializer] Failed to reload persona from config: {persona_reload_exc}"
                    )
            except Exception as load_exc:
                log_warning(
                    f"[core_initializer] Failed to load configurations from DB: {load_exc}"
                )

            log_debug("[core_initializer] About to call _build_actions_block()")
            try:
                await self._build_actions_block()
                log_debug(
                    "[core_initializer] 🎯 CRITICAL: SECOND CALL - _build_actions_block() returned successfully!"
                )
                log_debug("[core_initializer] Actions block build completed")
            except Exception as e:
                log_error(f"[core_initializer] Error in _build_actions_block: {e}")
                import traceback

                log_error(f"[core_initializer] Traceback: {traceback.format_exc()}")
                self.startup_errors.append(f"Actions block build failed: {e}")

            log_info(
                "[core_initializer] 🎯 CHECKPOINT: Actions block completed, proceeding to interface discovery"
            )

            # NOTE: Interface modules were already discovered earlier (pre-config load)
            log_debug("[core_initializer] Interface modules already discovered earlier")

            # 6. Initialize interface instances now that config is loaded
            log_info("[core_initializer] Initializing interface instances...")
            self._initialize_interface_instances()
            log_info("[core_initializer] ✅ Interface instances initialized")

            # 6.5. Register reload handlers for interfaces
            log_info("[core_initializer] Registering automatic reload handlers...")
            self._register_reload_handlers()
            log_info("[core_initializer] ✅ Reload handlers registered")

            # Note: Startup summary will be displayed by main.py after all interfaces are started
            log_info("[core_initializer] Core initialization completed successfully")

            # Mark initialization as completed
            self._initial_initialization = (
                False  # Reset flag - plugins can now trigger auto-refresh
            )
            log_debug(
                "[core_initializer] Set _initial_initialization=False - auto-refresh now allowed"
            )
            self.initialization_completed = True
            # Many components (plugins/interfaces) are optional by design.
            # Keep startup_errors for diagnostics, but don't abort core startup.
            if self.startup_errors:
                combined = "; ".join(self.startup_errors)
                log_warning(f"[core_initializer] Startup warnings/errors: {combined}")
            log_info(
                "[core_initializer] ✅ All core components initialized successfully"
            )

            # Start database pool cleanup monitor to prevent exhaustion under load
            try:
                from core.db import start_pool_cleanup_task

                await start_pool_cleanup_task()
            except Exception as e:
                log_warning(
                    f"[core_initializer] Failed to start pool cleanup task: {e}"
                )

            # Recover agentic turns that were in-flight when the process last
            # stopped. Agent Lane turns run detached from the message chain, so a
            # container restart leaves their ``agent_tasks`` row stuck as
            # ``running``. Reconcile those orphans on startup so they don't linger
            # forever and so the WebUI reflects reality.
            try:
                await self._recover_interrupted_agent_tasks()
            except Exception as e:
                log_warning(f"[core_initializer] Agent task recovery sweep failed: {e}")

            # Start chat update checker service (non-critical) — only if explicitly configured to auto-start
            try:
                from core.config_manager import config_registry

                auto_start = config_registry.get_value(
                    "CHAT_UPDATE_CHECKER_AUTO_START",
                    False,
                    label="Auto-start chat update checker",
                    description=(
                        "Start the background chat update checker at core startup. "
                        "Keep disabled unless a plugin (like Grillo Observer) needs it running continuously."
                    ),
                    value_type=bool,
                    group="scheduling",
                    component="core",
                    advanced=True,
                )
                if auto_start:
                    from core.chat_update_checker import start_chat_update_checker

                    await start_chat_update_checker()
                else:
                    log_debug(
                        "[core_initializer] Chat update checker auto-start disabled by config; checker will run on demand only"
                    )
            except Exception as e:
                log_warning(
                    f"[core_initializer] Failed to start chat update checker: {e}"
                )

            # Start all registered interfaces
            await self._start_interfaces()

            # Display summary at the end of initialization
            self._display_startup_summary()
            return True

        except Exception as e:
            log_error(f"[core_initializer] Error during initialization: {e}")
            self.startup_errors.append(f"Initialization error: {e}")
            # Also reset flag in case of error
            self._initial_initialization = False
            log_debug(
                "[core_initializer] Set _initial_initialization=False (after error)"
            )
            # Display summary even if initialization failed
            self.display_startup_summary()
            return False

    async def _start_interfaces(self):
        """Start all registered interfaces that have a start method."""
        import asyncio

        log_info("[core_initializer] Starting registered interfaces...")
        log_debug(
            f"[core_initializer] Interfaces in registry: {list(INTERFACE_REGISTRY.keys())}"
        )
        started_count = 0

        for interface_name, interface_instance in INTERFACE_REGISTRY.items():
            # Honour a persistent WebUI disable toggle: skip starting (and drop
            # the actions of) interfaces the operator turned off.
            if not self._is_interface_enabled(interface_name):
                log_info(
                    f"[core_initializer] 🔌 Interface '{interface_name}' disabled by config; not starting"
                )
                self._unregister_plugin_actions(interface_instance)
                if interface_name in self.active_interfaces:
                    self.active_interfaces.remove(interface_name)
                self.track_component(
                    interface_name,
                    "interface",
                    ComponentStatus.SKIPPED,
                    details="Disabled from WebUI",
                )
                continue

            has_start = hasattr(interface_instance, "start")
            is_callable = callable(getattr(interface_instance, "start", None))
            log_debug(
                f"[core_initializer] Interface {interface_name}: has_start={has_start}, is_callable={is_callable}"
            )

            if has_start and is_callable:
                try:
                    log_debug(
                        f"[core_initializer] Starting interface: {interface_name} as background task"
                    )
                    # Start interface as background task to avoid blocking
                    task = asyncio.create_task(interface_instance.start())
                    task.set_name(f"interface_{interface_name}")
                    started_count += 1
                    log_debug(
                        f"[core_initializer] Successfully queued interface: {interface_name}"
                    )
                except Exception as e:
                    log_error(
                        f"[core_initializer] Failed to start interface {interface_name}: {e}"
                    )
                    self.startup_errors.append(
                        f"Interface {interface_name} start failed: {e}"
                    )
            else:
                log_debug(
                    f"[core_initializer] Interface {interface_name} has no start method"
                )
        log_info(
            f"[core_initializer] Started {started_count} interfaces as background tasks"
        )

    def track_component(
        self,
        name: str,
        component_type: str,
        status: ComponentStatus = ComponentStatus.LOADING,
        actions: Optional[List[str]] = None,
        error: str = "",
        details: str = "",
    ):
        """Track the status of a system component."""
        self.components[name] = ComponentInfo(
            name=name,
            type=component_type,
            status=status,
            actions=actions or [],
            error=error,
            details=details,
        )
        log_debug(
            f"[core_initializer] Tracking component {name} ({component_type}): {status.value}"
        )

    def mark_component_success(
        self,
        name: str,
        actions: Optional[List[str]] = None,
        details: str = "",
        module_name: str = "",
        dir_path: str = "",
        category: str = "",
    ):
        """Mark a component as successfully loaded."""
        if name in self.components:
            self.components[name].status = ComponentStatus.SUCCESS
            if actions:
                self.components[name].actions = actions
            if details:
                self.components[name].details = details
            if module_name:
                self.components[name].module_name = module_name
            if dir_path:
                self.components[name].dir_path = dir_path
            if category:
                self.components[name].category = category
        else:
            # Create new component entry
            self.track_component(
                name, "unknown", ComponentStatus.SUCCESS, actions, details=details
            )
            self.components[name].module_name = module_name
            self.components[name].dir_path = dir_path
            self.components[name].category = category

    def mark_component_failed(self, name: str, error: str, details: str = ""):
        """Mark a component as failed to load."""
        if name in self.components:
            self.components[name].status = ComponentStatus.FAILED
            self.components[name].error = error
            if details:
                self.components[name].details = details
        else:
            # Create new component entry
            self.track_component(
                name, "unknown", ComponentStatus.FAILED, error=error, details=details
            )

    def get_system_resume(self) -> Dict[str, Any]:
        """Generate a complete system status resume."""
        successful = [
            c for c in self.components.values() if c.status == ComponentStatus.SUCCESS
        ]
        failed = [
            c for c in self.components.values() if c.status == ComponentStatus.FAILED
        ]
        loading = [
            c for c in self.components.values() if c.status == ComponentStatus.LOADING
        ]

        total_actions = sum(len(c.actions) for c in successful)

        return {
            "total_components": len(self.components),
            "successful": len(successful),
            "failed": len(failed),
            "loading": len(loading),
            "total_actions": total_actions,
            "successful_components": successful,
            "failed_components": failed,
            "loading_components": loading,
            "active_cortex": self.active_cortex,
            "active_interfaces": self.active_interfaces,
            "startup_errors": self.startup_errors,
            "initialization_completed": self.initialization_completed,
        }

    async def _initialize_registries(self):
        """Initialize the core registries."""
        try:
            from core.cortex_registry import register_default_engines

            try:
                register_default_engines(dev_enabled=self._enable_dev_components)
                log_debug("[core_initializer] Cortex registry initialized")
            except Exception as _e:
                log_warning(
                    f"[core_initializer] Cortex registry auto-registration failed: {_e}"
                )

            # The interfaces registry is initialized by each interface when it starts
            log_debug("[core_initializer] Registries initialized successfully")

            # NOTE: flush_env_overrides_to_db() will be called AFTER the Cortex engine is loaded
            # to avoid connection pool deadlocks during initialization
            # After registries are in place, migrate any existing config_registry
            # definitions into the new Exposed Variables registry so metadata/UI
            # info is centralized. This is a best-effort, idempotent migration.
            try:
                # Note: variables_engine is already imported earlier (step 3.5),
                # so exposed variables are already registered at this point.
                migration_module = importlib.import_module("core.exposed_migration")
                migrate_fn = getattr(
                    migration_module, "migrate_all_registered_configs", None
                )
                if callable(migrate_fn):
                    migrate_fn()
                    log_debug(
                        "[core_initializer] Exposed variables migration completed"
                    )
                else:
                    log_warning(
                        "[core_initializer] Exposed variables migration skipped: missing migrate_all_registered_configs"
                    )
            except ModuleNotFoundError:
                log_debug(
                    "[core_initializer] core.exposed_migration not found — skipping"
                )
            except Exception as _e:
                log_warning(
                    f"[core_initializer] Exposed variables migration failed: {_e}"
                )
        except Exception as e:
            log_error(f"[core_initializer] Failed to initialize registries: {e}", e)
            self.startup_errors.append(f"Registry initialization failed: {e}")

    def _configure_trainer_ids(self):
        """Configure trainer IDs from environment configuration."""
        from core.interfaces_registry import get_interface_registry
        from core.config import get_trainer_ids

        registry = get_interface_registry()

        trainer_ids = get_trainer_ids()
        registry.replace_trainer_ids(trainer_ids)
        for interface_name, trainer_id in trainer_ids.items():
            log_debug(
                f"[core_initializer] Configured trainer ID {trainer_id} for {interface_name}"
            )

    async def _load_cortex_engine(self, notify_fn=None):
        """Load the active cortex engine."""
        try:
            from core.config import get_active_cortex_engine

            self.active_cortex = await get_active_cortex_engine()
            self.track_component(
                self.active_cortex,
                "cortex",
                ComponentStatus.LOADING,
                details="Loading Cortex engine",
            )

            # Import here to avoid circular imports
            from core.plugin_instance import load_plugin

            await load_plugin(self.active_cortex, notify_fn=notify_fn)

            # Verify plugin was loaded successfully
            from core.plugin_instance import plugin

            if plugin is None:
                error_msg = f"Plugin {self.active_cortex} failed to load"
                log_error(f"[core_initializer] {error_msg}!")
                self.startup_errors.append(error_msg)
                self.mark_component_failed(
                    self.active_cortex,
                    error_msg,
                    "Cortex plugin initialization failed",
                )
            else:
                log_debug(
                    f"[core_initializer] Plugin {self.active_cortex} loaded successfully: {plugin.__class__.__name__}"
                )
                ok, error = self._evaluate_cortex_health(plugin)
                if ok:
                    self.mark_component_success(
                        self.active_cortex,
                        details=f"Cortex engine: {plugin.__class__.__name__}",
                    )
                else:
                    message = error or "Cortex engine loaded but not ready"
                    log_warning(
                        f"[core_initializer] Cortex engine health check failed: {message}"
                    )
                    self.mark_component_failed(
                        self.active_cortex,
                        message,
                        "Cortex engine configuration incomplete",
                    )

            log_debug(
                f"[core_initializer] Active Cortex engine loaded: {self.active_cortex}"
            )
        except Exception as e:
            error_msg = f"Failed to load active Cortex engine: {repr(e)}"
            log_error(f"[core_initializer] {error_msg}")
            self.startup_errors.append(f"Cortex engine error: {e}")
            if hasattr(self, "active_cortex") and self.active_cortex:
                self.mark_component_failed(
                    self.active_cortex,
                    str(e),
                    "Cortex loading exception",
                )
            else:
                self.track_component(
                    "unknown_cortex", "cortex", ComponentStatus.FAILED, error=str(e)
                )

    def _load_plugins(self):
        """Auto-discover and load all available plugins for validation and startup."""
        # Note: This now actually loads and starts action providers from
        # plugins, Cortex engines and interfaces. Files no longer need to follow a
        # ``*_plugin.py`` naming convention.

        root_dir = Path(__file__).parent.parent
        # Include cortex locations; legacy paths are removed
        search_dirs = ["plugins", "cortex", "interface"]

        # If dev components are enabled, also scan dev directories
        if self._enable_dev_components:
            search_dirs.extend(["plugins_dev", "interface_dev"])
            log_info(
                "[core_initializer] 🔧 Dev components enabled: scanning plugins_dev/ and cortex/*/dev/"
            )

        for base in search_dirs:
            base_path = root_dir / base
            if not base_path.exists():
                continue

            for py_file in base_path.rglob("*.py"):
                if py_file.name == "__init__.py" or py_file.name.startswith("_"):
                    continue

                if (
                    not self._enable_dev_components
                    and "dev" in py_file.parts
                    and "cortex" in py_file.parts
                ):
                    continue

                module_name = ".".join(
                    py_file.relative_to(root_dir).with_suffix("").parts
                )

                # Persistent runtime enable/disable: if this plugin was disabled
                # from the WebUI, skip loading it entirely (TRUE unload — behaves
                # as if the file did not exist) but keep a "ghost" component
                # record so the UI still lists it as a disabled (grey) plugin.
                plugin_short_name = module_name.split(".")[-1]
                if not self._is_plugin_enabled(plugin_short_name):
                    dir_path = str(py_file.parent)
                    category = derive_plugin_category(module_name, dir_path)
                    self.track_component(
                        plugin_short_name,
                        "plugin",
                        ComponentStatus.SKIPPED,
                        details="Disabled from WebUI",
                    )
                    info = self.components.get(plugin_short_name)
                    if info is not None:
                        info.module_name = module_name
                        info.dir_path = dir_path
                        info.category = category
                    log_info(
                        f"[core_initializer] ⏭️ Plugin '{plugin_short_name}' is disabled; skipping load"
                    )
                    continue

                # Enforce policy: plugin files must not write directly to queue internals
                try:
                    content = py_file.read_text(encoding="utf-8")
                    # Detect direct writes to the queue internals to enforce use of enqueue APIs
                    if (
                        "message_queue._queue.put" in content
                        or "message_queue._queue" in content
                        or "_queue.put(" in content
                        or "_queue._queue.put" in content
                        or "_queue._queue" in content
                    ):
                        err_msg = f"Plugin {py_file} writes directly to queue internals; please use enqueue()/enqueue_low_priority()"
                        log_error(f"[core_initializer] {err_msg}")
                        self.startup_errors.append(err_msg)
                        continue
                except Exception:
                    log_debug(
                        f"[core_initializer] Could not inspect plugin file for queue write policy: {py_file}"
                    )

                try:
                    module = importlib.import_module(module_name)
                except Exception as e:
                    log_warning(
                        f"[core_initializer] ⚠️ Failed to import {module_name}: {e}"
                    )
                    self.startup_errors.append(f"Module {module_name}: {e}")
                    continue

                if not hasattr(module, "PLUGIN_CLASS"):
                    continue

                plugin_class = getattr(module, "PLUGIN_CLASS")

                if plugin_class is None:
                    continue

                if not (
                    hasattr(plugin_class, "get_supported_action_types")
                    or hasattr(plugin_class, "get_supported_actions")
                ):
                    log_warning(
                        f"[core_initializer] ⚠️ Plugin {module_name} doesn't implement action interface"
                    )
                    continue

                try:
                    init_sig = inspect.signature(plugin_class.__init__)
                    required = [
                        p
                        for name, p in list(init_sig.parameters.items())[1:]
                        if p.default is inspect.Parameter.empty
                        and p.kind
                        in (
                            inspect.Parameter.POSITIONAL_ONLY,
                            inspect.Parameter.POSITIONAL_OR_KEYWORD,
                        )
                    ]
                    if required:
                        log_debug(
                            f"[core_initializer] Skipping {module_name}: constructor requires params"
                        )
                        continue

                    instance = plugin_class()

                    # Register the plugin immediately after instantiation so it's available for action discovery
                    PLUGIN_REGISTRY[plugin_short_name] = instance
                    log_debug(
                        f"[core_initializer] Plugin {module_name} registered in PLUGIN_REGISTRY as '{plugin_short_name}'"
                    )

                    if hasattr(instance, "start"):
                        try:
                            if asyncio.iscoroutinefunction(instance.start):
                                if not hasattr(self, "_pending_async_plugins"):
                                    self._pending_async_plugins = []
                                if not any(
                                    pending_name == module_name
                                    for pending_name, _ in self._pending_async_plugins
                                ):
                                    self._pending_async_plugins.append(
                                        (module_name, instance)
                                    )
                                log_info(
                                    f"[core_initializer] Queued async plugin for startup: {module_name}"
                                )
                            else:
                                instance.start()
                                log_info(
                                    f"[core_initializer] Started sync plugin: {module_name}"
                                )
                        except Exception as e:
                            log_error(
                                f"[core_initializer] Error starting plugin {module_name}: {repr(e)}"
                            )
                    else:
                        log_debug(
                            f"[core_initializer] Plugin {module_name} has no start method"
                        )

                    # Resolve declared category (if the plugin overrides
                    # get_metadata) so it takes precedence over the location
                    # based fallback.
                    declared_category = None
                    try:
                        meta = instance.get_metadata()
                        if isinstance(meta, dict):
                            declared_category = meta.get("category")
                    except Exception:
                        declared_category = None

                    dir_path = str(py_file.parent)
                    category = derive_plugin_category(
                        module_name, dir_path, declared_category
                    )
                    # Full plugin path from the project root, e.g.
                    # "plugins/grillo/grillo_chat_observer.py".
                    rel_path = py_file.relative_to(root_dir).as_posix()

                    # Track success for WebUI diagnostics
                    self.track_component(
                        plugin_short_name,
                        "plugin",
                        ComponentStatus.LOADING,
                    )
                    self.mark_component_success(
                        plugin_short_name,
                        details=f"Loaded from: {rel_path}",
                        module_name=module_name,
                        dir_path=dir_path,
                        category=category,
                    )

                except Exception as e:
                    log_error(
                        f"[core_initializer] Failed to start plugin {module_name}: {repr(e)}"
                    )
                    self.startup_errors.append(f"Plugin {module_name}: {e}")

    async def _initialize_persona_manager(self):
        """Initialize the core persona manager and await async init."""
        try:
            import importlib

            importlib.import_module("core.persona_manager")
            # Ensure the PersonaManager instance exists and run its async_init
            from core.persona_manager import get_persona_manager

            manager = get_persona_manager()
            if manager and hasattr(manager, "async_init"):
                await manager.async_init()
            log_debug(
                "[core_initializer] Persona manager initialized and async_init awaited"
            )
        except Exception as e:
            log_error(f"[core_initializer] Failed to initialize persona manager: {e}")
            self.startup_errors.append(f"Persona manager: {e}")

    def _ensure_core_actions(self):
        """Ensure core actions are loaded."""
        # Core actions are loaded automatically when imported
        log_debug("[core_initializer] Core actions check completed")

    def _discover_interfaces(self):
        """Auto-discover and import all interface modules from interface directory and core webui."""
        import pkgutil
        import importlib

        # First, load core webui (it's now a core component)
        try:
            log_debug("[core_initializer] Loading core WebUI component...")
            importlib.import_module("core.webui")
            log_debug("[core_initializer] Core WebUI loaded successfully")
        except Exception as e:
            import traceback

            tb = traceback.format_exc()
            log_warning(f"[core_initializer] Failed to import core WebUI: {e}")
            log_error(f"[core_initializer] Traceback while importing core.webui:\n{tb}")
            # Record the error with some traceback so we can diagnose import-time failures
            self.startup_errors.append(f"Core WebUI: {e} -- {tb}")
        finally:
            # Diagnostic dump: record whether core.webui is present in sys.modules
            try:
                import sys

                present = "core.webui" in sys.modules
                log_debug(
                    f"[core_initializer] Diagnostic: 'core.webui' in sys.modules = {present}"
                )
                if present:
                    mod = sys.modules.get("core.webui")
                    try:
                        has_init = hasattr(mod, "initialize_interface")
                        log_debug(
                            f"[core_initializer] core.webui module loaded: initialize_interface present={has_init}"
                        )
                    except Exception:
                        log_debug(
                            "[core_initializer] core.webui module loaded but unable to inspect initialize_interface"
                        )
            except Exception:
                pass

        # Discover interfaces from interface/ directory
        directories_to_scan = ["interface"]

        # If dev components are enabled, also scan interface_dev/
        if self._enable_dev_components:
            directories_to_scan.append("interface_dev")
            log_info(
                "[core_initializer] 🔧 Dev components enabled: scanning interface_dev/"
            )

        for dir_name in directories_to_scan:
            try:
                module = importlib.import_module(dir_name)
                module_file = getattr(module, "__file__", None)
                if not module_file:
                    log_warning(
                        f"[core_initializer] Skipping {dir_name} scan: module has no __file__"
                    )
                    continue
                module_path = os.path.dirname(module_file)

                log_debug(
                    f"[core_initializer] Scanning {dir_name} directory: {module_path}"
                )

                # Auto-discover all modules in package
                for importer, module_name, is_pkg in pkgutil.iter_modules(
                    [module_path]
                ):
                    if not is_pkg and not module_name.startswith("_"):
                        full_module_path = f"{dir_name}.{module_name}"
                        try:
                            log_debug(
                                f"[core_initializer] Importing interface module: {module_name} from {dir_name}"
                            )
                            importlib.import_module(full_module_path)
                            log_debug(
                                f"[core_initializer] Successfully imported: {module_name}"
                            )
                        except Exception as e:
                            import traceback

                            tb = traceback.format_exc()
                            log_warning(
                                f"[core_initializer] Failed to import interface {module_name}: {e}"
                            )
                            log_error(
                                f"[core_initializer] Traceback while importing {full_module_path}:\n{tb}"
                            )
                            self.startup_errors.append(
                                f"Interface {full_module_path}: {e} -- {tb}"
                            )

                log_debug(f"[core_initializer] {dir_name} auto-discovery complete")

            except Exception as e:
                import traceback

                tb = traceback.format_exc()
                log_error(f"[core_initializer] Error during {dir_name} discovery: {e}")
                log_error(
                    f"[core_initializer] Traceback during discovery of {dir_name}:\n{tb}"
                )
                self.startup_errors.append(f"{dir_name} discovery failed: {e} -- {tb}")

    def _initialize_interface_instances(self):
        """Initialize interface instances after config has been loaded from DB.

        This calls the initialize_interface() function on each interface module
        that exposes it. This allows interfaces to create their instances with
        the correct configuration values loaded from the database.
        """
        import sys

        # Get all loaded interface modules
        interface_modules = [
            name
            for name in sys.modules.keys()
            if name.startswith("interface.")
            or name.startswith("interface_dev.")
            or name == "core.webui"
        ]

        import sys as _sys

        log_debug(
            f"[core_initializer] Found {len(interface_modules)} interface modules to initialize: {interface_modules}"
        )
        # Diagnostic: check if core.webui is in sys.modules
        try:
            present = "core.webui" in _sys.modules
            log_debug(
                f"[core_initializer] Diagnostic: 'core.webui' in sys.modules = {present}"
            )
        except Exception:
            pass

        for module_name in interface_modules:
            try:
                module = sys.modules[module_name]

                # Check if module has initialize_interface function
                if hasattr(module, "initialize_interface"):
                    log_debug(
                        f"[core_initializer] Calling initialize_interface() for {module_name}"
                    )
                    init_func = getattr(module, "initialize_interface")
                    init_func()
                    log_debug(
                        f"[core_initializer] Successfully initialized {module_name}"
                    )
                else:
                    log_debug(
                        f"[core_initializer] Module {module_name} has no initialize_interface function"
                    )

            except Exception as e:
                import traceback

                tb = traceback.format_exc()
                log_warning(
                    f"[core_initializer] Failed to initialize interface {module_name}: {e}"
                )
                log_error(
                    f"[core_initializer] Traceback while initializing {module_name}:\n{tb}"
                )
                self.startup_errors.append(
                    f"Interface initialization {module_name}: {e} -- {tb}"
                )

        # After attempting initialization, dump registry and startup errors for diagnostics
        try:
            log_info(
                "[core_initializer] Diagnostic: INTERFACE_REGISTRY keys after initialization: "
                f"{list(INTERFACE_REGISTRY.keys())}"
            )
        except Exception:
            pass
        try:
            log_info(
                f"[core_initializer] Diagnostic: startup_errors: {self.startup_errors}"
            )
        except Exception:
            pass

    def _register_reload_handlers(self):
        """Register automatic reload handlers for components that need them.

        This ensures that when a configuration variable with needs_component_reload=True
        is changed, the corresponding component's reload handler is triggered automatically.
        """
        from core.config_manager import config_registry
        import sys

        # Discover reload handlers from registered interface modules (agnostic)
        for interface_name, interface_instance in INTERFACE_REGISTRY.items():
            try:
                module_name = getattr(interface_instance, "__module__", None)
                module = sys.modules.get(module_name) if module_name else None
                if module and hasattr(module, "reload_interface"):
                    handler = getattr(module, "reload_interface")
                    config_registry.register_reload_handler(interface_name, handler)
                    log_info(
                        f"[core_initializer] ✅ Reload handler registered for component: {interface_name}"
                    )
            except Exception as e:
                log_error(
                    f"[core_initializer] Failed to register reload handler for {interface_name}: {e}"
                )

        # Register reload handlers for Cortex engines (e.g., API keys)
        try:
            from core.config import (
                list_available_cortex_engines,
                get_active_cortex_engine,
            )
            from core.cortex_registry import get_cortex_registry
            from core.plugin_instance import load_plugin

            for engine_name in list_available_cortex_engines():

                async def _reload_cortex_engine(engine_name=engine_name):
                    cortex_registry = get_cortex_registry()
                    try:
                        active = await get_active_cortex_engine()
                    except Exception:
                        active = None

                    if active == engine_name:
                        await load_plugin(engine_name, ensure_started=True)
                        from core.plugin_instance import plugin as active_plugin

                        if active_plugin is None:
                            self.mark_component_failed(
                                engine_name,
                                "Cortex reload returned no instance",
                                "Reload failed",
                            )
                        else:
                            ok, error = self._evaluate_cortex_health(active_plugin)
                            if ok:
                                self.mark_component_success(
                                    engine_name,
                                    details=f"Cortex engine: {active_plugin.__class__.__name__}",
                                )
                            else:
                                message = error or "Cortex engine loaded but not ready"
                                self.mark_component_failed(
                                    engine_name,
                                    message,
                                    "Cortex engine configuration incomplete",
                                )
                    else:
                        if cortex_registry.get_engine(engine_name):
                            cortex_registry.unload_engine(engine_name)
                        cortex_registry.load_engine(engine_name)

                config_registry.register_reload_handler(
                    engine_name, _reload_cortex_engine
                )
                log_info(
                    f"[core_initializer] ✅ Reload handler registered for Cortex engine: {engine_name}"
                )
        except Exception as e:
            log_warning(
                f"[core_initializer] Failed to register Cortex reload handlers: {e}"
            )

    def _missing_required_config_vars(self, interface_instance: Any) -> List[str]:
        """Return the list of declared-required config keys that are absent.

        An interface declares its "must-have" configuration by exposing a
        ``required_config_vars`` attribute — an iterable of config-registry keys
        (e.g. Telegram: ``["BOTFATHER_TOKEN"]``, Discord: ``["DISCORD_BOT_TOKEN"]``).
        Each entry may be either:

        * a plain string — that key must be present (AND semantics), or
        * a tuple/list of strings — at least ONE of them must be present
          (OR semantics), e.g. Matrix accepts either a password or an access
          token: ``[("MATRIX_PASSWORD", "MATRIX_ACCESS_TOKEN")]``.

        The loader resolves each key through ``config_registry`` and treats a
        value that is ``None`` or an empty/whitespace string as missing. The
        interface itself performs no gating — it only declares intent.
        """
        if interface_instance is None:
            return []

        required = getattr(interface_instance, "required_config_vars", None)
        if not required:
            return []

        try:
            from core.config_manager import config_registry
        except Exception as e:  # pragma: no cover - defensive
            log_debug(
                f"[core_initializer] Unable to access config_registry for required "
                f"var check: {e}"
            )
            return []

        def _read_persisted_from_db(key: str) -> Any:
            """Read the DB-persisted value even inside a running event loop.

            ``config_registry.get_value`` (and its ``_load_from_db_sync``) skip
            the DB read when an event loop is already running — which is exactly
            the case during interface registration at startup. That makes a
            value saved through the WebUI look "missing" (false negative). To get
            the truth we run the async DB read in a dedicated thread that owns
            its own event loop, blocking only this loader gate.
            """
            import asyncio
            import concurrent.futures

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                # No running loop — the plain sync path already reads the DB.
                return None

            def _runner() -> Any:
                return asyncio.run(config_registry.get_persisted_value(str(key), None))

            try:
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(_runner).result(timeout=10)
            except Exception as e:  # pragma: no cover - defensive
                log_debug(
                    f"[core_initializer] DB read fallback failed for '{key}': {e}"
                )
                return None

        def _is_empty(value: Any) -> bool:
            if value is None:
                return True
            if isinstance(value, str) and not value.strip():
                return True
            return False

        def _present(key: str) -> bool:
            try:
                value = config_registry.get_value(str(key), None)
            except Exception as e:  # pragma: no cover - defensive
                log_debug(
                    f"[core_initializer] Error reading required config var '{key}': {e}"
                )
                value = None
            if _is_empty(value):
                # Fall back to a direct DB read: get_value skips the DB while an
                # event loop is running, so a WebUI-saved value would otherwise
                # be reported as missing (false negative).
                value = _read_persisted_from_db(str(key))
            return not _is_empty(value)

        missing: List[str] = []
        for entry in required:
            if isinstance(entry, (list, tuple, set)):
                # OR group: satisfied if any member is present.
                group = [str(k) for k in entry]
                if not any(_present(k) for k in group):
                    missing.append(" or ".join(group))
            else:
                if not _present(str(entry)):
                    missing.append(str(entry))
        return missing

    def register_interface(self, interface_name: str):
        """Register an active interface."""
        log_info(
            f"[core_initializer] 🔍 Attempting to register interface: {interface_name}"
        )

        interface_instance = INTERFACE_REGISTRY.get(interface_name)
        enabled = True
        disabled_reason = None
        if interface_instance is not None:
            enabled = getattr(interface_instance, "is_enabled", True)
            disabled_reason = getattr(interface_instance, "disabled_reason", None)

        if not enabled:
            reason = disabled_reason or "awaiting configuration"
            self.track_component(
                interface_name, "interface", ComponentStatus.SKIPPED, details=reason
            )
            log_info(
                f"🔌 Interface registered but disabled: {interface_name} ({reason})"
            )
            return

        # Declarative "must-have" configuration gate. An interface may declare a
        # ``required_config_vars`` attribute (list of config keys). The LOADER —
        # not the interface — verifies they are present. If any is missing/empty,
        # the interface is NOT loaded: it registers no actions (so its schemas do
        # not flood the LLM prompt) and is marked FAILED (red LED) in the WebUI.
        missing_vars = self._missing_required_config_vars(interface_instance)
        if missing_vars:
            reason = "Missing required configuration: " + ", ".join(missing_vars)
            self.track_component(interface_name, "interface", ComponentStatus.LOADING)
            # Pass the reason ONLY as the error (red line in the WebUI). Do NOT
            # also set it as ``details`` — the WebUI renders both ``details`` and
            # ``error``, so duplicating the text there shows the same message
            # twice (once black, once red). Leaving details empty lets the black
            # line fall back to the interface's own description.
            self.mark_component_failed(
                interface_name,
                reason,
            )
            # Keep the component typed as an interface for the WebUI.
            comp = self.components.get(interface_name)
            if comp is not None:
                comp.type = "interface"
                comp.category = "Interfaces"
            log_warning(
                f"🔌 Interface not loaded: {interface_name} ({reason}) — "
                "actions withheld from prompt"
            )
            return

        if interface_name not in self.active_interfaces:
            self.active_interfaces.append(interface_name)

            # Check if the interface exposes action schemas and log them
            actions: List[str] = []

            if interface_instance and hasattr(
                interface_instance, "get_supported_actions"
            ):
                try:
                    supported_actions = interface_instance.get_supported_actions()
                    if isinstance(supported_actions, dict):
                        actions = [str(a) for a in supported_actions.keys()]
                except Exception as e:
                    log_debug(
                        f"[core_initializer] Error getting actions for interface {interface_name}: {e}"
                    )

            if actions:
                log_info(
                    f"🔌 Interface loaded: {interface_name} - Registered actions: {', '.join(sorted(actions))}"
                )
            else:
                log_info(
                    f"🔌 Interface loaded: {interface_name} - No actions registered"
                )

            # Track the interface as a successfully loaded component so the WebUI
            # reports it as active (green LED) rather than "inactive" (grey). An
            # enabled interface that reaches this point is running; without this
            # its ComponentInfo stayed absent/LOADING and the components summary
            # fell back to an "unknown"/grey status.
            self.track_component(
                interface_name,
                "interface",
                ComponentStatus.LOADING,
                actions=actions,
            )
            self.mark_component_success(
                interface_name,
                actions=actions,
                category="Interfaces",
            )

            # After registering, rebuild actions to expose interface capabilities
            # BUT NOT during initial initialization (to avoid triggering rebuild while already building)
            # DISABLED: This causes infinite loops when interfaces register after initialization
            # TODO: Implement a smarter rebuild mechanism that doesn't re-import modules
            if False and not self._initial_initialization:
                try:
                    _schedule_rebuild_actions(self)
                except Exception as e:  # pragma: no cover - defensive
                    log_error(
                        f"[core_initializer] Error scheduling actions rebuild for {interface_name}: {e}"
                    )
            else:
                log_debug(
                    f"[core_initializer] Skipping actions rebuild for {interface_name} (initial initialization in progress)"
                )

            # Show updated status after interface registration
            self._show_interface_status()
        else:
            log_info(
                f"[core_initializer] 🔄 Interface {interface_name} is already registered"
            )

    def _show_interface_status(self):
        """Show current interface status."""
        if self.active_interfaces:
            interfaces_str = ", ".join(self.active_interfaces)
            log_info(f"📡 Active Interfaces: {interfaces_str}")
        else:
            log_info("📡 Active Interfaces: None")

    # ------------------------------------------------------------------
    # Runtime plugin enable/disable (WebUI plugins section — Phase C)
    # ------------------------------------------------------------------
    @staticmethod
    def _plugin_enabled_config_key(plugin_short_name: str) -> str:
        """Config-registry key holding a plugin's persistent enabled flag."""
        return f"PLUGIN_ENABLED__{plugin_short_name}"

    @staticmethod
    def is_core_plugin(plugin_short_name: str) -> bool:
        """Return True for curated core plugins that cannot be disabled."""
        return plugin_short_name in CORE_PLUGIN_SHORT_NAMES

    def _is_plugin_enabled(self, plugin_short_name: str) -> bool:
        """Return the persistent enabled state for a plugin (default: True).

        Core plugins are always enabled. The value is read from the config
        registry so a WebUI toggle survives restarts.
        """
        if self.is_core_plugin(plugin_short_name):
            return True
        try:
            from core.config_manager import config_registry

            value = config_registry.get_value(
                self._plugin_enabled_config_key(plugin_short_name),
                True,
                value_type=bool,
                component=plugin_short_name,
                group="plugins",
                hidden=True,
                label=f"{plugin_short_name} enabled",
                description="Runtime enable/disable flag for this plugin.",
            )
            return bool(value)
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(
                f"[core_initializer] Failed to read enabled flag for {plugin_short_name}: {exc}"
            )
            return True

    def _unregister_plugin_actions(self, instance: Any) -> None:
        """Remove every action handler owned by ``instance`` from the registry."""
        to_delete: List[str] = []
        for action_type, handler in list(ACTION_REGISTRY.items()):
            if isinstance(handler, list):
                remaining = [h for h in handler if h is not instance]
                if not remaining:
                    to_delete.append(action_type)
                elif len(remaining) == 1:
                    ACTION_REGISTRY[action_type] = remaining[0]
                else:
                    ACTION_REGISTRY[action_type] = remaining
            elif handler is instance:
                to_delete.append(action_type)
        for action_type in to_delete:
            ACTION_REGISTRY.pop(action_type, None)

    def _invalidate_action_caches(self) -> None:
        """Drop the action_parser caches so the change is seen immediately."""
        try:
            from core import action_parser

            action_parser._ACTION_PLUGINS = None
            action_parser._ACTION_HANDLERS = None  # type: ignore[attr-defined]
            action_parser._INTERFACE_ACTIONS = None
        except Exception:  # pragma: no cover - defensive
            pass

    async def disable_plugin(self, plugin_short_name: str) -> Dict[str, Any]:
        """Disable a plugin at runtime with a TRUE unload (no restart).

        The plugin instance is stopped, its actions are removed from the
        registry, and it is dropped from ``PLUGIN_REGISTRY`` — as if the file
        did not exist. A disabled "ghost" component record is kept so the WebUI
        still lists it (grey). The state is persisted via the config registry.
        """
        if self.is_core_plugin(plugin_short_name):
            return {
                "ok": False,
                "error": "core_plugin_cannot_be_disabled",
                "name": plugin_short_name,
            }

        instance = PLUGIN_REGISTRY.get(plugin_short_name)
        if instance is not None:
            # Best-effort teardown.
            for hook in ("stop", "teardown", "shutdown"):
                fn = getattr(instance, hook, None)
                if callable(fn):
                    try:
                        result = fn()
                        if asyncio.iscoroutine(result):
                            await result
                    except Exception as exc:  # pragma: no cover - defensive
                        log_warning(
                            f"[core_initializer] {hook}() failed for {plugin_short_name}: {exc}"
                        )
                    break

            self._unregister_plugin_actions(instance)
            PLUGIN_REGISTRY.pop(plugin_short_name, None)
            if plugin_short_name in self.loaded_plugins:
                self.loaded_plugins.remove(plugin_short_name)

        # Keep a ghost record so the UI lists it as disabled.
        info = self.components.get(plugin_short_name)
        if info is not None:
            info.status = ComponentStatus.SKIPPED
            info.actions = []
            info.details = "Disabled from WebUI"
        else:
            self.track_component(
                plugin_short_name,
                "plugin",
                ComponentStatus.SKIPPED,
                details="Disabled from WebUI",
            )

        self._invalidate_action_caches()
        await self._build_actions_block()

        # Persist the state. Read the flag first so its config-registry
        # definition is (lazily) registered before set_value writes to it.
        try:
            from core.config_manager import config_registry

            self._is_plugin_enabled(plugin_short_name)
            await config_registry.set_value(
                self._plugin_enabled_config_key(plugin_short_name), False
            )
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(
                f"[core_initializer] Failed to persist disabled state for {plugin_short_name}: {exc}"
            )

        log_info(
            f"[core_initializer] 🔻 Plugin '{plugin_short_name}' disabled (unloaded)"
        )
        return {"ok": True, "name": plugin_short_name, "enabled": False}

    async def enable_plugin(self, plugin_short_name: str) -> Dict[str, Any]:
        """Re-enable a previously disabled plugin at runtime (no restart).

        The plugin module is (re)imported, instantiated and registered exactly
        as during startup. The persistent flag is updated so the change sticks.
        """
        info = self.components.get(plugin_short_name)
        module_name = getattr(info, "module_name", "") if info else ""

        # Persist enabled first so any re-load path honours it.
        try:
            from core.config_manager import config_registry

            self._is_plugin_enabled(plugin_short_name)
            await config_registry.set_value(
                self._plugin_enabled_config_key(plugin_short_name), True
            )
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(
                f"[core_initializer] Failed to persist enabled state for {plugin_short_name}: {exc}"
            )

        if plugin_short_name in PLUGIN_REGISTRY:
            log_debug(
                f"[core_initializer] Plugin '{plugin_short_name}' already loaded; nothing to do"
            )
            return {"ok": True, "name": plugin_short_name, "enabled": True}

        if not module_name:
            return {
                "ok": False,
                "error": "unknown_plugin_module",
                "name": plugin_short_name,
            }

        ok = await self._instantiate_and_register(module_name, plugin_short_name)
        if not ok:
            return {
                "ok": False,
                "error": "instantiation_failed",
                "name": plugin_short_name,
            }

        self._invalidate_action_caches()
        await self._build_actions_block()
        log_info(f"[core_initializer] 🔺 Plugin '{plugin_short_name}' enabled (loaded)")
        return {"ok": True, "name": plugin_short_name, "enabled": True}

    # ------------------------------------------------------------------
    # Runtime interface enable/disable (WebUI plugins/interfaces grid)
    # ------------------------------------------------------------------
    @staticmethod
    def _interface_enabled_config_key(interface_name: str) -> str:
        """Config-registry key holding an interface's persistent enabled flag."""
        return f"INTERFACE_ENABLED__{interface_name}"

    @staticmethod
    def is_core_interface(interface_name: str) -> bool:
        """Return True for interfaces that cannot be disabled from the WebUI."""
        return interface_name in CORE_INTERFACE_NAMES

    def _is_interface_enabled(self, interface_name: str) -> bool:
        """Return the persistent enabled state for an interface (default: True).

        Core interfaces are always enabled. The value is read from the config
        registry so a WebUI toggle survives restarts.
        """
        if self.is_core_interface(interface_name):
            return True
        try:
            from core.config_manager import config_registry

            value = config_registry.get_value(
                self._interface_enabled_config_key(interface_name),
                True,
                value_type=bool,
                component=interface_name,
                group="interfaces",
                hidden=True,
                label=f"{interface_name} enabled",
                description="Runtime enable/disable flag for this interface.",
            )
            return bool(value)
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(
                f"[core_initializer] Failed to read enabled flag for interface {interface_name}: {exc}"
            )
            return True

    async def _teardown_interface_instance(self, interface_instance: Any) -> None:
        """Best-effort stop/teardown of a running interface instance."""
        for hook in ("stop", "teardown", "shutdown", "close"):
            fn = getattr(interface_instance, hook, None)
            if callable(fn):
                try:
                    result = fn()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception as exc:  # pragma: no cover - defensive
                    log_warning(
                        f"[core_initializer] {hook}() failed for interface: {exc}"
                    )
                break

    async def disable_interface(self, interface_name: str) -> Dict[str, Any]:
        """Disable an interface at runtime (stop it, drop its actions).

        The instance is kept in ``INTERFACE_REGISTRY`` (so it can be re-enabled
        without a re-import) but stopped, its actions removed from the registry
        and it is dropped from ``active_interfaces``. A grey ghost component
        record is kept so the WebUI still lists it. State is persisted.
        """
        if self.is_core_interface(interface_name):
            return {
                "ok": False,
                "error": "core_interface_cannot_be_disabled",
                "name": interface_name,
            }

        instance = INTERFACE_REGISTRY.get(interface_name)
        if instance is not None:
            await self._teardown_interface_instance(instance)
            self._unregister_plugin_actions(instance)

        if interface_name in self.active_interfaces:
            self.active_interfaces.remove(interface_name)

        info = self.components.get(interface_name)
        if info is not None:
            info.status = ComponentStatus.SKIPPED
            info.actions = []
            info.details = "Disabled from WebUI"
        else:
            self.track_component(
                interface_name,
                "interface",
                ComponentStatus.SKIPPED,
                details="Disabled from WebUI",
            )

        self._invalidate_action_caches()
        await self._build_actions_block()

        try:
            from core.config_manager import config_registry

            self._is_interface_enabled(interface_name)
            await config_registry.set_value(
                self._interface_enabled_config_key(interface_name), False
            )
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(
                f"[core_initializer] Failed to persist disabled state for interface {interface_name}: {exc}"
            )

        log_info(
            f"[core_initializer] 🔻 Interface '{interface_name}' disabled (stopped)"
        )
        return {"ok": True, "name": interface_name, "enabled": False}

    async def enable_interface(self, interface_name: str) -> Dict[str, Any]:
        """Re-enable a previously disabled interface at runtime (no restart).

        The existing instance in ``INTERFACE_REGISTRY`` is re-registered
        (actions + active list) and started again. State is persisted.
        """
        try:
            from core.config_manager import config_registry

            self._is_interface_enabled(interface_name)
            await config_registry.set_value(
                self._interface_enabled_config_key(interface_name), True
            )
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(
                f"[core_initializer] Failed to persist enabled state for interface {interface_name}: {exc}"
            )

        instance = INTERFACE_REGISTRY.get(interface_name)
        if instance is None:
            return {
                "ok": False,
                "error": "unknown_interface",
                "name": interface_name,
            }

        # Re-register supported actions.
        if hasattr(instance, "get_supported_actions"):
            try:
                for act in instance.get_supported_actions().keys():
                    register_action(act, instance)
            except Exception as exc:  # pragma: no cover - defensive
                log_error(
                    f"[core_initializer] Failed to register actions for interface {interface_name} on enable: {exc}"
                )

        if interface_name not in self.active_interfaces:
            self.active_interfaces.append(interface_name)

        info = self.components.get(interface_name)
        if info is not None:
            info.status = ComponentStatus.SUCCESS
            info.details = ""

        # Restart the interface's async loop as a background task.
        start_fn = getattr(instance, "start", None)
        if callable(start_fn):
            try:
                task = asyncio.create_task(instance.start())
                task.set_name(f"interface_{interface_name}")
            except Exception as exc:  # pragma: no cover - defensive
                log_warning(
                    f"[core_initializer] start() failed for interface {interface_name} on enable: {exc}"
                )

        self._invalidate_action_caches()
        await self._build_actions_block()
        log_info(
            f"[core_initializer] 🔺 Interface '{interface_name}' enabled (started)"
        )
        return {"ok": True, "name": interface_name, "enabled": True}

    async def _instantiate_and_register(
        self, module_name: str, plugin_short_name: str
    ) -> bool:
        """(Re)import a plugin module, instantiate it and register it.

        Mirrors the startup load path (``_load_plugins``) for a single plugin.
        Returns True on success. Used by :meth:`enable_plugin`.
        """
        try:
            module = importlib.import_module(module_name)
            importlib.reload(module)
        except Exception as exc:
            log_error(
                f"[core_initializer] Failed to import {module_name} on enable: {exc}"
            )
            return False

        plugin_class = getattr(module, "PLUGIN_CLASS", None)
        if plugin_class is None:
            log_error(f"[core_initializer] {module_name} has no PLUGIN_CLASS on enable")
            return False

        try:
            instance = plugin_class()
        except Exception as exc:
            log_error(
                f"[core_initializer] Failed to instantiate {module_name} on enable: {exc}"
            )
            return False

        PLUGIN_REGISTRY[plugin_short_name] = instance

        if hasattr(instance, "get_supported_actions"):
            try:
                supported = instance.get_supported_actions()
                if isinstance(supported, dict):
                    for act in supported.keys():
                        register_action(act, instance)
            except Exception as exc:
                log_error(
                    f"[core_initializer] Failed to register actions for {module_name} on enable: {exc}"
                )

        if hasattr(instance, "start"):
            try:
                if asyncio.iscoroutinefunction(instance.start):
                    await instance.start()
                else:
                    instance.start()
            except Exception as exc:
                log_warning(
                    f"[core_initializer] start() failed for {plugin_short_name} on enable: {exc}"
                )

        module_file = getattr(module, "__file__", "") or ""
        dir_path = str(Path(module_file).parent) if module_file else ""
        declared_category = None
        try:
            meta = instance.get_metadata()
            if isinstance(meta, dict):
                declared_category = meta.get("category")
        except Exception:
            declared_category = None
        category = derive_plugin_category(module_name, dir_path, declared_category)

        # Full plugin path from the project root, e.g.
        # "plugins/grillo/grillo_chat_observer.py". Fall back to the module
        # name if the file lies outside the project root for any reason.
        root_dir = Path(__file__).parent.parent
        rel_path = module_name
        if module_file:
            try:
                rel_path = Path(module_file).resolve().relative_to(root_dir).as_posix()
            except ValueError:
                rel_path = module_name

        self.track_component(plugin_short_name, "plugin", ComponentStatus.LOADING)
        self.mark_component_success(
            plugin_short_name,
            details=f"Loaded from: {rel_path}",
            module_name=module_name,
            dir_path=dir_path,
            category=category,
        )
        if plugin_short_name not in self.loaded_plugins:
            self.loaded_plugins.append(plugin_short_name)
        return True

    async def refresh_actions_block(self) -> None:
        """Public helper to rebuild the actions block.

        Ensures recently registered plugins or interfaces expose their
        actions immediately to the rest of the system.
        """
        await self._build_actions_block()

    async def start_pending_async_plugins(self):
        """Start async plugins that were pending due to no event loop."""
        if hasattr(self, "_pending_async_plugins"):
            pending_plugins = list(self._pending_async_plugins)
            self._pending_async_plugins.clear()
            for plugin_name, instance in pending_plugins:
                try:
                    await instance.start()
                    log_info(
                        f"[core_initializer] ✅ Started pending async plugin: {plugin_name}"
                    )
                except Exception as e:
                    log_error(
                        f"[core_initializer] Error starting pending plugin {plugin_name}: {repr(e)}"
                    )
            log_info("[core_initializer] All pending async plugins processed")

        # Ensure the Grillo autonomous-beat plugin (and its sub-plugins, e.g. the
        # LLM-failure recovery plugin) is started. GrilloPlugin is registered in
        # PLUGIN_REGISTRY but is not discovered by the generic plugin loop above,
        # so its start() (which launches the beat scheduler + recovery loop) must
        # be invoked explicitly here.
        try:
            grillo = PLUGIN_REGISTRY.get("grillo_plugin")
            if grillo is not None and hasattr(grillo, "start"):
                # Guard against double-start (idempotent): GrilloPlugin exposes
                # _running once its beat loop is active.
                if not getattr(grillo, "_running", False):
                    if asyncio.iscoroutinefunction(grillo.start):
                        await grillo.start()
                    else:
                        grillo.start()
                    log_info(
                        "[core_initializer] ✅ Started GrilloPlugin (beats + recovery)"
                    )
                else:
                    log_debug(
                        "[core_initializer] GrilloPlugin already running; skip start"
                    )
        except Exception as e:
            log_error(f"[core_initializer] Error starting GrilloPlugin: {repr(e)}")
            self.startup_errors.append(f"GrilloPlugin: {e}")

    async def _build_actions_block(self):
        """Collect and validate action schemas from all plugins and interfaces."""
        # TEMPORARILY DISABLE FLAG FOR TESTING
        # if self._building_actions_block:
        #     log_debug("[core_initializer] Already building actions block, skipping to prevent loop")
        #     return

        log_debug("[core_initializer] Starting _build_actions_block")

        self._building_actions_block = True
        log_debug("[core_initializer] Starting _build_actions_block")
        available_actions = {}
        log_debug("[core_initializer] Initialized available_actions dict")

        def _register(action_type: str, owner: str, schema: dict, instr_fn):
            from core.action_schema_converter import normalize_action_schema

            # Normalize schema to new format (handles both old and new formats)
            normalized = normalize_action_schema(action_type, schema)

            # Extract required/optional fields from normalized schema
            required = list(normalized.get("schema", {}).get("required", []))
            optional = list(
                set(normalized.get("schema", {}).get("properties", {}).keys())
                - set(required)
            )

            if not isinstance(required, list) or not isinstance(optional, list):
                raise ValueError(f"Invalid schema for {action_type} in {owner}")

            # Track which component declares each action
            self.interface_actions.setdefault(owner, set()).add(action_type)

            # Simplified structure: no more nested interfaces
            if action_type in available_actions:
                log_debug(
                    f"[core_initializer] Updating existing declaration for {action_type}"
                )
                # Merge required_fields and optional_fields
                existing = available_actions[action_type]

                # Get existing schema info (for backward compat)
                existing_required = set(existing.get("schema", {}).get("required", []))
                existing_optional = (
                    set(existing.get("schema", {}).get("properties", {}).keys())
                    - existing_required
                )

                new_required = set(required)
                new_optional = set(optional)

                # Merge fields, giving priority to required over optional
                merged_required = list(existing_required.union(new_required))
                merged_optional = list(
                    (existing_optional.union(new_optional)) - set(merged_required)
                )

                # Keep track of original source, append new sources
                existing_source = existing.get("source", "")
                new_source = f"{existing_source}, {owner}" if existing_source else owner

                # Update schema with merged properties
                merged_properties = {}
                for field in merged_required + merged_optional:
                    merged_properties[field] = {
                        "type": "string",
                        "description": f"Field: {field}",
                    }

                normalized["schema"]["properties"] = merged_properties
                normalized["schema"]["required"] = merged_required
                normalized["source"] = new_source

                available_actions[action_type] = normalized
                log_info(
                    f"[core_initializer] Merged {action_type} fields: required={merged_required}, optional={merged_optional}, source={new_source}"
                )
            else:
                # Add source to normalized schema
                normalized["source"] = owner
                available_actions[action_type] = normalized

            # Get and add instructions (from plugin's get_prompt_instructions method)
            instr = instr_fn(action_type) if instr_fn else None
            if instr is None:
                log_debug(f"Missing prompt instructions for {action_type}")
                instr = {}
            if not isinstance(instr, dict):
                log_warning(
                    f"Prompt instructions for {action_type} must be a dict, got {type(instr)}"
                )
                instr = {}

            # Add instructions to examples section if not already present
            if "examples" not in available_actions[action_type]:
                available_actions[action_type]["examples"] = {}

            if instr:
                available_actions[action_type]["examples"]["instructions"] = instr

        # --- Load action plugins from registry ---
        log_debug(
            f"[core_initializer] Loading actions from {len(PLUGIN_REGISTRY)} plugins: {list(PLUGIN_REGISTRY.keys())}"
        )
        log_debug("[core_initializer] Starting plugin loop")
        for name, plugin in PLUGIN_REGISTRY.items():
            log_debug(f"[core_initializer] Processing plugin: {name}")
            plugin_enabled = True
            if hasattr(plugin, "is_enabled"):
                try:
                    plugin_enabled = bool(plugin.is_enabled())
                    log_debug(
                        f"[core_initializer] Plugin {name} is_enabled={plugin_enabled}"
                    )
                except Exception as e:
                    log_warning(
                        f"[core_initializer] Error checking is_enabled for {name}: {e}"
                    )

            if (
                plugin_enabled
                and hasattr(plugin, "enabled")
                and not getattr(plugin, "enabled")
            ):
                plugin_enabled = False

            if not plugin_enabled:
                log_debug(
                    f"[core_initializer] Plugin {name} is disabled, skipping action registration"
                )
                continue
            if not hasattr(plugin, "get_supported_actions"):
                log_debug(
                    f"[core_initializer] Plugin {name} does not have get_supported_actions method"
                )
                continue
            try:
                supported = plugin.get_supported_actions()
                if not isinstance(supported, dict):
                    raise ValueError(
                        f"Plugin {name} must return dict from get_supported_actions"
                    )
                log_debug(
                    f"[core_initializer] Plugin {name} declares actions: {list(supported.keys())}"
                )
                for act, schema in supported.items():
                    _register(
                        act,
                        name,
                        schema,
                        getattr(plugin, "get_prompt_instructions", None),
                    )
            except Exception as e:
                log_error(f"[core_initializer] Error processing plugin {name}: {e}")

        # --- Load interface actions from registry ---
        log_debug("[core_initializer] Starting interface loop")
        for name, iface in INTERFACE_REGISTRY.items():
            log_debug(f"[core_initializer] Processing interface: {name}")
            if not hasattr(iface, "get_supported_actions"):
                continue
            try:
                supported = iface.get_supported_actions()
                if not isinstance(supported, dict):
                    raise ValueError(
                        f"Interface {name} must return dict from get_supported_actions"
                    )
                instr_fn = getattr(iface, "get_prompt_instructions", None)
                for act, schema in supported.items():
                    _register(act, name, schema, instr_fn)
            except Exception as e:
                log_error(f"[core_initializer] Error processing interface {name}: {e}")

        # --- Collect static context from registry members ---
        log_debug("[core_initializer] Starting static context collection")
        static_context: dict[str, Any] = {}
        log_debug("[core_initializer] Starting static injection from plugins")
        for plugin in PLUGIN_REGISTRY.values():
            log_debug(
                f"[core_initializer] Checking static injection for plugin: {plugin.__class__.__name__}"
            )
            if hasattr(plugin, "get_static_injection"):
                try:
                    data = plugin.get_static_injection()
                except TypeError:
                    # Plugin requires parameters; skip during startup
                    continue
                except Exception as e:
                    log_warning(
                        f"[core_initializer] Errore static injection da plugin {plugin}: {e}"
                    )
                    continue
                if inspect.isawaitable(data):
                    try:
                        # Add timeout to prevent hanging
                        data = await asyncio.wait_for(data, timeout=5.0)
                    except asyncio.TimeoutError:
                        log_warning(
                            f"[core_initializer] Timeout waiting for static injection from {plugin.__class__.__name__}"
                        )
                        continue
                    except Exception as e:
                        log_warning(
                            f"[core_initializer] Error awaiting static injection from {plugin.__class__.__name__}: {e}"
                        )
                        continue
                if isinstance(data, dict) and data:
                    for key, value in data.items():
                        static_context[str(key)] = value
                elif data:
                    log_warning(
                        f"[core_initializer] Static injection from {plugin.__class__.__name__} must be a dict, got {type(data)}"
                    )
        for iface in INTERFACE_REGISTRY.values():
            if hasattr(iface, "get_static_injection"):
                try:
                    data = iface.get_static_injection()
                    if inspect.isawaitable(data):
                        try:
                            # Add timeout to prevent hanging
                            data = await asyncio.wait_for(data, timeout=5.0)
                        except asyncio.TimeoutError:
                            log_warning(
                                f"[core_initializer] Timeout waiting for static injection from {iface.__class__.__name__}"
                            )
                            continue
                        except Exception as e:
                            log_warning(
                                f"[core_initializer] Error awaiting static injection from {iface.__class__.__name__}: {e}"
                            )
                            continue
                    if isinstance(data, dict) and data:
                        for key, value in data.items():
                            static_context[str(key)] = value
                    elif data:
                        log_warning(
                            f"[core_initializer] Static injection from {iface.__class__.__name__} must be a dict, got {type(data)}"
                        )
                except Exception as e:
                    log_warning(
                        f"[core_initializer] Errore static injection da interfaccia {iface}: {e}"
                    )

        self.actions_block = {
            "available_actions": available_actions,
            "static_context": static_context,
        }

        # Agentic Runtime 2.0 bootstrap:
        # 1) mirror available internal actions into the unified tool registry;
        # 2) connect enabled Synth-owned MCP servers and register their tools.
        # Keep this fail-safe so startup never aborts if MCP is unavailable.
        try:
            from core.tool_registry import tool_registry

            tool_registry.load_internal_actions(available_actions)
        except Exception as exc:
            log_warning(
                f"[core_initializer] Failed to load unified internal tools: {exc}"
            )

        if not self._agentic_runtime_bootstrapped:
            try:
                from core.mcp_bridge.client import mcp_client_bridge

                await mcp_client_bridge.connect_all()
                self._agentic_runtime_bootstrapped = True
            except Exception as exc:
                log_warning(
                    f"[core_initializer] MCP client bootstrap failed (non-fatal): {exc}"
                )

        log_debug(
            f"[core_initializer] Actions block built with {len(available_actions)} action types, static_context: {list(static_context.keys())}"
        )
        log_debug(
            f"[core_initializer] Available action types: {sorted(available_actions.keys())}"
        )
        log_debug("[core_initializer] About to reset _building_actions_block flag")

        # Reset the flag
        self._building_actions_block = False
        log_debug(
            "[core_initializer] _building_actions_block flag reset, exiting _build_actions_block()"
        )

    def _display_startup_summary(self):
        """Display a comprehensive startup summary."""
        # Prevent duplicate summaries
        if self._summary_displayed:
            log_debug("[core_initializer] Startup summary already displayed, skipping")
            return

        self._summary_displayed = True

        log_debug("[core_initializer] Starting display_startup_summary")

        # Get system resume
        log_debug("[core_initializer] Getting system resume...")
        resume = self.get_system_resume()
        log_debug("[core_initializer] System resume obtained successfully")

        log_info("=" * 80)
        log_info("🚀 synth FREEDOM PROJECT (SyntH) - SYSTEM ONLINE")
        log_info("=" * 80)

        # --- System Status ---
        if resume["initialization_completed"]:
            log_info("✅ SyntH initialization completed successfully!")
        else:
            log_info("⚠️  SyntH initialization in progress...")

        # --- Component Summary ---
        log_info("📊 COMPONENT STATUS SUMMARY:")
        log_info(f"   • Total components: {resume['total_components']}")
        log_info(f"   • ✅ Successful: {resume['successful']}")
        log_info(f"   • ❌ Failed: {resume['failed']}")
        log_info(f"   • 🔄 Loading: {resume['loading']}")
        log_info(f"   • ⚡ Total actions available: {resume['total_actions']}")

        # --- Cortex Engine ---
        available_engines = list_available_cortex_engines()
        if resume["active_cortex"]:
            cortex_status = (
                "✅"
                if any(
                    c.name == resume["active_cortex"]
                    and c.status == ComponentStatus.SUCCESS
                    for c in resume["successful_components"]
                )
                else "❌"
            )
            log_info(
                f"🧠 Active Cortex Engine: {cortex_status} {resume['active_cortex']}"
            )
        else:
            log_info("🧠 Active Cortex Engine: ❌ None")
        if available_engines:
            log_info(
                f"🧠 Available Cortex Engines: {', '.join(sorted(available_engines))}"
            )

        # --- Successful Components ---
        if resume["successful_components"]:
            log_info("✅ SUCCESSFUL COMPONENTS:")
            # Group by type
            by_type = {}
            for comp in resume["successful_components"]:
                if comp.type not in by_type:
                    by_type[comp.type] = []
                by_type[comp.type].append(comp)

            for comp_type, components in sorted(by_type.items()):
                type_emoji = {
                    "plugin": "🧩",
                    "interface": "🔌",
                    "cortex": "🧠",
                    "core": "⚙️",
                }.get(comp_type, "📦")
                log_info(f"   {type_emoji} {comp_type.upper()}S ({len(components)}):")
                for comp in sorted(components, key=lambda x: x.name):
                    if comp.actions:
                        actions_list = ", ".join(sorted(comp.actions))
                        log_info(f"      ├─ {comp.name}: {actions_list}")
                    else:
                        log_info(f"      ├─ {comp.name}: no actions")

        # --- Failed Components ---
        if resume["failed_components"]:
            log_info("❌ FAILED COMPONENTS:")
            for comp in sorted(resume["failed_components"], key=lambda x: x.name):
                log_info(f"   ├─ {comp.name} ({comp.type}): {comp.error}")
                if comp.details:
                    log_info(f"   │  └─ {comp.details}")

        # --- Loading Components ---
        if resume["loading_components"]:
            log_info("🔄 COMPONENTS STILL LOADING:")
            for comp in sorted(resume["loading_components"], key=lambda x: x.name):
                log_info(f"   ├─ {comp.name} ({comp.type})")
                if comp.details:
                    log_info(f"   │  └─ {comp.details}")

        # --- All available actions by category ---
        log_debug("[core_initializer] Checking available actions...")
        if self.actions_block.get("available_actions"):
            log_info("⚡ AVAILABLE SYSTEM ACTIONS:")
            action_categories = {}

            log_debug(
                f"[core_initializer] Processing {len(self.actions_block['available_actions'])} actions..."
            )
            # Group actions by source (interface/plugin)
            for action_type, action_data in self.actions_block[
                "available_actions"
            ].items():
                log_debug(f"[core_initializer] Processing action: {action_type}")
                source = action_data.get("source", "core")
                if source not in action_categories:
                    action_categories[source] = []
                action_categories[source].append(action_type)

            log_debug(
                f"[core_initializer] Action categories: {list(action_categories.keys())}"
            )
            for source, actions in sorted(action_categories.items()):
                log_info(f"   ├─ {source} ({len(actions)} actions)")
                for action in sorted(actions):
                    log_info(f"   │  ├─ {action}")
        else:
            log_debug("[core_initializer] No available_actions in actions_block")

        # Startup errors
        if self.startup_errors:
            log_warning("⚠️  STARTUP WARNINGS/ERRORS:")
            for error in self.startup_errors:
                log_warning(f"   - {error}")

        log_info("=" * 80)
        log_info("🎯 SYSTEM FULLY INITIALIZED AND READY FOR OPERATIONS")
        log_info("=" * 80)

        log_debug("[core_initializer] Startup summary completed successfully")

    def display_startup_summary(self):
        """Public method to log the startup summary on demand."""
        self._display_startup_summary()

    def register_plugin(self, plugin_name: str):
        """Record that a plugin has been loaded and started."""
        log_debug(
            f"[core_initializer] Instance register_plugin called for: {plugin_name}"
        )

        if plugin_name not in self.loaded_plugins:
            self.loaded_plugins.append(plugin_name)

            # Check if the plugin exposes action schemas and log them
            plugin_obj = PLUGIN_REGISTRY.get(plugin_name)
            actions: list[str] = []

            try:
                if plugin_obj and hasattr(plugin_obj, "get_supported_actions"):
                    supported_actions = plugin_obj.get_supported_actions()
                    if isinstance(supported_actions, dict):
                        actions = [
                            str(action_name) for action_name in supported_actions.keys()
                        ]

                # Track successful plugin loading
                self.track_component(
                    plugin_name,
                    "plugin",
                    ComponentStatus.SUCCESS,
                    actions,
                    details=f"Plugin with {len(actions)} actions"
                    if actions
                    else "Plugin with no actions",
                )

                if actions:
                    log_info(
                        f"🧩 Plugin loaded: {plugin_name} - Registered actions: {', '.join(sorted(actions))}"
                    )
                else:
                    log_info(f"🧩 Plugin loaded: {plugin_name} - No actions registered")

            except Exception as e:
                error_msg = f"Error getting actions: {e}"
                log_debug(
                    f"[core_initializer] Error getting actions for {plugin_name}: {e}"
                )
                self.mark_component_failed(
                    plugin_name, error_msg, "Plugin loaded but action retrieval failed"
                )
                log_info(
                    f"🧩 Plugin loaded: {plugin_name} - Error getting actions: {e}"
                )

        else:
            log_info(
                f"[core_initializer] 🔄 Plugin {plugin_name} is already registered"
            )

        log_debug(f"[core_initializer] Current loaded_plugins: {self.loaded_plugins}")

    def register_action(self, action_type: str, handler: Any) -> None:
        """Expose explicit action registration through the core initializer."""
        register_action(action_type, handler)

    def _register_component_validation_rules(self):
        """Register validation rules from loaded components."""
        try:
            from core.component_auto_registration import auto_register_all_components

            auto_register_all_components()
            log_debug("[core_initializer] Component validation rules registered")
        except Exception as e:
            log_error(
                f"[core_initializer] Failed to register component validation rules: {e}"
            )
            self.startup_errors.append(f"Component validation registration failed: {e}")


# Global instance
core_initializer = CoreInitializer()

# Registry for action handlers (plugins or interfaces)
ACTION_REGISTRY: dict[str, Any] = {}


def register_action(action_type: str, handler: Any) -> None:
    """Register a single action type with its handling object."""
    existing = ACTION_REGISTRY.get(action_type)

    # Special handling for static_inject - allow multiple handlers
    if action_type == "static_inject":
        if existing is not None:
            # If there's already a handler, create a list or extend existing list
            if isinstance(existing, list):
                existing.append(handler)
                log_debug(
                    f"[core_initializer] Added {handler.__class__.__name__} to existing static_inject handlers: {[h.__class__.__name__ for h in existing]}"
                )
            else:
                # Convert single handler to list and add new one
                ACTION_REGISTRY[action_type] = [existing, handler]
                log_debug(
                    f"[core_initializer] Converted static_inject to multi-handler: [{existing.__class__.__name__}, {handler.__class__.__name__}]"
                )
        else:
            # First handler for static_inject
            ACTION_REGISTRY[action_type] = handler
            log_debug(
                f"[core_initializer] Registered first static_inject handler: {handler.__class__.__name__}"
            )
    else:
        # Normal handling for other actions
        if existing is not None:
            log_warning(
                f"[core_initializer] Action '{action_type}' is already registered by {existing.__class__.__name__}. Overwriting with {handler.__class__.__name__}."
            )
        ACTION_REGISTRY[action_type] = handler
        log_debug(
            f"[core_initializer] Registered action: {action_type} -> {handler.__class__.__name__}"
        )

    # Invalidate caches - but don't automatically rebuild to avoid loops
    try:
        from core import action_parser

        action_parser._ACTION_HANDLERS = None  # type: ignore[unresolved-attribute]
        action_parser._INTERFACE_ACTIONS = None
        # Don't auto-rebuild here to prevent infinite loops
        # The rebuild will happen when _build_actions_block() is explicitly called
    except Exception:
        pass


# Global registry for plugin objects
PLUGIN_REGISTRY: dict[str, Any] = {}


def register_plugin(name: str, plugin_obj: Any) -> None:
    """Register a plugin instance and its actions."""
    log_debug(f"[core_initializer] Global register_plugin called for: {name}")

    # CRITICAL: Verify that the plugin has display_name
    if not hasattr(plugin_obj, "display_name"):
        error_msg = f"Plugin `{name}` (class `{plugin_obj.__class__.__name__}`) does not define `display_name`. All plugins MUST have a `display_name` class attribute."
        log_error(f"[core_initializer] ❌ {error_msg}")
        raise ValueError(error_msg)

    # Verify display_name is not empty
    display_name = getattr(plugin_obj, "display_name", "")
    if (
        not display_name
        or not isinstance(display_name, str)
        or not display_name.strip()
    ):
        error_msg = f"Plugin `{name}` (class `{plugin_obj.__class__.__name__}`) has invalid `display_name`: '{display_name}'. It must be a non-empty string."
        log_error(f"[core_initializer] ❌ {error_msg}")
        raise ValueError(error_msg)

    log_debug(
        f"[core_initializer] Plugin `{name}` has valid display_name: '{display_name}'"
    )

    # Avoid re-registering the same plugin by name
    existing = PLUGIN_REGISTRY.get(name)
    if existing is not None:
        log_debug(f"[core_initializer] Plugin {name} already registered; skipping")
        return

    PLUGIN_REGISTRY[name] = plugin_obj
    log_debug(f"[core_initializer] Registered plugin in PLUGIN_REGISTRY: {name}")

    # Automatically register supported actions
    if hasattr(plugin_obj, "get_supported_actions"):
        try:
            supported_actions = plugin_obj.get_supported_actions()
            if isinstance(supported_actions, dict):
                for act in supported_actions.keys():
                    register_action(act, plugin_obj)
            else:
                log_warning(
                    f"[core_initializer] Plugin {name} get_supported_actions() returned non-dict: {type(supported_actions)}"
                )
        except Exception as e:
            log_error(
                f"[core_initializer] Failed to register actions for plugin {name}: {e}"
            )

    # Record plugin for startup summary
    log_debug(f"[core_initializer] Calling core_initializer.register_plugin({name})")
    core_initializer.register_plugin(name)

    # Reset cached plugin list in action parser
    try:
        from core import action_parser

        action_parser._ACTION_PLUGINS = None
    except Exception:
        pass

    # Rebuild actions block to include new plugin's actions (but only if not already building)
    try:
        # Skip auto-refresh during initial initialization - it will be done at the end
        if core_initializer._initial_initialization:
            log_debug(
                f"[core_initializer] Skipping auto-refresh for plugin {name} during initial initialization"
            )
        elif not core_initializer._building_actions_block:
            import asyncio

            if asyncio.get_event_loop().is_running():
                # If event loop is running, schedule the refresh
                asyncio.create_task(core_initializer.refresh_actions_block())
            else:
                # If no event loop, run it synchronously
                asyncio.run(core_initializer.refresh_actions_block())
            log_debug(
                f"[core_initializer] Actions block refreshed after registering plugin {name}"
            )
        else:
            # If already building, schedule a retry after a short delay
            log_debug(
                f"[core_initializer] Actions block building in progress, scheduling retry for plugin {name}"
            )
            import asyncio

            async def retry_refresh():
                await asyncio.sleep(
                    0.1
                )  # Short delay to allow current build to complete
                try:
                    await core_initializer.refresh_actions_block()
                    log_debug(
                        f"[core_initializer] Actions block refresh completed after retry for plugin {name}"
                    )
                except Exception as e:
                    log_warning(
                        f"[core_initializer] Failed to refresh actions block after retry for plugin {name}: {e}"
                    )

            if asyncio.get_event_loop().is_running():
                asyncio.create_task(retry_refresh())
            else:
                # This shouldn't happen in normal operation, but handle it
                asyncio.run(retry_refresh())
    except Exception as e:
        log_warning(
            f"[core_initializer] Failed to refresh actions block after plugin {name} registration: {e}"
        )


# Global registry for interface objects
INTERFACE_REGISTRY: dict[str, Any] = {}


def register_interface(name: str, interface_obj: Any) -> None:
    """Register an interface instance and its actions."""

    # CRITICAL: Verify that the interface has display_name
    if not hasattr(interface_obj, "display_name"):
        error_msg = f"Interface `{name}` (class `{interface_obj.__class__.__name__}`) does not define `display_name`. All interfaces MUST have a `display_name` class attribute."
        log_error(f"[core_initializer] ❌ {error_msg}")
        raise ValueError(error_msg)

    # Verify display_name is not empty
    display_name = getattr(interface_obj, "display_name", "")
    if (
        not display_name
        or not isinstance(display_name, str)
        or not display_name.strip()
    ):
        error_msg = f"Interface `{name}` (class `{interface_obj.__class__.__name__}`) has invalid `display_name`: '{display_name}'. It must be a non-empty string."
        log_error(f"[core_initializer] ❌ {error_msg}")
        raise ValueError(error_msg)

    log_debug(
        f"[core_initializer] Interface `{name}` has valid display_name: '{display_name}'"
    )

    INTERFACE_REGISTRY[name] = interface_obj
    log_debug(f"[core_initializer] Registered interface: {name}")

    # Log detailed information about the interface loading
    log_debug(f"[core_initializer] Loading interface: {name}")

    is_enabled = True
    if interface_obj is not None:
        is_enabled = getattr(interface_obj, "is_enabled", True)

    # Automatically register supported actions (only when enabled)
    if is_enabled and hasattr(interface_obj, "get_supported_actions"):
        log_debug(f"[core_initializer] Interface '{name}' supports action registration")
        try:
            for act in interface_obj.get_supported_actions().keys():
                register_action(act, interface_obj)
        except Exception as e:
            log_error(
                f"[core_initializer] Failed to register actions for interface {name}: {e}"
            )
    elif not is_enabled:
        log_debug(
            f"[core_initializer] Skipping action registration for disabled interface '{name}'"
        )

    # Record interface for startup summary
    core_initializer.register_interface(name)

    # Flush any queued trainer notifications for this interface
    try:
        from core.notifier import flush_pending_for_interface

        flush_pending_for_interface(name)
    except Exception:
        pass


# NOTE: core actions are registered automatically when imported
# by other modules that need them, avoiding circular import issues

# Global variables for debounced rebuild
_ACTION_REBUILD_DEBOUNCE_SEC = 0.8
_action_rebuild_timer = None


def _schedule_rebuild_actions(core_init_instance):
    """Schedule a debounced rebuild of the actions block."""
    global _action_rebuild_timer
    if _action_rebuild_timer:
        _action_rebuild_timer.cancel()

    def rebuild_with_main_loop():
        """Run rebuild on main event loop to avoid creating new event loops."""
        try:
            # Try to get the main event loop
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # Schedule as a task on the running loop
                asyncio.create_task(core_init_instance._build_actions_block())
            else:
                # If loop exists but not running, run until complete
                loop.run_until_complete(core_init_instance._build_actions_block())
        except RuntimeError:
            # No event loop at all - this is a fallback but shouldn't happen
            try:
                asyncio.run(core_init_instance._build_actions_block())
            except Exception as e:
                from core.logging_utils import log_debug

                log_debug(f"[core_initializer] Error rebuilding actions: {e}")

    _action_rebuild_timer = threading.Timer(
        _ACTION_REBUILD_DEBOUNCE_SEC, rebuild_with_main_loop
    )
    _action_rebuild_timer.start()
