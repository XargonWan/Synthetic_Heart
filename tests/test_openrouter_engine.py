"""Tests for the OpenRouter LLM engine."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures & helpers
# ---------------------------------------------------------------------------

# Minimal OpenRouter /models API response payload
_SAMPLE_MODELS_RESPONSE: dict[str, Any] = {
    "data": [
        {
            "id": "anthropic/claude-sonnet-4",
            "name": "Anthropic: Claude Sonnet 4",
            "context_length": 200000,
            "top_provider": {"max_completion_tokens": 8192},
            "architecture": {"modality": "text+image->text", "tokenizer": "Claude"},
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            "supported_parameters": ["tools", "tool_choice", "temperature"],
        },
        {
            "id": "openai/gpt-4o",
            "name": "OpenAI: GPT-4o",
            "context_length": 128000,
            "top_provider": {"max_completion_tokens": 4096},
            "architecture": {"modality": "text+image->text", "tokenizer": "GPT"},
            "pricing": {"prompt": "0.000005", "completion": "0.000015"},
            "supported_parameters": ["tools", "temperature"],
        },
        {
            "id": "meta-llama/llama-3-70b",
            "name": "Meta: Llama 3 70B",
            "context_length": 8192,
            "top_provider": {"max_completion_tokens": 2048},
            "architecture": {"modality": "text->text"},
            "pricing": {"prompt": "0.0000008", "completion": "0.0000008"},
            "supported_parameters": ["temperature"],
        },
    ]
}


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
        resp.raise_for_status.side_effect = Exception(f"HTTP {status_code}")
    return resp


# ---------------------------------------------------------------------------
# Model catalog tests
# ---------------------------------------------------------------------------


class TestOpenRouterModel:
    def test_parse_vision_model(self) -> None:
        from engines.external_engines.openrouter import OpenRouterModel

        data = _SAMPLE_MODELS_RESPONSE["data"][0]
        m = OpenRouterModel.from_api(data)
        assert m.id == "anthropic/claude-sonnet-4"
        assert m.supports_vision is True
        assert m.supports_audio is False
        assert m.supports_tool_use is True
        assert m.context_length == 200000
        assert m.max_completion_tokens == 8192
        assert m.pricing_prompt == 0.000003

    def test_parse_text_only_model(self) -> None:
        from engines.external_engines.openrouter import OpenRouterModel

        data = _SAMPLE_MODELS_RESPONSE["data"][2]
        m = OpenRouterModel.from_api(data)
        assert m.id == "meta-llama/llama-3-70b"
        assert m.supports_vision is False
        assert m.supports_audio is False
        assert m.supports_tool_use is False
        assert m.modality == "text->text"

    def test_parse_audio_modality(self) -> None:
        from engines.external_engines.openrouter import OpenRouterModel

        data = {
            "id": "test/audio-model",
            "name": "Test Audio",
            "context_length": 4096,
            "architecture": {"modality": "text+image+audio->text"},
            "pricing": {},
            "supported_parameters": [],
        }
        m = OpenRouterModel.from_api(data)
        assert m.supports_vision is True
        assert m.supports_audio is True

    def test_parse_missing_fields(self) -> None:
        from engines.external_engines.openrouter import OpenRouterModel

        m = OpenRouterModel.from_api({"id": "minimal/model"})
        assert m.id == "minimal/model"
        assert m.context_length == 4096
        assert m.modality == "text->text"
        assert m.supports_vision is False


class TestModelCatalog:
    def test_fetch_catalog_sync(self) -> None:
        from engines.external_engines.openrouter import _fetch_catalog_sync

        with patch("engines.external_engines.openrouter.requests.get") as mock_get:
            mock_get.return_value = _make_http_response(
                json_body=_SAMPLE_MODELS_RESPONSE
            )
            models = _fetch_catalog_sync("https://openrouter.ai/api/v1")

        assert len(models) == 3
        assert "anthropic/claude-sonnet-4" in models
        assert "meta-llama/llama-3-70b" in models
        assert models["openai/gpt-4o"].supports_vision is True

    def test_fetch_catalog_failure(self) -> None:
        from engines.external_engines.openrouter import _fetch_catalog_sync

        with patch("engines.external_engines.openrouter.requests.get") as mock_get:
            mock_get.side_effect = Exception("Network error")
            models = _fetch_catalog_sync("https://openrouter.ai/api/v1")

        assert models == {}


# ---------------------------------------------------------------------------
# Model routes / resolution tests
# ---------------------------------------------------------------------------


class TestModelResolution:
    def _make_plugin(self) -> Any:
        """Create a plugin instance with mocked dependencies."""
        from engines.external_engines.openrouter import OpenRouterPlugin

        with patch("engines.external_engines.openrouter._refresh_catalog"):
            with patch("core.notifier.set_notifier"):
                plugin = OpenRouterPlugin.__new__(OpenRouterPlugin)
                plugin._current_model = "anthropic/claude-sonnet-4"
                plugin._current_request_meta = None
                plugin._notify_fn = lambda *a: None
                plugin.model_limits_map = {"default": 200000}
        return plugin

    def test_resolve_default(self) -> None:
        plugin = self._make_plugin()
        with patch(
            "engines.external_engines.openrouter.OPENROUTER_MODEL_ROUTES",
            MagicMock(value="{}"),
        ):
            result = plugin._resolve_model()
        assert result == "anthropic/claude-sonnet-4"

    def test_resolve_scope_override(self) -> None:
        plugin = self._make_plugin()
        routes = {"scopes": {"grillo": "anthropic/claude-haiku-3.5"}}
        mock_var = MagicMock()
        mock_var.value = routes
        with patch(
            "engines.external_engines.openrouter.OPENROUTER_MODEL_ROUTES", mock_var
        ):
            result = plugin._resolve_model(scope="grillo")
        assert result == "anthropic/claude-haiku-3.5"

    def test_resolve_action_override(self) -> None:
        plugin = self._make_plugin()
        routes = {"actions": {"create_personal_diary_entry": "meta-llama/llama-3-70b"}}
        mock_var = MagicMock()
        mock_var.value = routes
        with patch(
            "engines.external_engines.openrouter.OPENROUTER_MODEL_ROUTES", mock_var
        ):
            result = plugin._resolve_model(action_type="create_personal_diary_entry")
        assert result == "meta-llama/llama-3-70b"

    def test_resolve_action_glob(self) -> None:
        plugin = self._make_plugin()
        routes = {"actions": {"message_*": "openai/gpt-4o"}}
        mock_var = MagicMock()
        mock_var.value = routes
        with patch(
            "engines.external_engines.openrouter.OPENROUTER_MODEL_ROUTES", mock_var
        ):
            result = plugin._resolve_model(action_type="message_telegram_bot")
        assert result == "openai/gpt-4o"

    def test_action_override_beats_scope(self) -> None:
        plugin = self._make_plugin()
        routes = {
            "scopes": {"grillo": "anthropic/claude-haiku-3.5"},
            "actions": {"create_personal_diary_entry": "meta-llama/llama-3-70b"},
        }
        mock_var = MagicMock()
        mock_var.value = routes
        with patch(
            "engines.external_engines.openrouter.OPENROUTER_MODEL_ROUTES", mock_var
        ):
            result = plugin._resolve_model(
                scope="grillo", action_type="create_personal_diary_entry"
            )
        assert result == "meta-llama/llama-3-70b"


# ---------------------------------------------------------------------------
# System instruction tests
# ---------------------------------------------------------------------------


class TestSystemInstruction:
    def _make_plugin(self) -> Any:
        from engines.external_engines.openrouter import OpenRouterPlugin

        plugin = OpenRouterPlugin.__new__(OpenRouterPlugin)
        plugin._current_model = "test/model"
        return plugin

    def test_extracts_interface(self) -> None:
        plugin = self._make_plugin()
        prompt = {
            "input": {"source": {"interface": "telegram_bot"}},
        }
        si = plugin._build_system_instruction(prompt)
        assert "CURRENT INTERFACE: telegram_bot" in si
        assert "message_telegram_bot" in si

    def test_includes_verbose_instructions(self) -> None:
        plugin = self._make_plugin()
        prompt = {
            "input": {"interface": "synth_webui"},
            "instructions_verbose": "CUSTOM PERSONA INSTRUCTIONS",
        }
        si = plugin._build_system_instruction(prompt)
        assert "CUSTOM PERSONA INSTRUCTIONS" in si
        assert "CRITICAL OUTPUT FORMAT" in si


# ---------------------------------------------------------------------------
# Multimodal extraction tests
# ---------------------------------------------------------------------------


class TestMultimodalExtraction:
    def _make_plugin(self) -> Any:
        from engines.external_engines.openrouter import OpenRouterPlugin

        plugin = OpenRouterPlugin.__new__(OpenRouterPlugin)
        return plugin

    def test_extracts_image_attachment(self) -> None:
        plugin = self._make_plugin()
        prompt = {
            "input": {
                "payload": {"text": "describe this"},
                "attachments": [
                    {
                        "mime_type": "image/jpeg",
                        "data": "AAAA",  # dummy base64
                    }
                ],
            }
        }
        parts = plugin._extract_multimodal_parts(prompt)
        assert len(parts) == 1
        assert parts[0]["type"] == "image_url"
        assert "data:image/jpeg;base64,AAAA" in parts[0]["image_url"]["url"]

    def test_extracts_audio_attachment(self) -> None:
        plugin = self._make_plugin()
        prompt = {
            "attachments": [
                {"mime_type": "audio/mpeg", "data": "BBBB"},
            ]
        }
        parts = plugin._extract_multimodal_parts(prompt)
        assert len(parts) == 1
        assert parts[0]["type"] == "input_audio"
        assert parts[0]["input_audio"]["format"] == "mp3"
        assert parts[0]["input_audio"]["data"] == "BBBB"

    def test_supports_document_mime(self) -> None:
        plugin = self._make_plugin()
        prompt = {
            "attachments": [
                {"mime_type": "application/pdf", "data": "CCCC"},
            ]
        }
        parts = plugin._extract_multimodal_parts(prompt)
        assert len(parts) == 1
        assert parts[0]["type"] == "image_url"
        assert "data:application/pdf;base64,CCCC" in parts[0]["image_url"]["url"]

    def test_redacts_base64_data(self) -> None:
        plugin = self._make_plugin()
        prompt = {
            "attachments": [
                {"mime_type": "image/png", "data": "A" * 10000},
            ]
        }
        redacted = plugin._copy_and_redact_data(prompt)
        att = redacted["attachments"][0]
        assert att["data"].startswith("<redacted:")
        assert att["mime_type"] == "image/png"


# ---------------------------------------------------------------------------
# Chat completion tests
# ---------------------------------------------------------------------------


class TestChatCompletion:
    def _make_plugin(self) -> Any:
        from engines.external_engines.openrouter import OpenRouterPlugin

        plugin = OpenRouterPlugin.__new__(OpenRouterPlugin)
        plugin._current_model = "test/model"
        plugin._notify_fn = lambda *a: None
        return plugin

    @pytest.mark.asyncio
    async def test_successful_completion(self) -> None:
        plugin = self._make_plugin()

        response_body = {
            "choices": [
                {
                    "message": {
                        "content": '{"actions": [{"type": "message_synth_webui", "payload": {"text": "hello"}}]}'
                    }
                }
            ],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }

        with (
            patch(
                "engines.external_engines.openrouter.OPENROUTER_API_KEY",
                MagicMock(__str__=lambda s: "test-key", __bool__=lambda s: True),
            ),
            patch(
                "engines.external_engines.openrouter.OPENROUTER_BASE_URL",
                MagicMock(__str__=lambda s: "https://openrouter.ai/api/v1"),
            ),
            patch(
                "engines.external_engines.openrouter.OPENROUTER_SITE_URL",
                MagicMock(__str__=lambda s: ""),
            ),
            patch(
                "engines.external_engines.openrouter.OPENROUTER_APP_NAME",
                MagicMock(__str__=lambda s: "Test"),
            ),
            patch("engines.external_engines.openrouter.requests.post") as mock_post,
        ):
            mock_post.return_value = _make_http_response(json_body=response_body)

            result = await plugin._openai_chat_completion(
                prompt_text='{"actions": []}',
                system_instruction="You are a test.",
                max_tokens=1024,
                model="test/model",
            )

        assert "hello" in result
        mock_post.assert_called_once()

        # Verify request structure
        call_kwargs = mock_post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")
        assert payload["model"] == "test/model"
        assert len(payload["messages"]) == 2
        assert payload["messages"][0]["role"] == "system"
        assert payload["messages"][1]["role"] == "user"

    @pytest.mark.asyncio
    async def test_handles_api_error(self) -> None:
        plugin = self._make_plugin()

        error_body = {"error": {"message": "Rate limit exceeded"}}

        with (
            patch(
                "engines.external_engines.openrouter.OPENROUTER_API_KEY",
                MagicMock(__str__=lambda s: "test-key", __bool__=lambda s: True),
            ),
            patch(
                "engines.external_engines.openrouter.OPENROUTER_BASE_URL",
                MagicMock(__str__=lambda s: "https://openrouter.ai/api/v1"),
            ),
            patch(
                "engines.external_engines.openrouter.OPENROUTER_SITE_URL",
                MagicMock(__str__=lambda s: ""),
            ),
            patch(
                "engines.external_engines.openrouter.OPENROUTER_APP_NAME",
                MagicMock(__str__=lambda s: "Test"),
            ),
            patch("engines.external_engines.openrouter.requests.post") as mock_post,
        ):
            mock_post.return_value = _make_http_response(json_body=error_body)

            result = await plugin._openai_chat_completion(
                prompt_text="test",
                system_instruction="test",
                max_tokens=100,
            )

        parsed = json.loads(result)
        assert parsed["actions"][0]["type"] == "system_message"
        assert "Rate limit exceeded" in parsed["actions"][0]["payload"]["text"]

    @pytest.mark.asyncio
    async def test_handles_empty_choices(self) -> None:
        plugin = self._make_plugin()

        with (
            patch(
                "engines.external_engines.openrouter.OPENROUTER_API_KEY",
                MagicMock(__str__=lambda s: "test-key", __bool__=lambda s: True),
            ),
            patch(
                "engines.external_engines.openrouter.OPENROUTER_BASE_URL",
                MagicMock(__str__=lambda s: "https://openrouter.ai/api/v1"),
            ),
            patch(
                "engines.external_engines.openrouter.OPENROUTER_SITE_URL",
                MagicMock(__str__=lambda s: ""),
            ),
            patch(
                "engines.external_engines.openrouter.OPENROUTER_APP_NAME",
                MagicMock(__str__=lambda s: "Test"),
            ),
            patch("engines.external_engines.openrouter.requests.post") as mock_post,
        ):
            mock_post.return_value = _make_http_response(json_body={"choices": []})

            result = await plugin._openai_chat_completion(
                prompt_text="test",
                system_instruction="test",
                max_tokens=100,
            )

        parsed = json.loads(result)
        assert "missing choices" in parsed["actions"][0]["payload"]["text"]


# ---------------------------------------------------------------------------
# Health & interface limits tests
# ---------------------------------------------------------------------------


class TestHealthAndLimits:
    def _make_plugin(self) -> Any:
        from engines.external_engines.openrouter import OpenRouterPlugin

        plugin = OpenRouterPlugin.__new__(OpenRouterPlugin)
        plugin._current_model = "anthropic/claude-sonnet-4"
        return plugin

    def test_health_no_key(self) -> None:
        plugin = self._make_plugin()
        with patch(
            "engines.external_engines.openrouter.OPENROUTER_API_KEY",
            MagicMock(__bool__=lambda s: False, __str__=lambda s: ""),
        ):
            ok, msg = plugin.get_health_status()
        assert ok is False
        assert "not configured" in msg

    def test_health_with_key(self) -> None:
        plugin = self._make_plugin()
        with patch(
            "engines.external_engines.openrouter.OPENROUTER_API_KEY",
            MagicMock(__bool__=lambda s: True, __str__=lambda s: "sk-test"),
        ):
            ok, msg = plugin.get_health_status()
        assert ok is True

    def test_interface_limits_from_catalog(self) -> None:
        from engines.external_engines.openrouter import OpenRouterModel, _catalog

        plugin = self._make_plugin()
        _catalog.models["anthropic/claude-sonnet-4"] = OpenRouterModel(
            id="anthropic/claude-sonnet-4",
            name="Claude Sonnet 4",
            context_length=200000,
            max_completion_tokens=8192,
            supports_vision=True,
        )
        limits = plugin.get_interface_limits()
        assert limits["max_prompt_chars"] == 200000 * 3
        assert limits["max_response_chars"] == 8192
        assert limits["supports_images"] is True

        # Cleanup
        _catalog.models.pop("anthropic/claude-sonnet-4", None)

    def test_rate_limit(self) -> None:
        plugin = self._make_plugin()
        rl = plugin.get_rate_limit()
        assert rl == (60, 60, 0.5)


# ---------------------------------------------------------------------------
# Parse routes helper
# ---------------------------------------------------------------------------


class TestParseRoutes:
    def test_dict_passthrough(self) -> None:
        from engines.external_engines.openrouter import _parse_routes

        routes = {"scopes": {"grillo": "test/model"}}
        assert _parse_routes(routes) == routes

    def test_json_string(self) -> None:
        from engines.external_engines.openrouter import _parse_routes

        result = _parse_routes('{"scopes": {"grillo": "test/model"}}')
        assert result == {"scopes": {"grillo": "test/model"}}

    def test_invalid_string(self) -> None:
        from engines.external_engines.openrouter import _parse_routes

        assert _parse_routes("not json") == {}

    def test_none(self) -> None:
        from engines.external_engines.openrouter import _parse_routes

        assert _parse_routes(None) == {}


# ---------------------------------------------------------------------------
# Capabilities query
# ---------------------------------------------------------------------------


class TestCapabilities:
    def _make_plugin(self) -> Any:
        from engines.external_engines.openrouter import OpenRouterPlugin

        plugin = OpenRouterPlugin.__new__(OpenRouterPlugin)
        plugin._current_model = "test/model"
        return plugin

    def test_capabilities_from_catalog(self) -> None:
        from engines.external_engines.openrouter import OpenRouterModel, _catalog

        _catalog.models["test/model"] = OpenRouterModel(
            id="test/model",
            name="Test Model",
            context_length=8192,
            max_completion_tokens=2048,
            modality="text+image->text",
            supports_vision=True,
            supports_tool_use=True,
        )

        plugin = self._make_plugin()
        caps = plugin.get_model_capabilities("test/model")
        assert caps["available"] is True
        assert caps["supports_vision"] is True
        assert caps["supports_tool_use"] is True

        _catalog.models.pop("test/model", None)

    def test_capabilities_missing_model(self) -> None:
        plugin = self._make_plugin()
        caps = plugin.get_model_capabilities("nonexistent/model")
        assert caps["available"] is False

    def test_catalog_summary(self) -> None:
        from engines.external_engines.openrouter import OpenRouterModel, _catalog

        _catalog.models = {
            "a": OpenRouterModel(id="a", name="A", supports_vision=True),
            "b": OpenRouterModel(id="b", name="B", supports_audio=True),
        }
        _catalog.last_fetched = 100.0

        plugin = self._make_plugin()
        summary = plugin.get_catalog_summary()
        assert summary["total_models"] == 2
        assert summary["vision_models"] == 1
        assert summary["audio_models"] == 1

        _catalog.models = {}
        _catalog.last_fetched = 0.0
