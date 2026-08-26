"""Static sanity checks for the CNC Dashboard tab (shell wiring, section template, JS hygiene)."""

from pathlib import Path

SHELL = Path("core/webui_templates/synth_webui_shell.html")
DASH_SECTION = Path("core/webui_templates/sections/dashboard.html")
DASH_JS = Path("res/synth_webui/js/dashboard.js")
WEBUI = Path("core/webui.py")


def test_dashboard_nav_button_in_shell():
    text = SHELL.read_text(encoding="utf-8")
    assert '<button class="nav-btn" data-tab="dashboard"' in text
    assert 'aria-controls="tab-dashboard"' in text


def test_dashboard_panel_in_shell():
    text = SHELL.read_text(encoding="utf-8")
    assert (
        '<section class="tab-panel" id="tab-dashboard" data-tab="dashboard" role="tabpanel"></section>'
        in text
    )


def test_dashboard_section_template_has_shared_contract():
    text = DASH_SECTION.read_text(encoding="utf-8")
    for needle in (
        'id="tab-dashboard"',
        'data-tab="dashboard"',
        'id="dash-readouts"',
        'id="dash-search"',
        'id="dash-advanced"',
        'id="dash-refresh"',
        'id="dash-sidebar-count"',
        'id="dash-group-index"',
        'id="dash-rack"',
        'id="dash-toasts"',
        'src="/js/dashboard.js"',
    ):
        assert needle in text, f"missing {needle!r} in dashboard.html"


def test_dashboard_js_hygiene():
    text = DASH_JS.read_text(encoding="utf-8")
    assert "%%" not in text, "dashboard.js must not contain template placeholders"
    assert "export {" not in text, (
        "dashboard.js must be a classic script (no ES module exports)"
    )
    assert "\nimport " not in text, "dashboard.js must not use ES module imports"
    assert "window.SynthWebUI.initDashboardTab" in text


def test_dashboard_whitelisted_in_webui():
    text = WEBUI.read_text(encoding="utf-8")
    # both allowed_sections whitelists (serve_template_section + iframe_host) must include it
    assert text.count('"dashboard",') >= 2
