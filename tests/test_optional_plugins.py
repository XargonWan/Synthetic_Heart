"""Core must not hard-depend on the Vox plugin."""

import builtins
from typing import Any

import pytest

from core import optional_plugins


def test_missing_vox_plugin_degrades_to_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_import = builtins.__import__

    def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "plugins.vox_plugin":
            raise ImportError("vox removed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    assert optional_plugins.is_vox_enabled() is False
