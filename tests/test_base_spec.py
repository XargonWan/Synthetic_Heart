"""Tests for the Minecraft base build spec (``plugins/rift_vessel/minecraft/base_spec.py``).

A base *build* is world-specific, so its layout recipe lives in the Minecraft
adapter. These tests exercise the **pure**, deterministic layout derivation —
no bridge, no DB, no LLM — asserting the plan is a bounded, mob-safe shelter
(floor + walls + roof + door gap + interior torch + crafting table), that it is
inventory-aware (uses carried materials, reports the missing essentials), and
that it is coordinate/id-driven only (never inspects free text).
"""

from __future__ import annotations

from plugins.rift_vessel.minecraft.base_spec import (
    base_anchor,
    base_bounding_box,
    derive_base_layout,
)


def _full_inventory() -> dict[str, int]:
    """An inventory carrying every essential + optional material."""
    return {
        "cobblestone": 128,
        "torch": 8,
        "oak_door": 1,
        "crafting_table": 1,
        "white_bed": 1,
    }


# ---------------------------------------------------------------------------
# base_anchor / base_bounding_box — pure grid math
# ---------------------------------------------------------------------------


def test_base_anchor_is_interior_centre() -> None:
    anchor = base_anchor({"x": 0, "y": 64, "z": 0})
    # Interior is 3 wide/deep, so the centre offset is 1 on x and z.
    assert anchor == {"x": 1, "y": 64, "z": 1}


def test_bounding_box_wraps_interior_by_one() -> None:
    box = base_bounding_box({"x": 0, "y": 64, "z": 0})
    assert box["x1"] == -1 and box["z1"] == -1
    assert box["y1"] == 63  # floor one below the interior
    assert box["y2"] == 66  # roof one above head-room (interior_h = 2)
    assert box["x2"] == 3 and box["z2"] == 3


# ---------------------------------------------------------------------------
# derive_base_layout — full plan with all materials
# ---------------------------------------------------------------------------


def test_full_layout_is_ok_and_bounded() -> None:
    layout = derive_base_layout({"x": 0, "y": 64, "z": 0}, _full_inventory())
    assert layout["ok"] is True
    assert layout["missing"] == []
    # Bounded: a fixed small box, never sprawling.
    assert 0 < len(layout["blocks"]) < 200
    # Every shell block is a carried material at integer coords.
    for b in layout["blocks"]:
        assert b["item"] == "cobblestone"
        assert isinstance(b["x"], int)
        assert isinstance(b["y"], int)
        assert isinstance(b["z"], int)


def test_full_layout_includes_all_fixtures() -> None:
    layout = derive_base_layout({"x": 0, "y": 64, "z": 0}, _full_inventory())
    assert layout["door"] is not None and layout["door"]["item"] == "oak_door"
    assert layout["torch"] is not None and layout["torch"]["item"] == "torch"
    assert (
        layout["crafting_table"] is not None
        and layout["crafting_table"]["item"] == "crafting_table"
    )
    assert layout["bed"] is not None and layout["bed"]["item"] == "white_bed"


def test_door_gap_is_left_open_in_the_shell() -> None:
    layout = derive_base_layout({"x": 0, "y": 64, "z": 0}, _full_inventory())
    door = layout["door"]
    # The two interior-height cells at the door column/row must NOT be walled.
    door_cells = {
        (door["x"], door["y"], door["z"]),
        (door["x"], door["y"] + 1, door["z"]),
    }
    shell = {(b["x"], b["y"], b["z"]) for b in layout["blocks"]}
    assert not (door_cells & shell)


def test_torch_sits_at_interior_centre_floor() -> None:
    layout = derive_base_layout({"x": 0, "y": 64, "z": 0}, _full_inventory())
    anchor = layout["anchor"]
    torch = layout["torch"]
    assert (torch["x"], torch["z"]) == (anchor["x"], anchor["z"])


def test_shell_is_emitted_bottom_up_floor_before_roof() -> None:
    # The bridge places cells in list order and needs a solid neighbour to click
    # against, so the shell must be emitted bottom-up: the whole floor + walls
    # before any roof cell. If the roof came first it would float in air on the
    # first pass and leave the "house not closed" (TASK 3). Guard the order.
    layout = derive_base_layout({"x": 0, "y": 64, "z": 0}, {"cobblestone": 128})
    box = layout["box"]
    y_floor, y_roof = box["y1"], box["y2"]
    last_non_roof_idx = -1
    first_roof_idx = len(layout["blocks"])
    for i, b in enumerate(layout["blocks"]):
        if b["y"] == y_roof:
            first_roof_idx = min(first_roof_idx, i)
        else:
            last_non_roof_idx = max(last_non_roof_idx, i)
    # Every roof cell comes strictly after every floor/wall cell.
    assert first_roof_idx > last_non_roof_idx
    # And the very first emitted cell is the floor slab.
    assert layout["blocks"][0]["y"] == y_floor


# ---------------------------------------------------------------------------
# derive_base_layout — inventory awareness / degradation
# ---------------------------------------------------------------------------


def test_no_wall_material_yields_empty_plan_and_missing() -> None:
    layout = derive_base_layout({"x": 0, "y": 64, "z": 0}, {"torch": 8})
    assert layout["ok"] is False
    assert layout["blocks"] == []
    assert "wall_block" in layout["missing"]


def test_missing_optional_essentials_still_builds_shell() -> None:
    # Walls only: shell builds, but torch + crafting table are flagged missing.
    layout = derive_base_layout({"x": 0, "y": 64, "z": 0}, {"cobblestone": 128})
    assert layout["ok"] is True
    assert layout["blocks"]
    assert "torch" in layout["missing"]
    assert "crafting_table" in layout["missing"]
    assert layout["door"] is None
    assert layout["torch"] is None
    assert layout["bed"] is None


def test_wood_planks_used_when_no_stone() -> None:
    layout = derive_base_layout(
        {"x": 0, "y": 64, "z": 0},
        {"oak_planks": 128, "torch": 4, "crafting_table": 1},
    )
    assert layout["ok"] is True
    assert all(b["item"] == "oak_planks" for b in layout["blocks"])


def test_insufficient_wall_quantity_is_not_ok() -> None:
    # A material is *present* but nowhere near enough to build the shell: the
    # plan must report a quantity shortfall and refuse (ok=False) rather than
    # dispatch an impossible build the bridge can only place 0 of.
    layout = derive_base_layout(
        {"x": 0, "y": 64, "z": 0},
        {"acacia_planks": 10, "torch": 4, "crafting_table": 1},
    )
    assert layout["ok"] is False
    # The shell block list is still derived (for diagnostics) but flagged short.
    assert layout["blocks"]
    assert any(m.startswith("acacia_planks:need ") for m in layout["missing"])


# ---------------------------------------------------------------------------
# derive_base_layout — origin coercion / fail-safe
# ---------------------------------------------------------------------------


def test_bad_origin_is_rejected() -> None:
    layout = derive_base_layout({"x": 0, "y": None, "z": 0}, _full_inventory())
    assert layout["ok"] is False
    assert layout["origin"] is None
    assert "<origin>" in layout["missing"]


def test_float_origin_is_rounded_to_ints() -> None:
    layout = derive_base_layout({"x": 10.6, "y": 64.2, "z": -3.4}, _full_inventory())
    assert layout["origin"] == {"x": 11, "y": 64, "z": -3}


def test_empty_inventory_is_fail_safe() -> None:
    layout = derive_base_layout({"x": 0, "y": 64, "z": 0}, {})
    assert layout["ok"] is False
    assert layout["blocks"] == []
