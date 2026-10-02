import pytest
from types import SimpleNamespace

from core import message_queue, plugin_instance, rate_limit


@pytest.mark.asyncio
async def test_enqueue_clears_voice_and_tts_flags(monkeypatch):
    """The context dict should not retain ``is_voice_input``/``request_tts``
    after a non-voice message is enqueued.

    Regression: previously enqueue() only set those flags when the incoming
    message carried them, but never removed them.  If the context had been
    reused from a previous voice message, the stale ``is_voice_input`` value
    would persist and later cause message_chain to auto-inject a TTS reply
    even though the *current* input was plain text.  This test exercises the
    behaviour by passing in a pre-populated context and then verifying it is
    cleaned up.
    """

    # fake "previous" context that still believes the last input was voice
    context = {"is_voice_input": True, "request_tts": True}

    # avoid touching the database during this unit test
    from unittest.mock import AsyncMock

    monkeypatch.setattr(message_queue, "is_user_blocked", AsyncMock(return_value=False))

    # ensure a plugin is available so enqueue() doesn't bail out early
    from core import plugin_instance

    class DummyPlugin:
        def get_rate_limit(self):
            return 1000, 1, 1.0

    monkeypatch.setattr(plugin_instance, "get_plugin", lambda: DummyPlugin())
    monkeypatch.setattr(rate_limit, "is_allowed", lambda *args, **kwargs: True)

    # intercept consumer invocation so we can inspect the final context
    recorded = []

    async def fake_handle(bot, message, context_memory_or_prompt, interface=None, **kw):
        # context_memory_or_prompt may either be a dict or other object; for our
        # purposes it should be a dict containing the context that reaches the
        # plugin.
        if isinstance(context_memory_or_prompt, dict):
            recorded.append(context_memory_or_prompt.copy())
        else:
            recorded.append(context_memory_or_prompt)

    monkeypatch.setattr(plugin_instance, "handle_incoming_message", fake_handle)

    # construct a simple text-only message; minimal attributes used by enqueue
    msg = SimpleNamespace(
        chat_id=1,
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(
            type="private", id=1, title=None, username=None, first_name=None
        ),
        text="hello world",
    )

    # ensure the queue consumer is running so our fake_handle will be invoked
    await message_queue.run()

    # call enqueue with skip_mention_check to bypass the mention logic
    await message_queue.enqueue(
        bot=None,
        message=msg,
        context_memory=context,
        interface_id="telegram_bot",
        skip_mention_check=True,
    )

    # give the consumer loop a moment to process the item
    import asyncio

    await asyncio.sleep(0.1)

    assert recorded, "consumer should have been invoked"
    final_context = recorded[0]
    assert not final_context.get("is_voice_input", False), (
        "voice flag should be cleared by consumer"
    )
    assert not final_context.get("request_tts", False), (
        "request_tts flag should be cleared by consumer"
    )


@pytest.mark.asyncio
async def test_enqueue_and_wait_returns_plugin_response(monkeypatch):
    """enqueue_and_wait should return the plugin processing result."""

    from unittest.mock import AsyncMock

    monkeypatch.setattr(message_queue, "is_user_blocked", AsyncMock(return_value=False))

    class DummyPlugin:
        def get_rate_limit(self):
            return 1000, 1, 1.0

    monkeypatch.setattr(plugin_instance, "get_plugin", lambda: DummyPlugin())
    monkeypatch.setattr(
        message_queue.rate_limit, "is_allowed", lambda *args, **kwargs: True
    )

    async def fake_handle(bot, message, context_memory_or_prompt, interface=None, **kw):
        return "llm-ok"

    monkeypatch.setattr(plugin_instance, "handle_incoming_message", fake_handle)

    msg = SimpleNamespace(
        chat_id=1,
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(
            type="private", id=1, title=None, username=None, first_name=None
        ),
        text="hello world",
    )

    await message_queue.run()
    result = await message_queue.enqueue_and_wait(
        bot=None,
        message=msg,
        context_memory={},
        interface_id="telegram_bot",
        skip_mention_check=True,
        timeout=2.0,
    )

    assert result == "llm-ok"


