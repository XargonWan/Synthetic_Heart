from pathlib import Path

def test_fetch_skins_endpoint_present():
    tpl = Path('core/webui_templates/synth_webui_index.html').read_text(encoding='utf-8')
    assert "fetch('/api/skins')" in tpl or 'fetch("/api/skins")' in tpl
