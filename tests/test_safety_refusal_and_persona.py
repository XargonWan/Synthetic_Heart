import pytest
from types import SimpleNamespace
from core.prompt_engine import build_prompt_request


@pytest.mark.asyncio
async def test_build_prompt_request_includes_persona_by_default(monkeypatch):
    """Persona is included in all prompts by default (USE_PERSONA_IN_SYSTEM_PROMPTS=True)."""
    dummy_persona = "PERSONA: Rei, cute VRM avatar"

    async def mock_gather_static_injections(message, context_memory):
        return {"persona": dummy_persona}

    monkeypatch.setattr(
        "core.action_parser.gather_static_injections", mock_gather_static_injections
    )
    monkeypatch.setattr(
        "core.prompt_engine.load_json_instructions", lambda: "RESPONSE INSTRUCTIONS"
    )

    # 1. External interface -> persona should be prepended
    msg = SimpleNamespace(
        interface_path="telegram_bot/123",
        text="hello",
        message_id=42,
        date=SimpleNamespace(isoformat=lambda: "2026-05-27T00:00:00Z"),
    )
    res = await build_prompt_request(
        message=msg, context_memory={}, interface_name="telegram_bot"
    )
    assert "=== CRITICAL SYSTEM IDENTITY ===" in res["instructions"]
    assert dummy_persona in res["instructions"]

    # 2. Internal maintenance task (diary_merge) -> persona should still be included by default
    msg_merge = SimpleNamespace(
        interface_path="diary_merge/-1",
        text="merge data",
        message_id=43,
        date=SimpleNamespace(isoformat=lambda: "2026-05-27T00:00:00Z"),
    )
    res_merge = await build_prompt_request(
        message=msg_merge, context_memory={}, interface_name="diary_merge"
    )
    assert "=== CRITICAL SYSTEM IDENTITY ===" in res_merge["instructions"]
    assert dummy_persona in res_merge["instructions"]

    # 3. Internal maintenance beat (diary_consolidation) -> persona should still be included by default
    msg_consol = SimpleNamespace(
        interface_path="grillo/-1",
        text="consolidate data",
        message_id=44,
        date=SimpleNamespace(isoformat=lambda: "2026-05-27T00:00:00Z"),
        grillo_beat=True,
        beat_type="diary_consolidation",
    )
    res_consol = await build_prompt_request(
        message=msg_consol,
        context_memory={"beat_type": "diary_consolidation"},
        interface_name="grillo",
    )
    assert "=== CRITICAL SYSTEM IDENTITY ===" in res_consol["instructions"]
    assert dummy_persona in res_consol["instructions"]


# Note: Safety refusal short-circuit was removed from core/transport_layer.py
# because keyword-based detection is unreliable across languages and can match
# legitimate sentences. The corrector now handles all invalid JSON normally.
