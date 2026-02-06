def test_chat_header_tools_added_in_main():
    from pathlib import Path
    p = Path(__file__).parent.parent / 'res' / 'synth_webui' / 'js' / 'main.js'
    assert p.exists(), f"File not found: {p}"
    txt = p.read_text(encoding='utf-8')
    assert "attachHeaderTools('chat', winbox," in txt, "attachHeaderTools for chat not found in main.js"
    assert "title: 'Reset position'" in txt or 'title: "Reset position"' in txt, "Reset position tool not present for chat"


def test_reset_button_uses_synth_window_manager():
    from pathlib import Path
    p = Path(__file__).parent.parent / 'res' / 'synth_webui' / 'js' / 'main.js'
    txt = p.read_text(encoding='utf-8')
    assert 'reset-window-positions' in txt, 'reset button not present in main.js'
    assert 'window.SynthWindowManager' in txt and ("resetWindowPositions" in txt or "SynthWindowManager.restore('chat')" in txt or 'SynthWindowManager.ensureChatWindow' in txt), 'reset handler does not use SynthWindowManager or resetWindowPositions'