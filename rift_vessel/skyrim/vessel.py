"""SkyrimVessel — Rift Vessel adapter for The Elder Scrolls V: Skyrim."""

from __future__ import annotations

from typing import Any

from rift_vessel.rift_vessel_base import RiftVesselBase


SKYRIM_ACTIONS: dict[str, dict[str, Any]] = {
    "game_skyrim_attack": {
        "required_fields": ["target"],
        "description": "Attack a target (enemy or creature) with currently equipped weapon.",
        "parameters": {
            "target": {"type": "string", "description": "Name/ID of the target"},
            "power_attack": {
                "type": "boolean",
                "description": "Perform a power attack if true",
                "default": False,
            },
        },
    },
    "game_skyrim_cast_spell": {
        "required_fields": ["spell"],
        "description": "Cast a spell from the equipped/known list.",
        "parameters": {
            "spell": {"type": "string", "description": "Spell name or ID"},
            "target": {
                "type": "string",
                "description": "Optional target for offensive/heal spells",
            },
            "dual_cast": {
                "type": "boolean",
                "description": "Dual-cast if available",
                "default": False,
            },
        },
    },
    "game_skyrim_shout": {
        "required_fields": ["shout"],
        "description": "Use a Thu'um (dragon shout).",
        "parameters": {
            "shout": {"type": "string", "description": "Shout name or ID"},
            "words": {
                "type": "integer",
                "description": "Number of words (1-3)",
                "default": 1,
            },
        },
    },
    "game_skyrim_loot": {
        "description": "Loot a container or creature.",
        "parameters": {
            "target": {"type": "string", "description": "Container or corpse name/ID"},
            "items": {
                "type": "array",
                "description": "Specific items to take (empty = take all)",
                "items": {"type": "string"},
            },
        },
    },
    "game_skyrim_equip": {
        "required_fields": ["item"],
        "description": "Equip a weapon, spell, or piece of armor.",
        "parameters": {
            "item": {"type": "string", "description": "Item name or ID"},
            "hand": {
                "type": "string",
                "description": "left, right, or both",
                "default": "right",
            },
        },
    },
    "game_skyrim_use_item": {
        "required_fields": ["item"],
        "description": "Use a consumable item (potion, food, scroll).",
        "parameters": {
            "item": {"type": "string", "description": "Item name or ID"},
        },
    },
    "game_skyrim_dialog": {
        "required_fields": ["npc"],
        "description": "Start or continue dialog with an NPC.",
        "parameters": {
            "npc": {"type": "string", "description": "NPC name or ID"},
            "topic": {
                "type": "string",
                "description": "Dialog topic or keyword to use",
            },
        },
    },
    "game_skyrim_move": {
        "required_fields": ["location"],
        "description": "Move/walk to a specified location or follow an NPC.",
        "parameters": {
            "location": {
                "type": "string",
                "description": "Location name or map marker",
            },
            "sprint": {
                "type": "boolean",
                "description": "Sprint if true",
                "default": False,
            },
        },
    },
    "game_skyrim_quick_save": {
        "description": "Create a quicksave.",
    },
    "game_skyrim_quick_load": {
        "description": "Load the most recent quicksave.",
    },
    "game_skyrim_wait": {
        "required_fields": ["hours"],
        "description": "Wait/skip time.",
        "parameters": {
            "hours": {
                "type": "number",
                "description": "Hours to wait (1-24)",
            },
        },
    },
    "game_skyrim_open_map": {
        "description": "Open the world map.",
    },
    "game_skyrim_fast_travel": {
        "required_fields": ["location"],
        "description": "Fast travel to a discovered location.",
        "parameters": {
            "location": {"type": "string", "description": "Discovered location name"},
        },
    },
}


class SkyrimVessel(RiftVesselBase):
    def __init__(self) -> None:
        super().__init__()
        self._name = "skyrim"

    def get_interface_id(self) -> str:
        return "skyrim"

    def get_supported_actions(self) -> dict:
        return dict(SKYRIM_ACTIONS)

    async def execute_game_action(self, action: str, params: dict) -> dict:
        action_def = SKYRIM_ACTIONS.get(action)
        if action_def is None:
            return {"status": "error", "error": f"Unknown skyrim action: {action}"}
        return {
            "status": "ok",
            "action": action,
            "note": f"[SkyrimVessel] {action} queued (SKSE IPC not connected)",
        }

    def get_world_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "environment": {"const": "skyrim"},
                "health": {"type": "number"},
                "max_health": {"type": "number"},
                "magicka": {"type": "number"},
                "stamina": {"type": "number"},
                "location": {"type": "string"},
                "combat_state": {"type": "boolean"},
                "is_sneaking": {"type": "boolean"},
                "is_mounted": {"type": "boolean"},
                "current_weapon": {"type": "string"},
                "current_spell": {"type": "string"},
                "current_shout": {"type": "string"},
                "level": {"type": "integer"},
                "carry_weight_pct": {"type": "number"},
                "gold": {"type": "integer"},
            },
        }


VESSEL_CLASS = SkyrimVessel
