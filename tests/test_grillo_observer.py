import pytest
from datetime import datetime, timedelta, timezone

import plugins.grillo.grillo_chat_observer as gco
from core import message_queue


@pytest.mark.asyncio
async def test_observer_builds_prompt_and_collects(monkeypatch):
    plugin = gco.GrilloChatObserverPlugin()

    # force update checker to report new messages (DB not available)
    async def fake_check(consume=True):
        return {"updated": True, "new_messages": [], "last_checked": ""}

    monkeypatch.setattr("core.chat_update_checker.check_for_updates_once", fake_check)

    # Mock collect_recent_snippets to return predictable data
    async def fake_collect(limit: int) -> list[str]:
        return [
            "(chat:telegram_bot/1) Hello world",
            "(chat:telegram_bot/2) Another message",
        ]

    monkeypatch.setattr(plugin, "_collect_recent_snippets", fake_collect)

    # Mock create_activity_log
    class FakeGrillo:
        @staticmethod
        async def create_activity_log(beat_type, prompt_text=None):
            return 12345

    monkeypatch.setattr("plugins.grillo.grillo_impl.GrilloPlugin", FakeGrillo)

    # Spy on message_queue.enqueue_low_priority
    called = {}

    async def fake_enqueue(
        bot, message, context_memory=None, interface_id=None, original_message=None
    ):
        called["ctx"] = context_memory
        called["text"] = getattr(message, "text", None)

    monkeypatch.setattr(message_queue, "enqueue_low_priority", fake_enqueue)

    # ensure the first-run guard is bypassed
    plugin._last_run_ts = 1.0
    await plugin._run_observer()

    assert "ctx" in called and called["ctx"].get("beat_type") == "observer"
    # snippets are now attached to context and included in the text
    assert "(chat:telegram_bot/1)" in called["text"]
    assert called["ctx"].get("grillo_snippets") == [
        "(chat:telegram_bot/1) Hello world",
        "(chat:telegram_bot/2) Another message",
    ]


def test_build_observer_prompt_returns_string():
    plugin = gco.GrilloChatObserverPlugin()
    prompt = plugin._build_observer_prompt(["sample snippet"])
    assert isinstance(prompt, str)
    assert "Snippets:" in prompt
    # ensure at least one of the universal JSON instructions appears
    assert "actions" in prompt or "JSON" in prompt
    # Example JSON structure should be included
    assert (
        '{"actions": []}' in prompt
    )  # JSON example with double quotes should be present
    import plugins.grillo.grillo_chat_observer as gco_mod

    assert gco_mod.OBSERVER_INSTRUCTIONS in prompt


@pytest.mark.asyncio
async def test_collect_recent_snippets_includes_sender_and_timestamp(monkeypatch):
    plugin = gco.GrilloChatObserverPlugin()

    async def mock_get_last_active_chats_verbose(n):
        return [(1, "Chat A")]

    async def mock_load_chat_history(interface_path):
        from collections import deque

        return deque(
            [
                {
                    "text": "Hello",
                    "sender_name": "Rekku",
                    "timestamp": "2026-01-11T03:51:00Z",
                },
                {
                    "text": "User message",
                    "sender_name": "Jay",
                    "timestamp": "2026-01-11T03:52:00Z",
                },
            ]
        )

    import core.recent_chats as recent_chats

    monkeypatch.setattr(
        recent_chats,
        "get_last_active_chats_verbose",
        mock_get_last_active_chats_verbose,
    )
    import core.chat_history_cache as chat_history_cache

    monkeypatch.setattr(chat_history_cache, "load_chat_history", mock_load_chat_history)

    snippets = await plugin._collect_recent_snippets(2)
    assert isinstance(snippets, list)
    assert len(snippets) >= 1
    # Ensure sender and timestamp metadata are included
    assert "sender:" in snippets[0]
    assert "2026" in snippets[0]


@pytest.mark.asyncio
async def test_collect_recent_snippets_skips_recent_bot_messages(monkeypatch):
    plugin = gco.GrilloChatObserverPlugin()
    plugin.self_skip_window = 3600  # 1h

    async def mock_get_last_active_chats_verbose(n):
        return [(1, "Chat A")]

    monkeypatch.setattr(
        "core.recent_chats.get_last_active_chats_verbose",
        mock_get_last_active_chats_verbose,
    )
    monkeypatch.setattr("core.recent_chats.get_chat_path", lambda cid: "telegram_bot/1")

    async def fake_load_chat_history(path):
        from collections import deque

        return deque(
            [
                {
                    "text": "Hello",
                    "sender_name": "self",
                    "timestamp": (
                        datetime.now(timezone.utc) - timedelta(seconds=1800)
                    ).isoformat(),
                }
            ]
        )

    import core.chat_history_cache as chat_history_cache

    monkeypatch.setattr(chat_history_cache, "load_chat_history", fake_load_chat_history)

    snippets = await plugin._collect_recent_snippets(5)
    assert snippets == []  # skipped because last message was from self within window

    # if message is older than window we should include it
    async def fake_load_chat_history2(path):
        from collections import deque

        return deque(
            [
                {
                    "text": "Hello",
                    "sender_name": "self",
                    "timestamp": (
                        datetime.now(timezone.utc) - timedelta(seconds=7200)
                    ).isoformat(),
                }
            ]
        )

    monkeypatch.setattr(
        chat_history_cache, "load_chat_history", fake_load_chat_history2
    )
    snippets2 = await plugin._collect_recent_snippets(5)
    assert snippets2 != []


