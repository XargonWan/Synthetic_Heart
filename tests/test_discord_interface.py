import asyncio
import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from interface.discord_interface import DiscordInterface


def test_execute_action_with_interface_path(monkeypatch):
    di = DiscordInterface(bot_token="")

    called = {}

    async def fake_send_message(arg1, arg2=None, **kwargs):
        called["arg1"] = arg1
        called["arg2"] = arg2
        called.update(kwargs)

    monkeypatch.setattr(di, "send_message", fake_send_message)

    action = {
        "type": "send_message",
        "payload": {
            "interface_path": "discord_bot/111111111/222222222",
            "text": "hello from test",
        },
    }

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(di.execute_action(action, context={}, bot=None))
    loop.close()

    assert "arg1" in called
    # when interface_path provided, we expect send_message called with dict containing interface_path
    assert isinstance(called["arg1"], dict)
    assert called["arg1"]["interface_path"] == "discord_bot/111111111/222222222"
    assert called["arg1"]["text"] == "hello from test"


async def _async_noop(*args, **kwargs):
    return None


def test_send_dm_to_user_id(monkeypatch):
    di = DiscordInterface(bot_token="")

    # Shared fake user so we can observe what gets sent.
    class FakeUser:
        def __init__(self):
            self.sent = []

        async def send(self, content, file=None):
            # mimic Discord API signature (file kwarg)
            self.sent.append(content)

    class FakeClient:
        def __init__(self, user):
            self._user = user

        def get_channel(self, id):
            return None

        def get_user(self, id):
            return self._user

        async def fetch_user(self, id):
            return self._user

    shared_user = FakeUser()
    fake_client = FakeClient(shared_user)
    di.client = fake_client

    # Run two sends and ensure they reach the same FakeUser instance.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(di._discord_send("111111111", "hello dm"))
    loop.run_until_complete(di._discord_send("222222222", "second dm"))
    loop.close()

    assert shared_user.sent == ["hello dm", "second dm"]


async def _fake_add_message_to_context(*args, **kwargs):
    return None


