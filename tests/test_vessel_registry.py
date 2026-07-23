"""Tests for the Rift Vessel connector registry (no DB, no LLM)."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from core.vessel_registry import (
    VesselRegistry,
    register_vessel_connector,
    VESSEL_REGISTRY,
)


def test_register_and_query_connector() -> None:
    reg = VesselRegistry()
    reg.register_connector(
        "minecraft",
        "plugins.vessels.minecraft_connector",
        capabilities={"movement": True, "chat": True},
        label="Minecraft (PoC)",
    )
    assert "minecraft" in reg.get_available_connectors()
    meta = reg.get_connector_meta("minecraft")
    assert meta["label"] == "Minecraft (PoC)"
    assert meta["capabilities"]["movement"] is True


def test_unknown_connector_meta_is_empty() -> None:
    reg = VesselRegistry()
    assert reg.get_connector_meta("does_not_exist") == {}


def test_find_connector_by_capabilities() -> None:
    reg = VesselRegistry()
    reg.register_connector("a", "mod.a", capabilities={"movement": True})
    reg.register_connector("b", "mod.b", capabilities={"chat": True})
    assert reg.find_connector_by_capabilities({"chat": True}) == "b"
    assert reg.find_connector_by_capabilities({"perception": True}) is None


def test_load_connector_requires_connector_class(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reg = VesselRegistry()
    reg.register_connector("bad", "tests._fake_no_class_mod")

    fake = types.ModuleType("tests._fake_no_class_mod")
    sys.modules["tests._fake_no_class_mod"] = fake
    try:
        with pytest.raises(ValueError):
            reg.load_connector("bad")
    finally:
        sys.modules.pop("tests._fake_no_class_mod", None)


def test_load_connector_instantiates_class(monkeypatch: pytest.MonkeyPatch) -> None:
    reg = VesselRegistry()
    reg.register_connector("good", "tests._fake_good_mod")

    class _Conn:
        pass

    fake = types.ModuleType("tests._fake_good_mod")
    fake.CONNECTOR_CLASS = _Conn  # type: ignore[attr-defined]
    sys.modules["tests._fake_good_mod"] = fake
    try:
        inst = reg.load_connector("good")
        assert isinstance(inst, _Conn)
        # Cached: second load returns the same instance.
        assert reg.load_connector("good") is inst
        assert reg.get_instance("good") is inst
    finally:
        sys.modules.pop("tests._fake_good_mod", None)


def test_register_instance_and_unregister() -> None:
    reg = VesselRegistry()
    sentinel: Any = object()
    reg.register_instance("direct", sentinel, label="Direct")
    assert reg.get_instance("direct") is sentinel
    assert "direct" in reg.get_available_connectors()
    reg.unregister_connector("direct")
    assert reg.get_instance("direct") is None
    assert "direct" not in reg.get_available_connectors()


def test_unload_connector_forces_reload() -> None:
    reg = VesselRegistry()
    reg.register_instance("x", object())
    assert reg.get_instance("x") is not None
    reg.unload_connector("x")
    assert reg.get_instance("x") is None
    # Still registered (module path retained), just no live instance.
    assert "x" in reg.get_available_connectors()


def test_helper_registers_on_singleton() -> None:
    register_vessel_connector("_test_helper", "mod.helper", label="Helper")
    try:
        assert "_test_helper" in VESSEL_REGISTRY.get_available_connectors()
    finally:
        VESSEL_REGISTRY.unregister_connector("_test_helper")