@pytest.mark.asyncio
async def test_observer_propose_only_flag_in_prompt(monkeypatch):
    plugin = gco.GrilloChatObserverPlugin()
    plugin.propose_only = True

    # bypass DB update check
    async def fake_check(consume=True):
        return {"updated": True, "new_messages": [], "last_checked": ""}

    monkeypatch.setattr("core.chat_update_checker.check_for_updates_once", fake_check)

    # minimal snippet
    async def fake_collect(limit: int) -> list[str]:
        return ["test"]

    monkeypatch.setattr(plugin, "_collect_recent_snippets", fake_collect)

    class FakeGrillo:
        @staticmethod
        async def create_activity_log(beat_type, prompt_text=None):
            return None

    monkeypatch.setattr("plugins.grillo.grillo_impl.GrilloPlugin", FakeGrillo)

    captured = {}

    async def fake_enqueue(
        bot, message, context_memory=None, interface_id=None, original_message=None
    ):
        captured["text"] = getattr(message, "text", None)

    monkeypatch.setattr(message_queue, "enqueue_low_priority", fake_enqueue)

    # bypass first-run guard
    plugin._last_run_ts = 1.0
    await plugin._run_observer()

    assert (
        "proposal-only" in captured["text"].lower()
        or "proposal" in captured["text"].lower()
    )
    # the prompt should still include the word "chat" as a sanity check
    assert "chat" in captured["text"].lower()


@pytest.mark.asyncio
async def test_observer_runs_when_updates_present(monkeypatch):
    plugin = gco.GrilloChatObserverPlugin()

    # Make the checker report that there are updates
    async def fake_check(consume=True):
        return {
            "updated": True,
            "new_messages": [],
            "last_checked": "2026-01-01T00:00:00Z",
        }

    monkeypatch.setattr("core.chat_update_checker.check_for_updates_once", fake_check)

    # Spy on collect and enqueue to ensure both are executed
    called = {}

    async def fake_collect(limit: int) -> list[str]:
        called["collected"] = True
        return ["test snippet"]

    monkeypatch.setattr(plugin, "_collect_recent_snippets", fake_collect)

    async def fake_enqueue(
        bot, message, context_memory=None, interface_id=None, original_message=None
    ):
        called["enqueued"] = True

    from core import message_queue

    monkeypatch.setattr(message_queue, "enqueue_low_priority", fake_enqueue)

    # bypass the first-run guard
    plugin._last_run_ts = 1.0
    await plugin._run_observer()

    assert "collected" in called
    assert "enqueued" in called


@pytest.mark.asyncio
async def test_observer_db_check_updates_and_advances_last_run_ts(monkeypatch):
    """If the direct DB check finds messages since plugin._last_run_ts the
    observer should proceed, enqueue the prompt and advance its last_run_ts
    to the reported max_ts."""
    plugin = gco.GrilloChatObserverPlugin()

    # Initialize last run to an earlier timestamp
    plugin._last_run_ts = 1000.0

    expected_max_ts = datetime.fromtimestamp(1100.0, tz=timezone.utc)

    async def fake_execute(
        query: str, params: tuple[object, ...] = ()
    ) -> list[dict[str, object]]:
        # This corresponds to the COUNT/MAX query used in _run_observer()
        if "SELECT COUNT(*) as cnt, MAX(timestamp) as max_ts" in query:
            assert params
            assert params[0] == datetime.fromtimestamp(1000.0, tz=timezone.utc)
            return [{"cnt": 1, "max_ts": expected_max_ts}]
        return []

    # Replace DB executor used inside plugin
    monkeypatch.setattr("core.db.execute_query", fake_execute)

    # Spy on collect and enqueue
    called = {}
    # intercept config persistence
    persisted = {}

    async def fake_set_value(key, value):
        if key == "GRILLO_OBSERVER_LAST_RUN_TS":
            persisted["last_run"] = value

    monkeypatch.setattr("core.config_manager.config_registry.set_value", fake_set_value)

    async def fake_collect(limit: int) -> list[str]:
        called["collected"] = True
        return ["(chat:telegram_bot/1) Hello"]

    monkeypatch.setattr(plugin, "_collect_recent_snippets", fake_collect)

    async def fake_enqueue(
        bot, message, context_memory=None, interface_id=None, original_message=None
    ):
        called["enqueued"] = True

    from core import message_queue

    monkeypatch.setattr(message_queue, "enqueue_low_priority", fake_enqueue)

    await plugin._run_observer()

    assert called.get("collected") is True
    assert called.get("enqueued") is True
    # last_run_ts should have advanced to the DB-reported max_ts
    assert plugin._last_run_ts == 1100.0
    # also ensure the timestamp was persisted back to config
    assert persisted.get("last_run") == 1100.0


@pytest.mark.asyncio
async def test_observer_skips_when_no_updates(monkeypatch):
    plugin = gco.GrilloChatObserverPlugin()

    # Make the checker report that there are NO updates
    async def fake_check(consume=True):
        return {
            "updated": False,
            "new_messages": [],
            "last_checked": "2026-01-01T00:00:00Z",
        }

    monkeypatch.setattr("core.chat_update_checker.check_for_updates_once", fake_check)

    # Spy on collect and enqueue to ensure they are NOT executed
    called = {}

    async def fake_collect(limit: int) -> list[str]:
        called["collected"] = True
        return ["test snippet"]

    monkeypatch.setattr(plugin, "_collect_recent_snippets", fake_collect)

    async def fake_enqueue(
        bot, message, context_memory=None, interface_id=None, original_message=None
    ):
        called["enqueued"] = True

    from core import message_queue

    monkeypatch.setattr(message_queue, "enqueue_low_priority", fake_enqueue)

    plugin._last_run_ts = 1.0
    await plugin._run_observer()

    assert "collected" not in called
    assert "enqueued" not in called
