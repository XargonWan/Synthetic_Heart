"""Add External Endpoint: the providers that are only an OpenAI-compatible or a Claude API are one
button with a dropdown, the others (their own protocol) keep their own."""

from __future__ import annotations

from pathlib import Path

from core.external_endpoints.preset_registry import load_presets

ENGINES_JS = (
    Path(__file__).resolve().parent.parent / "res/synth_webui/js/engines.js"
).read_text(encoding="utf-8")


def plain() -> set[str]:
    return {
        p["provider_id"]
        for p in load_presets()
        if (p.get("category") or "llm") != "tts"
        and p["protocol"] in ("openai", "anthropic")
    }


def test_the_plain_apis_are_the_openai_compatible_ones_and_claude() -> None:
    assert plain() == {
        "openai",
        "xai_grok",
        "openrouter",
        "copilot",
        "ollama",
        "venice",
        "custom",
        "anthropic",
    }


def test_providers_with_their_own_protocol_keep_their_own_button() -> None:
    own = {
        p["provider_id"]
        for p in load_presets()
        if (p.get("category") or "llm") != "tts"
        and p["protocol"] not in ("openai", "anthropic")
    }
    assert own == {"gemini", "harmonyai"}
    assert not own & plain()


def test_the_picker_draws_one_card_for_the_plain_apis_and_a_dropdown_picks_the_provider() -> (
    None
):
    assert "function isPlainApi(preset)" in ENGINES_JS
    assert "['openai', 'anthropic'].includes(preset.protocol)" in ENGINES_JS
    assert "OpenAI / Claude API" in ENGINES_JS
    assert "ext-ep-family-select" in ENGINES_JS
    # the dropdown chooses among the real presets: nothing is re-declared in the script
    assert "selectPreset(chosen)" in ENGINES_JS
    # step 2 shows or hides the dropdown whenever a preset is selected
    assert "syncFamilySelect(preset);" in ENGINES_JS
