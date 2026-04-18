from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from plugins.soul_plugin import SoulPlugin
from plugins.soul_plugin import _SessionState
from core.soul.repository import InMemorySoulRepository, PostgresSoulRepository


@pytest.mark.asyncio
async def test_static_injection_contains_soul_keys() -> None:
    plugin = SoulPlugin()

    message = SimpleNamespace(
        interface_path="telegram_bot/123",
        text="I feel anxious about 2026-04-24 but also happy you are here.",
        caption=None,
    )

    payload = await plugin.get_static_injection(
        message, {"interface_path": "telegram_bot/123"}
    )

    assert "soul_user_profile" in payload
    assert "soul_session_state" in payload
    assert "soul_turn_emotion_delta" in payload
    assert "soul_active_foresight" in payload


@pytest.mark.asyncio
async def test_force_compile_clears_interface_buffer() -> None:
    plugin = SoulPlugin()

    message = SimpleNamespace(
        interface_path="telegram_bot/555",
        text="I need to remember the event on 2026-04-20",
        caption=None,
    )
    await plugin.get_static_injection(message, {"interface_path": "telegram_bot/555"})

    assert plugin._buffers["telegram_bot/555"]

    result = await plugin.execute_action(
        {
            "type": "soul_force_compile",
            "payload": {"interface_path": "telegram_bot/555"},
        },
        {},
        None,
        message,
    )

    assert result["compiled_memcells"] >= 1
    assert plugin._buffers["telegram_bot/555"] == []


@pytest.mark.asyncio
async def test_static_injection_recalls_relevant_memories() -> None:
    plugin = SoulPlugin()
    interface_path = "telegram_bot/999"

    seed_message = SimpleNamespace(
        interface_path=interface_path,
        text="Scarlet loves jasmine tea and cozy rainy evenings.",
        caption=None,
    )
    await plugin.get_static_injection(seed_message, {"interface_path": interface_path})
    await plugin._compile_interface(interface_path)

    recall_message = SimpleNamespace(
        interface_path=interface_path,
        text="What tea does Scarlet love again?",
        caption=None,
    )
    payload = await plugin.get_static_injection(
        recall_message, {"interface_path": interface_path}
    )

    recalled = payload.get("soul_recalled_memories")

    assert isinstance(recalled, list)
    recalled_entries = [str(entry) for entry in recalled]
    assert any("jasmine tea" in entry.lower() for entry in recalled_entries)


@pytest.mark.asyncio
async def test_scheduler_tick_compiles_idle_sessions() -> None:
    plugin = SoulPlugin()

    iface = "telegram_bot/77"
    plugin._buffers[iface] = ["hello", "event 2026-04-30"]
    session = _SessionState()
    plugin._sessions[iface] = session

    session.last_seen = datetime.now(timezone.utc) - timedelta(hours=1)

    await plugin._tick_scheduler()

    assert plugin._buffers[iface] == []


def test_repository_backend_postgres_selected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        SoulPlugin, "_get_repository_backend", staticmethod(lambda: "postgres")
    )
    monkeypatch.setattr(
        SoulPlugin,
        "_get_postgres_dsn",
        staticmethod(lambda: "postgresql://soul:soul@localhost:5432/soul_memory"),
    )

    plugin = SoulPlugin()

    assert isinstance(plugin._repo, PostgresSoulRepository)


def test_repository_backend_postgres_falls_back_without_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        SoulPlugin, "_get_repository_backend", staticmethod(lambda: "postgres")
    )
    monkeypatch.setattr(SoulPlugin, "_get_postgres_dsn", staticmethod(lambda: ""))

    plugin = SoulPlugin()

    assert isinstance(plugin._repo, InMemorySoulRepository)
