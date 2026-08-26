# plugins/rift_vessel/minecraft/base_spec.py
"""Minecraft base (shelter) layout spec — a hardcoded, structural build recipe.

A *base* build is a **world-specific** concern, so it lives in the Minecraft
adapter rather than the Rift Vessel core. This module derives, from an origin
cell and the body's current inventory, the exact list of block placements that
form a small, mob-safe first shelter: a walled + roofed + floored box with one
door to get in and out, at least one interior torch (so nothing spawns inside),
and a crafting table (so Synth can keep building from home). A bed is optional
(the *second* objective) and only added when a bed block is carried.

Design rules
------------
* **Structural only.** Every material is a canonical Minecraft item/block id
  (permitted, per the scope rules, because block ids are structural — never
  human chat text). Layout is pure grid math off the origin; nothing here reads
  or infers intent from language.
* **Bounded.** The volume is a fixed small box (interior ~3x2x3), so a build can
  never sprawl or loop — the placement count is a constant.
* **Inventory-aware, fail-open.** :func:`derive_base_layout` picks the first
  wall/floor material actually carried and reports what is missing, but never
  raises: an empty or unusable inventory yields an empty block list plus a
  ``missing`` report the caller can surface as a "you need N blocks" cue.
* **Deterministic.** Same origin + inventory always yields the same layout, so
  the connector can register the resulting base anchor/box reliably.
"""

from __future__ import annotations

from typing import Any, Dict, List

# --- Material preference lists (canonical Minecraft ids, most→least wanted) ---
# The build tries each candidate in order and uses the first the body carries.
# These are structural id lists, not chat keywords.
_WALL_MATERIALS: tuple[str, ...] = (
    "cobblestone",
    "stone",
    "oak_planks",
    "spruce_planks",
    "birch_planks",
    "jungle_planks",
    "acacia_planks",
    "dark_oak_planks",
    "dirt",
)
_FLOOR_MATERIALS: tuple[str, ...] = _WALL_MATERIALS
_ROOF_MATERIALS: tuple[str, ...] = _WALL_MATERIALS
_TORCH_MATERIALS: tuple[str, ...] = ("torch",)
_DOOR_MATERIALS: tuple[str, ...] = (
    "oak_door",
    "spruce_door",
    "birch_door",
    "jungle_door",
    "acacia_door",
    "dark_oak_door",
)
_TABLE_MATERIALS: tuple[str, ...] = ("crafting_table",)
_BED_MATERIALS: tuple[str, ...] = (
    "white_bed",
    "red_bed",
    "orange_bed",
    "yellow_bed",
    "lime_bed",
    "green_bed",
    "cyan_bed",
    "light_blue_bed",
    "blue_bed",
    "purple_bed",
    "magenta_bed",
    "pink_bed",
    "brown_bed",
    "black_bed",
    "gray_bed",
    "light_gray_bed",
)

# Interior footprint (in blocks). The shell adds one block of wall on every
# side, so the outer footprint is (INTERIOR + 2) on x/z and (INTERIOR_H + 2) on
# y (floor + roof). Kept small and constant so a build is bounded.
_INTERIOR_W = 3  # along +x
_INTERIOR_D = 3  # along +z
_INTERIOR_H = 2  # head-room (feet + head)


def _first_available(
    candidates: tuple[str, ...], inventory_counts: Dict[str, int]
) -> str | None:
    """Return the first candidate id present (count > 0) in ``inventory_counts``.

    Purely structural id matching against the live inventory snapshot. Returns
    ``None`` when the body carries none of the candidates.
    """
    if not inventory_counts:
        return None
    for cid in candidates:
        try:
            if int(inventory_counts.get(cid, 0)) > 0:
                return cid
        except (TypeError, ValueError):
            continue
    return None


