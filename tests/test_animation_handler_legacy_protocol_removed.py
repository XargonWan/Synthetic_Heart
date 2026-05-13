from core.animation_handler import KaradaStateServer


def test_legacy_animation_command_helper_removed() -> None:
    assert not hasattr(KaradaStateServer, "_send_animation_command")
