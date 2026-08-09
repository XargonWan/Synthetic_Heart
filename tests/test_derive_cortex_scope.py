"""Unit tests for core.config.derive_cortex_scope.

Covers the scope routing used to pick the cortex engine, including the
diary-merge case: diary consolidation is a grillo-family background task that is
re-dispatched without the ``grillo_beat`` flag, so it must still resolve to the
grillo scope rather than falling through to BASE_CORTEX.
"""

from core.config import derive_cortex_scope


def test_none_context_returns_base():
    assert derive_cortex_scope(None) is None


def test_empty_context_returns_base():
    assert derive_cortex_scope({}) is None


def test_trainer_scope():
    assert derive_cortex_scope({"is_trainer": True}) == "trainer"


def test_grillo_beat_scope():
    assert derive_cortex_scope({"grillo_beat": True}) == "grillo"


def test_diary_merge_routes_to_grillo():
    # Mirrors the context ai_diary enqueues for consolidation.
    ctx = {
        "diary_merge_beat": True,
        "diary_entry_id": 123,
        "allowed_action_types": ["update_diary_entry"],
    }
    assert derive_cortex_scope(ctx) == "grillo"


def test_trainer_takes_precedence_over_diary_merge():
    assert (
        derive_cortex_scope({"is_trainer": True, "diary_merge_beat": True}) == "trainer"
    )


def test_vessel_focus_flag_routes_to_vessel():
    assert derive_cortex_scope({"vessel_focus": True}) == "vessel"


def test_vessel_interface_routes_to_vessel():
    assert derive_cortex_scope({"interface": "vessel"}) == "vessel"


def test_vessel_interface_path_routes_to_vessel():
    # Mirrors the will-beat enqueue: interface_path "vessel/<world>".
    assert derive_cortex_scope({"interface_path": "vessel/minecraft"}) == "vessel"


def test_trainer_takes_precedence_over_vessel():
    assert derive_cortex_scope({"is_trainer": True, "vessel_focus": True}) == "trainer"
