def test_rei_persona_has_emotions_list():
    import pathlib
    import json

    root = pathlib.Path(__file__).resolve().parents[1]
    data = json.loads(
        (root / "skins" / "Rei" / "persona.json").read_text(encoding="utf-8")
    )
    assert "emotions" in data, 'Rei persona.json should define an "emotions" mapping'
    # New format: emotions is an object mapping emotion_name -> blendshape targets
    emot_map = data["emotions"]
    assert isinstance(emot_map, dict), "emotions must be a mapping of emotion->targets"
    keys = set(emot_map.keys())
    expected = set(
        ["happy", "sad", "angry", "fear", "disgust", "surprised", "neutral", "relaxed"]
    )
    assert expected.issubset(keys), (
        "persona.emotions keys must include canonical set (Ekman 6 + neutral + relaxed)"
    )