@pytest.mark.asyncio
async def test_plain_at_symbol_does_not_mark_explicit_trigger(monkeypatch):
    """Typing a plain '@' (without Discord-style <@id> mention) must NOT wake Synth."""
    di = DiscordInterface(bot_token="")

    # Fake client with bot user set
    di.client = SimpleNamespace(user=SimpleNamespace(id=999, name="SynthBot"))

    # Fake message that contains a plain '@' but does NOT include a Discord mention token
    class FakeChannel:
        id = 12345
        type = "text"

    fake_message = SimpleNamespace(
        content="hello @someone",
        author=SimpleNamespace(id=2, name="user2", display_name="User Two"),
        channel=FakeChannel(),
        guild=SimpleNamespace(id=1),
        mentions=[],
        role_mentions=[],
        reference=None,
        created_at=SimpleNamespace(isoformat=lambda: "2026-01-01T00:00:00"),
        id=111,
        attachments=[],
        content_raw=None,
    )

    captured = {}

    async def fake_enqueue(bot, wrapped, **kwargs):
        captured["wrapped"] = wrapped
        return None

    # Prevent external helpers from raising during the unit test
    monkeypatch.setattr(
        "core.chat_context_manager.add_message_to_context", _fake_add_message_to_context
    )
    monkeypatch.setattr(
        "interface.discord_interface.resolve_and_touch",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr("core.message_queue.enqueue", fake_enqueue)

    await di._process_message(fake_message)

    assert "wrapped" in captured
    assert getattr(captured["wrapped"], "is_explicit_trigger", False) is False


@pytest.mark.asyncio
async def test_real_discord_mention_marks_explicit_trigger(monkeypatch):
    """A real Discord-style mention (<@id>) should still mark explicit trigger."""
    di = DiscordInterface(bot_token="")

    # Fake client with bot user set
    di.client = SimpleNamespace(user=SimpleNamespace(id=999, name="SynthBot"))

    # Fake message that contains Discord mention token and mentions list contains bot
    class FakeChannel:
        id = 22222
        type = "text"

    fake_message = SimpleNamespace(
        content=f"<@{999}> hello",
        author=SimpleNamespace(id=3, name="user3", display_name="User Three"),
        channel=FakeChannel(),
        guild=SimpleNamespace(id=2),
        mentions=[SimpleNamespace(id=999, name="SynthBot")],
        role_mentions=[],
        reference=None,
        created_at=SimpleNamespace(isoformat=lambda: "2026-01-01T00:00:00"),
        id=222,
        attachments=[],
    )

    captured = {}

    async def fake_enqueue(bot, wrapped, **kwargs):
        captured["wrapped"] = wrapped
        return None

    # Prevent external helpers from raising during the unit test
    monkeypatch.setattr(
        "core.chat_context_manager.add_message_to_context", _fake_add_message_to_context
    )
    monkeypatch.setattr(
        "interface.discord_interface.resolve_and_touch",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr("core.message_queue.enqueue", fake_enqueue)

    await di._process_message(fake_message)

    assert "wrapped" in captured
    assert getattr(captured["wrapped"], "is_explicit_trigger", False) is True


# ---------------------------------------------------------------------------
# live voice session cleanup tests
# ---------------------------------------------------------------------------


class _FakeMember:
    def __init__(self, id: int, bot: bool = False):
        self.id = id
        self.bot = bot


def _make_channel(channel_id: int, members: list[_FakeMember]):
    return SimpleNamespace(id=channel_id, members=members)


@pytest.mark.asyncio
async def test_voice_cleanup_when_last_human_leaves(monkeypatch):
    """If the only human leaves a voice channel, the live voice session stops."""
    di = DiscordInterface(bot_token="")
    # fake client and bot user
    di.client = SimpleNamespace(user=SimpleNamespace(id=42))

    guild_id = 100
    channel_id = 200
    # active live state pointing at that channel
    di._live_voice_state = {guild_id: {"channel_id": channel_id}}

    # before snapshot: human + bot
    before_channel = _make_channel(
        channel_id, [_FakeMember(1), _FakeMember(42, bot=True)]
    )
    before = SimpleNamespace(channel=before_channel)
    after = SimpleNamespace(channel=None)

    # after update the channel only contains the bot
    after_channel = _make_channel(channel_id, [_FakeMember(42, bot=True)])
    di.client.get_channel = lambda cid: after_channel

    called = {}

    async def fake_stop(gid):
        called["stopped"] = gid
        return {"status": "ok"}

    monkeypatch.setattr(di, "_stop_live_voice", fake_stop)

    member = SimpleNamespace(id=1, bot=False, guild=SimpleNamespace(id=guild_id))

    await di._handle_voice_state_update(member, before, after)
    assert called.get("stopped") == guild_id


@pytest.mark.asyncio
async def test_voice_cleanup_not_triggered_if_other_human_remains(monkeypatch):
    """Leaving a channel with other humans present should not stop the session."""
    di = DiscordInterface(bot_token="")
    di.client = SimpleNamespace(user=SimpleNamespace(id=42))

    guild_id = 101
    channel_id = 201
    di._live_voice_state = {guild_id: {"channel_id": channel_id}}

    # before: two humans + bot
    before_channel = _make_channel(
        channel_id, [_FakeMember(2), _FakeMember(3), _FakeMember(42, bot=True)]
    )
    before = SimpleNamespace(channel=before_channel)
    after = SimpleNamespace(channel=None)

    # after update channel still has one human plus bot
    after_channel = _make_channel(
        channel_id, [_FakeMember(3), _FakeMember(42, bot=True)]
    )
    di.client.get_channel = lambda cid: after_channel

    called = {}

    async def fake_stop(gid):
        called["stopped"] = gid
        return {"status": "ok"}

    monkeypatch.setattr(di, "_stop_live_voice", fake_stop)

    member = SimpleNamespace(id=2, bot=False, guild=SimpleNamespace(id=guild_id))
    await di._handle_voice_state_update(member, before, after)
    assert "stopped" not in called


# ---------------------------------------------------------------------------
# live sync & diary behaviour tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_sync_forwarding(monkeypatch):
    """Messages should be forwarded to live session; no duplicate DB write on discord_live_."""
    di = DiscordInterface(bot_token="")
    di.client = SimpleNamespace(user=SimpleNamespace(id=1))

    class FakeMgr:
        def __init__(self):
            self.sent = []

        def is_session_active(self, gid):
            return True

        async def send_context_update(self, gid, text):
            self.sent.append((gid, text))

    fake_mgr = FakeMgr()
    monkeypatch.setattr(
        "core.live_session_manager.LiveSessionManager.get_instance",
        lambda: fake_mgr,
    )

    # force sync enabled
    monkeypatch.setattr(
        "core.config_manager.config_registry.get_value",
        lambda key, default, value_type=None, **kw: (
            True if key == "LIVE_SYNC_CHAT_HISTORY" else default
        ),
    )

    monkeypatch.setattr("core.message_queue.enqueue", lambda *a, **k: asyncio.sleep(0))

    fake_message = SimpleNamespace(
        content="hello",
        author=SimpleNamespace(id=2, name="bob"),
        guild=SimpleNamespace(id=42),
        channel=SimpleNamespace(id=123),
        mentions=[],
        role_mentions=[],
        reference=None,
        created_at=SimpleNamespace(isoformat=lambda: ""),
    )

    await di._process_message(fake_message)
    await asyncio.sleep(0)
    # Context update includes the sender prefix, e.g. "[Text chat] bob: hello"
    assert len(fake_mgr.sent) == 1
    gid, text = fake_mgr.sent[0]
    assert gid == 42
    assert "hello" in text
    assert "bob" in text
    # The message is already persisted to discord_bot/... by the normal pipeline;
    # we must NOT write a second discord_live_ entry (that was the duplicate bug).


@pytest.mark.asyncio
async def test_flush_live_diary_at_stop(monkeypatch):
    """Stopping a live session should flush a single diary entry."""
    di = DiscordInterface(bot_token="")
    monkeypatch.setattr(
        "engines.live.live_base.LiveSessionManager.get_instance",
        lambda: SimpleNamespace(deactivate_live_for_path=lambda ip, gid: None),
    )
    di.client = None

    calls = []

    def fake_add_diary(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("plugins.ai_diary.add_diary_entry", fake_add_diary)

    di._live_voice_state = {
        7: {
            "interface_path": "discord_live_7",
            "diary_buffer": [("u1", "m1"), ("u2", "m2")],
        }
    }

    res = await di._stop_live_voice(7)
    assert res.get("status") == "success"
    assert len(calls) == 1
    assert "Sessione vocale terminata" in calls[0].get("interaction_summary", "")


@pytest.mark.asyncio
async def test_old_write_live_diary_entry(monkeypatch):
    """Deprecated helper still writes a diary entry in executor."""
    calls = []

    def fake_add(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr("plugins.ai_diary.add_diary_entry", fake_add)
    from interface.discord_interface import _write_live_diary_entry

    await _write_live_diary_entry(99, "hello user", "reply model")
    assert len(calls) == 1
    assert "Voice turn" in calls[0].get("interaction_summary", "")


# ---------------------------------------------------------------------------
# /leave slash command tests
# ---------------------------------------------------------------------------


class _FakeVC:
    def __init__(self, channel_name):
        self.channel = SimpleNamespace(name=channel_name)


class _FakeGuild:
    def __init__(self, id, name, vc=None):
        self.id = id
        self.name = name
        self.voice_client = vc


def _make_client(guilds):
    # Build a fake client with both guilds list and voice_clients list.
    vcs = []
    for g in guilds:
        vc = getattr(g, "voice_client", None)
        if vc is not None:
            # attach guild ref on the voice client for ease of lookup
            setattr(vc, "guild", g)
            vcs.append(vc)
    return SimpleNamespace(
        guilds=guilds,
        voice_clients=vcs,
        get_guild=lambda gid: next((g for g in guilds if g.id == int(gid)), None),
    )


@pytest.mark.asyncio
async def test_slash_leave_no_connections(monkeypatch):
    di = DiscordInterface(bot_token="")
    di.client = _make_client([])
    responses = []

    async def fake_send(cid, text, **kwargs):
        responses.append(text)

    monkeypatch.setattr(di, "_discord_send", fake_send)
    msg = SimpleNamespace(
        content="/leave",
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=2),
        author=SimpleNamespace(id=123, bot=False),
    )
    await di._process_message(msg)
    assert responses and responses[0].startswith("❌"), (
        "should warn about no connections"
    )


@pytest.mark.asyncio
async def test_slash_leave_single_channel(monkeypatch):
    di = DiscordInterface(bot_token="")
    guild = _FakeGuild(10, "G", _FakeVC("C"))
    di.client = _make_client([guild])
    called = {}

    async def fake_stop(gid):
        called["stop"] = gid

    async def fake_leave(gid):
        called["leave"] = gid
        return {"status": "success"}

    responses = []

    async def fake_send(cid, text, **kwargs):
        responses.append(text)

    monkeypatch.setattr(di, "_stop_live_voice", fake_stop)
    monkeypatch.setattr(di, "_leave_voice", fake_leave)
    monkeypatch.setattr(di, "_discord_send", fake_send)

    msg = SimpleNamespace(
        content="/leave",
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=2),
        author=SimpleNamespace(id=123, bot=False),
    )
    await di._process_message(msg)
    assert called.get("stop") == 10 and called.get("leave") == 10
    assert "Left voice channel" in responses[-1]


@pytest.mark.asyncio
async def test_slash_leave_multiple_guilds(monkeypatch):
    di = DiscordInterface(bot_token="")
    g1 = _FakeGuild(10, "One", _FakeVC("A"))
    g2 = _FakeGuild(20, "Two", _FakeVC("B"))
    di.client = _make_client([g1, g2])
    called = {}

    async def fake_stop(gid):
        called.setdefault("stop", []).append(gid)

    async def fake_leave(gid):
        called.setdefault("leave", []).append(gid)
        return {"status": "success"}

    responses = []

    async def fake_send(cid, text, **kwargs):
        responses.append(text)

    monkeypatch.setattr(di, "_stop_live_voice", fake_stop)
    monkeypatch.setattr(di, "_leave_voice", fake_leave)
    monkeypatch.setattr(di, "_discord_send", fake_send)

    msg = SimpleNamespace(
        content="/leave",
        guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=2),
        author=SimpleNamespace(id=123, bot=False),
    )
    await di._process_message(msg)
    assert any("multiple voice channels" in r for r in responses)

    responses.clear()
    # ask to leave specific guild by id
    msg.content = "/leave 10"
    msg.author = SimpleNamespace(id=123, bot=False)
    await di._process_message(msg)
    assert 10 in called.get("stop", []) and 10 in called.get("leave", [])
    assert any("Left voice channel" in r for r in responses)


# ---------------------------------------------------------------------------
# join_voice_discord behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_join_voice_no_autostart_when_no_trainer(monkeypatch):
    """If no trainer is in the voice channel after join, live session must NOT start.

    The obsolete ``start_live_voice`` payload flag is ignored; auto-start is
    driven exclusively by trainer presence in channel members.
    """
    try:
        from engines.live.live_base import LiveSessionManager as _LSM

        monkeypatch.setattr(_LSM, "get_instance", lambda: _LSM())
        monkeypatch.setattr(_LSM, "is_trainer_only_voice", lambda self: False)
    except Exception:
        pass

    di = DiscordInterface(bot_token="")
    # di.client is None — auto-start block is skipped entirely
    joined: list[str] = []
    started: list[str] = []

    async def fake_join(cid):
        joined.append(cid)
        return {"status": "success"}

    async def fake_start(
        cid,
        attachments=None,
        initial_text=None,
        initial_sender=None,
        initial_timestamp=None,
    ):
        started.append(cid)
        return {"status": "success"}

    monkeypatch.setattr(di, "_join_voice", fake_join)
    monkeypatch.setattr(di, "_start_live_voice", fake_start)

    action = {
        "type": "join_voice_discord",
        "payload": {"channel_id": "123", "start_live_voice": True},
    }
    await di.execute_action(action, {}, bot=None, original_message=None)

    assert joined == ["123"]
    assert started == [], "live session must not start when client is None / no trainer"


@pytest.mark.asyncio
async def test_join_voice_autostart_when_trainer_present(monkeypatch):
    """If the trainer is in the channel after join, live session starts automatically."""
    try:
        from engines.live.live_base import LiveSessionManager as _LSM

        monkeypatch.setattr(_LSM, "get_instance", lambda: _LSM())
        monkeypatch.setattr(_LSM, "is_trainer_only_voice", lambda self: False)
    except Exception:
        pass

    TRAINER_ID = 777

    # fake registry: member 777 is the trainer
    class _FakeRegistry:
        def is_trainer(self, iface: str, user_id: str) -> bool:
            return user_id == str(TRAINER_ID)

    monkeypatch.setattr(
        "interface.discord_interface.get_interface_registry",
        lambda: _FakeRegistry(),
    )

    # fake channel with trainer member
    trainer_member = SimpleNamespace(id=TRAINER_ID, bot=False)
    fake_channel = SimpleNamespace(id=123, members=[trainer_member])

    # fake voice client already connected to that channel
    fake_vc = SimpleNamespace(channel=fake_channel)

    # fake client with voice_clients populated (used by the new auto-start logic)
    fake_client = SimpleNamespace(
        voice_clients=[fake_vc],
        get_channel=lambda cid: fake_channel,
    )

    di = DiscordInterface(bot_token="")
    di.client = fake_client

    joined: list[str] = []
    started: list[str] = []

    async def fake_join(cid):
        joined.append(cid)
        return {"status": "success"}

    async def fake_start(
        cid,
        attachments=None,
        initial_text=None,
        initial_sender=None,
        initial_timestamp=None,
    ):
        started.append(cid)
        return {"status": "success"}

    monkeypatch.setattr(di, "_join_voice", fake_join)
    monkeypatch.setattr(di, "_start_live_voice", fake_start)

    action = {
        "type": "join_voice_discord",
        "payload": {"channel_id": "123"},
    }
    await di.execute_action(action, {}, bot=None, original_message=None)

    assert joined == ["123"], "channel join must happen"
    assert started == [123], "live session must auto-start when trainer is present"


@pytest.mark.asyncio
async def test_join_voice_autostart_falls_back_to_direct_start_for_trainer(
    monkeypatch,
):
    """If Discord cache inspection fails, a trainer-requested join should still start live."""
    try:
        from engines.live.live_base import LiveSessionManager as _LSM

        monkeypatch.setattr(_LSM, "get_instance", lambda: _LSM())
        monkeypatch.setattr(_LSM, "is_trainer_only_voice", lambda self: False)
    except Exception:
        pass

    trainer_id = 777

    class FakeRegistry:
        def is_trainer(self, iface: str, user_id: str) -> bool:
            return user_id == str(trainer_id)

    monkeypatch.setattr(
        "interface.discord_interface.get_interface_registry",
        lambda: FakeRegistry(),
    )

    di = DiscordInterface(bot_token="")
    di.client = SimpleNamespace(
        voice_clients=[],
        get_channel=lambda cid: None,
        fetch_channel=AsyncMock(side_effect=RuntimeError("cache miss")),
    )

    joined: list[str] = []
    started: list[str] = []

    async def fake_join(cid):
        joined.append(cid)
        return {"status": "success"}

    async def fake_start(
        cid,
        attachments=None,
        initial_text=None,
        initial_sender=None,
        initial_timestamp=None,
    ):
        started.append(str(cid))
        return {"status": "success"}

    monkeypatch.setattr(di, "_join_voice", fake_join)
    monkeypatch.setattr(di, "_start_live_voice", fake_start)

    original_message = SimpleNamespace(
        text="join voice now",
        attachments=[],
        date=SimpleNamespace(isoformat=lambda: "2026-04-12T20:54:41+00:00"),
        from_user=SimpleNamespace(id=trainer_id, full_name="Trainer"),
    )

    action = {
        "type": "join_voice_discord",
        "payload": {"channel_id": "123"},
    }
    await di.execute_action(action, {}, bot=None, original_message=original_message)

    assert joined == ["123"]
    assert started == ["123"]


# ---------------------------------------------------------------------------
# Live-voice engine resolution tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_live_voice_uses_stored_engine_not_global_cortex(monkeypatch):
    """_stop_live_voice must call stop_live_voice_session on the engine stored
    in _live_voice_state, NOT on the globally active cortex (which may be
    selenium_gemini and wouldn't have that method)."""
    import interface.discord_interface as _mod

    GUILD_ID = 42

    # Global cortex has NO stop_live_voice_session
    global_plugin = SimpleNamespace()
    monkeypatch.setattr(_mod, "plugin", global_plugin, raising=False)

    # Stored live engine DOES have stop_live_voice_session
    stopped_guilds: list[int] = []

    class _FakeLiveEngine:
        async def stop_live_voice_session(self, guild_id: int) -> None:
            stopped_guilds.append(guild_id)

    fake_live_engine = _FakeLiveEngine()

    di = DiscordInterface(bot_token="")

    # Client: guild with no active voice_client (simplifies cleanup path)
    fake_guild = SimpleNamespace(voice_client=None)
    fake_client = SimpleNamespace(get_guild=lambda gid: fake_guild)
    di.client = fake_client

    # Pre-populate live state with the stored engine and interface_path
    di._live_voice_state = {
        GUILD_ID: {
            "channel_id": 99,
            "audio_buffer": SimpleNamespace(close=lambda: None),
            "live_engine": fake_live_engine,
            "interface_path": f"discord_live_{GUILD_ID}",
        }
    }

    # Silence LiveSessionManager.deactivate_live_for_path
    try:
        from engines.live.live_base import LiveSessionManager as _LSM

        async def _noop_deactivate(self, path, gid):
            pass

        monkeypatch.setattr(_LSM, "deactivate_live_for_path", _noop_deactivate)
    except Exception:
        pass

    result = await di._stop_live_voice(GUILD_ID)

    assert result["status"] == "success"
    assert GUILD_ID in stopped_guilds, (
        "_stop_live_voice must call stop_live_voice_session on the stored live engine"
    )
    assert not hasattr(global_plugin, "stop_live_voice_session"), (
        "global cortex must not have been given the method (sanity check)"
    )


@pytest.mark.asyncio
async def test_start_live_voice_uses_registry_engine_not_global_cortex(monkeypatch):
    """_start_live_voice must resolve the live-capable engine from the cortex
    registry, independent of the globally active cortex engine."""
    import interface.discord_interface as _mod

    GUILD_ID = 77
    CHANNEL_ID = 88

    # ---- Global cortex: no get_live_session_manager ----
    global_plugin = SimpleNamespace()
    monkeypatch.setattr(_mod, "plugin", global_plugin, raising=False)

    # ---- Fake live engine exposed via cortex registry ----
    manager_started: list[dict] = []

    class _FakeManager:
        def set_audio_callback(self, cb):
            pass

        def set_text_callback(self, cb):
            pass

        def set_tool_executor(self, executor):
            pass

        def set_turn_complete_callback(self, cb):
            pass

        def set_reconnect_failed_callback(self, cb):
            pass

        async def start_session(self, **kwargs) -> bool:
            manager_started.append(kwargs)
            return True

    fake_manager = _FakeManager()

    engine_used: list[object] = []

    class _FakeLiveCapableEngine:
        def get_live_session_manager(self):
            engine_used.append(self)
            return fake_manager

    fake_live_engine = _FakeLiveCapableEngine()

    class _FakeRegistry:
        def get_engines_by_cortex(self, cortex):
            return []

        def get_available_engines(self):
            return ["gemini_api"]

        def get_engine(self, name):
            return fake_live_engine if name == "gemini_api" else None

    monkeypatch.setattr(
        "interface.discord_interface.get_cortex_registry",
        lambda: _FakeRegistry(),
        raising=False,
    )
    # Also patch the import inside the method
    import core.cortex_registry as _cr_mod

    monkeypatch.setattr(_cr_mod, "get_cortex_registry", lambda: _FakeRegistry())

    # ---- Enable voice recv ----
    monkeypatch.setattr(_mod, "_HAS_VOICE_RECV", True, raising=False)
    import types

    fake_voice_recv = types.SimpleNamespace(VoiceRecvClient=object)
    monkeypatch.setattr(_mod, "voice_recv", fake_voice_recv, raising=False)

    # ---- Fake Discord objects ----
    fake_vc = SimpleNamespace(
        channel=SimpleNamespace(id=CHANNEL_ID, name="voice-test"),
        is_connected=lambda: True,
        is_playing=lambda: False,
        is_listening=lambda: True,
        play=lambda src: None,
        listen=lambda sink: None,
        move_to=lambda ch: None,
    )
    # Make fake_vc pretend to NOT be an instance of VoiceRecvClient
    # (isinstance check) — use a different object
    fake_guild = SimpleNamespace(
        id=GUILD_ID,
        voice_client=None,
    )
    fake_channel = SimpleNamespace(
        id=CHANNEL_ID,
        name="voice-test",
        guild=fake_guild,
    )

    async def _fake_connect(cls=None):
        return fake_vc

    fake_channel.connect = _fake_connect

    import asyncio

    fake_client = SimpleNamespace(
        get_channel=lambda cid: fake_channel,
        loop=asyncio.get_event_loop(),
    )

    # ---- Patch helpers ----
    async def _fake_system_instruction():
        return "system prompt"

    monkeypatch.setattr(
        _mod,
        "build_live_system_instruction",
        _fake_system_instruction,
        raising=False,
    )
    monkeypatch.setattr(
        _mod,
        "_build_gemini_tool_declarations",
        lambda: [],
        raising=False,
    )

    # Silence activate_live_for_path
    try:
        from engines.live.live_base import LiveSessionManager as _LSM2

        async def _noop_activate(self, path, gid, rejoin_callback=None):
            pass

        monkeypatch.setattr(_LSM2, "activate_live_for_path", _noop_activate)
    except Exception:
        pass

    # ---- Run ----
    di = DiscordInterface(bot_token="")
    di.client = fake_client

    result = await di._start_live_voice(CHANNEL_ID)

    assert result["status"] == "success", f"Expected success, got: {result}"
    assert len(engine_used) == 1, (
        "get_live_session_manager must be called on registry engine"
    )
    assert len(manager_started) == 1, "manager.start_session must be called"
    assert GUILD_ID in di._live_voice_state, "_live_voice_state must be set"
    state = di._live_voice_state[GUILD_ID]
    assert state["live_engine"] is fake_live_engine, (
        "_live_voice_state['live_engine'] must be the registry engine, not global cortex"
    )
    assert state["interface_path"] == f"discord_live_{GUILD_ID}", (
        "_live_voice_state['interface_path'] must be set for deactivation"
    )


@pytest.mark.asyncio
async def test_start_live_voice_passes_initial_text_to_manager(monkeypatch):
    """Fresh live sessions should receive the summon text as startup context."""
    import interface.discord_interface as _mod

    guild_id = 91
    channel_id = 92
    manager_started: list[dict[str, object]] = []

    class FakeManager:
        def set_audio_callback(self, cb):
            pass

        def set_text_callback(self, cb):
            pass

        def set_tool_executor(self, executor):
            pass

        def set_turn_complete_callback(self, cb):
            pass

        def set_reconnect_failed_callback(self, cb):
            pass

        async def start_session(self, **kwargs) -> bool:
            manager_started.append(kwargs)
            return True

    fake_manager = FakeManager()

    class FakeLiveEngine:
        def get_live_session_manager(self):
            return fake_manager

    class FakeRegistry:
        def get_engines_by_cortex(self, cortex):
            return []

        def get_available_engines(self):
            return ["gemini_api"]

        def get_engine(self, name):
            return FakeLiveEngine() if name == "gemini_api" else None

    monkeypatch.setattr(_mod, "_HAS_VOICE_RECV", True, raising=False)
    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", lambda: FakeRegistry()
    )

    import types

    monkeypatch.setattr(
        _mod, "voice_recv", types.SimpleNamespace(VoiceRecvClient=object), raising=False
    )

    fake_vc = SimpleNamespace(
        channel=SimpleNamespace(id=channel_id, name="voice-test"),
        is_connected=lambda: True,
        is_playing=lambda: False,
        is_listening=lambda: True,
        play=lambda src: None,
        listen=lambda sink: None,
        move_to=lambda ch: None,
    )
    fake_guild = SimpleNamespace(id=guild_id, voice_client=None)
    fake_channel = SimpleNamespace(id=channel_id, name="voice-test", guild=fake_guild)

    async def fake_connect(cls=None):
        return fake_vc

    fake_channel.connect = fake_connect
    fake_client = SimpleNamespace(
        get_channel=lambda cid: fake_channel,
        loop=asyncio.get_event_loop(),
    )

    async def fake_system_instruction(attachment_context=None):
        return "system prompt"

    monkeypatch.setattr(
        "core.prompt_engine.build_live_system_instruction",
        fake_system_instruction,
    )

    try:
        from engines.live.live_base import LiveSessionManager as _LSM2

        async def noop_activate(self, path, gid, rejoin_callback=None):
            pass

        monkeypatch.setattr(_LSM2, "activate_live_for_path", noop_activate)
    except Exception:
        pass

    di = DiscordInterface(bot_token="")
    di.client = fake_client

    result = await di._start_live_voice(
        channel_id,
        initial_text="join me in voice",
        initial_sender="Alice",
        initial_timestamp="2026-04-12T18:14:22.983048+00:00",
    )

    assert result["status"] == "success"
    assert manager_started, "manager.start_session must be called"
    start_kwargs = manager_started[0]
    assert start_kwargs["initial_user_message"] == "join me in voice"
    assert start_kwargs["initial_user_name"] == "Alice"
    assert (
        start_kwargs["initial_message_timestamp"] == "2026-04-12T18:14:22.983048+00:00"
    )


