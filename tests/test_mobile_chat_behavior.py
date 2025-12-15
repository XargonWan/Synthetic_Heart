"""Simple tests to assert mobile-friendly chat behavior is present in templates."""

from pathlib import Path


def test_mobile_auto_restore_comment_present():
    tpl = Path('core/webui_templates/synth_webui_index.html').read_text(encoding='utf-8')
    # Ensure mobile-specific behavior exists: chat toggle visible and home-stage padding removed
    assert '.chat-toggle-btn' in tpl
    assert '@media (max-width: 768px)' in tpl
    assert '.home-stage' in tpl
    # Ensure header bottom border is removed on mobile and home-stage expands to 100vh
    assert 'border-bottom: none' in tpl or 'border-bottom: 1px solid' not in tpl
    assert 'var(--topbar-height' in tpl and 'calc(100vh -' in tpl
    # Ensure mobile nav overlays above chat/toggle and archive modal and is fixed when open
    assert 'nav.main-nav' in tpl and 'z-index: 10600' in tpl
    # When open, it should be positioned fixed and with very high z-index
    assert 'nav.main-nav.open' in tpl and 'position: fixed' in tpl and 'z-index: 20000' in tpl
    # Ensure nav has a close button available on mobile
    assert '.nav-close' in tpl
    # Ensure JS recalculates nav position based on header bounds
    assert 'getBoundingClientRect' in tpl or 'header.top-bar' in tpl
    # Ensure archive modal has mobile fullscreen behavior
    assert 'const isMobileArchive' in tpl and 'z-index: 10500' in tpl and 'left: 0;' in tpl
