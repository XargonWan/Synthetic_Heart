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
    assert "instructions_verbose" in result, "Expected verbose chat instruction for chat interfaces"
    assert "You are participating in a live chat conversation" in result["instructions_verbose"]
