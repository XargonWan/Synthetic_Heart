"""The server renderer's body: the FBX reader, the Mixamo retargeting and the clip timing."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from core.avatar_render import fbx
from core.avatar_render.motion import AnimationRequest, MotionPlayer, clip_time

REAL_CLIP = Path("skins/Rei/animations/think/Thinking.fbx")
TICKS = 46186158000


# -- a minimal binary FBX writer (version 7.4, 32-bit offsets) -------------------------------


def _prop(value: Any) -> bytes:
    if isinstance(value, bool):
        return b"C" + bytes([int(value)])
    if isinstance(value, int):
        return b"L" + struct.pack("<q", value)
    if isinstance(value, float):
        return b"D" + struct.pack("<d", value)
    if isinstance(value, str):
        raw = value.encode()
        return b"S" + struct.pack("<I", len(raw)) + raw
    kind, data = value  # an array: ("l"|"f", list)
    dtype = {"l": "<i8", "f": "<f4"}[kind]
    raw = zlib.compress(np.asarray(data, dtype=dtype).tobytes())
    return kind.encode() + struct.pack("<III", len(data), 1, len(raw)) + raw


def _tree(spec: tuple[str, list[Any], list[Any]], at: int) -> bytes:
    """Build ``(name, props, [child specs])`` at absolute offset ``at``."""
    name, props, kids = spec
    body = b"".join(_prop(p) for p in props)
    pos = at + 13 + len(name) + len(body)
    children = b""
    for kid in kids:
        record = _tree(kid, pos + len(children))
        children += record
    if kids:
        children += b"\x00" * 13
    end = pos + len(children)
    return (
        struct.pack("<IIIB", end, len(props), len(body), len(name))
        + name.encode()
        + body
        + children
    )


def _p(name: str, *values: float) -> tuple[str, list[Any], list[Any]]:
    return ("P", [name, name, "", "A", *[float(v) for v in values]], [])


def _build_fbx(
    bones: list[tuple[int, str, dict[str, tuple[float, ...]], int | None]],
    curves: dict[tuple[int, str], dict[str, tuple[list[float], list[float]]]],
) -> bytes:
    """``bones``: (id, name, properties, parent id); ``curves``: (bone id, property) ->
    {"X": (seconds, values)}"""
    objects: list[Any] = []
    links: list[Any] = []
    next_id = 1000
    for bone_id, name, props, parent in bones:
        objects.append(
            (
                "Model",
                [bone_id, f"mixamorig:{name}\x00\x01Model", "LimbNode"],
                [("Properties70", [], [_p(k, *v) for k, v in props.items()])],
            )
        )
        links.append(("C", ["OO", bone_id, parent or 0], []))
    for (bone_id, prop), axes in curves.items():
        node_id = next_id
        next_id += 1
        objects.append(
            ("AnimationCurveNode", [node_id, "T\x00\x01AnimCurveNode", ""], [])
        )
        links.append(("C", ["OP", node_id, bone_id, prop], []))
        for axis, (seconds, values) in axes.items():
            curve_id = next_id
            next_id += 1
            objects.append(
                (
                    "AnimationCurve",
                    [curve_id, "\x00\x01AnimCurve", ""],
                    [
                        ("KeyTime", [("l", [round(s * TICKS) for s in seconds])], []),
                        ("KeyValueFloat", [("f", values)], []),
                    ],
                )
            )
            links.append(("C", ["OP", curve_id, node_id, f"d|{axis}"], []))
    data = fbx._MAGIC + struct.pack("<I", 7400)
    for spec in (("Objects", [], objects), ("Connections", [], links)):
        data += _tree(spec, len(data))
    return data + b"\x00" * 13


def _angle(q: np.ndarray) -> float:
    return float(2 * np.degrees(np.arccos(min(1.0, abs(float(q[3]))))))


# -- the reader -------------------------------------------------------------------------------


def test_a_rotation_curve_becomes_a_normalized_bone_rotation() -> None:
    data = _build_fbx(
        [
            (1, "Hips", {"Lcl Translation": (0, 100, 0)}, None),
            (2, "Head", {"Lcl Rotation": (0, 0, 0)}, 1),
        ],
        {(2, "Lcl Rotation"): {"Y": ([0.0, 1.0], [0.0, 90.0])}},
    )
    clip = fbx.load_mixamo_clip_from_bytes(data)

    assert clip.duration == pytest.approx(1.0)
    assert clip.hips_rest_height == pytest.approx(100.0)
    assert clip.hips_position is None
    at_rest = clip.sample_rotation("head", 0.0)
    turned = clip.sample_rotation("head", 1.0)
    assert at_rest is not None and turned is not None
    assert _angle(at_rest) == pytest.approx(0.0, abs=0.01)
    assert _angle(turned) == pytest.approx(90.0, abs=0.01)
    assert abs(turned[1]) == pytest.approx(np.sin(np.radians(45)), abs=1e-3)  # about Y
    half = clip.sample_rotation("head", 0.5)
    assert half is not None and _angle(half) == pytest.approx(45.0, abs=0.5)
    assert clip.sample_rotation("neck", 0.0) is None  # a bone the clip does not move


def test_a_bone_that_stays_at_its_rest_rotation_does_not_move_whatever_its_pre_rotation() -> (
    None
):
    """The retargeting is relative to the rest pose: Pre * R * Post^-1 against the rest world."""
    props = {
        "Lcl Rotation": (10.0, 20.0, 30.0),
        "PreRotation": (0.0, 0.0, 90.0),
    }
    data = _build_fbx(
        [
            (1, "Hips", {"PreRotation": (-90.0, 0.0, 0.0)}, None),
            (2, "Spine", props, 1),
        ],
        {
            (2, "Lcl Rotation"): {
                "X": ([0.0, 1.0], [10.0, 10.0]),
                "Y": ([0.0, 1.0], [20.0, 20.0]),
                "Z": ([0.0, 1.0], [30.0, 30.0]),
            }
        },
    )
    clip = fbx.load_mixamo_clip_from_bytes(data)
    spine = clip.sample_rotation("spine", 0.5)
    assert spine is not None and _angle(spine) == pytest.approx(0.0, abs=0.01)


def test_the_fbx_rotation_order_is_the_first_axis_applied_first() -> None:
    angles = np.array([[90.0, 0.0, 0.0]])
    # X then Y then Z is Rz @ Ry @ Rx; a pure X turn is the same in any order
    assert fbx.euler_matrices(angles, "XYZ")[0] @ [0, 1, 0] == pytest.approx(
        [0, 0, 1], abs=1e-9
    )
    both = np.array([[90.0, 90.0, 0.0]])
    xyz = fbx.euler_matrices(both, "XYZ")[0]
    zyx = fbx.euler_matrices(both, "ZYX")[0]
    assert not np.allclose(xyz, zyx)
    assert xyz @ [0, 1, 0] == pytest.approx(
        [1, 0, 0], abs=1e-9
    )  # X: y->z, then Y: z->x


def test_the_hips_translation_is_read_and_scaled_to_the_model() -> None:
    data = _build_fbx(
        [
            (1, "Hips", {"Lcl Translation": (0, 100, 0)}, None),
            (2, "Spine", {}, 1),
        ],
        {
            (1, "Lcl Translation"): {"Y": ([0.0, 1.0], [100.0, 80.0])},
            (2, "Lcl Rotation"): {"X": ([0.0, 1.0], [0.0, 10.0])},
        },
    )
    clip = fbx.load_mixamo_clip_from_bytes(data)
    assert clip.hips_position is not None
    low = clip.sample_hips_position(1.0)
    assert low is not None and low[1] == pytest.approx(80.0)

    player = MotionPlayer({"hips", "spine"}, hips_height=1.0, faces_minus_z=False)
    path = Path("clip.fbx")
    player.set_clip(path, clip)
    _rotations, hips = player.sample(AnimationRequest(path, None, 0.0), now=0.99)
    assert hips is not None and hips[1] == pytest.approx(
        0.8, abs=0.01
    )  # 80 cm of a 100 cm clip


def test_garbage_and_clips_without_a_mixamo_rig_are_refused() -> None:
    with pytest.raises(fbx.FbxError):
        fbx.load_mixamo_clip_from_bytes(b"not an fbx at all")
    with pytest.raises(fbx.FbxError):
        fbx.load_mixamo_clip_from_bytes(
            _build_fbx([(1, "Hips", {}, None)], {(1, "Lcl Rotation"): {}})
        )


@pytest.mark.skipif(not REAL_CLIP.is_file(), reason="the Rei skin is not here")
def test_the_real_thinking_clip_reads_like_the_webui_reads_it() -> None:
    clip = fbx.load_mixamo_clip(REAL_CLIP)
    assert 4.0 < clip.duration < 4.5
    assert {"hips", "head", "neck", "leftUpperArm", "rightHand"} <= set(clip.rotations)
    # the arms come down from the T-pose by about 80 degrees, the head tilts some
    arm = clip.sample_rotation("leftUpperArm", 0.0)
    assert arm is not None and 60.0 < _angle(arm) < 100.0


# -- the timing ------------------------------------------------------------------------------


def _timed_clip(duration: float = 4.0) -> fbx.MixamoClip:
    return fbx.MixamoClip(duration, {}, None, 0.0)


def test_a_structured_clip_plays_its_intro_once_then_loops() -> None:
    desc = {
        "fps": 30,
        "intro": {"start_frame": 0, "end_frame": 49},
        "loop": {"start_frame": 49, "end_frame": 70},
        "outro": {"start_frame": 70, "end_frame": 126},
    }
    clip = _timed_clip(4.2)
    assert clip_time(clip, desc, 0.5, "think") == pytest.approx(0.5)
    # intro lasts 50 frames; the loop covers frames 49..70 (22 frames) from then on
    assert clip_time(clip, desc, 50 / 30, "think") == pytest.approx(49 / 30)
    assert clip_time(clip, desc, 72 / 30, "think") == pytest.approx(49 / 30)  # wrapped
    for elapsed in (3.0, 10.0, 99.0):
        t = clip_time(clip, desc, elapsed, "think")
        assert 49 / 30 - 1e-6 <= t <= 70 / 30 + 1e-6


def test_think_without_a_descriptor_splits_the_clip_like_the_webui() -> None:
    clip = _timed_clip(4.0)  # 120 frames: intro 24, loop 24..96
    assert clip_time(clip, None, 0.4, "think") == pytest.approx(0.4)
    assert 24 / 30 <= clip_time(clip, None, 9.0, "think") <= 96 / 30


def test_other_clips_repeat_whole_or_hold_their_last_pose_when_play_once() -> None:
    clip = _timed_clip(2.0)  # 60 frames
    assert clip_time(clip, None, 2.5, "idle") == pytest.approx(0.5)
    assert clip_time(clip, {"play_once": True}, 9.0, "idle") == pytest.approx(59 / 30)


def test_intro_and_outro_without_a_loop_hold_the_last_intro_pose() -> None:
    desc = {
        "fps": 30,
        "intro": {"start_frame": 0, "end_frame": 29},
        "outro": {"start_frame": 30, "end_frame": 59},
    }
    assert clip_time(_timed_clip(2.0), desc, 50.0, "give") == pytest.approx(29 / 30)


# -- the player ------------------------------------------------------------------------------


def test_a_new_clip_fades_in_from_the_previous_pose_and_a_vrm0_model_is_turned() -> (
    None
):
    data = _build_fbx(
        [(1, "Hips", {}, None), (2, "Head", {}, 1)],
        {(2, "Lcl Rotation"): {"Y": ([0.0, 1.0], [90.0, 90.0])}},
    )
    clip = fbx.load_mixamo_clip_from_bytes(data)
    path = Path("a.fbx")
    player = MotionPlayer({"head"}, 1.0, faces_minus_z=False)
    player.set_clip(path, clip)

    first, _ = player.sample(AnimationRequest(path, None, 100.0), now=100.0)
    assert first["head"] @ np.array([0, 0, 1.0]) == pytest.approx([1, 0, 0], abs=1e-5)
    # the same clip started again: from the pose it had, so no jump
    again, _ = player.sample(AnimationRequest(path, None, 105.0), now=105.0)
    assert again["head"] == pytest.approx(first["head"], abs=1e-5)

    flipped = MotionPlayer({"head"}, 1.0, faces_minus_z=True)
    flipped.set_clip(path, clip)
    turned, _ = flipped.sample(AnimationRequest(path, None, 0.0), now=0.0)
    # VRM 0.x: x and z of the quaternion change sign; a turn about Y stays a turn about Y
    assert turned["head"] == pytest.approx(first["head"], abs=1e-5)


def test_a_lower_body_mask_leaves_the_legs_and_hips_alone() -> None:
    data = _build_fbx(
        [
            (1, "Hips", {"Lcl Translation": (0, 100, 0)}, None),
            (2, "Spine", {}, 1),
            (3, "LeftUpLeg", {}, 1),
        ],
        {
            (1, "Lcl Translation"): {"Y": ([0.0, 1.0], [100.0, 50.0])},
            (2, "Lcl Rotation"): {"X": ([0.0, 1.0], [0.0, 20.0])},
            (3, "Lcl Rotation"): {"X": ([0.0, 1.0], [0.0, 20.0])},
        },
    )
    clip = fbx.load_mixamo_clip_from_bytes(data)
    path = Path("m.fbx")
    player = MotionPlayer({"hips", "spine", "leftUpperLeg"}, 1.0, False)
    player.set_clip(path, clip)
    request = AnimationRequest(path, {"mask": "upper_body"}, 0.0)
    rotations, hips = player.sample(request, now=1.0)
    assert "spine" in rotations and "leftUpperLeg" not in rotations
    assert hips is None


def test_nothing_is_played_before_the_clip_is_read() -> None:
    player = MotionPlayer({"head"}, 1.0, False)
    request = AnimationRequest(Path("unread.fbx"), None, 0.0)
    assert player.sample(request, now=1.0) == ({}, None)
    assert player.clip(Path("unread.fbx")) == (False, None)
    assert player.sample(None) == ({}, None)
