import pathlib


def test_webui_blink_saccade_events_present():
    root = pathlib.Path(__file__).resolve().parents[1]
    js = (root / 'res' / 'synth_webui' / 'js' / 'vrm-viewer.mjs').read_text(encoding='utf-8')
    assert 'synth_animation_blink' in js, "synth_animation_blink event not found in WebUI JS"
    assert 'synth_animation_saccade' in js, "synth_animation_saccade event not found in WebUI JS"
