from pathlib import Path


def test_12h_toggle_updates_visible_timestamps_without_refresh() -> None:
    """Flipping the 12-hour toggle must re-render on-screen timestamps live."""
    chat = Path("res/synth_webui/js/chat-window.mjs").read_text(encoding="utf-8")
    assert "window.refreshChatTimestamps" in chat
    assert "data-ts" in chat
    hist = Path("res/synth_webui/js/history.js").read_text(encoding="utf-8")
    assert "window.refreshHistoryTimestamps" in hist
    main = Path("res/synth_webui/js/main.js").read_text(encoding="utf-8")
    handler_start = main.index("time-12h-toggle")
    handler = main[handler_start : handler_start + 1200]
    assert "refreshChatTimestamps" in handler
    assert "refreshHistoryTimestamps" in handler
    assert ".bubble-time { position: absolute" in chat and "color: inherit" in chat, (
        "bubble timestamps should inherit the bubble text color, not pin --text-soft"
    )
