"""The AzuraCast Radio DJ extension: its behaviour, against a fake ``ctx``.

The scheduling rules (when to speak, when a de-announce is stale, where the clean gap is)
are the ones the in-tree plugin had, carried over with their tests; what is new is the way
it reaches the Core: a scoped background turn, the synth's voice, the stage, a Recon
question and the Radio window's routes.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

SOURCE = Path(__file__).resolve().parent.parent / "extensions_src" / "radio_host"
_NAMES = (
    "radiolog",
    "azuracast_client",
    "track_monitor",
    "jingle",
    "radio",
    "ext_main",
)


def _load() -> dict[str, Any]:
    """Import the extension's modules the way its process does (its folder first on the
    path), then leave ``sys.modules`` as it was: names like ``radio`` are too generic to
    leave behind."""
    sys.path.insert(0, str(SOURCE))
    try:
        return {name: importlib.import_module(name) for name in _NAMES}
    finally:
        sys.path.remove(str(SOURCE))
        for name in _NAMES:
            sys.modules.pop(name, None)


_MODS = _load()
radio_module = _MODS["radio"]
azuracast_module = _MODS["azuracast_client"]
jingle_module = _MODS["jingle"]
ext_main_module = _MODS["ext_main"]


class FakeStorage:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.fail = False

    async def get(self, key: str, default: Any = None) -> Any:
        if self.fail:
            raise RuntimeError("storage down")
        return self.data.get(key, default)

    async def set(self, key: str, value: Any) -> None:
        if self.fail:
            raise RuntimeError("storage down")
        self.data[key] = value


class FakeTurns:
    def __init__(self) -> None:
        self.requests: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.error: Exception | None = None

    async def request(self, *args: Any, **kwargs: Any) -> None:
        if self.error:
            raise self.error
        self.requests.append((args, kwargs))


class FakeVoice:
    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.available = True
        self.spoken: list[tuple[str, str]] = []
        self.played: list[tuple[str, str, str]] = []

    async def synthesize(self, text: str, engine: str = "") -> Path | None:
        self.spoken.append((text, engine))
        if not self.available:
            return None
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"core_{len(self.spoken)}.wav"
        path.write_bytes(b"RIFF" + b"\x00" * 40)
        return path

    async def play(self, path: Any, text: str = "", engine: str = "") -> bool:
        self.played.append((str(path), text, engine))
        return True


class FakeCtx:
    def __init__(self, folder: Path, config: dict[str, Any] | None = None) -> None:
        self.data_dir = folder / "data"
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.values = dict(config or {})
        self.storage = FakeStorage()
        self.turns = FakeTurns()
        self.voice = FakeVoice(folder / "core")
        self.stopping = asyncio.Event()
        # set by the tests that need them
        self.interfaces: Any = None
        self.recon: Any = None
        self.web: Any = None
        self.lines: list[tuple[str, str]] = []
        self.log = SimpleNamespace(
            **{
                level: (
                    lambda message, level=level: self.lines.append((level, message))
                )
                for level in ("debug", "info", "warning", "error")
            }
        )
        ctx = self

        class Config:
            async def all(self) -> dict[str, Any]:
                return dict(ctx.values)

        class Core:
            async def get(self) -> dict[str, Any]:
                return {"synth_name": "Dee"}

        self.config = Config()
        self.core = Core()


_FOLDERS: list[Any] = []


def make_host(config: dict[str, Any] | None = None) -> Any:
    folder = tempfile.TemporaryDirectory()
    _FOLDERS.append(folder)  # alive for the whole test session
    ctx = FakeCtx(Path(folder.name), config)
    _MODS["radiolog"].bind(ctx.log)  # the extension's log lines land in ``ctx.lines``
    return radio_module.RadioHost(ctx)


CONFIGURED = {
    "AZURACAST_BASE_URL": "http://radio.local",
    "AZURACAST_API_KEY": "key",
    "AZURACAST_STATION_ID": "main",
}


# ---------------------------------------------------------------------------
# RADIO_HOST_ANNOUNCE_ENABLED gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_announce_disabled_injects_deannounce_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When RADIO_HOST_NEXT_SONG_ANNOUNCEMENT is False, _on_track_change must
    NOT pre-generate transition banter and must skip injection here.

    De-announce mode never wants pre-generated *transition* banter: the
    winding-down handler generates its own de-announce on the fly.  Worse,
    transition banter is keyed by its ``from`` track ``(prev_title,
    prev_artist)`` — the same key the de-announce generator polls — so a
    leftover transition entry gets popped and broadcast, airing an
    announcement of the NEXT song while the current one is still playing.
    """
    plugin = make_host()
    plugin._running = True
    plugin._next_song_announcement = False  # next-song announcement disabled

    # Capture the coroutine passed to asyncio.create_task
    captured_coro = []

    original_create_task = asyncio.create_task

    def capture_task(coro):
        captured_coro.append(coro)
        return original_create_task(coro)

    monkeypatch.setattr(plugin, "_spawn", capture_task)

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

    # In de-announce mode, no transition banter must be pre-generated —
    # otherwise a transition entry keyed on the current track collides with
    # the de-announce generator's poll key and gets broadcast, announcing
    # the NEXT song mid-track.
    assert len(captured_coro) == 0, (
        f"Expected 0 pre-gen tasks in de-announce mode, got {len(captured_coro)}"
    )

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
    plugin = make_host()
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
    AzuraCastClient = azuracast_module.AzuraCastClient

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
    plugin = make_host()
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
    plugin = make_host()
    plugin._running = True
    plugin._announce_if_no_listeners = True
    plugin._next_song_announcement = False  # de-announce only path

    # Capture the coroutine passed to asyncio.create_task
    captured_coro = []

    original_create_task = asyncio.create_task

    def capture_task(coro):
        captured_coro.append(coro)
        return original_create_task(coro)

    monkeypatch.setattr(plugin, "_spawn", capture_task)

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
    plugin = make_host()
    plugin._running = True
    plugin._announce_if_no_listeners = False
    plugin._next_song_announcement = False

    # Capture the coroutine passed to asyncio.create_task
    captured_coro = []

    original_create_task = asyncio.create_task

    def capture_task(coro):
        captured_coro.append(coro)
        return original_create_task(coro)

    monkeypatch.setattr(plugin, "_spawn", capture_task)

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
    plugin = make_host()
    plugin._running = True
    plugin._announce_if_no_listeners = True
    plugin._next_song_announcement = False

    # Capture the coroutine passed to asyncio.create_task
    captured_coro = []

    original_create_task = asyncio.create_task

    def capture_task(coro):
        captured_coro.append(coro)
        return original_create_task(coro)

    monkeypatch.setattr(plugin, "_spawn", capture_task)

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
    plugin = make_host()
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

    monkeypatch.setattr(plugin, "_spawn", capture_task)

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


