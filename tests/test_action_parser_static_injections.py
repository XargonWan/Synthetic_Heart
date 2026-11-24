from __future__ import annotations

import asyncio

import pytest


class _CachedStaticInjectionPlugin:
    allow_static_injection_stale_fallback = True
    static_injection_cache_ttl_seconds = 60.0

    def __init__(self) -> None:
        self.calls = 0

    def get_supported_action_types(self) -> list[str]:
        return ["static_inject"]

    async def get_static_injection(self) -> dict[str, object]:
        self.calls += 1
        if self.calls == 1:
            return {"soul_session_state": "fresh"}
        raise asyncio.TimeoutError()


class _BlockInjectionPlugin:
    """Emits the paired keys one turn can carry: a replacement and its legacy."""

    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def get_supported_action_types(self) -> list[str]:
        return ["static_inject"]

    async def get_static_injection(self) -> dict[str, object]:
        return dict(self.payload)


@pytest.mark.asyncio
async def test_replacement_block_drops_the_legacy_key_at_gather_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The superseded text must never enter the injection dict at all.

    ``_PLUGIN_CONTEXT_BLOCKS`` declares which built-in key each plugin block
    replaces (``home_weather`` -> ``weather``, ``home_location`` -> ``location``).
    The renderer used to drop the legacy key at RENDER time, which left the two
    keys side by side in the dict a beat sees and in the ``returning N keys``
    line, so a provider could survive into a route that never re-checks. Dropping
    it here is what makes the dict tell the truth about the turn.
    """
    from core import action_parser

    monkeypatch.setattr(action_parser, "_STATIC_INJECTION_CACHE", {})
    monkeypatch.setattr(
        action_parser,
        "_load_action_plugins",
        lambda: [
            _BlockInjectionPlugin(
                {"home_weather": "[Weather] cloudy 25.5°C", "weather": "wttr.in text"}
            )
        ],
    )

    injections = await action_parser.gather_static_injections()

    assert injections["home_weather"] == "[Weather] cloudy 25.5°C"
    assert "weather" not in injections


@pytest.mark.asyncio
async def test_legacy_key_survives_when_the_replacement_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other half: with no replacement present the provider is untouched.

    A deployment with the Home Assistant weather block switched off must keep
    the built-in wttr.in line exactly as before.
    """
    from core import action_parser

    monkeypatch.setattr(action_parser, "_STATIC_INJECTION_CACHE", {})
    monkeypatch.setattr(
        action_parser,
        "_load_action_plugins",
        lambda: [_BlockInjectionPlugin({"weather": "wttr.in text"})],
    )

    injections = await action_parser.gather_static_injections()

    assert injections == {"weather": "wttr.in text"}


@pytest.mark.asyncio
async def test_gather_static_injections_uses_cached_payload_after_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from core import action_parser

    plugin = _CachedStaticInjectionPlugin()

    monkeypatch.setattr(action_parser, "_STATIC_INJECTION_CACHE", {})
    monkeypatch.setattr(action_parser, "_load_action_plugins", lambda: [plugin])

    first = await action_parser.gather_static_injections()
    second = await action_parser.gather_static_injections()

    assert first == {"soul_session_state": "fresh"}
    assert second == {"soul_session_state": "fresh"}


class _SlowInjectionPlugin:
    """A plugin whose static injection takes a measurable amount of time."""

    def __init__(self, delay: float) -> None:
        self._delay = delay

    def get_supported_action_types(self) -> list[str]:
        return ["static_inject"]

    async def get_static_injection(self) -> dict[str, object]:
        await asyncio.sleep(self._delay)
        return {}


