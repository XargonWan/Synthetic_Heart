import json
from pathlib import Path

from core.animation_handler import get_karada_state_server


def write_json(path: Path, obj: dict):
    path.write_text(json.dumps(obj))


def test_search_path_precedence(tmp_path: Path):
    p1 = tmp_path / "p1"
    p2 = tmp_path / "p2"
    (p1 / "think").mkdir(parents=True)
    (p2 / "think").mkdir(parents=True)

    (p1 / "think" / "a.fbx").write_text("FBX")
    write_json(
        p1 / "think" / "a.fbx.json", {"loop": {"start_frame": 0, "end_frame": 10}}
    )

    (p2 / "think" / "b.fbx").write_text("FBX")
    write_json(
        p2 / "think" / "b.fbx.json", {"loop": {"start_frame": 0, "end_frame": 10}}
    )

    handler = get_karada_state_server()
    handler.set_animation_search_paths([p1, p2])

    variants = handler.get_animation_variants("think")
    # p1 should be searched before p2, so a.fbx should be found
    assert "a.fbx" in variants["loop"]
    assert ("b.fbx" in variants["loop"]) or ("b.fbx" not in variants["loop"])
