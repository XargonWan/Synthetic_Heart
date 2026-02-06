"""Simple tests to assert mobile-friendly chat behavior is present in templates."""

from pathlib import Path


def test_mobile_auto_restore_comment_present():
    shell = Path('core/webui_templates/synth_webui_shell_clean.html').read_text(encoding='utf-8')
    home = Path('core/webui_templates/sections/home.html').read_text(encoding='utf-8')
    js = Path('res/synth_webui/js/main.js').read_text(encoding='utf-8')
    vrm = Path('res/synth_webui/js/vrm-viewer.mjs').read_text(encoding='utf-8')
    # Ensure mobile-specific behavior exists: chat toggle visible and home-stage padding removed
    assert '.chat-toggle-btn' in home
    assert '@media (max-width: 768px)' in shell
    assert '.home-stage' in shell
    # Ensure header bottom border is removed on mobile and home-stage expands to 100vh
    assert 'var(--topbar-height' in shell and 'calc(100vh -' in shell
    # Ensure mobile nav overlays above chat/toggle and archive modal and is fixed when open
    assert 'nav.main-nav' in shell and 'z-index: 10600' in shell
    assert 'nav.main-nav.open' in shell and 'position: fixed' in shell
    # Ensure JS recalculates nav position based on header bounds
    assert 'getBoundingClientRect' in js or 'header.top-bar' in js
    # Ensure any floating resize handles are accounted for when menu opens
    assert '.chat-resize-handle' in vrm
    # Ensure archive modal has mobile fullscreen behavior
    assert 'const isMobileArchive' in vrm and 'z-index: 10500' in vrm and 'left: 0;' in vrm