def _coerce_origin(origin: Any) -> Dict[str, int] | None:
    """Coerce an origin into integer ``{x, y, z}`` block coordinates or ``None``.

    The origin is the interior floor corner (minimum x/y/z of the interior). It
    accepts any mapping with numeric ``x``/``y``/``z``; anything else yields
    ``None`` so the caller can fall back to the live body position.
    """
    if not isinstance(origin, dict):
        return None
    out: Dict[str, int] = {}
    for axis in ("x", "y", "z"):
        val = origin.get(axis)
        if val is None:
            return None
        try:
            out[axis] = int(round(float(val)))
        except (TypeError, ValueError):
            return None
    return out


def base_bounding_box(origin: Dict[str, int]) -> Dict[str, int]:
    """Return the outer bounding box (shell included) for an interior ``origin``.

    ``origin`` is the interior floor min-corner. The shell wraps the interior by
    one block on every side, so the outer box spans from ``origin - 1`` (floor)
    to the far interior corner + 1 (walls/roof). Structural grid math.
    """
    ox, oy, oz = origin["x"], origin["y"], origin["z"]
    return {
        "x1": ox - 1,
        "y1": oy - 1,
        "z1": oz - 1,
        "x2": ox + _INTERIOR_W,
        "y2": oy + _INTERIOR_H,
        "z2": oz + _INTERIOR_D,
    }


def base_anchor(origin: Dict[str, int]) -> Dict[str, int]:
    """Return the interior-centre anchor point (the "home" retreat coordinate).

    Used to register the base in the store so a night-retreat ``goto`` walks to
    the middle of the shelter interior. Structural grid math.
    """
    return {
        "x": origin["x"] + _INTERIOR_W // 2,
        "y": origin["y"],
        "z": origin["z"] + _INTERIOR_D // 2,
    }


