"""Regression tests for Iris vision response validation.

Covers:
  - JSON-actions response is rejected (engine returned SyntH schema instead of plain text)
  - Plain text responses pass through normally
  - Timeout on describe_media is handled gracefully without crashing
"""

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch


def _make_plugin(active_engine: str = "test-engine") -> "IrisPlugin":  # noqa: F821
    """Create a partially-initialized IrisPlugin suitable for unit tests."""
    from plugins.iris_plugin import IrisPlugin

    plugin = IrisPlugin.__new__(IrisPlugin)
    plugin._active_engine_name = active_engine
    plugin._default_prompt = "Describe this image in detail."
    plugin._default_model = ""
    plugin.refresh_config = MagicMock()  # skip DB config refresh
    return plugin


class TestIrisResponseValidation(unittest.IsolatedAsyncioTestCase):
    """iris_plugin.describe_media must reject JSON-actions payloads."""

    @patch("plugins.iris_plugin.IRIS_REGISTRY")
    @patch("os.path.exists", return_value=True)
    async def test_json_actions_response_is_rejected(self, _exists, mock_registry):
        """When the engine returns JSON with 'actions', describe_media returns None."""
        from plugins.iris_base import IrisResult

        json_response = IrisResult(
            description='{"actions": [{"type": "update_diary_entry", "payload": {"id": 3, "content": "some diary"}}]}',
            language=None,
        )

        mock_engine = MagicMock()
        mock_engine.describe_image = AsyncMock(return_value=json_response)
        mock_registry.load_engine.return_value = mock_engine

        plugin = _make_plugin()
        result = await plugin.describe_media("/tmp/fake.jpg", "image/jpeg")

        assert result is None, (
            "describe_media should return None when engine returns JSON actions"
        )

    @patch("plugins.iris_plugin.IRIS_REGISTRY")
    @patch("os.path.exists", return_value=True)
    async def test_plain_text_response_passes_through(self, _exists, mock_registry):
        """A plain text description must be returned without modification."""
        from plugins.iris_base import IrisResult

        plain_result = IrisResult(
            description="A cat sitting on a red sofa.",
            language="en",
        )

        mock_engine = MagicMock()
        mock_engine.describe_image = AsyncMock(return_value=plain_result)
        mock_registry.load_engine.return_value = mock_engine

        plugin = _make_plugin()
        result = await plugin.describe_media("/tmp/fake.jpg", "image/jpeg")

        assert result is not None
        assert result.description == "A cat sitting on a red sofa."

    @patch("plugins.iris_plugin.IRIS_REGISTRY")
    @patch("os.path.exists", return_value=True)
    async def test_json_without_actions_key_passes_through(self, _exists, mock_registry):
        """JSON that does NOT contain 'actions' is treated as valid plain text."""
        from plugins.iris_base import IrisResult

        non_action_json = IrisResult(
            description='{"description": "A cat", "confidence": 0.9}',
            language=None,
        )

        mock_engine = MagicMock()
        mock_engine.describe_image = AsyncMock(return_value=non_action_json)
        mock_registry.load_engine.return_value = mock_engine

        plugin = _make_plugin()
        result = await plugin.describe_media("/tmp/fake.jpg", "image/jpeg")

        # Should pass through — no "actions" key
        assert result is not None
        assert "description" in result.description


class TestIrisCallTimeout(unittest.IsolatedAsyncioTestCase):
    """_describe_attachment_images_with_iris must respect a 120s timeout."""

    async def test_iris_timeout_returns_placeholder(self):
        """When describe_media stalls, the call must be cancelled within 120s
        and a placeholder IrisResult returned instead of hanging forever."""

        async def slow_describe(*args, **kwargs):
            await asyncio.sleep(9999)  # simulate hang

        with patch("core.core_initializer.PLUGIN_REGISTRY") as mock_reg:
            mock_iris = MagicMock()
            mock_iris._active_engine_name = "test-engine"
            mock_iris.refresh_config = MagicMock()
            mock_iris.describe_media = AsyncMock(side_effect=slow_describe)
            mock_reg.get.return_value = mock_iris

            from core.plugin_instance import _describe_attachment_images_with_iris

            attachments = [
                {
                    "mime_type": "image/jpeg",
                    "data": "AAAA",  # minimal valid base64
                }
            ]

            # Patch asyncio.wait_for inside plugin_instance to use a very
            # short timeout so the test completes quickly.
            original_wait_for = asyncio.wait_for

            async def fast_timeout(coro, timeout):  # type: ignore[override]
                return await original_wait_for(coro, timeout=0.05)

            with patch("core.plugin_instance.asyncio.wait_for", side_effect=fast_timeout):
                result = await _describe_attachment_images_with_iris(attachments)

        from plugins.iris_base import IrisResult

        assert result is not None, "Should return a placeholder, not None"
        assert isinstance(result, IrisResult)
        assert result.description  # non-empty placeholder


if __name__ == "__main__":
    unittest.main()
