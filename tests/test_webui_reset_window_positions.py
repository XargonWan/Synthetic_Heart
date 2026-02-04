from pathlib import Path


def test_reset_window_positions_defined():
    root = Path('res/synth_webui/js')
    ui = (root / 'ui-helpers.js').read_text(encoding='utf-8')
    assert 'function resetWindowPositions' in ui
    # Ensure old malformed snippet not present
    assert 'raw || ek' not in ui


def test_resize_helpers_and_defaults_present():
    root = Path('res/synth_webui/js')
    mjs = (root / 'vrm-viewer.mjs').read_text(encoding='utf-8')
    ui = (root / 'ui-helpers.js').read_text(encoding='utf-8')

    # createResizeHandlesForElement is provided by vrm-viewer.mjs
    assert 'function createResizeHandlesForElement' in mjs
    # applyDefaultWindowPositions now lives in ui-helpers.js
    assert 'function applyDefaultWindowPositions' in ui
    # Ensure resize handles are created in a way that respects the draggable title bar
    assert 'headerEl.style.zIndex' in mjs
    assert 'ev.stopPropagation' in mjs
    # Chat title bar should also stop propagation on mousedown and get a raised z-index
    assert "chatTitleBar.addEventListener('mousedown'" in mjs or 'chatTitleBar.addEventListener("mousedown"' in mjs
    assert "chatTitleBar.style.zIndex" in mjs
    # Ensure global active interaction guard exists so drag/resize don't interfere
    assert '__synth_active_interaction' in mjs, 'Global interaction guard should be present to avoid drag/resize conflicts'
    # Chat uses pointer events for drag parity with debug window
    assert "pointerdown'" in mjs or 'pointerdown"' in mjs
