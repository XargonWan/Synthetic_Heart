"""Tests for core/prompt_renderers.py — all four engine renderers."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from core.prompt_renderers import (
    AnthropicRenderer,
    GeminiRenderer,
    OpenAIRenderer,
    TextRenderer,
)
from core.live_tool_registry import LiveToolRegistry
from core.prompt_request import PromptRequest, RuntimeContext, Turn


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manifest(
    name: str = "send_message",
    description: str = "Send a text reply",
    params: list[MagicMock] | None = None,
) -> MagicMock:
    """Return a duck-typed ToolManifest mock."""
    m = MagicMock()
    m.name = name
    m.description = description
    if params is None:
        params = [
            MagicMock(
                name="text",
                type="string",
                description="The reply text",
                required=True,
                enum=None,
            )
        ]
    # Patch .name attribute on each param mock
    for i, p in enumerate(params):
        if not isinstance(p, MagicMock):
            raise TypeError("params must be MagicMock instances")
        p.name = p.name  # ensure .name is set
    m.parameters = params
    return m


def _make_param(
    name: str,
    type_: str = "string",
    description: str = "",
    required: bool = True,
    enum: list | None = None,
) -> MagicMock:
    p = MagicMock(spec=[])
    p.name = name
    p.type = type_
    p.description = description
    p.required = required
    p.enum = enum
    return p


def _make_tool(
    name: str = "send_message",
    description: str = "Send a text reply",
) -> MagicMock:
    m = MagicMock(spec=[])
    m.name = name
    m.description = description
    m.parameters = [
        _make_param("text", description="The reply text"),
    ]
    return m


def _basic_request(**kwargs: object) -> PromptRequest:
    req = PromptRequest(
        system_instruction="You are Synth.",
        context_summary="Diary: nothing today.",
        conversation_history=[
            Turn(role="user", content="Hello!"),
            Turn(
                role="assistant",
                content='{"actions":[{"type":"send_message","payload":{"text":"Hi!"}}]}',
            ),
        ],
        current_text="How are you?",
        runtime_ctx=RuntimeContext(
            interface_name="test_interface",
            interface_path="test/123",
            username="Tester",
            language="en",
        ),
    )
    for k, v in kwargs.items():
        setattr(req, k, v)
    return req


# ---------------------------------------------------------------------------
# OpenAIRenderer
# ---------------------------------------------------------------------------


class TestOpenAIRenderer:
    def test_basic_messages_structure(self) -> None:
        req = _basic_request()
        renderer = OpenAIRenderer(req)
        messages = renderer.render()

        assert isinstance(messages, list)
        assert messages[0]["role"] == "system"
        # system content merges instruction + context_summary
        assert "You are Synth." in messages[0]["content"]
        assert "Diary: nothing today." in messages[0]["content"]

        # Past turns appear in order
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "Hello!"
        assert messages[2]["role"] == "assistant"

        # Last message is the current user turn
        assert messages[-1]["role"] == "user"
        assert "How are you?" in messages[-1]["content"]

    def test_runtime_prefix_injected_in_current_turn(self) -> None:
        req = _basic_request()
        renderer = OpenAIRenderer(req)
        messages = renderer.render()
        last = messages[-1]["content"]
        # Runtime context prefix should appear before the text
        assert "from:Tester" in last or "Tester" in last

    def test_runtime_prefix_omits_exact_timestamp_from_current_turn(self) -> None:
        req = _basic_request()
        req.runtime_ctx.timestamp = "2026-04-20 17:43 CEST (15:43 UTC)"
        renderer = OpenAIRenderer(req)

        messages = renderer.render()

        last = messages[-1]["content"]
        assert "2026-04-20 17:43 CEST" not in last
        assert "lang:en" in last
        assert "from:Tester" in last

    def test_no_tools_when_disabled(self) -> None:
        req = _basic_request(supports_tool_calling=False)
        req.tool_declarations = [_make_tool()]
        renderer = OpenAIRenderer(req)
        assert renderer.tool_schemas() == []

    def test_tool_schemas_when_enabled(self) -> None:
        req = _basic_request(supports_tool_calling=True)
        req.tool_declarations = [_make_tool("send_message", "Send a reply")]
        renderer = OpenAIRenderer(req)
        schemas = renderer.tool_schemas()

        assert len(schemas) == 1
        schema = schemas[0]
        assert schema["type"] == "function"
        assert schema["function"]["name"] == "send_message"
        assert "parameters" in schema["function"]

    def test_multimodal_replaces_last_user_turn(self) -> None:
        req = _basic_request()
        renderer = OpenAIRenderer(req)
        image_part = {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,abc"},
        }
        messages = renderer.render_with_multimodal([image_part])

        last = messages[-1]
        assert last["role"] == "user"
        assert isinstance(last["content"], list)
        types = [p.get("type") for p in last["content"]]
        assert "image_url" in types
        assert "text" in types

    def test_multimodal_image_only_turn_adds_grounding_text(self) -> None:
        req = _basic_request()
        req.current_text = ""
        renderer = OpenAIRenderer(req)
        image_part = {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,abc"},
        }

        messages = renderer.render_with_multimodal([image_part])

        last = messages[-1]
        text_part = next(part for part in last["content"] if part.get("type") == "text")
        assert "attached 1 image" in text_part["text"]
        assert "no accompanying text" in text_part["text"]
        assert (
            "Do not infer hidden, obscured, or non-visible features"
            in text_part["text"]
        )

    def test_multimodal_document_turn_adds_note_without_forwarding_binary(self) -> None:
        req = _basic_request(current_text="Can you inspect this manual?")
        renderer = OpenAIRenderer(req)
        document_part = {
            "type": "document",
            "document": {
                "mime_type": "application/pdf",
                "filename": "manual.pdf",
            },
        }

        messages = renderer.render_with_multimodal([document_part])

        last = messages[-1]
        assert last["role"] == "user"
        assert isinstance(last["content"], list)
        assert [part.get("type") for part in last["content"]] == ["text"]
        assert "manual.pdf" in last["content"][0]["text"]
        assert "did not forward the raw document binary" in last["content"][0]["text"]

    def test_multimodal_document_turn_embeds_extracted_text(self) -> None:
        req = _basic_request(current_text="Summarize the attached manual.")
        renderer = OpenAIRenderer(req)
        document_part = {
            "type": "document",
            "document": {
                "mime_type": "application/pdf",
                "filename": "manual.pdf",
                "extracted_text": "[Page 1]\nMotor spec page",
            },
        }

        messages = renderer.render_with_multimodal([document_part])

        last = messages[-1]
        assert last["role"] == "user"
        assert isinstance(last["content"], list)
        assert [part.get("type") for part in last["content"]] == ["text"]
        assert "extracted text from the attachment" in last["content"][0]["text"]
        assert "Motor spec page" in last["content"][0]["text"]

    def test_parse_tool_call_response_returns_action_json(self) -> None:
        data = {
            "choices": [
                {
                    "message": {
                        "tool_calls": [
                            {
                                "function": {
                                    "name": "send_message",
                                    "arguments": '{"text": "Hello!"}',
                                }
                            }
                        ]
                    }
                }
            ]
        }
        result = OpenAIRenderer.parse_tool_call_response(data)
        parsed = json.loads(result)
        assert "actions" in parsed
        assert parsed["actions"][0]["type"] == "send_message"
        assert parsed["actions"][0]["payload"]["text"] == "Hello!"

    def test_parse_tool_call_response_plain_text_passthrough(self) -> None:
        data = {
            "choices": [
                {"message": {"content": "Just a text reply", "tool_calls": None}}
            ]
        }
        result = OpenAIRenderer.parse_tool_call_response(data)
        assert result == "Just a text reply"

    def test_no_history_renders_only_system_and_current(self) -> None:
        req = _basic_request()
        req.conversation_history = []
        renderer = OpenAIRenderer(req)
        messages = renderer.render()
        # Only system + current user
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"


# ---------------------------------------------------------------------------
# AnthropicRenderer
# ---------------------------------------------------------------------------


class TestAnthropicRenderer:
    def test_render_returns_system_and_messages(self) -> None:
        req = _basic_request()
        renderer = AnthropicRenderer(req)
        result = renderer.render()

        assert "system" in result
        assert "messages" in result

    def test_system_is_list_of_blocks(self) -> None:
        req = _basic_request()
        renderer = AnthropicRenderer(req, enable_caching=False)
        result = renderer.render()

        system = result["system"]
        assert isinstance(system, list)
        for block in system:
            assert "type" in block
            assert "text" in block

    def test_cache_control_on_first_system_block_when_caching_enabled(self) -> None:
        req = _basic_request()
        renderer = AnthropicRenderer(req, enable_caching=True)
        result = renderer.render()

        first_block = result["system"][0]
        assert "cache_control" in first_block
        assert first_block["cache_control"] == {"type": "ephemeral"}

    def test_no_cache_control_when_caching_disabled(self) -> None:
        req = _basic_request()
        renderer = AnthropicRenderer(req, enable_caching=False)
        result = renderer.render()

        first_block = result["system"][0]
        assert "cache_control" not in first_block

    def test_context_summary_in_second_system_block_no_cache(self) -> None:
        req = _basic_request()
        renderer = AnthropicRenderer(req, enable_caching=True)
        result = renderer.render()

        # Second block (context summary) must NOT have cache_control
        assert len(result["system"]) >= 2
        second = result["system"][1]
        assert "Diary: nothing today." in second["text"]
        assert "cache_control" not in second

    def test_messages_alternate_roles(self) -> None:
        req = _basic_request()
        renderer = AnthropicRenderer(req)
        result = renderer.render()

        messages = result["messages"]
        assert messages[0]["role"] == "user"
        assert messages[1]["role"] == "assistant"
        # Last is current user turn
        assert messages[-1]["role"] == "user"

    def test_tools_included_when_tool_calling_enabled(self) -> None:
        req = _basic_request(supports_tool_calling=True)
        req.tool_declarations = [_make_tool()]
        renderer = AnthropicRenderer(req)
        result = renderer.render()

        assert "tools" in result
        assert result["tools"][0]["name"] == "send_message"
        assert "input_schema" in result["tools"][0]

    def test_no_tools_when_tool_calling_disabled(self) -> None:
        req = _basic_request(supports_tool_calling=False)
        req.tool_declarations = [_make_tool()]
        renderer = AnthropicRenderer(req)
        result = renderer.render()

        assert "tools" not in result

    def test_render_with_image_parts_upgrades_last_user_turn(self) -> None:
        req = _basic_request()
        renderer = AnthropicRenderer(req)
        img = {
            "type": "image",
            "source": {"type": "base64", "media_type": "image/png", "data": "abc"},
        }
        result = renderer.render_with_image_parts([img])

        last_msg = result["messages"][-1]
        assert isinstance(last_msg["content"], list)
        types = [p["type"] for p in last_msg["content"]]
        assert "image" in types
        assert "text" in types


# ---------------------------------------------------------------------------
# GeminiRenderer
# ---------------------------------------------------------------------------


class TestGeminiRenderer:
    def test_render_returns_required_keys(self) -> None:
        req = _basic_request()
        renderer = GeminiRenderer(req)
        result = renderer.render()

        assert "system_instruction_text" in result
        assert "contents" in result

    def test_system_instruction_text_present(self) -> None:
        req = _basic_request()
        renderer = GeminiRenderer(req)
        result = renderer.render()

        assert "You are Synth." in result["system_instruction_text"]

    def test_assistant_turns_use_model_role(self) -> None:
        req = _basic_request()
        renderer = GeminiRenderer(req)
        result = renderer.render()

        contents = result["contents"]
        roles = [c["role"] for c in contents]
        # The assistant turn in history should map to "model"
        assert "model" in roles
        assert "user" in roles

    def test_current_turn_appended_as_user(self) -> None:
        req = _basic_request()
        renderer = GeminiRenderer(req)
        result = renderer.render()

        last = result["contents"][-1]
        assert last["role"] == "user"
        # Text part should contain current message
        text_parts = [p.get("text", "") for p in last.get("parts", []) if "text" in p]
        combined = " ".join(text_parts)
        assert "How are you?" in combined

    def test_tools_key_present_when_tool_calling_enabled(self) -> None:
        req = _basic_request(supports_tool_calling=True)
        req.tool_declarations = [_make_tool()]
        renderer = GeminiRenderer(req)
        result = renderer.render()

        assert "tools" in result
        decls = result["tools"][0].get("function_declarations", [])
        assert len(decls) == 1
        assert decls[0]["name"] == "send_message"

    def test_tools_built_from_normalized_actions_preserve_parameters(self) -> None:
        actions = {
            "message_telegram_bot": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "Field: text",
                        },
                        "interface_path": {
                            "type": "string",
                            "description": "Field: interface_path",
                        },
                    },
                    "required": ["text", "interface_path"],
                },
                "brief": "Send a text message via Telegram",
                "source": "message_plugin, telegram_bot",
            }
        }

        req = _basic_request(supports_tool_calling=True)
        req.tool_declarations = LiveToolRegistry.build_manifests_from_actions(actions)
        renderer = GeminiRenderer(req)
        result = renderer.render()

        decls = result["tools"][0].get("function_declarations", [])
        assert len(decls) == 1
        params = decls[0]["parameters"]
        assert params["properties"]["text"]["type"] == "STRING"
        assert params["properties"]["interface_path"]["type"] == "STRING"
        assert sorted(params["required"]) == ["interface_path", "text"]

    def test_tools_built_from_normalized_actions_preserve_array_items(self) -> None:
        actions = {
            "create_personal_diary_entry": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "interaction_summary": {
                            "type": "string",
                            "description": "Brief summary",
                        },
                        "emotions": {
                            "type": "array",
                            "description": "Emotion entries",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "type": {"type": "string"},
                                    "intensity": {"type": "number"},
                                },
                                "required": ["type", "intensity"],
                            },
                        },
                        "context_tags": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "involved_users": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["interaction_summary"],
                },
                "brief": "Add a diary entry.",
                "source": "ai_diary",
            }
        }

        req = _basic_request(supports_tool_calling=True)
        req.tool_declarations = LiveToolRegistry.build_manifests_from_actions(actions)
        renderer = GeminiRenderer(req)
        result = renderer.render()

        params = result["tools"][0]["function_declarations"][0]["parameters"]
        emotions = params["properties"]["emotions"]
        assert emotions["type"] == "ARRAY"
        assert emotions["items"]["type"] == "OBJECT"
        assert emotions["items"]["properties"]["type"]["type"] == "STRING"
        assert emotions["items"]["properties"]["intensity"]["type"] == "NUMBER"
        assert params["properties"]["context_tags"]["items"]["type"] == "STRING"
        assert params["properties"]["involved_users"]["items"]["type"] == "STRING"

    def test_tools_built_from_normalized_actions_default_missing_array_items(
        self,
    ) -> None:
        actions = {
            "create_personal_diary_entry": {
                "schema": {
                    "type": "object",
                    "properties": {
                        "context_tags": {
                            "type": "array",
                            "description": "Tags for topics discussed (optional)",
                        },
                        "meta_blob": {
                            "type": "object",
                            "description": "Optional nested object.",
                        },
                    },
                    "required": [],
                },
                "brief": "Add a diary entry.",
                "source": "ai_diary",
            }
        }

        req = _basic_request(supports_tool_calling=True)
        req.tool_declarations = LiveToolRegistry.build_manifests_from_actions(actions)
        renderer = GeminiRenderer(req)
        result = renderer.render()

        params = result["tools"][0]["function_declarations"][0]["parameters"]
        assert params["properties"]["context_tags"]["type"] == "ARRAY"
        assert params["properties"]["context_tags"]["items"]["type"] == "STRING"
        assert params["properties"]["meta_blob"]["type"] == "OBJECT"
        assert params["properties"]["meta_blob"]["properties"] == {}

    def test_no_tools_key_when_disabled(self) -> None:
        req = _basic_request(supports_tool_calling=False)
        req.tool_declarations = [_make_tool()]
        renderer = GeminiRenderer(req)
        result = renderer.render()

        assert "tools" not in result

    def test_parse_function_call_response_extracts_actions(self) -> None:
        data = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "functionCall": {
                                    "name": "send_message",
                                    "args": {"text": "Hi!"},
                                }
                            }
                        ]
                    }
                }
            ]
        }
        result = GeminiRenderer.parse_function_call_response(data)
        parsed = json.loads(result)
        assert "actions" in parsed
        assert parsed["actions"][0]["type"] == "send_message"
        assert parsed["actions"][0]["payload"]["text"] == "Hi!"


# ---------------------------------------------------------------------------
# TextRenderer
# ---------------------------------------------------------------------------


class TestTextRenderer:
    def test_render_returns_string(self) -> None:
        req = _basic_request()
        renderer = TextRenderer(req)
        result = renderer.render()

        assert isinstance(result, str)

    def test_render_contains_current_text(self) -> None:
        req = _basic_request()
        renderer = TextRenderer(req)
        result = renderer.render()

        assert "How are you?" in result

    def test_render_contains_system_instruction(self) -> None:
        req = _basic_request()
        renderer = TextRenderer(req)
        result = renderer.render()

        assert "You are Synth." in result

    def test_render_is_not_indented_json(self) -> None:
        """TextRenderer must NOT produce json.dumps(..., indent=2) blobs."""
        req = _basic_request()
        renderer = TextRenderer(req)
        result = renderer.render()

        # A json.dumps(..., indent=2) blob would have lines starting with spaces
        # and double-quoted keys; plain text output is flat prose.
        assert '    "' not in result, "Output looks like indented JSON — use flat text"
