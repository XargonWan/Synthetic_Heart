"""The play_once/intro-outro conflict is reported once per animation file."""

import pytest

from core import animation_handler
from core.animation_handler import KaradaStateServer


def test_conflicting_play_once_warns_once(monkeypatch: pytest.MonkeyPatch) -> None:
    messages: list[str] = []
    monkeypatch.setattr(animation_handler, "log_warning", messages.append)
    monkeypatch.setattr(animation_handler, "_PLAY_ONCE_WARNED", set())
    ah = KaradaStateServer()
    descriptor = {
        "play_once": True,
        "intro": {"start_frame": 0, "end_frame": 5},
    }
    for _ in range(5):
        ah._analyze_animation_structure(descriptor, "Look Around.fbx")
    ah._analyze_animation_structure(descriptor, "Other.fbx")
    assert sum("Look Around.fbx" in m for m in messages) == 1
    assert sum("Other.fbx" in m for m in messages) == 1
