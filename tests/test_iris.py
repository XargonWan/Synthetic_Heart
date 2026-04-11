"""Tests for Iris vision registry and plugin."""

from __future__ import annotations

import types
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# IrisRegistry unit tests
# ---------------------------------------------------------------------------


def test_registry_register_and_list() -> None:
    from core.iris_registry import IrisRegistry

    reg = IrisRegistry()
    reg.register_engine("test", "some.module.path", {"vision": True}, "Test engine")
    assert "test" in reg.get_available_engines()


def test_registry_unregister() -> None:
    from core.iris_registry import IrisRegistry

    reg = IrisRegistry()
    reg.register_engine("e1", "mod", {}, "E1")
    reg.unregister_engine("e1")
    assert "e1" not in reg.get_available_engines()


def test_registry_unknown_engine_raises() -> None:
    from core.iris_registry import IrisRegistry

    reg = IrisRegistry()
    with pytest.raises(ValueError, match="Unknown engine"):
        reg.load_engine("nonexistent")


def test_registry_find_by_capabilities() -> None:
    from core.iris_registry import IrisRegistry

    reg = IrisRegistry()
    reg.register_engine("local_vision", "mod", {"vision": True, "local": True}, "")
    reg.register_engine("cloud_vision", "mod2", {"vision": True, "local": False}, "")

    result = reg.find_engine_by_capabilities({"local": True})
    assert result == "local_vision"

    result2 = reg.find_engine_by_capabilities({"vision": True})
    assert result2 == "local_vision"  # first match


def test_registry_load_engine_missing_engine_class() -> None:
    from core.iris_registry import IrisRegistry

    dummy_mod = types.ModuleType("fake_iris_engine")

    reg = IrisRegistry()
    reg._engine_modules["bad"] = "fake_iris_engine"

    with patch("importlib.import_module", return_value=dummy_mod):
        with pytest.raises(ValueError, match="ENGINE_CLASS"):
            reg.load_engine("bad")


def test_registry_load_engine_caches_instance() -> None:
    from core.iris_registry import IrisRegistry
    from plugins.iris_base import IrisEngineBase, IrisResult

    class FakeEngine(IrisEngineBase):
        def describe_image(
            self,
            file_path: str,
            mime_type: str | None = None,
            prompt: str | None = None,
        ) -> IrisResult | None:
            return IrisResult(description="a cat", language="en")

    dummy_mod = types.ModuleType("fake_m")
    dummy_mod.ENGINE_CLASS = FakeEngine  # type: ignore[attr-defined]

    reg = IrisRegistry()
    reg._engine_modules["fake"] = "fake_m"
    with patch("importlib.import_module", return_value=dummy_mod):
        inst1 = reg.load_engine("fake")
        inst2 = reg.load_engine("fake")
    assert inst1 is inst2


def test_registry_register_instance() -> None:
    from core.iris_registry import IrisRegistry
    from plugins.iris_base import IrisEngineBase, IrisResult

    class DirectEngine(IrisEngineBase):
        def describe_image(
            self,
            file_path: str,
            mime_type: str | None = None,
            prompt: str | None = None,
        ) -> IrisResult | None:
            return IrisResult(description="direct", language=None)

    reg = IrisRegistry()
    instance = DirectEngine()
    reg.register_instance("direct", instance, label="Direct test engine")
    loaded = reg.load_engine("direct")
    assert loaded is instance
    assert reg.get_engine_meta("direct")["label"] == "Direct test engine"


# ---------------------------------------------------------------------------
# IrisPlugin integration-style tests (mocked engine)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_iris_plugin_disabled_returns_none() -> None:
    """When ACTIVE_IRIS_ENGINE is 'disabled' the plugin returns None."""
    with patch("core.core_initializer.register_plugin"):
        from plugins.iris_plugin import IrisPlugin

        plugin = IrisPlugin.__new__(IrisPlugin)
        plugin._active_engine_name = "disabled"
        plugin._engine_settings = {}
        plugin._default_prompt = "Describe this image."

        result = await plugin.describe_media("/tmp/fake.jpg")
        assert result is None


