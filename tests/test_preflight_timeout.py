import pytest

pytest.skip(
    "Legacy preflight timeout removed (Recon replaces it)",
    allow_module_level=True,
)

import asyncio
from types import SimpleNamespace
from datetime import datetime

from core.prompt_engine import build_json_prompt


@pytest.mark.asyncio
async def test_preflight_timeout(monkeypatch):
    # Force preflight to be enabled and strategy to llm_action
    def fake_get_value(key, default=None, value_type=None):
        if key == "MEMORY_SEARCH_PREFLIGHT":
            return True
        if key == "MEMORY_SEARCH_PREFLIGHT_MAX_RESULTS":
            return 3
        if key == "MEMORY_SEARCH_PREFLIGHT_STRATEGY":
            return "llm_action"
        if key == "MEMORY_SEARCH_PREFLIGHT_TIMEOUT":
            return 1  # 1 second timeout
        return default

    monkeypatch.setattr("core.prompt_engine.config_registry.get_value", fake_get_value)

    async def slow_llm_preflight(
        *, text: str, interface_name: str | None, original_message, max_results: int
    ):
        await asyncio.sleep(2)
        return ["This should not be included"]

    monkeypatch.setattr(
        "core.prompt_engine.llm_memory_search_preflight", slow_llm_preflight
    )

    # Ensure fallback free_memory_search does not return anything
    async def empty_free_search(q, limit=10):
        await asyncio.sleep(0)
        return []

    monkeypatch.setattr("core.prompt_engine.free_memory_search", empty_free_search)

    message = SimpleNamespace(
        text="Rekku, ti ricordi il mostro austriaco?",
        date=datetime.utcnow(),
        message_id=123,
        from_user=SimpleNamespace(id=1, full_name="Test", username="tester"),
        chat=SimpleNamespace(id=1, type="private"),
        interface_path="telegram",
    )

    # The call should complete quickly (not block for 2s) and not include the slow preflight snippet
    prompt = await build_json_prompt(message, {})
    serialized = str(prompt)
    assert "This should not be included" not in serialized
