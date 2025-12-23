from pathlib import Path


def test_blink_js_contains_timing_and_state():
    tpl = Path('core/webui_templates/synth_webui_index.html').read_text(encoding='utf-8')
    assert '_blinkInProgress' in tpl
    assert '_blinkCloseMs' in tpl
    assert "_blinkState = 'open'" in tpl or '"_blinkState = \"open\""' in tpl
    assert '_forceOpenEyes' in tpl
    assert "prevAction === 'think'" in tpl or "prevAction === \"think\"" in tpl
    assert '+/-15%' in tpl or '* 0.3' in tpl
    # New logic: ensure eyes locking decision and smooth reset are present
    assert '_resetEyesSmoothly' in tpl
    assert "const isLong = (typeof duration === 'number' && duration > 300)" in tpl or 'isLong' in tpl
    assert "_setEyesState" in tpl
