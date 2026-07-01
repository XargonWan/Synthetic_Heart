"""Tests for Iris vision registry and plugin."""

from __future__ import annotations

import base64
import types
from typing import Any, cast
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
            model: str | None = None,
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
            model: str | None = None,
        ) -> IrisResult | None:
            return IrisResult(description="direct", language=None)

    reg = IrisRegistry()
    instance = DirectEngine()
    reg.register_instance(
        "direct",
        instance,
        label="Direct test engine",
        capabilities={"vision": True},
    )
    loaded = reg.load_engine("direct")
    assert loaded is instance
    assert reg.get_engine_meta("direct")["label"] == "Direct test engine"
    assert reg.get_engine_meta("direct")["capabilities"] == {"vision": True}


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
        plugin._default_model = ""

        result = await plugin.describe_media("/tmp/fake.jpg")
        assert result is None


def test_iris_plugin_is_enabled_tracks_active_engine() -> None:
    from plugins.iris_plugin import IrisPlugin

    plugin = IrisPlugin.__new__(IrisPlugin)
    plugin._active_engine_name = "disabled"
    plugin._engine_settings = {}
    plugin._default_prompt = "Describe this image."
    plugin._default_model = ""

    with patch.object(
        plugin,
        "refresh_config",
        side_effect=lambda: setattr(plugin, "_active_engine_name", "disabled"),
    ):
        assert plugin.is_enabled() is False

    with patch.object(
        plugin,
        "refresh_config",
        side_effect=lambda: setattr(plugin, "_active_engine_name", "vision"),
    ):
        assert plugin.is_enabled() is True


@pytest.mark.asyncio
async def test_iris_plugin_inline_returns_none() -> None:
    """'inline' has no description engine, so describe_media returns None."""
    with patch("core.core_initializer.register_plugin"):
        from plugins.iris_plugin import IrisPlugin

        plugin = IrisPlugin.__new__(IrisPlugin)
        plugin._active_engine_name = "inline"
        plugin._engine_settings = {}
        plugin._default_prompt = "Describe this image."
        plugin._default_model = ""

        result = await plugin.describe_media("/tmp/fake.jpg")
        assert result is None


def test_iris_plugin_inline_is_not_enabled() -> None:
    """'inline' must not expose the vision_describe action (is_enabled False)."""
    from plugins.iris_plugin import IrisPlugin

    plugin = IrisPlugin.__new__(IrisPlugin)
    plugin._active_engine_name = "inline"
    plugin._engine_settings = {}
    plugin._default_prompt = "Describe this image."
    plugin._default_model = ""

    with patch.object(
        plugin,
        "refresh_config",
        side_effect=lambda: setattr(plugin, "_active_engine_name", "inline"),
    ):
        assert plugin.is_enabled() is False


def test_get_active_iris_engine_reflects_pseudo_engine() -> None:
    """_get_active_iris_engine reports the configured engine, including 'inline'."""
    import core.plugin_instance as pi

    class _FakeIris:
        def __init__(self, name: str) -> None:
            self._active_engine_name = name

        def refresh_config(self) -> None:  # no-op
            pass

    for engine_name in ("inline", "disabled", "selenium-llm-engine"):
        registry = {"iris_plugin": _FakeIris(engine_name)}
        with patch("core.core_initializer.PLUGIN_REGISTRY", registry):
            assert pi._get_active_iris_engine() == engine_name


@pytest.mark.asyncio
async def test_iris_plugin_file_not_found_returns_none() -> None:
    """When the file does not exist the plugin returns None."""
    with patch("core.core_initializer.register_plugin"):
        from plugins.iris_plugin import IrisPlugin

        plugin = IrisPlugin.__new__(IrisPlugin)
        plugin._active_engine_name = "myengine"
        plugin._engine_settings = {}
        plugin._default_prompt = "Describe this image."
        plugin._default_model = ""

        result = await plugin.describe_media("/tmp/does_not_exist_xyz.jpg")
        assert result is None


