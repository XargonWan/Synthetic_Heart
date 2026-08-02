import asyncio

import pytest

from interface import matrix_interface as mi


class DummyClient:
    def __init__(
        self, homeserver, user_id, device_id=None, store_path=None, config=None
    ):
        self.rooms = {}
        self.user_id = user_id
        self.device_id = device_id

    def add_event_callback(self, *args, **kwargs):
        # noop for tests
        return None


def test_interface_initializes_without_credentials(monkeypatch):
    """The interface should remain enabled/visible even if no password/token
    are configured; it must not be forcibly disabled by the constructor.
    """
    monkeypatch.setattr(mi, "AsyncClient", DummyClient)

    # Prevent constructor from trying to schedule start() via event loop
    monkeypatch.setattr(
        asyncio, "get_running_loop", lambda: (_ for _ in ()).throw(RuntimeError())
    )
    monkeypatch.setattr(
        asyncio, "get_event_loop", lambda: (_ for _ in ()).throw(Exception())
    )

    inst = mi.MatrixInterface("https://matrix.org/homeserver", "@tester:matrix.org")

    assert inst.is_enabled is True
    assert inst._auth_configured is False
    assert inst._logged_in is False
    # auto_join should be set from default config
    assert getattr(inst, "auto_join", True) is True


@pytest.mark.asyncio
async def test_start_skips_when_no_credentials(monkeypatch):
    monkeypatch.setattr(mi, "AsyncClient", DummyClient)

    # Ensure message_queue.run is a fast noop for the test
    async def _noop_run(*a, **k):
        return None

    monkeypatch.setattr(mi.message_queue, "run", _noop_run)

    # Prevent constructor from scheduling a background start
    monkeypatch.setattr(
        asyncio, "get_running_loop", lambda: (_ for _ in ()).throw(RuntimeError())
    )
    monkeypatch.setattr(
        asyncio, "get_event_loop", lambda: (_ for _ in ()).throw(Exception())
    )

    inst = mi.MatrixInterface("https://matrix.org/homeserver", "@tester:matrix.org")

    # start() should return early because credentials are not configured
    await inst.start()
    assert inst._sync_task is None


@pytest.mark.asyncio
async def test_private_message_trainer_only_ignores_non_trusted(monkeypatch):
    monkeypatch.setattr(mi, "AsyncClient", DummyClient)

    # Prevent constructor from scheduling start()
    monkeypatch.setattr(
        asyncio, "get_running_loop", lambda: (_ for _ in ()).throw(RuntimeError())
    )
    monkeypatch.setattr(
        asyncio, "get_event_loop", lambda: (_ for _ in ()).throw(Exception())
    )

    # Ensure message_queue.run is a fast noop
    async def _noop_run(*a, **k):
        return None

    monkeypatch.setattr(mi.message_queue, "run", _noop_run)

    # No trusted users
    monkeypatch.setattr(mi, "get_matrix_trusted_users", lambda: set())

    inst = mi.MatrixInterface("https://matrix.org/homeserver", "@tester:matrix.org")

    # Dummy room/event representing a private chat
    class Room:
        room_id = "!r1:matrix.org"
        member_count = 1
        canonical_alias = None

    class Event:
        sender = "@stranger:matrix.org"
        body = "hello"
        event_id = "e1"
        server_timestamp = None

    # Patch add_message_to_context so DB isn't touched
    async def _fake_add_message_to_context(*a, **k):
        return None

    import core.chat_context_manager as ccm

    monkeypatch.setattr(ccm, "add_message_to_context", _fake_add_message_to_context)

    called = []

    async def fake_enqueue(bot, wrapped, interface_id=None):
        called.append((bot, wrapped, interface_id))

    monkeypatch.setattr(mi.message_queue, "enqueue", fake_enqueue)

    await inst._on_message(Room(), Event())
    assert called == []


