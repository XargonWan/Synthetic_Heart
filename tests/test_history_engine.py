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


@pytest.mark.asyncio
async def test_cross_chat_history_survives_non_dict_context_values(monkeypatch) -> None:
    """A context carrying plain-string lists must not kill the cross-chat block.

    ``context_memory`` is not only a chat map: it also carries per-turn routing
    and plugin flags, and some of those are LISTS OF STRINGS — ``grillo_snippets``
    on every observer beat, ``attachment_paths`` on any turn with media. The
    unified builder used to append those to its candidate list, and
    ``_is_internal_noise`` then called ``.get`` on a str, raising
    ``AttributeError: 'str' object has no attribute 'get'`` and aborting the
    whole unified block — so ``history_recent`` came back EMPTY and the model
    was never told what had just been said in its other conversations.

    Live symptom (2026-09-21): the hourly observer beat's prompt carried no
    ``[Recent context from other conversations]`` block at all — the DM
    conversation the beat was about to reply to was simply absent — while
    ordinary chat turns on the same deployment carried it. The failure was
    logged at DEBUG, so nothing appeared in the log at the deployment's
    INFO level.
    """
    from core.history_engine import HistoryEngine

    other_chat = "telegram_bot/5208932647"

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
                        "sender_name": "Scar",
                        "text": "the harness is ready, she woke up clean",
                        "timestamp": "2026-09-21T20:16:00+00:00",
                        "interface_path": other_chat,
                    }
                ]
            )
        ),
    )
    monkeypatch.setattr("core.core_initializer.PLUGIN_REGISTRY", {})

    # Observer-beat shape: the beat's own context dict on ``context_memory``,
    # with the snippet/target lists the beat enqueues and the synthetic
    # ``grillo/-1`` path the queue consumer writes onto it.
    context_memory = {
        "grillo_beat": True,
        "beat_type": "observer",
        "grillo_snippets": [f"(chat:{other_chat} | sender:Scar | 45m) snippet text"],
        "grillo_targets": [{"interface_path": other_chat}],
        "decay_driven": True,
        "interface_path": "grillo/-1",
    }

    context = await HistoryEngine().build_context(
        message=SimpleNamespace(chat_id=-1, text="observer prompt"),
        context_memory=context_memory,
        interface_name="grillo",
        text="observer prompt",
    )

    joined_recent = "\n".join(context["history_recent"])
    assert "the harness is ready, she woke up clean" in joined_recent

    # Same defect on an ordinary chat turn: attachment_paths is a list of
    # strings on the per-turn context.
    context_memory = {
        "telegram_bot/999": deque(
            [
                {
                    "sender_name": "Scar",
                    "text": "look at this",
                    "timestamp": "2026-09-21T20:17:00+00:00",
                    "interface_path": "telegram_bot/999",
                }
            ]
        ),
        "attachment_paths": ["res/attachments/20260921_pic.png"],
    }
    context = await HistoryEngine().build_context(
        message=SimpleNamespace(interface_path="telegram_bot/999"),
        context_memory=context_memory,
        interface_name="telegram_bot",
        text="look at this",
    )
    joined_recent = "\n".join(context["history_recent"])
    assert "the harness is ready, she woke up clean" in joined_recent


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


# ── Exchange window (CONTEXT_EXCHANGE_WINDOW) ────────────────────────────────


def _lines(*specs: str) -> list[str]:
    """Render history lines from "Sender: text" specs, with a rising timestamp."""
    out: list[str] = []
    for i, spec in enumerate(specs):
        sender, _, text = spec.partition(": ")
        out.append(f'[24/09/26:{700 + i:04d}] {sender}: "{text}"')
    return out


def test_exchange_window_keeps_the_last_n_exchanges() -> None:
    """Two exchanges = the last two turns by somebody else plus what followed."""
    from core.history_engine import _select_exchange_window

    lines = _lines(
        "Scar: H1",
        "self (you): A1",
        "self (you): A2",
        "Scar: H2",
        "self (you): A3",
        "self: A4",
        "Scar: H3",
        "self (you): A5",
    )
    selected = _select_exchange_window(lines, 2)
    assert selected == lines[3:]


def test_exchange_window_survives_a_run_of_unanswered_replies() -> None:
    """The failure mode this exists for: the persona's own replies eat a
    message-counted window and the orphan-turn rule then deletes what is left
    (measured 2026-09-24: six slots held one exchange)."""
    from core.history_engine import _select_exchange_window

    lines = _lines(
        "Scar: the half-four thing",
        "self (you): " + "x" * 500,
        "self (you): " + "x" * 500,
        "self (you): " + "x" * 500,
        "self (you): " + "x" * 500,
        "self (you): " + "x" * 500,
        "Scar: are you awake",
        "self (you): " + "x" * 500,
    )
    selected = _select_exchange_window(lines, 2)
    assert selected == lines[0:]