@pytest.mark.asyncio
async def test_iris_plugin_calls_engine(tmp_path) -> None:
    """Plugin calls the engine and returns IrisResult."""

    from plugins.iris_base import IrisEngineBase, IrisResult

    class MockEngine(IrisEngineBase):
        def describe_image(
            self,
            file_path: str,
            mime_type: str | None = None,
            prompt: str | None = None,
            model: str | None = None,
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
        plugin._default_model = ""

        with (
            patch("plugins.iris_plugin.IRIS_REGISTRY", reg),
            patch.object(plugin, "refresh_config"),
        ):
            result = await plugin.describe_media(str(test_file), "image/jpeg")

    assert result is not None
    assert result.description == "a sunny beach"
    assert result.language == "en"


@pytest.mark.asyncio
async def test_iris_plugin_passes_model_override_to_engine(tmp_path) -> None:
    from plugins.iris_base import IrisEngineBase, IrisResult

    class MockEngine(IrisEngineBase):
        def describe_image(
            self,
            file_path: str,
            mime_type: str | None = None,
            prompt: str | None = None,
            model: str | None = None,
        ) -> IrisResult | None:
            assert model == "chatgpt-vision"
            return IrisResult(description="a sunny beach", language="en")

    test_file = tmp_path / "test.jpg"
    test_file.write_bytes(b"\xff\xd8\xff")

    with patch("core.core_initializer.register_plugin"):
        from plugins.iris_plugin import IrisPlugin
        from core.iris_registry import IrisRegistry

        reg = IrisRegistry()
        reg.register_instance("mock", MockEngine(), label="Mock")

        plugin = IrisPlugin.__new__(IrisPlugin)
        plugin._active_engine_name = "mock"
        plugin._engine_settings = {}
        plugin._default_prompt = "Describe this image."
        plugin._default_model = ""

        with (
            patch("plugins.iris_plugin.IRIS_REGISTRY", reg),
            patch.object(plugin, "refresh_config"),
        ):
            result = await plugin.describe_media(
                str(test_file),
                "image/jpeg",
                prompt="Describe this image.",
                model="chatgpt-vision",
            )

    assert result is not None
    assert result.description == "a sunny beach"


@pytest.mark.asyncio
async def test_external_iris_engine_uses_model_override(tmp_path) -> None:
    from core.external_endpoints.bridges.iris_bridge import ExternalIrisEngine
    from core.external_endpoints.models import EndpointProtocol, ExternalEndpoint

    class DummyAdapter:
        async def describe_image(
            self,
            image_bytes: bytes,
            mime_type: str | None = None,
            prompt: str | None = None,
            model: str | None = None,
            **kwargs: object,
        ) -> str | None:
            assert model == "chatgpt-vision"
            return "a sunny beach"

    endpoint = ExternalEndpoint(
        id=1,
        name="chatgpt_vision",
        display_label="ChatGPT Vision",
        protocol=EndpointProtocol.OPENAI,
        base_url="https://api.openai.com",
        api_key_enc=None,
        enabled=True,
        capabilities={"vision": True},
        subsystem_map={"vision": True},
        available_models=["chatgpt-vision"],
        default_model="gpt-4.1-vision",
        probe_status="success",
        last_probe_at=None,
        extra_config={},
    )

    adapter = DummyAdapter()
    engine = ExternalIrisEngine(endpoint, adapter)  # type: ignore[arg-type]

    test_file = tmp_path / "test.jpg"
    test_file.write_bytes(b"\xff\xd8\xff")

    result = await engine.describe_image(
        str(test_file),
        "image/jpeg",
        prompt="Describe this image.",
        model="chatgpt-vision",
    )
    assert result is not None
    assert result.description == "a sunny beach"


@pytest.mark.asyncio
async def test_gemini_adapter_uses_model_override(tmp_path) -> None:
    import sys
    from types import ModuleType

    from core.external_endpoints.adapters.gemini_adapter import GeminiAdapter

    dummy_client = types.SimpleNamespace(models=types.SimpleNamespace())

    def generate_content(
        *, model: str, contents: list[object], config: object | None = None
    ) -> object:
        assert model == "chatgpt-vision"
        assert config is not None

        class DummyResponse:
            text = "a sunny beach"

        return DummyResponse()

    dummy_client.models.generate_content = generate_content

    google_module = cast(Any, ModuleType("google"))
    genai_module = cast(Any, ModuleType("google.genai"))
    types_module = cast(Any, ModuleType("google.genai.types"))

    class DummyPart:
        @staticmethod
        def from_bytes(data: bytes, mime_type: str) -> dict[str, object]:
            return {"type": "image", "data": data}

    class DummyGenerateContentConfig(dict):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

    class DummySafetySetting(dict):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)

    class DummyHarmCategory:
        HARM_CATEGORY_HARASSMENT = "harassment"
        HARM_CATEGORY_HATE_SPEECH = "hate_speech"
        HARM_CATEGORY_SEXUALLY_EXPLICIT = "sexually_explicit"
        HARM_CATEGORY_DANGEROUS_CONTENT = "dangerous_content"

    class DummyHarmBlockThreshold:
        OFF = "off"

    types_module.Part = DummyPart
    types_module.GenerateContentConfig = DummyGenerateContentConfig
    types_module.SafetySetting = DummySafetySetting
    types_module.HarmCategory = DummyHarmCategory
    types_module.HarmBlockThreshold = DummyHarmBlockThreshold
    genai_module.types = types_module
    google_module.genai = genai_module

    with (
        patch.dict(
            sys.modules,
            {
                "google": google_module,
                "google.genai": genai_module,
                "google.genai.types": types_module,
            },
            clear=False,
        ),
        patch.object(GeminiAdapter, "_get_client", return_value=dummy_client),
    ):
        adapter = GeminiAdapter(api_key="unused")
        result = await adapter.describe_image(
            b"\xff\xd8\xff",
            "image/jpeg",
            prompt="Describe this image.",
            model="chatgpt-vision",
        )

    assert result == "a sunny beach"


