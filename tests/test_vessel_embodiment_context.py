"""Tests for the canonical Rift Vessel embodiment detector and the ai_diary
mid-session diary guard (AGENTS.md §5c).

The helper ``core.interface_path_utils.is_vessel_embodiment_context`` is the
single structural detector reused by ``core.agent_router`` and
``plugins.ai_diary``. It must decide purely from routing metadata (never from
message text) and fail safe (return ``False``) on any malformed input.
"""

from core.interface_path_utils import is_vessel_embodiment_context


def test_none_and_non_dict_return_false() -> None:
    assert is_vessel_embodiment_context(None) is False
    assert is_vessel_embodiment_context("vessel/minecraft") is False  # type: ignore[arg-type]
    assert is_vessel_embodiment_context(123) is False  # type: ignore[arg-type]


def test_empty_dict_returns_false() -> None:
    assert is_vessel_embodiment_context({}) is False


def test_explicit_vessel_focus_flag() -> None:
    assert is_vessel_embodiment_context({"vessel_focus": True}) is True


def test_interface_equals_vessel() -> None:
    assert is_vessel_embodiment_context({"interface": "vessel"}) is True


def test_interface_path_prefix() -> None:
    assert is_vessel_embodiment_context({"interface_path": "vessel/minecraft"}) is True


def test_chat_id_prefix() -> None:
    assert is_vessel_embodiment_context({"chat_id": "vessel/minecraft"}) is True


def test_non_vessel_context_returns_false() -> None:
    assert (
        is_vessel_embodiment_context(
            {"interface": "telegram_bot", "interface_path": "telegram_bot/123"}
        )
        is False
    )


def test_partial_match_is_not_prefix() -> None:
    # A path that merely contains 'vessel' but does not start with it must not
    # be treated as an embodiment turn.
    assert (
        is_vessel_embodiment_context({"interface_path": "telegram_bot/vessel"}) is False
    )
