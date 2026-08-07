"""Unit tests for the world-agnostic Rift Vessel goal debrief helpers.

Covers only the pure/structural mechanism in ``core.vessel_goal_debrief`` and
the Minecraft-adapter product resolver ``target_names.derive_products`` — no DB,
connector or LLM. See AGENTS.md §5c.
"""

from __future__ import annotations

from typing import Any, Dict

from core import vessel_goal_debrief as vgd


def _cfg_from(mapping: Dict[str, Any]):
    def _get(key: str, default: Any) -> Any:
        return mapping.get(key, default)

    return _get


# --- is_debrief_enabled -----------------------------------------------------


def test_is_debrief_enabled_default_on() -> None:
    assert vgd.is_debrief_enabled(_cfg_from({})) is True


def test_is_debrief_enabled_explicit_off() -> None:
    assert (
        vgd.is_debrief_enabled(_cfg_from({"VESSEL_GOAL_DEBRIEF_ENABLED": False}))
        is False
    )
    assert (
        vgd.is_debrief_enabled(_cfg_from({"VESSEL_GOAL_DEBRIEF_ENABLED": "false"}))
        is False
    )


def test_is_debrief_enabled_failsafe() -> None:
    def _boom(_key: str, _default: Any) -> Any:
        raise RuntimeError("boom")

    assert vgd.is_debrief_enabled(_boom) is True


# --- is_debrief_history_enabled ---------------------------------------------


def test_is_debrief_history_enabled_default_on() -> None:
    assert vgd.is_debrief_history_enabled(_cfg_from({})) is True


def test_is_debrief_history_enabled_explicit_off() -> None:
    assert (
        vgd.is_debrief_history_enabled(
            _cfg_from({"VESSEL_GOAL_DEBRIEF_USE_HISTORY": False})
        )
        is False
    )
    assert (
        vgd.is_debrief_history_enabled(
            _cfg_from({"VESSEL_GOAL_DEBRIEF_USE_HISTORY": "false"})
        )
        is False
    )


def test_is_debrief_history_enabled_failsafe() -> None:
    def _boom(_key: str, _default: Any) -> Any:
        raise RuntimeError("boom")

    assert vgd.is_debrief_history_enabled(_boom) is True


# --- resolve_debrief_interval ----------------------------------------------


def test_resolve_debrief_interval_default() -> None:
    assert vgd.resolve_debrief_interval(_cfg_from({})) == 30


def test_resolve_debrief_interval_clamped() -> None:
    assert (
        vgd.resolve_debrief_interval(_cfg_from({"VESSEL_GOAL_DEBRIEF_INTERVAL_SEC": 1}))
        == 5
    )
    assert (
        vgd.resolve_debrief_interval(
            _cfg_from({"VESSEL_GOAL_DEBRIEF_INTERVAL_SEC": 99999})
        )
        == 3600
    )


def test_resolve_debrief_interval_bad_value() -> None:
    assert (
        vgd.resolve_debrief_interval(
            _cfg_from({"VESSEL_GOAL_DEBRIEF_INTERVAL_SEC": "x"})
        )
        == 30
    )


# --- resolve_stall_ticks ----------------------------------------------------


def test_resolve_stall_ticks_default() -> None:
    assert vgd.resolve_stall_ticks(_cfg_from({})) == 4


def test_resolve_stall_ticks_clamped() -> None:
    assert (
        vgd.resolve_stall_ticks(_cfg_from({"VESSEL_GOAL_DEBRIEF_STALL_TICKS": 1})) == 2
    )
    assert (
        vgd.resolve_stall_ticks(_cfg_from({"VESSEL_GOAL_DEBRIEF_STALL_TICKS": 500}))
        == 100
    )


# --- goal_signature ---------------------------------------------------------


def test_goal_signature_none_for_no_goal() -> None:
    assert vgd.goal_signature(None) is None
    assert vgd.goal_signature({}) is None
    assert vgd.goal_signature({"description": "x"}) is None


def test_goal_signature_combines_progress_fields() -> None:
    goal = {"id": 7, "current_step": 2, "updated_at": "2026-01-01T00:00:00"}
    assert vgd.goal_signature(goal) == "7:2:2026-01-01T00:00:00"


def test_goal_signature_changes_on_progress() -> None:
    a = {"id": 7, "current_step": 1, "updated_at": "t1"}
    b = {"id": 7, "current_step": 2, "updated_at": "t2"}
    assert vgd.goal_signature(a) != vgd.goal_signature(b)


# --- update_stall_state -----------------------------------------------------


def test_update_stall_state_no_goal_resets() -> None:
    state: Dict[str, Any] = {"sig": "7:1:t", "count": 3}
    assert vgd.update_stall_state(state, None, 4) is False
    assert state == {"sig": None, "count": 0}