@pytest.mark.asyncio
async def test_private_message_trainer_only_allows_trusted_user(monkeypatch):
    monkeypatch.setattr(mi, "AsyncClient", DummyClient)
    monkeypatch.setattr(
        asyncio, "get_running_loop", lambda: (_ for _ in ()).throw(RuntimeError())
    )
    monkeypatch.setattr(
        asyncio, "get_event_loop", lambda: (_ for _ in ()).throw(Exception())
    )

    async def _noop_run(*a, **k):
        return None

    monkeypatch.setattr(mi.message_queue, "run", _noop_run)

    # Trusted user present
    monkeypatch.setattr(mi, "get_matrix_trusted_users", lambda: {"@trusted:matrix.org"})

    inst = mi.MatrixInterface("https://matrix.org/homeserver", "@tester:matrix.org")

    class Room:
        room_id = "!r2:matrix.org"
        member_count = 1
        canonical_alias = None

    class Event:
        sender = "@trusted:matrix.org"
        body = "hello"
        event_id = "e2"
        server_timestamp = None

    async def _fake_add_message_to_context(*a, **k):
        return None

    import core.chat_context_manager as ccm

    monkeypatch.setattr(ccm, "add_message_to_context", _fake_add_message_to_context)

    called = []

    async def fake_enqueue(bot, wrapped, interface_id=None):
        called.append((bot, wrapped, interface_id))

    monkeypatch.setattr(mi.message_queue, "enqueue", fake_enqueue)

    await inst._on_message(Room(), Event())
    assert len(called) == 1


@pytest.mark.asyncio
async def test_invite_policy_trainer_only_ignores_non_trainer_invite(monkeypatch):
    monkeypatch.setattr(mi, "AsyncClient", DummyClient)
    monkeypatch.setattr(
        asyncio, "get_running_loop", lambda: (_ for _ in ()).throw(RuntimeError())
    )
    monkeypatch.setattr(
        asyncio, "get_event_loop", lambda: (_ for _ in ()).throw(Exception())
    )

    inst = mi.MatrixInterface("https://matrix.org/homeserver", "@tester:matrix.org")

    # Ensure auto_join is enabled but invite policy is trainer_only by default
    inst.auto_join = True
    inst.invite_policy = "trainer_only"

    # Patch client.join to record calls
    calls = []

    async def fake_join(room_id):
        calls.append(room_id)

    inst.client.join = fake_join

    class Room:
        room_id = "!invite1:matrix.org"

    class Event:
        membership = "invite"
        sender = "@random:matrix.org"

    # No trusted users
    monkeypatch.setattr(mi, "get_matrix_trusted_users", lambda: set())

    await inst._on_invite(Room(), Event())
    assert calls == []


@pytest.mark.asyncio
async def test_invite_policy_trainer_only_autojoins_trusted_invite(monkeypatch):
    monkeypatch.setattr(mi, "AsyncClient", DummyClient)
    monkeypatch.setattr(
        asyncio, "get_running_loop", lambda: (_ for _ in ()).throw(RuntimeError())
    )
    monkeypatch.setattr(
        asyncio, "get_event_loop", lambda: (_ for _ in ()).throw(Exception())
    )

    inst = mi.MatrixInterface("https://matrix.org/homeserver", "@tester:matrix.org")
    inst.auto_join = True
    inst.invite_policy = "trainer_only"

    calls = []

    async def fake_join(room_id):
        calls.append(room_id)

    inst.client.join = fake_join

    class Room:
        room_id = "!invite2:matrix.org"

    class Event:
        membership = "invite"
        sender = "@trusted:matrix.org"

    monkeypatch.setattr(mi, "get_matrix_trusted_users", lambda: {"@trusted:matrix.org"})

    await inst._on_invite(Room(), Event())
    assert calls == ["!invite2:matrix.org"]


