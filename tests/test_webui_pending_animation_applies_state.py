import pathlib


def test_webui_pending_animation_applies_state():
    root = pathlib.Path(__file__).resolve().parents[1]
    html = (root / 'core' / 'webui_templates' / 'synth_webui_index.html').read_text(encoding='utf-8')
    # Ensure pending VRM load processing forwards a rich animation_state to the handler
    assert 'const st = last.animation_state || window.__synth_current_animation_state || null' in html, 'pending animation_state forwarding not present in template'
    assert 'animationHandler.applyAnimationState(st)' in html or 'animationHandler.applyAnimationState(last.animation_state)' in html, 'applyAnimationState call not present for pending commands'


def test_webui_pending_skip_still_applies_state():
    root = pathlib.Path(__file__).resolve().parents[1]
    html = (root / 'core' / 'webui_templates' / 'synth_webui_index.html').read_text(encoding='utf-8')
    assert 'Pending command matches started state; skipping' in html
    assert 'last.animation_state || window.__synth_last_rich_animation_state' in html
    assert 'applyAnimationState(st)' in html
