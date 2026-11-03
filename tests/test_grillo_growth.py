"""Tests for the G.R.I.L.L.O. Self-Growth weekly agent.

Covers the pure scheduling math (`_seconds_until_next_run`) and the
`run_growth_cycle` off/on/request/dry_run branches with the DB / LLM /
delivery collaborators mocked out.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
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


# ----------------------------------------------------- recon-based revision


def test_get_recon_instruction_includes_revise_when_pending(
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

    assert "revise" in instruction
    assert "approve" in instruction
    assert "reject" in instruction


@pytest.mark.asyncio
async def test_parse_recon_revise_regenerates(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = {"self_growth": "g", "likes": [], "dislikes": []}
    monkeypatch.setattr(plugin, "_load_pending_proposal", lambda: proposal)
    monkeypatch.setattr(gg.config_registry, "get_value", lambda *a, **k: True)
    revise_mock = AsyncMock(return_value={"success": True, "message": "ok"})
    monkeypatch.setattr(plugin, "_revise_pending_proposal", revise_mock)

    result = await plugin.parse_recon_response(
        {"decision": "revise"}, text="rendila più concreta, meno astratta"
    )

    assert result == []
    revise_mock.assert_awaited_once_with("rendila più concreta, meno astratta")


@pytest.mark.asyncio
async def test_parse_recon_revise_noop_when_nothing_pending(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugin, "_load_pending_proposal", lambda: None)
    monkeypatch.setattr(gg.config_registry, "get_value", lambda *a, **k: True)
    revise_mock = AsyncMock()
    monkeypatch.setattr(plugin, "_revise_pending_proposal", revise_mock)

    result = await plugin.parse_recon_response(
        {"decision": "revise"}, text="cambia questa parte"
    )

    assert result == []
    revise_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_revise_pending_proposal_overwrites_and_redelivers(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_proposal = {"self_growth": "old", "likes": [], "dislikes": []}
    new_proposal = {"self_growth": "new revised", "likes": ["a"], "dislikes": ["b"]}

    monkeypatch.setattr(plugin, "_load_pending_proposal", lambda: old_proposal)
    monkeypatch.setattr(
        plugin, "_fetch_recent_diaries", AsyncMock(return_value="diary")
    )
    monkeypatch.setattr(gg, "get_current_growth", AsyncMock(return_value="prev"))
    monkeypatch.setattr(plugin, "_current_likes_dislikes", lambda: ([], []))
    monkeypatch.setattr(plugin, "_recall_memories", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        plugin, "_ask_llm_for_revise", AsyncMock(return_value=new_proposal)
    )
    save_mock = AsyncMock()
    monkeypatch.setattr(plugin, "_save_pending_proposal", save_mock)
    deliver_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(plugin, "_deliver_proposal", deliver_mock)

    result = await plugin._revise_pending_proposal("rendila più concreta")

    assert result["success"] is True
    # The pending slot is overwritten with the new proposal (old discarded).
    save_mock.assert_awaited_once_with(new_proposal)
    deliver_mock.assert_awaited_once_with(new_proposal)
    assert result["proposal"] == new_proposal


@pytest.mark.asyncio
async def test_revise_pending_proposal_noop_when_nothing_pending(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(plugin, "_load_pending_proposal", lambda: None)
    revise_mock = AsyncMock()
    monkeypatch.setattr(plugin, "_ask_llm_for_revise", revise_mock)
    save_mock = AsyncMock()
    monkeypatch.setattr(plugin, "_save_pending_proposal", save_mock)
    deliver_mock = AsyncMock()
    monkeypatch.setattr(plugin, "_deliver_proposal", deliver_mock)

    result = await plugin._revise_pending_proposal("cambia questa parte")

    assert result["success"] is False
    revise_mock.assert_not_awaited()
    save_mock.assert_not_awaited()
    deliver_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_revise_pending_proposal_fails_when_llm_returns_none(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_proposal = {"self_growth": "old", "likes": [], "dislikes": []}
    monkeypatch.setattr(plugin, "_load_pending_proposal", lambda: old_proposal)
    monkeypatch.setattr(
        plugin, "_fetch_recent_diaries", AsyncMock(return_value="diary")
    )
    monkeypatch.setattr(gg, "get_current_growth", AsyncMock(return_value="prev"))
    monkeypatch.setattr(plugin, "_current_likes_dislikes", lambda: ([], []))
    monkeypatch.setattr(plugin, "_recall_memories", AsyncMock(return_value=[]))
    monkeypatch.setattr(plugin, "_ask_llm_for_revise", AsyncMock(return_value=None))
    save_mock = AsyncMock()
    monkeypatch.setattr(plugin, "_save_pending_proposal", save_mock)
    deliver_mock = AsyncMock()
    monkeypatch.setattr(plugin, "_deliver_proposal", deliver_mock)

    result = await plugin._revise_pending_proposal("rendila più concreta")

    assert result["success"] is False
    save_mock.assert_not_awaited()
    deliver_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_execute_action_revise_triggers_revision(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    revise_mock = AsyncMock(return_value={"success": True, "message": "ok"})
    monkeypatch.setattr(plugin, "_revise_pending_proposal", revise_mock)

    await plugin.execute_action(
        {
            "type": "apply_growth_proposal",
            "payload": {"revise": True, "feedback": "rendila più concreta"},
        },
        {},
    )

    revise_mock.assert_awaited_once_with("rendila più concreta")


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


# ------------------------------------------------------------- retractions


def _false_claim() -> Any:
    from core.soul.models import RetractedClaim

    return RetractedClaim(
        id="rtr-1",
        subject="trip to Ca' Senz'Autobus",
        claim="Jay and the persona are going to Ca' Senz'Autobus",
        reason="it does not exist",
        status="active",
    )


def _wire_rewrite(
    plugin: GrilloGrowthPlugin,
    monkeypatch: pytest.MonkeyPatch,
    *,
    claims: list[Any],
    replies: list[dict[str, Any] | None],
) -> list[dict[str, Any]]:
    """Mock everything around ``_ask_llm_for_rewrite``; return the prompts seen."""
    seen: list[dict[str, Any]] = []

    async def _generate(engine: Any, prompt: dict[str, Any]) -> dict[str, Any] | None:
        seen.append(prompt)
        return replies[min(len(seen), len(replies)) - 1]

    class _Registry:
        def get_engine(self, name: str) -> object:
            return object()

    monkeypatch.setattr(
        gg,
        "get_current_growth",
        AsyncMock(
            return_value=(
                "You helped Jay with InsightFace. You are preparing for your trip "
                "to Ca' Senz'Autobus with Jay."
            )
        ),
    )
    monkeypatch.setattr(plugin, "_current_likes_dislikes", lambda: (["a"], ["b"]))
    monkeypatch.setattr(plugin, "_get_persona_injection", AsyncMock(return_value="p"))
    monkeypatch.setattr(gg, "get_active_cortex_engine", AsyncMock(return_value="x"))
    monkeypatch.setattr(gg, "get_cortex_registry", lambda: _Registry())
    monkeypatch.setattr(gg, "load_claims", AsyncMock(return_value=claims))
    monkeypatch.setattr(plugin, "_generate_and_parse", _generate)
    monkeypatch.setattr(plugin, "_reads_as_abstract", lambda *a, **k: False)
    return seen


_CLEAN = {"self_growth": "You fixed InsightFace with Jay.", "likes": [], "dislikes": []}
_DIRTY = {
    "self_growth": "You are going to Ca' Senz'Autobus trip with Jay.",
    "likes": [],
    "dislikes": [],
}


@pytest.mark.asyncio
async def test_rewrite_strips_and_announces_a_retracted_claim(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _wire_rewrite(plugin, monkeypatch, claims=[_false_claim()], replies=[_CLEAN])

    result = await plugin._ask_llm_for_rewrite(
        "2026-10-01: pizza\n2026-10-02: packing for the trip to Ca' Senz'Autobus",
        ["went to the lake", "trip to Ca' Senz'Autobus is planned"],
    )

    assert result is not None and result["self_growth"] == _CLEAN["self_growth"]
    payload = seen[0]["input"]["payload"]
    assert "FALSE" in payload["known_false_claims"]
    assert "Senz" not in payload["current_self_growth"]
    assert "InsightFace" in payload["current_self_growth"]
    assert "Senz" not in payload["past_week_diary"]
    assert "pizza" in payload["past_week_diary"]
    assert "Senz" not in payload["recalled_memories"]
    assert "known_false_claims" in seen[0]["instructions"]


@pytest.mark.asyncio
async def test_rewrite_retries_once_when_the_reply_restates_the_claim(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _wire_rewrite(
        plugin, monkeypatch, claims=[_false_claim()], replies=[_DIRTY, _CLEAN]
    )
    result = await plugin._ask_llm_for_rewrite("diary", [])
    assert result is not None and result["self_growth"] == _CLEAN["self_growth"]
    assert len(seen) == 2
    assert "restated a statement" in seen[1]["instructions"]


@pytest.mark.asyncio
async def test_rewrite_is_skipped_when_the_retry_still_restates_the_claim(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    _wire_rewrite(plugin, monkeypatch, claims=[_false_claim()], replies=[_DIRTY])
    assert await plugin._ask_llm_for_rewrite("diary", []) is None


@pytest.mark.asyncio
async def test_rewrite_without_claims_is_unchanged(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _wire_rewrite(plugin, monkeypatch, claims=[], replies=[_DIRTY])
    result = await plugin._ask_llm_for_rewrite("diary", [])
    assert result is not None
    assert "known_false_claims" not in seen[0]["input"]["payload"]
    assert "known_false_claims" not in seen[0]["instructions"]


@pytest.mark.asyncio
async def test_pending_proposal_is_discarded_when_it_restates_a_claim(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        plugin,
        "_load_pending_proposal",
        lambda: {"self_growth": "You wait for the trip to Ca' Senz'Autobus."},
    )
    clear = AsyncMock()
    monkeypatch.setattr(plugin, "_clear_pending_proposal", clear)
    assert await plugin.discard_pending_proposal_matching([_false_claim()]) is True
    clear.assert_awaited_once()

    clear.reset_mock()
    monkeypatch.setattr(
        plugin, "_load_pending_proposal", lambda: {"self_growth": "You like tea."}
    )
    assert await plugin.discard_pending_proposal_matching([_false_claim()]) is False
    clear.assert_not_awaited()


# ------------------------------------------------- missed-slot catch-up


def test_previous_slot_is_most_recent_occurrence(plugin: GrilloGrowthPlugin) -> None:
    # Tuesday 2026-11-03 10:00 -> the slot that matters is Sunday 2026-11-01 03:00.
    now = datetime(2026, 11, 3, 10, 0, 0, tzinfo=timezone.utc)
    assert plugin._previous_slot("Sunday", "03:00", now) == datetime(
        2026, 11, 1, 3, 0, 0, tzinfo=timezone.utc
    )


def test_previous_slot_at_exact_slot_time(plugin: GrilloGrowthPlugin) -> None:
    now = datetime(2026, 11, 1, 3, 0, 0, tzinfo=timezone.utc)
    assert plugin._previous_slot("Sunday", "03:00", now) == now


@pytest.mark.asyncio
async def test_last_run_prefers_persisted_timestamp(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        gg.config_registry, "get_value", lambda *a, **k: "2026-10-25T03:01:00+00:00"
    )
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    assert await plugin._last_run_at(now) == datetime(
        2026, 10, 25, 3, 1, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_last_run_falls_back_to_latest_growth_state(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gg.config_registry, "get_value", lambda *a, **k: "")
    monkeypatch.setattr(
        gg,
        "get_growth_history",
        AsyncMock(return_value=[{"created_at": "2026-10-18T03:00:00+00:00"}]),
    )
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    assert await plugin._last_run_at(now) == datetime(
        2026, 10, 18, 3, 0, tzinfo=timezone.utc
    )


@pytest.mark.asyncio
async def test_last_run_seed_is_persisted_on_fresh_install(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gg.config_registry, "get_value", lambda *a, **k: "")
    monkeypatch.setattr(gg, "get_growth_history", AsyncMock(return_value=[]))
    record = AsyncMock()
    monkeypatch.setattr(plugin, "_record_run", record)
    now = datetime(2026, 11, 3, tzinfo=timezone.utc)
    assert await plugin._last_run_at(now) == now
    record.assert_awaited_once()


@pytest.mark.asyncio
async def test_successful_cycle_records_the_run(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = {"self_growth": "You grew.", "likes": [], "dislikes": []}
    monkeypatch.setattr(gg.config_registry, "get_value", lambda *a, **k: "on")
    monkeypatch.setattr(plugin, "_fetch_recent_diaries", AsyncMock(return_value=""))
    monkeypatch.setattr(gg, "get_current_growth", AsyncMock(return_value=""))
    monkeypatch.setattr(plugin, "_current_likes_dislikes", lambda: ([], []))
    monkeypatch.setattr(plugin, "_recall_memories", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        plugin, "_ask_llm_for_rewrite", AsyncMock(return_value=proposal)
    )
    monkeypatch.setattr(gg, "save_growth_state", AsyncMock(return_value=7))
    monkeypatch.setattr(plugin, "_apply_likes_dislikes", AsyncMock())
    record = AsyncMock()
    monkeypatch.setattr(plugin, "_record_run", record)

    await plugin.run_growth_cycle(force=True)
    record.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_cycle_does_not_record_the_run(
    plugin: GrilloGrowthPlugin, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(gg.config_registry, "get_value", lambda *a, **k: "on")
    monkeypatch.setattr(plugin, "_fetch_recent_diaries", AsyncMock(return_value=""))
    monkeypatch.setattr(gg, "get_current_growth", AsyncMock(return_value=""))
    monkeypatch.setattr(plugin, "_current_likes_dislikes", lambda: ([], []))
    monkeypatch.setattr(plugin, "_recall_memories", AsyncMock(return_value=[]))
    monkeypatch.setattr(plugin, "_ask_llm_for_rewrite", AsyncMock(return_value=None))
    record = AsyncMock()
    monkeypatch.setattr(plugin, "_record_run", record)

    result = await plugin.run_growth_cycle(force=True)
    assert result["success"] is False
    record.assert_not_awaited()