def test_exchange_window_drops_oldest_exchanges_over_the_char_cap() -> None:
    from core.history_engine import _select_exchange_window

    lines = _lines(
        "Scar: old turn",
        "self (you): " + "x" * 300,
        "Scar: middle turn",
        "self (you): " + "x" * 300,
        "Scar: newest turn",
        "self (you): " + "x" * 300,
    )
    selected = _select_exchange_window(lines, 3, char_cap=500)
    # Whole oldest exchanges are dropped until the cap is met; the newest one
    # always survives, and it is never truncated.
    assert selected == lines[4:]
    assert len("\n".join(selected)) <= 500


def test_exchange_window_keeps_the_newest_exchange_whole_over_the_cap() -> None:
    from core.history_engine import _select_exchange_window

    lines = _lines("Scar: a huge turn", "self (you): " + "x" * 4000)
    selected = _select_exchange_window(lines, 3, char_cap=100)
    assert selected == lines


def test_exchange_window_treats_spelled_out_self_labels_as_the_persona() -> None:
    """`self (you)` is the persona's own line, not another speaker."""
    from core.history_engine import _is_other_speaker_line

    assert not _is_other_speaker_line('[24/09/26:0700] self (you): "mine"')
    assert not _is_other_speaker_line('[24/09/26:0700] self: "mine"')
    assert not _is_other_speaker_line('[24/09/26:0700] assistant: "mine"')
    assert _is_other_speaker_line('[24/09/26:0700] Scar: "his"')
    assert _is_other_speaker_line('[24/09/26:0700] 2B: "mama"')
    # Unparseable lines are events, not people.
    assert not _is_other_speaker_line("[diary 13/08/26:0130] summary: something")


def test_exchange_window_off_returns_the_lines_untouched() -> None:
    from core.history_engine import _select_exchange_window

    lines = _lines("Scar: H1", "self (you): A1", "Scar: H2")
    assert _select_exchange_window(lines, 0) == lines
    assert _select_exchange_window([], 3) == []


def test_exchange_window_falls_back_to_a_char_bounded_tail() -> None:
    """Only the persona spoke: there is no exchange to anchor on."""
    from core.history_engine import _select_exchange_window

    lines = _lines("self (you): " + "y" * 200, "self (you): " + "y" * 200)
    selected = _select_exchange_window(lines, 3, char_cap=300)
    assert selected == lines[1:]


@pytest.mark.asyncio
async def test_build_context_uses_the_exchange_window_for_the_active_chat(
    monkeypatch,
) -> None:
    """With the knob on, the active chat keeps whole exchanges: the run of the
    persona's own replies stops eating the window (the reported incident kept
    one exchange out of six message slots)."""
    from core.history_engine import HistoryEngine

    current_path = "telegram_bot/5208932647"
    # Three human turns; the persona answers in long runs between them.
    senders = [
        "Scar",
        "self",
        "self",
        "self",
        "self",
        "self",
        "Scar",
        "self",
        "self",
        "Scar",
        "self",
        "self",
    ]
    entries = [
        {
            "sender_name": sender,
            "text": f"line {i}",
            "timestamp": f"2026-09-24T{i:02d}:00:00+00:00",
            "interface_path": current_path,
        }
        for i, sender in enumerate(senders)
    ]
    context_memory = {current_path: deque(entries)}

    monkeypatch.setattr(
        "core.chat_history_cache.load_chat_history", AsyncMock(return_value=deque())
    )
    monkeypatch.setattr(
        "core.chat_history_cache.load_global_chat_history",
        AsyncMock(return_value=deque()),
    )
    monkeypatch.setattr("core.core_initializer.PLUGIN_REGISTRY", {})
    monkeypatch.setenv("CONTEXT_EXCHANGE_WINDOW", "1")
    monkeypatch.setenv("CONTEXT_EXCHANGE_CHAR_CAP", "8000")

    context = await HistoryEngine().build_context(
        message=SimpleNamespace(interface_path=current_path),
        context_memory=context_memory,
        interface_name="telegram_bot",
        text="current input",
    )

    joined = "\n".join(context["history_current_chat"])
    # One exchange = the last human turn and the replies it got.
    assert "line 9" in joined
    assert "line 10" in joined and "line 11" in joined
    # Everything before it is gone, including the six-line run of her replies.
    assert "line 8" not in joined
    assert "line 6" not in joined
    assert "line 0" not in joined


