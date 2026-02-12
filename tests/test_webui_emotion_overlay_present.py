import pathlib


def test_webui_emotion_overlay_present():
    root = pathlib.Path(__file__).resolve().parents[1]
    html = (root / "core" / "webui_templates" / "synth_webui_index.html").read_text(
        encoding="utf-8"
    )
    assert "_emotionOverlay" in html
    assert "source: 'emotion_overlay'" in html or 'source: "emotion_overlay"' in html
