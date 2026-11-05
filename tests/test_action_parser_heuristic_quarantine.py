import pytest

from types import SimpleNamespace

from core.action_parser import run_actions


@pytest.mark.asyncio
async def test_heuristic_recovered_action_quarantined(monkeypatch):
    # Create an LLM-originated message
    msg = SimpleNamespace()
    msg.from_cortex = True

    action = {
        "type": "send_message",
        "payload": {"text": "Hello", "interface_path": "telegram_bot/-1"},
        "metadata": {"heuristic_recovery": True},
    }

    # Ensure synth autonomy allows execution path to reach heuristic check
    monkeypatch.setattr(
        "core.action_parser.config_registry.get_value",
        lambda k, d=None, **kwargs: "autonomous" if k == "SYNTH_AUTONOMY_MODE" else d,
    )

    result = await run_actions([action], {}, None, msg)

    # Action should not be executed; it should be treated as failed/proposal due to heuristic flag
    assert isinstance(result, dict)
    assert result.get("processed") == []
    assert result.get("failed_actions") and len(result.get("failed_actions")) == 1
    errs = result.get("failed_actions")[0].get("errors", [])
    assert any("heuristic" in e.lower() or "quarantine" in e.lower() for e in errs)
