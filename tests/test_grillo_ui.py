def test_grillo_ui_shows_response_label():
    """Ensure the Grillo history UI includes a 'Response' section for entries."""
    placeholder = open(
        "core/webui_templates/sections/diary.html", "r", encoding="utf-8"
    ).read()
    content = open("res/synth_webui/js/history.js", "r", encoding="utf-8").read()

    assert "sections/history.html" in placeholder
    assert "Response:" in content, "Diary Grillo UI should show 'Response:' label"
