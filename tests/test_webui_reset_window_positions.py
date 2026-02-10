from pathlib import Path


def test_reset_window_positions_defined():
    path = Path("core/webui_templates/synth_webui_index.html")
    content = path.read_text(encoding="utf-8")
    assert "function resetWindowPositions" in content
    # Ensure old malformed snippet not present
    assert "raw || ek" not in content
    assert "if (btn) { btn.style.display = " not in content


def test_resize_helpers_and_defaults_present():
    path = Path("core/webui_templates/synth_webui_index.html")
    content = path.read_text(encoding="utf-8")
    assert "function createResizeHandlesForElement" in content
    assert "window.applyDefaultWindowPositions" in content
    # Ensure resize handles are created in a way that respects the draggable title bar
    assert "headerEl.style.zIndex" in content
    assert "ev.stopPropagation" in content
    # Chat title bar should also stop propagation on mousedown and get a raised z-index
    assert (
        "chatTitleBar.addEventListener('mousedown'" in content
        or 'chatTitleBar.addEventListener("mousedown"' in content
    )
    assert "chatTitleBar.style.zIndex" in content
    # Ensure global active interaction guard exists so drag/resize don't interfere
    assert "__synth_active_interaction" in content, (
        "Global interaction guard should be present to avoid drag/resize conflicts"
    )
    # Chat uses pointer events for drag parity with debug window
    assert "pointerdown'" in content or 'pointerdown"' in content