@pytest.mark.asyncio
async def test_iris_plugin_file_not_found_returns_none() -> None:
    """When the file does not exist the plugin returns None."""
    with patch("core.core_initializer.register_plugin"):
        from plugins.iris_plugin import IrisPlugin

        plugin = IrisPlugin.__new__(IrisPlugin)
        plugin._active_engine_name = "myengine"
        plugin._engine_settings = {}
        plugin._default_prompt = "Describe this image."

        result = await plugin.describe_media("/tmp/does_not_exist_xyz.jpg")
        assert result is None


@pytest.mark.asyncio
async def test_iris_plugin_calls_engine(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Plugin calls the engine and returns IrisResult."""

    from plugins.iris_base import IrisEngineBase, IrisResult

    class MockEngine(IrisEngineBase):
        def describe_image(
            self,
            file_path: str,
            mime_type: str | None = None,
            prompt: str | None = None,
        ) -> IrisResult | None:
            return IrisResult(description="a sunny beach", language="en")

    test_file = tmp_path / "test.jpg"
    test_file.write_bytes(b"\xff\xd8\xff")  # minimal JPEG header

    with patch("core.core_initializer.register_plugin"):
        from plugins.iris_plugin import IrisPlugin
        from core.iris_registry import IrisRegistry

        reg = IrisRegistry()
        reg.register_instance("mock", MockEngine(), label="Mock")

        plugin = IrisPlugin.__new__(IrisPlugin)
        plugin._active_engine_name = "mock"
        plugin._engine_settings = {}
        plugin._default_prompt = "Describe this image."

        with (
            patch("plugins.iris_plugin.IRIS_REGISTRY", reg),
            patch.object(plugin, "refresh_config"),
        ):
            result = await plugin.describe_media(str(test_file), "image/jpeg")

    assert result is not None
    assert result.description == "a sunny beach"
    assert result.language == "en"


# ---------------------------------------------------------------------------
# IrisResult dataclass
# ---------------------------------------------------------------------------


def test_iris_result_defaults() -> None:
    from plugins.iris_base import IrisResult

    r = IrisResult(description="hello")
    assert r.description == "hello"
    assert r.language is None
    assert r.confidence is None


def test_iris_result_full() -> None:
    from plugins.iris_base import IrisResult

    r = IrisResult(description="a cat", language="en", confidence=0.95)
    assert r.language == "en"
    assert r.confidence == 0.95


# ---------------------------------------------------------------------------
# IrisPlugin action handler
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_handle_custom_action_vision_describe(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from plugins.iris_base import IrisEngineBase, IrisResult

    class MockEngine(IrisEngineBase):
        def describe_image(
            self,
            file_path: str,
            mime_type: str | None = None,
            prompt: str | None = None,
        ) -> IrisResult | None:
            return IrisResult(description="mountains", language="en", confidence=0.9)

    test_file = tmp_path / "img.png"
    test_file.write_bytes(b"\x89PNG")

    with patch("core.core_initializer.register_plugin"):
        from plugins.iris_plugin import IrisPlugin
        from core.iris_registry import IrisRegistry

        reg = IrisRegistry()
        reg.register_instance("mock", MockEngine(), label="Mock")

        plugin = IrisPlugin.__new__(IrisPlugin)
        plugin._active_engine_name = "mock"
        plugin._engine_settings = {}
        plugin._default_prompt = "Describe."

        with (
            patch("plugins.iris_plugin.IRIS_REGISTRY", reg),
            patch.object(plugin, "refresh_config"),
        ):
            response = await plugin.handle_custom_action(
                "vision_describe",
                {"image_path": str(test_file), "mime_type": "image/png"},
            )

    assert response["status"] == "success"
    assert response["description"] == "mountains"
    assert response["language"] == "en"
    assert response["confidence"] == 0.9


@pytest.mark.asyncio
async def test_handle_custom_action_unknown() -> None:
    with patch("core.core_initializer.register_plugin"):
        from plugins.iris_plugin import IrisPlugin

        plugin = IrisPlugin.__new__(IrisPlugin)
        plugin._active_engine_name = "disabled"
        plugin._engine_settings = {}
        plugin._default_prompt = "Describe."

        response = await plugin.handle_custom_action("unknown_action", {})

    assert response["status"] == "error"
