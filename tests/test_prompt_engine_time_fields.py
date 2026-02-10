import asyncio
from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from core.prompt_engine import build_json_prompt


async def _dummy_gather(message, ctx):
    return {}


def test_build_json_prompt_includes_local_time_server_tz(monkeypatch):
    # Patch gather_static_injections to avoid unrelated behavior
    monkeypatch.setattr("core.action_parser.gather_static_injections", _dummy_gather)

    # Patch utc_to_local to return a deterministic local time (04:15)
    def fake_utc_to_local(dt):
        return datetime(2026, 2, 10, 4, 15, tzinfo=ZoneInfo("Europe/Rome"))

    monkeypatch.setattr("core.time_zone_utils.utc_to_local", fake_utc_to_local)

    message = SimpleNamespace(
        chat_id=1,
        text="hello",
        message_id=1,
        from_user=SimpleNamespace(full_name="user", username="user"),
        date=datetime(2026, 2, 10, 3, 15, tzinfo=ZoneInfo("UTC")),
    )

    result = asyncio.run(build_json_prompt(message, {}, interface_name="discord_bot"))
    payload = result["input"]["payload"]

    assert payload.get("local_time") == "04:15"
    assert payload.get("local_hour") == 4
    assert payload.get("time_of_day") == "early_morning"


def test_build_json_prompt_respects_session_meta_timezone(monkeypatch):
    monkeypatch.setattr("core.action_parser.gather_static_injections", _dummy_gather)

    # Session override to Asia/Tokyo (UTC+9)
    async def fake_get_session_meta(interface_path):
        return {"timezone": "Asia/Tokyo"}

    monkeypatch.setattr("core.session_meta.get_session_meta", fake_get_session_meta)

    message = SimpleNamespace(
        chat_id=1,
        text="hi",
        message_id=2,
        from_user=SimpleNamespace(full_name="user", username="user"),
        date=datetime(2026, 2, 10, 0, 0, tzinfo=ZoneInfo("UTC")),
        interface_path="iface:test",
    )

    result = asyncio.run(build_json_prompt(message, {}, interface_name="telegram"))
    payload = result["input"]["payload"]

    # UTC 00:00 at Asia/Tokyo should be 09:00
    assert payload.get("local_time") == "09:00"
    assert payload.get("local_hour") == 9
    assert payload.get("time_of_day") == "morning"


def test_build_json_prompt_respects_config_toggle(monkeypatch):
    monkeypatch.setattr("core.action_parser.gather_static_injections", _dummy_gather)

    # Toggle config to disable local time
    orig_get_value = getattr(__import__("core.prompt_engine", fromlist=["config_registry"]).config_registry, "get_value")

    def fake_get_value(key, default=None, value_type=None):
        if key == "INCLUDE_LOCAL_TIME_IN_PROMPTS":
            return False
        return orig_get_value(key, default, value_type=value_type)

    monkeypatch.setattr("core.prompt_engine.config_registry.get_value", fake_get_value)

    message = SimpleNamespace(
        chat_id=1,
        text="hello",
        message_id=3,
        from_user=SimpleNamespace(full_name="user", username="user"),
        date=datetime(2026, 2, 10, 3, 15, tzinfo=ZoneInfo("UTC")),
    )

    result = asyncio.run(build_json_prompt(message, {}, interface_name="discord_bot"))
    payload = result["input"]["payload"]

    assert "local_time" not in payload
    assert "time_of_day" not in payload


def test_naive_datetime_treated_as_utc(monkeypatch):
    monkeypatch.setattr("core.action_parser.gather_static_injections", _dummy_gather)

    # Session override to Europe/Rome (UTC+1)
    async def fake_get_session_meta(interface_path):
        return {"timezone": "Europe/Rome"}

    monkeypatch.setattr("core.session_meta.get_session_meta", fake_get_session_meta)

    # Provide naive datetime (no tzinfo) -- should be treated as UTC
    message = SimpleNamespace(
        chat_id=1,
        text="hi",
        message_id=4,
        from_user=SimpleNamespace(full_name="user", username="user"),
        date=datetime(2026, 2, 10, 0, 0),
        interface_path="iface:test",
    )

    result = asyncio.run(build_json_prompt(message, {}, interface_name="telegram"))
    payload = result["input"]["payload"]

    # Naive 2026-02-10 00:00 treated as UTC -> Europe/Rome should be 01:00
    assert payload.get("local_time") == "01:00"
    assert payload.get("local_hour") == 1
