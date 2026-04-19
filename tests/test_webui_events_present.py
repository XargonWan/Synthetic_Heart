import pathlib


def test_webui_emits_animation_events():
    root = pathlib.Path(__file__).resolve().parents[1]
    module_text = (root / "res" / "synth_webui" / "js" / "vrm-viewer.mjs").read_text(
        encoding="utf-8"
    )
    assert "synth_animation_state_updated" in module_text, (
        "synth_animation_state_updated event not found in active VRM viewer module"
    )
    assert "synth_animation_lipsync_changed" in module_text, (
        "synth_animation_lipsync_changed event not found in active VRM viewer module"
    )
