"""The Telegram user account can write first to someone it shares a group with.

Live (2026-11-12): ``send_message`` to ``telegram/31321637`` failed with
``Could not find the input entity for PeerUser(user_id=31321637)``. A user
account can only address a bare user id it has *seen*; the startup warm-up only
loads recent dialogs, and that person had never written privately. Telethon
learns a user from any request that returns it, so listing the members of a
shared group is enough.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, AsyncIterator

import pytest

from interface.telegram import _compat
from interface.telegram._compat import BadRequest, TelethonBot

JAY = 31321637


class FakeClient:
    """Minimal Telethon client: knows a user only after listing a group."""

    def __init__(
        self,
        dialogs: list[Any],
        members: dict[str, list[int]],
        usernames: dict[str, int] | None = None,
    ) -> None:
        self.dialogs = dialogs
        self.members = members
        self.usernames = usernames or {}
        self.known: set[int] = set()
        self.scans: list[str] = []
        self.sent: list[tuple[Any, str]] = []

    async def get_input_entity(self, peer: Any) -> Any:
        if peer in self.known:
            return ("input", peer)
        raise ValueError(f"Could not find the input entity for PeerUser({peer})")

    async def get_entity(self, peer: Any) -> Any:
        if isinstance(peer, str):
            user_id = self.usernames.get(peer.lstrip("@").lower())
            if user_id is None:
                raise ValueError(f'No user has "{peer}" as username')
            self.known.add(user_id)  # ResolveUsername hands back the access hash
            return SimpleNamespace(id=user_id)
        await self.get_input_entity(peer)
        return SimpleNamespace(id=peer)

    async def get_dialogs(self, limit: int | None = None) -> list[Any]:
        return self.dialogs

    async def iter_participants(
        self, entity: Any, limit: int | None = None
    ) -> AsyncIterator[Any]:
        self.scans.append(entity)
        for user_id in self.members.get(entity, []):
            self.known.add(user_id)  # Telethon caches every user it receives
            yield SimpleNamespace(id=user_id)

    async def send_message(self, peer: Any, text: str, **_: Any) -> Any:
        self.sent.append((peer, text))
        return SimpleNamespace(id=7, chat_id=peer, date=None)


def _group(name: str) -> Any:
    return SimpleNamespace(is_group=True, is_channel=False, entity=name)


def _private() -> Any:
    return SimpleNamespace(is_group=False, is_channel=False, entity="private")


def test_a_user_in_a_shared_group_can_be_written_to_by_id() -> None:
    client = FakeClient(
        [_private(), _group("g1"), _group("g2")], {"g1": [1, 2], "g2": [JAY]}
    )
    bot = TelethonBot(client)

    sent = asyncio.run(bot.send_message(str(JAY), "ciao"))

    assert client.sent == [(JAY, "ciao")]
    assert sent.message_id == 7
    assert client.scans == ["g1", "g2"]  # stopped as soon as the user was found


def test_an_already_known_user_costs_no_scan() -> None:
    client = FakeClient([_group("g1")], {"g1": [JAY]})
    client.known.add(JAY)

    asyncio.run(TelethonBot(client).send_message(JAY, "ciao"))

    assert client.scans == []


def test_a_user_nobody_shares_a_group_with_gets_an_actionable_error() -> None:
    client = FakeClient([_group("g1")], {"g1": [1, 2]})
    bot = TelethonBot(client)

    with pytest.raises(BadRequest) as caught:
        asyncio.run(bot.send_message(JAY, "ciao"))

    message = str(caught.value)
    assert message.startswith("Chat not found")  # wording the retry helpers key on
    assert str(JAY) in message
    assert "@username" in message
    assert client.sent == []


def test_a_miss_is_remembered_so_the_groups_are_not_rescanned() -> None:
    client = FakeClient([_group("g1")], {"g1": [1]})
    bot = TelethonBot(client)

    for _ in range(3):
        with pytest.raises(BadRequest):
            asyncio.run(bot.send_message(JAY, "ciao"))

    assert client.scans == ["g1"]


def test_a_miss_expires(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient([_group("g1")], {"g1": [1]})
    bot = TelethonBot(client)
    with pytest.raises(BadRequest):
        asyncio.run(bot.send_message(JAY, "ciao"))

    monkeypatch.setattr(_compat, "_PEER_MISS_TTL_SECONDS", 0.0)
    client.members["g1"].append(JAY)  # they joined in the meantime

    asyncio.run(bot.send_message(JAY, "ciao"))
    assert client.sent == [(JAY, "ciao")]


def test_groups_are_left_to_telethon() -> None:
    client = FakeClient([_group("g1")], {"g1": [JAY]})
    bot = TelethonBot(client)

    assert asyncio.run(bot.ensure_peer("-1003098886330")) == -1003098886330
    assert client.scans == []


# -- public @username --------------------------------------------------------


@pytest.fixture()
def peer_id_is_entity_id(monkeypatch: pytest.MonkeyPatch) -> None:
    from telethon import utils

    monkeypatch.setattr(utils, "get_peer_id", lambda entity: entity.id)


def test_a_username_destination_is_settled_on_the_numeric_id(
    peer_id_is_entity_id: None,
) -> None:
    client = FakeClient([], {}, usernames={"jaycheshire": JAY})
    bot = TelethonBot(client)

    asyncio.run(bot.send_message("@JayCheshire", "ciao"))

    assert client.sent == [(JAY, "ciao")]  # numeric, not "@JayCheshire"


def test_a_bare_username_is_accepted_too(peer_id_is_entity_id: None) -> None:
    client = FakeClient([], {}, usernames={"jaycheshire": JAY})
    assert asyncio.run(TelethonBot(client).ensure_peer("jaycheshire")) == JAY


def test_an_unknown_username_is_a_clear_chat_not_found(
    peer_id_is_entity_id: None,
) -> None:
    bot = TelethonBot(FakeClient([], {}))

    with pytest.raises(BadRequest) as caught:
        asyncio.run(bot.ensure_peer("@nobody_here"))

    assert str(caught.value).startswith("Chat not found")
    assert "nobody_here" in str(caught.value)


def test_an_unreachable_id_falls_back_on_the_username_another_interface_knows(
    peer_id_is_entity_id: None,
) -> None:
    client = FakeClient([_group("g1")], {"g1": [1]}, usernames={"jaycheshire": JAY})
    bot = TelethonBot(client)
    asked: list[int] = []

    async def lookup(user_id: int) -> str | None:
        asked.append(user_id)
        return "jaycheshire"

    bot.username_lookup = lookup

    asyncio.run(bot.send_message(JAY, "ciao"))

    assert asked == [JAY]
    assert client.sent == [(JAY, "ciao")]


def test_a_username_that_belongs_to_someone_else_is_not_trusted(
    peer_id_is_entity_id: None,
) -> None:
    client = FakeClient([], {}, usernames={"someoneelse": 999})
    bot = TelethonBot(client)

    async def lookup(user_id: int) -> str | None:
        return "someoneelse"

    bot.username_lookup = lookup

    with pytest.raises(BadRequest):
        asyncio.run(bot.send_message(JAY, "ciao"))
    assert client.sent == []


def test_a_failing_username_lookup_degrades_to_the_clear_error(
    peer_id_is_entity_id: None,
) -> None:
    bot = TelethonBot(FakeClient([], {}))

    async def lookup(user_id: int) -> str | None:
        raise RuntimeError("bot api down")

    bot.username_lookup = lookup

    with pytest.raises(BadRequest) as caught:
        asyncio.run(bot.send_message(JAY, "ciao"))
    assert "@username" in str(caught.value)
