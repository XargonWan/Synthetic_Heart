from plugins.grillo.grillo_impl import GrilloPlugin


async def test_grillo_uses_enqueue_low_priority(monkeypatch):
    called = {}

    async def fake_enqueue_low_priority(
        bot, message, context_memory=None, interface_id=None, original_message=None
    ):
        called["args"] = {
            "bot": bot,
            "message": message,
            "context_memory": context_memory,
            "interface_id": interface_id,
        }
        return None

    import core.message_queue as message_queue

    monkeypatch.setattr(
        message_queue, "enqueue_low_priority", fake_enqueue_low_priority
    )

    plugin = GrilloPlugin()
    # simulate enqueueing a beat; call the _enqueue_with_low_priority method
    await plugin._enqueue_with_low_priority("Test beat prompt", "curiosity")

    assert "args" in called
    assert called["args"]["interface_id"] == "grillo"
    assert called["args"]["message"].text == "Test beat prompt"
    assert called["args"]["context_memory"]["allowed_action_types"] == [
        "create_personal_diary_entry"
    ]


async def test_grillo_enqueue_populates_activity_metadata(monkeypatch):
    captured = {}

    async def fake_create_activity_log(*, beat_type, prompt_text, metadata=None):
        captured["beat_type"] = beat_type
        captured["prompt_text"] = prompt_text
        captured["metadata"] = metadata
        return 321

    async def fake_enqueue_low_priority(
        bot, message, context_memory=None, interface_id=None, original_message=None
    ):
        captured["message"] = message
        return None

    import core.message_queue as message_queue

    monkeypatch.setattr(
        message_queue, "enqueue_low_priority", fake_enqueue_low_priority
    )

    plugin = GrilloPlugin()
    monkeypatch.setattr(plugin, "create_activity_log", fake_create_activity_log)

    await plugin._enqueue_with_low_priority("Test beat prompt", "curiosity")

    assert captured["beat_type"] == "curiosity"
    assert captured["metadata"]["origin"] == "grillo_scheduler"
    assert captured["metadata"]["interface"] == "grillo"
    assert captured["metadata"]["chat_id"] == "-1"
    assert captured["metadata"]["allowed_action_types"] == [
        "create_personal_diary_entry"
    ]
    assert captured["message"].message_id == "grillo_curiosity_321"


async def test_grillo_diary_consolidation_only_allows_update(monkeypatch):
    called = {}

    async def fake_enqueue_low_priority(
        bot, message, context_memory=None, interface_id=None, original_message=None
    ):
        called["context_memory"] = context_memory
        return None

    import core.message_queue as message_queue

    monkeypatch.setattr(
        message_queue, "enqueue_low_priority", fake_enqueue_low_priority
    )

    plugin = GrilloPlugin()
    await plugin._enqueue_with_low_priority("Consolidate diary", "diary_consolidation")

    assert called["context_memory"]["allowed_action_types"] == ["update_diary_entry"]
