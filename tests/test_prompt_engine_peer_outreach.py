"""The peer instruction block must also reach a proactive beat.

A beat runs in an internal ``grillo/-1`` chat, so it never looks like a group
message; ``_peer_context_form`` decides from the offered target list instead.
Live 2026-10-29 20:15: the outreach into the three-way group carried no peer
block at all, so the peer SyntH's lines read exactly like the trainer's.
"""

from types import SimpleNamespace

from core.prompt_engine import _peer_context_form

GROUP = "telegram_bot/-5414243676"
DM = "telegram_bot/5208932647"
PEER_ID = 8243553794


def _peer_mode(monkeypatch, enabled: bool = True, rows=None):
    rows = [{"id": PEER_ID, "name": "2B"}] if rows is None else rows

    def _read_config(key, default):
        if key == "SYNTH_PEER_ENABLED":
            return enabled
        if key == "SYNTH_PEERS":
            return rows
        if key == "SYNTH_PEER_POLICY":
            return "mention_only"
        return default

    monkeypatch.setattr("core.peer_policy._read_config", _read_config)


def _msg(chat_type: str):
    return SimpleNamespace(chat=SimpleNamespace(id=-1, type=chat_type))


def _beat_ctx(*paths: str) -> dict:
    return {
        "interface_path": "grillo/-1",
        "grillo_targets": [{"interface_path": p} for p in paths],
    }


def test_group_message_keeps_the_chat_form(monkeypatch):
    _peer_mode(monkeypatch)
    assert _peer_context_form(_msg("group"), "telegram_bot", {}) == "chat"
    assert _peer_context_form(_msg("supergroup"), "telegram_bot", {}) == "chat"


def test_beat_reaching_into_a_peer_group_gets_the_outreach_form(monkeypatch):
    _peer_mode(monkeypatch)
    assert (
        _peer_context_form(_msg("internal"), "grillo", _beat_ctx(GROUP)) == "outreach"
    )


def test_beat_with_only_a_direct_message_target_gets_nothing(monkeypatch):
    _peer_mode(monkeypatch)
    assert _peer_context_form(_msg("internal"), "grillo", _beat_ctx(DM)) is None


def test_beat_without_offered_targets_gets_nothing(monkeypatch):
    _peer_mode(monkeypatch)
    assert (
        _peer_context_form(_msg("internal"), "grillo", {"interface_path": "grillo/-1"})
        is None
    )


def test_private_chat_still_gets_no_peer_block(monkeypatch):
    _peer_mode(monkeypatch)
    assert _peer_context_form(_msg("private"), "telegram_bot", {}) is None


def test_beat_block_requires_peer_mode(monkeypatch):
    _peer_mode(monkeypatch, enabled=False)
    assert _peer_context_form(_msg("internal"), "grillo", _beat_ctx(GROUP)) is None

    _peer_mode(monkeypatch, rows=[])
    assert _peer_context_form(_msg("internal"), "grillo", _beat_ctx(GROUP)) is None


def test_beat_with_target_only_in_prior_context(monkeypatch):
    """A search beat nests the offered list under ``prior_context``."""
    _peer_mode(monkeypatch)
    ctx = {
        "interface_path": "web_search/main",
        "prior_context": {"grillo_targets": [{"interface_path": GROUP}]},
    }
    assert _peer_context_form(_msg("internal"), "grillo", ctx) == "outreach"
