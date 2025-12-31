def test_template_contains_llm_controls():
    path = 'core/webui_templates/synth_webui_index.html'
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
    assert 'id="llm-engine-select"' in content
    assert 'id="llm-login-btn"' in content
    assert 'id="llm-engine-model"' in content
    assert 'id="llm-engine-login-state"' in content
    # Ensure details items properly append the constructed summary element
    assert 'details.appendChild(summary)' in content
    # Template should prefer Selkies HTTPS port when available
    assert 'selk && selk.https_port' in content or 'selk.https_port' in content
    # Template should substitute loopback Selkies host with page host for external access
    assert "host === '127.0.0.1'" in content and "host === 'localhost'" in content
    # Combobox adjustments: no placeholder and prominent styling
    assert 'Select LLM engine' not in content
    assert 'min-width:360px' in content or 'min-width:260px' in content
    # Template should attempt a robust open: try preferred protocol and fall back to the other
    assert 'tryOpen' in content and 'preferredProto' in content and 'otherPort' in content
