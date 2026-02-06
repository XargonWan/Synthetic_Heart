def test_template_contains_llm_controls():
    components = 'core/webui_templates/sections/components.html'
    with open(components, 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'id="llm-engine-select"' in content
    assert 'id="llm-login-btn"' in content
    assert 'id="llm-engine-model"' in content
    assert 'id="llm-engine-login-state"' in content
    # Combobox adjustments: no placeholder and prominent styling
    assert 'Select LLM engine' not in content
    assert 'min-width:360px' in content or 'min-width:260px' in content

    js = 'res/synth_webui/js/main.js'
    with open(js, 'r', encoding='utf-8') as f:
        js_content = f.read()
    # Ensure details items properly append the constructed summary element
    assert 'details.appendChild(summary)' in js_content
