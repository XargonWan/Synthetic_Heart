"""A VRM (0.x or 1.0) model read from its ``.vrm``/``.glb`` file, with no GL involved.

Only what a close-up face render needs: meshes (positions, normals, uvs, skin weights,
morph targets), their materials and textures, the node hierarchy, the humanoid bone map
and the expression -> morph-target bindings. ``core.avatar_render.renderer`` draws it.

Matrices are column-vector (``M @ v``), 4x4, ``float32``.

Expressions use the VRM 1.0 preset names everywhere (``aa ih ou ee oh blink happy angry
sad relaxed surprised``); a VRM 0.x model's presets are mapped onto them.
"""

from __future__ import annotations

import io
import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

#: VRM 0.x blend-shape preset -> VRM 1.0 name.
_VRM0_PRESETS = {
    "a": "aa",
    "i": "ih",
    "u": "ou",
    "e": "ee",
    "o": "oh",
    "blink": "blink",
    "joy": "happy",
    "angry": "angry",
    "sorrow": "sad",
    "fun": "relaxed",
}
#: VRM 1.0 presets this renderer knows how to bind.
_VRM1_PRESETS = (
    "aa",
    "ih",
    "ou",
    "ee",
    "oh",
    "blink",
    "happy",
    "angry",
    "sad",
    "relaxed",
    "surprised",
)

_COMPONENT_TYPES = {
    5120: np.int8,
    5121: np.uint8,
    5122: np.int16,
    5123: np.uint16,
    5125: np.uint32,
    5126: np.float32,
}
_TYPE_WIDTH = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}


class VrmError(ValueError):
    """The file is not a VRM/GLB this loader can read."""


@dataclass
class Material:
    base_color: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    texture: int | None = None
    alpha_mode: str = "OPAQUE"  # OPAQUE | MASK | BLEND
    cutoff: float = 0.5
    double_sided: bool = False
    # -- MToon (the toon shading VRM models use); the defaults draw "lit, no shade" --
    #: the colour of the shaded side (sRGB) and the texture it is multiplied with
    shade_color: tuple[float, float, float] = (1.0, 1.0, 1.0)
    shade_texture: int | None = None
    #: where the light/shade ramp starts and ends, in ``dot(normal, light)``. Equal edges
    #: are a hard step; a very low pair means "lit everywhere" (flat, as in anime models).
    shade_edges: tuple[float, float] = (-1.0, -1.0)
    emission_color: tuple[float, float, float] = (0.0, 0.0, 0.0)  # sRGB
    emission_texture: int | None = None
    #: outline: half-width in metres (0 = none), colour (sRGB) and how much light it takes
    outline_width: float = 0.0
    outline_color: tuple[float, float, float] = (0.0, 0.0, 0.0)
    outline_lighting_mix: float = 1.0


def _vec(value: Any, n: int, default: tuple[float, ...]) -> tuple[float, ...]:
    try:
        out = tuple(float(x) for x in list(value)[:n])
    except (TypeError, ValueError):
        return default
    return out if len(out) == n else default


def _apply_mtoon_v0(material: Material, props: dict[str, Any]) -> None:
    """VRM 0.x ``materialProperties`` entry of a ``VRM/MToon`` shader (Unity's MToon)."""
    if "MToon" not in str(props.get("shader", "")):
        return
    f = props.get("floatProperties", {}) or {}
    v = props.get("vectorProperties", {}) or {}
    t = props.get("textureProperties", {}) or {}
    color = _vec(v.get("_Color"), 4, material.base_color)
    material.base_color = (color[0], color[1], color[2], color[3])
    material.shade_color = _vec(v.get("_ShadeColor"), 3, (1.0, 1.0, 1.0))  # type: ignore[assignment]
    if isinstance(t.get("_ShadeTexture"), int):
        material.shade_texture = int(t["_ShadeTexture"])
    # Unity MToon: the ramp runs from _ShadeShift to lerp(1, _ShadeShift, _ShadeToony).
    shift = float(f.get("_ShadeShift", 0.0))
    toony = float(f.get("_ShadeToony", 0.9))
    material.shade_edges = (shift, 1.0 + (shift - 1.0) * toony)
    material.emission_color = _vec(v.get("_EmissionColor"), 3, (0.0, 0.0, 0.0))  # type: ignore[assignment]
    if isinstance(t.get("_EmissionMap"), int):
        material.emission_texture = int(t["_EmissionMap"])
    # Outline: only the world-space mode (1); the width is in centimetres.
    if int(f.get("_OutlineWidthMode", 0)) == 1:
        material.outline_width = max(0.0, float(f.get("_OutlineWidth", 0.0))) * 0.01
    material.outline_color = _vec(v.get("_OutlineColor"), 3, (0.0, 0.0, 0.0))  # type: ignore[assignment]
    material.outline_lighting_mix = (
        float(f.get("_OutlineLightingMix", 1.0))
        if int(f.get("_OutlineColorMode", 0)) == 1
        else 0.0
    )


