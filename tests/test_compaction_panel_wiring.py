"""The Settings tab's "Memory Compaction" card is actually wired to its endpoint.

The card is the only way to run the nightly pass on demand, so it is worth a test that
the markup and the script agree on the ids and the URL: a rename on one side alone would
leave a button that silently does nothing.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SETTINGS_HTML = REPO / "core" / "webui_templates" / "sections" / "settings.html"
MAIN_JS = REPO / "res" / "synth_webui" / "js" / "main.js"


def test_the_settings_tab_carries_the_card() -> None:
    html = SETTINGS_HTML.read_text(encoding="utf-8")

    assert "<h2>Memory Compaction</h2>" in html
    assert 'id="run-compaction-now"' in html
    assert 'id="compaction-status"' in html
    # It sits next to the re-distillation card, which is where the operator looks.
    assert html.index("Memory Re-distillation") < html.index("Memory Compaction")


def test_the_button_calls_the_endpoint() -> None:
    js = MAIN_JS.read_text(encoding="utf-8")

    assert "run-compaction-now" in js
    assert "compaction-status" in js
    assert "'/api/grillo/compaction'" in js
    assert "method: 'POST'" in js


def test_the_panel_says_what_the_pass_did() -> None:
    """The summary line is the point: a pass that skipped everything must not look quiet."""
    js = MAIN_JS.read_text(encoding="utf-8")

    assert "skipped as already compacted" in js
    assert "already have a memory" in js
