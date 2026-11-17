#!/usr/bin/env python3
"""Make the ``give`` clip: both arms held out in front, hands cupped palm up, as when handing something to someone.

A maintainer tool, not part of the runtime. It needs Blender's FBX reader/writer and its maths module, which the project
does not depend on, so it is run with ``uv`` pulling ``bpy`` in for the run only (about 300 MB, once)::

    uv run --python 3.11 --with bpy python tools/make_give_clip.py \\
        --base "skins/Rei/animations/idle/Idle.fbx" --out skins/Rei/animations/give/Give.fbx

WHY NOT SIMPLY EXPORT FROM BLENDER. The mod plays every clip on the skeleton of the FIRST clip it loaded, by track path
(``Skeleton3D:mixamorig_RightArm``), and a track holds the bone's LOCAL rotation in that skeleton's own bone frames. A clip
exported by Blender has other bone frames (the arm differs by 78 degrees) and another path (``Armature/Skeleton3D:...``):
played on the shared skeleton it does nothing. So this tool does not export. It takes a Mixamo FBX of the skin, keeps
everything in it (skeleton, bone frames, names, paths) and replaces only the animation curves.

HOW THE POSE IS MADE. The bones are AIMED, not given guessed rotation axes: each arm bone is turned so that it points where
it should in the room (forward, down, in), the roll of the forearm is chosen by where the thumb ends up (palm up = thumb
outward) and the fingers curl toward the palm. All that is done in model space, then turned into the local rotation each bone
needs (``PreRotation`` and the local Euler rotation of FBX), frame by frame.

The clip is a layer over the upper body (``"mask": "upper_body"``, like ``gaming``): the arms come up over half a second
(``intro``), then the pose is held and repeats (``loop``; its two ends are the same pose, so there is nothing to blend).
There is no ``outro``: a layer is taken off by cross-fading back to the base clip. The spine, neck and head stay upright and
the mod's gaze drives the head.

Writes ``Give.fbx`` and its ``Give.fbx.json`` descriptor next to it, and prints where the hands end up (a check).
"""

from __future__ import annotations

import argparse
import array
import json
import math
import sys
from pathlib import Path
from typing import Any

FPS = 30
INTRO_END = 15  # half a second to bring the arms up
LOOP_END = 75  # then held for two seconds, the same pose at both ends
BREATH_FRAME = 45  # the middle of the hold: arms a little lower and the back a little curved, as when breathing
TICKS_PER_SECOND = 46186158000  # FBX time
SIDES = ("Right", "Left")
MODEL_PREFIX = "mixamorig:"


def _modules() -> tuple[Any, Any, Any, Any]:
    try:
        import bpy  # noqa: F401  (importing it puts Blender's bundled add-ons on the path)
        from io_scene_fbx import encode_bin, parse_fbx
        from mathutils import Euler, Quaternion, Vector
    except ImportError:
        sys.exit(
            "bpy is not installed. Run:  uv run --python 3.11 --with bpy python tools/make_give_clip.py --help"
        )
    return parse_fbx, encode_bin, (Euler, Quaternion, Vector), None


# -- reading the skeleton out of the FBX ---------------------------------------------------------------------------------