@pytest.mark.asyncio
async def test_enqueue_preserves_explicit_interface_path(monkeypatch):
    """Explicit message.interface_path should be preserved through the queue."""

    from unittest.mock import AsyncMock

    monkeypatch.setattr(message_queue, "is_user_blocked", AsyncMock(return_value=False))

    class DummyPlugin:
        def get_rate_limit(self):
            return 1000, 1, 1.0

    monkeypatch.setattr(plugin_instance, "get_plugin", lambda: DummyPlugin())
    monkeypatch.setattr(
        message_queue.rate_limit, "is_allowed", lambda *args, **kwargs: True
    )

    recorded = []

    async def fake_handle(bot, message, context_memory_or_prompt, interface=None, **kw):
        if isinstance(context_memory_or_prompt, dict):
            recorded.append(context_memory_or_prompt.copy())
        return "ok"

    monkeypatch.setattr(plugin_instance, "handle_incoming_message", fake_handle)

    msg = SimpleNamespace(
        chat_id=1,
        from_user=SimpleNamespace(id=42),
        chat=SimpleNamespace(
            type="private", id=1, title=None, username=None, first_name=None
        ),
        text="hello world",
    )
    msg.interface_path = "telegram_bot/12345"

    await message_queue.run()
    result = await message_queue.enqueue_and_wait(
        bot=None,
        message=msg,
        context_memory={},
        interface_id="telegram_bot",
        skip_mention_check=True,
        timeout=2.0,
    )

    assert result == "ok"
    assert recorded
    assert recorded[0].get("interface_path") == "telegram_bot/12345"


@pytest.mark.asyncio
async def test_enqueue_recovers_grillo_activity_id_from_synthetic_message_id(
    monkeypatch,
):
    from unittest.mock import AsyncMock

    monkeypatch.setattr(message_queue, "is_user_blocked", AsyncMock(return_value=False))

    class DummyPlugin:
        def get_rate_limit(self):
            return 1000, 1, 1.0

    monkeypatch.setattr(plugin_instance, "get_plugin", lambda: DummyPlugin())
    monkeypatch.setattr(
        message_queue.rate_limit, "is_allowed", lambda *args, **kwargs: True
    )

    recorded = []

    async def fake_handle(bot, message, context_memory_or_prompt, interface=None, **kw):
        if isinstance(context_memory_or_prompt, dict):
            recorded.append(context_memory_or_prompt.copy())
        return "ok"

    monkeypatch.setattr(plugin_instance, "handle_incoming_message", fake_handle)

    msg = SimpleNamespace(
        chat_id=5551234567,
        message_id="grillo_observer_6364",
        from_user=SimpleNamespace(id=-1),
        chat=SimpleNamespace(
            type="private",
            id=5551234567,
            title=None,
            username=None,
            first_name=None,
        ),
        text="background observer",
    )

    await message_queue.run()
    result = await message_queue.enqueue_and_wait(
        bot=None,
        message=msg,
        context_memory={},
        interface_id="telegram_bot",
        skip_mention_check=True,
        timeout=2.0,
    )

    assert result == "ok"
    assert recorded
    assert recorded[0].get("activity_log_id") == 6364
    assert recorded[0].get("grillo_activity_log_id") == 6364


@pytest.mark.asyncio
async def test_supervisor_restarts_dead_consumer(monkeypatch):
    """The supervisor watchdog must restart the consumer if it dies unexpectedly.

    Regression: a per-message LLM timeout cancellation could propagate up and
    kill the consumer loop permanently. Without a supervisor the consumer never
    restarted, so every subsequent message queued silently and the VRM avatar
    stayed frozen on the ``think`` animation. The consumer must survive an
    accidental cancellation.
    """
    import asyncio

    # Speed the watchdog up so the test doesn't wait 5s.
    monkeypatch.setattr(message_queue, "_SUPERVISOR_INTERVAL_SECONDS", 0.05)

    # Make sure we start from a clean, non-shutdown state.
    await message_queue.stop()

    try:
        await message_queue.run()

        first_task = message_queue._consumer_task
        assert first_task is not None and not first_task.done()

        # Simulate an *accidental* cancellation (as a per-message timeout cancel
        # propagating up would cause). This must NOT be treated as a shutdown.
        first_task.cancel()

        # Wait for the supervisor to notice and restart the consumer.
        for _ in range(40):
            await asyncio.sleep(0.05)
            new_task = message_queue._consumer_task
            if (
                new_task is not None
                and new_task is not first_task
                and not new_task.done()
            ):
                break

        new_task = message_queue._consumer_task
        assert new_task is not None, "supervisor should have recreated the consumer"
        assert new_task is not first_task, "consumer task should be a fresh instance"
        assert not new_task.done(), "restarted consumer should be running"
    finally:
        await message_queue.stop()


