"""The shipped Rei animations for the posture / activity states (sit, crouch, gaming), the think loop,
and the walk a world can ask for (Rei's and Zero's own)."""

import json
from pathlib import Path

import pytest

from core.animation_handler import (
    ANIMATION_STATE_PRIORITIES,
    AnimationState,
    get_karada_state_server,
)
from plugins.rift_vessel.embodiment_assets import SKINS_ROOT, resolve_animation

REI_ANIMS = SKINS_ROOT / "Rei" / "animations"


@pytest.fixture
def handler():
    h = get_karada_state_server()
    h._registered_state_animations.clear()
    h._state_aliases.clear()
    h._search_paths = []
    return h


def _descriptor(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_posture_states_exist_and_give_way_to_talking_and_thinking():
    for name in ("sit", "crouch", "gaming"):
        state = AnimationState(name)
        assert state.value == name
    p = ANIMATION_STATE_PRIORITIES
    assert 0 < p[AnimationState.SIT] < p[AnimationState.WRITE]
    assert 0 < p[AnimationState.CROUCH] < p[AnimationState.WRITE]
    assert p[AnimationState.GAMING] < p[AnimationState.WRITE] < p[AnimationState.TALK] < p[AnimationState.THINK]


def test_sit_rotation_is_the_chair_clip_not_the_bed_one(handler):
    variants = handler.get_animation_variants("sit")
    assert variants["loop"] == ["Sitting Idle.fbx"]
    assert not [v for v in variants["post"] + variants["other"] if "bed" in v.lower()]
    # kept for later, on its own: a bed is a different thing to sit on
    assert (REI_ANIMS / "sit" / "bed" / "sit-bed-swing-legs.fbx").is_file()


def test_crouch_loops_and_stands_up_once(handler):
    variants = handler.get_animation_variants("crouch")
    assert variants["loop"] == ["Crouching Idle.fbx"]
    assert variants["post"] == ["Crouch To Stand.fbx"]


def test_gaming_moves_the_upper_body_only(handler):
    variants = handler.get_animation_variants("gaming")
    assert variants["loop"] == ["Gaming-sitting.fbx"]
    assert _descriptor(REI_ANIMS / "gaming" / "Gaming-sitting.fbx.json")["mask"] == "upper_body"


def test_the_manifest_offers_the_new_states(handler):
    states = {e.get("state") for e in handler.get_animation_manifest()["animations"].values()}
    assert {"sit", "crouch", "gaming"} <= states


def test_thinking_has_a_loop_that_closes_on_itself():
    """intro -> loop -> outro are one run of frames, and the loop is made to repeat without a step.

    A held pose DRIFTS (Thinking has no cycle in it): frames 43..73 end 4 degrees on the head and 3 on the arm away
    from where they began, so a plain repeat steps every second (the mean over the bones said 0.7 and hid it; no
    window of the clip does better than 2 degrees on its worst bone). So the descriptor asks for a seamless loop
    (`blend_frames`): the client blends the window's end toward the frames BEFORE its start. That blend must come
    from the part where the pose is still (the arrival, frames before ~46, moves the arm 20 degrees): measured on
    the clip with the RetroXR mod's `tests/loop_scan.gd`, the window 54..74 blended over 16 frames has the smallest
    step per frame (0.37 deg at 30 fps; 43..73 over 12 was 2.8) and strays 4.3 degrees from the clip at most."""
    d = _descriptor(REI_ANIMS / "think" / "Thinking.fbx.json")
    intro, loop, outro = d["intro"], d["loop"], d["outro"]
    assert intro["start_frame"] == 0
    assert intro["end_frame"] == loop["start_frame"]
    assert loop["end_frame"] == outro["start_frame"]
    assert (loop["start_frame"], loop["end_frame"]) == (54, 74)
    blend = loop["blend_frames"]
    assert blend >= 12, "a plain cut steps by the drift"
    assert blend <= loop["start_frame"], "the frames before the window's start must exist"
    assert blend <= loop["end_frame"] - loop["start_frame"]
    assert loop["start_frame"] - blend >= 36, "the blend must not reach back into the arrival"
    assert outro["end_frame"] <= 127  # the clip has 128 frames


def test_each_skin_walks_with_its_own_clip():
    rei = resolve_animation("walk", "Rei")
    assert rei["namespace"] == "state"
    assert [c["file"] for c in rei["clips"]] == ["Walking.fbx"]
    zero = resolve_animation("walk", "Zero")
    assert zero["skin"] == "Zero", "Zero's own walk, not Rei's"
    assert zero["clips"][0]["url"] == "/skins/Zero/animations/walk/Walking.fbx"