class Rig:
    """The skeleton as the FBX states it: parents, ``PreRotation``, rest local rotation and offset; and from them the rest
    pose in model space. Units are the file's (centimetres)."""

    def __init__(self, root: Any, m: tuple[Any, Any, Any]) -> None:
        euler_t, quat_t, vec_t = m
        self.Quaternion, self.Vector = quat_t, vec_t
        objects = next(e for e in root.elems if e.id == b"Objects")
        connections = next(e for e in root.elems if e.id == b"Connections")
        self.id_of: dict[str, int] = {}
        self.name_of: dict[int, str] = {}
        self.pre: dict[str, Any] = {}
        self.rest_lcl: dict[str, Any] = {}
        self.offset: dict[str, Any] = {}
        for e in objects.elems:
            if e.id != b"Model":
                continue
            name = e.props[1].split(b"\x00")[0].decode()
            self.id_of[name] = e.props[0]
            self.name_of[e.props[0]] = name
            values: dict[str, tuple[float, ...]] = {
                "PreRotation": (0.0, 0.0, 0.0),
                "Lcl Rotation": (0.0, 0.0, 0.0),
                "Lcl Translation": (0.0, 0.0, 0.0),
            }
            props = next((c for c in e.elems if c.id == b"Properties70"), None)
            for p in props.elems if props else []:
                key = p.props[0].decode()
                if key in values:
                    values[key] = tuple(float(v) for v in p.props[4:7])
                elif key == "RotationOrder" and int(p.props[4]) != 0:
                    sys.exit(f"{name}: a rotation order other than XYZ is not handled")
                elif key in ("Lcl Scaling",) and tuple(p.props[4:7]) != (1.0, 1.0, 1.0):
                    sys.exit(f"{name}: a scale other than 1 is not handled")
                elif key in (
                    "PostRotation",
                    "RotationOffset",
                    "RotationPivot",
                    "ScalingOffset",
                    "ScalingPivot",
                ) and any(float(v) != 0.0 for v in p.props[4:7]):
                    sys.exit(f"{name}: {key} is not handled")
            self.pre[name] = euler_t(
                tuple(math.radians(v) for v in values["PreRotation"]), "XYZ"
            ).to_quaternion()
            self.rest_lcl[name] = euler_t(
                tuple(math.radians(v) for v in values["Lcl Rotation"]), "XYZ"
            ).to_quaternion()
            self.offset[name] = vec_t(values["Lcl Translation"])
        self.parent: dict[str, str | None] = {n: None for n in self.id_of}
        for c in connections.elems:
            if (
                c.props[0] == b"OO"
                and c.props[1] in self.name_of
                and c.props[2] in self.name_of
            ):
                self.parent[self.name_of[c.props[1]]] = self.name_of[c.props[2]]
        self.order = self._parents_first()
        self.rest_rot: dict[str, Any] = {}
        self.rest_pos: dict[str, Any] = {}
        for n in self.order:
            p = self.parent[n]
            full = self.pre[n] @ self.rest_lcl[n]
            self.rest_rot[n] = full if p is None else self.rest_rot[p] @ full
            self.rest_pos[n] = (
                self.offset[n]
                if p is None
                else self.rest_pos[p] + self.rest_rot[p] @ self.offset[n]
            )

    def _parents_first(self) -> list[str]:
        done: list[str] = []
        pending = list(self.id_of)
        while pending:
            for n in list(pending):
                p = self.parent[n]
                if p is None or p in done:
                    done.append(n)
                    pending.remove(n)
        return done


class Pose:
    """A pose as MODEL-SPACE rotation deltas from the rest pose, per bone; a bone with none inherits its parent's."""

    def __init__(self, rig: Rig) -> None:
        self.rig = rig
        self.D: dict[str, Any] = {}

    def delta(self, bone: str) -> Any:
        if bone in self.D:
            return self.D[bone]
        parent = self.rig.parent[bone]
        return self.delta(parent) if parent else self.rig.Quaternion()

    def pos(self, bone: str) -> Any:
        parent = self.rig.parent[bone]
        if parent is None:
            return self.rig.rest_pos[bone].copy()
        return self.pos(parent) + self.delta(parent) @ (
            self.rig.rest_pos[bone] - self.rig.rest_pos[parent]
        )

    def direction(self, bone: str, child: str) -> Any:
        return (
            self.delta(bone) @ (self.rig.rest_pos[child] - self.rig.rest_pos[bone])
        ).normalized()

    def aim(self, bone: str, child: str, want: Any) -> None:
        """Turn ``bone`` (and, through the inheritance, what hangs from it) so that it points at ``want`` (a unit vector)."""
        self.D[bone] = self.direction(bone, child).rotation_difference(
            want
        ) @ self.delta(bone)

    def roll(self, bone: str, child: str, radians: float) -> None:
        """Twist ``bone`` about its own length."""
        self.D[bone] = self.rig.Quaternion(
            self.direction(bone, child), radians
        ) @ self.delta(bone)

    def about(self, bone: str, axis: Any, radians: float) -> None:
        self.D[bone] = self.rig.Quaternion(axis.normalized(), radians) @ self.delta(
            bone
        )

    def local(self, bone: str) -> Any:
        """The local rotation (the FBX ``Lcl Rotation``) this pose needs of ``bone``."""
        rig = self.rig
        model = self.delta(bone) @ rig.rest_rot[bone]
        parent = rig.parent[bone]
        parent_model = (
            (self.delta(parent) @ rig.rest_rot[parent]) if parent else rig.Quaternion()
        )
        return rig.pre[bone].inverted() @ (parent_model.inverted() @ model)


# -- the two poses ---------------------------------------------------------------------------------------------------------


