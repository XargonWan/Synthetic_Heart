import pytest
from types import SimpleNamespace
from core.prompt_engine import build_prompt_request
from core.transport_layer import run_corrector_middleware


@pytest.mark.asyncio
async def test_build_prompt_request_excludes_persona_for_internal_tasks(monkeypatch):
    dummy_persona = "PERSONA: Rei, cute VRM avatar"

    async def mock_gather_static_injections(message, context_memory):
        return {"persona": dummy_persona}

    monkeypatch.setattr(
        "core.action_parser.gather_static_injections", mock_gather_static_injections
    )
    monkeypatch.setattr(
        "core.prompt_engine.load_json_instructions", lambda: "RESPONSE INSTRUCTIONS"
    )

    # 1. Test external interface (e.g. telegram_bot) -> persona should be prepended
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

    # 2. Test internal maintenance task (e.g. diary_merge interface) -> persona should be skipped
    msg_merge = SimpleNamespace(
        interface_path="diary_merge/-1",
        text="merge data",
        message_id=43,
        date=SimpleNamespace(isoformat=lambda: "2026-05-27T00:00:00Z"),
    )
    res_merge = await build_prompt_request(
        message=msg_merge, context_memory={}, interface_name="diary_merge"
    )
    assert "=== CRITICAL SYSTEM IDENTITY ===" not in res_merge["instructions"]
    assert dummy_persona not in res_merge["instructions"]

    # 3. Test internal maintenance beat (e.g. diary_consolidation beat) -> persona should be skipped
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
    assert "=== CRITICAL SYSTEM IDENTITY ===" not in res_consol["instructions"]
    assert dummy_persona not in res_consol["instructions"]


@pytest.mark.asyncio
async def test_corrector_middleware_handles_safety_refusals():
    refusal_1 = "你好，我无法给到相关内容。"
    refusal_2 = "I cannot fulfill this request as it violates safety guidelines."

    res1 = await run_corrector_middleware(text=refusal_1, context={})
    res2 = await run_corrector_middleware(text=refusal_2, context={})

    assert res1 == '{"actions": []}'
    assert res2 == '{"actions": []}'

    res_normal = await run_corrector_middleware(
        text="normal invalid json text", context={}
    )
    assert res_normal is None or res_normal != '{"actions": []}'


@pytest.mark.asyncio
async def test_corrector_middleware_does_not_swallow_legit_replies():
    """Generic phrases like 'unable to' or 'not allowed' in an ordinary reply
    must not trigger the refusal short-circuit (which would drop the reply)."""
    legit_texts = [
        "I was unable to find that song in the library, want me to retry?",
        "Guests are not allowed in that channel, but I can invite them elsewhere.",
        "The upload failed because the file format is unable to be parsed.",
    ]
    for text in legit_texts:
        res = await run_corrector_middleware(text=text, context={})
        assert res != '{"actions": []}', f"Legit reply swallowed as refusal: {text!r}"
