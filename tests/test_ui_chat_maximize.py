def test_chat_maximize_button_exists_in_home_template():
    from pathlib import Path
    p = Path(__file__).parent.parent / 'core' / 'webui_templates' / 'sections' / 'home.html'
    assert p.exists(), f"Template not found: {p}"
    txt = p.read_text(encoding='utf-8')
    assert 'id="chat-maximize"' in txt, 'chat-maximize button not present in home template'


def test_chat_maximized_css_present():
    from pathlib import Path
    p = Path(__file__).parent.parent / 'core' / 'webui_templates' / 'synth_webui_index.html.tmp'
    assert p.exists(), f"Template not found: {p}"
    txt = p.read_text(encoding='utf-8')
    assert '#chat.maximized' in txt, 'CSS rule for #chat.maximized not found'