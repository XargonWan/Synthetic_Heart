"""Regression test: the vessel corrector must carry the live world state.

The corrector sends a fresh single-message prompt with no history. A failed
in-world action (e.g. craft/drop) was therefore corrected blind — the model
could not see its inventory, position, or nearby blocks, and re-emitted recipe
chains that failed again. This verifies the correction prompt embeds a compact
structural world-state block whenever the context carries
``vessel_world_state``, and that the block is absent for ordinary chats.
"""

import json

import pytest

import core.transport_layer as transport_layer
from core.transport_layer import _render_vessel_world_state_block


class _FakePlugin:
    def __init__(self) -> None:
        self.captured_prompt: str | None = None

    async def handle_incoming_message(self, bot, message, prompt) -> str:
        self.captured_prompt = prompt
        return '{"actions": []}'


def _vessel_world_state() -> dict:
    return {
        "environment": "minecraft",
        "health": 20,
        "position": {"x": 148, "y": 64, "z": -180},
        "flags": {"connected": True, "is_day": True},
        "possible_actions": ["say", "move", "look", "observe", "craft"],
        "inventory_counts": {"oak_log": 3, "oak_planks": 2},
        "entities": [
            {"name": "chicken", "type": "chicken", "distance": 6},
            {"name": "sheep", "type": "sheep", "distance": 12},
        ],
        "blocks": [
            {"name": "grass_block", "distance": 1},
            {"name": "oak_log", "distance": 4},
        ],
        "affordances": [
            {"kind": "block", "target": "grass_block", "verb": "use", "distance": 1},
            {"kind": "block", "target": "oak_log", "verb": "mine", "distance": 4},
        ],
    }


def test_render_vessel_world_state_block_structural():
    block = _render_vessel_world_state_block(_vessel_world_state())
    assert "LIVE WORLD STATE" in block
    assert "x=148" in block
    assert "Health: 20" in block
    assert "oak_log x3" in block
    assert "oak_planks x2" in block
    assert "chicken (6m)" in block
    assert "grass_block (1m)" in block
    assert "mine → oak_log (4m)" in block


def test_render_vessel_world_state_block_empty_inventory():
    ws = _vessel_world_state()
    ws["inventory_counts"] = {}
    block = _render_vessel_world_state_block(ws)
    assert "Inventory: none" in block


def test_render_vessel_world_state_block_guards():
    assert _render_vessel_world_state_block(None) == ""
    assert _render_vessel_world_state_block("not a dict") == ""
    assert _render_vessel_world_state_block({}) == ""
    assert _render_vessel_world_state_block({"position": {"x": 1}}) != ""


@pytest.mark.asyncio
async def test_corrector_prompt_includes_vessel_world_state(monkeypatch):
    fake_plugin = _FakePlugin()

    import core.plugin_instance as plugin_instance

    monkeypatch.setattr(
        plugin_instance, "get_plugin", lambda: fake_plugin, raising=False
    )
    monkeypatch.setattr(
        "core.persona_manager.get_persona_manager",
        lambda: None,
    )

    from types import SimpleNamespace

    original_message = SimpleNamespace(
        correction_context={
            "successful_actions": [{"type": "vessel_minecraft_say"}],
            "successful_types": ["vessel_minecraft_say"],
            "successful_count": 1,
            "failed_actions": [
                {
                    "action": {
                        "type": "vessel_minecraft_craft",
                        "payload": {"item": "wooden_pickaxe"},
                    },
                    "errors": [
                        "no craftable recipe for 'wooden_pickaxe' with current materials"
                    ],
                }
            ],
            "failed_count": 1,
        },
        chat_id=1,
    )

    result = await transport_layer.run_corrector_middleware(
        text="invalid json",
        bot=None,
        context={
            "interface": "vessel",
            "interface_path": "vessel/minecraft/192_168_1_69_16269",
            "vessel_world_state": _vessel_world_state(),
            "message": original_message,
            "correction_context": {
                "successful_actions": [{"type": "vessel_minecraft_say"}],
                "successful_types": ["vessel_minecraft_say"],
                "successful_count": 1,
                "failed_actions": [
                    {
                        "action": {
                            "type": "vessel_minecraft_craft",
                            "payload": {"item": "wooden_pickaxe"},
                        },
                        "errors": [
                            "no craftable recipe for 'wooden_pickaxe' with current materials"
                        ],
                    }
                ],
                "failed_count": 1,
            },
        },
        chat_id=1,
    )

    assert result is not None
    assert fake_plugin.captured_prompt is not None
    assert "LIVE WORLD STATE" in fake_plugin.captured_prompt
    assert "oak_log x3" in fake_plugin.captured_prompt
    assert "Inventory" in fake_plugin.captured_prompt


@pytest.mark.asyncio
async def test_corrector_omits_world_state_for_ordinary_chat(monkeypatch):
    fake_plugin = _FakePlugin()

    import core.plugin_instance as plugin_instance

    monkeypatch.setattr(
        plugin_instance, "get_plugin", lambda: fake_plugin, raising=False
    )

    result = await transport_layer.run_corrector_middleware(
        text="this is not valid json",
        bot=None,
        context={"interface": "telegram_bot", "interface_path": "telegram_bot/1"},
        chat_id=1,
    )

    assert result is not None
    assert fake_plugin.captured_prompt is not None
    prompt = json.loads(fake_plugin.captured_prompt)
    assert "LIVE WORLD STATE" not in prompt["system_message"]["message"]
