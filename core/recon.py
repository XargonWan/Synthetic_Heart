import asyncio
from typing import Any, Dict, List
from core.logging_utils import log_debug, log_info, log_warning
from core.config_manager import config_registry

# Expose config flag
try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "ENABLE_RECON",
        label="Enable Recon (preflight)",
        default=True,
        value_type=bool,
        ui_type="bool",
        description="Enable Recon preflight contributions from plugins",
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
except Exception:
    pass


async def gather_recon_contributions(
    message=None,
    context_memory=None,
    text: str | None = None,
    tags: List[str] | None = None,
    keywords: List[str] | None = None,
    max_results: int = 5,
) -> List[Dict[str, Any]]:
    """Call plugin hooks `get_recon_contributions` and merge results.

    Returns list of normalized contribution dicts.
    """
    enabled = bool(config_registry.get_var("ENABLE_RECON", True))
    if not enabled:
        log_debug("[recon] ENABLE_RECON disabled, skipping contributions")
        return []

    contributions = []
    try:
        from core.core_initializer import PLUGIN_REGISTRY

        plugins = list(PLUGIN_REGISTRY.values())
    except Exception as e:
        log_warning(f"[recon] Failed to access PLUGIN_REGISTRY: {e}")
        plugins = []

    for plugin in plugins:
        try:
            if hasattr(plugin, "get_recon_contributions"):
                fn = plugin.get_recon_contributions
                try:
                    result = fn(
                        message=message,
                        context_memory=context_memory,
                        text=text,
                        tags=tags,
                        keywords=keywords,
                        max_results=max_results,
                    )
                except TypeError:
                    # Older plugin signature without kwargs
                    result = fn()
                if asyncio.iscoroutine(result):
                    result = await result
                if result:
                    if isinstance(result, dict):
                        contributions.append(result)
                    elif isinstance(result, list):
                        contributions.extend(result)
        except Exception as e:
            log_warning(
                f"[recon] Plugin {plugin.__class__.__name__} recon hook failed: {e}"
            )

    # Deduplicate naive by string repr
    seen = set()
    dedup = []
    for c in contributions:
        key = str(c)
        if key not in seen:
            seen.add(key)
            dedup.append(c)
    log_info(f"[recon] Collected {len(dedup)} contributions from plugins")
    return dedup
