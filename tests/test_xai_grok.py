"""Tests for the xAI Grok LLM engine."""

from __future__ import annotations

import base64
import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
import requests

from core.prompt_request import PromptRequest, RuntimeContext
from engines.external_engines.xai_grok import (
    DEFAULT_MODEL,
    MODEL_CONFIGS,
    XaiGrokPlugin,
)


def _make_http_response(
    status_code: int = 200,
    json_body: dict[str, Any] | None = None,
    text: str = "",
) -> MagicMock:
    """Create a mock requests.Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text or json.dumps(json_body or {})
    resp.json.return_value = json_body or {}
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = requests.HTTPError(
            f"HTTP {status_code}", response=resp
        )
    return resp


class TestXaiGrokPlugin:
    def _make_plugin(self) -> XaiGrokPlugin:
        """Create a plugin instance with mocked dependencies."""
        with patch("core.notifier.set_notifier"):
            return XaiGrokPlugin()

    def test_initialization(self) -> None:
        plugin = self._make_plugin()
        assert plugin.get_current_model() == DEFAULT_MODEL
        assert "default" in plugin.model_limits_map
        assert plugin.display_name == "xAI Grok"
        assert plugin.supports_prompt_request is True

    def test_health_status(self) -> None:
        plugin = self._make_plugin()

        # Test when API key is not configured
        with patch(
            "engines.external_engines.xai_grok.XAI_API_KEY",
            MagicMock(__str__=lambda s: "", __bool__=lambda s: False),
        ):
            ok, msg = plugin.get_health_status()
            assert ok is False
            assert "not configured" in msg

        # Test when API key is configured
        with patch(
            "engines.external_engines.xai_grok.XAI_API_KEY",
            MagicMock(__str__=lambda s: "xai-test-key", __bool__=lambda s: True),
        ):
            ok, msg = plugin.get_health_status()
            assert ok is True
            assert msg == ""

    def test_get_supported_models(self) -> None:
        plugin = self._make_plugin()
        models = plugin.get_supported_models()
        assert set(models) == set(MODEL_CONFIGS.keys())

    def test_model_switching(self) -> None:
        plugin = self._make_plugin()

        # Switch to valid model
        plugin.set_current_model("grok-2-vision-1212")
        assert plugin.get_current_model() == "grok-2-vision-1212"

        # Switch to invalid model (should keep current)
        plugin.set_current_model("grok-nonexistent")
        assert plugin.get_current_model() == "grok-2-vision-1212"

    def test_get_rate_limit(self) -> None:
        plugin = self._make_plugin()
        assert plugin.get_rate_limit() == (60, 60, 0.5)

    def test_get_interface_limits(self) -> None:
        plugin = self._make_plugin()

        # Test standard model limits
        plugin.set_current_model("grok-4-1-fast-reasoning")
        limits = plugin.get_interface_limits()
        assert limits["max_prompt_chars"] == 2000000 * 3
        assert limits["supports_images"] is False
        assert limits["supports_functions"] is True

        # Test vision model limits
        plugin.set_current_model("grok-2-vision-1212")
        limits = plugin.get_interface_limits()
        assert limits["max_prompt_chars"] == 32768 * 3
        assert limits["supports_images"] is True

    def test_extract_image_parts(self) -> None:
        plugin = self._make_plugin()

        # Test prompt dict containing base64 attachments
        prompt = {
            "attachments": [
                {
                    "mime_type": "image/png",
                    "data": "base64data",
                },
                {
                    "mime_type": "image/gif",
                    "path": "dummy.gif",
                    "data": "gifbase64",
                },
            ]
        }
        parts = plugin._extract_image_parts(prompt)
        assert len(parts) == 2
        assert parts[0]["type"] == "image_url"
        assert parts[0]["image_url"]["url"] == "data:image/png;base64,base64data"
        assert parts[1]["image_url"]["url"] == "data:image/gif;base64,gifbase64"

    def test_extract_image_parts_from_path(self) -> None:
        plugin = self._make_plugin()

        prompt = {
            "attachments": [
                {
                    "path": "test.jpg",
                }
            ]
        }

        with (
            patch("pathlib.Path.exists", return_value=True),
            patch("pathlib.Path.read_bytes", return_value=b"jpeg_data"),
        ):
            parts = plugin._extract_image_parts(prompt)
            assert len(parts) == 1
            assert parts[0]["type"] == "image_url"
            expected_b64 = base64.b64encode(b"jpeg_data").decode("utf-8")
            assert (
                parts[0]["image_url"]["url"] == f"data:image/jpeg;base64,{expected_b64}"
            )

    def test_copy_and_redact_data(self) -> None:
        plugin = self._make_plugin()
        prompt = {
            "attachments": [
                {
                    "mime_type": "image/png",
                    "data": "A" * 1000,
                }
            ]
        }
        redacted = plugin._copy_and_redact_data(prompt)
        assert redacted["attachments"][0]["data"] == "<redacted: 1000 chars>"
        # Ensure original prompt is untouched
        assert prompt["attachments"][0]["data"] == "A" * 1000

    def test_build_system_instruction(self) -> None:
        plugin = self._make_plugin()

        # Test standard interface formatting
        prompt = {
            "interface": "telegram_bot",
        }
        si = plugin._build_system_instruction(prompt)
        assert "CURRENT INTERFACE: telegram_bot" in si
        assert "message_telegram_bot" in si

        # Test grillo beat formatting
        prompt_grillo = {
            "interface": "grillo",
            "grillo_beat": True,
        }
        si_grillo = plugin._build_system_instruction(prompt_grillo)
        assert "CURRENT INTERFACE: grillo (INTERNAL)" in si_grillo
        assert "Do NOT output any message_* actions" in si_grillo

        # Test inclusion of verbose instructions
        prompt_verbose = {
            "interface": "synth_webui",
            "instructions_verbose": "These are verbose custom instructions.",
        }
        si_verbose = plugin._build_system_instruction(prompt_verbose)
        assert "These are verbose custom instructions." in si_verbose

    @pytest.mark.asyncio
    async def test_generate_response_no_key(self) -> None:
        plugin = self._make_plugin()
        with patch(
            "engines.external_engines.xai_grok.XAI_API_KEY",
            MagicMock(__str__=lambda s: "", __bool__=lambda s: False),
        ):
            res = await plugin.generate_response("test message")
            assert "Key not configured" in res

    @pytest.mark.asyncio
    async def test_generate_response_success_string(self) -> None:
        plugin = self._make_plugin()

        response_body = {
            "choices": [
                {
                    "message": {
                        "content": '{"actions": [{"type": "message_synth_webui", "payload": {"text": "hello"}}]}'
                    }
                }
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 30},
        }

        with (
            patch(
                "engines.external_engines.xai_grok.XAI_API_KEY",
                MagicMock(__str__=lambda s: "xai-test-key", __bool__=lambda s: True),
            ),
            patch(
                "engines.external_engines.xai_grok.XAI_BASE_URL",
                MagicMock(
                    __str__=lambda s: "https://api.x.ai", __bool__=lambda s: True
                ),
            ),
            patch(
                "engines.external_engines.xai_grok.XAI_MAX_TOKENS",
                MagicMock(
                    __str__=lambda s: "4096",
                    __int__=lambda s: 4096,
                    __bool__=lambda s: True,
                ),
            ),
            patch("engines.external_engines.xai_grok.requests.post") as mock_post,
        ):
            mock_post.return_value = _make_http_response(json_body=response_body)

            res = await plugin.generate_response("hello")
            assert "hello" in res
            mock_post.assert_called_once()

            call_kwargs = mock_post.call_args[1]
            assert call_kwargs["json"]["model"] == DEFAULT_MODEL
            assert len(call_kwargs["json"]["messages"]) == 2
            assert call_kwargs["json"]["messages"][1]["role"] == "user"
            assert call_kwargs["json"]["messages"][1]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_generate_response_prompt_request(self) -> None:
        plugin = self._make_plugin()

        pr = PromptRequest(
            system_instruction="You are a helpful assistant",
            conversation_history=[],
            current_text="Hi",
            runtime_ctx=RuntimeContext(
                interface_name="test_interface",
                interface_path="test/123",
                username="Tester",
                language="en",
            ),
        )

        response_body = {
            "choices": [{"message": {"content": "parsed_response_content"}}]
        }

        with (
            patch(
                "engines.external_engines.xai_grok.XAI_API_KEY",
                MagicMock(__str__=lambda s: "xai-test-key", __bool__=lambda s: True),
            ),
            patch(
                "engines.external_engines.xai_grok.XAI_BASE_URL",
                MagicMock(
                    __str__=lambda s: "https://api.x.ai", __bool__=lambda s: True
                ),
            ),
            patch(
                "engines.external_engines.xai_grok.XAI_MAX_TOKENS",
                MagicMock(
                    __str__=lambda s: "4096",
                    __int__=lambda s: 4096,
                    __bool__=lambda s: True,
                ),
            ),
            patch("engines.external_engines.xai_grok.requests.post") as mock_post,
        ):
            mock_post.return_value = _make_http_response(json_body=response_body)

            res = await plugin.generate_response(pr)
            assert res == "parsed_response_content"
            mock_post.assert_called_once()

    @pytest.mark.asyncio
    async def test_generate_response_correction_prompt(self) -> None:
        plugin = self._make_plugin()

        correction_prompt = {
            "system_message": {
                "type": "invalid_json",
                "instructions": "Please correct the JSON format",
                "interface": "synth_webui",
                "message_action": "message_synth_webui",
            }
        }

        response_body = {"choices": [{"message": {"content": '{"actions": []}'}}]}

        with (
            patch(
                "engines.external_engines.xai_grok.XAI_API_KEY",
                MagicMock(__str__=lambda s: "xai-test-key", __bool__=lambda s: True),
            ),
            patch(
                "engines.external_engines.xai_grok.XAI_BASE_URL",
                MagicMock(
                    __str__=lambda s: "https://api.x.ai", __bool__=lambda s: True
                ),
            ),
            patch(
                "engines.external_engines.xai_grok.XAI_MAX_TOKENS",
                MagicMock(
                    __str__=lambda s: "4096",
                    __int__=lambda s: 4096,
                    __bool__=lambda s: True,
                ),
            ),
            patch("engines.external_engines.xai_grok.requests.post") as mock_post,
        ):
            mock_post.return_value = _make_http_response(json_body=response_body)

            res = await plugin.generate_response(correction_prompt)
            assert res == '{"actions": []}'
            mock_post.assert_called_once()

            call_kwargs = mock_post.call_args[1]
            messages = call_kwargs["json"]["messages"]
            assert len(messages) == 2
            assert messages[0]["role"] == "system"
            assert "JSON correction assistant" in messages[0]["content"]

    @pytest.mark.asyncio
    async def test_handle_incoming_message(self) -> None:
        plugin = self._make_plugin()

        mock_bot = MagicMock()
        mock_message = MagicMock()
        mock_message.interface = "telegram_bot"
        mock_message.chat_id = 12345
        mock_message.interface_path = "synth_webui/12345"

        with patch.object(
            plugin, "generate_response", return_value="hello back"
        ) as mock_gen:
            res = await plugin.handle_incoming_message(mock_bot, mock_message, "hello")
            assert res == "hello back"
            mock_gen.assert_called_once_with("hello")

    @pytest.mark.asyncio
    async def test_api_http_error(self) -> None:
        plugin = self._make_plugin()

        with (
            patch(
                "engines.external_engines.xai_grok.XAI_API_KEY",
                MagicMock(__str__=lambda s: "xai-test-key", __bool__=lambda s: True),
            ),
            patch(
                "engines.external_engines.xai_grok.XAI_BASE_URL",
                MagicMock(
                    __str__=lambda s: "https://api.x.ai", __bool__=lambda s: True
                ),
            ),
            patch(
                "engines.external_engines.xai_grok.XAI_MAX_TOKENS",
                MagicMock(
                    __str__=lambda s: "4096",
                    __int__=lambda s: 4096,
                    __bool__=lambda s: True,
                ),
            ),
            patch("engines.external_engines.xai_grok.requests.post") as mock_post,
        ):
            mock_post.return_value = _make_http_response(
                status_code=500, text="Internal Server Error"
            )
            res = await plugin.generate_response("hello")
            assert "xAI Grok API error" in res
            assert "500" in res

    @pytest.mark.asyncio
    async def test_api_network_error(self) -> None:
        plugin = self._make_plugin()

        with (
            patch(
                "engines.external_engines.xai_grok.XAI_API_KEY",
                MagicMock(__str__=lambda s: "xai-test-key", __bool__=lambda s: True),
            ),
            patch(
                "engines.external_engines.xai_grok.XAI_BASE_URL",
                MagicMock(
                    __str__=lambda s: "https://api.x.ai", __bool__=lambda s: True
                ),
            ),
            patch(
                "engines.external_engines.xai_grok.XAI_MAX_TOKENS",
                MagicMock(
                    __str__=lambda s: "4096",
                    __int__=lambda s: 4096,
                    __bool__=lambda s: True,
                ),
            ),
            patch("engines.external_engines.xai_grok.requests.post") as mock_post,
        ):
            mock_post.side_effect = Exception("Connection timed out")
            res = await plugin.generate_response("hello")
            assert "request failed" in res
            assert "Connection timed out" in res