def test_update_stall_state_progress_resets_counter() -> None:
    state: Dict[str, Any] = {"sig": None, "count": 0}
    g1 = {"id": 1, "current_step": 0, "updated_at": "t1"}
    vgd.update_stall_state(state, g1, 4)
    vgd.update_stall_state(state, g1, 4)
    assert state["count"] == 1
    # Real progress: new signature -> counter back to 0.
    g2 = {"id": 1, "current_step": 1, "updated_at": "t2"}
    assert vgd.update_stall_state(state, g2, 4) is False
    assert state["count"] == 0
    assert state["sig"] == vgd.goal_signature(g2)


def test_update_stall_state_reaches_threshold() -> None:
    state: Dict[str, Any] = {"sig": None, "count": 0}
    goal = {"id": 9, "current_step": 0, "updated_at": "frozen"}
    results = [vgd.update_stall_state(state, goal, 4) for _ in range(6)]
    # First call establishes the signature (count 0); it takes ``stall_ticks``
    # *unchanged* follow-up ticks to reach the threshold.
    assert results == [False, False, False, False, True, True]


# --- Minecraft adapter: derive_products -------------------------------------


def test_derive_products_matches_tools_and_intermediates() -> None:
    from plugins.rift_vessel.minecraft import target_names as tn

    assert tn.derive_products("craft a wooden pickaxe") == ["wooden_pickaxe"]
    assert tn.derive_products("make a crafting table") == ["crafting_table"]
    assert tn.derive_products("build a furnace to smelt iron") == ["furnace"]


def test_derive_products_spaced_form() -> None:
    from plugins.rift_vessel.minecraft import target_names as tn

    assert "stone_pickaxe" in tn.derive_products("I want a stone pickaxe")


def test_derive_products_empty_and_none() -> None:
    from plugins.rift_vessel.minecraft import target_names as tn

    assert tn.derive_products(None) == []
    assert tn.derive_products("") == []
    assert tn.derive_products("explore the caves") == []


def test_derive_products_distinct_in_match_order() -> None:
    from plugins.rift_vessel.minecraft import target_names as tn

    result = tn.derive_products("craft a crafting table then a wooden pickaxe")
    assert set(result) == {"crafting_table", "wooden_pickaxe"}


# --- Minecraft adapter: evaluate_goal_completion_from_history ---------------
#
# History-based goal completion (TASK 1). Many goals leave no inventory trace
# (place a block, kill a mob, say something), so the inventory scan alone never
# marks them done. This half inspects the session's ``vessel_activity_log`` and
# confirms completion when a SUCCESSFUL action row matches the goal's concrete
# structural target id. Matching is purely structural, by canonical Minecraft
# id — never by parsing free text. Fully fail-safe.


def _patch_rows(monkeypatch: Any, rows: list[dict[str, Any]]) -> None:
    """Patch the activity-log loader the connector imports at call time."""

    async def _fake_load(_session_id: str) -> list[dict[str, Any]]:
        return rows

    import core.vessel_diary_compactor as vdc

    monkeypatch.setattr(vdc, "load_activity_rows", _fake_load)


def _conn():
    from plugins.rift_vessel.minecraft.minecraft import MinecraftConnector

    return MinecraftConnector()


async def test_history_no_goal_or_session_is_unsatisfied(monkeypatch: Any) -> None:
    conn = _conn()
    assert (await conn.evaluate_goal_completion_from_history(None, "s1"))[
        "satisfied"
    ] is False
    assert (
        await conn.evaluate_goal_completion_from_history(
            {"target_kind": "block", "target_name": "stone"}, None
        )
    )["satisfied"] is False


async def test_history_block_place_matches_placed_block(monkeypatch: Any) -> None:
    _patch_rows(
        monkeypatch,
        [{"event_type": "action_place", "metadata": {"target": "oak_planks"}}],
    )
    conn = _conn()
    res = await conn.evaluate_goal_completion_from_history(
        {"target_kind": "block", "target_name": "oak_planks"}, "s1"
    )
    assert res["satisfied"] is True
    assert res["reason"] == "action_in_history"
    assert res["item"] == "oak_planks"
    assert res["event_type"] == "action_place"


async def test_history_block_mine_matches(monkeypatch: Any) -> None:
    _patch_rows(
        monkeypatch,
        [{"event_type": "action_mine", "metadata": {"name": "iron_ore"}}],
    )
    conn = _conn()
    res = await conn.evaluate_goal_completion_from_history(
        {"target_kind": "block", "target_name": "iron_ore"}, "s1"
    )
    assert res["satisfied"] is True


