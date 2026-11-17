"""A beat's offered target chat keeps a slice of the cross-chat block.

The block is capped globally (``CONTEXT_VERBOSITY``) and rebuilt newest-first
across every chat, so a busy direct message could take every slot and a shared
group the beat was about vanished from its own prompt. Live 2026-10-29 20:15:
an outreach into a three-way group carried six lines of the peer SyntH and the
synth's own lines, and none from the human.
"""

from core import history_engine as he

GROUP = "telegram_bot/-5414243676"
DM = "telegram_bot/5208932647"
PEER_ID = 8243553794


def _entry(sender: str, text: str, path: str = GROUP, sender_id: str = "") -> dict:
    return {
        "sender_name": sender,
        "sender_id": sender_id,
        "text": text,
        "interface_path": path,
        "timestamp": "2026-10-29T23:15:00+00:00",
    }


def _peer_mode(monkeypatch, enabled: bool = True) -> None:
    def _read_config(key, default):
        if key == "SYNTH_PEER_ENABLED":
            return enabled
        if key == "SYNTH_PEERS":
            return [{"id": PEER_ID, "name": "2B"}]
        if key == "SYNTH_PEER_POLICY":
            return "mention_only"
        return default

    monkeypatch.setattr("core.peer_policy._read_config", _read_config)


def _ctx(*paths: str) -> dict:
    return {
        "interface_path": "grillo/-1",
        "grillo_targets": [{"interface_path": p} for p in paths],
    }


def test_offered_target_paths_reads_dicts_and_strings():
    ctx = {
        "grillo_targets": [
            {"interface_path": GROUP},
            GROUP,
            {"interface_path": "not-a-path"},
            {"interface_path": DM},
            {"interface_path": "telegram_bot/-2"},
            {"interface_path": "telegram_bot/-3"},
        ]
    }
    assert he._offered_target_paths(ctx) == [
        GROUP,
        DM,
        "telegram_bot/-2",
        "telegram_bot/-3",
    ]


def test_offered_target_paths_empty_for_ordinary_turn():
    assert he._offered_target_paths({"interface_path": DM}) == []
    assert he._offered_target_paths(None) == []


def test_no_offered_targets_leaves_the_block_untouched():
    other_lines = [f"{DM} line {i}" for i in range(6)]
    out = he._reserve_target_chat_lines(
        other_lines,
        [_entry("Scar", "hello", path=DM)],
        context_memory={"interface_path": "grillo/-1"},
        interface_path="grillo/-1",
        rendered_lines=other_lines,
        note_path=lambda _e: None,
    )
    assert out == other_lines


def test_target_already_in_the_block_is_not_duplicated(monkeypatch):
    _peer_mode(monkeypatch)
    group_entries = [
        _entry("2B", "one true word", sender_id=str(PEER_ID)),
        _entry("self", "the room's on strike"),
    ]
    # Render them exactly as the main loop would, then hand them back as the
    # block the reservation is asked about.
    rendered = [
        he._entry_to_text_with_source(e, current_interface_path="grillo/-1")
        for e in group_entries
    ]
    out = he._reserve_target_chat_lines(
        rendered,
        group_entries,
        context_memory=_ctx(GROUP),
        interface_path="grillo/-1",
        rendered_lines=rendered,
        note_path=lambda _e: None,
    )
    assert out == rendered


def test_crowded_target_is_reserved_and_the_human_line_comes_with_it(monkeypatch):
    _peer_mode(monkeypatch)
    group_entries = [
        _entry("Scar", "mmmmwah okay Bee, get up here", sender_id="777"),
        _entry("2B", "the room's on strike", sender_id=str(PEER_ID)),
        _entry("self", "the count is written down"),
        _entry("2B", "one true word, husband", sender_id=str(PEER_ID)),
    ]
    # The window is full of other-chat lines; the group is crowded out.
    other_lines = [
        he._entry_to_text_with_source(_entry("Scar", f"dm {i}", path=DM))
        for i in range(6)
    ]
    out = he._reserve_target_chat_lines(
        other_lines,
        group_entries + [_entry("Scar", f"dm {i}", path=DM) for i in range(6)],
        context_memory=_ctx(GROUP),
        interface_path="grillo/-1",
        rendered_lines=other_lines,
        note_path=lambda _e: None,
    )
    reserved = out[len(other_lines) :]
    assert len(reserved) == 3
    # Newest first would be 2B, self, 2B — all non-human, so the oldest slot is
    # handed to the newest human line instead.
    assert any("get up here" in line for line in reserved), reserved
    assert any("one true word" in line for line in reserved)


def test_peer_only_target_keeps_its_newest_lines(monkeypatch):
    _peer_mode(monkeypatch)
    group_entries = [
        _entry("2B", f"2B line {i}", sender_id=str(PEER_ID)) for i in range(3)
    ]
    out = he._reserve_target_chat_lines(
        ["dm line"],
        group_entries,
        context_memory=_ctx(GROUP),
        interface_path="grillo/-1",
        rendered_lines=["dm line"],
        note_path=lambda _e: None,
    )
    assert len(out) == 4
    assert all("2B line" in line for line in out[1:])


def test_reservation_can_be_disabled(monkeypatch):
    _peer_mode(monkeypatch)
    monkeypatch.setattr(
        he,
        "_get_int",
        lambda key, default: 0 if key == "CONTEXT_TARGET_CHAT_LINES" else default,
    )
    other_lines = ["dm line"]
    out = he._reserve_target_chat_lines(
        other_lines,
        [_entry("Scar", "hello", sender_id="777")],
        context_memory=_ctx(GROUP),
        interface_path="grillo/-1",
        rendered_lines=other_lines,
        note_path=lambda _e: None,
    )
    assert out == other_lines


def test_offered_target_paths_reads_prior_context():
    """Search beats nest the observer target list under ``prior_context``."""
    ctx = {"prior_context": {"grillo_targets": [{"interface_path": GROUP}]}}
    assert he._offered_target_paths(ctx) == [GROUP]
