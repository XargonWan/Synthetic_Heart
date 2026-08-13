from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


def test_relative_age_marker_thresholds(monkeypatch) -> None:
    """Entries older than HISTORY_AGE_MARKER_MINUTES get a relative-age marker;
    fresh entries and disabled markers get none."""
    from core import history_engine

    now = datetime.now(timezone.utc)
    # Fresh — below the 10-minute default threshold.
    assert (
        history_engine._relative_age_marker(
            (now - timedelta(minutes=5)).isoformat(), now=now
        )
        == ""
    )
    # Older than threshold but sub-hour → minutes.
    assert (
        history_engine._relative_age_marker(
            (now - timedelta(minutes=25)).isoformat(), now=now
        )
        == "[25 minutes earlier]"
    )
    # 1 hour → singular unit.
    assert (
        history_engine._relative_age_marker(
            (now - timedelta(hours=1, minutes=2)).isoformat(), now=now
        )
        == "[1 hour earlier]"
    )
    # 3 hours.
    assert (
        history_engine._relative_age_marker(
            (now - timedelta(hours=3)).isoformat(), now=now
        )
        == "[3 hours earlier]"
    )
    # 2 days.
    assert (
        history_engine._relative_age_marker(
            (now - timedelta(days=2)).isoformat(), now=now
        )
        == "[2 days earlier]"
    )
    # Unusable timestamps → no marker.
    assert history_engine._relative_age_marker(None, now=now) == ""
    assert history_engine._relative_age_marker("garbage", now=now) == ""


def test_relative_age_marker_disabled_when_threshold_zero(monkeypatch) -> None:
    """HISTORY_AGE_MARKER_MINUTES=0 turns the marker off entirely."""
    from core import history_engine

    monkeypatch.setattr(
        "core.history_engine._get_int",
        lambda key, default: 0 if key == "HISTORY_AGE_MARKER_MINUTES" else default,
    )
    now = datetime.now(timezone.utc)
    assert (
        history_engine._relative_age_marker(
            (now - timedelta(hours=5)).isoformat(), now=now
        )
        == ""
    )


def test_entry_to_text_includes_age_marker_for_old_messages(monkeypatch) -> None:
    """An hours-old chat line carries the relative-age marker inside its quoted
    content so the model can see it is stale (CHANGELOG 2026-07-05)."""
    from core import history_engine

    now = datetime.now(timezone.utc)
    old = {
        "sender_name": "Scar",
        "text": "nighty night bubu",
        "timestamp": (now - timedelta(hours=3)).isoformat(),
        "interface_path": "telegram_bot/123",
    }
    line = history_engine._entry_to_text(old)
    assert "[3 hours earlier]" in line
    assert "nighty night bubu" in line

    fresh = {
        "sender_name": "Scar",
        "text": "hi there",
        "timestamp": (now - timedelta(minutes=1)).isoformat(),
        "interface_path": "telegram_bot/123",
    }
    line = history_engine._entry_to_text(fresh)
    assert "[1 minute earlier]" not in line
    assert "hi there" in line


@pytest.mark.asyncio
async def test_history_engine_ignores_cortex_switch_notifications(
    monkeypatch,
) -> None:
    from core.history_engine import HistoryEngine

    current_path = "synth_webui/current"

    context_memory = {
        current_path: deque(
            [
                {
                    "sender_name": "self",
                    "text": "✅ Cortex engine dynamically updated to `gemini`.",
                    "timestamp": "2026-04-19T00:08:00+00:00",
                    "interface_path": current_path,
                },
                {
                    "sender_name": "Alice",
                    "text": "hello there",
                    "timestamp": "2026-04-19T00:08:01+00:00",
                    "interface_path": current_path,
                },
            ]
        )
    }

    monkeypatch.setattr(
        "core.chat_history_cache.load_chat_history",
        AsyncMock(return_value=deque()),
    )
    monkeypatch.setattr(
        "core.chat_history_cache.load_global_chat_history",
        AsyncMock(
            return_value=deque(
                [
                    {
                        "sender_name": "self",
                        "text": "✅ Cortex engine dynamically updated to `openrouter`.",
                        "timestamp": "2026-04-19T00:04:00+00:00",
                        "interface_path": "synth_webui/other",
                    },
                    {
                        "sender_name": "Alice",
                        "text": "cross chat line",
                        "timestamp": "2026-04-19T00:05:00+00:00",
                        "interface_path": "synth_webui/other",
                    },
                ]
            )
        ),
    )
    monkeypatch.setattr("core.core_initializer.PLUGIN_REGISTRY", {})

    context = await HistoryEngine().build_context(
        message=SimpleNamespace(interface_path=current_path),
        context_memory=context_memory,
        interface_name="synth_webui",
        text="current input",
    )

    joined_current = "\n".join(context["history_current_chat"])
    joined_recent = "\n".join(context["history_recent"])

    assert "hello there" in joined_current
    assert "cross chat line" in joined_recent
    assert "Cortex engine dynamically updated" not in joined_current
    assert "Cortex engine dynamically updated" not in joined_recent


