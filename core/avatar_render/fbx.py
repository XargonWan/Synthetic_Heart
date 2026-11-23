"""A reader for the Mixamo animations (binary FBX) the Karada state plays, for the server renderer.

The WebUI loads a clip with three.js' ``FBXLoader`` and retargets it with
``res/synth_webui/js/loadMixamoAnimation.js``; this module does the same in Python (numpy only):

* :func:`parse_fbx` reads the binary FBX node tree (only the arrays the animation needs are
  decoded: the meshes stay compressed).
* :func:`load_mixamo_clip` evaluates the rotation curves of the ``mixamorig`` bones
  (``PreRotation * Euler * PostRotation^-1``, as three.js does) and retargets them to the VRM
  normalized humanoid: ``parent rest world * rotation * rest world^-1``.

The result (:class:`MixamoClip`) does not depend on the VRM model. Putting it on a model's raw
bones is :mod:`core.avatar_render.motion`'s job.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

_MAGIC = b"Kaydara FBX Binary  \x00\x1a\x00"
_TICKS_PER_SECOND = 46186158000.0
#: Array properties worth decoding. Everything else (vertices, indices, ...) is skipped.
_WANTED_ARRAYS = frozenset({"KeyTime", "KeyValueFloat"})
#: The FBX ``RotationOrder`` enum: the order the axes are applied in (the first one first).
_ROTATION_ORDERS = ("XYZ", "XZY", "YZX", "YXZ", "ZXY", "ZYX")

_SIDES = (("Left", "left"), ("Right", "right"))
_FINGERS = (
    ("Index", "Index", ("Proximal", "Intermediate", "Distal")),
    ("Middle", "Middle", ("Proximal", "Intermediate", "Distal")),
    ("Ring", "Ring", ("Proximal", "Intermediate", "Distal")),
    ("Pinky", "Little", ("Proximal", "Intermediate", "Distal")),
    ("Thumb", "Thumb", ("Metacarpal", "Proximal", "Distal")),
)


def _build_rig_map() -> dict[str, str]:
    """Mixamo rig name (without the ``mixamorig`` prefix) -> VRM humanoid bone name."""
    out = {
        "Hips": "hips",
        "Spine": "spine",
        "Spine1": "chest",
        "Spine2": "upperChest",
        "Neck": "neck",
        "Head": "head",
    }
    for mixamo, vrm in _SIDES:
        out[f"{mixamo}Shoulder"] = f"{vrm}Shoulder"
        out[f"{mixamo}Arm"] = f"{vrm}UpperArm"
        out[f"{mixamo}ForeArm"] = f"{vrm}LowerArm"
        out[f"{mixamo}Hand"] = f"{vrm}Hand"
        out[f"{mixamo}UpLeg"] = f"{vrm}UpperLeg"
        out[f"{mixamo}Leg"] = f"{vrm}LowerLeg"
        out[f"{mixamo}Foot"] = f"{vrm}Foot"
        out[f"{mixamo}ToeBase"] = f"{vrm}Toes"
        for finger, vrm_finger, joints in _FINGERS:
            for index, joint in enumerate(joints, start=1):
                out[f"{mixamo}Hand{finger}{index}"] = f"{vrm}{vrm_finger}{joint}"
    return out


#: Same table as ``mixamoVRMRigMap.js``.
MIXAMO_TO_VRM = _build_rig_map()
#: The bones a ``mask: upper_body`` clip does not drive.
LOWER_BODY_BONES = frozenset(
    name
    for name in MIXAMO_TO_VRM.values()
    if name == "hips" or any(k in name for k in ("Leg", "Foot", "Toes"))
)


class FbxError(ValueError):
    """The file is not a binary FBX this reader understands."""


@dataclass
class _Node:
    name: str
    props: list[Any]
    children: list[_Node] = field(default_factory=list)

    def child(self, name: str) -> _Node | None:
        return next((c for c in self.children if c.name == name), None)

    def all(self, name: str) -> list[_Node]:
        return [c for c in self.children if c.name == name]


# -- the binary container ---------------------------------------------------------------

_ARRAY_TYPES = {"f": "<f4", "d": "<f8", "l": "<i8", "i": "<i4", "b": "?"}


def _read_props(
    data: bytes, pos: int, count: int, node_name: str
) -> tuple[list[Any], int]:
    props: list[Any] = []
    for _ in range(count):
        kind = chr(data[pos])
        pos += 1
        if kind == "Y":
            props.append(struct.unpack_from("<h", data, pos)[0])
            pos += 2
        elif kind == "C":
            props.append(bool(data[pos]))
            pos += 1
        elif kind == "I":
            props.append(struct.unpack_from("<i", data, pos)[0])
            pos += 4
        elif kind == "F":
            props.append(struct.unpack_from("<f", data, pos)[0])
            pos += 4
        elif kind == "D":
            props.append(struct.unpack_from("<d", data, pos)[0])
            pos += 8
        elif kind == "L":
            props.append(struct.unpack_from("<q", data, pos)[0])
            pos += 8
        elif kind in _ARRAY_TYPES:
            length, encoding, size = struct.unpack_from("<III", data, pos)
            pos += 12
            if node_name in _WANTED_ARRAYS:
                raw = data[pos : pos + size]
                if encoding == 1:
                    raw = zlib.decompress(raw)
                props.append(np.frombuffer(raw, dtype=_ARRAY_TYPES[kind], count=length))
            else:
                props.append(None)
            pos += size
        elif kind in ("S", "R"):
            (size,) = struct.unpack_from("<I", data, pos)
            pos += 4
            raw = data[pos : pos + size]
            props.append(raw.decode("utf-8", "replace") if kind == "S" else raw)
            pos += size
        else:
            raise FbxError(f"unknown FBX property type {kind!r}")
    return props, pos


def _read_node(data: bytes, pos: int, wide: bool) -> tuple[_Node | None, int]:
    head = 25 if wide else 13
    if wide:
        end, count, _list_len, name_len = struct.unpack_from("<QQQB", data, pos)
    else:
        end, count, _list_len, name_len = struct.unpack_from("<IIIB", data, pos)
    if end == 0:  # the null record that closes a list of children
        return None, pos + head
    pos += head
    name = data[pos : pos + name_len].decode("utf-8", "replace")
    pos += name_len
    props, pos = _read_props(data, pos, count, name)
    node = _Node(name, props)
    while pos < end:
        child, pos = _read_node(data, pos, wide)
        if child is not None:
            node.children.append(child)
    return node, end


def parse_fbx(data: bytes) -> _Node:
    """The node tree of a binary FBX file, under a synthetic root node."""
    if not data.startswith(_MAGIC):
        raise FbxError("not a binary FBX file")
    version = struct.unpack_from("<I", data, 23)[0]
    wide = version >= 7500
    root = _Node("", [])
    pos = 27
    while pos < len(data) - 8:
        try:
            node, pos = _read_node(data, pos, wide)
        except (struct.error, zlib.error, IndexError) as exc:
            raise FbxError(f"truncated FBX file: {exc}") from exc
        if node is None:
            break
        root.children.append(node)
    return root


# -- rotations --------------------------------------------------------------------------


def _axis_matrices(axis: str, degrees: np.ndarray) -> np.ndarray:
    a = np.radians(degrees)
    c, s = np.cos(a), np.sin(a)
    one, zero = np.ones_like(a), np.zeros_like(a)
    if axis == "X":
        rows = [[one, zero, zero], [zero, c, -s], [zero, s, c]]
    elif axis == "Y":
        rows = [[c, zero, s], [zero, one, zero], [-s, zero, c]]
    else:
        rows = [[c, -s, zero], [s, c, zero], [zero, zero, one]]
    return np.stack([np.stack(r, axis=-1) for r in rows], axis=-2)


def euler_matrices(degrees: np.ndarray, order: str = "XYZ") -> np.ndarray:
    """Rotation matrices ``(n, 3, 3)`` of Euler angles ``(n, 3)`` in degrees.

    ``order`` is the FBX order: its first axis is applied first, so ``"XYZ"`` is ``Rz @ Ry @ Rx``.
    """
    degrees = np.atleast_2d(np.asarray(degrees, dtype=np.float64))
    index = {"X": 0, "Y": 1, "Z": 2}
    out = np.broadcast_to(np.eye(3), (len(degrees), 3, 3)).copy()
    for axis in order:
        out = _axis_matrices(axis, degrees[:, index[axis]]) @ out
    return out


def matrices_to_quaternions(m: np.ndarray) -> np.ndarray:
    """Quaternions ``(n, 4)`` (x, y, z, w) of rotation matrices ``(n, 3, 3)``, sign-continuous."""
    n = len(m)
    q = np.zeros((n, 4))
    m00, m11, m22 = m[:, 0, 0], m[:, 1, 1], m[:, 2, 2]
    trace = m00 + m11 + m22
    choice = np.stack([trace, m00, m11, m22], axis=1).argmax(axis=1)
    w_case, x_case, y_case, z_case = (choice == i for i in range(4))
    if w_case.any():
        r = m[w_case]
        s = np.sqrt(1.0 + trace[w_case]) * 2.0
        q[w_case] = np.stack(
            [
                (r[:, 2, 1] - r[:, 1, 2]) / s,
                (r[:, 0, 2] - r[:, 2, 0]) / s,
                (r[:, 1, 0] - r[:, 0, 1]) / s,
                s / 4.0,
            ],
            axis=1,
        )
    if x_case.any():
        r = m[x_case]
        s = np.sqrt(np.maximum(1.0 + r[:, 0, 0] - r[:, 1, 1] - r[:, 2, 2], 1e-12)) * 2
        q[x_case] = np.stack(
            [
                s / 4.0,
                (r[:, 0, 1] + r[:, 1, 0]) / s,
                (r[:, 0, 2] + r[:, 2, 0]) / s,
                (r[:, 2, 1] - r[:, 1, 2]) / s,
            ],
            axis=1,
        )
    if y_case.any():
        r = m[y_case]
        s = np.sqrt(np.maximum(1.0 + r[:, 1, 1] - r[:, 0, 0] - r[:, 2, 2], 1e-12)) * 2
        q[y_case] = np.stack(
            [
                (r[:, 0, 1] + r[:, 1, 0]) / s,
                s / 4.0,
                (r[:, 1, 2] + r[:, 2, 1]) / s,
                (r[:, 0, 2] - r[:, 2, 0]) / s,
            ],
            axis=1,
        )
    if z_case.any():
        r = m[z_case]
        s = np.sqrt(np.maximum(1.0 + r[:, 2, 2] - r[:, 0, 0] - r[:, 1, 1], 1e-12)) * 2
        q[z_case] = np.stack(
            [
                (r[:, 0, 2] + r[:, 2, 0]) / s,
                (r[:, 1, 2] + r[:, 2, 1]) / s,
                s / 4.0,
                (r[:, 1, 0] - r[:, 0, 1]) / s,
            ],
            axis=1,
        )
    q /= np.linalg.norm(q, axis=1, keepdims=True)
    if (
        n > 1
    ):  # keep neighbours on the same side, so that interpolating takes the short way
        flips = np.where(np.sum(q[1:] * q[:-1], axis=1) < 0.0, -1.0, 1.0)
        q *= np.concatenate([[1.0], np.cumprod(flips)])[:, None]
    return q


def quaternion_to_matrix(q: np.ndarray) -> np.ndarray:
    """The 3x3 float32 rotation matrix of a (x, y, z, w) quaternion."""
    x, y, z, w = (float(v) for v in q)
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=np.float32,
    )


# -- the scene ----------------------------------------------------------------------------


@dataclass
class _Model:
    name: str
    translation: np.ndarray
    rotation: np.ndarray  # degrees
    pre: np.ndarray | None
    post: np.ndarray | None
    order: str
    parent: int | None = None
    #: "Lcl Rotation" / "Lcl Translation" -> {"X": (times_s, values), ...}
    curves: dict[str, dict[str, tuple[np.ndarray, np.ndarray]]] = field(
        default_factory=dict
    )


def _properties(node: _Node) -> dict[str, list[Any]]:
    block = node.child("Properties70")
    out: dict[str, list[Any]] = {}
    for p in block.children if block else []:
        if p.name == "P" and p.props:
            out[str(p.props[0])] = p.props[4:]
    return out


def _vec3(values: list[Any] | None, default: tuple[float, float, float]) -> np.ndarray:
    if values is None or len(values) < 3:
        return np.array(default, dtype=np.float64)
    return np.array([float(v) for v in values[:3]], dtype=np.float64)


def _clean_name(raw: str) -> str:
    """``"mixamorig:Hips\\x00\\x01Model"`` -> ``"mixamorigHips"`` (three.js drops the colon)."""
    return raw.split("\x00")[0].replace(":", "").replace(" ", "_")


def _local_rotation(model: _Model, euler_deg: np.ndarray | None = None) -> np.ndarray:
    """``(n, 3, 3)``: ``Pre * R(euler) * Post^-1`` for the angles, or the static ones."""
    angles = model.rotation[None, :] if euler_deg is None else euler_deg
    out = euler_matrices(angles, model.order)
    if model.pre is not None:
        out = euler_matrices(model.pre[None, :], model.order) @ out
    if model.post is not None:
        out = out @ np.swapaxes(euler_matrices(model.post[None, :], model.order), 1, 2)
    return out


@dataclass
class MixamoClip:
    """A Mixamo animation retargeted to the VRM normalized humanoid."""

    duration: float
    #: VRM bone name -> (times in seconds, quaternions (n, 4) x y z w), VRM 1.0 handedness
    rotations: dict[str, tuple[np.ndarray, np.ndarray]]
    #: hips translation in the clip's own units: (times, (n, 3)); ``None`` when not animated
    hips_position: tuple[np.ndarray, np.ndarray] | None
    #: height of the hips in the clip's rest pose (to scale the translation to a model)
    hips_rest_height: float

    def sample_rotation(self, bone: str, t: float) -> np.ndarray | None:
        track = self.rotations.get(bone)
        if track is None:
            return None
        return _interpolate(track[0], track[1], t, normalize=True)

    def sample_hips_position(self, t: float) -> np.ndarray | None:
        if self.hips_position is None:
            return None
        return _interpolate(self.hips_position[0], self.hips_position[1], t)


def _interpolate(
    times: np.ndarray, values: np.ndarray, t: float, normalize: bool = False
) -> np.ndarray:
    if len(times) == 1 or t <= times[0]:
        return values[0]
    if t >= times[-1]:
        return values[-1]
    hi = int(np.searchsorted(times, t))
    lo = hi - 1
    a = (t - times[lo]) / max(times[hi] - times[lo], 1e-9)
    out = values[lo] * (1.0 - a) + values[hi] * a
    if normalize:
        out = out / max(float(np.linalg.norm(out)), 1e-9)
    return out


def _curves_to_samples(
    axes: dict[str, tuple[np.ndarray, np.ndarray]], default: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """Merge the X/Y/Z curves of one property onto one list of times: (times, (n, 3))."""
    times = np.unique(np.concatenate([t for t, _v in axes.values()]))
    out = np.empty((len(times), 3))
    for i, axis in enumerate("XYZ"):
        if axis in axes:
            out[:, i] = np.interp(times, axes[axis][0], axes[axis][1])
        else:
            out[:, i] = default[i]
    return times, out


def _collect_models(root: _Node) -> dict[int, _Model]:
    objects = root.child("Objects")
    connections = root.child("Connections")
    if objects is None or connections is None:
        raise FbxError("the FBX has no scene")
    models: dict[int, _Model] = {}
    for node in objects.all("Model"):
        props = _properties(node)
        order_index = int(props["RotationOrder"][0]) if "RotationOrder" in props else 0
        order = _ROTATION_ORDERS[order_index if 0 <= order_index < 6 else 0]
        models[int(node.props[0])] = _Model(
            name=_clean_name(str(node.props[1])),
            translation=_vec3(props.get("Lcl Translation"), (0.0, 0.0, 0.0)),
            rotation=_vec3(props.get("Lcl Rotation"), (0.0, 0.0, 0.0)),
            pre=_vec3(props["PreRotation"], (0.0, 0.0, 0.0))
            if "PreRotation" in props
            else None,
            post=_vec3(props["PostRotation"], (0.0, 0.0, 0.0))
            if "PostRotation" in props
            else None,
            order=order,
        )
    curve_data: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    for node in objects.all("AnimationCurve"):
        key_time, key_value = node.child("KeyTime"), node.child("KeyValueFloat")
        if key_time is None or key_value is None:
            continue
        times, values = key_time.props[0], key_value.props[0]
        if times is None or values is None or len(times) == 0:
            continue
        curve_data[int(node.props[0])] = (
            np.asarray(times, dtype=np.float64) / _TICKS_PER_SECOND,
            np.asarray(values, dtype=np.float64),
        )
    curve_nodes = {int(n.props[0]) for n in objects.all("AnimationCurveNode")}
    curve_axes: dict[int, dict[str, tuple[np.ndarray, np.ndarray]]] = {}
    links = connections.all("C")
    # Curve -> curve node first (the file lists them in any order), then curve node -> model.
    for link in links:
        if link.props[0] == "OP" and len(link.props) > 3:
            child, parent, prop = (
                int(link.props[1]),
                int(link.props[2]),
                str(link.props[3]),
            )
            if child in curve_data and parent in curve_nodes and prop.startswith("d|"):
                curve_axes.setdefault(parent, {})[prop[2:]] = curve_data[child]
    for link in links:
        child, parent = int(link.props[1]), int(link.props[2])
        if link.props[0] == "OO" and child in models and parent in models:
            models[child].parent = parent
        elif link.props[0] == "OP" and len(link.props) > 3:
            if child in curve_axes and parent in models:
                models[parent].curves[str(link.props[3])] = curve_axes[child]
    return models


def load_mixamo_clip_from_bytes(data: bytes) -> MixamoClip:
    """Retarget the animation in the bytes of a Mixamo FBX to the VRM normalized humanoid."""
    models = _collect_models(parse_fbx(data))

    rest_world: dict[int, np.ndarray] = {}

    def world(mid: int) -> np.ndarray:
        if mid not in rest_world:
            model = models[mid]
            local = _local_rotation(model)[0]
            parent = model.parent
            rest_world[mid] = local if parent is None else world(parent) @ local
        return rest_world[mid]

    rotations: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    hips_position: tuple[np.ndarray, np.ndarray] | None = None
    hips_rest = 0.0
    duration = 0.0
    for mid, model in models.items():
        if not model.name.startswith("mixamorig"):
            continue
        rig = model.name[len("mixamorig") :]
        bone = MIXAMO_TO_VRM.get(rig)
        if bone is None:
            continue
        if bone == "hips":
            hips_rest = float(model.translation[1])
        axes = model.curves.get("Lcl Rotation")
        if axes:
            times, euler = _curves_to_samples(axes, model.rotation)
            duration = max(duration, float(times[-1]))
            parent_world = (
                world(model.parent) if model.parent is not None else np.eye(3)
            )
            # parent rest world * track rotation * rest world^-1 (loadMixamoAnimation.js)
            retargeted = parent_world @ _local_rotation(model, euler) @ world(mid).T
            rotations[bone] = (times, matrices_to_quaternions(retargeted))
        moves = model.curves.get("Lcl Translation")
        if bone == "hips" and moves:
            times, positions = _curves_to_samples(moves, model.translation)
            duration = max(duration, float(times[-1]))
            hips_position = (times, positions)
    if not rotations:
        raise FbxError("the FBX has no Mixamo rotation curves")
    return MixamoClip(
        duration=duration,
        rotations=rotations,
        hips_position=hips_position,
        hips_rest_height=hips_rest,
    )


def load_mixamo_clip(path: Path | str) -> MixamoClip:
    """Read a Mixamo ``.fbx`` file (see :func:`load_mixamo_clip_from_bytes`)."""
    return load_mixamo_clip_from_bytes(Path(path).read_bytes())
