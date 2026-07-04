from core import chat_attention


def test_engagement_disabled_by_default(monkeypatch):
    """With the window at 0 (default), mark_engaged is a no-op and is_engaged is always False."""
    monkeypatch.setattr(
        "core.chat_attention.config_registry.get_value",
        lambda k, d=None, **kwargs: 0,
    )
    chat_attention.ENGAGEMENT_STATE.clear()

    chat_attention.mark_engaged(111)

    assert chat_attention.ENGAGEMENT_STATE == {}
    assert chat_attention.is_engaged(111) is False


def test_engagement_marks_and_is_engaged_within_window(monkeypatch):
    monkeypatch.setattr(
        "core.chat_attention.config_registry.get_value",
        lambda k, d=None, **kwargs: 60,
    )
    chat_attention.ENGAGEMENT_STATE.clear()

    chat_attention.mark_engaged(222)

    assert chat_attention.is_engaged(222) is True
    # A different, never-engaged scope must not be affected.
    assert chat_attention.is_engaged(333) is False


def test_engagement_expires_after_window(monkeypatch):
    monkeypatch.setattr(
        "core.chat_attention.config_registry.get_value",
        lambda k, d=None, **kwargs: 30,
    )
    chat_attention.ENGAGEMENT_STATE.clear()

    # Simulate a trigger 100 seconds ago -- well past a 30s window.
    import time

    chat_attention.ENGAGEMENT_STATE[444] = time.time() - 100

    assert chat_attention.is_engaged(444) is False
