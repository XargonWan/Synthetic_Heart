def test_template_contains_llm_controls():
    path = 'core/webui_templates/synth_webui_index.html'
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'id="llm-engine-select"' in content
    assert 'id="llm-login-btn"' in content
    assert 'id="llm-engine-model"' in content
    assert 'id="llm-engine-login-state"' in content