@pytest.mark.asyncio
async def test_start_live_voice_loads_uninitialized_live_engine(monkeypatch):
    """_start_live_voice should load a registered live-capable engine on demand."""
    import asyncio
    import interface.discord_interface as _mod

    guild_id = 193
    channel_id = 194
    manager_started: list[dict[str, object]] = []
    load_calls: list[str] = []

    class FakeManager:
        def set_audio_callback(self, cb):
            pass

        def set_text_callback(self, cb):
            pass

        def set_tool_executor(self, executor):
            pass

        def set_turn_complete_callback(self, cb):
            pass

        def set_reconnect_failed_callback(self, cb):
            pass

        async def start_session(self, **kwargs) -> bool:
            manager_started.append(kwargs)
            return True

    fake_manager = FakeManager()

    class FakeLiveEngine:
        def get_live_session_manager(self):
            return fake_manager

    fake_live_engine = FakeLiveEngine()

    class FakeRegistry:
        def get_engines_by_cortex(self, cortex):
            return ["gemini_api"] if cortex == "live" else []

        def get_available_engines(self):
            return ["gemini_api"]

        def get_engine(self, name):
            return None

        def load_engine(self, name):
            load_calls.append(name)
            if name == "gemini_api":
                return fake_live_engine
            raise ValueError(name)

    monkeypatch.setattr(_mod, "_HAS_VOICE_RECV", True, raising=False)
    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", lambda: FakeRegistry()
    )
    monkeypatch.setattr(
        "core.config_manager.config_registry.get_value",
        lambda key, default=None, **kwargs: default,
    )

    import types

    monkeypatch.setattr(
        _mod, "voice_recv", types.SimpleNamespace(VoiceRecvClient=object), raising=False
    )

    fake_vc = SimpleNamespace(
        channel=SimpleNamespace(id=channel_id, name="voice-test"),
        is_connected=lambda: True,
        is_playing=lambda: False,
        is_listening=lambda: True,
        play=lambda src: None,
        listen=lambda sink: None,
        move_to=lambda ch: None,
    )
    fake_guild = SimpleNamespace(id=guild_id, voice_client=None)
    fake_channel = SimpleNamespace(id=channel_id, name="voice-test", guild=fake_guild)

    async def fake_connect(cls=None):
        return fake_vc

    fake_channel.connect = fake_connect
    fake_client = SimpleNamespace(
        get_channel=lambda cid: fake_channel,
        loop=asyncio.get_event_loop(),
    )

    async def fake_system_instruction(attachment_context=None):
        return "system prompt"

    monkeypatch.setattr(
        "core.prompt_engine.build_live_system_instruction",
        fake_system_instruction,
    )

    try:
        from engines.live.live_base import LiveSessionManager as _LSM2

        async def noop_activate(self, path, gid, rejoin_callback=None):
            pass

        monkeypatch.setattr(_LSM2, "activate_live_for_path", noop_activate)
    except Exception:
        pass

    di = DiscordInterface(bot_token="")
    di.client = fake_client

    result = await di._start_live_voice(channel_id)

    assert result["status"] == "success"
    assert load_calls == ["gemini_api"]
    assert manager_started, "manager.start_session must run after on-demand load"
