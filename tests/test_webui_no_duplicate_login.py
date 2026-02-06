def test_no_duplicate_login_button_in_template():
    path = 'core/webui_templates/sections/components.html'
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    # Only the main selector login button should be present
    assert 'id="llm-login-btn"' in content
    assert 'id="llm-card-login-btn"' not in content
