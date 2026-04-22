def test_chat_maximize_button_not_in_home_template():
    from pathlib import Path

    p = (
        Path(__file__).parent.parent
        / "core"
        / "webui_templates"
        / "sections"
        / "home.html"
    )
    assert p.exists(), f"Template not found: {p}"
    txt = p.read_text(encoding="utf-8")
    assert 'id="chat-maximize"' not in txt, (
        "chat-maximize button should be removed from home template (use WinBox header control)"
    )


def test_chat_maximized_css_present():
    from pathlib import Path

    p = Path(__file__).parent.parent / "res" / "synth_webui" / "js" / "main.js"
    assert p.exists(), f"Script not found: {p}"
    txt = p.read_text(encoding="utf-8")
    assert "window.SynthWindowManager" in txt, (
        "SynthWindowManager maximize path missing"
    )
    assert "function applyMaximizeConstraints(entry)" in txt, (
        "maximize constraints helper not found in main.js"
    )
    assert "onmaximize:" in txt, "WinBox maximize hook not wired in main.js"


def test_chat_restore_reuses_saved_normal_rect():
    from pathlib import Path

    p = Path(__file__).parent.parent / "res" / "synth_webui" / "js" / "main.js"
    assert p.exists(), f"Script not found: {p}"
    txt = p.read_text(encoding="utf-8")
    assert "function setNormalRect(entry, rect)" in txt, (
        "Normal-rect persistence helper not found in main.js"
    )
    assert "function applyNormalRect(entry)" in txt, (
        "Normal-rect restore helper not found in main.js"
    )
    assert "captureNormalRect(entry);" in txt and "entry.winbox.maximize();" in txt, (
        "Maximize path should capture the normal rect before maximizing"
    )
    assert (
        "const storedRect = (entry.winbox.max || entry.minimized) ? entry.lastNormalRect : null;"
        in txt
    ), (
        "State persistence should keep the non-maximized rect when windows are maximized or minimized"
    )


def test_chat_window_state_persists_without_session_id_suffix():
    from pathlib import Path

    p = Path(__file__).parent.parent / "res" / "synth_webui" / "js" / "main.js"
    assert p.exists(), f"Script not found: {p}"
    txt = p.read_text(encoding="utf-8")
    assert "function getWindowStorageKeys(id)" in txt, (
        "Window storage helper not found in main.js"
    )
    assert "const stableStateKey = `${CHAT_WINDOW_STATE_KEY}-${id}`;" in txt, (
        "Window state should persist on a stable per-window key"
    )
    assert "const stableRectKey = `${CHAT_RECT_KEY}-${id}`;" in txt, (
        "Window rect should persist on a stable per-window key"
    )
    assert "localStorage.setItem(stableStateKey, state);" in txt, (
        "State persistence should always write the stable key"
    )
    assert "localStorage.setItem(stableRectKey, JSON.stringify(payload));" in txt, (
        "Rect persistence should always write the stable key"
    )


def test_native_winbox_maximize_captures_normal_rect():
    from pathlib import Path

    p = Path(__file__).parent.parent / "res" / "synth_webui" / "js" / "main.js"
    assert p.exists(), f"Script not found: {p}"
    txt = p.read_text(encoding="utf-8")
    assert "const nativeMaximize = typeof winbox.maximize === 'function' ? winbox.maximize.bind(winbox) : null;" in txt, (
        "Native WinBox maximize should be wrapped before use"
    )
    assert "winbox.maximize = function(...args) {" in txt, (
        "WinBox maximize wrapper not found in main.js"
    )
    assert "if (!this.max && !this.min) captureNormalRect(entry);" in txt, (
        "Native WinBox maximize should capture the normal rect before maximizing"
    )


def test_restore_from_max_skips_transient_capture_overwrite():
    from pathlib import Path

    p = Path(__file__).parent.parent / "res" / "synth_webui" / "js" / "main.js"
    assert p.exists(), f"Script not found: {p}"
    txt = p.read_text(encoding="utf-8")
    assert "restoringFromMax: false" in txt, (
        "Window entries should track restore-from-max transitions"
    )
    assert "entry.restoringFromMax = !!this.max;" in txt, (
        "Native restore should mark when it is leaving the maximized state"
    )
    assert "if (!entry.restoringFromMax && !this.max && !this.min) try { captureNormalRect(entry); } catch (e) { /* ignore */ }" in txt, (
        "Move/resize handlers should not overwrite lastNormalRect during restore-from-max"
    )
    assert "if (entry.restoringFromMax && !this.max && !this.min) applyNormalRect(entry);" in txt, (
        "onrestore should only reapply the saved normal rect for restore-from-max transitions"
    )
