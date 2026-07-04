import pytest

from interface import discord_interface
from core.interfaces_registry import get_interface_registry
from types import SimpleNamespace


class DummyLiveMgr:
    def __init__(self, trainer_only: bool):
        self._trainer_only = trainer_only

    def is_trainer_only_voice(self):
        return self._trainer_only


@pytest.fixture(autouse=True)
def reset_registry(monkeypatch):
    # ensure clean registry between tests
    reg = get_interface_registry()
    reg._trainer_ids.clear()
    return reg


def test_parse_single_numeric(monkeypatch):
    monkeypatch.setattr(
        discord_interface.config_registry,
        "get_var",
        lambda *args, **kwargs: "discord_bot:81667438105600000",
    )
    result = discord_interface._parse_trainer_id_from_config()
    assert isinstance(result, int)
    assert result == 81667438105600000


def test_parse_username_and_id(monkeypatch):
    # mixture of types should return list
    monkeypatch.setattr(
        discord_interface.config_registry,
        "get_var",
        lambda *args, **kwargs: "discord:alice#1234,discord_bot:81667438105600000",
    )
    result = discord_interface._parse_trainer_id_from_config()
    assert isinstance(result, list)
    assert 81667438105600000 in result
    assert "alice#1234" in result


def test_interface_registration_with_config(monkeypatch):
    captured = {}

    def fake_set(interface, tid):
        captured["iface"] = interface
        captured["tid"] = tid

    monkeypatch.setattr(
        discord_interface.get_interface_registry(),
        "set_trainer_id",
        fake_set,
    )
    monkeypatch.setattr(
        discord_interface.config_registry,
        "get_var",
        lambda *args, **kwargs: "discord:alice",
    )
    # instantiate; token may be blank
    discord_interface.DiscordInterface("")
    assert captured["iface"] == "discord_bot"
    assert captured["tid"] == "alice"


@pytest.mark.asyncio
async def test_join_voice_trainer_gating(monkeypatch):
    # prepare dummy manager enforcing trainer-only voice
    monkeypatch.setattr(
        "engines.live.live_base.LiveSessionManager.get_instance",
        lambda: DummyLiveMgr(trainer_only=True),
    )

    # create an interface instance (token irrelevant)
    iface = discord_interface.DiscordInterface("")

    # patch _join_voice to record calls
    joined = []

    async def fake_join(channel_id):
        joined.append(channel_id)
        return {"status": "success"}

    iface._join_voice = fake_join

    # register trainer as numeric and by name
    get_interface_registry().set_trainer_id(
        "discord_bot",
        [
            81667438105600000,
            "xargonwan",
            "xargonwan#0001",
        ],
    )

    # build an original_message with from_user attr
    user = SimpleNamespace(
        id=81667438105600000, name="foo", display_name="foo", discriminator="0001"
    )
    orig = SimpleNamespace(from_user=user)

    # include a dummy channel_id so execution proceeds past the gate check
    action = {"type": "join_voice_discord", "payload": {"channel_id": "1234"}}
    context = {"sender_id": str(user.id)}

    await iface.execute_action(action, context, bot=None, original_message=orig)
    assert joined, "trainer should be allowed and _join_voice called"

    joined.clear()

    # try with username match instead of id
    user2 = SimpleNamespace(
        id=999, name="xargonwan", display_name="test", discriminator="0001"
    )
    orig2 = SimpleNamespace(from_user=user2)
    context2 = {"sender_id": str(user2.id)}
    await iface.execute_action(action, context2, bot=None, original_message=orig2)
    assert joined, "trainer should be allowed by username"

    joined.clear()

    # non-trainer should be rejected
    user3 = SimpleNamespace(
        id=555, name="bar", display_name="bar", discriminator="1234"
    )
    orig3 = SimpleNamespace(from_user=user3)
    context3 = {"sender_id": str(user3.id)}
    await iface.execute_action(action, context3, bot=None, original_message=orig3)
    assert not joined, "non-trainer must not be allowed"
