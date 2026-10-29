"""Scene tracking is paid opt-in functionality, with visible dashboard controls."""

from types import SimpleNamespace

import pytest

from core.variables_engine import exposed_vars
from plugins.scene_context.scene_context import SceneContextPlugin
from plugins.scene_context.store import SceneStore


def test_scene_controls_are_visible_toggles_and_default_off():
    for key in ("SCENE_CONTEXT_ENABLED", "SCENE_CONTEXT_POSTFLIGHT_ENABLED"):
        definition = exposed_vars.get_definition(key)
        assert definition is not None
        assert definition.default is False
        assert definition.ui_type == "bool"
        assert definition.value_type is bool
        assert definition.component == "scene_context"
        assert not definition.hidden
        assert not definition.advanced
    postflight = exposed_vars.get_definition("SCENE_CONTEXT_POSTFLIGHT_ENABLED")
    assert postflight is not None
    assert "call" in postflight.description.lower()


@pytest.mark.asyncio
async def test_fresh_config_makes_no_scene_calls_or_store(tmp_path, monkeypatch):
    from core.config_manager import config_registry

    monkeypatch.setattr(
        config_registry, "get_value", lambda key, default=None, **kwargs: default
    )
    path = tmp_path / "scene.sqlite3"
    plugin = SceneContextPlugin(store=SceneStore(path))
    message = SimpleNamespace(
        interface_path="telegram_bot/1",
        text="we are at home",
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=1, is_bot=False),
    )

    async def forbidden(*args, **kwargs):
        pytest.fail("Opted-out tracker reached an extractor")

    monkeypatch.setattr(plugin, "_extract_postflight", forbidden)
    assert not plugin.is_recon_eligible(message, {})
    assert await plugin.get_static_injection(message, {}) == {}
    await plugin.on_debrief(
        processed_actions=[
            {"type": "send_message", "payload": {"text": "I sit beside you"}}
        ],
        failed_actions=[],
        results={},
        context={},
        original_message=message,
    )
    assert not path.exists()


@pytest.mark.asyncio
async def test_postflight_requires_separate_opt_in(tmp_path, monkeypatch):
    from core.config_manager import config_registry

    monkeypatch.setattr(
        config_registry,
        "get_value",
        lambda key, default=None, **kwargs: (
            True if key == "SCENE_CONTEXT_ENABLED" else default
        ),
    )
    plugin = SceneContextPlugin(store=SceneStore(tmp_path / "scene.sqlite3"))
    message = SimpleNamespace(
        interface_path="telegram_bot/1",
        text="we are at home",
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=1, is_bot=False),
    )
    context = {}
    await plugin.get_recon_instruction(message=message, context_memory=context)

    async def forbidden(*args, **kwargs):
        pytest.fail("Postflight request happened without its own opt-in")

    monkeypatch.setattr(plugin, "_extract_postflight", forbidden)
    await plugin.on_debrief(
        processed_actions=[
            {"type": "send_message", "payload": {"text": "I sit beside you"}}
        ],
        failed_actions=[],
        results={},
        context=context,
        original_message=message,
    )
