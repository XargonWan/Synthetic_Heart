import pathlib
import json


def test_rei_persona_has_blendshape_map():
    root = pathlib.Path(__file__).resolve().parents[1]
    persona_path = root / 'skins' / 'Rei' / 'persona.json'
    assert persona_path.exists(), 'Rei persona.json must exist for face mapping tests'
    data = json.loads(persona_path.read_text(encoding='utf-8'))
    assert 'blendshape_map' in data, 'Rei persona.json should define a blendshape_map'