@pytest.mark.asyncio
async def test_describe_attachment_images_with_iris() -> None:
    from core.iris_registry import IrisRegistry
    from core.plugin_instance import _describe_attachment_images_with_iris
    from plugins.iris_base import IrisEngineBase, IrisResult
    from plugins.iris_plugin import IrisPlugin

    class MockEngine(IrisEngineBase):
        def describe_image(
            self,
            file_path: str,
            mime_type: str | None = None,
            prompt: str | None = None,
            model: str | None = None,
        ) -> IrisResult | None:
            return IrisResult(description="a red ball", language="en")

    reg = IrisRegistry()
    reg.register_instance(
        "mock", MockEngine(), label="Mock", capabilities={"vision": True}
    )

    plugin = IrisPlugin.__new__(IrisPlugin)
    plugin._active_engine_name = "mock"
    plugin._engine_settings = {}
    plugin._default_prompt = "Describe this image."
    plugin._default_model = ""

    data = base64.b64encode(b"dummy").decode("ascii")
    attachment = {"mime_type": "image/png", "data": data}

    with (
        patch("plugins.iris_plugin.IRIS_REGISTRY", reg),
        patch.object(
            plugin,
            "refresh_config",
        ),
        patch.dict(
            "core.core_initializer.PLUGIN_REGISTRY",
            {"iris_plugin": plugin},
            clear=True,
        ),
    ):
        result = await _describe_attachment_images_with_iris(
            [attachment], prompt="Describe this image."
        )

    assert result is not None
    assert result.description == "a red ball"
    assert result.language == "en"


@pytest.mark.asyncio
async def test_describe_attachment_images_with_iris_disabled_engine_returns_placeholder() -> (
    None
):
    from core.plugin_instance import _describe_attachment_images_with_iris
    from plugins.iris_plugin import IrisPlugin

    plugin = IrisPlugin.__new__(IrisPlugin)
    plugin._active_engine_name = "disabled"
    plugin._default_model = ""

    attachment = {"mime_type": "image/png", "data": "ZmFrZQ=="}

    with (
        patch.dict(
            "core.core_initializer.PLUGIN_REGISTRY",
            {"iris_plugin": plugin},
            clear=True,
        ),
        patch.object(plugin, "refresh_config"),
    ):
        result = await _describe_attachment_images_with_iris(
            [attachment], prompt="Describe this image."
        )

    assert result is not None
    assert "disabled" in result.description
    assert "image/png" in result.description


