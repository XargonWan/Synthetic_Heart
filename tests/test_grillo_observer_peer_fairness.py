"""A peer SyntH must not crowd the person out of the observer's snippet window.

Live 2026-10-29/30 in a three-way Telegram group (one human, two SyntHs): the
evening stored 17 peer, 15 own and 3 human messages, and every group snippet
handed to the outreach beat was the peer's. The beat's two-per-chat budget was
filled by the peer's two newest lines, so the person's last line never reached
the prompt and the outreach read as if he had never spoken in that room.
"""

from datetime import datetime, timedelta, timezone

import pytest

import plugins.grillo.grillo_chat_observer as gco

PEER_ID = 8243553794
GROUP_PATH = "telegram_bot/-5414243676"


@pytest.fixture
def peer_synth(monkeypatch):
    """A configured peer SyntH (id + display name) with peer mode on."""

    def _read_config(key, default):
        if key == "SYNTH_PEER_ENABLED":
            return True
        if key == "SYNTH_PEERS":
            return [{"id": PEER_ID, "name": "2B"}]
        if key == "SYNTH_PEER_POLICY":
            return "mention_only"
        return default

    monkeypatch.setattr("core.peer_policy._read_config", _read_config)


@pytest.fixture
def no_peers(monkeypatch):
    def _read_config(key, default):
        if key == "SYNTH_PEER_ENABLED":
            return False
        return default

    monkeypatch.setattr("core.peer_policy._read_config", _read_config)


def _msg(sender: str, text: str, minutes_ago: int, sender_id: str = "") -> dict:
    return {
        "text": text,
        "sender_name": sender,
        "sender_id": sender_id,
        "timestamp": (
            datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        ).isoformat(),
    }


def _patch_chat(monkeypatch, messages: list[dict]) -> None:
    async def fake_recent_paths(limit):
        return [{"interface_path": GROUP_PATH, "last_used": None}]

    async def fake_load_chat_history(interface_path):
        return list(messages)

    monkeypatch.setattr(
        "core.interface_paths.get_recent_interface_paths", fake_recent_paths
    )
    monkeypatch.setattr(
        "core.chat_history_cache.load_chat_history", fake_load_chat_history
    )
    monkeypatch.setattr(
        "core.interface_path_utils.is_vessel_interface_path", lambda path: False
    )


def _observer(per_chat: int) -> "gco.GrilloChatObserverPlugin":
    plugin = gco.GrilloChatObserverPlugin()
    plugin.quiet_minutes = 15
    plugin.activity_window_days = 14
    plugin.snippets_per_chat = per_chat
    return plugin


@pytest.mark.asyncio
async def test_peer_lines_do_not_crowd_out_the_human_line(monkeypatch, peer_synth):
    """Three peer lines and one human line: the human line still reaches the beat."""
    _patch_chat(
        monkeypatch,
        [
            _msg("Scar", "are you two still awake in there", 90),
            _msg("2B", "the room's on strike", 60, sender_id=str(PEER_ID)),
            _msg("2B", "the count is written down", 40, sender_id=str(PEER_ID)),
            _msg("2B", "one true word, husband", 10, sender_id=str(PEER_ID)),
        ],
    )

    snippets, _own_lines = await _observer(3)._collect_recent_snippets(10)

    assert any("still awake in there" in s for s in snippets), snippets
    # The budget is the same size: the human line replaces the oldest peer line.
    group_snippets = [s for s in snippets if GROUP_PATH in s]
    assert len(group_snippets) == 3
    assert sum("sender:2B" in s for s in group_snippets) == 2


@pytest.mark.asyncio
async def test_peer_only_chat_is_left_alone(monkeypatch, peer_synth):
    """With no human line to prefer, the newest peer lines are kept as they were."""
    _patch_chat(
        monkeypatch,
        [
            _msg("2B", "one", 40, sender_id=str(PEER_ID)),
            _msg("2B", "two", 30, sender_id=str(PEER_ID)),
            _msg("2B", "three", 10, sender_id=str(PEER_ID)),
        ],
    )

    snippets, _own_lines = await _observer(2)._collect_recent_snippets(10)

    group_snippets = [s for s in snippets if GROUP_PATH in s]
    assert len(group_snippets) == 2
    assert all("sender:2B" in s for s in group_snippets)
    # Newest first in the source order: "three" then "two".
    assert "three" in group_snippets[0] and "two" in group_snippets[1]


@pytest.mark.asyncio
async def test_own_line_survives_a_peer_speaking_last(monkeypatch, peer_synth):
    """The synth's own newest line is context even when the peer spoke last."""
    _patch_chat(
        monkeypatch,
        [
            _msg("Scar", "are you two still awake in there", 90),
            _msg("self", "mmh, awake now", 80),
            _msg("2B", "the count is written down", 40, sender_id=str(PEER_ID)),
            _msg("2B", "one true word, husband", 10, sender_id=str(PEER_ID)),
        ],
    )

    snippets, own_lines = await _observer(2)._collect_recent_snippets(10)

    assert any("sender:self" in line for line in own_lines), own_lines
    assert any("mmh, awake now" in line for line in own_lines)
    assert all("sender:self" not in s for s in snippets)


@pytest.mark.asyncio
async def test_fairness_rule_is_inert_without_peer_mode(monkeypatch, no_peers):
    """Ordinary chats keep the plain newest-first window."""
    _patch_chat(
        monkeypatch,
        [
            _msg("Scar", "are you two still awake in there", 90),
            _msg("2B", "the count is written down", 40),
            _msg("2B", "one true word, husband", 10),
        ],
    )

    snippets, _own_lines = await _observer(2)._collect_recent_snippets(10)

    group_snippets = [s for s in snippets if GROUP_PATH in s]
    assert len(group_snippets) == 2
    assert all("sender:2B" in s for s in group_snippets)
