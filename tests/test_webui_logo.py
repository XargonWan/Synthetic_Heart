import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_logo_asset_present_and_template_uses_placeholder():
    # The templates should declare the logo placeholder and the static image
    # should be present in the repository image assets.
    template_path = ROOT / "core" / "webui_templates" / "synth_webui_index.html"
    template = template_path.read_text(encoding="utf-8")
    assert "%%LOGO_URL%%" in template
    assert 'rel="icon"' in template
    assert 'rel="apple-touch-icon"' in template

    asset = ROOT / "res" / "synth_webui" / "static" / "synth_logo_bg.png"
    assert asset.exists(), f"Logo asset missing at {asset}"


def test_webui_uses_static_logo_path():
    # Ensure the code uses the simple static path as the logo URL (no complex fallback).

    content = (ROOT / "core" / "webui.py").read_text(encoding="utf-8")
    assert "self.logo_url = '/static/synth_logo_bg.png'" in content
