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
