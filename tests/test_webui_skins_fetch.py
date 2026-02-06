from pathlib import Path

def test_fetch_skins_endpoint_present():
    tpl = Path('res/synth_webui/js/skins-ui.js').read_text(encoding='utf-8')
    assert "fetch('/api/skins')" in tpl or 'fetch("/api/skins")' in tpl