@pytest.mark.asyncio
async def test_history_engine_ignores_cortex_scope_override_notifications(
    monkeypatch,
) -> None:
    from core.history_engine import HistoryEngine

    current_path = "telegram_bot/123"

    context_memory = {
        current_path: deque(
            [
                {
                    "sender_name": "self",
                    "text": "✅ Cortex engine override for grillo updated to `xtx`.",
                    "timestamp": "2026-05-08T10:00:00+00:00",
                    "interface_path": current_path,
                },
                {
                    "sender_name": "self",
                    "text": "✅ Cortex engine override for trainer updated to `openrouter`.",
                    "timestamp": "2026-05-08T10:00:01+00:00",
                    "interface_path": current_path,
                },
                {
                    "sender_name": "Alice",
                    "text": "alright, done",
                    "timestamp": "2026-05-08T10:00:02+00:00",
                    "interface_path": current_path,
                },
            ]
        )
    }

    monkeypatch.setattr(
        "core.chat_history_cache.load_chat_history",
        AsyncMock(return_value=deque()),
    )
    monkeypatch.setattr(
        "core.chat_history_cache.load_global_chat_history",
        AsyncMock(return_value=deque()),
    )
    monkeypatch.setattr("core.core_initializer.PLUGIN_REGISTRY", {})

    context = await HistoryEngine().build_context(
        message=SimpleNamespace(interface_path=current_path),
        context_memory=context_memory,
        interface_name="telegram_bot",
        text="how's it going",
    )

    joined = "\n".join(context["history_current_chat"])
    assert "alright, done" in joined
    assert "override for grillo" not in joined
    assert "override for trainer" not in joined


@pytest.mark.asyncio
async def test_history_engine_excludes_vessel_rows_from_non_vessel_context(
    monkeypatch,
) -> None:
    from core.history_engine import HistoryEngine

    current_path = "telegram_bot/123"
    monkeypatch.setattr(
        "core.chat_history_cache.load_chat_history",
        AsyncMock(return_value=deque()),
    )
    monkeypatch.setattr(
        "core.chat_history_cache.load_global_chat_history",
        AsyncMock(
            return_value=deque(
                [
                    {
                        "sender_name": "player",
                        "text": "stale vessel line",
                        "timestamp": "2026-08-05T14:09:13+00:00",
                        "interface_path": "vessel/minecraft/old-server",
                    },
                    {
                        "sender_name": "Alice",
                        "text": "ordinary chat line",
                        "timestamp": "2026-08-06T09:00:00+00:00",
                        "interface_path": "telegram_bot/456",
                    },
                ]
            )
        ),
    )
    monkeypatch.setattr("core.core_initializer.PLUGIN_REGISTRY", {})

    context = await HistoryEngine().build_context(
        message=SimpleNamespace(interface_path=current_path),
        context_memory={current_path: deque()},
        interface_name="telegram_bot",
        text="current input",
    )

    joined = "\n".join(context["history_current_chat"] + context["history_recent"])
    assert "ordinary chat line" in joined
    assert "stale vessel line" not in joined


async def test_empty_text_entries_do_not_render_blank_lines(monkeypatch) -> None:
    """Chat-like entries with no text (e.g. media without a caption) must not
    become blank '[ts] Sender: ""' lines in history_current_chat. They carry
    zero signal and previously surfaced as empty-content user/assistant turns
    in the provider messages array (blank blocks in Langfuse traces).
    Diary-like dicts (interaction_summary) must still render."""
    from core.history_engine import HistoryEngine, _is_ignored_prompt_history_entry

    current_path = "telegram_bot/123"
    now_ts = "2026-08-11T05:00:00+00:00"

    # Unit-level: the guard itself.
    assert _is_ignored_prompt_history_entry(
        {"sender_name": "Scar", "text": "", "timestamp": now_ts}
    )
    assert _is_ignored_prompt_history_entry(
        {"sender_name": "self", "text": "  ", "timestamp": now_ts}
    )
    # Diary-like dicts are exempt (no text field but a summary).
    assert not _is_ignored_prompt_history_entry(
        {"sender_name": "Scar", "interaction_summary": "We talked", "timestamp": now_ts}
    )
    # Real chat lines are never ignored by this rule.
    assert not _is_ignored_prompt_history_entry(
        {"sender_name": "Scar", "text": "hello", "timestamp": now_ts}
    )

    monkeypatch.setattr(
        "core.chat_history_cache.load_chat_history",
        AsyncMock(return_value=deque()),
    )
    monkeypatch.setattr(
        "core.chat_history_cache.load_global_chat_history",
        AsyncMock(return_value=deque()),
    )
    monkeypatch.setattr("core.core_initializer.PLUGIN_REGISTRY", {})

    context = await HistoryEngine().build_context(
        message=SimpleNamespace(interface_path=current_path),
        context_memory={
            current_path: deque(
                [
                    {
                        "sender_name": "Scar",
                        "text": "",
                        "timestamp": now_ts,
                        "interface_path": current_path,
                    },
                    {
                        "sender_name": "Scar",
                        "text": "real question",
                        "timestamp": now_ts,
                        "interface_path": current_path,
                    },
                ]
            )
        },
        interface_name="telegram_bot",
        text="current input",
    )

    joined = "\n".join(context["history_current_chat"])
    assert "real question" in joined
    assert 'Scar: ""' not in joined


def test_diary_entry_renders_created_at_timestamp() -> None:
    """ai_diary entries carry ``created_at`` (not ``timestamp``/``date``). The
    recent-context block was rendering them as ``[diary ]`` with an empty
    timestamp (langfuse d61bb37b 2026-08-13), so the model could not see how
    old a diary line was and treated vague summaries as current context.
    ``_entry_to_text`` must read ``created_at`` for the diary branch."""
    from core.history_engine import _entry_to_text

    line = _entry_to_text(
        {
            "interaction_summary": "Dee is showing Daddy her bunny cosplay outfit.",
            "personal_thought": "My heart is racing...",
            "created_at": "2026-08-13T01:30:00+00:00",
            "id": 123,
        }
    )
    assert line.startswith("[diary 13/08/26:0130] summary: ")
    assert "Dee is showing Daddy" in line
