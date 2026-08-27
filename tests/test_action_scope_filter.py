"""Tests for the per-turn action-scope filtering in ``core.prompt_engine``.

Covers the Hybrid-C dynamic scope gate: the Fast-Lane chat prompt should only
list actions whose declared ``scope`` (or structural name-prefix fallback) is
visible for the current turn, while every action stays REGISTERED and callable.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from core.prompt_engine import (
    _action_scopes,
    _action_scopes_by_name,
    _derive_default_prompt_action_types,
)


class TestActionScopes:
    def test_explicit_string_scope(self) -> None:
        assert _action_scopes({"scope": "vessel"}) == {"vessel"}

    def test_explicit_list_scope(self) -> None:
        assert _action_scopes({"scope": ["vessel", "recon"]}) == {"vessel", "recon"}

    def test_blank_scope_falls_back_to_core(self) -> None:
        assert _action_scopes({"scope": "  "}) == {"core"}

    def test_missing_scope_defaults_core(self) -> None:
        assert _action_scopes({"required_fields": []}) == {"core"}

    def test_non_dict_defaults_core(self) -> None:
        assert _action_scopes(None) == {"core"}


class TestActionScopesByName:
    def test_explicit_scope_wins_over_prefix(self) -> None:
        # ``vessel_connect`` is deliberately tagged ``core`` so it stays visible.
        assert _action_scopes_by_name("vessel_connect", {"scope": "core"}) == {"core"}

    def test_vessel_prefix_fallback(self) -> None:
        assert _action_scopes_by_name("vessel_say", {}) == {"vessel"}

    def test_agent_prefix_fallback(self) -> None:
        assert _action_scopes_by_name("agent_read_file", {}) == {"agent"}

    def test_unprefixed_defaults_core(self) -> None:
        assert _action_scopes_by_name("note_to_self", {}) == {"core"}


class TestDeriveDefaultPromptActionTypes:
    def _actions(self) -> dict[str, dict]:
        return {
            "reply_to_user": {"scope": "core"},
            "note_to_self": {},  # defaults to core
            "spawn_drone": {"scope": "core"},  # escape hatch stays visible
            "vessel_say": {"scope": "vessel"},
            "agent_read_file": {"scope": "agent"},
        }

    def test_core_only_turn_hides_vessel_and_agent(self) -> None:
        allowed = _derive_default_prompt_action_types(
            self._actions(), interface_name=None, turn_scopes={"core"}
        )
        assert "reply_to_user" in allowed
        assert "note_to_self" in allowed
        assert "spawn_drone" in allowed
        assert "vessel_say" not in allowed
        assert "agent_read_file" not in allowed

    def test_vessel_turn_reveals_vessel_actions(self) -> None:
        allowed = _derive_default_prompt_action_types(
            self._actions(), interface_name=None, turn_scopes={"core", "vessel"}
        )
        assert "vessel_say" in allowed
        assert "reply_to_user" in allowed
        # Agent scope is never added on a Fast-Lane turn.
        assert "agent_read_file" not in allowed

    def test_none_turn_scopes_keeps_all(self) -> None:
        # ``turn_scopes=None`` disables the gate entirely (back-compat path).
        allowed = _derive_default_prompt_action_types(
            self._actions(), interface_name=None, turn_scopes=None
        )
        assert "vessel_say" in allowed
        assert "agent_read_file" in allowed
