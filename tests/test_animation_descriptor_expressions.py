import json
from pathlib import Path


def test_thinking_descriptor_has_eyes_closed_expression():
    root = Path(__file__).resolve().parents[1]
    desc_path = root / 'skins' / 'Rei' / 'animations' / 'think' / 'Thinking.fbx.json'
    assert desc_path.exists(), f"Descriptor {desc_path} must exist"
    desc = json.loads(desc_path.read_text(encoding='utf-8'))
    assert 'expressions' in desc and isinstance(desc['expressions'], list)
    found = False
    for ex in desc['expressions']:
        if ex.get('start_frame') == 20:
            targets = ex.get('targets') or {}
            if 'eyes_closed' in targets and float(targets['eyes_closed']) == 1.0:
                found = True
                break
    assert found, 'Expected an expression with start_frame=20 and targets.eyes_closed=1.0'
