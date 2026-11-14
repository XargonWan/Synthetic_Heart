"""Tests for the headless avatar renderer (``core.avatar_render``).

The model is a tiny VRM 0.x built here: one head bone, one skinned quad with a morph target
bound to the ``a`` (mouth) expression. The GL tests are skipped where no GL context exists.
"""

from __future__ import annotations

import asyncio
import json
import random
import struct
import wave
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest

from core.avatar_render import animator as an
from core.avatar_render import source as src
from core.avatar_render.renderer import (
    BACKGROUND_RGB,
    AvatarPose,
    RendererUnavailable,
    create_context,
    rgb_to_i420,
)
from core.avatar_render.vrm import VrmError, parse_vrm


def _pad(data: bytes, fill: bytes = b"\x00") -> bytes:
    return data + fill * (-len(data) % 4)


def build_vrm(vrm1: bool = False) -> bytes:
    """A GLB: root -> head bone, a skinned quad at head height, one mouth morph target."""
    cx, cy, half = 0.0, 1.5, 0.25
    positions = np.array(
        [
            [cx - half, cy - half, 0],
            [cx + half, cy - half, 0],
            [cx + half, cy + half, 0],
            [cx - half, cy + half, 0],
        ],
        dtype=np.float32,
    )
    normals = np.tile(np.array([0, 0, 1], np.float32), (4, 1))
    uvs = np.array([[0, 1], [1, 1], [1, 0], [0, 0]], dtype=np.float32)
    joints = np.zeros((4, 4), dtype=np.uint8)
    weights = np.tile(np.array([1, 0, 0, 0], np.float32), (4, 1))
    indices = np.array([0, 1, 2, 0, 2, 3], dtype=np.uint16)
    morph = np.tile(
        np.array([0.3, 0, 0], np.float32), (4, 1)
    )  # the "mouth" slides the quad
    ibm = np.eye(4, dtype=np.float32)
    ibm[1, 3] = -cy  # inverse of the head's world translation (column-major below)

    chunks: list[tuple[bytes, int | None]] = [
        (positions.tobytes(), 34962),
        (normals.tobytes(), 34962),
        (uvs.tobytes(), 34962),
        (joints.tobytes(), 34962),
        (weights.tobytes(), 34962),
        (indices.tobytes(), 34963),
        (morph.tobytes(), 34962),
        (ibm.T.astype(np.float32).tobytes(), None),
    ]
    blob, views, offset = b"", [], 0
    for data, target in chunks:
        padded = _pad(data)
        view: dict[str, Any] = {
            "buffer": 0,
            "byteOffset": offset,
            "byteLength": len(data),
        }
        if target:
            view["target"] = target
        views.append(view)
        blob += padded
        offset += len(padded)

    accessors = [
        {"bufferView": 0, "componentType": 5126, "count": 4, "type": "VEC3"},
        {"bufferView": 1, "componentType": 5126, "count": 4, "type": "VEC3"},
        {"bufferView": 2, "componentType": 5126, "count": 4, "type": "VEC2"},
        {"bufferView": 3, "componentType": 5121, "count": 4, "type": "VEC4"},
        {"bufferView": 4, "componentType": 5126, "count": 4, "type": "VEC4"},
        {"bufferView": 5, "componentType": 5123, "count": 6, "type": "SCALAR"},
        {"bufferView": 6, "componentType": 5126, "count": 4, "type": "VEC3"},
        {"bufferView": 7, "componentType": 5126, "count": 1, "type": "MAT4"},
    ]
    gltf: dict[str, Any] = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0, 2]}],
        "nodes": [
            {"name": "root", "children": [1]},
            {"name": "head", "translation": [0, cy, 0]},
            {"name": "mesh", "mesh": 0, "skin": 0},
        ],
        "meshes": [
            {
                "primitives": [
                    {
                        "attributes": {
                            "POSITION": 0,
                            "NORMAL": 1,
                            "TEXCOORD_0": 2,
                            "JOINTS_0": 3,
                            "WEIGHTS_0": 4,
                        },
                        "indices": 5,
                        "targets": [{"POSITION": 6}],
                        "material": 0,
                    }
                ]
            }
        ],
        "skins": [{"joints": [1], "inverseBindMatrices": 7}],
        "materials": [
            {
                "pbrMetallicRoughness": {"baseColorFactor": [1.0, 0.2, 0.2, 1.0]},
                "doubleSided": True,
            }
        ],
        "accessors": accessors,
        "bufferViews": views,
        "buffers": [{"byteLength": len(blob)}],
    }
    if vrm1:
        gltf["extensions"] = {
            "VRMC_vrm": {
                "humanoid": {"humanBones": {"head": {"node": 1}}},
                "expressions": {
                    "preset": {
                        "aa": {
                            "morphTargetBinds": [{"node": 2, "index": 0, "weight": 1.0}]
                        }
                    }
                },
            }
        }
    else:
        gltf["extensions"] = {
            "VRM": {
                "humanoid": {"humanBones": [{"bone": "head", "node": 1}]},
                "blendShapeMaster": {
                    "blendShapeGroups": [
                        {
                            "presetName": "a",
                            "binds": [{"mesh": 0, "index": 0, "weight": 100}],
                        }
                    ]
                },
            }
        }
    js = _pad(json.dumps(gltf).encode(), b" ")
    total = 12 + 8 + len(js) + 8 + len(blob)
    return (
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<I4s", len(js), b"JSON")
        + js
        + struct.pack("<I4s", len(blob), b"BIN\x00")
        + blob
    )


