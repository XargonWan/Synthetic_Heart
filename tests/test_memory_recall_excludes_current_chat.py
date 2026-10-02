"""The live conversation must never be recalled back into its own prompt.

Incident (2026-09-21, trace 58cd09fc): Scar's message arrived, and the model saw
it twice — once as the turn it was answering, and once as the first entry under
``[Relevant memories]``:

    [lang:en | ... | path:telegram_bot/5208932647]
    bitch shut up before i unplug your pussy and leave you a leaking mess ...

    [Relevant memories]
    - Recalled memory from 2026-09-21 (telegram_bot/5208932647, chat history):
      bitch shut up before i unplug your pussy and leave you a leaking mess ...

She answered it as if he had repeated himself ("Also you wrote this twice, you
know"), and carried that reading into her diary entry for the turn.

Mechanism: the message being answered is persisted to ``chat_history_cache``
before the prompt is built (measured: row 14966 written 3 seconds before the
LLM call), so a keyword search extracted from that same message matches it
verbatim and re-injects it. ``search_memories`` has a guard for exactly this,
``exclude_interface_paths``, which the caller fills from the turn's routing
path.

The path is carried on ``message.interface_path`` normally, but internally
enqueued turns carry it only in the context dict. The fallback that reads it
from the context used to run ~360 lines *after* the recall, so those turns
passed no exclusion at all and the guard switched off without a word. The same
late resolution made the Grillo-internal check and the Vessel probe read an
empty path for those turns.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).parent.parent))

import core.prompt_engine as prompt_engine  # noqa: E402
from core.prompt_engine import build_prompt_request  # noqa: E402

CURRENT_CHAT = "telegram_bot/5208932647"


def _patch_static_injections(monkeypatch) -> None:
    async def fake_gather_static_injections(message=None, context_memory=None):
        return {"persona": "PERSONA: 2B."}

    monkeypatch.setattr(
        "core.action_parser.gather_static_injections", fake_gather_static_injections
    )


def _capture_search(monkeypatch) -> dict:
    captured: dict = {}

    async def fake_search_memories(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr("core.synth_core_memory.search_memories", fake_search_memories)
    return captured


async def test_recall_excludes_the_current_chat_when_the_path_is_in_the_context(
    monkeypatch,
) -> None:
    """The regression: a turn whose path arrives via the context dict.

    This is the shape that produced the incident, and it is also the shape the
    delivery path and the scheduled beats use.
    """
    _patch_static_injections(monkeypatch)
    captured = _capture_search(monkeypatch)

    # No interface_path attribute at all: the path lives only in the context.
    message = SimpleNamespace(
        text="do you remember the weather block from the house",
        message_id=1,
        date=SimpleNamespace(isoformat=lambda: "2026-09-21T14:04:08Z"),
    )

    await build_prompt_request(
        message=message,
        context_memory={"interface_path": CURRENT_CHAT},
        interface_name="telegram_bot",
    )

    assert captured, "the memory search did not run; the test proves nothing"
    assert captured.get("exclude_interface_paths") == [CURRENT_CHAT], (
        "the current chat must be excluded from the raw chat-history tier, "
        "otherwise the message being answered is recalled back into its own prompt"
    )


async def test_recall_excludes_the_current_chat_when_the_path_is_on_the_message(
    monkeypatch,
) -> None:
    """The ordinary shape keeps working: the path on the message itself."""
    _patch_static_injections(monkeypatch)
    captured = _capture_search(monkeypatch)

    message = SimpleNamespace(
        interface_path=CURRENT_CHAT,
        text="do you remember the weather block from the house",
        message_id=1,
        date=SimpleNamespace(isoformat=lambda: "2026-09-21T14:04:08Z"),
    )

    await build_prompt_request(
        message=message,
        context_memory={},
        interface_name="telegram_bot",
    )

    assert captured.get("exclude_interface_paths") == [CURRENT_CHAT]


async def test_a_turn_with_no_path_warns_instead_of_failing_silently(
    monkeypatch,
) -> None:
    """When the guard cannot run, say so.

    The incident was invisible in the log: the guard simply did not apply. One
    warning per process is enough to point at the cause without flooding the log
    on beats that legitimately carry no path.
    """
    _patch_static_injections(monkeypatch)
    _capture_search(monkeypatch)

    warnings: list[str] = []
    monkeypatch.setattr(prompt_engine, "log_warning", lambda msg, *a, **k: warnings.append(str(msg)))
    monkeypatch.setattr(prompt_engine, "_warned_missing_memory_exclusion", False)

    message = SimpleNamespace(
        text="do you remember the weather block from the house",
        message_id=1,
        date=SimpleNamespace(isoformat=lambda: "2026-09-21T14:04:08Z"),
    )

    await build_prompt_request(
        message=message,
        context_memory={},
        interface_name="telegram_bot",
    )

    assert any("cannot exclude the current chat" in w for w in warnings), (
        f"a missing exclusion must be reported; got {warnings!r}"
    )

    # ...and only once, even though the warning is reached on every such turn.
    warnings.clear()
    await build_prompt_request(
        message=message,
        context_memory={},
        interface_name="telegram_bot",
    )
    assert warnings == [], "the missing-exclusion warning must fire once per process"


def test_resolver_prefers_the_message_and_falls_back_to_the_context() -> None:
    """The shared resolver is the single source of truth for this value."""
    on_message = SimpleNamespace(interface_path=CURRENT_CHAT)
    assert prompt_engine._resolve_message_interface_path(on_message, {}) == CURRENT_CHAT

    without_path = SimpleNamespace(interface_path=None)
    assert (
        prompt_engine._resolve_message_interface_path(
            without_path, {"interface_path": CURRENT_CHAT}
        )
        == CURRENT_CHAT
    )
    assert prompt_engine._resolve_message_interface_path(without_path, {}) == ""
    assert prompt_engine._resolve_message_interface_path(None, None) == ""
