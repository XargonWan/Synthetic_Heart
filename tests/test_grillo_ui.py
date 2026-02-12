def test_grillo_ui_shows_response_label():
    """Ensure the Grillo history UI includes a 'Response' section for entries."""
    path = "core/webui_templates/sections/diary.html"
    content = open(path, "r", encoding="utf-8").read()
    assert "Response:" in content, "Diary Grillo UI should show 'Response:' label"
