import pathlib


def test_webui_emits_animation_events():
    root = pathlib.Path(__file__).resolve().parents[1]
    js = (root / 'res' / 'synth_webui' / 'js' / 'vrm-viewer.mjs').read_text(encoding='utf-8')
    assert 'synth_animation_state_updated' in js, "synth_animation_state_updated event not found in WebUI JS"
    assert 'synth_animation_lipsync_changed' in js, "synth_animation_lipsync_changed event not found in WebUI JS"
