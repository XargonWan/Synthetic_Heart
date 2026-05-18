"""Central bridge that aggregates Rift Vessel adapters and integrates them into SyntH.

The RiftVesselBridge is registered in INTERFACE_REGISTRY at startup.
It acts as:
  - A registry of all loaded RiftVesselBase adapters
  - A router for game_* actions from the action parser
  - A world-state provider for the prompt engine
"""

from __future__ import annotations

from core.logging_utils import log_debug, log_info
from rift_vessel.rift_vessel_base import RiftVesselBase
from rift_vessel.schema import WorldState, WorldEvent


# Global registry of loaded vessel adapters
_vessels: dict[str, RiftVesselBase] = {}


def register_vessel(name: str, vessel: RiftVesselBase) -> None:
    """Register a loaded Rift Vessel adapter."""
    _vessels[name] = vessel
    log_info(f"[rift_bridge] Registered vessel: {name}")


def unregister_vessel(name: str) -> None:
    """Remove a vessel from the registry."""
    _vessels.pop(name, None)
    log_debug(f"[rift_bridge] Unregistered vessel: {name}")


def get_vessel(name: str) -> RiftVesselBase | None:
    """Get a registered vessel by name."""
    return _vessels.get(name)


def get_vessel_for_interface(interface_path: str) -> RiftVesselBase | None:
    """Find a vessel whose interface_id matches the interface_path prefix."""
    if not interface_path or "/" not in interface_path:
        return None
    prefix = interface_path.split("/", 1)[0]
    for name, vessel in _vessels.items():
        if vessel.get_interface_id() == prefix:
            return vessel
    return None


def get_all_vessels() -> dict[str, RiftVesselBase]:
    """Return all registered vessels."""
    return dict(_vessels)


def get_all_game_action_types() -> list[str]:
    """Return union of all game_* action types from all vessels."""
    types: list[str] = []
    for vessel in _vessels.values():
        types.extend(vessel.get_supported_game_action_types())
    return types


async def execute_game_action(action_type: str, payload: dict) -> dict:
    """Route a game_* action to the correct vessel.

    The payload MUST contain 'interface_path' to identify which vessel
    should execute the action.

    Returns:
        dict with at minimum {"status": "ok"|"error", ...}
    """
    interface_path = payload.get("interface_path", "")
    vessel = get_vessel_for_interface(interface_path)
    if vessel is None:
        return {"status": "error", "error": f"No vessel for {interface_path}"}

    action_name = action_type  # e.g. "game_attack"
    return await vessel.execute_game_action(action_name, payload)


async def get_world_state(interface_path: str) -> WorldState | None:
    """Get the latest cached world state for a vessel."""
    vessel = get_vessel_for_interface(interface_path)
    if vessel is None:
        return None
    return vessel.get_latest_world_state()


async def push_world_event(event: WorldEvent) -> None:
    """Push a world event to the target vessel's on_world_event handler."""
    vessel = get_vessel(event.source)
    if vessel is not None:
        await vessel.on_world_event(event)


def get_supported_actions() -> dict:
    """Return the union of all supported game actions across all vessels.

    This is called by the action parser discovery system.
    """
    actions: dict = {}
    for name, vessel in _vessels.items():
        vessel_actions = vessel.get_supported_actions()
        for act_type, act_def in vessel_actions.items():
            if act_type not in actions:
                actions[act_type] = act_def
    return actions


# RiftVesselBridge itself registers as an interface in INTERFACE_REGISTRY
# so the core treats game_* actions as routable action types.
VESSEL_BRIDGE_INTERFACE_ID = "rift_vessel_bridge"


def register_in_interface_registry() -> None:
    """Register the Rift Vessel bridge in SyntH's INTERFACE_REGISTRY.

    This makes game_* action types discoverable by the action parser.
    Call this once at startup (from rift_vessel/__init__.py).
    """
    from types import SimpleNamespace
    from core.core_initializer import register_interface
    from core.validation_registry import get_validation_registry

    async def execute_action(action, context, bot, original_message):
        action_type = action["type"]
        payload = action.get("payload", {})
        result = await execute_game_action(action_type, payload)
        return result

    bridge_obj = SimpleNamespace(
        interface_id=VESSEL_BRIDGE_INTERFACE_ID,
        display_name="Rift Vessel Bridge",
        get_supported_actions=get_supported_actions,
        get_supported_action_types=lambda: list(get_supported_actions().keys()),
        execute_action=execute_action,
        is_enabled=True,
        get_interface_id=lambda: VESSEL_BRIDGE_INTERFACE_ID,
    )

    register_interface(VESSEL_BRIDGE_INTERFACE_ID, bridge_obj)
    log_info(
        f"[rift_bridge] Registered in INTERFACE_REGISTRY as '{VESSEL_BRIDGE_INTERFACE_ID}'"
    )

    # Register default validation rules for game_* actions
    registry = get_validation_registry()
    registry.register_response_metadata_keys(
        VESSEL_BRIDGE_INTERFACE_ID,
        ["game_state", "world_events", "rift_context"],
    )
    log_debug(
        "[rift_bridge] Registered rift response metadata keys in validation registry"
    )
