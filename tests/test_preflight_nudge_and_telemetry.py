import pytest

pytest.skip(
    "Legacy preflight telemetry removed (Recon replaces it)",
    allow_module_level=True,
)

import asyncio
from types import SimpleNamespace
from datetime import datetime

import pytest

import core.prompt_engine as pe


def fake_get_value(key, default=None, value_type=None):
    if key == "MEMORY_SEARCH_PREFLIGHT":
        return True
    if key == "MEMORY_SEARCH_PREFLIGHT_MAX_RESULTS":
        return 3
    if key == "MEMORY_SEARCH_PREFLIGHT_STRATEGY":
        return "llm_action"
    return default


def test_nudge_present(monkeypatch):
    monkeypatch.setattr("core.prompt_engine.config_registry.get_value", fake_get_value)

    message = SimpleNamespace(
        text="Rekku, ti ricordi il sogno?",
        date=datetime.utcnow(),
        message_id=123,
        from_user=SimpleNamespace(id=1, full_name="Test", username="tester"),
        chat=SimpleNamespace(id=1, type="private"),
        interface_path="telegram",
    )

    prompt = asyncio.run(pe.build_json_prompt(message, {}))
    instr = prompt.get("instructions", "")
    assert (
        "consider returning" in instr.lower()
        or "consider returning a concise user-facing" in instr
    )


@pytest.mark.asyncio
async def test_preflight_telemetry_logging(monkeypatch):
    # Enable preflight and force free_db strategy
    def fake_get_value2(key, default=None, value_type=None):
        if key == "MEMORY_SEARCH_PREFLIGHT":
            return True
        if key == "MEMORY_SEARCH_PREFLIGHT_MAX_RESULTS":
            return 3
        if key == "MEMORY_SEARCH_PREFLIGHT_STRATEGY":
            return "free_db"
        return default

    monkeypatch.setattr("core.prompt_engine.config_registry.get_value", fake_get_value2)

    # Replace free_memory_search with a fake that returns some snippets
    async def fake_free(q, limit=10):
        await asyncio.sleep(0)
        return ["snippet1", "snippet2"]

    monkeypatch.setattr(pe, "free_memory_search", fake_free)

    captured = []

    def fake_log_info(msg):
        captured.append(msg)

    monkeypatch.setattr(pe, "log_info", fake_log_info)

    message = SimpleNamespace(
        text="Rekku, ti ricordi il sogno?",
        date=datetime.utcnow(),
        message_id=123,
        from_user=SimpleNamespace(id=1, full_name="Test", username="tester"),
        chat=SimpleNamespace(id=1, type="private"),
        interface_path="telegram",
    )

    _ = await pe.build_json_prompt(message, {})

    # Check that preflight telemetry log was emitted for free_db
    assert any(
        "[json_prompt][preflight_summary]" in str(m) and "strategy=free_db" in str(m)
        for m in captured
    )