class Room:
    """The room's axes in the file's model space, read from the rest pose: up, the character's right, and so forward."""

    def __init__(self, rig: Rig) -> None:
        pose = Pose(rig)
        self.up = (
            pose.pos(MODEL_PREFIX + "Head") - pose.pos(MODEL_PREFIX + "Hips")
        ).normalized()
        right = pose.pos(MODEL_PREFIX + "RightShoulder") - pose.pos(
            MODEL_PREFIX + "LeftShoulder"
        )
        self.right = (right - self.up * right.dot(self.up)).normalized()
        self.forward = self.up.cross(self.right).normalized()
        self.outward = {"Right": self.right, "Left": -self.right}

    def unit(self, *terms: tuple[Any, float]) -> Any:
        total = terms[0][0] * 0.0
        for vec, weight in terms:
            total = total + vec * weight
        return total.normalized()


def _bone(side: str, part: str) -> str:
    return f"{MODEL_PREFIX}{side}{part}"


def arms_down(pose: Pose, room: Room, side: str) -> None:
    out = room.outward[side]
    pose.aim(
        _bone(side, "Shoulder"),
        _bone(side, "Arm"),
        room.unit((out, 1.0), (room.up, -0.12)),
    )
    pose.aim(
        _bone(side, "Arm"),
        _bone(side, "ForeArm"),
        room.unit((room.up, -1.0), (out, 0.12)),
    )
    pose.aim(
        _bone(side, "ForeArm"),
        _bone(side, "Hand"),
        room.unit((room.up, -1.0), (out, 0.06), (room.forward, 0.08)),
    )
    pose.aim(
        _bone(side, "Hand"),
        _bone(side, "HandMiddle1"),
        room.unit((room.up, -1.0), (out, 0.04), (room.forward, 0.1)),
    )


def arms_out(pose: Pose, room: Room, side: str, lowered: float = 0.0) -> float:
    """Upper arm hanging forward and down, forearm level and a little inward, hands cupped palm up.

    Returns how far outward the thumb is of the pinky (positive = the palm faces up), for the caller to check."""
    out, up, fwd = room.outward[side], room.up, room.forward
    pose.aim(
        _bone(side, "Shoulder"), _bone(side, "Arm"), room.unit((out, 1.0), (fwd, 0.12))
    )
    pose.aim(
        _bone(side, "Arm"),
        _bone(side, "ForeArm"),
        room.unit((up, -0.85 - lowered), (fwd, 0.5), (out, 0.1)),
    )
    pose.aim(
        _bone(side, "ForeArm"),
        _bone(side, "Hand"),
        room.unit((fwd, 0.95), (up, 0.06 - lowered), (out, -0.22)),
    )

    def thumb_outwardness() -> float:
        return float(
            (
                pose.pos(_bone(side, "HandThumb1"))
                - pose.pos(_bone(side, "HandPinky1"))
            ).dot(out)
        )

    # Palm up: the forearm rolls one way or the other, and the thumb says which (it ends up on the outer side).
    best_sign, best_score = 1.0, -math.inf
    for sign in (1.0, -1.0):
        saved = dict(pose.D)
        pose.roll(_bone(side, "ForeArm"), _bone(side, "Hand"), sign * math.radians(70))
        score = thumb_outwardness()
        pose.D = saved
        if score > best_score:
            best_sign, best_score = sign, score
    pose.roll(_bone(side, "ForeArm"), _bone(side, "Hand"), best_sign * math.radians(70))
    pose.aim(
        _bone(side, "Hand"),
        _bone(side, "HandMiddle1"),
        room.unit((fwd, 0.95), (up, 0.16 - lowered), (out, -0.2)),
    )
    pose.roll(
        _bone(side, "Hand"), _bone(side, "HandMiddle1"), best_sign * math.radians(20)
    )
    # Fingers curl toward the palm: about (finger x palm normal), whatever the bone's own axes are.
    across = pose.pos(_bone(side, "HandIndex1")) - pose.pos(_bone(side, "HandPinky1"))
    along = pose.pos(_bone(side, "HandMiddle1")) - pose.pos(_bone(side, "Hand"))
    palm = across.cross(along).normalized()
    if palm.dot(up) < 0:
        palm = -palm
    for finger, angles in (
        ("Index", (22, 32, 22)),
        ("Middle", (22, 32, 22)),
        ("Ring", (22, 32, 22)),
        ("Pinky", (22, 32, 22)),
        ("Thumb", (8, 12, 8)),
    ):
        for joint, degrees in zip((1, 2, 3), angles):
            bone, child = (
                _bone(side, f"Hand{finger}{joint}"),
                _bone(side, f"Hand{finger}{joint + 1}"),
            )
            pose.about(
                bone, pose.direction(bone, child).cross(palm), math.radians(degrees)
            )
    return thumb_outwardness()


