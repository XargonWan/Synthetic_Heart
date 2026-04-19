def test_debug_minimize_tool_present_in_vrm_viewer():
    """vrm-viewer should delegate to the consolidated debug module, not contain legacy inline implementation."""
    from pathlib import Path

    p = Path(__file__).parent.parent / "res" / "synth_webui" / "js" / "vrm-viewer.mjs"
    assert p.exists(), f"File not found: {p}"
    txt = p.read_text(encoding="utf-8")
    # Ensure the legacy inline implementation is not present and the module loader is used instead
    assert "attachHeaderTools('debug', winbox, [" not in txt, (
        "Legacy attachHeaderTools for debug still found in vrm-viewer.mjs"
    )
    assert (
        "/js/debug-window.mjs" in txt
        or "/res/synth_webui/js/debug-window.mjs" in txt
        or "debug-window.mjs" in txt
    ), "vrm-viewer.mjs should reference debug-window.mjs loader"


def test_debug_minimize_tool_present_in_index_template():
    """Index should load the debug module and the module should contain the Minimize tool definition."""
    from pathlib import Path

    p = (
        Path(__file__).parent.parent
        / "core"
        / "webui_templates"
        / "synth_webui_index.html"
    )
    assert p.exists(), f"Template not found: {p}"
    txt = p.read_text(encoding="utf-8")
    assert (
        "/js/debug-window.mjs" in txt
        or "/res/synth_webui/js/debug-window.mjs" in txt
        or "debug-window.mjs" in txt
    ), "Index should load debug-window module"

    # Verify the module provides the Minimize tool in its header tools
    q = Path(__file__).parent.parent / "res" / "synth_webui" / "js" / "debug-window.mjs"
    assert q.exists(), f"File not found: {q}"
    qtxt = q.read_text(encoding="utf-8")
    assert "Minimize" in qtxt and "minimize('debug')" in qtxt, (
        "Minimize tool not present in debug-window.mjs header tools"
    )
