def test_webui_template_exposes_emotion_presets():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    html = (root / 'res' / 'synth_webui' / 'js' / 'vrm-viewer.mjs').read_text(encoding='utf-8')
    assert 'window.__synth_emotion_face_presets' in html, 'WebUI template should expose persona emotion presets'
    assert 'window.__synth_persona_emotions_list' in html, 'WebUI template should expose persona emotions list'
