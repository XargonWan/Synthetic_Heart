# core/llm_registry.py

"""
Registry to manage LLM engines without hardcoded dependencies.
"""

import importlib
from typing import Dict, Any, Optional, List
from core.logging_utils import log_debug, log_info, log_warning, log_error


class LLMRegistry:
    """Central registry for all LLM engines."""

    def __init__(self):
        self._engines: Dict[str, Any] = {}
        self._engine_modules: Dict[str, str] = {}

    def register_engine_module(self, name: str, module_path: str):
        """Register an LLM engine module path."""
        self._engine_modules[name] = module_path
        log_debug(f"[llm_registry] Registered engine module: {name} -> {module_path}")

    def get_default_engine(self) -> str:
        """Get the default LLM engine name."""
        available = self.get_available_engines()
        if "manual" in available:
            return "manual"
        elif available:
            return available[0]
        else:
            raise ValueError("No LLM engines are registered")

    def get_available_engines(self) -> List[str]:
        """Get list of available engine names."""
        return list(self._engine_modules.keys())

    def load_engine(self, name: str, notify_fn=None) -> Any:
        """Load an LLM engine by name.

        If the engine is not yet registered, attempts to load it directly
        from the llm_engines module to support dynamic loading.
        """
        # Validate that name is not None or empty
        if not name or not isinstance(name, str):
            error_msg = f"Invalid engine name: {repr(name)}. Engine name must be a non-empty string."
            log_error(f"[llm_registry] ❌ {error_msg}")
            raise ValueError(error_msg)

        module_path = None

        # Check if registered
        if name in self._engine_modules:
            module_path = self._engine_modules[name]
        else:
            # Try dynamic loading: assume module path follows convention
            # (this allows plugins to work without being pre-registered)
            module_path = f"llm_engines.{name}"
            log_debug(
                f"[llm_registry] Engine '{name}' not registered, attempting dynamic load from {module_path}"
            )

        if not module_path:
            raise ValueError(f"Unknown LLM engine: {name}")

        try:
            module = importlib.import_module(module_path)
            log_debug(f"[llm_registry] Module {module_path} imported successfully.")
        except ModuleNotFoundError as e:
            log_error(f"[llm_registry] ❌ Unable to import {module_path}: {e}", e)
            raise ValueError(f"Invalid LLM plugin: {name}")

        if not hasattr(module, "PLUGIN_CLASS"):
            raise ValueError(f"Plugin `{name}` does not define `PLUGIN_CLASS`.")

        plugin_class = getattr(module, "PLUGIN_CLASS")

        # CRITICAL: Verify that the plugin class has display_name
        if not hasattr(plugin_class, "display_name"):
            error_msg = f"Plugin `{name}` (class `{plugin_class.__name__}`) does not define `display_name`. All plugins MUST have a `display_name` class attribute."
            log_error(f"[llm_registry] ❌ {error_msg}")
            raise ValueError(error_msg)

        # Verify display_name is not empty
        display_name = getattr(plugin_class, "display_name", "")
        if (
            not display_name
            or not isinstance(display_name, str)
            or not display_name.strip()
        ):
            error_msg = f"Plugin `{name}` (class `{plugin_class.__name__}`) has invalid `display_name`: '{display_name}'. It must be a non-empty string."
            log_error(f"[llm_registry] ❌ {error_msg}")
            raise ValueError(error_msg)

        log_debug(
            f"[llm_registry] Plugin `{name}` has valid display_name: '{display_name}'"
        )

        try:
            plugin_args = plugin_class.__init__.__code__.co_varnames
            if "notify_fn" in plugin_args:
                plugin_instance = plugin_class(notify_fn=notify_fn)
            else:
                plugin_instance = plugin_class()
        except Exception as e:
            log_error(f"[llm_registry] ❌ Error during plugin initialization: {e}", e)
            raise

        self._engines[name] = plugin_instance
        log_debug(
            f"[llm_registry] Engine initialized: {plugin_instance.__class__.__name__}"
        )

        return plugin_instance

    def get_engine(self, name: str) -> Optional[Any]:
        """Get a loaded engine instance."""
        return self._engines.get(name)

    def unload_engine(self, name: str):
        """Unload an engine instance."""
        if name in self._engines:
            del self._engines[name]
            log_debug(f"[llm_registry] Unloaded engine: {name}")


# Global registry instance
_llm_registry = LLMRegistry()


def get_llm_registry() -> LLMRegistry:
    """Get the global instance of the LLM registry."""
    return _llm_registry


def register_default_engines():
    """Auto-discover and register all LLM engines from llm_engines directory.

    NOTE: This is OPTIONAL. Plugins can also be loaded dynamically without
    pre-registration. This function is called during initialization for convenience,
    but the system will also attempt dynamic loading if a plugin isn't registered.
    """
    import os
    import pkgutil

    try:
        import llm_engines
    except ImportError as e:
        log_warning(f"[llm_registry] Could not import llm_engines package: {e}")
        return

    registry = get_llm_registry()

    # Auto-discover all modules in llm_engines package
    try:
        llm_engines_path = os.path.dirname(llm_engines.__file__)
        log_debug(f"[llm_registry] Discovering LLM engines in: {llm_engines_path}")

        discovered_count = 0
        for importer, module_name, is_pkg in pkgutil.iter_modules([llm_engines_path]):
            if not is_pkg and not module_name.startswith("_"):
                module_path = f"llm_engines.{module_name}"
                try:
                    # Try to import the module to check if it has PLUGIN_CLASS
                    import importlib

                    module = importlib.import_module(module_path)

                    # Check if module has PLUGIN_CLASS defined
                    if hasattr(module, "PLUGIN_CLASS"):
                        registry.register_engine_module(module_name, module_path)
                        log_debug(
                            f"[llm_registry] Auto-registered engine: {module_name}"
                        )
                        discovered_count += 1
                    else:
                        log_debug(
                            f"[llm_registry] Skipping {module_name} (no PLUGIN_CLASS)"
                        )
                except Exception as e:
                    log_warning(
                        f"[llm_registry] Failed to auto-register {module_name}: {e}"
                    )

        available_engines = registry.get_available_engines()
        log_info(
            f"[llm_registry] Auto-discovery complete: {discovered_count} engines registered: {', '.join(available_engines)}"
        )
    except Exception as e:
        log_warning(f"[llm_registry] LLM auto-discovery failed: {e}")
        log_info(
            "[llm_registry] Continuing without pre-registration - plugins will load dynamically on demand"
        )
