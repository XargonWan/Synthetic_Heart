import pytest
from types import SimpleNamespace

import core.action_parser as action_parser


@pytest.mark.asyncio
async def test_grillo_message_dedupe_suppresses(monkeypatch):
    # simulate that a similar message was recently sent
    async def fake_check(interface_path, text, window):
        return True

    monkeypatch.setattr(action_parser, "_grillo_recent_same_message", fake_check)

    context = {"grillo_beat": True}
    original = SimpleNamespace(from_cortex=True)
    actions = [
        {
            "type": "message_telegram_bot",
            "payload": {"text": "hello", "interface_path": "telegram_bot/123"},
        }
    ]

    # bypass validation to avoid interface lookups during the test
    monkeypatch.setattr(action_parser, "validate_action", lambda a, c, o: (True, []))

    async def fake_run_action(a, c, b, o):
        return {}

    monkeypatch.setattr(action_parser, "run_action", fake_run_action)

    result = await action_parser.run_actions(
        actions, context, bot=None, original_message=original
    )
    # no actions should have been processed because the duplicate guard dropped it
    assert result.get("processed") == []
    # errors may include correction-related entries but we don't care


@pytest.mark.asyncio
async def test_grillo_message_dedupe_allows_non_grillo(monkeypatch):
    # even if _grillo_recent_same_message returns True, non-grillo runs should ignore it
    async def fake_check(interface_path, text, window):
        return True

    monkeypatch.setattr(action_parser, "_grillo_recent_same_message", fake_check)

    context = {}  # no grillo_beat flag
    original = SimpleNamespace(from_cortex=True)
    actions = [
        {
            "type": "message_telegram_bot",
            "payload": {"text": "hello", "interface_path": "telegram_bot/123"},
        }
    ]

    monkeypatch.setattr(action_parser, "validate_action", lambda a, c, o: (True, []))

    async def fake_run_action(a, c, b, o):
        return {}

    monkeypatch.setattr(action_parser, "run_action", fake_run_action)

    result = await action_parser.run_actions(
        actions, context, bot=None, original_message=original
    )
    # since this isn't a grillo beat, duplicate guard should not drop it
    assert result.get("processed") == actions
