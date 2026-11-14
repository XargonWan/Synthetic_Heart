"""A small headless renderer for a VRM model: a close-up "webcam" bust, no browser.

``moderngl`` on an EGL context. In a container without a GPU, Mesa's software rasteriser
(llvmpipe) provides the GL, so the Mesa EGL libraries must be installed in the image
(Debian: ``libegl1``, ``libegl-mesa0``, ``libgl1-mesa-dri``); a host with a GPU uses it.

What is drawn is an approximation of the browser's Karada render, not a copy: textured and
lit (no MToon shading, no outlines), the model in its rest pose with the arms lowered, a
little head sway and breathing, and the face expressions as morph targets. It is *not* the
Karada animation engine (no FBX animations).

Everything here must run on ONE thread: the GL context belongs to the thread that made it.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from core.avatar_render.vrm import Primitive, VrmModel

#: Background behind the avatar (the WebUI's dark theme colour), 0-255.
BACKGROUND_RGB = (13, 13, 23)
_MAX_JOINTS = 256


class RendererUnavailable(RuntimeError):
    """No GL context could be created (no moderngl, or no EGL/Mesa)."""


@dataclass
class AvatarPose:
    """What to draw this frame."""

    #: VRM 1.0 preset name -> weight 0..1 (``aa``, ``blink``, ``happy``, ...)
    expressions: dict[str, float] = field(default_factory=dict)
    #: humanoid bone name -> 3x3 rotation, in the parent's frame, applied on top of rest
    bone_deltas: dict[str, np.ndarray] = field(default_factory=dict)


def create_context() -> Any:
    """A standalone GL context. Raises :class:`RendererUnavailable` when there is none."""
    try:
        import moderngl
    except Exception as exc:
        raise RendererUnavailable(f"moderngl is not available: {exc!r}") from exc
    kwargs: dict[str, Any] = (
        {"backend": "egl"} if sys.platform.startswith("linux") else {}
    )
    errors: list[str] = []
    for platform in (None, "surfaceless"):  # surfaceless: containers without a display
        previous = os.environ.get("EGL_PLATFORM")
        if platform is not None:
            os.environ["EGL_PLATFORM"] = platform
        try:
            return moderngl.create_standalone_context(**kwargs)
        except Exception as exc:
            errors.append(f"{platform or 'default'}: {exc}")
        finally:
            if platform is not None:
                if previous is None:
                    os.environ.pop("EGL_PLATFORM", None)
                else:
                    os.environ["EGL_PLATFORM"] = previous
    raise RendererUnavailable(
        "no GL context could be created (" + "; ".join(errors) + ")"
    )


def probe() -> str:
    """Create and release a context; return the GL renderer string (e.g. ``llvmpipe``)."""
    ctx = create_context()
    try:
        return str(ctx.info.get("GL_RENDERER", "unknown"))
    finally:
        ctx.release()


_VERTEX = """
#version 330
uniform mat4 mvp;
uniform mat4 joints[256];
in vec3 in_pos; in vec3 in_nrm; in vec2 in_uv; in vec4 in_joints; in vec4 in_weights;
out vec3 v_nrm; out vec2 v_uv;
void main() {
    mat4 sk = joints[int(in_joints.x)] * in_weights.x + joints[int(in_joints.y)] * in_weights.y
            + joints[int(in_joints.z)] * in_weights.z + joints[int(in_joints.w)] * in_weights.w;
    gl_Position = mvp * (sk * vec4(in_pos, 1.0));
    v_nrm = mat3(sk) * in_nrm;
    v_uv = in_uv;
}
"""
_FRAGMENT = """
#version 330
uniform sampler2D tex; uniform vec4 base; uniform float cutoff; uniform float blended; uniform vec3 light_dir;
in vec3 v_nrm; in vec2 v_uv; out vec4 frag;
void main() {
    vec4 c = texture(tex, v_uv) * base;
    if (c.a < cutoff) discard;
    float lit = 0.6 + 0.4 * max(dot(normalize(v_nrm), normalize(light_dir)), 0.0);
    frag = vec4(c.rgb * lit, blended > 0.5 ? c.a : 1.0);
}
"""


def _perspective(fovy_deg: float, aspect: float, near: float, far: float) -> np.ndarray:
    f = 1.0 / np.tan(np.radians(fovy_deg) / 2.0)
    m = np.zeros((4, 4), dtype=np.float32)
    m[0, 0], m[1, 1] = f / aspect, f
    m[2, 2], m[2, 3] = (far + near) / (near - far), 2 * far * near / (near - far)
    m[3, 2] = -1.0
    return m


def _look_at(eye: np.ndarray, target: np.ndarray) -> np.ndarray:
    forward = target - eye
    forward = forward / np.linalg.norm(forward)
    side = np.cross(forward, np.array([0.0, 1.0, 0.0], dtype=np.float32))
    side /= np.linalg.norm(side)
    up = np.cross(side, forward)
    m = np.eye(4, dtype=np.float32)
    m[0, :3], m[1, :3], m[2, :3] = side, up, -forward
    m[:3, 3] = -m[:3, :3] @ eye
    return m


def _rot_z(angle: float) -> np.ndarray:
    c, s = float(np.cos(angle)), float(np.sin(angle))
    return np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]], dtype=np.float32)


def rgb_to_i420(rgb: np.ndarray) -> bytes:
    """Planar I420 (limited-range BT.601) from an ``(h, w, 3)`` uint8 array (even sizes)."""
    h, w, _ = rgb.shape
    f = rgb.astype(np.float32)
    y = 16.0 + (0.257 * f[..., 0] + 0.504 * f[..., 1] + 0.098 * f[..., 2])
    sub = f.reshape(h // 2, 2, w // 2, 2, 3).mean(axis=(1, 3))
    u = 128.0 + (-0.148 * sub[..., 0] - 0.291 * sub[..., 1] + 0.439 * sub[..., 2])
    v = 128.0 + (0.439 * sub[..., 0] - 0.368 * sub[..., 1] - 0.071 * sub[..., 2])
    return b"".join(
        np.clip(np.rint(p), 0, 255).astype(np.uint8).tobytes() for p in (y, u, v)
    )


class AvatarRenderer:
    """Draws ``model`` at ``width`` x ``height``; one instance, one thread."""

    def __init__(self, model: VrmModel, width: int, height: int) -> None:
        if width % 2 or height % 2 or width < 2 or height < 2:
            raise ValueError("width and height must be even")
        import moderngl

        self._gl = moderngl
        self.model = model
        self.width, self.height = width, height
        self._ctx = create_context()
        self.gl_renderer = str(self._ctx.info.get("GL_RENDERER", "unknown"))
        self._prog = self._ctx.program(vertex_shader=_VERTEX, fragment_shader=_FRAGMENT)
        self._white = self._ctx.texture((1, 1), 4, b"\xff\xff\xff\xff")
        self._textures: dict[int, Any] = {}
        self._draws: list[_Draw] = []
        for prim in model.primitives:
            self._draws.append(_Draw(self, prim))
        self._fbo = self._ctx.framebuffer(
            self._ctx.texture((width, height), 3),
            self._ctx.depth_renderbuffer((width, height)),
        )
        self._fbo.use()
        self._ctx.enable(moderngl.DEPTH_TEST)
        self._ctx.enable(moderngl.BLEND)
        self._ctx.blend_func = moderngl.SRC_ALPHA, moderngl.ONE_MINUS_SRC_ALPHA

        self._rest_rotations: dict[int, np.ndarray] = self._arm_rest_rotations()
        self._prog["mvp"].write(self._camera(width / height).T.tobytes())
        facing = -1.0 if model.faces_minus_z else 1.0
        self._prog["light_dir"].value = (0.3, 0.5, 0.8 * facing)

    # -- set up -------------------------------------------------------------

    def texture(self, index: int | None) -> Any:
        if index is None:
            return self._white
        if index not in self._textures:
            image = self.model.texture_image(index)
            if image is None:
                self._textures[index] = self._white
            else:
                # glTF's v=0 is the first row of the image, as in GL's texture memory:
                # the pixels go up unflipped.
                tex = self._ctx.texture(image.size, 4, image.tobytes())
                tex.build_mipmaps()
                tex.filter = (self._gl.LINEAR_MIPMAP_LINEAR, self._gl.LINEAR)
                tex.repeat_x = tex.repeat_y = True
                self._textures[index] = tex
        return self._textures[index]

    def _arm_rest_rotations(self) -> dict[int, np.ndarray]:
        """Lower the arms from the T-pose so the close-up is not framed by two horizontal arms."""
        model = self.model
        worlds = model.world_matrices()
        out: dict[int, np.ndarray] = {}
        for upper, lower in (
            ("leftUpperArm", "leftLowerArm"),
            ("rightUpperArm", "rightLowerArm"),
        ):
            u, lo = model.humanoid.get(upper), model.humanoid.get(lower)
            if u is None or lo is None:
                continue
            direction = worlds[lo][:3, 3] - worlds[u][:3, 3]
            if abs(direction[0]) < 1e-6:
                continue
            angle = -np.sign(direction[0]) * np.radians(68.0)
            # A rotation about the world Z axis, put in the bone's parent frame.
            parent = model.parents.get(u)
            parent_rot = (
                worlds[parent][:3, :3]
                if parent is not None
                else np.eye(3, dtype=np.float32)
            )
            delta = parent_rot.T @ _rot_z(float(angle)) @ parent_rot
            out[u] = (delta @ model.rest_rotation(u)).astype(np.float32)
        return out

    def _camera(self, aspect: float) -> np.ndarray:
        model = self.model
        worlds = model.world_matrices()
        head = model.humanoid.get("head")
        center = (
            worlds[head][:3, 3]
            if head is not None
            else np.array([0.0, 1.4, 0.0], np.float32)
        )
        target = center + np.array([0.0, -0.05, 0.0], dtype=np.float32)
        side = -1.0 if model.faces_minus_z else 1.0
        eye = target + np.array([0.0, 0.03, 0.85 * side], dtype=np.float32)
        return (_perspective(32.0, aspect, 0.05, 10.0) @ _look_at(eye, target)).astype(
            np.float32
        )

    # -- a frame ------------------------------------------------------------

    def render(self, pose: AvatarPose) -> bytes:
        """Draw ``pose`` and return the frame as planar I420 bytes."""
        model = self.model
        rotations = dict(self._rest_rotations)
        for name, delta in pose.bone_deltas.items():
            node = model.humanoid.get(name)
            if node is not None:
                base = rotations.get(node, model.rest_rotation(node))
                rotations[node] = (delta @ base).astype(np.float32)
        worlds = model.world_matrices(rotations)

        # Morph weights per (glTF mesh, target) from the expressions.
        weights: dict[tuple[int, int], float] = {}
        for name, value in pose.expressions.items():
            if value <= 0.001:
                continue
            for mesh, target, scale in model.expressions.get(name, ()):
                key = (mesh, target)
                weights[key] = min(1.0, weights.get(key, 0.0) + value * scale)

        skin_cache: dict[tuple[int, int | None], np.ndarray] = {}
        self._ctx.clear(*(c / 255.0 for c in BACKGROUND_RGB), 1.0)
        for blended in (False, True):  # opaque and cut-out first, then the blended ones
            self._fbo.depth_mask = not blended
            for draw in self._draws:
                if draw.blend == blended:
                    draw.draw(worlds, weights, skin_cache)
        self._fbo.depth_mask = True

        data = self._fbo.read(components=3)
        rgb = np.frombuffer(data, dtype=np.uint8).reshape(self.height, self.width, 3)[
            ::-1
        ]
        return rgb_to_i420(rgb)

    def close(self) -> None:
        try:
            self._ctx.release()
        except Exception:
            pass


class _Draw:
    """One primitive's GPU buffers and per-frame update."""

    def __init__(self, renderer: AvatarRenderer, prim: Primitive) -> None:
        self.r = renderer
        self.prim = prim
        ctx, prog = renderer._ctx, renderer._prog
        self.blend = prim.material.alpha_mode == "BLEND"
        self.cutoff = (
            prim.material.cutoff
            if prim.material.alpha_mode == "MASK"
            else (0.02 if self.blend else 0.0)
        )
        self.texture = renderer.texture(prim.material.texture)
        self.vbo_pos = ctx.buffer(prim.positions.tobytes(), dynamic=True)
        self.vao = ctx.vertex_array(
            prog,
            [
                (self.vbo_pos, "3f", "in_pos"),
                (ctx.buffer(prim.normals.tobytes()), "3f", "in_nrm"),
                (ctx.buffer(prim.uvs.tobytes()), "2f", "in_uv"),
                (ctx.buffer(prim.joints.tobytes()), "4f", "in_joints"),
                (ctx.buffer(prim.weights.tobytes()), "4f", "in_weights"),
            ],
            ctx.buffer(prim.indices.tobytes()),
        )
        self._morphed = False

    def draw(
        self,
        worlds: list[np.ndarray],
        weights: dict[tuple[int, int], float],
        skin_cache: dict[tuple[int, int | None], np.ndarray],
    ) -> None:
        prim, prog = self.prim, self.r._prog
        active = [
            (t, w)
            for (m, t), w in weights.items()
            if m == prim.mesh and t < len(prim.targets)
        ]
        if active:
            positions = prim.positions.copy()
            for target, weight in active:
                positions += prim.targets[target] * np.float32(weight)
            self.vbo_pos.write(positions.tobytes())
        elif self._morphed:
            self.vbo_pos.write(prim.positions.tobytes())
        self._morphed = bool(active)

        key = (prim.node, prim.skin)
        joints = skin_cache.get(key)
        if joints is None:
            joints = np.zeros((_MAX_JOINTS, 4, 4), dtype=np.float32)
            joints[:] = np.eye(4, dtype=np.float32)
            if prim.skin is None:
                joints[0] = worlds[prim.node]
            else:
                skin = self.r.model.skins[prim.skin]
                count = min(len(skin.joints), _MAX_JOINTS)
                joint_worlds = np.stack([worlds[j] for j in skin.joints[:count]])
                # glTF: a skinned mesh's own node transform is ignored.
                joints[:count] = joint_worlds @ skin.inverse_bind[:count]
            skin_cache[key] = joints
        prog["joints"].write(joints.transpose(0, 2, 1).tobytes())
        prog["base"].value = tuple(prim.material.base_color)
        prog["cutoff"].value = self.cutoff
        prog["blended"].value = 1.0 if self.blend else 0.0
        self.texture.use(0)
        self.vao.render()
