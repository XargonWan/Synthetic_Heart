import pathlib


def test_webui_emotion_overlay_present():
    root = pathlib.Path(__file__).resolve().parents[1]
    html = (root / 'res' / 'synth_webui' / 'js' / 'vrm-viewer.mjs').read_text(encoding='utf-8')
    assert '_emotionOverlay' in html
    assert "source: 'emotion_overlay'" in html or 'source: "emotion_overlay"' in html