@pytest.mark.asyncio
async def test_stop_prevents_consumer_restart(monkeypatch):
    """After a deliberate stop() the supervisor must not resurrect the consumer."""
    import asyncio

    monkeypatch.setattr(message_queue, "_SUPERVISOR_INTERVAL_SECONDS", 0.05)

    await message_queue.run()
    await message_queue.stop()

    # Give any lingering supervisor time to (wrongly) restart the consumer.
    await asyncio.sleep(0.2)

    assert message_queue._consumer_task is None
    assert message_queue._supervisor_task is None
    assert message_queue._shutdown_requested is True

    # Clean up for other tests: allow the queue to run again.
    message_queue._shutdown_requested = False


@pytest.mark.asyncio
async def test_drop_stale_vessel_perceptions_prunes_only_autonomous(monkeypatch):
    """Only autonomous vessel perceptions for the same world scope are pruned.

    A real player chat (``vessel_player_chat``), a perception for another world,
    and non-vessel traffic must all survive; the Queue's unfinished-task
    accounting stays consistent with the number pruned.
    """
    import heapq

    # Fresh, running-loop-bound queue for this test.
    q = message_queue._get_queue()
    # Drain anything left over from other tests.
    q._queue.clear()
    q._unfinished_tasks = 0
    q._finished.set()

    def _put(priority: int, counter: int, item: dict) -> None:
        heapq.heappush(q._queue, (priority, counter, item))
        q._unfinished_tasks += 1
        q._finished.clear()

    scope = "vessel/minecraft"
    _put(1, 1, {"interface": "vessel", "chat_id": scope})  # autonomous → prune
    _put(1, 2, {"interface": "vessel", "chat_id": scope})  # autonomous → prune
    _put(0, 3, {"interface": "vessel", "chat_id": scope, "vessel_player_chat": True})
    _put(1, 4, {"interface": "vessel", "chat_id": "vessel/other"})  # other world
    _put(2, 5, {"interface": "telegram_bot", "chat_id": scope})  # non-vessel

    message_queue._drop_stale_vessel_perceptions(scope)

    remaining = [entry[2] for entry in q._queue]
    # The two same-world autonomous perceptions are gone.
    assert len(remaining) == 3
    assert {"interface": "vessel", "chat_id": scope} not in remaining
    # The player chat, the other-world perception, and non-vessel chat survive.
    assert any(i.get("vessel_player_chat") for i in remaining)
    assert any(i.get("chat_id") == "vessel/other" for i in remaining)
    assert any(i.get("interface") == "telegram_bot" for i in remaining)
    # Unfinished-task counter decremented by exactly the number pruned.
    assert q._unfinished_tasks == 3

    # Cleanup.
    q._queue.clear()
    q._unfinished_tasks = 0
    q._finished.set()


@pytest.mark.asyncio
async def test_supersede_pending_vessel_beats_keeps_only_fresh_autonomous(monkeypatch):
    """Older autonomous beats for one world are superseded; the rest survive.

    A fresh will beat makes queued older autonomous beats for the SAME world
    stale — leaving them in the queue lets ``compact_similar_messages`` coalesce
    them into one turn with N identical prompts (the repeated-line bug). A player
    chat, a ``no_compact`` beat, another world's beat, and non-vessel traffic
    must all survive; unfinished-task accounting stays consistent.
    """
    import heapq

    q = message_queue._get_queue()
    q._queue.clear()
    q._unfinished_tasks = 0
    q._finished.set()

    def _put(priority: int, counter: int, item: dict) -> None:
        heapq.heappush(q._queue, (priority, counter, item))
        q._unfinished_tasks += 1
        q._finished.clear()

    scope = "vessel/minecraft"
    _put(1, 1, {"interface": "vessel", "chat_id": scope})  # older beat → drop
    _put(1, 2, {"interface": "vessel", "chat_id": scope})  # older beat → drop
    _put(0, 3, {"interface": "vessel", "chat_id": scope, "vessel_player_chat": True})
    _put(1, 4, {"interface": "vessel", "chat_id": scope, "no_compact": True})
    _put(1, 5, {"interface": "vessel", "chat_id": "vessel/other"})  # other world
    _put(2, 6, {"interface": "telegram_bot", "chat_id": scope})  # non-vessel

    message_queue._supersede_pending_vessel_beats(scope)

    remaining = [entry[2] for entry in q._queue]
    # The two plain same-world autonomous beats are gone.
    assert len(remaining) == 4
    assert {"interface": "vessel", "chat_id": scope} not in remaining
    # Player chat, no_compact beat, other world, and non-vessel all survive.
    assert any(i.get("vessel_player_chat") for i in remaining)
    assert any(i.get("no_compact") for i in remaining)
    assert any(i.get("chat_id") == "vessel/other" for i in remaining)
    assert any(i.get("interface") == "telegram_bot" for i in remaining)
    assert q._unfinished_tasks == 4

    # Cleanup.
    q._queue.clear()
    q._unfinished_tasks = 0
    q._finished.set()


