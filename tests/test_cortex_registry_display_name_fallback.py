from core.cortex_registry import get_cortex_registry


def test_load_engine_with_missing_display_name(monkeypatch):
    reg = get_cortex_registry()

    # Create a fake module with PLUGIN_CLASS lacking display_name
    class FakePlugin:
        # intentionally no display_name
        def __init__(self):
            pass

    # Monkeypatch importlib to return a fake module object when imported
    import types

    fake_mod = types.SimpleNamespace(PLUGIN_CLASS=FakePlugin)

    import importlib

    real_import = importlib.import_module

    def fake_import(name):
        if name == "cortex.llm_engine.fake_missing_display":
            return fake_mod
        return real_import(name)

    monkeypatch.setattr(importlib, "import_module", fake_import)
    # Register and load
    reg.register_engine_module(
        "fake_missing_display", "cortex.llm_engine.fake_missing_display", cortex="llm"
    )
    inst = reg.load_engine("fake_missing_display")
    assert inst is not None
    # Display name should have been derived from module name
    # Validate registration meta contains label / or that no exception was raised
    assert "fake_missing_display" in reg._engines
