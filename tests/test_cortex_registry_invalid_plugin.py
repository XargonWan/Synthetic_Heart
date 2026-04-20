from core.cortex_registry import get_cortex_registry
import types
import importlib


def test_plugin_with_none_plugin_class(monkeypatch):
    reg = get_cortex_registry()

    fake_mod = types.SimpleNamespace(PLUGIN_CLASS=None)

    real_import = importlib.import_module

    def fake_import(name):
        # Accept both new and legacy import paths
        if name in (
            "cortex.llm_provider.invalid_none",
            "cortex.llm_engine.invalid_none",
        ):
            return fake_mod
        return real_import(name)

    monkeypatch.setattr(importlib, "import_module", fake_import)

    reg.register_engine_module(
        "invalid_none",
        "cortex.llm_provider.invalid_none",
        cortex="llm_provider",
    )

    try:
        reg.load_engine("invalid_none")
        assert False, "Expected ValueError for invalid PLUGIN_CLASS"
    except ValueError as e:
        assert "PLUGIN_CLASS" in str(e) and "None" in str(e)