def derive_base_layout(
    origin: Any,
    inventory_counts: Dict[str, int],
) -> Dict[str, Any]:
    """Derive the full block layout for a first shelter from ``origin``.

    ``origin`` is the interior floor min-corner (integer ``{x, y, z}``);
    ``inventory_counts`` is the live ``{item_id: count}`` snapshot. Returns a
    structural build plan::

        {
          "ok": bool,                 # a wall material was available
          "origin": {x, y, z},        # normalised interior floor min-corner
          "anchor": {x, y, z},        # interior-centre retreat point
          "box": {x1..z2},            # outer bounding box (shell included)
          "blocks": [{x, y, z, item}],# every wall/floor/roof placement
          "door": {x, y, z, item} | None,
          "torch": {x, y, z, item} | None,
          "crafting_table": {x, y, z, item} | None,
          "bed": {x, y, z, item} | None,
          "missing": [item_id, ...],  # essential materials not carried
        }

    The plan lists solid shell cells (floor, four walls, roof), leaving one wall
    cell open for the door, plus the door/torch/table/bed placements when their
    materials are carried. Purely structural; never raises.
    """
    normalised = _coerce_origin(origin)
    counts = inventory_counts if isinstance(inventory_counts, dict) else {}

    result: Dict[str, Any] = {
        "ok": False,
        "origin": normalised,
        "anchor": None,
        "box": None,
        "blocks": [],
        "door": None,
        "torch": None,
        "crafting_table": None,
        "bed": None,
        "missing": [],
    }
    if normalised is None:
        result["missing"] = ["<origin>"]
        return result

    ox, oy, oz = normalised["x"], normalised["y"], normalised["z"]
    anchor = base_anchor(normalised)
    box = base_bounding_box(normalised)
    result["anchor"] = anchor
    result["box"] = box

    wall = _first_available(_WALL_MATERIALS, counts)
    floor = _first_available(_FLOOR_MATERIALS, counts) or wall
    roof = _first_available(_ROOF_MATERIALS, counts) or wall
    torch = _first_available(_TORCH_MATERIALS, counts)
    door = _first_available(_DOOR_MATERIALS, counts)
    table = _first_available(_TABLE_MATERIALS, counts)
    bed = _first_available(_BED_MATERIALS, counts)

    missing: List[str] = []
    if wall is None:
        missing.append("wall_block")
    if torch is None:
        missing.append("torch")
    if table is None:
        missing.append("crafting_table")

    if wall is None:
        result["missing"] = missing
        return result

    # Grid bounds (shell). Outer footprint occupies [ox-1 .. ox+INTERIOR_W] on x
    # and [oz-1 .. oz+INTERIOR_D] on z; floor at oy-1, roof at oy+INTERIOR_H.
    x_lo, x_hi = ox - 1, ox + _INTERIOR_W
    z_lo, z_hi = oz - 1, oz + _INTERIOR_D
    y_floor = oy - 1
    y_roof = oy + _INTERIOR_H

    # Reserve one interior-height wall cell on the -z face for the door, so the
    # shelter has an actual entrance instead of a sealed box (the buried bug).
    door_x = ox + _INTERIOR_W // 2
    door_z = z_lo
    door_cells = {(door_x, oy, door_z), (door_x, oy + 1, door_z)}

    blocks: List[Dict[str, Any]] = []

    def _add(x: int, y: int, z: int, item: str) -> None:
        blocks.append({"x": x, "y": y, "z": z, "item": item})

    # Emit the shell bottom-up so every cell has a solid neighbour already
    # placed to click against: floor slab first, then the walls rising off it,
    # then the roof last (which clicks onto the finished wall tops). Placing the
    # roof before the walls — the old order — left the roof cells floating in
    # air on the first bridge pass (``no-solid-face``), one of the reasons the
    # "house was not closed"; the bridge seal pass now backstops it, but the
    # right build order minimises the holes in the first place.

    # Floor (solid slab one below the interior).
    for x in range(x_lo, x_hi + 1):
        for z in range(z_lo, z_hi + 1):
            _add(x, y_floor, z, floor or wall)

    # Four walls, at the two interior heights (feet + head), skipping the door.
    for y in range(oy, oy + _INTERIOR_H):
        for x in range(x_lo, x_hi + 1):
            for z in (z_lo, z_hi):
                if (x, y, z) in door_cells:
                    continue
                _add(x, y, z, wall)
        for z in range(oz, oz + _INTERIOR_D):  # interior z, avoid corner dupes
            for x in (x_lo, x_hi):
                if (x, y, z) in door_cells:
                    continue
                _add(x, y, z, wall)

    # Roof (solid slab one above head room) — placed last, onto the wall tops.
    for x in range(x_lo, x_hi + 1):
        for z in range(z_lo, z_hi + 1):
            _add(x, y_roof, z, roof or wall)

    # Quantity check: the plan chose a material by presence, but the shell needs
    # many of it. Count required shell blocks per material and compare against
    # the carried counts; if any material is short, the build cannot complete
    # (the bridge would place 0/N), so report a quantity shortfall in ``missing``
    # and mark the plan not-ok rather than dispatching an impossible build.
    required_counts: Dict[str, int] = {}
    for b in blocks:
        item = b["item"]
        required_counts[item] = required_counts.get(item, 0) + 1
    for item, need in required_counts.items():
        try:
            have = int(counts.get(item, 0))
        except (TypeError, ValueError):
            have = 0
        if have < need:
            missing.append(f"{item}:need {need} (have {have})")

    if any(":need " in m for m in missing):
        result["blocks"] = blocks
        result["ok"] = False
        result["missing"] = missing
        return result

    result["blocks"] = blocks
    result["ok"] = True
    result["missing"] = missing

    # Door: two-tall entrance in the reserved -z wall gap.
    if door is not None:
        result["door"] = {"x": door_x, "y": oy, "z": door_z, "item": door}

    # Torch: interior centre floor so light fills the box (mobs cannot spawn).
    if torch is not None:
        result["torch"] = {
            "x": anchor["x"],
            "y": oy,
            "z": anchor["z"],
            "item": torch,
        }

    # Crafting table: an interior corner so Synth can keep building from home.
    if table is not None:
        result["crafting_table"] = {
            "x": ox,
            "y": oy,
            "z": oz,
            "item": table,
        }

    # Bed (optional — the *second* objective): interior back corner when carried.
    if bed is not None:
        result["bed"] = {
            "x": ox + _INTERIOR_W - 1,
            "y": oy,
            "z": oz + _INTERIOR_D - 1,
            "item": bed,
        }

    return result
