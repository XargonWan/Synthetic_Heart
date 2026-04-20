import pathlib


def test_webui_blink_saccade_events_present():
    root = pathlib.Path(__file__).resolve().parents[1]
    html = (root / "core" / "webui_templates" / "synth_webui_index.html").read_text(
        encoding="utf-8"
    )
    assert "synth_animation_blink" in html, (
        "synth_animation_blink event not found in WebUI template"
    )
    assert "synth_animation_saccade" in html, (
        "synth_animation_saccade event not found in WebUI template"
    )
