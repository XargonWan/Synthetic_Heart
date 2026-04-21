def test_chat_maximize_button_not_in_home_template():
    from pathlib import Path

    p = (
        Path(__file__).parent.parent
        / "core"
        / "webui_templates"
        / "sections"
        / "home.html"
    )
    assert p.exists(), f"Template not found: {p}"
    txt = p.read_text(encoding="utf-8")
    assert 'id="chat-maximize"' not in txt, (
        "chat-maximize button should be removed from home template (use WinBox header control)"
    )


def test_chat_maximized_css_present():
    from pathlib import Path

    p = Path(__file__).parent.parent / "res" / "synth_webui" / "js" / "main.js"
    assert p.exists(), f"Script not found: {p}"
    txt = p.read_text(encoding="utf-8")
    assert "window.SynthWindowManager" in txt, (
        "SynthWindowManager maximize path missing"
    )
    assert "function applyMaximizeConstraints(entry)" in txt, (
        "maximize constraints helper not found in main.js"
    )
    assert "onmaximize:" in txt, "WinBox maximize hook not wired in main.js"
