import pytest

pytest.skip(
    "Legacy memory_search instructions removed (Recon replaces it)",
    allow_module_level=True,
)

from core.prompt_engine import build_prompt


@pytest.mark.asyncio
async def test_prompt_includes_strong_memory_search_instruction(monkeypatch):
    # Build prompt for a question that references past memory
    msgs = await build_prompt(
        "Rekku, ti ricordi il mostro austriaco?", identity_prompt=""
    )

    concatenated = "\n".join(m.get("content", "") for m in msgs)

    assert "DO NOT ANSWER DIRECTLY" in concatenated
    assert "or you are unsure" in concatenated
    assert "MUST first call the `memory_search`" in concatenated
    assert "WAIT for the `memory_search_result`" in concatenated
    assert "memory_search" in concatenated
    assert "ONLY valid JSON" in concatenated
