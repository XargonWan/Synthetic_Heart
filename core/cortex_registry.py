# core/cortex_registry.py

"""Registry to manage Cortex engines via base-module discovery."""

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
    """Central registry for all Cortex engines via base modules.

    Each cortex kind is discovered by locating a `*_base.py` module under
    `cortex/<kind>/` and invoking its `discover_and_register()` callback.
    """

    def __init__(self):
        self._engines: Dict[str, Any] = {}
        self._engine_modules: Dict[str, str] = {}
        self._engine_meta: Dict[
            str, Dict[str, Any]
        ] = {}  # name -> {cortex, capabilities}
        self._cortex_kinds: Dict[str, Dict[str, Any]] = {}

    def register_cortex_kind(
        self,
        kind: str,
        label: str,
        capabilities: Optional[Capabilities] = None,
    ) -> None:
        """Register metadata for a cortex kind (from its base module)."""
        self._cortex_kinds[kind] = {
            "label": label or kind,
            "capabilities": capabilities or {},
        }
        log_debug(f"[cortex_registry] Registered cortex kind: {kind} ({label or kind})")

    def register_engine_module(
        self,
        name: str,
        module_path: str,
        cortex: str = "llm_provider",
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
        cortex: str = "llm_provider",
        capabilities: Optional[Capabilities] = None,
    ):
        """Compatibility entrypoint (same as register_engine_module)."""
        return self.register_engine_module(name, module_path, cortex, capabilities)

    def get_default_engine(self, cortex: str = "llm_provider") -> str:
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
        """Load an engine by name using the registered module path."""
        if not name or not isinstance(name, str):
            error_msg = f"Invalid engine name: {repr(name)}. Engine name must be a non-empty string."
            log_error(f"[cortex_registry] ❌ {error_msg}")
            raise ValueError(error_msg)

        module_path = self._engine_modules.get(name)
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
        # Defensive check for invalid PLUGIN_CLASS
        if plugin_class is None or not hasattr(plugin_class, "__name__"):
            error_msg = (
                f"Plugin `{name}` exports `PLUGIN_CLASS` but it is None or invalid."
            )
            log_error(f"[cortex_registry] ❌ {error_msg}")
            raise ValueError(error_msg)

        # Verify display_name
        if not hasattr(plugin_class, "display_name"):
            error_msg = f"Plugin `{name}` (class `{plugin_class.__name__}`) does not define `display_name`."
            log_error(f"[cortex_registry] ❌ {error_msg}")
            raise ValueError(error_msg)

        display_name = getattr(plugin_class, "display_name", "")
        if (
            not display_name
            or not isinstance(display_name, str)
            or not display_name.strip()
        ):
            error_msg = f"Plugin `{name}` (class `{plugin_class.__name__}`) has invalid `display_name`: '{display_name}'."
            log_error(f"[cortex_registry] ❌ {error_msg}")
            raise ValueError(error_msg)

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


def register_default_engines(*, dev_enabled: bool = False) -> None:
    """Auto-discover and register cortex base modules.

    Each `cortex/<kind>/*_base.py` module is imported and asked to
    `discover_and_register()` its own children. Dev-only plugins under
    `cortex/<kind>/dev` are registered only when `dev_enabled=True`.
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
        for entry in os.scandir(base_path):
            if not entry.is_dir():
                continue
            if entry.name.startswith("_"):
                continue

            kind_path = entry.path
            base_modules = []
            for _, module_name, is_pkg in pkgutil.iter_modules([kind_path]):
                if is_pkg:
                    continue
                if module_name.endswith("_base"):
                    base_modules.append(module_name)

            if not base_modules:
                continue
            if len(base_modules) > 1:
                log_warning(
                    f"[cortex_registry] Multiple base modules found in {entry.name}: {base_modules}; using first."
                )

            base_module_name = base_modules[0]
            module_path = f"cortex.{entry.name}.{base_module_name}"
            try:
                mod = importlib.import_module(module_path)
            except Exception as e:
                log_error(
                    f"[cortex_registry] ❌ Failed to import cortex base module {module_path}: {e}",
                    e,
                )
                continue

            discover = getattr(mod, "discover_and_register", None)
            if not callable(discover):
                log_error(
                    f"[cortex_registry] ❌ Base module {module_path} does not define discover_and_register()"
                )
                continue

            try:
                discover(registry, dev_enabled=dev_enabled)
                log_debug(
                    f"[cortex_registry] ✅ Cortex base discovery executed: {module_path}"
                )
            except Exception as e:
                log_error(
                    f"[cortex_registry] ❌ Base discovery failed for {module_path}: {e}",
                    e,
                )

        available_engines = registry.get_available_engines()
        log_info(
            f"[cortex_registry] Auto-discovery complete: engines registered: {', '.join(available_engines)}"
        )
    except Exception as e:
        log_warning(f"[cortex_registry] Engine auto-discovery failed: {e}")