# -- the loader -------------------------------------------------------------------


def test_a_vrm0_model_is_read() -> None:
    model = parse_vrm(build_vrm())
    assert model.faces_minus_z is True
    assert model.humanoid == {"head": 1}
    # VRM 0.x "a" is exposed under the VRM 1.0 name "aa", weight 100 -> 1.0.
    assert model.expressions == {"aa": [(0, 0, 1.0)]}
    prim = model.primitives[0]
    assert prim.skin == 0 and prim.indices.tolist() == [0, 1, 2, 0, 2, 3]
    assert len(prim.targets) == 1 and prim.positions.shape == (4, 3)
    assert model.skins[0].joints == [1]
    worlds = model.world_matrices()
    assert worlds[1][1, 3] == pytest.approx(1.5)  # the head bone's height


def test_a_vrm1_model_faces_plus_z_and_binds_by_node() -> None:
    model = parse_vrm(build_vrm(vrm1=True))
    assert model.faces_minus_z is False
    assert model.expressions == {"aa": [(0, 0, 1.0)]}  # node 2 -> glTF mesh 0


@pytest.mark.parametrize(
    "data", [b"", b"not a glb at all, just text........", b"glTF" + b"\x00" * 30]
)
def test_garbage_is_refused(data: bytes) -> None:
    with pytest.raises(VrmError):
        parse_vrm(data)


def test_a_glb_without_the_vrm_extension_is_refused() -> None:
    glb = build_vrm()
    start = glb.index(b'"VRM"')
    broken = glb[:start] + b'"VRX"' + glb[start + 5 :]
    with pytest.raises(VrmError):
        parse_vrm(broken)


# -- the animator ---------------------------------------------------------------------


def test_blinking_closes_and_reopens_the_eyes_now_and_then() -> None:
    animator = an.AvatarAnimator(random.Random(1))
    levels = [
        animator.pose(i * 0.04).expressions.get("blink", 0.0) for i in range(400)
    ]  # 16 s
    assert max(levels) == pytest.approx(1.0, abs=0.2)
    assert levels[0] == 0.0
    closed = sum(1 for v in levels if v > 0.0)
    assert 8 <= closed < 100  # a few brief blinks, not a stuck eyelid


def test_the_mouth_follows_the_voice() -> None:
    animator = an.AvatarAnimator(random.Random(1))
    silent = animator.pose(1.0, speech_level=0.0).expressions
    loud = animator.pose(1.0, speech_level=1.0).expressions
    assert silent.get("aa", 0.0) == 0.0
    assert loud["aa"] == pytest.approx(0.9)


def test_an_idle_face_is_not_blank_and_an_emotion_replaces_that() -> None:
    animator = an.AvatarAnimator()
    assert animator.pose(1.0).expressions["relaxed"] == pytest.approx(0.12)
    sad = animator.pose(1.0, expressions={"sad": 0.6}).expressions
    assert sad["sad"] == pytest.approx(0.6) and "relaxed" not in sad
    assert set(animator.pose(1.0).bone_deltas) == {"head", "neck", "chest"}


