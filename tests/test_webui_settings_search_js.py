"""The Settings search also matches the value a setting holds."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

SETTINGS_JS = Path(__file__).resolve().parents[1] / "res/synth_webui/js/settings.js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node missing")

ITEMS = [
    {"key": "MAX_RETRIES", "label": "Retries", "value": 500, "default": 3},
    {"key": "CACHE_SIZE", "label": "Cache", "value": None, "default": 500},
    {"key": "LOG_CHAT_ID", "label": "Log chat", "value": "telegram_bot/-100/239"},
    {"key": "SYNTH_PEERS", "label": "Peers", "value": ["telegram_bot", "discord"]},
    {"key": "TIMEOUT", "label": "Timeout", "value": 30, "default": 30},
    {
        "key": "BOT_TOKEN",
        "label": "Token",
        "value": "500:secret-500",
        "ui_type": "password",
    },
    {"key": "API_KEY", "label": "Key", "value": "500-key", "sensitive": True},
]


def _keys(query: str) -> list[str]:
    script = (
        "global.window = {}; global.document = {getElementById: () => null};"
        f"require({str(SETTINGS_JS)!r});"
        "const items = JSON.parse(process.argv[1]);"
        "const out = window.SynthSettings.filterList(items, process.argv[2]);"
        "process.stdout.write(JSON.stringify(out.map((i) => i.key)));"
    )
    res = subprocess.run(
        ["node", "-e", script, json.dumps(ITEMS), query],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(res.stdout)


def test_a_number_finds_every_setting_holding_it_including_unset_defaults() -> None:
    assert _keys("500") == ["MAX_RETRIES", "CACHE_SIZE"]


def test_a_name_finds_settings_whose_value_mentions_it() -> None:
    assert _keys("telegram_bot") == ["LOG_CHAT_ID", "SYNTH_PEERS"]


def test_secrets_are_never_matched_by_value() -> None:
    assert "BOT_TOKEN" not in _keys("secret")
    assert "API_KEY" not in _keys("500-key")


def test_names_and_several_words_still_work() -> None:
    assert _keys("cache") == ["CACHE_SIZE"]
    assert _keys("log 239") == ["LOG_CHAT_ID"]
    assert len(_keys("")) == len(ITEMS)
