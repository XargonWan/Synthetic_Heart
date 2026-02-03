import asyncio
from typing import Any, Dict, List
from core.logging_utils import log_debug, log_info, log_warning
from core.config_manager import config_registry

# Expose config flag
try:
    from core.variables_engine import register_exposed_var
    register_exposed_var(
        "ENABLE_DEBRIEF",
        label="Enable Debrief (postflight)",
        default=True,
        value_type=bool,
        ui_type="bool",
        description="Enable Debrief postflight hooks from plugins",
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
except Exception:
    pass


async def run_debrief(processed_actions: List[Dict], failed_actions: List[Dict], results: Dict, context: Dict | None = None, original_message: Any = None) -> None:
    enabled = bool(config_registry.get_var("ENABLE_DEBRIEF", True))
    if not enabled:
        log_debug("[debrief] ENABLE_DEBRIEF disabled, skipping debrief hooks")
        return

    try:
        from core.core_initializer import PLUGIN_REGISTRY
        plugins = list(PLUGIN_REGISTRY.values())
    except Exception as e:
        log_warning(f"[debrief] Failed to access PLUGIN_REGISTRY: {e}")
        plugins = []

    for plugin in plugins:
        try:
            if hasattr(plugin, "on_debrief"):
                try:
                    rval = plugin.on_debrief(processed_actions=processed_actions, failed_actions=failed_actions, results=results, context=context or {}, original_message=original_message)
                except TypeError:
                    rval = plugin.on_debrief(processed_actions, failed_actions, results, context or {}, original_message)
                if asyncio.iscoroutine(rval):
                    await rval
        except Exception as e:
            log_warning(f"[debrief] Plugin {plugin.__class__.__name__} on_debrief failed: {e}")

    log_info("[debrief] Debrief hooks completed")