def test_emotions_become_expressions_from_numbers_not_words() -> None:
    out = an.emotion_expressions(
        {"happy": 1.0, "sad": 0.2, "love": 1.0, "neutral": 0.9}
    )
    assert out == {"happy": pytest.approx(0.7), "sad": pytest.approx(0.14)}
    # A transient expression of the Karada server wins; VRM 0.x names are understood.
    override = {"type": "vrm_expression_set", "name": "joy", "intensity": 0.5}
    assert an.emotion_expressions({}, override) == {"happy": 0.5}
    targets: Any = {
        "type": "vrm_expression_set",
        "targets": {"angry": 1.0, "bogus": "x"},
    }
    assert an.emotion_expressions({}, targets) == {"angry": 1.0}
    bad = cast(Any, {"happy": "bad"})  # a garbled value is skipped, not fatal
    assert an.emotion_expressions(bad) == {}


def _wav(path: Path, samples: np.ndarray, rate: int = 16000) -> Path:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(samples.astype(np.int16).tobytes())
    return path


def test_speech_track_is_the_loudness_of_the_clip(tmp_path: Path) -> None:
    quiet = np.zeros(16000, dtype=np.float32)  # 1 s of silence, then 1 s of tone
    tone = 8000 * np.sin(np.linspace(0, 2 * np.pi * 200, 16000))
    track = an.SpeechTrack.from_wav(
        _wav(tmp_path / "v.wav", np.concatenate([quiet, tone])), started=100.0
    )
    assert track is not None
    assert track.level_at(100.5) == 0.0
    assert track.level_at(101.5) > 0.9
    assert track.level_at(99.0) == 0.0 and track.level_at(103.0) == 0.0
    assert not track.over(101.9) and track.over(102.1)


def test_an_unreadable_clip_gives_no_track(tmp_path: Path) -> None:
    assert an.SpeechTrack.from_wav(tmp_path / "missing.wav") is None
    (tmp_path / "bad.wav").write_bytes(b"RIFFnope")
    assert an.SpeechTrack.from_wav(tmp_path / "bad.wav") is None


# -- pixels -------------------------------------------------------------------------------


def test_rgb_to_i420_is_planar_limited_range() -> None:
    white = np.full((4, 6, 3), 255, dtype=np.uint8)
    out = rgb_to_i420(white)
    assert len(out) == 6 * 4 * 3 // 2
    assert out[0] == 235 and out[24] == 128 and out[24 + 6] == 128
    black = rgb_to_i420(np.zeros((4, 6, 3), dtype=np.uint8))
    assert black[0] == 16
    red = rgb_to_i420(np.tile(np.array([255, 0, 0], np.uint8), (4, 6, 1)))
    assert red[24 + 6] > 200  # V (Cr) is high for red


# -- the GL renderer -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def gl_available() -> None:
    try:
        create_context().release()
    except RendererUnavailable as exc:
        pytest.skip(f"no GL context here: {exc}")


def _luma(i420: bytes, w: int, h: int) -> np.ndarray:
    return np.frombuffer(i420[: w * h], dtype=np.uint8).reshape(h, w)


def test_the_renderer_draws_the_model(gl_available: None) -> None:
    from core.avatar_render.renderer import AvatarRenderer

    model = parse_vrm(build_vrm())
    renderer = AvatarRenderer(model, 64, 48)
    try:
        frame = renderer.render(AvatarPose())
        assert len(frame) == 64 * 48 * 3 // 2
        luma = _luma(frame, 64, 48)
        background_luma = rgb_to_i420(
            np.full((2, 2, 3), BACKGROUND_RGB, dtype=np.uint8)
        )[0]
        drawn = int((np.abs(luma.astype(int) - int(background_luma)) > 20).sum())
        assert drawn > 100  # the quad is on screen, in front of the dark background
        assert int(luma[0, 0]) == pytest.approx(int(background_luma), abs=3)

        # The "a" expression slides the quad: the picture changes, and goes back.
        moved = renderer.render(AvatarPose(expressions={"aa": 1.0}))
        assert not np.array_equal(_luma(moved, 64, 48), luma)
        again = renderer.render(AvatarPose())
        assert np.array_equal(_luma(again, 64, 48), luma)
    finally:
        renderer.close()


def test_the_renderer_wants_an_even_size(gl_available: None) -> None:
    from core.avatar_render.renderer import AvatarRenderer

    with pytest.raises(ValueError):
        AvatarRenderer(parse_vrm(build_vrm()), 63, 48)


def test_a_real_skin_renders_when_present(gl_available: None) -> None:
    from core.avatar_render.renderer import AvatarRenderer
    from core.avatar_render.vrm import load_vrm

    path = Path("skins/Rei/model.vrm")
    if not path.is_file():
        pytest.skip("the Rei skin is not in this checkout")
    renderer = AvatarRenderer(load_vrm(path), 128, 72)
    try:
        luma = _luma(renderer.render(AvatarPose(expressions={"aa": 0.5})), 128, 72)
        assert luma.std() > 10  # a picture, not a flat colour
    finally:
        renderer.close()


