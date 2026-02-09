def test_webui_face_keys_include_common_morphs():
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    html = (root / 'core' / 'webui_templates' / 'synth_webui_index.html').read_text(encoding='utf-8')

    # Ensure common expressive morphs are exposed to the debug UI
    for key in ['eyes_wide', 'mouth_open', 'mouth_frown', 'brow_down', 'mouth_smile', 'eyes_smile', 'eye_look_left']:
        assert key in html, f"Expected '{key}' to be present in WebUI face keys"
    # Also ensure the additional requested morphs are present (granular face morphs)
    for key in ['eye_blink_left','eye_blink_right','eye_look_down','eye_look_right','eye_look_up','eyes_closed','eyesClosed','eyes_smile','mouth_O','mouth_smile']:
        assert key in html, f"Expected '{key}' to be present in WebUI face keys"

    # Ensure composite names do not appear in the 'extra' aliases array used to seed face keys
    extra_block_start = html.find("const extra = [")
    assert extra_block_start != -1
    extra_block_end = html.find("];", extra_block_start)
    assert extra_block_end != -1
    extra_block = html[extra_block_start:extra_block_end]

    # Emotions like 'sad' must not be seeded as *facial morph* aliases (they belong in Feelings)
    assert 'sad' not in extra_block.lower(), "Emotion 'sad' should not be present in facial morph aliases (extra list)"

    # Ensure composite feeling metrics are explicitly declared and NOT part of the initial 'extra' face keys
    assert "const compositeMetrics = new Set(['valence','arousal','stress','calm','relaxed','neutral']);" in html
    for composite in ['valence','arousal','stress','calm']:
        assert composite not in extra_block.lower(), f"Composite metric '{composite}' should not be present in the extra face aliases"

    # Ensure exclusion is case-insensitive and persona emotion list is consulted
    assert 'personaEmotionKeysLower' in html, 'Persona emotion exclusion should be present in getFaceKeys()'

    # Ensure the client will consult persona emotion presets when resolving emotion keys (Feelings->face)
    assert 'window.__synth_emotion_face_presets' in html, 'WebUI should reference persona emotion presets for mapping feelings to blendshapes'
