import asyncio
from datetime import datetime
import pytest

import plugins.grillo.grillo_chat_observer as gco
import plugins.grillo.grillo_dream as gd


@pytest.mark.asyncio
async def test_observer_logs_activity(caplog, monkeypatch):
    caplog.set_level("INFO")

    observer = gco.GrilloChatObserverPlugin()

    async def fake_execute_query(q, params):
        # Simulate 1 new message and a recent max_ts
        return [{"cnt": 1, "max_ts": int(datetime.utcnow().timestamp())}]

    async def fake_collect(limit):
        return ["(chat:telegram_bot/1 | sender:someone | 2026-01-01) Test message"]

    async def fake_create_activity_log(*args, **kwargs):
        return 999

    async def fake_enqueue_low_priority(bot, message, context_memory=None, interface_id=None, original_message=None):
        return True

    monkeypatch.setattr("core.db.execute_query", fake_execute_query)
    monkeypatch.setattr(observer, "_collect_recent_snippets", fake_collect)
    monkeypatch.setattr("plugins.grillo.grillo_impl.GrilloPlugin.create_activity_log", fake_create_activity_log)
    monkeypatch.setattr("core.message_queue.enqueue_low_priority", fake_enqueue_low_priority)

    # Run observer once
    await observer._run_observer()

    # Assert the activity log line was emitted and contains our activity id
    logged = "\n".join([r.getMessage() for r in caplog.records])
    assert "GRILLO_ACTIVITY id=999" in logged
    assert "Observer prompt enqueued for LLM processing" in logged


@pytest.mark.asyncio
async def test_dream_logs_activity(caplog, monkeypatch):
    caplog.set_level("INFO")

    dream = gd.GrilloDreamPlugin()

    async def fake_collect(limit):
        return ["(chat:telegram_bot/1 | sender:someone | 2026-01-01) Dreamy message"]

    async def fake_create_activity_log(*args, **kwargs):
        return 4242

    async def fake_enqueue_low_priority(bot, message, context_memory=None, interface_id=None, original_message=None):
        return True

    monkeypatch.setattr(dream, "_collect_fragments", fake_collect)
    monkeypatch.setattr("plugins.grillo.grillo_impl.GrilloPlugin.create_activity_log", fake_create_activity_log)
    monkeypatch.setattr("core.message_queue.enqueue_low_priority", fake_enqueue_low_priority)

    await dream._generate_dream()

    logged = "\n".join([r.getMessage() for r in caplog.records])
    assert "GRILLO_ACTIVITY id=4242" in logged
    assert "Dream enqueued for LLM processing" in logged
