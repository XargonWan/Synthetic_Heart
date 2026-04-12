import asyncio
from datetime import datetime
from types import SimpleNamespace

from core.prompt_engine import build_json_prompt


def test_build_json_prompt_reply_without_text(monkeypatch):
    async def dummy_gather(message, ctx):
        return {}

    monkeypatch.setattr("core.action_parser.gather_static_injections", dummy_gather)

    message = SimpleNamespace(
        chat_id=1,
        text="hello",
        message_id=1,
        from_user=SimpleNamespace(full_name="user", username="user"),
        date=datetime.utcnow(),
        reply_to_message=SimpleNamespace(
            message_id=2,
            from_user=SimpleNamespace(full_name="bot", username="bot"),
        ),
    )

    result = asyncio.run(build_json_prompt(message, {}, interface_name="discord_bot"))
    assert result["input"]["interface"] == "discord_bot"
    assert (
        result["input"]["payload"]["reply_message_id"]["text"] == "[Non-text content]"
    )
    # For chat-like interfaces we expect an unminified verbose instruction block
    assert "instructions_verbose" in result, (
        "Expected verbose chat instruction for chat interfaces"
    )
    assert (
        "You are participating in a live chat conversation"
        in result["instructions_verbose"]
    )


def test_build_json_prompt_inherits_image_data_from_context_memory(monkeypatch):
    async def dummy_gather(message, ctx):
        return {}

    monkeypatch.setattr("core.action_parser.gather_static_injections", dummy_gather)

    message = SimpleNamespace(
        chat_id=1,
        text="hello",
        message_id=1,
        from_user=SimpleNamespace(full_name="user", username="user"),
        date=datetime.utcnow(),
    )

    image_data = {
        "type": "image",
        "source": {"interface": "webui", "user_id": 1},
        "image_data": {"path": "/tmp/test.jpg"},
    }
    context_memory = {"image_data": image_data}

    result = asyncio.run(
        build_json_prompt(message, context_memory, interface_name="discord_bot")
    )

    assert result["input"]["payload"]["image"] == image_data


def test_instructions_prohibit_embedded_emotion_tags():
    from core.prompt_engine import load_json_instructions

    instructions = load_json_instructions()
    assert "Do NOT embed emotion tags" in instructions, (
        "Instructions should forbid embedding emotion tags in message text"
    )


def test_build_json_prompt_filters_actions_by_allowlist(monkeypatch):
    async def dummy_gather(message, ctx):
        return {}

    monkeypatch.setattr("core.action_parser.gather_static_injections", dummy_gather)

    from core.core_initializer import core_initializer

    original_actions_block = core_initializer.actions_block
    core_initializer.actions_block = {
        "available_actions": {
            "create_personal_diary_entry": {
                "schema": {"type": "object", "properties": {}, "required": []},
                "brief": "Create diary entry.",
                "source": "ai_diary",
            },
            "message_telegram_bot": {
                "schema": {"type": "object", "properties": {}, "required": []},
                "brief": "Send Telegram message.",
                "source": "telegram_bot",
            },
        }
    }

    try:
        message = SimpleNamespace(
            chat_id=-1,
            text="internal beat",
            message_id=1,
            from_user=SimpleNamespace(full_name="grillo", username="grillo"),
            date=datetime.utcnow(),
            interface_path="grillo/-1",
        )

        context_memory = {
            "grillo_beat": True,
            "beat_type": "self_reflection",
            "allowed_action_types": ["create_personal_diary_entry"],
        }

        result = asyncio.run(
            build_json_prompt(message, context_memory, interface_name="grillo")
        )

        assert list(result["actions"].keys()) == ["create_personal_diary_entry"]
    finally:
        core_initializer.actions_block = original_actions_block
