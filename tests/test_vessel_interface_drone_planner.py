"""Unit tests for the out-of-band drone-planner routing helpers.

These cover the two *pure* static helpers used by the anti-circling fallback in
:class:`interface.vessel_interface.VesselInterface` — deciding whether a goal
already carries a walkable waypoint, and clamping the planner cooldown. Both are
side-effect free (no DB, no bridge, no LLM), so they can be exercised directly.
"""

from __future__ import annotations

from typing import Any

from interface.vessel_interface import VesselInterface


class TestGoalHasRoute:
    def test_none_goal_is_not_routable(self) -> None:
        assert VesselInterface._goal_has_route(None) is False

    def test_non_dict_goal_is_not_routable(self) -> None:
        assert VesselInterface._goal_has_route("explore") is False  # type: ignore[arg-type]

    def test_directionless_goal_is_not_routable(self) -> None:
        goal: dict[str, Any] = {"id": 1, "description": "explore the world"}
        assert VesselInterface._goal_has_route(goal) is False

    def test_block_target_is_routable(self) -> None:
        goal = {"target_kind": "block", "target_name": "oak_log"}
        assert VesselInterface._goal_has_route(goal) is True

    def test_entity_target_is_routable(self) -> None:
        goal = {"target_kind": "entity", "target_name": "cow"}
        assert VesselInterface._goal_has_route(goal) is True

    def test_empty_target_name_is_not_routable(self) -> None:
        goal = {"target_kind": "block", "target_name": "   "}
        assert VesselInterface._goal_has_route(goal) is False

    def test_coordinate_target_kind_alone_is_not_routable(self) -> None:
        # "coordinate" is not a walkable name — a destination is what steers.
        goal = {"target_kind": "coordinate", "target_name": ""}
        assert VesselInterface._goal_has_route(goal) is False

    def test_numeric_destination_alone_is_not_routable(self) -> None:
        # A bare destination only says *where to walk*, not *what to do* there;
        # the planner must still fire to assign a concrete gameplay target.
        goal = {"destination": {"x": 100, "z": -40}}
        assert VesselInterface._goal_has_route(goal) is False

    def test_partial_destination_dict_is_not_routable(self) -> None:
        goal = {"destination": {"x": 100}}
        assert VesselInterface._goal_has_route(goal) is False

    def test_flat_destination_alone_is_not_routable(self) -> None:
        goal = {"destination_x": 12.0, "destination_z": -8.0}
        assert VesselInterface._goal_has_route(goal) is False

    def test_target_wins_even_with_destination(self) -> None:
        # A goal with a real block/entity target is routable regardless of any
        # destination it also carries.
        goal = {
            "target_kind": "block",
            "target_name": "iron_ore",
            "destination": {"x": 100, "z": -40},
        }
        assert VesselInterface._goal_has_route(goal) is True


class TestResolveDronePlanInterval:
    def _cfg(self, value: Any) -> Any:
        def cfg(key: str, default: Any) -> Any:
            return value

        return cfg

    def test_default_when_unset(self) -> None:
        assert VesselInterface._resolve_drone_plan_interval(self._cfg(120)) == 120.0

    def test_clamped_low(self) -> None:
        assert VesselInterface._resolve_drone_plan_interval(self._cfg(1)) == 30.0

    def test_clamped_high(self) -> None:
        assert VesselInterface._resolve_drone_plan_interval(self._cfg(999999)) == 3600.0

    def test_non_numeric_falls_back_to_default(self) -> None:
        assert VesselInterface._resolve_drone_plan_interval(self._cfg("nope")) == 120.0