@pytest.mark.asyncio
async def test_describe_attachment_images_with_iris_engine_failure_returns_placeholder() -> (
    None
):
    from core.iris_registry import IrisRegistry
    from core.plugin_instance import _describe_attachment_images_with_iris
    from plugins.iris_base import IrisEngineBase
    from plugins.iris_plugin import IrisPlugin

    class MockEngine(IrisEngineBase):
        def describe_image(
            self,
            file_path: str,
            mime_type: str | None = None,
            prompt: str | None = None,
            model: str | None = None,
        ) -> None:
            return None

    reg = IrisRegistry()
    reg.register_instance(
        "mock",
        MockEngine(),
        label="Mock",
        capabilities={"vision": True},
    )

    plugin = IrisPlugin.__new__(IrisPlugin)
    plugin._active_engine_name = "mock"
    plugin._default_model = ""

    attachment = {"mime_type": "image/jpeg", "data": "ZmFrZQ=="}

    with (
        patch("plugins.iris_plugin.IRIS_REGISTRY", reg),
        patch.dict(
            "core.core_initializer.PLUGIN_REGISTRY",
            {"iris_plugin": plugin},
            clear=True,
        ),
        patch.object(plugin, "refresh_config"),
    ):
        result = await _describe_attachment_images_with_iris(
            [attachment], prompt="Describe this image."
        )

    assert result is not None
    assert "error" in result.description
    assert "image/jpeg" in result.description


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
async def test_handle_custom_action_vision_describe(tmp_path) -> None:
    from plugins.iris_base import IrisEngineBase, IrisResult

    class MockEngine(IrisEngineBase):
        def describe_image(
            self,
            file_path: str,
            mime_type: str | None = None,
            prompt: str | None = None,
            model: str | None = None,
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
        plugin._default_model = ""

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
async def test_execute_action_vision_describe_uses_current_attachment() -> None:
    from plugins.iris_base import IrisEngineBase, IrisResult

    class MockEngine(IrisEngineBase):
        def describe_image(
            self,
            file_path: str,
            mime_type: str | None = None,
            prompt: str | None = None,
            model: str | None = None,
        ) -> IrisResult | None:
            with open(file_path, "rb") as fh:
                assert fh.read() == b"\x89PNG"
            assert mime_type == "image/png"
            return IrisResult(description="mountains", language="en", confidence=0.9)

    attachment_b64 = base64.b64encode(b"\x89PNG").decode("ascii")
    fake_message = types.SimpleNamespace(
        interface_path="custom/123",
        attachments=[
            {
                "mime_type": "image/png",
                "filename": "img.png",
                "data": attachment_b64,
            }
        ],
    )

    with patch("core.core_initializer.register_plugin"):
        from plugins.iris_plugin import IrisPlugin
        from core.iris_registry import IrisRegistry

        reg = IrisRegistry()
        reg.register_instance("mock", MockEngine(), label="Mock")

        plugin = IrisPlugin.__new__(IrisPlugin)
        plugin._active_engine_name = "mock"
        plugin._engine_settings = {}
        plugin._default_prompt = "Describe."
        plugin._default_model = ""

        with (
            patch("plugins.iris_plugin.IRIS_REGISTRY", reg),
            patch.object(plugin, "refresh_config"),
        ):
            response = await plugin.execute_action(
                {"type": "vision_describe", "payload": {}},
                {},
                None,
                fake_message,
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
        plugin._default_model = ""

        response = await plugin.handle_custom_action("unknown_action", {})

    assert response["status"] == "error"


# ---------------------------------------------------------------------------
# Attachment stripping and metadata propagation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_describe_attachment_returns_full_iris_result_with_metadata() -> None:
    """Successful Iris analysis must return the full IrisResult, not just a string."""
    from core.iris_registry import IrisRegistry
    from core.plugin_instance import _describe_attachment_images_with_iris
    from plugins.iris_base import IrisEngineBase, IrisResult
    from plugins.iris_plugin import IrisPlugin

    class MockEngine(IrisEngineBase):
        def describe_image(
            self,
            file_path: str,
            mime_type: str | None = None,
            prompt: str | None = None,
            model: str | None = None,
        ) -> IrisResult | None:
            return IrisResult(
                description="sunset over ocean", language="en", confidence=0.92
            )

    reg = IrisRegistry()
    reg.register_instance(
        "mock", MockEngine(), label="Mock", capabilities={"vision": True}
    )

    plugin = IrisPlugin.__new__(IrisPlugin)
    plugin._active_engine_name = "mock"
    plugin._engine_settings = {}
    plugin._default_prompt = "Describe."
    plugin._default_model = ""

    data = base64.b64encode(b"fake-image").decode("ascii")
    attachment = {"mime_type": "image/jpeg", "data": data}

    with (
        patch("plugins.iris_plugin.IRIS_REGISTRY", reg),
        patch.object(plugin, "refresh_config"),
        patch.dict(
            "core.core_initializer.PLUGIN_REGISTRY", {"iris_plugin": plugin}, clear=True
        ),
    ):
        result = await _describe_attachment_images_with_iris([attachment])

    assert isinstance(result, IrisResult)
    assert result.description == "sunset over ocean"
    assert result.language == "en"
    assert result.confidence == pytest.approx(0.92)


@pytest.mark.asyncio
async def test_describe_attachment_unavailable_plugin_returns_iris_result() -> None:
    """When iris_plugin is missing from the registry, an IrisResult placeholder is returned."""
    from core.plugin_instance import _describe_attachment_images_with_iris
    from plugins.iris_base import IrisResult

    attachment = {"mime_type": "image/png", "data": "ZmFrZQ=="}

    with patch.dict("core.core_initializer.PLUGIN_REGISTRY", {}, clear=True):
        result = await _describe_attachment_images_with_iris([attachment])

    assert isinstance(result, IrisResult)
    assert "unavailable" in result.description
    assert "image/png" in result.description


@pytest.mark.asyncio
async def test_audio_attachments_not_stripped() -> None:
    """Audio attachments must survive the image-stripping filter."""

    # Simulating the strip logic from the callsite
    attachments = [
        {"mime_type": "image/png", "data": "img_data"},
        {"mime_type": "audio/ogg", "data": "audio_data"},
        {"mime_type": "video/mp4", "data": "video_data"},
        {"mime_type": "application/pdf", "data": "pdf_data"},
    ]

    # This is the same filter applied in plugin_instance.py callsite
    filtered = [
        att
        for att in attachments
        if not (att.get("mime_type") or "").startswith(("image/", "video/"))
    ]

    assert len(filtered) == 2
    assert filtered[0]["mime_type"] == "audio/ogg"
    assert filtered[1]["mime_type"] == "application/pdf"
