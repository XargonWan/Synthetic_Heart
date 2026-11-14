"""A small headless renderer for a VRM model: a close-up "webcam" bust, no browser.

``moderngl`` on an EGL context. In a container without a GPU, Mesa's software rasteriser
(llvmpipe) provides the GL, so the Mesa EGL libraries must be installed in the image
(Debian: ``libegl1``, ``libegl-mesa0``, ``libgl1-mesa-dri``); a host with a GPU uses it.

What is drawn is an approximation of the browser's Karada render (three.js + three-vrm),
not a copy: the same camera, lights and sRGB output, and MToon's flat/toon colouring
(shade colour, light/shade ramp, emission, world-space outlines) read from the model, in its
rest pose with the arms lowered, a little head sway and breathing, and the face expressions as
morph targets. It is *not* the Karada animation engine (no FBX animations), and MToon's rim
light, UV animation and per-pixel masks are left out.

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

# The WebUI's scene (res/synth_webui/js/vrm-viewer.mjs): a 30 degree camera and three lights.
_FOV_DEG = 30.0
#: "webcam" framing (``fitCloseupFraming``): head and shoulders around this height, this far
#: away, pulled back in a narrow window so the shoulders still fit (below aspect 0.8).
_CLOSEUP_TARGET_Y = 1.30
_CLOSEUP_DISTANCE = 1.25
_CLOSEUP_DIR = (0.03, 1.0)  # (up, towards the camera)
#: three's AmbientLight(0.6), DirectionalLight(1.2) at (1, 1.2, 1), DirectionalLight(0.4) at (-1, 1.2, -1)
_AMBIENT, _KEY, _FILL = 0.6, 1.2, 0.4
_KEY_POS = (1.0, 1.2, 1.0)
_FILL_POS = (-1.0, 1.2, -1.0)


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
uniform float outline_w;   // 0 in the colour pass; the hull's half-width (metres) in the outline pass
in vec3 in_pos; in vec3 in_nrm; in vec2 in_uv; in vec4 in_joints; in vec4 in_weights;
out vec3 v_nrm; out vec2 v_uv;
void main() {
    mat4 sk = joints[int(in_joints.x)] * in_weights.x + joints[int(in_joints.y)] * in_weights.y
            + joints[int(in_joints.z)] * in_weights.z + joints[int(in_joints.w)] * in_weights.w;
    vec3 nrm = normalize(mat3(sk) * in_nrm);
    vec4 world = sk * vec4(in_pos, 1.0);
    world.xyz += nrm * outline_w;
    gl_Position = mvp * world;
    v_nrm = nrm;
    v_uv = in_uv;
}
"""
# Lighting is done in linear light and written as sRGB, as three.js does
# (renderer.outputColorSpace = SRGBColorSpace). MToon colours the surface by its light/shade
# ramp (below) and multiplies the result by the light that reaches it: the ambient light plus
# each directional light weighted by the cosine to the normal, as in three-vrm.
_FRAGMENT = """
#version 330
uniform sampler2D tex; uniform sampler2D shade_tex; uniform sampler2D emis_tex;
uniform vec4 base; uniform vec3 shade_col; uniform vec3 emis_col;
uniform float cutoff; uniform float blended;
uniform vec2 edges;                 // light/shade ramp, in dot(normal, key light)
uniform vec3 key_dir; uniform vec3 fill_dir; uniform vec3 light_i; // ambient, key, fill intensity
uniform float exposure;
uniform float outline_pass; uniform vec3 outline_col; uniform float outline_mix;
in vec3 v_nrm; in vec2 v_uv; out vec4 frag;
vec3 to_lin(vec3 c) { return mix(c / 12.92, pow((c + 0.055) / 1.055, vec3(2.4)), step(0.04045, c)); }
vec3 to_srgb(vec3 c) {
    c = clamp(c, 0.0, 1.0);
    return mix(c * 12.92, 1.055 * pow(c, vec3(1.0 / 2.4)) - 0.055, step(0.0031308, c));
}
void main() {
    vec4 t = texture(tex, v_uv);
    float alpha = t.a * base.a;
    if (alpha < cutoff) discard;
    vec3 n = normalize(v_nrm);
    float key_cos = dot(n, key_dir);
    float light = (light_i.x + light_i.y * max(key_cos, 0.0) + light_i.z * max(dot(n, fill_dir), 0.0)) * exposure;
    if (outline_pass > 0.5) {
        frag = vec4(to_srgb(to_lin(outline_col) * mix(1.0, light, outline_mix)), 1.0);
        return;
    }
    vec3 lit = to_lin(t.rgb) * to_lin(base.rgb);
    vec3 shade = to_lin(shade_col) * to_lin(texture(shade_tex, v_uv).rgb);
    float ramp = clamp((key_cos - edges.x) / max(edges.y - edges.x, 1e-5), 0.0, 1.0);
    vec3 col = mix(shade, lit, ramp) * light + to_lin(emis_col) * to_lin(texture(emis_tex, v_uv).rgb);
    frag = vec4(to_srgb(col), blended > 0.5 ? alpha : 1.0);
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

    def __init__(
        self, model: VrmModel, width: int, height: int, exposure: float = 1.0
    ) -> None:
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
        # A VRM 0.x model faces -Z and three-vrm turns it to face +Z; here the model stays
        # put, so the lights (like the camera) are turned half a circle instead.
        facing = -1.0 if model.faces_minus_z else 1.0
        for name, pos in (("key_dir", _KEY_POS), ("fill_dir", _FILL_POS)):
            vec = np.array([pos[0] * facing, pos[1], pos[2] * facing])
            self._prog[name].value = tuple(float(x) for x in vec / np.linalg.norm(vec))
        self._prog["light_i"].value = (_AMBIENT, _KEY, _FILL)
        self._prog["exposure"].value = float(exposure)
        self._prog["tex"].value = 0
        self._prog["shade_tex"].value = 1
        self._prog["emis_tex"].value = 2
        self._prog["outline_w"].value = 0.0
        self._prog["outline_pass"].value = 0.0

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
        """The WebUI's close-up "webcam" camera (``fitCloseupFraming``)."""
        side = -1.0 if self.model.faces_minus_z else 1.0
        up, toward = _CLOSEUP_DIR
        norm = float(np.hypot(up, toward))
        dist = _CLOSEUP_DISTANCE * max(1.0, 0.8 / max(aspect, 0.1))
        target = np.array([0.0, _CLOSEUP_TARGET_Y, 0.0], dtype=np.float32)
        eye = target + np.array(
            [0.0, up / norm * dist, side * toward / norm * dist], dtype=np.float32
        )
        return (
            _perspective(_FOV_DEG, aspect, 0.1, 20.0) @ _look_at(eye, target)
        ).astype(np.float32)

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
        # Outlines: the model's back faces, pushed out along their normals. Drawn after the
        # opaque colour pass so the colour always wins where the two overlap.
        self._fbo.depth_mask = True
        self._ctx.enable(self._gl.CULL_FACE)
        self._ctx.cull_face = "front"
        for draw in self._draws:
            if draw.outline_width > 0.0 and not draw.blend:
                draw.draw_outline()
        self._ctx.disable(self._gl.CULL_FACE)

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
        material = prim.material
        self.texture = renderer.texture(material.texture)
        # No shade / emission texture: a white pixel, so the colour factor alone applies.
        self.shade_texture = renderer.texture(material.shade_texture)
        self.emission_texture = renderer.texture(material.emission_texture)
        self.outline_width = material.outline_width
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
        self._joints_bytes = b""

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
        self._joints_bytes = joints.transpose(0, 2, 1).tobytes()
        prog["joints"].write(self._joints_bytes)
        material = prim.material
        prog["base"].value = tuple(material.base_color)
        prog["shade_col"].value = tuple(material.shade_color)
        prog["emis_col"].value = tuple(material.emission_color)
        prog["edges"].value = tuple(material.shade_edges)
        prog["cutoff"].value = self.cutoff
        prog["blended"].value = 1.0 if self.blend else 0.0
        prog["outline_pass"].value = 0.0
        prog["outline_w"].value = 0.0
        self.texture.use(0)
        self.shade_texture.use(1)
        self.emission_texture.use(2)
        self.vao.render()

    def draw_outline(self) -> None:
        """The outline hull of the primitive just drawn (same skin and morph state)."""
        prog, material = self.r._prog, self.prim.material
        prog["joints"].write(self._joints_bytes)  # the program's uniform is shared
        prog["outline_pass"].value = 1.0
        prog["outline_w"].value = float(self.outline_width)
        prog["outline_col"].value = tuple(material.outline_color)
        prog["outline_mix"].value = float(material.outline_lighting_mix)
        self.texture.use(0)
        self.vao.render()
        prog["outline_pass"].value = 0.0
        prog["outline_w"].value = 0.0
