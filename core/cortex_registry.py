# core/cortex_registry.py

"""Registry to manage Cortex engines (llm, live, agent) without hardcoded dependencies."""

import importlib
from typing import Dict, Any, Optional, List, TypedDict
from core.logging_utils import log_debug, log_info, log_warning, log_error


class Capabilities(TypedDict, total=False):
    vision: bool
    audio: bool
    actions: bool
    bidi: bool
    low_latency: bool


class CortexRegistry:
    """Central registry for all Cortex engines (llm/live/agent).

    This mirrors the previous LLMRegistry semantics but adds support
    for cortex kinds and capability flags.
    """

    def __init__(self):
        self._engines: Dict[str, Any] = {}
        self._engine_modules: Dict[str, str] = {}
        self._engine_meta: Dict[
            str, Dict[str, Any]
        ] = {}  # name -> {cortex, capabilities}

    def register_engine_module(
        self,
        name: str,
        module_path: str,
        cortex: str = "llm",
        capabilities: Optional[Capabilities] = None,
        label: str | None = None,
    ):
        """Register an engine module path with optional cortex kind, capabilities and a human-readable label.

        The optional `label` should be a short sentence explaining what the engine is
        and when it should be used. This label is displayed in the WebUI so operators
        can decide which engine to select for a given cortex kind.
        """
        self._engine_modules[name] = module_path
        self._engine_meta[name] = {
            "cortex": cortex,
            "capabilities": capabilities or {},
            "label": label or "",
        }
        log_debug(
            f"[cortex_registry] Registered engine module: {name} (cortex={cortex}) -> {module_path}"
        )

    def register_engine(
        self,
        name: str,
        module_path: str,
        cortex: str = "llm",
        capabilities: Optional[Capabilities] = None,
    ):
        """Compatibility entrypoint (same as register_engine_module)."""
        return self.register_engine_module(name, module_path, cortex, capabilities)

    def get_default_engine(self, cortex: str = "llm") -> str:
        """Get the default engine name for a cortex."""
        available = [
            n for n, meta in self._engine_meta.items() if meta.get("cortex") == cortex
        ]
        if "manual" in available:
            return "manual"
        elif available:
            return available[0]
        else:
            raise ValueError(f"No engines registered for cortex: {cortex}")

    def get_available_engines(self, cortex: Optional[str] = None) -> List[str]:
        """Get list of available engine names, optionally filtered by cortex kind."""
        if cortex is None:
            return list(self._engine_modules.keys())
        return [
            n for n, meta in self._engine_meta.items() if meta.get("cortex") == cortex
        ]

    def find_engine_by_capabilities(
        self, cortex: str, required: Capabilities
    ) -> Optional[str]:
        """Find a registered engine in `cortex` that satisfies all required capabilities."""
        for name, meta in self._engine_meta.items():
            if meta.get("cortex") != cortex:
                continue
            caps = meta.get("capabilities") or {}
            ok = True
            for k, v in required.items():
                if v and not caps.get(k):
                    ok = False
                    break
            if ok:
                return name
        return None

    def load_engine(self, name: str, notify_fn=None) -> Any:
        """Load an engine by name. Attempts dynamic loading using registered module or
        by module path convention when not registered."""
        if not name or not isinstance(name, str):
            error_msg = f"Invalid engine name: {repr(name)}. Engine name must be a non-empty string."
            log_error(f"[cortex_registry] ❌ {error_msg}")
            raise ValueError(error_msg)

        module_path = None
        if name in self._engine_modules:
            module_path = self._engine_modules[name]
        else:
            # Fallback dynamic load: try both llm and live conventions
            candidates = [f"cortex.llm_engine.{name}", f"cortex.live.{name}"]
            log_debug(
                f"[cortex_registry] Engine '{name}' not registered, attempting dynamic load from candidates: {candidates}"
            )
            for p in candidates:
                try:
                    importlib.import_module(p)
                    module_path = p
                    break
                except ModuleNotFoundError:
                    continue

        if not module_path:
            raise ValueError(f"Unknown engine: {name}")

        try:
            module = importlib.import_module(module_path)
            log_debug(f"[cortex_registry] Module {module_path} imported successfully.")
        except ModuleNotFoundError as e:
            log_error(f"[cortex_registry] ❌ Unable to import {module_path}: {e}", e)
            raise ValueError(f"Invalid plugin: {name}")

        if not hasattr(module, "PLUGIN_CLASS"):
            raise ValueError(f"Plugin `{name}` does not define `PLUGIN_CLASS`.")

        plugin_class = getattr(module, "PLUGIN_CLASS")
        # Defensive check: some migration shims set PLUGIN_CLASS = None. Treat
        # that as an invalid plugin and raise a clear error instead of
        # letting AttributeError bubble up later when attempting to access
        # attributes on None.
        if plugin_class is None or not hasattr(plugin_class, "__name__"):
            error_msg = (
                f"Plugin `{name}` exports `PLUGIN_CLASS` but it is None or invalid."
            )
            log_error(f"[cortex_registry] ❌ {error_msg}")
            raise ValueError(error_msg)

        # Verify display_name
        # Historically this was enforced strictly, but to improve robustness we
        # accept plugins missing `display_name` by deriving a friendly display
        # name from the module/plugin class and logging a warning. This makes
        # onboarding of legacy engines (migration shims) less brittle while
        # encouraging authors to set an explicit `display_name`.
        if not hasattr(plugin_class, "display_name"):
            fallback = name.replace("_", " ").title() or plugin_class.__name__
            warning_msg = (
                f"Plugin `{name}` (class `{plugin_class.__name__}`) does not define `display_name`. "
                f"Using fallback display name: '{fallback}'."
            )
            log_warning(f"[cortex_registry] ⚠️ {warning_msg}")
            display_name = fallback
        else:
            display_name = getattr(plugin_class, "display_name", "")
            if (
                not display_name
                or not isinstance(display_name, str)
                or not display_name.strip()
            ):
                fallback = name.replace("_", " ").title() or plugin_class.__name__
                warning_msg = (
                    f"Plugin `{name}` (class `{plugin_class.__name__}`) has invalid `display_name`: '{display_name}'. "
                    f"Using fallback display name: '{fallback}'."
                )
                log_warning(f"[cortex_registry] ⚠️ {warning_msg}")
                display_name = fallback

        try:
            plugin_args = plugin_class.__init__.__code__.co_varnames
            if "notify_fn" in plugin_args:
                plugin_instance = plugin_class(notify_fn=notify_fn)
            else:
                plugin_instance = plugin_class()
        except Exception as e:
            log_error(
                f"[cortex_registry] ❌ Error during plugin initialization: {e}", e
            )
            raise

        self._engines[name] = plugin_instance
        log_debug(
            f"[cortex_registry] Engine initialized: {plugin_instance.__class__.__name__}"
        )
        return plugin_instance

    def get_engine(self, name: str) -> Optional[Any]:
        return self._engines.get(name)

    def unload_engine(self, name: str):
        if name in self._engines:
            del self._engines[name]
            log_debug(f"[cortex_registry] Unloaded engine: {name}")


