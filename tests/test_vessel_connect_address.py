"""``vessel_connect`` honours a server address only when the asker named it."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from plugins.rift_vessel.vessel_base import VesselActionResult
from plugins.rift_vessel.vessel_plugin import VesselPlugin, _vet_address_override

ASK = "Rekku logga in minecraft su pvp.gentoobtw.fyi"


def test_host_named_in_message_is_kept() -> None:
    out, note = _vet_address_override({"host": "pvp.gentoobtw.fyi"}, ASK)
    assert out == {"host": "pvp.gentoobtw.fyi"} and note is None


def test_server_spelling_and_inline_port_are_understood() -> None:
    text = "entra su Play.Example.com:25570 per favore"
    out, note = _vet_address_override({"server": "play.example.com:25570"}, text)
    assert out == {"host": "play.example.com", "port": 25570} and note is None


def test_host_before_sentence_punctuation_matches() -> None:
    out, _ = _vet_address_override({"host": "mc.example.org"}, "vai su mc.example.org.")
    assert out == {"host": "mc.example.org"}


@pytest.mark.parametrize(
    "host",
    ["rift.venice.ai", "pvp.gentoobtw", "gentoobtw.fyi", "evil.com", "a b", "x/../y"],
)
def test_host_not_in_message_is_dropped(host: str) -> None:
    out, note = _vet_address_override({"host": host}, ASK)
    assert out == {} and note


def test_longer_domain_does_not_vouch_for_a_shorter_one() -> None:
    out, note = _vet_address_override(
        {"host": "gentoobtw.fyi"}, "entra su pvp.gentoobtw.fyi"
    )
    assert out == {} and note


def test_port_not_in_message_is_dropped_but_host_kept() -> None:
    out, note = _vet_address_override({"host": "pvp.gentoobtw.fyi", "port": 9999}, ASK)
    assert out == {"host": "pvp.gentoobtw.fyi"} and note is None


@pytest.mark.parametrize("port", [0, 70000, "abc"])
def test_invalid_port_is_dropped(port: Any) -> None:
    out, _ = _vet_address_override(
        {"host": "pvp.gentoobtw.fyi", "port": port}, f"{ASK} 0 70000"
    )
    assert out == {"host": "pvp.gentoobtw.fyi"}


def test_no_address_means_no_override() -> None:
    assert _vet_address_override({"game": "minecraft"}, ASK) == ({}, None)


def _plugin(monkeypatch: pytest.MonkeyPatch) -> tuple[VesselPlugin, dict[str, Any]]:
    p = VesselPlugin.__new__(VesselPlugin)
    captured: dict[str, Any] = {}

    async def _connect(
        self: VesselPlugin,
        connector_name: str | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> VesselActionResult:
        captured["overrides"] = overrides
        return VesselActionResult(ok=True, detail="connected")

    monkeypatch.setattr(VesselPlugin, "connect_world", _connect)
    return p, captured


@pytest.mark.asyncio
async def test_execute_action_uses_the_original_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p, captured = _plugin(monkeypatch)
    action = {
        "type": "vessel_connect",
        "payload": {"game": "minecraft", "server": "pvp.gentoobtw.fyi"},
    }
    result = await p.execute_action(action, {}, None, SimpleNamespace(text=ASK))
    assert result["status"] == "ok"
    assert captured["overrides"] == {"host": "pvp.gentoobtw.fyi"}


@pytest.mark.asyncio
async def test_execute_action_drops_an_invented_address_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p, captured = _plugin(monkeypatch)
    action = {
        "type": "vessel_connect",
        "payload": {"game": "minecraft", "host": "rift.venice.ai"},
    }
    result = await p.execute_action(
        action, {}, None, SimpleNamespace(text="Rekku, entra su minecraft")
    )
    assert captured["overrides"] is None
    assert "rift.venice.ai" in result["message"]


@pytest.mark.asyncio
async def test_execute_action_without_a_message_never_overrides(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p, captured = _plugin(monkeypatch)
    action = {
        "type": "vessel_connect",
        "payload": {"game": "minecraft", "host": "10.0.0.9"},
    }
    await p.execute_action(action, {}, None, None)
    assert captured["overrides"] is None


@pytest.mark.asyncio
async def test_address_named_earlier_in_the_chat_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p, captured = _plugin(monkeypatch)

    async def _history(path: str, limit: int | None = None) -> list[dict[str, Any]]:
        assert path == "telegram/1/2"
        return [
            {"sender_name": "Jay", "text": ASK},
            {"sender_name": "self", "text": "mi collego a evil.example.net"},
        ]

    monkeypatch.setattr("core.chat_history_cache.load_chat_history", _history)
    ctx = {"interface_path": "telegram/1/2"}
    ok = {
        "type": "vessel_connect",
        "payload": {"game": "minecraft", "host": "pvp.gentoobtw.fyi"},
    }
    await p.execute_action(ok, ctx, None, SimpleNamespace(text="ok, riprova ora"))
    assert captured["overrides"] == {"host": "pvp.gentoobtw.fyi"}

    # What Synth itself said earlier never vouches for an address.
    own = {
        "type": "vessel_connect",
        "payload": {"game": "minecraft", "host": "evil.example.net"},
    }
    await p.execute_action(own, ctx, None, SimpleNamespace(text="ok, riprova ora"))
    assert captured["overrides"] is None
