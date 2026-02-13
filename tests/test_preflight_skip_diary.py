import pytest

pytest.skip(
    "Legacy preflight diary skip removed (Recon replaces it)",
    allow_module_level=True,
)

from core import action_parser as ap


@pytest.mark.asyncio
async def test_skip_diary_entry_during_preflight(monkeypatch):
    # Force validate_action to succeed so we reach the preflight check
    monkeypatch.setattr(
        ap, "validate_action", lambda action, context, original_message: (True, [])
    )

    action = {"type": "create_personal_diary_entry", "payload": {"content": "test"}}
    ctx = {"preflight": True}

    res = await ap.run_action(action, ctx, bot=None, original_message=None)
    assert isinstance(res, dict)
    assert res.get("skipped_due_to_preflight") is True
    assert res.get("action") == action