def _apply_mtoon_v1(
    material: Material, mtoon: dict[str, Any], mat: dict[str, Any]
) -> None:
    """VRM 1.0 ``VRMC_materials_mtoon`` extension of a glTF material."""
    material.shade_color = _vec(mtoon.get("shadeColorFactor"), 3, (1.0, 1.0, 1.0))  # type: ignore[assignment]
    tex = (mtoon.get("shadeMultiplyTexture") or {}).get("index")
    if isinstance(tex, int):
        material.shade_texture = tex
    # VRM 1.0: shading = linearstep(-1 + toony, 1 - toony, dot + shift)
    shift = float(mtoon.get("shadingShiftFactor", 0.0))
    toony = float(mtoon.get("shadingToonyFactor", 0.9))
    material.shade_edges = (-1.0 + toony - shift, 1.0 - toony - shift)
    material.emission_color = _vec(mat.get("emissiveFactor"), 3, (0.0, 0.0, 0.0))  # type: ignore[assignment]
    emissive = (mat.get("emissiveTexture") or {}).get("index")
    if isinstance(emissive, int):
        material.emission_texture = emissive
    if mtoon.get("outlineWidthMode") == "worldCoordinates":
        material.outline_width = max(0.0, float(mtoon.get("outlineWidthFactor", 0.0)))
    material.outline_color = _vec(mtoon.get("outlineColorFactor"), 3, (0.0, 0.0, 0.0))  # type: ignore[assignment]
    material.outline_lighting_mix = float(mtoon.get("outlineLightingMixFactor", 1.0))


@dataclass
class Primitive:
    node: int
    mesh: int
    skin: int | None
    positions: np.ndarray  # (n, 3) float32
    normals: np.ndarray  # (n, 3) float32
    uvs: np.ndarray  # (n, 2) float32
    joints: np.ndarray  # (n, 4) float32 (indices into the skin's joint list)
    weights: np.ndarray  # (n, 4) float32
    indices: np.ndarray  # (m,) uint32
    targets: list[np.ndarray]  # morph target position deltas, each (n, 3) float32
    material: Material


@dataclass
class Skin:
    joints: list[int]
    inverse_bind: np.ndarray  # (k, 4, 4) float32


@dataclass
class VrmModel:
    nodes: list[dict[str, Any]]
    parents: dict[int, int]
    primitives: list[Primitive]
    skins: list[Skin]
    humanoid: dict[str, int]
    #: preset name -> [(glTF mesh index, morph target index, weight 0..1)]
    expressions: dict[str, list[tuple[int, int, float]]]
    #: VRM 0.x models face -Z, VRM 1.0 models face +Z.
    faces_minus_z: bool
    _gltf: dict[str, Any] = field(repr=False, default_factory=dict)
    _bin: bytes = field(repr=False, default=b"")
    _images: dict[int, Any] = field(repr=False, default_factory=dict)

    # -- textures -----------------------------------------------------------

    def texture_image(self, texture_index: int) -> Any | None:
        """The texture as an RGBA Pillow image (cached), or ``None`` when unreadable."""
        if texture_index in self._images:
            return self._images[texture_index]
        image = None
        try:
            from PIL import Image

            source = self._gltf["textures"][texture_index]["source"]
            view = self._gltf["bufferViews"][self._gltf["images"][source]["bufferView"]]
            start = view.get("byteOffset", 0)
            data = self._bin[start : start + view["byteLength"]]
            image = Image.open(io.BytesIO(data)).convert("RGBA")
        except Exception:
            image = None
        self._images[texture_index] = image
        return image

    # -- skeleton -----------------------------------------------------------

    def local_matrix(
        self, index: int, rotation: np.ndarray | None = None
    ) -> np.ndarray:
        """Local transform of node ``index``; ``rotation`` (3x3) replaces its own."""
        node = self.nodes[index]
        if "matrix" in node and rotation is None:
            return np.array(node["matrix"], dtype=np.float32).reshape(4, 4).T
        out = np.eye(4, dtype=np.float32)
        r = (
            rotation
            if rotation is not None
            else _quat_to_matrix(node.get("rotation", (0, 0, 0, 1)))
        )
        scale = node.get("scale", (1.0, 1.0, 1.0))
        out[:3, :3] = r * np.array(scale, dtype=np.float32)[None, :]
        out[:3, 3] = node.get("translation", (0.0, 0.0, 0.0))
        return out

    def rest_rotation(self, index: int) -> np.ndarray:
        node = self.nodes[index]
        if "matrix" in node:
            m = np.array(node["matrix"], dtype=np.float32).reshape(4, 4).T[:3, :3]
            scale = np.linalg.norm(m, axis=0)
            return m / np.where(scale == 0, 1, scale)
        return _quat_to_matrix(node.get("rotation", (0, 0, 0, 1)))

    def world_matrices(
        self, rotations: dict[int, np.ndarray] | None = None
    ) -> list[np.ndarray]:
        """World matrix of every node; ``rotations`` overrides local rotations."""
        rotations = rotations or {}
        worlds: list[np.ndarray | None] = [None] * len(self.nodes)

        def world(i: int) -> np.ndarray:
            cached = worlds[i]
            if cached is not None:
                return cached
            local = self.local_matrix(i, rotations.get(i))
            parent = self.parents.get(i)
            result = local if parent is None else world(parent) @ local
            worlds[i] = result
            return result

        return [world(i) for i in range(len(self.nodes))]


