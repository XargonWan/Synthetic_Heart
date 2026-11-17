"""Mirror Telegram's immutable, context-routed message shape."""

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from plugins.scene_context.scene_context import SceneContextPlugin
from plugins.scene_context.store import SceneStore


@pytest.fixture(autouse=True)
def opted_in_scene_config(monkeypatch):
    from core.config_manager import config_registry

    original = config_registry.get_value
    monkeypatch.setattr(
        config_registry,
        "get_value",
        lambda key, default=None, **kw: (
            True
            if key in {"SCENE_CONTEXT_ENABLED", "SCENE_CONTEXT_POSTFLIGHT_ENABLED"}
            else original(key, default, **kw)
        ),
    )


@dataclass(frozen=True)
class TelegramMessage:
    message_id: int
    date: datetime
    chat: object
    from_user: object
    text: str


@pytest.mark.asyncio
async def test_telegram_context_path_drives_all_scene_hooks(tmp_path, monkeypatch):
    plugin = SceneContextPlugin(store=SceneStore(tmp_path / "scene.sqlite3"))
    monkeypatch.setattr(plugin, "_enabled", lambda: True)
    message = TelegramMessage(
        message_id=17,
        date=datetime(2026, 10, 29, tzinfo=timezone.utc),
        chat=SimpleNamespace(id=1, type="private"),
        from_user=SimpleNamespace(id=1, first_name="Human", is_bot=False),
        text="You are on top of me",
    )
    context = {"interface_path": "telegram_bot/1"}
    assert not hasattr(message, "interface_path")
    assert plugin.is_recon_eligible(message, context)
    await plugin.get_recon_instruction(message=message, context_memory=context)
    await plugin.parse_recon_response(
        {
            "changes": [
                {
                    "field": "relation",
                    "value": {
                        "subject": "self",
                        "predicate": "on top of",
                        "object": "human",
                    },
                    "evidence": message.text,
                }
            ]
        },
        message=message,
        context_memory=context,
        text=message.text,
    )
    injection = await plugin.get_static_injection(message, context)
    assert "on top of Human" in injection["scene_context"]
    calls = []

    async def extract(messages, **kwargs):
        calls.append(messages)
        return {"changes": []}

    monkeypatch.setattr(plugin, "_extract_postflight", extract)
    await plugin.on_debrief(
        processed_actions=[
            {"type": "send_message", "payload": {"text": "I rest my head"}}
        ],
        failed_actions=[],
        results={},
        context=dict(context),
        original_message=message,
    )
    assert len(calls) == 1


def test_context_only_vessel_path_is_excluded(tmp_path, monkeypatch):
    from types import SimpleNamespace

    plugin = SceneContextPlugin(store=SceneStore(tmp_path / "scene.sqlite3"))
    monkeypatch.setattr(plugin, "_enabled", lambda: True)
    message = SimpleNamespace(
        from_user=SimpleNamespace(id=1, is_bot=False),
        chat=SimpleNamespace(type="private"),
    )
    assert not plugin.is_recon_eligible(message, {"interface_path": "vessel/minecraft"})
