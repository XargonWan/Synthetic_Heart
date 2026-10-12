from pathlib import Path


def test_vrm_viewer_eye_fallback_is_reusable() -> None:
    """White/open eyes until reload means the iris texture degraded AFTER load.

    The eye-material fallback must be a named function (not inline in loadVRM)
    so it can re-run periodically and when the tab becomes visible again.
    """
    p = Path("res/synth_webui/js/vrm-viewer.mjs")
    assert p.exists(), "vrm-viewer.mjs missing"
    text = p.read_text(encoding="utf-8")
    assert "function applyEyeMaterialFallback" in text, (
        "vrm-viewer.mjs should define applyEyeMaterialFallback so the eye "
        "fallback can re-run after load, not just once in loadVRM"
    )
    assert "window.applyEyeMaterialFallback = applyEyeMaterialFallback" in text, (
        "vrm-viewer.mjs should expose applyEyeMaterialFallback on window "
        "so it can be re-triggered on demand"
    )
