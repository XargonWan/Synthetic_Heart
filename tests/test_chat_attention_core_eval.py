from core.chat_attention import evaluate_triggers


def test_evaluate_triggers_prefers_sleep(monkeypatch):
    # Configure both wake and sleep triggers where the text contains both
    monkeypatch.setattr(
        "core.chat_attention.config_registry.get_value",
        lambda k, d=None, **kwargs: (
            "bye synth"
            if k == "CHAT_SLEEP_COMMANDS"
            else ("hey synth" if k == "CHAT_WAKE_COMMANDS" else "")
        ),
    )

    text = "hey synth... bye synth"
    should_sleep, should_wake, is_cmd = evaluate_triggers(text)
    assert should_sleep is True
    assert should_wake is False  # sleep has priority
    assert is_cmd is True


def test_empty_triggers_return_false(monkeypatch):
    monkeypatch.setattr(
        "core.chat_attention.config_registry.get_value",
        lambda k, d=None, **kwargs: (
            "" if k in ("CHAT_SLEEP_COMMANDS", "CHAT_WAKE_COMMANDS") else ""
        ),
    )
    should_sleep, should_wake, is_cmd = evaluate_triggers("bye synth")
    assert should_sleep is False
    assert should_wake is False
    assert is_cmd is False
