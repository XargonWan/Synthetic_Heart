
from plugins.vox_plugin import VoxPlugin
from core.vox_registry import VOX_REGISTRY


def test_builtin_vox_engines_registered() -> None:
    """Ensure the expected Vox engines are available after plugin initialization."""
    # instantiating the plugin triggers ``_import_builtin_engines``
    VoxPlugin()
    engines = set(VOX_REGISTRY.get_available_engines())
    assert engines == {"http", "chatterbox", "kitten"}
