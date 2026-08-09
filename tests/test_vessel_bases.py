"""Tests for the Rift Vessel base (home) store (``plugins/rift_vessel/vessel_bases.py``).

A **base** is a place Synth claimed in a world as home — where it builds,
stores things, shelters and sleeps (AGENTS.md §5c: the store is Rift Vessel
**core**, the concrete building is the adapter's job). These tests cover the
**pure**, DB-free helpers that sanitise the structural inputs (coordinates and
scope) and normalise a DB row — no DB, no bridge, no LLM. They assert the store
is coordinate-driven and never inspects any free text for keywords.
"""

from __future__ import annotations

import json

from plugins.rift_vessel.vessel_bases import (
    SCOPE_NONE,
    _clip,
    _coerce_box,
    _coerce_point,
    _coerce_scope,
    _row_to_base,
)


# ---------------------------------------------------------------------------
# _clip — length-capped trimmed string
# ---------------------------------------------------------------------------


def test_clip_trims_and_caps() -> None:
    assert _clip("  hi  ", 10) == "hi"
    assert _clip(None, 10) == ""
    assert _clip("x" * 50, 8) == "x" * 8


# ---------------------------------------------------------------------------
# _coerce_scope — single structural scope level
# ---------------------------------------------------------------------------


def test_coerce_scope_lowercases_and_defaults() -> None:
    assert _coerce_scope("Minecraft") == "minecraft"
    assert _coerce_scope("  ") == SCOPE_NONE
    assert _coerce_scope(None) == SCOPE_NONE


def test_coerce_scope_caps_length() -> None:
    long = "a" * 200
    assert len(_coerce_scope(long)) == 64


# ---------------------------------------------------------------------------
# _coerce_point — anchor point requires all three axes
# ---------------------------------------------------------------------------


def test_coerce_point_requires_all_axes() -> None:
    assert _coerce_point({"x": 1, "y": 2, "z": 3}) == {"x": 1.0, "y": 2.0, "z": 3.0}
    # Missing an axis => not a usable point (the retreat reflex needs y too).
    assert _coerce_point({"x": 1, "z": 3}) is None
    assert _coerce_point({}) is None
    assert _coerce_point("nope") is None


def test_coerce_point_rejects_non_numeric() -> None:
    assert _coerce_point({"x": "a", "y": 2, "z": 3}) is None


# ---------------------------------------------------------------------------
# _coerce_box — bounding box requires all six corners
# ---------------------------------------------------------------------------


def test_coerce_box_requires_six_corners() -> None:
    box = {"x1": 0, "y1": 0, "z1": 0, "x2": 4, "y2": 3, "z2": 5}
    out = _coerce_box(box)
    assert out == {k: float(v) for k, v in box.items()}
    # A partial box is rejected.
    assert _coerce_box({"x1": 0, "y1": 0, "z1": 0}) is None
    assert _coerce_box("nope") is None


# ---------------------------------------------------------------------------
# _row_to_base — normalise a DB row (tuple or dict) with JSON-encoded coords
# ---------------------------------------------------------------------------


def test_row_to_base_parses_json_anchor_and_box() -> None:
    row = {
        "id": 7,
        "session_id": "s1",
        "scope": "vessel",
        "game": "minecraft",
        "world": "none",
        "name": "Lakeside",
        "kind": "home",
        "anchor": json.dumps({"x": 10, "y": 64, "z": -3}),
        "box": json.dumps({"x1": 8, "y1": 63, "z1": -5, "x2": 12, "y2": 66, "z2": -1}),
        "note": "cozy",
        "status": "active",
    }
    base = _row_to_base(row)
    assert base is not None
    assert base["id"] == 7
    assert base["anchor"] == {"x": 10.0, "y": 64.0, "z": -3.0}
    assert base["box"]["x2"] == 12.0
    assert base["kind"] == "home"


def test_row_to_base_accepts_tuple_row() -> None:
    # Column order matches _BASE_COLS.
    row = (
        3,
        "s1",
        "vessel",
        "minecraft",
        "none",
        "outpost",
        "outpost",
        json.dumps({"x": 1, "y": 2, "z": 3}),
        None,
        None,
        "active",
    )
    base = _row_to_base(row)
    assert base is not None
    assert base["name"] == "outpost"
    assert base["anchor"] == {"x": 1.0, "y": 2.0, "z": 3.0}
    assert base["box"] is None


def test_row_to_base_none_and_defaults() -> None:
    assert _row_to_base(None) is None
    minimal = _row_to_base({"id": 1, "name": "x"})
    assert minimal is not None
    assert minimal["scope"] == SCOPE_NONE
    assert minimal["kind"] == "home"
    assert minimal["anchor"] is None
    assert minimal["box"] is None