@pytest.mark.asyncio
async def test_build_context_keeps_the_message_count_when_the_knob_is_off(
    monkeypatch,
) -> None:
    """Knob off (or absent): the message count decides, which is what the
    reported incident ran into, six slots holding one exchange."""
    from core import history_engine
    from core.history_engine import HistoryEngine

    current_path = "telegram_bot/5208932647"
    senders = [
        "Scar",
        "self",
        "self",
        "self",
        "self",
        "self",
        "Scar",
        "self",
        "self",
        "Scar",
        "self",
        "self",
    ]
    entries = [
        {
            "sender_name": sender,
            "text": f"line {i}",
            "timestamp": f"2026-09-24T{i:02d}:00:00+00:00",
            "interface_path": current_path,
        }
        for i, sender in enumerate(senders)
    ]
    context_memory = {current_path: deque(entries)}

    monkeypatch.setattr(
        "core.chat_history_cache.load_chat_history", AsyncMock(return_value=deque())
    )
    monkeypatch.setattr(
        "core.chat_history_cache.load_global_chat_history",
        AsyncMock(return_value=deque()),
    )
    monkeypatch.setattr("core.core_initializer.PLUGIN_REGISTRY", {})
    monkeypatch.delenv("CONTEXT_EXCHANGE_WINDOW", raising=False)
    # Pin the message count so the comparison does not depend on the host's
    # CONTEXT_VERBOSITY (10 by default, 6 in the 2D deployment).
    real_get_int = history_engine._get_int
    monkeypatch.setattr(
        history_engine,
        "_get_int",
        lambda key, default: (
            6 if key == "CONTEXT_VERBOSITY" else real_get_int(key, default)
        ),
    )

    context = await HistoryEngine().build_context(
        message=SimpleNamespace(interface_path=current_path),
        context_memory=context_memory,
        interface_name="telegram_bot",
        text="current input",
    )

    joined = "\n".join(context["history_current_chat"])
    # The last six messages, whatever the sender: the older run survives and the
    # newest human turn is still there, which is how a window ends up holding a
    # single readable exchange once the orphan-turn rule runs.
    assert "line 6" in joined
    assert "line 11" in joined
    assert "line 5" not in joined


@pytest.mark.asyncio
async def test_exchange_window_reads_the_cache_even_when_the_buffer_is_full(
    monkeypatch,
) -> None:
    """The in-memory buffer is a deque with maxlen=CONTEXT_VERBOSITY, so on its
    own it can never carry an exchange window that reaches further back than
    that. The persisted cache must therefore be merged in while the knob is on,
    even when the buffer already holds more messages than the message count."""
    from core import history_engine
    from core.history_engine import HistoryEngine

    current_path = "telegram_bot/5208932647"
    senders = ["Scar", "self", "Scar", "self", "Scar", "self"]
    entries = [
        {
            "sender_name": sender,
            "text": f"line {i}",
            "timestamp": f"2026-09-24T{i:02d}:00:00+00:00",
            "interface_path": current_path,
        }
        for i, sender in enumerate(senders)
    ]
    context_memory = {current_path: deque(entries)}

    calls = 0

    async def _counting_cache_load(*args, **kwargs):
        nonlocal calls
        calls += 1
        return deque()

    monkeypatch.setattr(
        "core.chat_history_cache.load_chat_history", _counting_cache_load
    )
    monkeypatch.setattr(
        "core.chat_history_cache.load_global_chat_history",
        AsyncMock(return_value=deque()),
    )
    monkeypatch.setattr("core.core_initializer.PLUGIN_REGISTRY", {})
    real_get_int = history_engine._get_int
    monkeypatch.setattr(
        history_engine,
        "_get_int",
        lambda key, default: (
            6 if key == "CONTEXT_VERBOSITY" else real_get_int(key, default)
        ),
    )

    async def _build() -> None:
        await HistoryEngine().build_context(
            message=SimpleNamespace(interface_path=current_path),
            context_memory=context_memory,
            interface_name="telegram_bot",
            text="current input",
        )

    # Knob off: six buffered messages already exceed the message count of six,
    # which is the legacy rule, and no cache read is needed.
    monkeypatch.delenv("CONTEXT_EXCHANGE_WINDOW", raising=False)
    await _build()
    assert calls == 0

    # Knob on: the window wants to reach further back than the buffer can, so
    # the persisted rows are read.
    monkeypatch.setenv("CONTEXT_EXCHANGE_WINDOW", "5")
    monkeypatch.setenv("CONTEXT_EXCHANGE_CHAR_CAP", "8000")
    await _build()
    assert calls == 1