@pytest.mark.asyncio
async def test_slow_injections_are_reported_once_for_the_whole_gather(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One timing line per prompt build, naming the slowest contributor.

    Every plugin over the threshold used to log its own line, so a single beat could
    print six of them and the one number that matters (which plugin is slowest, and how
    long the whole gather took) was buried. The per-plugin detail stays at debug.
    """
    from core import action_parser

    monkeypatch.setattr(action_parser, "_STATIC_INJECTION_CACHE", {})
    monkeypatch.setattr(action_parser, "_SLOW_INJECTION_SEC", 0.01)
    monkeypatch.setattr(
        action_parser,
        "_load_action_plugins",
        lambda: [_SlowInjectionPlugin(0.05)],
    )

    info_lines: list[str] = []
    debug_lines: list[str] = []
    monkeypatch.setattr(action_parser, "log_info", info_lines.append)
    monkeypatch.setattr(action_parser, "log_debug", debug_lines.append)

    await action_parser.gather_static_injections()

    timing = [line for line in info_lines if "static injections:" in line]
    assert len(timing) == 1, info_lines
    assert "slowest: _SlowInjectionPlugin" in timing[0]
    assert "block(s) in" in timing[0]
    assert not [line for line in debug_lines if "took" in line], (
        "a slow plugin is named in the aggregate line, not logged again on its own"
    )


@pytest.mark.asyncio
async def test_a_fast_gather_says_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No slow plugin means no timing line: the panel is for problems, not decoration."""
    from core import action_parser

    monkeypatch.setattr(action_parser, "_STATIC_INJECTION_CACHE", {})
    monkeypatch.setattr(
        action_parser,
        "_load_action_plugins",
        lambda: [_BlockInjectionPlugin({"weather": "wttr.in text"})],
    )

    info_lines: list[str] = []
    monkeypatch.setattr(action_parser, "log_info", info_lines.append)

    await action_parser.gather_static_injections()

    assert not [line for line in info_lines if "static injections:" in line]


class _ScopedInjectionPlugin:
    """A plugin whose payload belongs to one chat, plus one key true in all chats."""

    allow_static_injection_stale_fallback = True
    static_injection_cache_ttl_seconds = 60.0
    static_injection_global_keys = frozenset({"situational"})

    def __init__(self) -> None:
        self.fail = False

    def get_supported_action_types(self) -> list[str]:
        return ["static_inject"]

    def static_injection_scope(self, message=None, context_memory=None) -> str:
        return str(getattr(message, "chat_id", ""))

    async def get_static_injection(self, message=None, context_memory=None):
        if self.fail:
            raise asyncio.TimeoutError()
        return {
            "recalled": f"memory of chat {message.chat_id}",
            "situational": "travelling this week",
        }


@pytest.mark.asyncio
async def test_stale_fallback_does_not_leak_one_chats_memories_into_another(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A timeout in chat B must not serve chat A's recalled memories.

    The cache used to be keyed by plugin class alone, so the last payload built
    for ANY chat was served to whichever chat timed out next. Keys the plugin
    declares global still ride across chats: the persona has to stay aware of
    what is going on outside the conversation it is in.
    """
    from types import SimpleNamespace

    from core import action_parser

    plugin = _ScopedInjectionPlugin()
    monkeypatch.setattr(action_parser, "_STATIC_INJECTION_CACHE", {})
    monkeypatch.setattr(action_parser, "_load_action_plugins", lambda: [plugin])

    chat_a = SimpleNamespace(chat_id="A")
    chat_b = SimpleNamespace(chat_id="B")

    fresh_a = await action_parser.gather_static_injections(chat_a)
    assert fresh_a["recalled"] == "memory of chat A"

    plugin.fail = True
    fallback_b = await action_parser.gather_static_injections(chat_b)
    assert "recalled" not in fallback_b
    assert fallback_b.get("situational") == "travelling this week"

    # Chat A's own fallback still works, and still carries its own memories.
    fallback_a = await action_parser.gather_static_injections(chat_a)
    assert fallback_a["recalled"] == "memory of chat A"
    assert fallback_a["situational"] == "travelling this week"


def test_the_scoped_cache_is_pruned_once_it_outgrows_its_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One entry per chat must not grow the cache without bound."""
    import time

    from core import action_parser

    now = time.time()
    cache = {
        f"plugin|chat{i}": (now - 5000, {"k": "old"})
        for i in range(action_parser._STATIC_INJECTION_CACHE_MAX + 5)
    }
    cache["plugin|fresh"] = (now, {"k": "new"})
    monkeypatch.setattr(action_parser, "_STATIC_INJECTION_CACHE", cache)

    action_parser._prune_static_injection_cache(now, 300.0)

    assert list(cache) == ["plugin|fresh"]