@pytest.mark.asyncio
async def test_drop_vessel_queue_for_world_removes_all_scope_items(monkeypatch):
    """At session teardown, EVERY queued item for the world scope is dropped.

    Unlike the two prune helpers above, a closed session must also remove the
    pending player chat (there is no live embodiment left to answer it). Only
    the closing world scope is touched: another world's vessel traffic and
    non-vessel chats survive, and unfinished-task accounting stays consistent.
    """
    import heapq

    q = message_queue._get_queue()
    q._queue.clear()
    q._unfinished_tasks = 0
    q._finished.set()

    def _put(priority: int, counter: int, item: dict) -> None:
        heapq.heappush(q._queue, (priority, counter, item))
        q._unfinished_tasks += 1
        q._finished.clear()

    scope = "vessel/minecraft"
    _put(1, 1, {"interface": "vessel", "chat_id": scope})  # will beat → drop
    _put(1, 2, {"interface": "vessel", "chat_id": scope, "no_compact": True})  # drop
    _put(0, 3, {"interface": "vessel", "chat_id": scope, "vessel_player_chat": True})
    _put(1, 4, {"interface": "vessel", "chat_id": "vessel/other"})  # other world
    _put(2, 5, {"interface": "telegram_bot", "chat_id": scope})  # non-vessel

    dropped = message_queue.drop_vessel_queue_for_world(scope)

    assert dropped == 3
    remaining = [entry[2] for entry in q._queue]
    # Nothing for the closed world scope survives — not even the player chat.
    assert len(remaining) == 2
    assert not any(
        i.get("interface") == "vessel" and i.get("chat_id") == scope for i in remaining
    )
    # The other world's vessel traffic and the non-vessel chat are untouched.
    assert any(i.get("chat_id") == "vessel/other" for i in remaining)
    assert any(i.get("interface") == "telegram_bot" for i in remaining)
    assert q._unfinished_tasks == 2

    # An empty/no-match purge is a safe no-op.
    assert message_queue.drop_vessel_queue_for_world(scope) == 0

    # Cleanup.
    q._queue.clear()
    q._unfinished_tasks = 0
    q._finished.set()


