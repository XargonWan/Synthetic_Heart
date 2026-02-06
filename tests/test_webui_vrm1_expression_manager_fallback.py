import pathlib


def test_webui_supports_vrm1_expression_manager_fallback():
    root = pathlib.Path(__file__).resolve().parents[1]
    html = (root / 'res' / 'synth_webui' / 'js' / 'vrm-viewer.mjs').read_text(encoding='utf-8')
    assert 'expressionManager' in html
    assert '_getFaceController' in html
    assert "kind: 'vrm1'" in html
