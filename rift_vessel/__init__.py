"""Rift Vessel — game-world embodiment adapters for SyntH.

Importing this package registers the RiftVesselBridge in SyntH's
INTERFACE_REGISTRY, making game_* action types discoverable by
the action parser and validation system.
"""

from rift_vessel.bridge import register_in_interface_registry

register_in_interface_registry()