# -- the video source ----------------------------------------------------------------------------


class _FakeRenderer:
    fail = False

    def __init__(self, model: Any, width: int, height: int) -> None:
        self.gl_renderer = "fake-gl"
        self.poses: list[AvatarPose] = []
        self.closed = False

    def render(self, pose: AvatarPose) -> bytes:
        if _FakeRenderer.fail:
            raise RuntimeError("GL lost")
        self.poses.append(pose)
        return b"frame"

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_gl(monkeypatch: pytest.MonkeyPatch) -> type[_FakeRenderer]:
    _FakeRenderer.fail = False
    monkeypatch.setattr(src, "AvatarRenderer", _FakeRenderer)
    monkeypatch.setattr(src, "load_vrm", lambda path: object())
    return _FakeRenderer


@pytest.mark.asyncio
async def test_the_source_renders_frames_from_the_karada_state(
    fake_gl: type[_FakeRenderer], tmp_path: Path
) -> None:
    frames: list[bytes] = []
    state = ({"happy": 1.0}, None)
    source = src.AvatarVideoSource(
        Path("m.vrm"), 64, 48, 50, state_reader=lambda: state
    )
    await source.start(frames.append)
    try:
        for _ in range(100):
            if len(frames) >= 3:
                break
            await asyncio.sleep(0.02)
        assert len(frames) >= 3 and set(frames) == {b"frame"}
        renderer = source._renderer
        assert isinstance(renderer, _FakeRenderer)
        assert renderer.poses[0].expressions["happy"] == pytest.approx(0.7)

        # While a clip plays the mouth opens with it.
        tone = 8000 * np.sin(np.linspace(0, 2 * np.pi * 400, 16000))
        source.speak(_wav(tmp_path / "v.wav", tone))
        seen = len(renderer.poses)
        for _ in range(100):
            if any(p.expressions.get("aa", 0) > 0.3 for p in renderer.poses[seen:]):
                break
            await asyncio.sleep(0.02)
        assert any(p.expressions.get("aa", 0) > 0.3 for p in renderer.poses[seen:])
    finally:
        await source.stop()
    assert renderer.closed is True


@pytest.mark.asyncio
async def test_the_source_gives_up_after_repeated_failures(
    fake_gl: type[_FakeRenderer],
) -> None:
    failed: list[str] = []

    async def _on_failed() -> None:
        failed.append("failed")

    source = src.AvatarVideoSource(
        Path("m.vrm"), 64, 48, 100, state_reader=lambda: ({}, None)
    )
    fake_gl.fail = True
    await source.start(lambda frame: None, on_failed=_on_failed)
    for _ in range(100):
        if failed:
            break
        await asyncio.sleep(0.02)
    await source.stop()
    assert failed == ["failed"]


@pytest.mark.asyncio
async def test_a_source_that_cannot_start_raises_and_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _no_gl(model: Any, width: int, height: int) -> Any:
        raise RendererUnavailable("no EGL")

    monkeypatch.setattr(src, "AvatarRenderer", _no_gl)
    monkeypatch.setattr(src, "load_vrm", lambda path: object())
    source = src.AvatarVideoSource(Path("m.vrm"), 64, 48, 10)
    with pytest.raises(RendererUnavailable):
        await source.start(lambda frame: None)


def test_the_model_to_draw_is_the_configured_then_the_active_then_rei(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import core.animation_handler as ah

    configured = tmp_path / "mine.vrm"
    configured.write_bytes(b"x")
    active = tmp_path / "active.vrm"
    active.write_bytes(b"x")

    class _Server:
        def get_active_vrm_path(self) -> Path:
            return active

    monkeypatch.setattr(ah, "get_karada_state_server", lambda: _Server())
    assert src.resolve_model_path(str(configured)) == configured
    assert src.resolve_model_path("/nonexistent.vrm") == active
    assert src.resolve_model_path("") == active

    class _NoModel:
        def get_active_vrm_path(self) -> None:
            return None

    monkeypatch.setattr(ah, "get_karada_state_server", lambda: _NoModel())
    expected = next(
        (p for p in (src._DEFAULT_MODEL, src._SEED_MODEL) if p.is_file()), None
    )
    assert src.resolve_model_path("") == expected
