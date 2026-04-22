def test_chat_header_tools_not_injected_in_main():
    from pathlib import Path

    p = Path(__file__).parent.parent / "res" / "synth_webui" / "js" / "main.js"
    assert p.exists(), f"File not found: {p}"
    txt = p.read_text(encoding="utf-8")
    assert "attachHeaderTools('chat', winbox, [{" not in txt, (
        "Chat should rely on native WinBox controls only"
    )
    assert "Reset position" not in txt, (
        "Reset position should not be injected into the chat titlebar"
    )


def test_reset_button_uses_synth_window_manager():
    from pathlib import Path

    p = Path(__file__).parent.parent / "res" / "synth_webui" / "js" / "main.js"
    txt = p.read_text(encoding="utf-8")
    assert "reset-window-positions" in txt, "reset button not present in main.js"
    assert "window.SynthWindowManager" in txt and (
        "resetWindowPositions" in txt
        or "SynthWindowManager.restore('chat')" in txt
        or "SynthWindowManager.ensureChatWindow" in txt
    ), "reset handler does not use SynthWindowManager or resetWindowPositions"