# Global registry instance
_cortex_registry = CortexRegistry()


def get_cortex_registry() -> CortexRegistry:
    """Get the global instance of the Cortex registry."""
    return _cortex_registry


def register_default_engines():
    """Auto-discover and register engines from conventional folders.

    Scans both `cortex/llm_engine` and `cortex/live` for modules that define
    `PLUGIN_CLASS` and auto-registers them under proper cortex kinds.
    """
    import os
    import pkgutil

    try:
        import cortex
    except ImportError as e:
        log_warning(f"[cortex_registry] Could not import cortex package: {e}")
        return

    registry = get_cortex_registry()

    try:
        base_path = os.path.dirname(cortex.__file__)
        # Check llm_engine
        llm_path = os.path.join(base_path, "llm_engine")
        if os.path.isdir(llm_path):
            for importer, module_name, is_pkg in pkgutil.iter_modules([llm_path]):
                if not is_pkg and not module_name.startswith("_"):
                    module_path = f"cortex.llm_engine.{module_name}"
                    try:
                        mod = importlib.import_module(module_path)
                        if hasattr(mod, "PLUGIN_CLASS"):
                            # Try to extract a short label from the module or PLUGIN_CLASS
                            label = None
                            try:
                                label = getattr(mod, "ENGINE_LABEL", None)
                            except Exception:
                                label = None
                            try:
                                if not label and hasattr(mod, "PLUGIN_CLASS"):
                                    label = getattr(
                                        mod.PLUGIN_CLASS, "engine_label", None
                                    )
                            except Exception:
                                pass
                            registry.register_engine_module(
                                module_name,
                                module_path,
                                cortex="llm",
                                label=(label or None),
                            )
                            log_debug(
                                f"[cortex_registry] Auto-registered llm engine: {module_name}"
                            )
                    except Exception as e:
                        log_warning(
                            f"[cortex_registry] Failed to auto-register llm engine {module_name}: {e}"
                        )
        # Check live engines
        live_path = os.path.join(base_path, "live")
        if os.path.isdir(live_path):
            for importer, module_name, is_pkg in pkgutil.iter_modules([live_path]):
                if not is_pkg and not module_name.startswith("_"):
                    module_path = f"cortex.live.{module_name}"
                    try:
                        mod = importlib.import_module(module_path)
                        if hasattr(mod, "PLUGIN_CLASS"):
                            # If module exports CAPABILITIES dict, pick it up
                            caps = getattr(mod, "CAPABILITIES", None)
                            # Auto-discover label as well for live engines
                            label = None
                            try:
                                label = getattr(mod, "ENGINE_LABEL", None)
                            except Exception:
                                label = None
                            registry.register_engine_module(
                                module_name,
                                module_path,
                                cortex="live",
                                capabilities=caps,
                                label=(label or None),
                            )
                            log_debug(
                                f"[cortex_registry] Auto-registered live engine: {module_name}"
                            )
                    except Exception as e:
                        log_warning(
                            f"[cortex_registry] Failed to auto-register live engine {module_name}: {e}"
                        )

        available_engines = registry.get_available_engines()
        log_info(
            f"[cortex_registry] Auto-discovery complete: engines registered: {', '.join(available_engines)}"
        )
    except Exception as e:
        log_warning(f"[cortex_registry] Engine auto-discovery failed: {e}")
        log_info(
            "[cortex_registry] Continuing without pre-registration - engines will load dynamically on demand"
        )
