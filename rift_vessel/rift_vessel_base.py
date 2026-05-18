from __future__ import annotations

from abc import ABC, abstractmethod

from rift_vessel.schema import WorldEvent, WorldState


class RiftVesselBase(ABC):
    """Abstract base for all game/environment embodiment adapters.

    A RiftVessel IS a SyntH interface (it registers in INTERFACE_REGISTRY)
    but carries additional semantics for world interaction.
    """

    def __init__(self) -> None:
        self._latest_world_state: WorldState | None = None

    @abstractmethod
    def get_interface_id(self) -> str:
        """Return e.g. 'skyrim', 'vrchat', 'godot'."""

    @abstractmethod
    def get_supported_actions(self) -> dict:
        """Return environment-specific actions the vessel can execute.

        Example::
            {
                "game_attack": {
                    "required_fields": ["target"],
                    "description": "Attack a target entity",
                },
                "game_move": {
                    "required_fields": ["location"],
                    "description": "Move to a named location",
                },
            }
        """

    def get_supported_game_action_types(self) -> list[str]:
        """Return list of game_* action types this vessel supports."""
        return list(self.get_supported_actions().keys())

    def start(self) -> None:
        """Optional: start background connection to the game."""

    def stop(self) -> None:
        """Optional: teardown connection to the game."""

    async def send_world_state(self, state: WorldState) -> None:
        """Push current SyntH world-state understanding to the game adapter.

        Called periodically by the bridge to sync SyntH's mental model
        with the game's actual state.
        """
        self._latest_world_state = state

    @abstractmethod
    async def execute_game_action(self, action: str, params: dict) -> dict:
        """Execute a game-native action and return the result."""

    async def on_world_event(self, event: WorldEvent) -> None:
        """Called when the game adapter pushes a world-state change.

        Default: stores the event. Subclasses can override to enqueue
        into the core message queue.
        """
        self._latest_event = event

    def get_world_schema(self) -> dict:
        """Return the JSON schema for this environment's WorldState."""
        return {}

    def get_latest_world_state(self) -> WorldState | None:
        """Return the most recent cached WorldState."""
        return self._latest_world_state

    @staticmethod
    def get_supported_actions() -> dict:
        """Static variant for action discovery.
        Subclasses can override with class-level action definitions.
        """
        return {}

    def is_enabled(self) -> bool:
        return True
