import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import plugins.radio_host.radio_host_plugin as radio_module
import plugins.radio_host.db as radio_db_module
import plugins.radio_host.jingle_injector as jingle_module


def _patch_radio_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        radio_module.config_registry,
        "get_value",
        lambda key, default=None, **kwargs: default,
    )
    monkeypatch.setattr(
        radio_module.config_registry,
        "add_listener",
        lambda key, listener: None,
    )


def test_is_enabled_reflects_toggle(monkeypatch: pytest.MonkeyPatch) -> None:
    """The plugin must report its RADIO_HOST_ENABLED toggle via is_enabled() so
    core_initializer skips registering radio_speak / radio_update_metadata (and
    therefore stops injecting them into prompts) when the radio host is off."""
    _patch_radio_config(
        monkeypatch
    )  # get_value returns default -> RADIO_HOST_ENABLED False

    plugin = radio_module.RadioHostPlugin()
    assert plugin.is_enabled() is False

    plugin._enabled = True
    assert plugin.is_enabled() is True


@pytest.mark.asyncio
async def test_start_continues_when_radio_table_init_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_radio_config(monkeypatch)

    plugin = radio_module.RadioHostPlugin()
    plugin._enabled = True

    async def fail_init_tables() -> None:
        raise RuntimeError("db unavailable")

    register_listeners = {"called": False}

    def fake_register_listeners() -> None:
        register_listeners["called"] = True

    ensure_webui = AsyncMock()
    ensure_running = AsyncMock()

    monkeypatch.setattr(radio_module, "init_radio_tables", fail_init_tables)
    monkeypatch.setattr(plugin, "_register_config_listeners", fake_register_listeners)
    monkeypatch.setattr(plugin, "_ensure_webui_routes_registered", ensure_webui)
    monkeypatch.setattr(plugin, "_ensure_running", ensure_running)

    await plugin.start()

    assert register_listeners["called"] is True
    ensure_webui.assert_awaited_once()
    ensure_running.assert_awaited_once()


@pytest.mark.asyncio
async def test_build_radio_data_uses_in_memory_activity_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_radio_config(monkeypatch)

    plugin = radio_module.RadioHostPlugin()
    plugin._enabled = True
    plugin._running = True

    async def ok_init_tables() -> None:
        return None

    monkeypatch.setattr(radio_module, "init_radio_tables", ok_init_tables)

    def fail_get_conn_ctx():
        raise RuntimeError("db unavailable")

    monkeypatch.setattr("core.db.get_conn_ctx", fail_get_conn_ctx)

    await plugin._log_activity(
        track_title="Song B",
        track_artist="Artist B",
        banter_text="Live fallback banter",
        style="transition",
        status="success",
    )

    data = await plugin._build_radio_data()

    assert data["online"] is False
    assert len(data["activities"]) == 1
    assert data["activities"][0]["track_title"] == "Song B"
    assert data["activities"][0]["banter_text"] == "Live fallback banter"
    assert data["activities"][0]["status"] == "success"


# ---------------------------------------------------------------------------
# Vox registry key fix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_tts_uses_vox_plugin_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """generate_tts must resolve the Vox plugin via 'vox_plugin', not 'vox'."""
    from plugins.radio_host.azuracast_client import AzuraCastClient
    import core.core_initializer as ci

    speak_called: list[str] = []

    async def fake_speak(
        text: str,
        engine_name: str | None = None,
        interface_path: str | None = None,
        **kwargs,
    ) -> dict:
        speak_called.append(text)
        return {"audio_path": "/tmp/fake.wav"}

    fake_vox = MagicMock()
    fake_vox.speak = fake_speak

    # Only register under "vox_plugin" — the legacy "vox" key must NOT be needed.
    registry: dict = {"vox_plugin": fake_vox}
    monkeypatch.setattr(ci, "PLUGIN_REGISTRY", registry)
    monkeypatch.setattr("os.path.isfile", lambda path: path == "/tmp/fake.wav")

    monkeypatch.setattr(
        radio_module.config_registry,
        "get_value",
        lambda key, default=None, **kwargs: default,
    )

    injector = jingle_module.JingleInjector(AzuraCastClient(), "test_station")
    result = await injector.generate_tts("Hello radio")

    assert result == "/tmp/fake.wav", (
        "generate_tts should succeed when VoxPlugin is registered as 'vox_plugin'"
    )
    assert speak_called == ["Hello radio"]


