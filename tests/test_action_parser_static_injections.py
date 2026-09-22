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
