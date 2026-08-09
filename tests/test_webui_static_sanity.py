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


def test_chat_window_typing_indicator_does_not_clear_before_response():
    p = Path("res/synth_webui/js/chat-window.mjs")
    assert p.exists(), "chat-window.mjs missing"
    text = p.read_text(encoding="utf-8")
    assert "let _pendingSynthResponse = false" in text, (
        "chat-window.mjs should track pending synth responses before clearing typing indicators"
    )
    assert "data && data.type === 'message_ack'" in text, (
        "chat-window.mjs should handle message_ack events to track pending responses"
    )
    assert "if (_pendingSynthResponse) {" in text, (
        "chat-window.mjs should defer typing indicator removal while a synth response is pending"
    )
    assert "_checkProcessingMetaAndMaybeRemoveTypingIndicator" in text, (
        "chat-window.mjs should verify processing state before removing the typing indicator"
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


def test_try_catch_balance_in_main_js():
    """Basic sanity check: ensure every 'try {' has a corresponding 'catch' or 'finally' token.

    This test ignores occurrences that appear inside strings or single-line comments
    (e.g. template snippets) to avoid false positives from string literals.
    """
    import re

    p = Path("res/synth_webui/js/main.js")
    text = p.read_text(encoding="utf-8")

    # Remove template literals and quoted strings to avoid counting 'try {' inside them
    text_no_templates = re.sub(r"`(?:\\.|[^`])*`", "", text)
    text_no_strings = re.sub(r'(["\'])(?:\\.|(?!\1).)*\1', "", text_no_templates)

    # Strip single-line comments
    clean_lines = [ln.split("//", 1)[0] for ln in text_no_strings.splitlines()]
    clean_text = "\n".join(clean_lines)

    tries = clean_text.count("try {")
    catches = clean_text.count("catch (") + clean_text.count("finally {")
    assert tries <= catches, (
        f"Unbalanced try/catch/finally in main.js (try: {tries}, catch|finally: {catches})"
    )


def test_engine_config_save_does_not_replace_active_endpoint_cards() -> None:
    """Engine config saves must leave the active editor DOM intact."""
    text = (Path("res/synth_webui/js/main.js")).read_text(encoding="utf-8")
    start = text.index("function initEngineConfigEditor()")
    end = text.index("async function loadComponentsSummary()", start)
    editor = text[start:end]
    assert "refreshEndpoints" not in editor