@pytest.mark.asyncio
async def test_supervisor_reports_a_stalled_consumer(monkeypatch):
    """A hung generation must be visible while it hangs, not only after a restart.

    Regression (2026-09-22): the consumer task stays ALIVE for as long as a
    stalled provider request allows, so the supervisor's ``task.done()`` check
    saw a healthy consumer while every queued message sat behind it — the user's
    messages were answered by nothing and synth.log said nothing at all. The
    supervisor must now name the item that is stuck.
    """
    import asyncio

    monkeypatch.setattr(message_queue, "_SUPERVISOR_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(message_queue, "_CONSUMER_STALL_WARN_SECONDS", 0.0)

    warnings: list[str] = []
    monkeypatch.setattr(
        message_queue,
        "log_warning",
        lambda message, *args, **kwargs: warnings.append(str(message)),
    )

    await message_queue.stop()
    try:
        await message_queue.run()

        message_queue._note_item_in_flight(
            {"chat_id": 4242, "interface": "telegram_bot"}
        )

        stalled: list[str] = []
        for _ in range(40):
            await asyncio.sleep(0.05)
            stalled = [w for w in warnings if "4242" in w]
            if stalled:
                break

        assert stalled, "a stalled consumer must be reported while it is stalled"
        assert "stalled" in stalled[0]
        assert "4242" in stalled[0]

        # Once per item, not once per supervisor tick.
        await asyncio.sleep(0.2)
        assert len([w for w in warnings if "4242" in w]) == 1
    finally:
        message_queue._clear_item_in_flight()
        await message_queue.stop()


@pytest.mark.asyncio
async def test_in_flight_clearing_silences_the_stall_report(monkeypatch):
    """A turn that finished must never be reported as stalled."""

    monkeypatch.setattr(message_queue, "_SUPERVISOR_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(message_queue, "_CONSUMER_STALL_WARN_SECONDS", 0.0)

    message_queue._note_item_in_flight({"chat_id": 777, "interface": "grillo"})
    assert message_queue._stalled_item_report() is not None

    message_queue._clear_item_in_flight()
    assert message_queue._stalled_item_report() is None


# ---------------------------------------------------------------------------
# The engine the user chose after startup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_engine_is_loaded_on_demand_when_none_is_active(monkeypatch):
    """An install that picks its engine after startup must still be able to answer.

    The engine is loaded once during core init, and the first-run setup page asks the
    user for one afterwards. Without this the queue dropped every message with a log
    line: no reply, nothing on screen, and nothing the user would think to grep for.
    """
    state = {"plugin": None}
    loaded: list[str] = []

    def fake_get():
        return state["plugin"]

    async def fake_load(name, **kwargs):
        loaded.append(name)
        state["plugin"] = object()

    async def fake_active(scope=None):
        return "my-endpoint"

    monkeypatch.setattr(message_queue.plugin_instance, "get_plugin", fake_get)
    monkeypatch.setattr(message_queue.plugin_instance, "load_plugin", fake_load)
    monkeypatch.setattr("core.config.get_active_cortex_engine", fake_active)

    plugin = await message_queue.ensure_active_plugin("test")

    assert plugin is state["plugin"], "the loaded plugin must be handed back"
    assert loaded == ["my-endpoint"], "the configured engine must be loaded once"


@pytest.mark.asyncio
async def test_an_already_active_engine_is_not_reloaded(monkeypatch):
    existing = object()
    monkeypatch.setattr(message_queue.plugin_instance, "get_plugin", lambda: existing)

    async def fake_load(name, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("an active engine was reloaded")

    monkeypatch.setattr(message_queue.plugin_instance, "load_plugin", fake_load)

    assert await message_queue.ensure_active_plugin("test") is existing


@pytest.mark.asyncio
async def test_a_failed_on_demand_load_returns_nothing_instead_of_raising(monkeypatch):
    """Doubt in this helper must not take the queue down with it.

    'anthropic' is the registry default and is not available on a fresh native
    install, so this is the real failure this has to survive.
    """
    monkeypatch.setattr(message_queue.plugin_instance, "get_plugin", lambda: None)

    async def fake_active(scope=None):
        raise ValueError("Cortex engine 'anthropic' is not available")

    monkeypatch.setattr("core.config.get_active_cortex_engine", fake_active)

    assert await message_queue.ensure_active_plugin("test") is None


@pytest.mark.asyncio
async def test_an_unanswerable_message_is_reported_to_the_interface():
    """Silence was the bug: the WebUI drew no bubble and no error at all."""
    sent = []

    class _Interface:
        async def send_message(self, payload, original_message=None):
            sent.append(payload)

    message = SimpleNamespace(chat_id="webui_default")
    await message_queue.notify_unanswerable(_Interface(), message, "synth_webui")

    assert sent, "the user has to be told something"
    assert sent[0]["interface_path"] == "synth_webui/webui_default"
    assert "engine" in sent[0]["text"].lower()


@pytest.mark.asyncio
async def test_a_failing_notice_never_breaks_the_queue():
    """This runs inside the queue's failure path, so it cannot raise."""

    class _Broken:
        async def send_message(self, payload, original_message=None):
            raise RuntimeError("no websocket")

    message = SimpleNamespace(chat_id="webui_default")
    await message_queue.notify_unanswerable(_Broken(), message, "synth_webui")
