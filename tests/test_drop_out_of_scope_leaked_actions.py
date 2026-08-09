"""Tests for ``core.message_chain._drop_out_of_scope_leaked_actions``.

A state-retaining external engine (e.g. the browser-driven selenium endpoint)
keeps the conversation history across turns, so on a plain chat turn it can echo
an action it was only offered on an earlier Vessel turn (e.g.
``vessel_minecraft_collect_block``) even though the current scoped prompt never
listed it. That leaked action is not deliverable on the chat interface: it drives
the corrector into the ``😵`` loop. This filter drops any action whose ``type``
is not in the per-turn scoped allowlist recorded from the exact prompt, while
keeping every in-scope deliverable action.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.message_chain import _drop_out_of_scope_leaked_actions


class TestDropOutOfScopeLeakedActions:
    def test_drops_leaked_vessel_action_on_core_turn(self) -> None:
        actions = [
            {"type": "vessel_minecraft_collect_block", "payload": {"name": "sand"}},
        ]
        ctx = {"allowed_action_types": ["message_synth_webui", "stt_transcribe"]}
        result = _drop_out_of_scope_leaked_actions(actions, ctx)
        assert result == []

    def test_keeps_in_scope_action(self) -> None:
        actions = [
            {"type": "message_synth_webui", "payload": {"text": "ciao"}},
        ]
        ctx = {"allowed_action_types": ["message_synth_webui", "stt_transcribe"]}
        result = _drop_out_of_scope_leaked_actions(actions, ctx)
        assert result == actions

    def test_keeps_deliverable_drops_leak_in_mixed_array(self) -> None:
        actions = [
            {"type": "message_synth_webui", "payload": {"text": "ciao"}},
            {"type": "vessel_minecraft_collect_block", "payload": {"name": "sand"}},
        ]
        ctx = {"allowed_action_types": ["message_synth_webui"]}
        result = _drop_out_of_scope_leaked_actions(actions, ctx)
        assert result == [{"type": "message_synth_webui", "payload": {"text": "ciao"}}]

    def test_no_allowlist_leaves_actions_untouched(self) -> None:
        # A turn without a recorded scoped allowlist (e.g. a beat with its own
        # explicit scope) must never be narrowed by this filter.
        actions = [
            {"type": "vessel_minecraft_collect_block", "payload": {"name": "sand"}},
        ]
        assert _drop_out_of_scope_leaked_actions(actions, {}) == actions
        assert (
            _drop_out_of_scope_leaked_actions(actions, {"allowed_action_types": None})
            == actions
        )

    def test_empty_allowlist_leaves_actions_untouched(self) -> None:
        actions = [{"type": "vessel_minecraft_collect_block", "payload": {}}]
        assert (
            _drop_out_of_scope_leaked_actions(actions, {"allowed_action_types": []})
            == actions
        )

    def test_supports_action_key_alias(self) -> None:
        actions = [{"action": "vessel_minecraft_collect_block", "payload": {}}]
        ctx = {"allowed_action_types": ["message_synth_webui"]}
        assert _drop_out_of_scope_leaked_actions(actions, ctx) == []

    def test_reads_allowed_actions_fallback_key(self) -> None:
        actions = [
            {"type": "message_synth_webui", "payload": {"text": "ciao"}},
            {"type": "agent_dispatch", "payload": {}},
        ]
        ctx = {"allowed_actions": ["message_synth_webui"]}
        result = _drop_out_of_scope_leaked_actions(actions, ctx)
        assert result == [{"type": "message_synth_webui", "payload": {"text": "ciao"}}]

    def test_non_dict_actions_preserved(self) -> None:
        actions = ["not-a-dict", {"type": "message_synth_webui", "payload": {}}]
        ctx = {"allowed_action_types": ["message_synth_webui"]}
        result = _drop_out_of_scope_leaked_actions(actions, ctx)
        assert result == actions

    def test_non_list_input_returned_as_is(self) -> None:
        ctx = {"allowed_action_types": ["message_synth_webui"]}
        assert _drop_out_of_scope_leaked_actions(None, ctx) is None  # type: ignore[arg-type]

    def test_non_dict_ctx_returned_as_is(self) -> None:
        actions = [{"type": "vessel_minecraft_collect_block", "payload": {}}]
        assert _drop_out_of_scope_leaked_actions(actions, None) == actions
