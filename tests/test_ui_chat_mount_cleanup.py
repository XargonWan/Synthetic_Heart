def test_chat_mount_clears_inline_styles():
    from pathlib import Path
    p = Path(__file__).parent.parent / 'res' / 'synth_webui' / 'js' / 'chat-window.mjs'
    assert p.exists(), f"File not found: {p}"
    txt = p.read_text(encoding='utf-8')
    # Ensure we clear common inline styles that could interfere with WinBox
    assert 'mount.style.left' in txt, 'mount.style.left clearing not present'
    assert 'mount.style.inset' in txt, 'mount.style.inset clearing not present'
    assert 'mount.classList.remove' in txt, 'mount.classList.remove cleanup not present'