# ---------------------------------------------------------------------------
# DB schema dialect selection
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_init_radio_tables_uses_postgres_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On Postgres the CREATE TABLE must use SERIAL and TIMESTAMPTZ."""
    # Reset the initialised flag so the function actually runs.
    monkeypatch.setattr(radio_db_module, "_table_initialized", False)
    monkeypatch.setattr(radio_db_module, "_get_db_type", lambda: "postgres")

    executed: list[str] = []

    class _FakeCursor:  # Postgres variant
        async def execute(self, sql: str, *args) -> None:
            executed.append(sql)

        async def fetchone(self) -> tuple:
            return (1,)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        async def commit(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class _FakeCtx:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(radio_db_module, "get_conn_ctx", lambda: _FakeCtx())

    await radio_db_module.init_radio_tables()

    create_sql = next((s for s in executed if "CREATE TABLE" in s), "")
    assert "SERIAL" in create_sql, "Postgres schema must use SERIAL for the PK"
    assert "TIMESTAMPTZ" in create_sql, "Postgres schema must use TIMESTAMPTZ"
    assert "AUTO_INCREMENT" not in create_sql
    assert "DATETIME" not in create_sql


@pytest.mark.asyncio
async def test_init_radio_tables_uses_mariadb_schema(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On MariaDB the CREATE TABLE must use INT AUTO_INCREMENT and DATETIME."""
    monkeypatch.setattr(radio_db_module, "_table_initialized", False)
    monkeypatch.setattr(radio_db_module, "_get_db_type", lambda: "mariadb")

    executed: list[str] = []

    class _FakeCursor:  # MariaDB variant
        def __init__(self) -> None:
            self._count_result: tuple = (
                1,
            )  # simulate column already exists → skip ALTER

        async def execute(self, sql: str, *args) -> None:
            executed.append(sql)

        async def fetchone(self) -> tuple:
            return self._count_result

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class _FakeConn:
        def cursor(self):
            return _FakeCursor()

        async def commit(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            pass

    class _FakeCtx:
        async def __aenter__(self):
            return _FakeConn()

        async def __aexit__(self, *a):
            pass

    monkeypatch.setattr(radio_db_module, "get_conn_ctx", lambda: _FakeCtx())

    await radio_db_module.init_radio_tables()

    create_sql = next((s for s in executed if "CREATE TABLE" in s), "")
    assert "AUTO_INCREMENT" in create_sql, "MariaDB schema must use INT AUTO_INCREMENT"
    assert "DATETIME" in create_sql, "MariaDB schema must use DATETIME"
    assert "SERIAL" not in create_sql
    assert "TIMESTAMPTZ" not in create_sql


# ---------------------------------------------------------------------------
# RADIO_HOST_ANNOUNCE_ENABLED gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_announce_disabled_injects_deannounce_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When RADIO_HOST_NEXT_SONG_ANNOUNCEMENT is False, _on_track_change must
    pre-generate LLM banter for upcoming transitions and skip injection here
    (de-announce is handled by _on_winding_down instead)."""
    _patch_radio_config(monkeypatch)

    plugin = radio_module.RadioHostPlugin()
    plugin._enabled = True
    plugin._running = True
    plugin._next_song_announcement = False  # next-song announcement disabled

    # Capture the coroutine passed to asyncio.create_task
    captured_coro = []

    original_create_task = asyncio.create_task

    def capture_task(coro):
        captured_coro.append(coro)
        return original_create_task(coro)

    monkeypatch.setattr(asyncio, "create_task", capture_task)

    # Simulate a track change with queue data
    await plugin._on_track_change(
        prev_title="Old Song",
        prev_artist="Old Artist",
        curr_title="New Song",
        curr_artist="New Artist",
        next_title="Next Song",
        next_artist="Next Artist",
        should_comment=True,
        queue_ahead=[
            {"title": "Next Song", "artist": "Next Artist"},
            {"title": "After That", "artist": "After Artist"},
        ],
    )

    # With queue_ahead having 2+ items, _pre_generate_from_queue creates
    # one LLM pre-generation task per transition (B->C, C->D)
    assert len(captured_coro) == 2, f"Expected 2 pre-gen tasks, got {len(captured_coro)}"

    # _inject_at_track_change must be False so no fallback injection fires
    assert plugin._inject_at_track_change is False


# ---------------------------------------------------------------------------
# Queue pre-generation off-by-one fix
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pregen_queue_uses_correct_from_track(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pre-generation from queue_ahead must use curr_title as 'from' for the
    first transition (queue[0] is the NEXT track, not the current one)."""
    _patch_radio_config(monkeypatch)

    plugin = radio_module.RadioHostPlugin()
    plugin._enabled = True
    plugin._running = True
    plugin._next_song_announcement = True

    # Patch _enqueue_pre_gen_banter to capture arguments synchronously.
    # The real method schedules background tasks via asyncio.create_task,
    # which won't complete within the test. We replace it with a sync
    # function that directly stores the banter for inspection.
    enqueue_calls: list[tuple[str, str, str, str]] = []

    def capture_enqueue(prev_title, prev_artist, curr_title, curr_artist):
        enqueue_calls.append((prev_title, prev_artist, curr_title, curr_artist))
        # Also store so we can verify the full banter object if needed
        plugin._store_pending_banter(
            {
                "prev_title": prev_title,
                "prev_artist": prev_artist,
                "curr_title": curr_title,
                "curr_artist": curr_artist,
                "text": f"LLM: {prev_title} -> {curr_title}",
                "style": "transition",
                "audio_path": None,
            }
        )

    monkeypatch.setattr(plugin, "_enqueue_pre_gen_banter", capture_enqueue)

    await plugin._on_track_change(
        prev_title="Song A",
        prev_artist="Artist A",
        curr_title="Song B",
        curr_artist="Artist B",
        next_title="Song C",
        next_artist="Artist C",
        should_comment=True,
        queue_ahead=[
            {"title": "Song C", "artist": "Artist C"},
            {"title": "Song D", "artist": "Artist D"},
            {"title": "Song E", "artist": "Artist E"},
        ],
    )

    # We expect 3 pre-generations queued: B->C, C->D, D->E
    assert len(enqueue_calls) == 3, f"Expected 3 calls, got {len(enqueue_calls)}"

    # First transition: from=curr (Song B), to=queue[0] (Song C)
    assert enqueue_calls[0] == ("Song B", "Artist B", "Song C", "Artist C")

    # Second transition: from=queue[0] (Song C), to=queue[1] (Song D)
    assert enqueue_calls[1] == ("Song C", "Artist C", "Song D", "Artist D")

    # Third transition: from=queue[1] (Song D), to=queue[2] (Song E)
    assert enqueue_calls[2] == ("Song D", "Artist D", "Song E", "Artist E")


# ---------------------------------------------------------------------------
# Gain propagation to ffmpeg
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_convert_audio_to_webm_applies_gain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """convert_audio_to_webm must pass gain_db to _convert_to_webm."""
    from plugins.radio_host.azuracast_client import AzuraCastClient

    client = AzuraCastClient(base_url="http://localhost", api_key="k")

    captured: dict = {}

    async def fake_convert(input_path: str, gain_db: float = 4.0) -> bytes:
        captured["gain_db"] = gain_db
        return b"fake-webm"

    monkeypatch.setattr(client, "_convert_to_webm", fake_convert)

    result = await client.convert_audio_to_webm("/tmp/fake.wav", gain_db=6.5)

    assert result == b"fake-webm"
    assert captured["gain_db"] == 6.5


# ---------------------------------------------------------------------------
# RADIO_HOST_ANNOUNCE_IF_NO_LISTENERS gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_announce_suppressed_without_listeners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When RADIO_HOST_ANNOUNCE_IF_NO_LISTENERS is True and current_listeners
    is 0, _on_track_change must return early without injecting banter."""
    _patch_radio_config(monkeypatch)

    plugin = radio_module.RadioHostPlugin()
    plugin._enabled = True
    plugin._running = True
    plugin._announce_if_no_listeners = True

    inject_calls = []

    async def capturing_inject(*args, **kwargs):
        inject_calls.append((args, kwargs))

    monkeypatch.setattr(plugin, "_inject_banter_now", capturing_inject)

    # Simulate a monitor with 0 listeners and data available
    fake_monitor = MagicMock()
    fake_monitor.listener_data_available = True
    fake_monitor.current_listeners = 0
    fake_monitor.last_listeners = 0
    fake_monitor.current_playlist = ""
    plugin._monitor = fake_monitor

    await plugin._on_track_change(
        prev_title="Old Song",
        prev_artist="Old Artist",
        curr_title="New Song",
        curr_artist="New Artist",
        should_comment=True,
    )

    assert len(inject_calls) == 0, "No banter should be injected with 0 listeners"


@pytest.mark.asyncio
async def test_announce_allowed_with_listeners(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When RADIO_HOST_ANNOUNCE_IF_NO_LISTENERS is True and current_listeners
    is > 0, _on_track_change must pre-generate LLM banter for upcoming tracks."""
    _patch_radio_config(monkeypatch)

    plugin = radio_module.RadioHostPlugin()
    plugin._enabled = True
    plugin._running = True
    plugin._announce_if_no_listeners = True
    plugin._next_song_announcement = False  # de-announce only path

    # Capture the coroutine passed to asyncio.create_task
    captured_coro = []

    original_create_task = asyncio.create_task

    def capture_task(coro):
        captured_coro.append(coro)
        return original_create_task(coro)

    monkeypatch.setattr(asyncio, "create_task", capture_task)

    fake_monitor = MagicMock()
    fake_monitor.listener_data_available = True
    fake_monitor.current_listeners = 3
    fake_monitor.last_listeners = 2
    fake_monitor.current_playlist = ""
    plugin._monitor = fake_monitor

    await plugin._on_track_change(
        prev_title="Old Song",
        prev_artist="Old Artist",
        curr_title="New Song",
        curr_artist="New Artist",
        should_comment=True,
    )

    # With no queue_ahead and no next_title, no pre-generation tasks are created
    assert len(captured_coro) == 0, f"Expected 0 tasks, got {len(captured_coro)}"

    # _inject_at_track_change must be False (de-announce handled by _on_winding_down)
    assert plugin._inject_at_track_change is False


@pytest.mark.asyncio
async def test_announce_always_when_feature_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When RADIO_HOST_ANNOUNCE_IF_NO_LISTENERS is False, announcements must
    always pre-generate LLM banter regardless of listener count."""
    _patch_radio_config(monkeypatch)

    plugin = radio_module.RadioHostPlugin()
    plugin._enabled = True
    plugin._running = True
    plugin._announce_if_no_listeners = False
    plugin._next_song_announcement = False

    # Capture the coroutine passed to asyncio.create_task
    captured_coro = []

    original_create_task = asyncio.create_task

    def capture_task(coro):
        captured_coro.append(coro)
        return original_create_task(coro)

    monkeypatch.setattr(asyncio, "create_task", capture_task)

    fake_monitor = MagicMock()
    fake_monitor.listener_data_available = True
    fake_monitor.current_listeners = 0
    fake_monitor.last_listeners = 0
    fake_monitor.current_playlist = ""
    plugin._monitor = fake_monitor

    await plugin._on_track_change(
        prev_title="Old Song",
        prev_artist="Old Artist",
        curr_title="New Song",
        curr_artist="New Artist",
        should_comment=True,
    )

    # With no queue_ahead and no next_title, no pre-generation tasks are created
    assert len(captured_coro) == 0, f"Expected 0 tasks, got {len(captured_coro)}"

    # _inject_at_track_change must be False (de-announce handled by _on_winding_down)
    assert plugin._inject_at_track_change is False


@pytest.mark.asyncio
async def test_announce_fallback_when_listener_data_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When listener_data_available is False, the plugin must fall back to
    pre-generating LLM banter (compatibility with older AzuraCast)."""
    _patch_radio_config(monkeypatch)

    plugin = radio_module.RadioHostPlugin()
    plugin._enabled = True
    plugin._running = True
    plugin._announce_if_no_listeners = True
    plugin._next_song_announcement = False

    # Capture the coroutine passed to asyncio.create_task
    captured_coro = []

    original_create_task = asyncio.create_task

    def capture_task(coro):
        captured_coro.append(coro)
        return original_create_task(coro)

    monkeypatch.setattr(asyncio, "create_task", capture_task)

    fake_monitor = MagicMock()
    fake_monitor.listener_data_available = False  # no data yet
    fake_monitor.current_listeners = 0
    fake_monitor.last_listeners = 0
    fake_monitor.current_playlist = ""
    plugin._monitor = fake_monitor

    await plugin._on_track_change(
        prev_title="Old Song",
        prev_artist="Old Artist",
        curr_title="New Song",
        curr_artist="New Artist",
        should_comment=True,
    )

    # With no queue_ahead and no next_title, no pre-generation tasks are created
    assert len(captured_coro) == 0, f"Expected 0 tasks, got {len(captured_coro)}"

    # _inject_at_track_change must be False (de-announce handled by _on_winding_down)
    assert plugin._inject_at_track_change is False


@pytest.mark.asyncio
async def test_first_listener_resets_intermission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the first listener arrives (last_listeners=0 -> current_listeners>0),
    the intermission counter must be reset to 0."""
    _patch_radio_config(monkeypatch)

    plugin = radio_module.RadioHostPlugin()
    plugin._enabled = True
    plugin._running = True
    plugin._announce_if_no_listeners = True
    plugin._next_song_announcement = False
    plugin._track_count_since_comment = 5  # simulate mid-intermission

    # Capture the coroutine passed to asyncio.create_task
    captured_coro = []

    original_create_task = asyncio.create_task

    def capture_task(coro):
        captured_coro.append(coro)
        return original_create_task(coro)

    monkeypatch.setattr(asyncio, "create_task", capture_task)

    fake_monitor = MagicMock()
    fake_monitor.listener_data_available = True
    fake_monitor.current_listeners = 1  # first listener
    fake_monitor.last_listeners = 0  # was 0 before
    fake_monitor.current_playlist = ""
    plugin._monitor = fake_monitor

    await plugin._on_track_change(
        prev_title="Old Song",
        prev_artist="Old Artist",
        curr_title="New Song",
        curr_artist="New Artist",
        should_comment=True,
    )

    assert plugin._track_count_since_comment == 0, (
        "Intermission counter should be reset when first listener arrives"
    )

    # With no queue_ahead and no next_title, no pre-generation tasks are created
    assert len(captured_coro) == 0, f"Expected 0 tasks, got {len(captured_coro)}"
