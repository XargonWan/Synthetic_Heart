"""The temporal block must show each situational note's own validity window.

Live (2026-09-23, the 2D instance): a prompt carried

    [Temporal context]
    - [TSC EVENT] The wedding is tomorrow (2026-09-23); the day is Dee's, ...

on a morning when the human had already said the wedding happened two mornings
earlier. The note's summary is prose written on the day it was filed, so it goes
stale in a way the model cannot see; the window the note was filed FOR is what
makes the claim readable against the Reality Anchor's current date.
"""

from __future__ import annotations

from core.prompt_engine import _build_context_summary, _temporal_note_window


def _note(**overrides: object) -> dict:
    entry = {
        "note_type": "EVENT",
        "subject": "wedding day",
        "summary": "The wedding is tomorrow (2026-09-23); the day is Dee's.",
        "priority": 3,
        "confidence": 0.9,
        "valid_from": "2026-09-23T00:32:10.682082+00:00",
        "valid_until": "2026-09-24T00:00:00+00:00",
        "source": "debrief",
    }
    entry.update(overrides)
    return entry


def test_the_block_shows_the_window_next_to_the_summary() -> None:
    text = _build_context_summary({"soul_temporal_context": [_note()]})

    assert "[Temporal context]" in text
    line = next(line for line in text.splitlines() if line.startswith("- [TSC"))
    assert line.startswith("- [TSC EVENT] [2026-09-23 00:32 -> 2026-09-24 00:00]")
    assert "The wedding is tomorrow (2026-09-23); the day is Dee's." in line


def test_a_note_without_bounds_renders_exactly_as_before() -> None:
    """A producer that supplies no window must not change the block."""
    text = _build_context_summary(
        {
            "soul_temporal_context": [
                _note(valid_from=None, valid_until=None, summary="Moving house.")
            ]
        }
    )

    assert "- [TSC EVENT] Moving house." in text


def test_only_an_end_bound_is_rendered_as_an_end() -> None:
    text = _build_context_summary(
        {"soul_temporal_context": [_note(valid_from=None, summary="Moving house.")]}
    )

    assert "- [TSC EVENT] [until 2026-09-24 00:00] Moving house." in text


def test_the_window_helper_tolerates_junk() -> None:
    assert _temporal_note_window("nonsense") == ""
    assert _temporal_note_window({}) == ""
    assert _temporal_note_window({"valid_from": "  "}) == ""
