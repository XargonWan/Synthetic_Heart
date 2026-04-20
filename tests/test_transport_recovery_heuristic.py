from core.transport_layer import _attempt_recover_actions_from_text


def test_recovery_skips_without_interface_path():
    original_text = '... "type":"message_telegram_bot" "payload": {"text": "Hello without interface"} ...'
    found = {"actions": []}
    metadata = {}

    _attempt_recover_actions_from_text(original_text, found, metadata)

    # Should not recover because interface_path is required for message_* heuristics
    assert found["actions"] == []


def test_recovery_with_valid_interface_path_creates_metadata(monkeypatch):
    # Ensure the core_initializer declares message_telegram_bot as available

    monkeypatch.setattr(
        "core.core_initializer.core_initializer.actions_block",
        {"available_actions": {"message_telegram_bot": {}}},
    )

    original_text = 'noise "type":"message_telegram_bot" "payload": {"text": "Ciao","interface_path": "telegram_bot/-123"} tail'
    found = {"actions": []}
    metadata = {}

    _attempt_recover_actions_from_text(original_text, found, metadata)

    assert len(found["actions"]) == 1
    recovered = found["actions"][0]
    assert recovered["type"] == "message_telegram_bot"
    assert recovered["payload"]["text"] == "Ciao"
    assert recovered["payload"]["interface_path"] == "telegram_bot/-123"
    assert recovered.get("metadata", {}).get("heuristic_recovery") is True