async def test_history_entity_kill_matches(monkeypatch: Any) -> None:
    _patch_rows(
        monkeypatch,
        [{"event_type": "action_attack", "metadata": {"target": "zombie"}}],
    )
    conn = _conn()
    res = await conn.evaluate_goal_completion_from_history(
        {"target_kind": "entity", "target_name": "zombie"}, "s1"
    )
    assert res["satisfied"] is True
    assert res["item"] == "zombie"


async def test_history_craft_product_matches(monkeypatch: Any) -> None:
    _patch_rows(
        monkeypatch,
        [{"event_type": "action_craft", "metadata": {"item": "wooden_pickaxe"}}],
    )
    conn = _conn()
    res = await conn.evaluate_goal_completion_from_history(
        {"description": "craft a wooden pickaxe"}, "s1"
    )
    assert res["satisfied"] is True
    assert res["item"] == "wooden_pickaxe"


async def test_history_wrong_event_type_does_not_match(monkeypatch: Any) -> None:
    # A block target must NOT be satisfied by an entity-kill event, even if the
    # id happened to appear — event class and id must both line up.
    _patch_rows(
        monkeypatch,
        [{"event_type": "action_attack", "metadata": {"target": "stone"}}],
    )
    conn = _conn()
    res = await conn.evaluate_goal_completion_from_history(
        {"target_kind": "block", "target_name": "stone"}, "s1"
    )
    assert res["satisfied"] is False


async def test_history_wrong_id_does_not_match(monkeypatch: Any) -> None:
    _patch_rows(
        monkeypatch,
        [{"event_type": "action_mine", "metadata": {"target": "dirt"}}],
    )
    conn = _conn()
    res = await conn.evaluate_goal_completion_from_history(
        {"target_kind": "block", "target_name": "diamond_ore"}, "s1"
    )
    assert res["satisfied"] is False


async def test_history_empty_rows_is_unsatisfied(monkeypatch: Any) -> None:
    _patch_rows(monkeypatch, [])
    conn = _conn()
    res = await conn.evaluate_goal_completion_from_history(
        {"target_kind": "block", "target_name": "stone"}, "s1"
    )
    assert res["satisfied"] is False


async def test_history_loader_error_is_failsafe(monkeypatch: Any) -> None:
    async def _boom(_session_id: str) -> list[dict[str, Any]]:
        raise RuntimeError("db down")

    import core.vessel_diary_compactor as vdc

    monkeypatch.setattr(vdc, "load_activity_rows", _boom)
    conn = _conn()
    res = await conn.evaluate_goal_completion_from_history(
        {"target_kind": "block", "target_name": "stone"}, "s1"
    )
    assert res["satisfied"] is False


async def test_history_failed_action_row_is_not_satisfied(monkeypatch: Any) -> None:
    # A craft ATTEMPT that failed (logged with ``_result.ok: False``, e.g. no
    # recipe / not enough materials) must NOT auto-complete the goal — otherwise
    # a goal is closed from a failed attempt and the body just walks around with
    # nothing to pursue (the reported bug).
    _patch_rows(
        monkeypatch,
        [
            {
                "event_type": "action_craft",
                "metadata": {
                    "item": "wooden_pickaxe",
                    "_result": {"ok": False, "detail": "no craftable recipe"},
                },
            }
        ],
    )
    conn = _conn()
    res = await conn.evaluate_goal_completion_from_history(
        {"description": "craft a wooden pickaxe"}, "s1"
    )
    assert res["satisfied"] is False


async def test_history_failed_block_action_row_is_not_satisfied(
    monkeypatch: Any,
) -> None:
    _patch_rows(
        monkeypatch,
        [
            {
                "event_type": "action_mine",
                "metadata": {
                    "target": "stone",
                    "_result": {"ok": False, "detail": "need a better tool"},
                },
            }
        ],
    )
    conn = _conn()
    res = await conn.evaluate_goal_completion_from_history(
        {"target_kind": "block", "target_name": "stone"}, "s1"
    )
    assert res["satisfied"] is False


async def test_history_successful_action_row_is_satisfied(monkeypatch: Any) -> None:
    # A craft attempt that SUCCEEDED (``_result.ok: True``) is real progress and
    # still auto-completes the goal.
    _patch_rows(
        monkeypatch,
        [
            {
                "event_type": "action_craft",
                "metadata": {
                    "item": "wooden_pickaxe",
                    "_result": {"ok": True, "detail": "crafted 1x wooden_pickaxe"},
                },
            }
        ],
    )
    conn = _conn()
    res = await conn.evaluate_goal_completion_from_history(
        {"description": "craft a wooden pickaxe"}, "s1"
    )
    assert res["satisfied"] is True
    assert res["item"] == "wooden_pickaxe"
