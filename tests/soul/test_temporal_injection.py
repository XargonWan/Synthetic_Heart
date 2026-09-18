"""Tests for the situational-notes block that is rendered into a prompt.

Context (live, 2026-09-18): the debrief writes a fresh note every time it
re-describes a circumstance, so the active store held 64 notes and a single
prompt carried 28 of them - among them 12 accounts of one evening gathering,
some contradicting each other ("happened last night and went fine" next to
"expected tonight"). The store keeps every note; this block is what the model is
shown, so it is ranked and bounded here.
"""

from __future__ import annotations

from core.soul.situational import is_same_circumstance, subject_tokens

from plugins.soul_plugin.soul_plugin import SoulPlugin


def _note(subject: str, *, priority: int = 0, confidence: float = 0.5) -> dict:
    return {
        "note_type": "EVENT",
        "subject": subject,
        "summary": f"summary about {subject}",
        "priority": priority,
        "confidence": confidence,
        "valid_from_relative": "an hour ago",
        "valid_until_relative": "in 5 hours",
        "source": "debrief",
    }


def test_subject_tokens_drop_time_words_and_bare_names() -> None:
    assert subject_tokens("Gathering at Sandro's") == {"gathering", "sandro"}
    assert subject_tokens("Gathering at Sandro's tonight") == {
        "gathering",
        "sandro",
    }
    assert subject_tokens("Scar") == {"scar"}
    assert subject_tokens(None) == set()


def test_is_same_circumstance_ignores_bare_names() -> None:
    gathering = subject_tokens("Gathering at Sandro's tonight")
    assert is_same_circumstance(gathering, subject_tokens("Gathering at Sandro's"))
    assert is_same_circumstance(
        gathering, subject_tokens("Gathering at Sandro's place")
    )
    assert not is_same_circumstance(gathering, subject_tokens("Scar"))
    assert not is_same_circumstance(gathering, subject_tokens("Anniversary today"))
    assert not is_same_circumstance(subject_tokens("Scar"), subject_tokens("Scarlet"))


def test_duplicate_accounts_of_one_event_collapse_to_one_note() -> None:
    notes = [
        _note("Gathering at Sandro's tonight", priority=2, confidence=0.8),
        _note("Gathering at Sandro's", priority=2, confidence=0.75),
        _note("Gathering at Sandro's place", priority=1, confidence=0.6),
        _note("gathering tomorrow", priority=2, confidence=0.85),
        _note("Human", priority=1, confidence=0.6),
    ]

    selected = SoulPlugin.select_temporal_notes(notes, limit=8)

    subjects = [note["subject"] for note in selected]
    # One account of the gathering survives (the highest ranked), plus "Human",
    # which names a person rather than a circumstance and so cannot stand in for
    # another note. "gathering tomorrow" names the same event without a place,
    # so it is a separate note and is kept.
    assert "Gathering at Sandro's tonight" in subjects
    assert "Gathering at Sandro's" not in subjects
    assert "Gathering at Sandro's place" not in subjects
    assert "Human" in subjects
    assert len(selected) < len(notes)


def test_block_is_capped_and_ranked_by_priority_then_confidence() -> None:
    notes = [
        _note("quiet low", priority=0, confidence=0.9),
        _note("urgent low", priority=2, confidence=0.1),
        _note("urgent high", priority=2, confidence=0.9),
        _note("medium", priority=1, confidence=0.5),
    ]

    selected = SoulPlugin.select_temporal_notes(notes, limit=2)

    assert [note["subject"] for note in selected] == ["urgent high", "urgent low"]


def test_limit_is_honoured_and_never_zero() -> None:
    notes = [_note(f"topic alpha{i}") for i in range(10)]

    assert len(SoulPlugin.select_temporal_notes(notes, limit=3)) == 3
    assert len(SoulPlugin.select_temporal_notes(notes, limit=0)) == 1
    assert len(SoulPlugin.select_temporal_notes(notes, limit=40)) == 10


def test_unrelated_subjects_are_all_kept() -> None:
    notes = [
        _note("Anniversary of best friend's death", priority=2, confidence=0.85),
        _note("Scar takes twice-daily medication", priority=1, confidence=0.7),
        _note("Memcell compiler bug investigation", priority=2, confidence=0.7),
    ]

    selected = SoulPlugin.select_temporal_notes(notes, limit=8)

    assert len(selected) == 3