def build(rig: Rig, room: Room, kind: str) -> tuple[Pose, dict[str, float]]:
    pose = Pose(rig)
    checks: dict[str, float] = {}
    if kind == "breath":
        for bone in ("Spine1", "Spine2"):
            pose.about(MODEL_PREFIX + bone, room.right, math.radians(0.75))
    for side in SIDES:
        if kind == "down":
            arms_down(pose, room, side)
        else:
            checks[side] = arms_out(
                pose, room, side, lowered=0.04 if kind == "breath" else 0.0
            )
    return pose, checks


def ease(t: float) -> float:
    return 0.5 - 0.5 * math.cos(math.pi * min(max(t, 0.0), 1.0))


def sample_frames(rig: Rig, room: Room) -> list[dict[str, Any]]:
    """Every frame 0..LOOP_END: ``{bone: local Quaternion}``, blended from the key poses (the hold starts and ends the same)."""
    keys = {name: build(rig, room, name)[0] for name in ("down", "out", "breath")}
    local = {name: {b: p.local(b) for b in rig.order} for name, p in keys.items()}
    frames: list[dict[str, Any]] = []
    for f in range(LOOP_END + 1):
        if f <= INTRO_END:
            a, b, t = "down", "out", ease(f / INTRO_END)
        elif f <= BREATH_FRAME:
            a, b, t = (
                "out",
                "breath",
                ease((f - INTRO_END) / (BREATH_FRAME - INTRO_END)),
            )
        else:
            a, b, t = (
                "breath",
                "out",
                ease((f - BREATH_FRAME) / (LOOP_END - BREATH_FRAME)),
            )
        frames.append(
            {bone: local[a][bone].slerp(local[b][bone], t) for bone in rig.order}
        )
    return frames


# -- writing it into the FBX -------------------------------------------------------------------------------------------------


def patch(
    root: Any, rig: Rig, frames: list[dict[str, Any]], m: tuple[Any, Any, Any]
) -> None:
    """Replace every animation curve of ``root`` (in place): rotations with the new ones, anything else held at its first value."""
    objects = next(e for e in root.elems if e.id == b"Objects")
    connections = next(e for e in root.elems if e.id == b"Connections")
    by_id = {
        e.props[0]: e
        for e in objects.elems
        if e.id in (b"AnimationCurve", b"AnimationCurveNode")
    }
    node_of: dict[int, tuple[str, str]] = {}  # curve node id -> (bone, property)
    curves_of: dict[int, dict[str, int]] = {}  # curve node id -> {"d|X": curve id, ...}
    for c in connections.elems:
        if c.props[0] != b"OP":
            continue
        child, parent = c.props[1], c.props[2]
        prop = c.props[3].decode()
        if (
            child in by_id
            and by_id[child].id == b"AnimationCurveNode"
            and parent in rig.name_of
        ):
            node_of[child] = (rig.name_of[parent], prop)
        elif child in by_id and by_id[child].id == b"AnimationCurve":
            curves_of.setdefault(parent, {})[prop] = child
    tick_list = [round(i * TICKS_PER_SECOND / FPS) for i in range(len(frames))]
    n = len(frames)
    previous: dict[str, Any] = {}
    series: dict[str, list[tuple[float, float, float]]] = {}
    for bone in rig.order:
        previous[bone] = rig.rest_lcl[bone].to_euler("XYZ")
        rows = []
        for frame in frames:
            e = frame[bone].to_euler("XYZ", previous[bone])
            previous[bone] = e
            rows.append(tuple(math.degrees(v) for v in e))
        series[bone] = rows
    replaced = 0
    for node_id, (bone, prop) in node_of.items():
        for axis, curve_id in curves_of.get(node_id, {}).items():
            curve = by_id[curve_id]
            fields = {sub.id: sub for sub in curve.elems}
            old = fields[b"KeyValueFloat"].props[0]
            # (the arrays keep the typecodes the reader gave them: the writer wants exactly those)
            times = array.array(fields[b"KeyTime"].props[0].typecode, tick_list)
            if prop == "Lcl Rotation":
                values = array.array(
                    old.typecode,
                    [series[bone][i]["XYZ".index(axis[-1])] for i in range(n)],
                )
            else:
                values = array.array(
                    old.typecode, [old[0]] * n
                )  # a translation or scale: held at where the base clip starts
            fields[b"KeyTime"].props[0] = times
            fields[b"KeyValueFloat"].props[0] = values
            fields[b"KeyAttrRefCount"].props[0] = array.array(
                fields[b"KeyAttrRefCount"].props[0].typecode, [n]
            )
            replaced += 1
    if replaced == 0:
        sys.exit("no animation curve was connected to a bone: is that a Mixamo FBX?")
    stop = tick_list[-1]
    for e in objects.elems:
        if e.id == b"AnimationStack":
            for p in next(c for c in e.elems if c.id == b"Properties70").elems:
                if p.props[0] in (b"LocalStop", b"ReferenceStop"):
                    p.props[4] = stop
    for e in root.elems:
        if e.id == b"Takes":
            for take in e.elems:
                for sub in take.elems:
                    if sub.id in (b"LocalTime", b"ReferenceTime"):
                        sub.props[1] = stop
    print(f"replaced {replaced} animation curves with {n} keys each ({n / FPS:.2f} s)")


