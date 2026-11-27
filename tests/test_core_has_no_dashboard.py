"""The Dashboard is an extension: the Core's WebUI no longer has a tab, a template or a script of it."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_the_core_no_longer_has_a_dashboard_tab() -> None:
    shell = (ROOT / "core" / "webui_templates" / "synth_webui_shell.html").read_text(
        encoding="utf-8"
    )
    assert 'data-tab="dashboard"' not in shell and "tab-dashboard" not in shell
    assert '"dashboard",' not in (ROOT / "core" / "webui.py").read_text(
        encoding="utf-8"
    )
    assert not (
        ROOT / "core" / "webui_templates" / "sections" / "dashboard.html"
    ).exists()
    assert not (ROOT / "res" / "synth_webui" / "js" / "dashboard.js").exists()
