"""Tests for WebUI branding and internal identifiers."""

from core.webui import BRAND_NAME, INTERNAL_CHAT_NAME, SynthWebUIInterface


def test_brand_name_is_synthetic_heart():
    assert BRAND_NAME == "Synthetic Heart"


def test_prettify_name_for_interface():
    # The prettify helper should display the user-facing name for the interface
    assert SynthWebUIInterface._prettify_name("synth_webui") == "Synthetic Heart"


def test_internal_chat_name_is_webui():
    # Ensure the internal identifier used for the LLM and action manager remains 'webui'
    assert INTERNAL_CHAT_NAME == "webui"


def test_no_subtitle_in_templates():
    # Ensure human-facing subtitle 'Synthetic Heart Command Console' was removed
    from pathlib import Path

    tpl_base = Path("core/webui_templates/base.html").read_text(encoding="utf-8")
    tpl_shell = Path("core/webui_templates/synth_webui_shell.html").read_text(
        encoding="utf-8"
    )
    assert "Synthetic Heart Command Console" not in tpl_base
    assert "Synthetic Heart Command Console" not in tpl_shell


def test_manifest_and_icons_present():
    from pathlib import Path

    tpl = Path("core/webui_templates/synth_webui_shell.html").read_text(
        encoding="utf-8"
    )
    assert "manifest.webmanifest" in tpl
    assert "/static/synth_icon_180.png" in tpl
    assert "/static/synth_icon_192.png" in tpl
    assert "/static/synth_icon_512.png" in tpl
    # Fallback to original logo should be present
    tpl_base = Path("core/webui_templates/base.html").read_text(encoding="utf-8")
    assert "synth_logo_bg.png" in tpl or "%%LOGO_URL%%" in tpl_base