# ---------------------------------------------------------------------------
# Stale de-announce abandonment
# ---------------------------------------------------------------------------


def _plugin_with_nowplaying(monkeypatch: pytest.MonkeyPatch, now_playing: dict) -> Any:
    plugin = make_host()
    plugin._running = True
    plugin._station_id = "1"

    fake_client = MagicMock()
    fake_client.get_nowplaying = AsyncMock(return_value=now_playing)
    plugin._client = fake_client
    return plugin


@pytest.mark.asyncio
async def test_deannounce_target_expired_when_later_track_on_air(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If banter generation outran the winding-down window and a different,
    full-length song is now on air, the de-announce target is expired and the
    injection must be abandoned (returns True)."""
    plugin = _plugin_with_nowplaying(
        monkeypatch,
        {
            "now_playing": {
                "song": {"title": "Saccio ca putesse"},
                "playlist": "default",
                "duration": 180,
            }
        },
    )

    expired = await plugin._deannounce_target_expired("I Tried")

    assert expired is True


@pytest.mark.asyncio
async def test_deannounce_target_not_expired_when_target_still_on_air(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the target song is still the currently-playing track, the
    de-announce is still valid (returns False)."""
    plugin = _plugin_with_nowplaying(
        monkeypatch,
        {
            "now_playing": {
                "song": {"title": "I Tried"},
                "playlist": "default",
                "duration": 180,
            }
        },
    )

    expired = await plugin._deannounce_target_expired("I Tried")

    assert expired is False


@pytest.mark.asyncio
async def test_deannounce_target_not_expired_during_short_bumper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A short bumper/jingle bridging the gap must NOT expire the de-announce:
    the target's transition gap is still the one we want to fill."""
    plugin = _plugin_with_nowplaying(
        monkeypatch,
        {
            "now_playing": {
                "song": {"title": "Station Bumper"},
                "playlist": "jingles",
                "duration": 12,
            }
        },
    )

    expired = await plugin._deannounce_target_expired("I Tried")

    assert expired is False


@pytest.mark.asyncio
async def test_deannounce_target_not_expired_on_fetch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient nowplaying fetch failure must never suppress a legitimate
    de-announce (fail-open: returns False)."""
    plugin = make_host()
    plugin._running = True
    plugin._station_id = "1"

    fake_client = MagicMock()
    fake_client.get_nowplaying = AsyncMock(side_effect=RuntimeError("api down"))
    plugin._client = fake_client

    expired = await plugin._deannounce_target_expired("I Tried")

    assert expired is False


@pytest.mark.asyncio
async def test_clean_gap_deferred_past_bumper_in_transition_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a short bumper/jingle is bridging the transition gap, the clean
    gap opens only when it finishes.  The resolved timestamp must be pushed
    into the future by roughly the bumper's remaining time so the spoken
    de-announce airs after the jingle, not overlapping it."""
    import time as _time

    plugin = _plugin_with_nowplaying(
        monkeypatch,
        {
            "now_playing": {
                "song": {"title": "Station Bumper"},
                "playlist": "jingles",
                "duration": 12,
                "elapsed": 4,
            }
        },
    )

    resolved = await plugin._resolve_clean_gap_end_ts(song_end_ts=0.0)

    # Bumper has ~8s remaining, so the clean gap opens ~8s from now.
    assert resolved > _time.time() + 6.0


@pytest.mark.asyncio
async def test_clean_gap_is_now_when_real_track_already_on_air(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a real (non-bumper) track is already on air when we check, the clean
    gap is now — the resolver targets the current moment rather than waiting."""
    import time as _time

    plugin = _plugin_with_nowplaying(
        monkeypatch,
        {
            "now_playing": {
                "song": {"title": "Some Full Song"},
                "playlist": "default",
                "duration": 180,
                "elapsed": 10,
            }
        },
    )

    resolved = await plugin._resolve_clean_gap_end_ts(song_end_ts=0.0)

    assert abs(resolved - _time.time()) < 2.0


@pytest.mark.asyncio
async def test_clean_gap_falls_back_to_song_end_on_fetch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A persistent nowplaying fetch failure must fail safe: the resolver
    returns the original song_end_ts so a legitimate de-announce still airs."""
    plugin = make_host()
    plugin._running = True
    plugin._station_id = "1"

    fake_client = MagicMock()
    fake_client.get_nowplaying = AsyncMock(side_effect=RuntimeError("api down"))
    plugin._client = fake_client

    async def _instant_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(radio_module.asyncio, "sleep", _instant_sleep)

    fallback = 12345.0
    resolved = await plugin._resolve_clean_gap_end_ts(song_end_ts=fallback)

    assert resolved == fallback


# ---------------------------------------------------------------------------
# New: how the radio reaches the Core
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_main_registers_the_interface_its_actions_and_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tempfile.TemporaryDirectory()
    _FOLDERS.append(folder)
    ctx = FakeCtx(Path(folder.name), CONFIGURED)
    calls: dict[str, Any] = {}

    async def register(interface_id: str, **kwargs: Any) -> dict[str, Any]:
        calls["register"] = (interface_id, kwargs)
        return {}

    ctx.interfaces = SimpleNamespace(
        register=register,
        on_action=lambda h: calls.__setitem__("action", h),
        on_send_message=lambda h: calls.__setitem__("send", h),
    )
    ctx.recon = SimpleNamespace(on_contribute=lambda h: calls.__setitem__("recon", h))
    ctx.web = SimpleNamespace(on_request=lambda h: calls.__setitem__("web", h))
    # the monitor would poll a station that does not exist: not part of this test
    monkeypatch.setattr(radio_module.RadioHost, "_ensure_running", AsyncMock())
    ctx.stopping.set()  # return as soon as everything is wired
    await ext_main_module.main(ctx)

    interface_id, kwargs = calls["register"]
    assert interface_id == "radio_host"
    assert set(kwargs["actions"]) == {"radio_speak", "radio_update_metadata"}
    assert kwargs["actions"]["radio_speak"]["fields"]["text"]["type"] == "string"
    assert {"action", "send", "recon", "web"} <= set(calls)


@pytest.mark.asyncio
async def test_banter_is_requested_as_a_scoped_background_turn() -> None:
    host = make_host({**CONFIGURED, "RADIO_HOST_LISTENER_HISTORY": 7})
    host._apply_config(host.ctx.values)
    host._station_name = "Radio Dee"
    await host._enqueue_banter_generation("A", "Art A", "B", "Art B", pre_generate=True)

    ((args, kwargs),) = host.ctx.turns.requests
    interface_id, path, text = args
    assert (interface_id, path) == ("radio_host", "radio_host/-2")
    assert text.count("'B'") >= 2 and "'A' by Art A" in text and "Radio Dee" in text
    assert "ONLY the 'radio_speak'" in text
    assert kwargs["allowed_actions"] == ["radio_speak"]
    assert kwargs["priority"] == "elevated"
    assert kwargs["history_recent_max"] == 7
    assert kwargs["context"] == {
        "current_track_title": "B",
        "current_track_artist": "Art B",
        "radio_pre_generating": True,
        "radio_pregenerate_prev_title": "A",
        "radio_pregenerate_prev_artist": "Art A",
        "radio_pregenerate_next_title": "B",
        "radio_pregenerate_next_artist": "Art B",
    }


@pytest.mark.asyncio
async def test_a_deannounce_turn_does_not_mention_the_next_song() -> None:
    host = make_host(CONFIGURED)
    host._apply_config(host.ctx.values)
    await host._enqueue_banter_generation(
        "A", "Art A", "", "", pre_generate=True, deannounce_only=True
    )
    ((args, _),) = host.ctx.turns.requests
    assert "JUST FINISHED: 'A'" in args[2] and "NOW PLAYING" not in args[2]


@pytest.mark.asyncio
async def test_a_refused_turn_is_logged_not_raised() -> None:
    host = make_host(CONFIGURED)
    host.ctx.turns.error = RuntimeError("rate limited")
    await host._enqueue_banter_generation("A", "a", "B", "b", pre_generate=True)
    assert any(
        level == "error" and "Failed to enqueue" in message
        for level, message in host.ctx.lines
    )


@pytest.mark.asyncio
async def test_a_pre_generated_remark_is_voiced_and_kept_for_its_transition() -> None:
    host = make_host(CONFIGURED)
    context = {
        "radio_pre_generating": True,
        "radio_pregenerate_prev_title": "A",
        "radio_pregenerate_prev_artist": "Art A",
        "radio_pregenerate_next_title": "B",
        "radio_pregenerate_next_artist": "Art B",
    }
    result = await host.handle_action(
        "radio_host", "radio_speak", {"text": "Next up: B"}, context
    )
    assert result["status"] == "success"
    banter = host._pop_matching_banter("A", "Art A")
    assert banter["text"] == "Next up: B" and banter["curr_title"] == "B"
    kept = Path(banter["audio_path"])
    assert kept.is_file() and host.ctx.data_dir in kept.parents  # the radio's own copy
    assert host.ctx.voice.played == []  # nothing goes on air yet


@pytest.mark.asyncio
async def test_speaking_on_air_broadcasts_shows_the_stage_and_logs() -> None:
    host = make_host(CONFIGURED)
    host._apply_config(host.ctx.values)
    host._synth_name = "Dee"
    host._client.convert_audio_to_webm = AsyncMock(return_value=b"webm-bytes")
    host._client.broadcast_webm = AsyncMock(return_value={"status": "success"})

    result = await host.handle_action(
        "radio_host",
        "radio_speak",
        {"text": "Hello listeners", "style": "intro"},
        {"current_track_title": "B", "current_track_artist": "Art B"},
    )

    assert result == {"status": "success", "message": "Hello listeners"}
    sent = host._client.broadcast_webm.await_args
    assert sent is not None
    assert sent.args[0] == b"webm-bytes"
    assert sent.kwargs["title"] == "Dee is speaking"
    ((wav, text, _engine),) = host.ctx.voice.played
    assert text == "Hello listeners"
    assert not Path(wav).exists()  # the working copy is cleaned up
    (activity,) = host._recent_activities
    assert (activity["track_title"], activity["style"], activity["status"]) == (
        "B",
        "intro",
        "success",
    )
    assert (host._audio_dir / activity["audio_file"]).read_bytes() == b"webm-bytes"
    assert host.ctx.storage.data["activities"][0]["id"] == activity["id"]


@pytest.mark.asyncio
async def test_a_missing_voice_is_a_failed_action_not_a_crash() -> None:
    host = make_host(CONFIGURED)
    host.ctx.voice.available = False
    result = await host.handle_action("radio_host", "radio_speak", {"text": "Hi"}, {})
    assert result["status"] == "failed" and "tts_failed" in result["message"]
    assert host.ctx.voice.played == []


@pytest.mark.asyncio
async def test_empty_speech_unknown_actions_and_foreign_interfaces_are_refused() -> (
    None
):
    host = make_host(CONFIGURED)
    assert (await host.handle_action("radio_host", "radio_speak", {"text": " "}, {}))[
        "status"
    ] == "skipped"
    assert (await host.handle_action("radio_host", "nope", {}, {}))[
        "status"
    ] == "failed"
    assert (await host.handle_action("other", "radio_speak", {"text": "x"}, {}))[
        "status"
    ] == "failed"


@pytest.mark.asyncio
async def test_metadata_is_updated_only_with_both_fields_and_a_station() -> None:
    host = make_host(CONFIGURED)
    host._apply_config(host.ctx.values)
    host._client.update_nowplaying_metadata = AsyncMock(return_value=True)
    ok = await host.handle_action(
        "radio_host",
        "radio_update_metadata",
        {"artist": "X", "title": "Y", "album": "Z"},
        {},
    )
    assert ok["status"] == "success"
    host._client.update_nowplaying_metadata.assert_awaited_with("main", "X", "Y", "Z")
    skipped = await host.handle_action(
        "radio_host", "radio_update_metadata", {"artist": "X"}, {}
    )
    assert skipped["status"] == "skipped"


@pytest.mark.asyncio
async def test_send_message_is_not_how_the_radio_speaks() -> None:
    host = make_host(CONFIGURED)
    assert await host.on_send_message("radio_host", {"text": "hi"}, {}) is False


# --- Recon ------------------------------------------------------------------


def test_the_models_verdict_is_read_strictly() -> None:
    is_relevant = radio_module.is_relevant
    assert is_relevant(True) and is_relevant({"radio_status": True})
    assert is_relevant({"radio_status": "yes"}) and is_relevant("TRUE")
    for no in (False, None, {}, {"radio_status": False}, "no", 3, [True]):
        assert not is_relevant(no)


@pytest.mark.asyncio
async def test_the_live_status_is_added_only_for_radio_questions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = make_host(CONFIGURED)
    status = {
        "online": True,
        "station_name": "Radio Dee",
        "current_track_title": "Song",
        "current_track_artist": "Artist",
        "next_track_title": "Next",
        "next_track_artist": "NA",
        "listeners": 4,
        "schedule_description": "Night show",
    }
    monkeypatch.setattr(host, "get_live_status", AsyncMock(return_value=status))
    (snippet,) = await host.on_contribute(
        "radio_status", {"radio_status": True}, "", ""
    )
    text = snippet["content"]
    for needle in (
        "Radio Dee",
        "Artist - Song",
        "NA - Next",
        "Listeners: 4",
        "Night show",
    ):
        assert needle in text
    assert (
        await host.on_contribute("radio_status", {"radio_status": False}, "", "") == []
    )
    assert await host.on_contribute("other_key", True, "", "") == []
    monkeypatch.setattr(host, "get_live_status", AsyncMock(return_value=None))
    assert await host.on_contribute("radio_status", True, "", "") == []


def test_an_offline_station_is_said_to_be_offline() -> None:
    assert "OFFLINE" in radio_module.format_status(
        {"online": False, "station_name": "Radio Dee"}
    )


@pytest.mark.asyncio
async def test_status_is_none_until_the_station_is_configured() -> None:
    assert await make_host().get_live_status() is None


# --- the Radio window -------------------------------------------------------


def request(path: str, method: str = "GET", **query: str) -> Any:
    return SimpleNamespace(method=method, path=path, query=query, headers={}, body=b"")


@pytest.mark.asyncio
async def test_the_window_reads_status_and_remarks_from_the_routes() -> None:
    host = make_host(CONFIGURED)
    host._apply_config(host.ctx.values)
    await host._log_activity(
        "T", "A", "words", "transition", "success", "banter_1.webm"
    )
    response = await host.on_web_request(request("data"))
    assert response.status == 200
    data = response.body
    assert data["configured"] is True and data["online"] is False
    (row,) = data["activities"]
    assert row["audio_url"] == f"api/audio?id={row['id']}"
    assert "audio_file" not in row  # the file name stays inside


@pytest.mark.asyncio
async def test_a_replay_clip_is_served_only_by_activity_id() -> None:
    host = make_host(CONFIGURED)
    host._audio_dir.mkdir(parents=True)
    (host._audio_dir / "banter_5.webm").write_bytes(b"clip")
    secret = host._audio_dir.parent / "secret.txt"
    secret.write_text("nope")
    await host._log_activity("T", "A", "w", "transition", "success", "banter_5.webm")
    await host._log_activity("T", "A", "w", "transition", "success", "../secret.txt")
    good, evil = (a["id"] for a in reversed(host._recent_activities))
    ok = await host.on_web_request(request("audio", id=str(good)))
    assert ok.status == 200 and ok.body == b"clip"
    assert ok.headers["content-type"] == "audio/webm"
    for bad in (str(evil), "abc", "../secret.txt", "999", ""):
        refused = await host.on_web_request(request("audio", id=bad))
        assert refused.status == 404


@pytest.mark.asyncio
async def test_the_window_routes_are_read_only() -> None:
    host = make_host(CONFIGURED)
    assert (await host.on_web_request(request("data", "POST"))).status == 405
    assert (await host.on_web_request(request("nothing"))).status == 404


# --- settings and storage ---------------------------------------------------


def test_settings_are_applied_with_safe_fallbacks() -> None:
    host = make_host()
    host._apply_config(
        {
            **CONFIGURED,
            "RADIO_HOST_POLL_INTERVAL_S": "30",
            "RADIO_HOST_INTERMISSION": "bad",
            "RADIO_HOST_GAIN_DB": 6.5,
            "RADIO_HOST_NEXT_SONG_ANNOUNCEMENT": True,
            "RADIO_HOST_ANNOUNCE_IF_NO_LISTENERS": False,
            "RADIO_HOST_ON_THE_FLY_TIMEOUT_S": 0,
            "AZURACAST_STREAMER_USERNAME": "",
        }
    )
    assert host._poll_interval == 30 and host._intermission == 1  # bad value: default
    assert host._gain_db == 6.5 and host._next_song_announcement is True
    assert host._announce_if_no_listeners is False
    assert host._on_the_fly_timeout_s == radio_module._ON_THE_FLY_BACKGROUND_TIMEOUT_S
    assert host._streamer_username == "SyntH"  # empty: default
    assert host._client.configured and host._station_id == "main"
    assert host._has_runtime_config()


def test_without_all_three_station_settings_nothing_runs() -> None:
    host = make_host()
    host._station_name = "stale"
    host._apply_config({"AZURACAST_BASE_URL": "http://x", "AZURACAST_API_KEY": "k"})
    assert not host._has_runtime_config() and host._station_name == ""


@pytest.mark.asyncio
async def test_changed_settings_restart_the_monitor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host = make_host(CONFIGURED)
    host._apply_config(host.ctx.values)
    host._config_seen = dict(host.ctx.values)
    host._running = True
    events: list[str] = []

    async def stopped() -> None:
        events.append("stop")
        host._running = False

    async def started() -> None:
        events.append("start")

    monkeypatch.setattr(host, "_ensure_stopped", stopped)
    monkeypatch.setattr(host, "_ensure_running", started)
    monkeypatch.setattr(radio_module, "CONFIG_POLL_S", 0.01)
    task = asyncio.ensure_future(host._watch_config())
    await asyncio.sleep(0.05)
    assert events == []  # nothing changed: nothing restarted
    host.ctx.values["AZURACAST_STATION_ID"] = "other"
    for _ in range(100):
        if events:
            break
        await asyncio.sleep(0.01)
    host.ctx.stopping.set()
    await asyncio.wait_for(task, 2)
    assert events == ["stop", "start"] and host._station_id == "other"


@pytest.mark.asyncio
async def test_remarks_survive_a_restart_and_a_broken_store_is_tolerated() -> None:
    host = make_host(CONFIGURED)
    await host._log_activity("T", "A", "words", "transition", "success")
    again = radio_module.RadioHost(host.ctx)
    await again._load_activities()
    assert [a["banter_text"] for a in again._recent_activities] == ["words"]
    assert again._activity_seq == 1
    host.ctx.storage.fail = True
    await host._log_activity("T2", "A", "more", "transition", "success")  # no raise
    assert len(host._recent_activities) == 2
    await again._load_activities()  # no raise


@pytest.mark.asyncio
async def test_the_log_keeps_only_the_newest_remarks() -> None:
    host = make_host(CONFIGURED)
    for n in range(radio_module.MAX_ACTIVITIES + 5):
        await host._log_activity(f"T{n}", "A", "w", "transition", "success")
    assert len(host._recent_activities) == radio_module.MAX_ACTIVITIES
    assert (
        host._recent_activities[0]["track_title"]
        == f"T{radio_module.MAX_ACTIVITIES + 4}"
    )
    assert len(host.ctx.storage.data["activities"]) == radio_module.MAX_ACTIVITIES


# --- the clips --------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_synthesised_clip_becomes_the_radios_own_file() -> None:
    host = make_host(CONFIGURED)
    path = await host._injector.generate_tts("Hello radio")
    assert path is not None and Path(path).is_file()
    assert host._audio_dir in Path(path).parents
    assert host.ctx.voice.spoken == [("Hello radio", "")]
    host.ctx.voice.available = False
    assert await host._injector.generate_tts("again") is None


@pytest.mark.asyncio
async def test_the_voice_engine_override_is_passed_to_the_core() -> None:
    host = make_host({**CONFIGURED, "RADIO_HOST_VOX_ENGINE": "kitten"})
    host._apply_config(host.ctx.values)
    await host._injector.generate_tts("Hi")
    assert host.ctx.voice.spoken == [("Hi", "kitten")]


def test_only_the_newest_replay_clips_are_kept() -> None:
    host = make_host(CONFIGURED)
    folder = host._audio_dir
    folder.mkdir(parents=True)
    import os

    for n in range(5):
        clip = folder / f"banter_{n}.webm"
        clip.write_bytes(b"x")
        os.utime(clip, (1000 + n, 1000 + n))
    removed = host._injector.trim_replays(keep=2)
    assert sorted(removed) == ["banter_0.webm", "banter_1.webm", "banter_2.webm"]
    assert sorted(p.name for p in folder.iterdir()) == [
        "banter_3.webm",
        "banter_4.webm",
    ]


def test_the_extension_imports_nothing_from_the_core() -> None:
    import re

    for path in SOURCE.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not re.search(
            r"^\s*(from|import)\s+(core|plugins|interface)\b", text, re.M
        ), path