@pytest.mark.asyncio
async def test_reload_from_config_triggers_short_sync_and_updates_policies(monkeypatch):
    """reload_from_config should apply policy changes and perform a short sync when logged in."""
    monkeypatch.setattr(mi, "AsyncClient", DummyClient)

    # Prevent constructor from scheduling start()
    monkeypatch.setattr(
        asyncio, "get_running_loop", lambda: (_ for _ in ()).throw(RuntimeError())
    )
    monkeypatch.setattr(
        asyncio, "get_event_loop", lambda: (_ for _ in ()).throw(Exception())
    )

    inst = mi.MatrixInterface("https://matrix.org/homeserver", "@tester:matrix.org")

    # Mark the instance as authenticated and provide a fake sync() implementation
    inst._logged_in = True
    inst._auth_configured = True

    sync_calls = []

    async def fake_sync(*args, **kwargs):
        sync_calls.append(True)

    inst.client.sync = fake_sync

    # Change registry values (ConfigVar wrappers reflect these changes).
    await mi.config_registry.set_value("MATRIX_PRIVATE_MESSAGES", "allow_all")
    await mi.config_registry.set_value("MATRIX_INVITE_POLICY", "allow_all")

    # Now explicitly call reload_from_config and assert effects
    await inst.reload_from_config()

    assert inst.private_message_policy == "allow_all"
    assert inst.invite_policy == "allow_all"
    assert len(sync_calls) >= 1


@pytest.mark.asyncio
async def test_config_update_triggers_global_instance_reload_listener(monkeypatch):
    """Changing a MATRIX_* exposed var should schedule reload on the global instance."""
    called = []

    class FakeGlobal:
        async def reload_from_config(self):
            called.append(True)

    # Replace the global instance with our fake and trigger a config change
    monkeypatch.setattr(mi, "MATRIX_INTERFACE_INSTANCE", FakeGlobal())

    await mi.config_registry.set_value("MATRIX_PRIVATE_MESSAGES", "trainer_only")

    # allow scheduled task to run
    await asyncio.sleep(0.01)

    assert called == [True]


# ---------------------------------------------------------------------------
# _send_matrix_message delivery-outcome contract (Bug #1 regression)
#
# The method must return a real bool: True only when matrix-nio confirms an
# event_id, False when the send fails (matrix-nio returns an ErrorResponse
# instead of raising) or raises. It must never mask a failed send as success.
# ---------------------------------------------------------------------------


def _make_matrix_instance(monkeypatch):
    monkeypatch.setattr(mi, "AsyncClient", DummyClient)
    monkeypatch.setattr(
        asyncio, "get_running_loop", lambda: (_ for _ in ()).throw(RuntimeError())
    )
    monkeypatch.setattr(
        asyncio, "get_event_loop", lambda: (_ for _ in ()).throw(Exception())
    )
    return mi.MatrixInterface("https://matrix.org/homeserver", "@tester:matrix.org")


@pytest.mark.asyncio
async def test_send_matrix_message_returns_true_on_event_id(monkeypatch):
    inst = _make_matrix_instance(monkeypatch)

    class OkResponse:
        event_id = "$evt:matrix.org"

    async def fake_room_send(**kwargs):
        return OkResponse()

    inst.client.room_send = fake_room_send

    ok = await inst._send_matrix_message("!room:matrix.org", "hello")
    assert ok is True


@pytest.mark.asyncio
async def test_send_matrix_message_returns_false_on_error_response(monkeypatch):
    inst = _make_matrix_instance(monkeypatch)

    class ErrorResponse:
        # matrix-nio ErrorResponse has no truthy event_id
        event_id = None
        message = "M_FORBIDDEN"

    async def fake_room_send(**kwargs):
        return ErrorResponse()

    inst.client.room_send = fake_room_send

    ok = await inst._send_matrix_message("!room:matrix.org", "hello")
    assert ok is False


@pytest.mark.asyncio
async def test_send_matrix_message_returns_false_on_exception(monkeypatch):
    inst = _make_matrix_instance(monkeypatch)

    async def fake_room_send(**kwargs):
        raise RuntimeError("network down")

    inst.client.room_send = fake_room_send

    ok = await inst._send_matrix_message("!room:matrix.org", "hello")
    assert ok is False


@pytest.mark.asyncio
async def test_send_matrix_message_returns_false_without_client(monkeypatch):
    inst = _make_matrix_instance(monkeypatch)
    inst.client = None

    ok = await inst._send_matrix_message("!room:matrix.org", "hello")
    assert ok is False