def _quat_to_matrix(q: Any) -> np.ndarray:
    x, y, z, w = (float(v) for v in q)
    return np.array(
        [
            [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
            [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
            [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
        ],
        dtype=np.float32,
    )


def _read_accessor(gltf: dict[str, Any], blob: bytes, index: int) -> np.ndarray:
    acc = gltf["accessors"][index]
    dtype = np.dtype(_COMPONENT_TYPES[acc["componentType"]])
    width = _TYPE_WIDTH[acc["type"]]
    count = acc["count"]
    if "bufferView" not in acc:
        return np.zeros((count, width), dtype=dtype)
    view = gltf["bufferViews"][acc["bufferView"]]
    base = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = view.get("byteStride") or dtype.itemsize * width
    if stride == dtype.itemsize * width:
        arr = np.frombuffer(
            blob, dtype=dtype, count=count * width, offset=base
        ).reshape(count, width)
    else:
        arr = np.ndarray(
            (count, width),
            dtype=dtype,
            buffer=blob,
            offset=base,
            strides=(stride, dtype.itemsize),
        )
    arr = np.array(arr)
    if acc.get("normalized"):
        arr = np.maximum(arr.astype(np.float32) / np.iinfo(dtype).max, -1.0)
    return arr


def _expressions(gltf: dict[str, Any]) -> dict[str, list[tuple[int, int, float]]]:
    ext = gltf.get("extensions", {})
    out: dict[str, list[tuple[int, int, float]]] = {}
    if "VRMC_vrm" in ext:  # VRM 1.0
        presets = ext["VRMC_vrm"].get("expressions", {}).get("preset", {})
        for name in _VRM1_PRESETS:
            binds = []
            for bind in presets.get(name, {}).get("morphTargetBinds", []) or []:
                mesh = gltf["nodes"][bind["node"]].get("mesh")
                if mesh is not None:
                    binds.append(
                        (int(mesh), int(bind["index"]), float(bind.get("weight", 1.0)))
                    )
            if binds:
                out[name] = binds
    elif "VRM" in ext:  # VRM 0.x
        for group in (
            ext["VRM"].get("blendShapeMaster", {}).get("blendShapeGroups", []) or []
        ):
            name = _VRM0_PRESETS.get(str(group.get("presetName", "")).lower())
            if name is None:
                continue
            binds = [
                (int(b["mesh"]), int(b["index"]), float(b.get("weight", 100.0)) / 100.0)
                for b in group.get("binds", []) or []
            ]
            if binds:
                out.setdefault(name, []).extend(binds)
    return out


def _humanoid(gltf: dict[str, Any]) -> dict[str, int]:
    ext = gltf.get("extensions", {})
    if "VRMC_vrm" in ext:
        bones = ext["VRMC_vrm"].get("humanoid", {}).get("humanBones", {})
        return {name: int(b["node"]) for name, b in bones.items() if "node" in b}
    bones = ext.get("VRM", {}).get("humanoid", {}).get("humanBones", []) or []
    return {b["bone"]: int(b["node"]) for b in bones if "bone" in b and "node" in b}


def parse_vrm(data: bytes) -> VrmModel:
    """Parse the bytes of a binary glTF (``.vrm`` / ``.glb``)."""
    if len(data) < 20 or data[:4] != b"glTF":
        raise VrmError("not a binary glTF (.vrm/.glb) file")
    try:
        json_len, _ = struct.unpack_from("<II", data, 12)
        gltf = json.loads(data[20 : 20 + json_len])
        offset = 20 + json_len
        blob = b""
        if offset + 8 <= len(data):
            bin_len, _ = struct.unpack_from("<II", data, offset)
            blob = data[offset + 8 : offset + 8 + bin_len]
    except Exception as exc:
        raise VrmError(f"unreadable glTF container: {exc}") from exc
    ext = gltf.get("extensions", {})
    if "VRM" not in ext and "VRMC_vrm" not in ext:
        raise VrmError("the file has no VRM extension")

    nodes = gltf.get("nodes", [])
    parents = {c: i for i, n in enumerate(nodes) for c in n.get("children", [])}
    skins = [
        Skin(
            joints=[int(j) for j in s["joints"]],
            inverse_bind=(
                _read_accessor(gltf, blob, s["inverseBindMatrices"])
                .astype(np.float32)
                .reshape(-1, 4, 4)
                .transpose(0, 2, 1)
                if "inverseBindMatrices" in s
                else np.tile(np.eye(4, dtype=np.float32), (len(s["joints"]), 1, 1))
            ),
        )
        for s in gltf.get("skins", [])
    ]
    materials = []
    v0_props = (ext.get("VRM", {}) or {}).get("materialProperties", []) or []
    for index, mat in enumerate(gltf.get("materials", [])):
        pbr = mat.get("pbrMetallicRoughness", {})
        material = Material(
            base_color=tuple(pbr.get("baseColorFactor", (1.0, 1.0, 1.0, 1.0))),
            texture=pbr.get("baseColorTexture", {}).get("index"),
            alpha_mode=mat.get("alphaMode", "OPAQUE"),
            cutoff=float(mat.get("alphaCutoff", 0.5)),
            double_sided=bool(mat.get("doubleSided", False)),
        )
        try:
            mtoon = (mat.get("extensions", {}) or {}).get("VRMC_materials_mtoon")
            if mtoon:
                _apply_mtoon_v1(material, mtoon, mat)
            elif index < len(v0_props):
                _apply_mtoon_v0(material, v0_props[index])
        except (TypeError, ValueError, KeyError):
            pass  # unreadable shading data: the material stays plain (lit, no shade)
        materials.append(material)

    primitives: list[Primitive] = []
    for node_index, node in enumerate(nodes):
        if "mesh" not in node:
            continue
        for prim in gltf["meshes"][node["mesh"]]["primitives"]:
            if prim.get("mode", 4) != 4 or "indices" not in prim:
                continue
            attrs = prim["attributes"]
            positions = _read_accessor(gltf, blob, attrs["POSITION"]).astype(np.float32)
            n = len(positions)
            skinned = "JOINTS_0" in attrs and "skin" in node
            primitives.append(
                Primitive(
                    node=node_index,
                    mesh=node["mesh"],
                    skin=node["skin"] if skinned else None,
                    positions=positions,
                    normals=(
                        _read_accessor(gltf, blob, attrs["NORMAL"]).astype(np.float32)
                        if "NORMAL" in attrs
                        else np.zeros((n, 3), np.float32)
                    ),
                    uvs=(
                        _read_accessor(gltf, blob, attrs["TEXCOORD_0"]).astype(
                            np.float32
                        )
                        if "TEXCOORD_0" in attrs
                        else np.zeros((n, 2), np.float32)
                    ),
                    joints=(
                        _read_accessor(gltf, blob, attrs["JOINTS_0"]).astype(np.float32)
                        if skinned
                        else np.zeros((n, 4), np.float32)
                    ),
                    weights=(
                        _read_accessor(gltf, blob, attrs["WEIGHTS_0"]).astype(
                            np.float32
                        )
                        if skinned
                        else np.tile(np.array([1, 0, 0, 0], np.float32), (n, 1))
                    ),
                    indices=_read_accessor(gltf, blob, prim["indices"])
                    .astype(np.uint32)
                    .ravel(),
                    targets=[
                        _read_accessor(gltf, blob, t["POSITION"]).astype(np.float32)
                        for t in prim.get("targets", [])
                        if "POSITION" in t
                    ],
                    material=materials[prim["material"]]
                    if "material" in prim
                    else Material(),
                )
            )
    if not primitives:
        raise VrmError("the model has no drawable meshes")

    return VrmModel(
        nodes=nodes,
        parents=parents,
        primitives=primitives,
        skins=skins,
        humanoid=_humanoid(gltf),
        expressions=_expressions(gltf),
        faces_minus_z="VRMC_vrm" not in ext,
        _gltf=gltf,
        _bin=blob,
    )


def load_vrm(path: Path | str) -> VrmModel:
    """Read and parse a ``.vrm`` file."""
    return parse_vrm(Path(path).read_bytes())