def to_encoder(src: Any, enc: Any) -> Any:
    """The parsed tree rebuilt with the writer's element class (the reader's is read-only data)."""
    out = enc.FBXElem(src.id)
    for value, kind in zip(src.props, src.props_type):
        code = chr(kind)
        add = {
            "Y": out.add_int16,
            "C": out.add_char,
            "Z": out.add_int8,
            "I": out.add_int32,
            "L": out.add_int64,
            "F": out.add_float32,
            "D": out.add_float64,
            "R": out.add_bytes,
            "S": out.add_string,
            "B": out.add_bool,
            "i": out.add_int32_array,
            "l": out.add_int64_array,
            "f": out.add_float32_array,
            "d": out.add_float64_array,
            "b": out.add_bool_array,
            "c": out.add_byte_array,
        }.get(code)
        if add is None:
            sys.exit(f"unknown FBX property type {code!r} in {src.id!r}")
        add(value)
    out.elems = [to_encoder(child, enc) for child in src.elems]
    return out


def report(rig: Rig, room: Room, frames: list[dict[str, Any]], at: int) -> None:
    """Where the hands are at frame ``at``, in metres from the hips: ahead, up, and how far apart."""
    pose = Pose(rig)
    for bone in rig.order:
        pose.D[bone] = rig.Quaternion()
    # Rebuild the model-space deltas of that frame from its local rotations.
    model: dict[str, Any] = {}
    for bone in rig.order:
        parent = rig.parent[bone]
        full = rig.pre[bone] @ frames[at][bone]
        model[bone] = full if parent is None else model[parent] @ full
        pose.D[bone] = model[bone] @ rig.rest_rot[bone].inverted()
    hips = pose.pos(MODEL_PREFIX + "Hips")
    chest = pose.pos(MODEL_PREFIX + "Spine2")
    spots = {side: pose.pos(_bone(side, "Hand")) for side in SIDES}
    for side, spot in spots.items():
        d = (spot - hips) / 100.0
        print(
            f"[frame {at}] {side} hand: {d.dot(room.forward):+.2f} m ahead of the hips, {d.dot(room.up):+.2f} m up "
            f"(chest is {((chest - hips) / 100.0).dot(room.up):+.2f}), {d.dot(room.right):+.2f} m to the right"
        )
    print(
        f"[frame {at}] hands {(spots['Right'] - spots['Left']).length / 100.0:.2f} m apart"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--base",
        required=True,
        help="a Mixamo FBX of the skin: its skeleton and paths are kept",
    )
    parser.add_argument("--out", required=True, help="where to write Give.fbx")
    args = parser.parse_args()
    parse_fbx, encode_bin, maths, _ = _modules()
    root, version = parse_fbx.parse(str(Path(args.base).resolve()))
    rig = Rig(root, (maths[0], maths[1], maths[2]))
    missing = [
        b
        for b in (
            MODEL_PREFIX + "Hips",
            MODEL_PREFIX + "Head",
            MODEL_PREFIX + "RightHandIndex4",
        )
        if b not in rig.id_of
    ]
    if missing:
        sys.exit(f"{args.base} is not a Mixamo skeleton (no {missing})")
    room = Room(rig)
    for side in SIDES:
        _, checks = build(rig, room, "out")
        if checks[side] <= 0:
            sys.exit(
                f"{side} palm does not face up (thumb {checks[side]:+.2f}): the roll was chosen wrongly"
            )
    frames = sample_frames(rig, room)
    report(rig, room, frames, INTRO_END)
    patch(root, rig, frames, (maths[0], maths[1], maths[2]))
    out = Path(args.out).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    encode_bin.write(str(out), to_encoder(root, encode_bin), version)
    descriptor = {
        "fps": FPS,
        "mask": "upper_body",
        "intro": {"start_frame": 0, "end_frame": INTRO_END},
        "loop": {"start_frame": INTRO_END, "end_frame": LOOP_END},
    }
    Path(str(out) + ".json").write_text(
        json.dumps(descriptor, indent=2) + "\n", encoding="utf-8"
    )
    print(f"wrote {out} and its descriptor")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
