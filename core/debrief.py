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
    register_exposed_var(
        "AUTO_RECOVERY_POLICY",
        label="Auto Recovery Policy",
        default="require_review",
        value_type=str,
        ui_type="string",
        description="Policy for debrief recovery actions: require_review | auto_under_policy | auto_always",
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
    register_exposed_var(
        "AUTO_RECOVERY_TIME_WINDOW",
        label="Auto Recovery Time Window (s)",
        default=86400,
        value_type=int,
        ui_type="number",
        description="How far back Debrief should consider unresolved promises for recovery actions",
        scope="agent",
        component="agent",
        needs_component_reload=False,
    )
except Exception:
    pass


async def run_debrief(
    processed_actions: List[Dict],
    failed_actions: List[Dict],
    results: Dict,
    context: Dict | None = None,
    original_message: Any = None,
) -> None:
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

    recovery_actions: List[Dict[str, Any]] = []

    for plugin in plugins:
        try:
            if hasattr(plugin, "on_debrief"):
                try:
                    rval = plugin.on_debrief(processed_actions=processed_actions, failed_actions=failed_actions, results=results, context=context or {}, original_message=original_message)
                except TypeError:
                    rval = plugin.on_debrief(processed_actions, failed_actions, results, context or {}, original_message)
                if asyncio.iscoroutine(rval):
                    await rval
                if isinstance(rval, dict) and rval.get("recovery_actions"):
                    actions = rval.get("recovery_actions")
                    if isinstance(actions, list):
                        recovery_actions.extend(actions)
        except Exception as e:
            log_warning(f"[debrief] Plugin {plugin.__class__.__name__} on_debrief failed: {e}")

    if recovery_actions:
        policy = str(
            config_registry.get_value(
                "AUTO_RECOVERY_POLICY", "require_review", value_type=str
            )
            or "require_review"
        ).lower()

        if policy in ("auto_always", "auto_under_policy"):
            try:
                from core.action_parser import run_actions
                from core.action_safety import is_action_allowed_for_execution

                to_execute = []
                for item in recovery_actions:
                    if not isinstance(item, dict):
                        continue
                    action_type = item.get("action_type") or item.get("type")
                    payload = item.get("payload") or {}
                    if not action_type:
                        continue
                    action = {"type": action_type, "payload": payload}

                    if policy == "auto_under_policy":
                        allowed, reason, _ = is_action_allowed_for_execution(
                            action, context or {}, original_message
                        )
                        if not allowed:
                            log_info(
                                f"[debrief] Recovery action blocked by policy: {action_type} ({reason})"
                            )
                            continue

                    to_execute.append(action)

                if to_execute:
                    log_info(
                        f"[debrief] Executing {len(to_execute)} recovery action(s) (policy={policy})"
                    )
                    await run_actions(to_execute, {"from_debrief": True}, None, original_message)
            except Exception as e:
                log_warning(f"[debrief] Recovery execution failed: {e}")
        else:
            # Require review: stash in context and notify for visibility
            try:
                if isinstance(context, dict):
                    context["recovery_actions"] = recovery_actions
            except Exception:
                pass
            try:
                from core.notifier import notify_intelligent

                notify_intelligent(
                    f"Debrief detected {len(recovery_actions)} recovery action(s) requiring review."
                )
            except Exception:
                pass

    log_info("[debrief] Debrief hooks completed")