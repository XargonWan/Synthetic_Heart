"""Setting things down, holding them out, putting one thing into another, following.

What the model is offered for these (the schemas), what is said alongside an action, and what the connector keeps of
them. The mod is a fake socket that answers every action; the body's own behaviour is the mod's tests' business.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from plugins.rift_vessel.retroxr.retroxr import RetroXRConnector


class AnsweringSocket:
    """A mod that says ok to every action frame."""

    def __init__(self) -> None:
        self.conn: RetroXRConnector | None = None
        self.sent: list[dict[str, Any]] = []

    async def send_json(self, payload: dict[str, Any]) -> None:
        self.sent.append(payload)
        if payload.get("type") == "action" and self.conn is not None:
            conn = self.conn
            asyncio.get_event_loop().call_soon(
                lambda: asyncio.ensure_future(
                    conn.handle_frame(
                        {
                            "type": "action_result",
                            "id": payload["id"],
                            "ok": True,
                            "detail": "",
                            "data": {},
                        }
                    )
                )
            )

    async def close(self) -> None:
        pass

    def actions(self, verb: str) -> list[dict[str, Any]]:
        return [
            f
            for f in self.sent
            if f.get("type") == "action" and f.get("action") == verb
        ]


async def _connected(verbs: list[str]) -> tuple[RetroXRConnector, AnsweringSocket]:
    conn = RetroXRConnector()
    ws = AnsweringSocket()
    ws.conn = conn
    conn.attach_socket(ws, {"room": "den", "verbs": verbs})
    assert await conn.connect({}, lambda e: None) is True
    return conn, ws


def _plugin_in(monkeypatch: pytest.MonkeyPatch, world: str, connector: Any):
    """A VesselPlugin embodied in ``world``, whose connector is ``connector``."""
    from plugins.rift_vessel.vessel_plugin import VesselPlugin

    monkeypatch.setattr(VesselPlugin, "refresh_config", lambda self: None)
    monkeypatch.setattr(
        VesselPlugin, "_import_builtin_connectors", staticmethod(lambda: None)
    )
    monkeypatch.setattr(
        "plugins.rift_vessel.vessel_plugin.register_plugin", lambda name, inst: None
    )
    plugin = VesselPlugin()
    monkeypatch.setattr(
        "plugins.rift_vessel.vessel_plugin.VESSEL_REGISTRY.load_connector",
        lambda name: connector,
    )
    monkeypatch.setattr(VesselPlugin, "_connected_world", lambda self: world)
    return plugin


def _offered(plugin: Any, world: str) -> dict[str, dict[str, Any]]:
    prefix = f"vessel_{world}_"
    return {
        n.removeprefix(prefix): a
        for n, a in plugin.get_supported_actions().items()
        if n.startswith(prefix)
    }


# -- what she is offered ------------------------------------------------------


async def test_combine_is_offered_when_the_mod_declares_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, _ = await _connected(
        ["say", "combine", "hand_over", "put_down", "follow", "unfollow", "operate"]
    )
    offered = _offered(_plugin_in(monkeypatch, "retroxr", conn), "retroxr")
    assert {"combine", "hand_over", "put_down", "follow", "unfollow", "operate"} <= set(
        offered
    )
    combine = offered["combine"]
    assert combine["required_fields"] == ["target"] and combine["optional_fields"] == [
        "item",
        "how",
    ]


async def test_combine_is_not_offered_when_the_mod_does_not_declare_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, _ = await _connected(["say", "hand_over", "follow"])
    assert "combine" not in _offered(
        _plugin_in(monkeypatch, "retroxr", conn), "retroxr"
    )


def test_a_world_that_never_heard_of_combine_is_not_offered_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A world that restricts nothing (core_verbs None, Minecraft's case) still gets the historic set, without the new verb."""
    stub = SimpleNamespace(
        core_verbs=None, get_world_actions=lambda: {}, core_verb_descriptions={}
    )
    offered = _offered(_plugin_in(monkeypatch, "minecraft", stub), "minecraft")
    assert "use" in offered and "attack" in offered, (
        "the historic core set is still there"
    )
    assert "combine" not in offered


