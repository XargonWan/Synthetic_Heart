def test_raycast_global_is_window_scoped():
    from pathlib import Path
    files = [
        Path(__file__).parent.parent / 'res' / 'synth_webui' / 'js' / 'vrm-viewer.mjs',
        Path(__file__).parent.parent / 'core' / 'webui_templates' / 'synth_webui_index.html'
    ]
    import re
    for p in files:
        assert p.exists(), f"File not found: {p}"
        txt = p.read_text(encoding='utf-8')
        assert 'window.__synthRaycastTargets' in txt, f"window.__synthRaycastTargets not declared in {p}"
        # Ensure there are no bare assignments (not prefixed with window.) which would cause ReferenceError in modules
        m = re.search(r'(?<!window\.)\b__synthRaycastTargets\s*=', txt)
        assert not m, f"Undeclared assignment to __synthRaycastTargets found in {p}: {m.group(0) if m else ''}"
