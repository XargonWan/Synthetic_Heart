import json
import pytest
from pathlib import Path

from core.animation_handler import get_animation_handler, AnimationState


@pytest.fixture(autouse=True)
def reset_handler():
    # Ensure a fresh handler for each test
    handler = get_animation_handler()
    handler._registered_state_animations.clear()
    handler._state_aliases.clear()
    handler._search_paths = []
    handler._sequential_states = {AnimationState.IDLE.value}
    yield


def write_json(path: Path, obj: dict):
    path.write_text(json.dumps(obj))


def test_variants_discovery(tmp_path: Path):
    # Create a fake animations structure
    base = tmp_path / "custom_anim"
    think_dir = base / "think"
    think_dir.mkdir(parents=True)

    # thinking.fbx + descriptor with loop
    (think_dir / "thinking.fbx").write_text("FBX_PLACEHOLDER")
    write_json(think_dir / "thinking.fbx.json", {"loop": {"start_frame": 10, "end_frame": 50}})

    # thinking_post.fbx marked with play_once
    (think_dir / "thinking_post.fbx").write_text("FBX_PLACEHOLDER")
    write_json(think_dir / "thinking_post.fbx.json", {"play_once": True})

    handler = get_animation_handler()
    # set custom search path (this should be checked before Rei fallback)
    handler.set_animation_search_paths([base])

    variants = handler.get_animation_variants('think')
    assert 'thinking.fbx' in variants['loop']
    assert 'thinking_post.fbx' in variants['post']


def test_exact_file_match_and_aliases(tmp_path: Path):
    base = tmp_path / "custom_root"
    base.mkdir()
    # exact file match eat.fbx in root
    (base / "eat.fbx").write_text("FBX_PLACEHOLDER")
    write_json(base / "eat.fbx.json", {"loop": {"start_frame": 0, "end_frame": 30}})

    handler = get_animation_handler()
    handler.set_animation_search_paths([base])

    variants = handler.get_animation_variants('eat')
    assert 'eat.fbx' in variants['loop']

    # register alias: chow -> eat
    handler.register_state_aliases({'chow': ['eat']})
    variants_alias = handler.get_animation_variants('chow')
    assert 'eat.fbx' in variants_alias['loop']


def test_register_state_animations_override():
    handler = get_animation_handler()
    handler.register_state_animations('think', {'loop': ['override_loop.fbx'], 'post': ['override_post.fbx']})
    variants = handler.get_animation_variants('think')
    assert variants['loop'] == ['override_loop.fbx']
    assert variants['post'] == ['override_post.fbx']
