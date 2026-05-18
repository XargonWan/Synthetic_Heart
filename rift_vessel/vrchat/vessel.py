"""VRChatVessel — Rift Vessel adapter for VRChat.

Communicates with VRChat over the local OSC interface (UDP port 9000
for outbound, 9001 for inbound avatar state).  This Phase-0 stub
defines all action types and schemas; real OSC I/O will require the
optional ``python-osc`` package.
"""

from __future__ import annotations

from typing import Any

from rift_vessel.rift_vessel_base import RiftVesselBase


VRCHAT_ACTIONS: dict[str, dict[str, Any]] = {
    "game_vrchat_set_parameter": {
        "required_fields": ["parameter", "value"],
        "description": "Set an avatar OSC parameter (float, bool, or int).",
        "parameters": {
            "parameter": {"type": "string", "description": "Avatar parameter name"},
            "value": {"type": "any", "description": "Value (float 0-1, bool, or int)"},
        },
    },
    "game_vrchat_change_avatar": {
        "required_fields": ["avatar_id"],
        "description": "Switch to a different avatar by its ID.",
        "parameters": {
            "avatar_id": {"type": "string", "description": "VRChat avatar ID"},
        },
    },
    "game_vrchat_chatbox_message": {
        "required_fields": ["message"],
        "description": "Send a text message to the player's VRChat chatbox overlay.",
        "parameters": {
            "message": {
                "type": "string",
                "description": "Text to display (max 144 chars)",
            },
            "duration_s": {
                "type": "number",
                "description": "Display duration in seconds",
                "default": 5,
            },
        },
    },
    "game_vrchat_chatbox_typing": {
        "description": "Show or hide the typing indicator in the chatbox.",
        "parameters": {
            "typing": {
                "type": "boolean",
                "description": "True to show typing indicator",
                "default": True,
            },
        },
    },
    "game_vrchat_teleport_to": {
        "required_fields": ["target"],
        "description": "Teleport to a player or named location inside the world.",
        "parameters": {
            "target": {
                "type": "string",
                "description": "Player display name or location name",
            },
        },
    },
    "game_vrchat_spawn_emoji": {
        "required_fields": ["emoji"],
        "description": "Spawn a reaction emoji above the player's head.",
        "parameters": {
            "emoji": {
                "type": "string",
                "description": "Emoji shortcode (e.g. 'heart' or 'fire')",
            },
        },
    },
    "game_vrchat_mute": {
        "description": "Toggle microphone mute state.",
        "parameters": {
            "muted": {
                "type": "boolean",
                "description": "True to mute, False to unmute",
                "default": True,
            },
        },
    },
    "game_vrchat_get_nearby_players": {
        "description": "Get a list of players currently nearby in the world.",
    },
    "game_vrchat_emote": {
        "required_fields": ["emote"],
        "description": "Play a VRChat animation emote.",
        "parameters": {
            "emote": {
                "type": "string",
                "description": "Emote name or animation trigger",
            },
        },
    },
    "game_vrchat_get_world_info": {
        "description": "Get current world information (name, player count, fps, ping).",
    },
}


class VRChatVessel(RiftVesselBase):
    def __init__(self) -> None:
        super().__init__()
        self._name = "vrchat"

    def get_interface_id(self) -> str:
        return "vrchat"

    def get_supported_actions(self) -> dict:
        return dict(VRCHAT_ACTIONS)

    async def execute_game_action(self, action: str, params: dict) -> dict:
        action_def = VRCHAT_ACTIONS.get(action)
        if action_def is None:
            return {"status": "error", "error": f"Unknown vrchat action: {action}"}
        return {
            "status": "ok",
            "action": action,
            "note": f"[VRChatVessel] {action} queued (OSC not connected)",
        }

    def get_world_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "environment": {"const": "vrchat"},
                "world_name": {"type": "string"},
                "instance_id": {"type": "string"},
                "player_count": {"type": "integer"},
                "fps": {"type": "number"},
                "ping_ms": {"type": "integer"},
                "tracked_avatar_params": {
                    "type": "object",
                    "additionalProperties": {"type": "number"},
                    "description": "Current avatar OSC parameter values",
                },
            },
        }


VESSEL_CLASS = VRChatVessel
