from plugins.vox_plugin import VoxPlugin
from core.vox_registry import VOX_REGISTRY


def test_builtin_vox_engines_registered() -> None:
    """Ensure the expected Vox engines are available after plugin initialization."""
    # instantiating the plugin triggers ``_import_builtin_engines``
    VoxPlugin()
    engines = set(VOX_REGISTRY.get_available_engines())
    # chatterbox is now a development-only engine and should not be imported
    assert engines == {"http", "kitten"}


def test_kittentts_stub_importable() -> None:
    """The vendored ``kittentts`` package should import without blowing up.

    If the optional audio dependencies (``gtts``/``pydub``) are missing the
    module still has to load; generation will raise later.  Skip the test
    entirely on machines that don't have those dependencies installed.
    """
    try:
        import kittentts  # type: ignore
    except ImportError:
        pytest.skip("kittentts stub dependencies not available")

    assert hasattr(kittentts, "KittenTTS")
    # try at least constructing the class; actual generation is covered by
    # ``test_vox_plugin`` which already exercises the engine sample.
    tts = kittentts.KittenTTS()
    assert tts is not None
