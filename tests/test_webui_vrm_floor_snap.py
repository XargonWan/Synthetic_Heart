from pathlib import Path


def test_vrm_rest_hips_height_cached_and_reused() -> None:
    """Retarget must scale from rest-pose hips height, not the live animated pose."""
    viewer = Path("res/synth_webui/js/vrm-viewer.mjs").read_text(encoding="utf-8")
    assert "restHipsHeight" in viewer, (
        "vrm-viewer.mjs should cache rest-pose hips height at model load"
    )
    loader = Path("res/synth_webui/js/loadMixamoAnimation.js").read_text(
        encoding="utf-8"
    )
    assert "userData?.restHipsHeight" in loader, (
        "loadMixamoAnimation.js should prefer the cached rest hips height"
    )


def test_vrm_model_snapped_to_floor_at_load() -> None:
    """Models authored below y=0 must be lifted so feet start on the floor."""
    viewer = Path("res/synth_webui/js/vrm-viewer.mjs").read_text(encoding="utf-8")
    assert "setFromObject" in viewer, (
        "vrm-viewer.mjs should measure the rest-pose bounds at model load"
    )
    assert "position.y +=" in viewer, (
        "vrm-viewer.mjs should lift models whose lowest point is under the floor"
    )
