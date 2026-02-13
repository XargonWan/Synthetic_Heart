import glob
from pathlib import Path


def test_no_unexpanded_placeholders_in_res_js():
    """Ensure static JS shipped in /res does not contain template placeholders like %%PLACEHOLDER%%."""
    js_files = glob.glob(str(Path("res/synth_webui/js") / "*.js"))
    assert js_files, "No JS files found in res/synth_webui/js"
    for p in js_files:
        text = Path(p).read_text(encoding="utf-8")
        assert "%%" not in text, f"Found unexpanded placeholder in {p}"


def test_ui_helpers_has_no_export():
    p = Path("res/synth_webui/js/ui-helpers.js")
    assert p.exists(), "ui-helpers.js missing"
    text = p.read_text(encoding="utf-8")
    assert "export {" not in text, (
        "ui-helpers.js should not use ES module exports in non-module script"
    )


def test_phase_priorities_terminated():
    p = Path("res/synth_webui/js/main.js")
    assert p.exists(), "main.js missing"
    text = p.read_text(encoding="utf-8")
    # Locate the PHASE_PRIORITIES declaration and assert it ends with a semicolon
    idx = text.find("const PHASE_PRIORITIES")
    assert idx != -1, "PHASE_PRIORITIES not found in main.js"
    snippet = text[idx : idx + 200]
    assert "};" in snippet, (
        "PHASE_PRIORITIES declaration appears to be malformed (missing '};')"
    )
