from pathlib import Path


def test_logs_feed_capped_and_sockets_parked_on_pagehide() -> None:
    """Overnight tabs must not grow unbounded DOM or reconnect-loop against
    frozen (bfcache) pages."""
    main = Path("res/synth_webui/js/main.js").read_text(encoding="utf-8")
    assert "logsReconnectDelay" in main
    assert "pagehide" in main
    chat = Path("res/synth_webui/js/chat-window.mjs").read_text(encoding="utf-8")
    assert "pagehide" in chat
