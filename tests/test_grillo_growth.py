"""Tests for the G.R.I.L.L.O. Self-Growth weekly agent.

Covers the pure scheduling math (`_seconds_until_next_run`) and the
`run_growth_cycle` off/on/request/dry_run branches with the DB / LLM /
delivery collaborators mocked out.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

import plugins.grillo.grillo_growth as gg
from plugins.grillo.grillo_growth import GrilloGrowthPlugin


@pytest.fixture
def plugin() -> GrilloGrowthPlugin:
    """A fresh plugin instance.

    Construction registers exposed vars and calls register_plugin; both
    registries tolerate re-registration, so this is safe in tests.
    """
    return GrilloGrowthPlugin()


# ----------------------------------------------------------- scheduling math


class _FrozenClock:
    """Proxies the real ``datetime`` module attr but pins ``now()``."""

    def __init__(self, frozen: datetime) -> None:
        self._frozen = frozen

    def now(self, tz=None):  # noqa: ANN001, ANN201
        return self._frozen

    def __getattr__(self, name: str):  # noqa: ANN204
        return getattr(datetime, name)


def _seconds(
    plugin: GrilloGrowthPlugin,
    monkeypatch: pytest.MonkeyPatch,
    day: str,
    hhmm: str,
    now: datetime,
) -> int:
    """Compute _seconds_until_next_run with a frozen `now`."""
    monkeypatch.setattr(gg, "datetime", _FrozenClock(now))
    return plugin._seconds_until_next_run(day, hhmm)


def test_seconds_until_next_run_same_day_later_today(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sunday 2026-01-04 is a Sunday (weekday()==6). Now 01:00, target 03:00.
    now = datetime(2026, 1, 4, 1, 0, 0, tzinfo=timezone.utc)
    assert _seconds(plugin, monkeypatch, "Sunday", "03:00", now) == 2 * 3600


def test_seconds_until_next_run_same_day_already_passed(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Sunday 04:00, target Sunday 03:00 already passed -> next week.
    now = datetime(2026, 1, 4, 4, 0, 0, tzinfo=timezone.utc)
    assert _seconds(plugin, monkeypatch, "Sunday", "03:00", now) == 7 * 24 * 3600 - 3600


def test_seconds_until_next_run_days_ahead(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 2026-01-05 is a Monday (weekday()==0). Target Wednesday 03:00.
    now = datetime(2026, 1, 5, 3, 0, 0, tzinfo=timezone.utc)
    # 2 days ahead exactly.
    assert _seconds(plugin, monkeypatch, "Wednesday", "03:00", now) == 2 * 24 * 3600


def test_seconds_until_next_run_bad_time_defaults_to_0300(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 1, 4, 1, 0, 0, tzinfo=timezone.utc)
    # Garbage time -> falls back to 03:00, so 2h from 01:00 on Sunday.
    assert _seconds(plugin, monkeypatch, "Sunday", "not-a-time", now) == 2 * 3600


def test_seconds_until_next_run_unknown_day_defaults_to_sunday(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 1, 4, 1, 0, 0, tzinfo=timezone.utc)
    # Unknown day name -> Sunday (index 6). Now is Sunday 01:00 -> 2h.
    assert _seconds(plugin, monkeypatch, "Blursday", "03:00", now) == 2 * 3600


# --------------------------------------------------------- run_growth_cycle


@pytest.mark.asyncio
async def test_run_growth_cycle_off_without_force(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gg.config_registry, "get_value", lambda *a, **k: "off")
    result = await plugin.run_growth_cycle(force=False)
    assert result["success"] is False
    assert "off" in result["message"].lower()


@pytest.mark.asyncio
async def test_run_growth_cycle_dry_run_builds_but_does_not_apply(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = {"self_growth": "I am growing.", "likes": ["a"], "dislikes": ["b"]}

    monkeypatch.setattr(gg.config_registry, "get_value", lambda *a, **k: "on")
    monkeypatch.setattr(
        plugin, "_fetch_recent_diaries", AsyncMock(return_value="diary")
    )
    monkeypatch.setattr(gg, "get_current_growth", AsyncMock(return_value="prev"))
    monkeypatch.setattr(plugin, "_current_likes_dislikes", lambda: (["x"], ["y"]))
    monkeypatch.setattr(plugin, "_recall_memories", AsyncMock(return_value="mem"))
    monkeypatch.setattr(
        plugin, "_ask_llm_for_rewrite", AsyncMock(return_value=proposal)
    )
    save_mock = AsyncMock(return_value=99)
    monkeypatch.setattr(gg, "save_growth_state", save_mock)
    apply_mock = AsyncMock()
    monkeypatch.setattr(plugin, "_apply_likes_dislikes", apply_mock)

    result = await plugin.run_growth_cycle(dry_run=True, force=True)

    assert result["success"] is True
    assert result["proposal"] == proposal
    save_mock.assert_not_awaited()
    apply_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_growth_cycle_on_applies_state_and_likes(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = {
        "self_growth": "I have grown.",
        "likes": ["music"],
        "dislikes": ["noise"],
    }

    monkeypatch.setattr(gg.config_registry, "get_value", lambda *a, **k: "on")
    monkeypatch.setattr(
        plugin, "_fetch_recent_diaries", AsyncMock(return_value="diary")
    )
    monkeypatch.setattr(gg, "get_current_growth", AsyncMock(return_value="prev"))
    monkeypatch.setattr(plugin, "_current_likes_dislikes", lambda: ([], []))
    monkeypatch.setattr(plugin, "_recall_memories", AsyncMock(return_value="mem"))
    monkeypatch.setattr(
        plugin, "_ask_llm_for_rewrite", AsyncMock(return_value=proposal)
    )
    save_mock = AsyncMock(return_value=123)
    monkeypatch.setattr(gg, "save_growth_state", save_mock)
    apply_mock = AsyncMock()
    monkeypatch.setattr(plugin, "_apply_likes_dislikes", apply_mock)

    result = await plugin.run_growth_cycle(force=True)

    assert result["success"] is True
    assert "123" in result["message"]
    save_mock.assert_awaited_once_with(
        "I have grown.", source="weekly", likes=["music"], dislikes=["noise"]
    )
    apply_mock.assert_awaited_once_with(["music"], ["noise"])


@pytest.mark.asyncio
async def test_run_growth_cycle_request_delivers_proposal(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = {"self_growth": "growth", "likes": [], "dislikes": []}

    monkeypatch.setattr(gg.config_registry, "get_value", lambda *a, **k: "request")
    monkeypatch.setattr(
        plugin, "_fetch_recent_diaries", AsyncMock(return_value="diary")
    )
    monkeypatch.setattr(gg, "get_current_growth", AsyncMock(return_value=""))
    monkeypatch.setattr(plugin, "_current_likes_dislikes", lambda: ([], []))
    monkeypatch.setattr(plugin, "_recall_memories", AsyncMock(return_value=""))
    monkeypatch.setattr(
        plugin, "_ask_llm_for_rewrite", AsyncMock(return_value=proposal)
    )
    save_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(gg, "save_growth_state", save_mock)
    deliver_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(plugin, "_deliver_proposal", deliver_mock)

    result = await plugin.run_growth_cycle(force=True)

    assert result["success"] is True
    assert result["proposal"] == proposal
    deliver_mock.assert_awaited_once_with(proposal)
    # In request mode the state is NOT applied directly.
    save_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_growth_cycle_no_valid_rewrite(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gg.config_registry, "get_value", lambda *a, **k: "on")
    monkeypatch.setattr(
        plugin, "_fetch_recent_diaries", AsyncMock(return_value="diary")
    )
    monkeypatch.setattr(gg, "get_current_growth", AsyncMock(return_value=""))
    monkeypatch.setattr(plugin, "_current_likes_dislikes", lambda: ([], []))
    monkeypatch.setattr(plugin, "_recall_memories", AsyncMock(return_value=""))
    monkeypatch.setattr(plugin, "_ask_llm_for_rewrite", AsyncMock(return_value=None))

    result = await plugin.run_growth_cycle(force=True)
    assert result["success"] is False


# --------------------------------------------------- request approve/discard


@pytest.mark.asyncio
async def test_apply_pending_proposal_no_pending(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugin, "_load_pending_proposal", lambda: None)
    result = await plugin.apply_pending_proposal(approve=True)
    assert result["success"] is False
    assert "pending" in result["message"].lower()


@pytest.mark.asyncio
async def test_apply_pending_proposal_approve_commits_and_clears(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = {
        "self_growth": "I have grown.",
        "likes": ["music"],
        "dislikes": ["noise"],
    }
    monkeypatch.setattr(plugin, "_load_pending_proposal", lambda: proposal)
    save_mock = AsyncMock(return_value=456)
    monkeypatch.setattr(gg, "save_growth_state", save_mock)
    apply_mock = AsyncMock()
    monkeypatch.setattr(plugin, "_apply_likes_dislikes", apply_mock)
    clear_mock = AsyncMock()
    monkeypatch.setattr(plugin, "_clear_pending_proposal", clear_mock)

    result = await plugin.apply_pending_proposal(approve=True)

    assert result["success"] is True
    assert "456" in result["message"]
    save_mock.assert_awaited_once_with(
        "I have grown.", source="approved", likes=["music"], dislikes=["noise"]
    )
    apply_mock.assert_awaited_once_with(["music"], ["noise"])
    clear_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_apply_pending_proposal_reject_discards_without_commit(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = {"self_growth": "x", "likes": [], "dislikes": []}
    monkeypatch.setattr(plugin, "_load_pending_proposal", lambda: proposal)
    save_mock = AsyncMock(return_value=1)
    monkeypatch.setattr(gg, "save_growth_state", save_mock)
    clear_mock = AsyncMock()
    monkeypatch.setattr(plugin, "_clear_pending_proposal", clear_mock)

    result = await plugin.apply_pending_proposal(approve=False)

    assert result["success"] is True
    assert "discard" in result["message"].lower()
    save_mock.assert_not_awaited()
    clear_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_execute_apply_growth_proposal_action(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    apply_mock = AsyncMock(return_value={"success": True, "message": "ok"})
    monkeypatch.setattr(plugin, "apply_pending_proposal", apply_mock)

    # approve defaults to True when omitted
    await plugin.execute_action({"type": "apply_growth_proposal", "payload": {}}, {})
    apply_mock.assert_awaited_with(approve=True)

    # explicit string "false" discards
    await plugin.execute_action(
        {"type": "apply_growth_proposal", "payload": {"approve": "false"}}, {}
    )
    apply_mock.assert_awaited_with(approve=False)


@pytest.mark.asyncio
async def test_deliver_proposal_saves_pending(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = {"self_growth": "g", "likes": ["a"], "dislikes": ["b"]}

    monkeypatch.setattr(
        gg.config_registry,
        "get_value",
        lambda key, *a, **k: "telegram_bot/999" if "INTERFACE" in key else "",
    )
    save_pending_mock = AsyncMock()
    monkeypatch.setattr(plugin, "_save_pending_proposal", save_pending_mock)
    monkeypatch.setattr(plugin, "_current_likes_dislikes", lambda: ([], []))

    fake_interface = object()

    import core.core_initializer as ci

    monkeypatch.setitem(ci.INTERFACE_REGISTRY, "telegram_bot", fake_interface)
    deliver_mock = AsyncMock(return_value=True)
    import core.auto_response as ar

    monkeypatch.setattr(ar, "request_llm_delivery", deliver_mock)

    ok = await plugin._deliver_proposal(proposal)

    assert ok is True
    save_pending_mock.assert_awaited_once_with(proposal)
    deliver_mock.assert_awaited_once()


def test_get_static_injection_empty_when_no_pending(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugin, "_load_pending_proposal", lambda: None)
    assert plugin.get_static_injection() == {}


def test_get_static_injection_reminds_when_pending(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = {
        "self_growth": "I have grown a lot this week.",
        "likes": ["music"],
        "dislikes": ["noise"],
    }
    monkeypatch.setattr(plugin, "_load_pending_proposal", lambda: proposal)

    injection = plugin.get_static_injection()

    assert "self_growth_pending_proposal" in injection
    reminder = injection["self_growth_pending_proposal"]
    assert "apply_growth_proposal" in reminder
    assert "I have grown a lot this week." in reminder
    # likes/dislikes changes must be surfaced too (issue A)
    assert "music" in reminder
    assert "noise" in reminder


# ----------------------------------------------------- recon-based approval


def test_get_recon_instruction_none_when_no_pending(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugin, "_load_pending_proposal", lambda: None)
    instruction = plugin.get_recon_instruction()
    assert '"decision": "none"' in instruction


def test_get_recon_instruction_embeds_pending(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = {
        "self_growth": "I have grown a lot this week.",
        "likes": ["music"],
        "dislikes": ["noise"],
    }
    monkeypatch.setattr(plugin, "_load_pending_proposal", lambda: proposal)
    monkeypatch.setattr(plugin, "_current_likes_dislikes", lambda: ([], []))

    instruction = plugin.get_recon_instruction()

    assert "I have grown a lot this week." in instruction
    assert "approve" in instruction
    assert "reject" in instruction
    assert "music" in instruction


@pytest.mark.asyncio
async def test_parse_recon_approve_commits(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = {"self_growth": "g", "likes": [], "dislikes": []}
    monkeypatch.setattr(plugin, "_load_pending_proposal", lambda: proposal)
    monkeypatch.setattr(gg.config_registry, "get_value", lambda *a, **k: True)
    apply_mock = AsyncMock(return_value={"success": True, "message": "ok"})
    monkeypatch.setattr(plugin, "apply_pending_proposal", apply_mock)

    result = await plugin.parse_recon_response({"decision": "approve"})

    assert result == []
    apply_mock.assert_awaited_once_with(approve=True)


@pytest.mark.asyncio
async def test_parse_recon_reject_discards(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = {"self_growth": "g", "likes": [], "dislikes": []}
    monkeypatch.setattr(plugin, "_load_pending_proposal", lambda: proposal)
    monkeypatch.setattr(gg.config_registry, "get_value", lambda *a, **k: True)
    apply_mock = AsyncMock(return_value={"success": True, "message": "discarded"})
    monkeypatch.setattr(plugin, "apply_pending_proposal", apply_mock)

    result = await plugin.parse_recon_response({"decision": "reject"})

    assert result == []
    apply_mock.assert_awaited_once_with(approve=False)


@pytest.mark.asyncio
async def test_parse_recon_none_is_noop(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = {"self_growth": "g", "likes": [], "dislikes": []}
    monkeypatch.setattr(plugin, "_load_pending_proposal", lambda: proposal)
    monkeypatch.setattr(gg.config_registry, "get_value", lambda *a, **k: True)
    apply_mock = AsyncMock()
    monkeypatch.setattr(plugin, "apply_pending_proposal", apply_mock)

    result = await plugin.parse_recon_response({"decision": "none"})

    assert result == []
    apply_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_parse_recon_noop_when_nothing_pending(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugin, "_load_pending_proposal", lambda: None)
    monkeypatch.setattr(gg.config_registry, "get_value", lambda *a, **k: True)
    apply_mock = AsyncMock()
    monkeypatch.setattr(plugin, "apply_pending_proposal", apply_mock)

    result = await plugin.parse_recon_response({"decision": "approve"})

    assert result == []
    apply_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_parse_recon_disabled_is_noop(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = {"self_growth": "g", "likes": [], "dislikes": []}
    monkeypatch.setattr(plugin, "_load_pending_proposal", lambda: proposal)
    monkeypatch.setattr(gg.config_registry, "get_value", lambda *a, **k: False)
    apply_mock = AsyncMock()
    monkeypatch.setattr(plugin, "apply_pending_proposal", apply_mock)

    result = await plugin.parse_recon_response({"decision": "approve"})

    assert result == []
    apply_mock.assert_not_awaited()
