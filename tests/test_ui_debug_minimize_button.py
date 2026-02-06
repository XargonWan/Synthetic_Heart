def test_debug_minimize_tool_present_in_vrm_viewer():
    from pathlib import Path
    p = Path(__file__).parent.parent / 'res' / 'synth_webui' / 'js' / 'vrm-viewer.mjs'
    assert p.exists(), f"File not found: {p}"
    txt = p.read_text(encoding='utf-8')
    assert "attachHeaderTools('debug', winbox, [" in txt, "attachHeaderTools for debug not found in vrm-viewer.mjs"
    assert "title: 'Minimize'" in txt or 'title: "Minimize"' in txt, "Minimize tool not present in vrm-viewer.mjs header tools"


def test_debug_minimize_tool_present_in_index_template():
    from pathlib import Path
    p = Path(__file__).parent.parent / 'core' / 'webui_templates' / 'synth_webui_index.html'
    assert p.exists(), f"Template not found: {p}"
    txt = p.read_text(encoding='utf-8')
    assert "attachHeaderTools('debug', winbox, [" in txt, "attachHeaderTools for debug not found in synth_webui_index.html"
    assert "title: 'Minimize'" in txt or 'title: "Minimize"' in txt, "Minimize tool not present in synth_webui_index.html header tools"
