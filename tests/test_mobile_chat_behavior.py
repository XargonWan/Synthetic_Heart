"""Simple tests to assert mobile-friendly chat behavior is present in templates."""

from pathlib import Path


def test_mobile_auto_restore_comment_present():
    tpl = Path('core/webui_templates/synth_webui_index.html').read_text(encoding='utf-8')
    # Ensure mobile-specific behavior exists: chat toggle visible and home-stage padding removed
    assert '.chat-toggle-btn' in tpl
    assert '@media (max-width: 768px)' in tpl
    assert '.home-stage' in tpl
    # Ensure header bottom border is removed on mobile and home-stage expands to 100vh
    assert 'header.top-bar { border-bottom: none' in tpl or 'header.top-bar { border-bottom: none; box-shadow: none' in tpl
    assert '.home-stage' in tpl and ('min-height: 100vh' in tpl or 'min-height: calc(100vh' in tpl)
