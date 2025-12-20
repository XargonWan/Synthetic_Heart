import pathlib


def test_webui_emits_animation_events():
    root = pathlib.Path(__file__).resolve().parents[1]
    html = (root / 'core' / 'webui_templates' / 'synth_webui_index.html').read_text(encoding='utf-8')
    assert 'synth_animation_state_updated' in html, "synth_animation_state_updated event not found in WebUI template"
    assert 'synth_animation_lipsync_changed' in html, "synth_animation_lipsync_changed event not found in WebUI template"
