from pathlib import Path


def test_reset_window_positions_defined():
    content = Path("res/synth_webui/js/ui-helpers.js").read_text(encoding="utf-8")
    assert "function resetWindowPositions" in content
    # Ensure old malformed snippet not present
    assert "raw || ek" not in content
    assert "if (btn) { btn.style.display = " not in content


def test_resize_helpers_and_defaults_present():
    helper_content = Path("res/synth_webui/js/ui-helpers.js").read_text(
        encoding="utf-8"
    )
    viewer_content = Path("res/synth_webui/js/vrm-viewer.mjs").read_text(
        encoding="utf-8"
    )
    assert "function createResizeHandlesForElement" in viewer_content
    assert "window.applyDefaultWindowPositions" in helper_content
    # Ensure resize handles are created in a way that respects the draggable title bar
    assert "headerEl.style.zIndex" in viewer_content
    assert "ev.stopPropagation" in viewer_content
    # Chat title bar should also stop propagation on mousedown and get a raised z-index
    assert (
        "chatTitleBar.addEventListener('mousedown'" in viewer_content
        or 'chatTitleBar.addEventListener("mousedown"' in viewer_content
    )
    assert "chatTitleBar.style.zIndex" in viewer_content
    # Ensure global active interaction guard exists so drag/resize don't interfere
    assert "__synth_active_interaction" in viewer_content, (
        "Global interaction guard should be present to avoid drag/resize conflicts"
    )
    # Chat uses pointer events for drag parity with debug window
    assert "pointerdown'" in viewer_content or 'pointerdown"' in viewer_content