def test_a_world_can_opt_in_to_combine(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = SimpleNamespace(
        core_verbs=frozenset({"say", "combine"}),
        get_world_actions=lambda: {},
        core_verb_descriptions={},
    )
    offered = _offered(_plugin_in(monkeypatch, "somegame", stub), "somegame")
    assert set(offered) >= {"say", "combine"} and "use" not in offered


async def test_follow_is_described_as_it_is_in_a_room(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The generic text promises a target and 'fails if there is nothing to follow'; in a room it is always the player."""
    conn, _ = await _connected(["say", "follow", "unfollow"])
    offered = _offered(_plugin_in(monkeypatch, "retroxr", conn), "retroxr")
    assert "No target is needed" in offered["follow"]["description"]
    assert "retroxr room" in offered["follow"]["description"], "the world is filled in"
    assert "Fails if there is nothing" not in offered["follow"]["description"]


async def test_the_verb_descriptions_give_facts_and_no_orders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from plugins.rift_vessel.retroxr.retroxr import WORLD_VERBS

    for verb in ("put_down", "hand_over", "bring", "operate"):
        text = WORLD_VERBS[verb]["description"]
        assert "you must" not in text.lower() and "always " not in text.lower(), verb
    hand = WORLD_VERBS["hand_over"]
    assert set(hand["optional_fields"]) == {"seconds", "say"}
    assert "you put it down" not in hand["description"], (
        "not taken is not followed by a put-down any more"
    )
    assert "still holding it" in hand["description"]
    assert set(WORLD_VERBS["bring"]["optional_fields"]) == {"seconds", "say"}
    assert WORLD_VERBS["put_down"]["optional_fields"] == ["target"]


# -- what is said alongside an action ----------------------------------------


@pytest.mark.parametrize("verb", ["hand_over", "bring"])
async def test_what_she_says_with_the_action_is_said_and_not_sent_with_it(
    verb: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core import core_initializer

    monkeypatch.delitem(core_initializer.PLUGIN_REGISTRY, "vox_plugin", raising=False)
    conn, ws = await _connected(["say", verb])
    payload: dict[str, Any] = {"say": "Ecco, prendi.", "seconds": 20}
    if verb == "bring":
        payload["target"] = "Game Boy"
    res = await conn.act(verb, payload)
    assert res.ok
    if conn._speech_tasks:
        await asyncio.gather(*list(conn._speech_tasks))
    await asyncio.sleep(0)
    acted = ws.actions(verb)
    assert len(acted) == 1 and "say" not in acted[0]["payload"], (
        "the words are not the mod's business"
    )
    assert acted[0]["payload"]["seconds"] == 20
    said = ws.actions("say")
    assert len(said) == 1 and said[0]["payload"]["text"] == "Ecco, prendi."
    assert said[0]["payload"]["audio"] is True, (
        "spoken as well as written, as any say of hers"
    )


async def test_no_words_with_the_action_is_no_say(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    conn, ws = await _connected(["say", "hand_over"])
    assert (await conn.act("hand_over", {"seconds": 10})).ok
    assert ws.actions("say") == []
    assert (await conn.act("hand_over", {"say": "   "})).ok
    assert ws.actions("say") == [], "words that are only spaces are not said"


# -- what she keeps of it ------------------------------------------------------


async def test_what_she_combined_is_in_her_journal() -> None:
    conn, _ = await _connected(["say", "combine"])
    assert (await conn.act("combine", {"item": "Mario", "target": "Game Boy"})).ok
    assert (await conn.act("combine", {"target": "Game Boy"})).ok
    texts = [t for _, t in conn._journal]
    assert "combine Mario with Game Boy" in texts and "combine Game Boy" in texts


async def test_losing_the_player_while_following_is_part_of_what_happened() -> None:
    conn, _ = await _connected(["say", "follow"])
    await conn._dispatch_event(
        {
            "event_type": "follow_lost",
            "summary": "You could not get to the player.",
            "data": {},
        }
    )
    await conn._dispatch_event(
        {
            "event_type": "combine_done",
            "summary": "You put Mario in the Game Boy.",
            "data": {},
        }
    )
    texts = [t for _, t in conn._journal]
    assert (
        "You could not get to the player." in texts
        and "You put Mario in the Game Boy." in texts
    )


async def test_following_is_a_fact_in_what_she_is_told_and_in_her_state() -> None:
    conn, _ = await _connected(["say", "follow"])
    await conn.handle_frame(
        {
            "type": "state",
            "state": {
                "position": {"x": 0, "y": 0, "z": 0},
                "holding": "",
                "extra": {"entities": [], "following": True},
            },
        }
    )
    assert "You are following the player." in (conn.describe_surroundings() or "")
    state = await conn.get_world_state()
    assert state is not None and state.flags["following"] is True
    await conn.handle_frame(
        {
            "type": "state",
            "state": {
                "position": {"x": 0, "y": 0, "z": 0},
                "holding": "",
                "extra": {"entities": [], "following": False},
            },
        }
    )
    assert "following" not in (conn.describe_surroundings() or "")
    state = await conn.get_world_state()
    assert state is not None and state.flags["following"] is False
