"""Simple tests to assert mobile-friendly chat behavior is present in templates."""

from pathlib import Path


def test_responsive_nav_and_layout_present():
    shell = Path('core/webui_templates/synth_webui_shell.html').read_text(encoding='utf-8')
    home = Path('core/webui_templates/sections/home.html').read_text(encoding='utf-8')
    js = Path('res/synth_webui/js/main.js').read_text(encoding='utf-8')
    vrm = Path('res/synth_webui/js/vrm-viewer.mjs').read_text(encoding='utf-8')
    # Ensure responsive behavior exists: chat toggle visible and home-stage canvas sizing present
    assert '.chat-toggle-btn' in home
    assert '.home-stage' in shell
    assert 'var(--topbar-height' in shell and 'calc(100vh -' in shell
    # Ensure navigation is always visible (no hamburger toggle)
    assert 'nav.main-nav' in shell
    assert 'hamburger' not in shell
    # Ensure JS recalculates nav/topbar height based on header bounds
    assert 'getBoundingClientRect' in js or 'header.top-bar' in js
    # Ensure any floating resize handles are accounted for in VRM viewer
    assert '.chat-resize-handle' in vrm
