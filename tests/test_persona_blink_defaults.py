import json
from pathlib import Path


def test_rei_persona_has_blink_defaults():
    p = Path('skins/Rei/persona.json').read_text(encoding='utf-8')
    data = json.loads(p)
    defaults = data.get('defaults', {})
    blink = defaults.get('blink')
    assert blink is not None, 'blink defaults missing in Rei persona'
    assert 'close_ms' in blink and 'hold_ms' in blink and 'open_ms' in blink, 'blink timing fields missing'
    assert isinstance(blink.get('intensity', None), (int, float)), 'blink intensity missing or invalid'
