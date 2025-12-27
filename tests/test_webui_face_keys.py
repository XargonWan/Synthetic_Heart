def test_webui_face_keys_include_common_morphs():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    html = (root / 'core' / 'webui_templates' / 'synth_webui_index.html').read_text(encoding='utf-8')

    # Ensure common expressive morphs are exposed to the debug UI
    for key in ['eyes_wide', 'mouth_open', 'mouth_frown', 'brow_down', 'mouth_smile', 'eyes_smile', 'eye_look_left']:
        assert key in html, f"Expected '{key}' to be present in WebUI face keys"
