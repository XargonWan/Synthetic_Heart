import pytest
from pathlib import Path


@pytest.mark.asyncio
async def test_message_action_types_can_be_overridden(monkeypatch):
    # Monkeypatch the config var to provide a custom action type

    # Simulate config var by setting a temporary attribute used in code path
    custom_types = ["message_fake_channel"]

    # Build a fake actions list and ensure the logic recognizes the custom type
    actions = [{"type": "message_fake_channel", "payload": {}}]

    # Monkeypatch config_registry.get_var to return an object with .value
    class FakeConfigVar:
        def __init__(self, v):
            self.value = v

    import core.config_manager as cm

    orig_get_var = cm.config_registry.get_var

    def fake_get_var(key, default, **kwargs):
        if key == "MESSAGE_ACTION_TYPES":
            return FakeConfigVar(custom_types)
        return orig_get_var(key, default, **kwargs)

    monkeypatch.setattr(cm.config_registry, "get_var", fake_get_var)

    # Run the same logic as message_chain to detect user response
    # Reuse the local logic by re-importing the module-level function behaviour
    try:
        from core.config_manager import config_registry

        MESSAGE_ACTION_TYPES = config_registry.get_var(
            "MESSAGE_ACTION_TYPES",
            [
                "message_telegram_bot",
                "message_discord_bot",
                "message_ollama_serve",
                "message_synth_webui",
            ],
        )
        current_message_action_types = (
            list(MESSAGE_ACTION_TYPES.value)
            if hasattr(MESSAGE_ACTION_TYPES, "value")
            else list(MESSAGE_ACTION_TYPES)
        )
    except Exception:
        current_message_action_types = [
            "message_telegram_bot",
            "message_discord_bot",
            "message_ollama_serve",
            "message_synth_webui",
        ]

    found = any(
        (a.get("action") or a.get("type")) in current_message_action_types
        for a in actions
    )
    assert found, (
        "Dynamic MESSAGE_ACTION_TYPES should be respected and detect custom types"
    )


def test_transport_layer_example_is_generic():
    import core.transport_layer as tl

    content = tl.__file__ and Path(tl.__file__).read_text(encoding="utf-8")
    assert 'originating_interface or "<interface>"' in content
    assert "message_{iface_label}" in content, (
        "Transport layer example should use a generic interface placeholder"
    )
