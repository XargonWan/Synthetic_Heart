import sys
import types
from types import SimpleNamespace
from typing import Any, cast

import pytest

from core.external_endpoints.adapters.anthropic_adapter import AnthropicAdapter
from core.external_endpoints.adapters.base import ModelInfo
from core.external_endpoints.adapters.gemini_adapter import GeminiAdapter
from core.external_endpoints.adapters.openai_compat import OpenAICompatAdapter
from core.external_endpoints.models import EndpointProtocol, ExternalEndpoint


class FakeAiohttpResponse:
    def __init__(
        self, status: int, payload: Any = None, body: bytes | None = None
    ) -> None:
        self.status = status
        self._payload = payload
        self._body = body or b""

    async def json(self, *args: Any, **kwargs: Any) -> Any:
        return self._payload

    async def text(self, *args: Any, **kwargs: Any) -> str:
        return str(self._payload)

    async def read(self, *args: Any, **kwargs: Any) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakeAiohttpSession:
    def __init__(self):
        self.calls: list[str] = []
        self.request_kwargs: list[dict[str, Any]] = []
        self.responses: list[FakeAiohttpResponse] = []

    def get(self, url: str, *args, **kwargs):
        self.calls.append(url)
        self.request_kwargs.append(kwargs)
        return self.responses.pop(0)

    def post(self, url: str, *args, **kwargs):
        self.calls.append(url)
        self.request_kwargs.append(kwargs)
        return self.responses.pop(0)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.mark.asyncio
async def test_openai_compat_list_models_from_dict_response(monkeypatch):
    data = [
        {"id": "chatgpt", "object": "model", "owned_by": "selenium-llm-engine"},
        {"id": "claude", "object": "model", "owned_by": "selenium-llm-engine"},
    ]
    adapter = OpenAICompatAdapter(base_url="http://localhost:14848", api_key="x")

    session = FakeAiohttpSession()
    session.responses = [FakeAiohttpResponse(status=200, payload={"data": data})]

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)

    models = await adapter.list_models()

    assert isinstance(models, list)
    assert len(models) == 2
    assert models[0].id == "chatgpt"
    assert models[1].id == "claude"
    assert session.calls == ["http://localhost:14848/v1/models"]


@pytest.mark.asyncio
async def test_openai_compat_list_models_fallback_name(monkeypatch):
    data = [{"id": "grok", "object": "model", "name": "Grok"}]
    adapter = OpenAICompatAdapter(base_url="http://localhost:14848", api_key="x")

    session = FakeAiohttpSession()
    session.responses = [FakeAiohttpResponse(status=200, payload={"data": data})]

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)

    models = await adapter.list_models()

    assert len(models) == 1
    assert models[0].name == "Grok"
    assert session.calls == ["http://localhost:14848/v1/models"]


@pytest.mark.asyncio
async def test_openai_compat_list_models_prefers_v1_path(monkeypatch):
    data = [{"id": "qwen", "object": "model", "name": "Qwen"}]
    adapter = OpenAICompatAdapter(base_url="http://localhost:14848", api_key="x")

    session = FakeAiohttpSession()
    session.responses = [FakeAiohttpResponse(status=200, payload={"data": data})]

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)

    models = await adapter.list_models()

    assert all(call.endswith("/v1/models") for call in session.calls)
    assert models[0].id == "qwen"


@pytest.mark.asyncio
async def test_openai_compat_http_chat_urls_include_api_v1_paths():
    adapter = OpenAICompatAdapter(base_url="http://localhost:14848")
    urls = adapter._http_chat_urls()

    assert any(url.endswith("/v1/chat/completions") for url in urls)
    assert any(url.endswith("/v1/chat") for url in urls)
    assert any(url.endswith("/api/v1/chat/completions") for url in urls)
    assert any(url.endswith("/api/v1/chat") for url in urls)


@pytest.mark.asyncio
async def test_openai_compat_list_models_uses_adapter_timeout(monkeypatch):
    data = [{"id": "grok", "object": "model", "name": "Grok"}]
    adapter = OpenAICompatAdapter(
        base_url="http://localhost:14848", api_key="x", timeout=300.0
    )

    session = FakeAiohttpSession()
    session.responses = [FakeAiohttpResponse(status=200, payload={"data": data})]

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)

    models = await adapter.list_models()

    assert len(models) == 1
    timeout = session.request_kwargs[0]["timeout"]
    assert timeout.total == 300.0
    assert timeout.connect == 300.0
    assert timeout.sock_connect == 300.0
    assert timeout.sock_read == 300.0


@pytest.mark.asyncio
async def test_openai_compat_probe_capabilities_reads_model_capabilities(monkeypatch):
    adapter = OpenAICompatAdapter(base_url="http://localhost:14848", api_key="x")

    session = FakeAiohttpSession()
    session.responses = [
        FakeAiohttpResponse(
            status=200,
            payload={
                "data": [
                    {
                        "id": "vision-model",
                        "object": "model",
                        "capabilities": {"vision": True},
                    }
                ]
            },
        ),
        FakeAiohttpResponse(status=404, payload={}, body=b""),
        FakeAiohttpResponse(status=404, payload={}, body=b""),
    ]

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)

    caps = await adapter.probe_capabilities()

    assert caps["vision"] is True
    assert caps["auris"] is False
    assert caps["vox"] is False


@pytest.mark.asyncio
async def test_openai_compat_probe_capabilities_probes_image_support(monkeypatch):
    adapter = OpenAICompatAdapter(base_url="http://localhost:14848", api_key="x")

    session = FakeAiohttpSession()
    session.responses = [
        FakeAiohttpResponse(
            status=200, payload={"data": [{"id": "chat-only", "object": "model"}]}
        ),
        FakeAiohttpResponse(
            status=200, payload={"choices": [{"message": {"content": "ok"}}]}
        ),
        FakeAiohttpResponse(status=404, payload={}, body=b""),
        FakeAiohttpResponse(status=404, payload={}, body=b""),
    ]

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)

    caps = await adapter.probe_capabilities()

    assert caps["vision"] is True
    assert caps["auris"] is False
    assert caps["vox"] is False
    assert session.request_kwargs[1]["json"]["model"] == "chat-only"


@pytest.mark.asyncio
async def test_openai_compat_probe_capabilities_detects_vision_for_gemini_flash(
    monkeypatch,
):
    adapter = OpenAICompatAdapter(base_url="http://localhost:14848", api_key="x")

    session = FakeAiohttpSession()
    session.responses = [
        FakeAiohttpResponse(
            status=200,
            payload={"data": [{"id": "gemini-2.5-flash", "object": "model"}]},
        ),
        FakeAiohttpResponse(
            status=200, payload={"choices": [{"message": {"content": "ok"}}]}
        ),
        FakeAiohttpResponse(status=404, payload={}, body=b""),
        FakeAiohttpResponse(status=404, payload={}, body=b""),
    ]

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)

    caps = await adapter.probe_capabilities()

    assert caps["vision"] is True
    assert caps["auris"] is False
    assert caps["vox"] is False


@pytest.mark.asyncio
async def test_openai_compat_probe_capabilities_tries_each_model_id(monkeypatch):
    adapter = OpenAICompatAdapter(base_url="http://localhost:14848", api_key="x")

    async def fake_list_models():
        return [
            ModelInfo(id="text-only", name="TextOnly"),
            ModelInfo(id="vision-model", name="VisionModel"),
        ]

    called_models: list[str | None] = []

    async def fake_probe_vision_support(model: str | None = None) -> bool:
        called_models.append(model)
        return model == "vision-model"

    monkeypatch.setattr(adapter, "list_models", fake_list_models)
    monkeypatch.setattr(adapter, "_probe_vision_support", fake_probe_vision_support)

    caps = await adapter.probe_capabilities()

    assert caps["vision"] is True
    assert called_models == ["text-only", "vision-model"]


# ---------------------------------------------------------------------------
# ping_test
# ---------------------------------------------------------------------------


class FakePingResponse:
    def __init__(self, status: int, body: dict) -> None:
        self.status = status
        self._body = body

    async def json(self) -> dict:
        return self._body

    async def text(self) -> str:
        return str(self._body)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


class FakePingSession:
    def __init__(self, response: FakePingResponse) -> None:
        self._response = response
        self.last_kwargs: dict[str, Any] | None = None

    def post(self, *args, **kwargs):
        self.last_kwargs = kwargs
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass


@pytest.mark.asyncio
async def test_openai_compat_ping_test_success(monkeypatch):
    body = {"choices": [{"message": {"content": "pong"}}]}
    adapter = OpenAICompatAdapter(base_url="http://fake-host", api_key="x")

    fake_resp = FakePingResponse(status=200, body=body)
    fake_session = FakePingSession(fake_resp)

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: fake_session)

    ok, echo = await adapter.ping_test(model="test-model")

    assert ok is True
    assert echo == "pong"


class FakeOpenAIChatCompletions:
    async def create(self, model, messages, stream, extra_body=None, **kwargs):
        class Message:
            def __init__(self):
                self.content = ""
                self.reasoning_content = 'Okay, the user just typed "ping".'

        class Choice:
            def __init__(self):
                self.message = Message()
                self.finish_reason = "length"

        class Response:
            def __init__(self):
                self.choices = [Choice()]
                self.model = model
                self.usage = None

        return Response()


class FakeOpenAIClient:
    def __init__(self):
        self.chat = SimpleNamespace(completions=FakeOpenAIChatCompletions())


@pytest.mark.asyncio
async def test_openai_compat_chat_completion_uses_reasoning_content(monkeypatch):
    adapter = OpenAICompatAdapter(base_url="http://fake-host", api_key="x")
    fake_client = FakeOpenAIClient()
    monkeypatch.setattr(adapter, "_get_client", lambda: fake_client)

    response = await adapter.chat_completion(
        [{"role": "user", "content": "ping"}], model="qwen/qwen3.5-9b"
    )

    assert response.content == 'Okay, the user just typed "ping".'
    assert response.model == "qwen/qwen3.5-9b"


@pytest.mark.asyncio
async def test_openai_compat_chat_completion_logs_tool_payload(monkeypatch):
    adapter = OpenAICompatAdapter(base_url="http://fake-host", api_key="x")
    captured: dict[str, Any] = {}

    class FakeChoice:
        def __init__(self) -> None:
            self.message = SimpleNamespace(content='{"actions": []}')
            self.finish_reason = "stop"

    class FakeResponse:
        def __init__(self) -> None:
            self.choices = [FakeChoice()]
            self.model = "qwen/qwen3.5-9b"
            self.usage = None

    class FakeCompletions:
        async def create(self, **kwargs: Any) -> FakeResponse:
            return FakeResponse()

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    monkeypatch.setattr(adapter, "_get_client", lambda: fake_client)

    monkeypatch.setattr(
        "core.external_endpoints.adapters.openai_compat.log_cortex_response",
        lambda *args, **kwargs: None,
    )

    def _capture_request(*args: Any, **kwargs: Any) -> None:
        captured["payload"] = kwargs.get("payload")

    monkeypatch.setattr(
        "core.external_endpoints.adapters.openai_compat.log_cortex_request",
        _capture_request,
    )

    tools = [
        {
            "type": "function",
            "function": {
                "name": "send_message",
                "description": "Send a reply",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]
    tool_choice = {"type": "function", "function": {"name": "send_message"}}

    response = await adapter.chat_completion(
        [{"role": "user", "content": "ping"}],
        model="qwen/qwen3.5-9b",
        tools=tools,
        tool_choice=tool_choice,
    )

    assert response.content == '{"actions": []}'
    assert captured["payload"]["tools"] == tools
    assert captured["payload"]["tool_choice"] == tool_choice


@pytest.mark.asyncio
async def test_openai_compat_chat_completion_parses_native_tool_calls(monkeypatch):
    adapter = OpenAICompatAdapter(base_url="http://fake-host", api_key="x")

    class FakeFunction:
        def __init__(self) -> None:
            self.name = "send_message"
            self.arguments = '{"text":"hello from native tool"}'

    class FakeToolCall:
        def __init__(self) -> None:
            self.id = "tc_1"
            self.type = "function"
            self.function = FakeFunction()

    class FakeChoice:
        def __init__(self) -> None:
            self.message = SimpleNamespace(content=None, tool_calls=[FakeToolCall()])
            self.finish_reason = "tool_calls"

    class FakeResponse:
        def __init__(self) -> None:
            self.choices = [FakeChoice()]
            self.model = "qwen/qwen3.5-9b"
            self.usage = None

    class FakeCompletions:
        async def create(self, **kwargs: Any) -> FakeResponse:
            return FakeResponse()

    fake_client = SimpleNamespace(chat=SimpleNamespace(completions=FakeCompletions()))
    monkeypatch.setattr(adapter, "_get_client", lambda: fake_client)
    monkeypatch.setattr(
        "core.external_endpoints.adapters.openai_compat.log_cortex_response",
        lambda *args, **kwargs: None,
    )

    response = await adapter.chat_completion(
        [{"role": "user", "content": "ping"}], model="qwen/qwen3.5-9b"
    )

    assert response.finish_reason == "tool_call"
    assert response.content == (
        '{"actions": [{"type": "send_message", '
        '"payload": {"text": "hello from native tool"}}]}'
    )


@pytest.mark.asyncio
async def test_external_cortex_engine_falls_back_to_first_available_model(monkeypatch):
    from core.external_endpoints.bridges.cortex_bridge import ExternalCortexEngine

    endpoint = ExternalEndpoint(
        id=1,
        name="lmstudio",
        display_label="LM Studio",
        protocol=EndpointProtocol.OPENAI,
        base_url="http://localhost:1234",
        api_key_enc=None,
        enabled=True,
        capabilities={},
        subsystem_map={"cortex": True},
        available_models=["qwen/qwen3.5-9b"],
        default_model=None,
        probe_status="success",
        last_probe_at=None,
        extra_config={},
    )
    called: dict[str, Any] = {}

    class FakeAdapter:
        async def chat_completion(self, messages, model=None, **kwargs):
            called["model"] = model
            return SimpleNamespace(content="ok", model=model)

    engine = ExternalCortexEngine(endpoint, cast(Any, FakeAdapter()))
    response = await engine.generate_response([{"role": "user", "content": "ping"}])

    assert response == "ok"
    assert called["model"] == "qwen/qwen3.5-9b"


@pytest.mark.asyncio
async def test_gemini_adapter_chat_completion_logs_usage_metadata(monkeypatch):
    class FakeGenerateContentConfig:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class FakeSafetySetting:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    fake_types = SimpleNamespace(
        GenerateContentConfig=FakeGenerateContentConfig,
        SafetySetting=FakeSafetySetting,
        HarmCategory=SimpleNamespace(
            HARM_CATEGORY_HARASSMENT="harassment",
            HARM_CATEGORY_HATE_SPEECH="hate",
            HARM_CATEGORY_SEXUALLY_EXPLICIT="sex",
            HARM_CATEGORY_DANGEROUS_CONTENT="danger",
        ),
        HarmBlockThreshold=SimpleNamespace(OFF="OFF"),
    )
    fake_google = cast(Any, types.ModuleType("google"))
    fake_genai = cast(Any, types.ModuleType("google.genai"))
    fake_genai.types = fake_types
    fake_google.genai = fake_genai
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)

    captured: dict[str, Any] = {}

    class FakeResponse:
        def __init__(self) -> None:
            self.text = '{"actions": []}'
            self.prompt_feedback = None
            self.candidates = []
            self.usage_metadata = SimpleNamespace(
                prompt_token_count=123,
                candidates_token_count=45,
                total_token_count=168,
                cached_content_token_count=12,
            )

    class FakeModels:
        def generate_content(self, **kwargs: Any) -> FakeResponse:
            captured["generate_kwargs"] = kwargs
            return FakeResponse()

    class FakeClient:
        def __init__(self) -> None:
            self.models = FakeModels()

    adapter = GeminiAdapter(api_key="unused")
    monkeypatch.setattr(adapter, "_get_client", lambda: FakeClient())
    monkeypatch.setattr(
        "core.external_endpoints.adapters.gemini_adapter.log_cortex_request",
        lambda *args, **kwargs: None,
    )

    def _capture_response(*args: Any, **kwargs: Any) -> None:
        captured["log_args"] = args
        captured["log_kwargs"] = kwargs

    monkeypatch.setattr(
        "core.external_endpoints.adapters.gemini_adapter.log_cortex_response",
        _capture_response,
    )

    response = await adapter.chat_completion(
        [{"role": "user", "content": "ping"}],
        model="gemini-3.1-flash-lite-preview",
    )

    assert response.content == '{"actions": []}'
    assert response.model == "gemini-3.1-flash-lite-preview"
    assert captured["log_args"][0] == "gemini:default"
    assert captured["log_kwargs"]["usage"] == {
        "prompt_tokens": 123,
        "completion_tokens": 45,
        "total_tokens": 168,
        "cache_read_input_tokens": 12,
    }


@pytest.mark.asyncio
async def test_gemini_adapter_forwards_tools_and_parses_function_calls(monkeypatch):
    class FakeGenerateContentConfig:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class FakeSafetySetting:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class FakeFunctionDeclaration:
        def __init__(self, **kwargs: Any) -> None:
            self.name = kwargs["name"]
            self.description = kwargs.get("description", "")
            self.parameters = kwargs.get("parameters")

    class FakeTool:
        def __init__(self, **kwargs: Any) -> None:
            self.function_declarations = kwargs.get("function_declarations", [])

    fake_types = SimpleNamespace(
        GenerateContentConfig=FakeGenerateContentConfig,
        SafetySetting=FakeSafetySetting,
        FunctionDeclaration=FakeFunctionDeclaration,
        Tool=FakeTool,
        HarmCategory=SimpleNamespace(
            HARM_CATEGORY_HARASSMENT="harassment",
            HARM_CATEGORY_HATE_SPEECH="hate",
            HARM_CATEGORY_SEXUALLY_EXPLICIT="sex",
            HARM_CATEGORY_DANGEROUS_CONTENT="danger",
        ),
        HarmBlockThreshold=SimpleNamespace(OFF="OFF"),
    )
    fake_google = cast(Any, types.ModuleType("google"))
    fake_genai = cast(Any, types.ModuleType("google.genai"))
    fake_genai.types = fake_types
    fake_google.genai = fake_genai
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)

    captured: dict[str, Any] = {}

    class FakeFunctionCall:
        def __init__(self, name: str, args: dict[str, Any]) -> None:
            self.name = name
            self.args = args

    class FakePart:
        def __init__(self) -> None:
            self.function_call = FakeFunctionCall(
                "send_message", {"text": "hello from tool call"}
            )

    class FakeContent:
        def __init__(self) -> None:
            self.parts = [FakePart()]

    class FakeCandidate:
        def __init__(self) -> None:
            self.finish_reason = None
            self.content = FakeContent()

    class FakeResponse:
        def __init__(self) -> None:
            self.text = ""
            self.prompt_feedback = None
            self.candidates = [FakeCandidate()]
            self.usage_metadata = None

    class FakeModels:
        def generate_content(self, **kwargs: Any) -> FakeResponse:
            captured["generate_kwargs"] = kwargs
            return FakeResponse()

    class FakeClient:
        def __init__(self) -> None:
            self.models = FakeModels()

    adapter = GeminiAdapter(api_key="unused")
    monkeypatch.setattr(adapter, "_get_client", lambda: FakeClient())

    def _capture_request(*args: Any, **kwargs: Any) -> None:
        captured["request_payload"] = kwargs.get("payload")

    monkeypatch.setattr(
        "core.external_endpoints.adapters.gemini_adapter.log_cortex_request",
        _capture_request,
    )
    monkeypatch.setattr(
        "core.external_endpoints.adapters.gemini_adapter.log_cortex_response",
        lambda *args, **kwargs: None,
    )

    raw_tools = [
        {
            "function_declarations": [
                {
                    "name": "send_message",
                    "description": "Send a reply",
                    "parameters": {
                        "type": "OBJECT",
                        "properties": {"text": {"type": "STRING"}},
                        "required": ["text"],
                    },
                }
            ]
        }
    ]

    response = await adapter.chat_completion(
        [{"role": "user", "content": "ping"}],
        model="gemini-3.1-flash-lite-preview",
        tools=raw_tools,
    )

    assert response.model == "gemini-3.1-flash-lite-preview"
    assert response.finish_reason == "tool_call"
    assert response.content == (
        '{"actions": [{"type": "send_message", '
        '"payload": {"text": "hello from tool call"}}]}'
    )
    assert captured["request_payload"]["tools"] == raw_tools

    config = captured["generate_kwargs"]["config"]
    assert isinstance(config, FakeGenerateContentConfig)
    sdk_tools = config.kwargs["tools"]
    assert len(sdk_tools) == 1
    assert isinstance(sdk_tools[0], FakeTool)
    assert sdk_tools[0].function_declarations[0].name == "send_message"


@pytest.mark.asyncio
async def test_gemini_adapter_preserves_provider_error_status(monkeypatch):
    class FakeGenerateContentConfig:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    class FakeSafetySetting:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

    fake_types = SimpleNamespace(
        GenerateContentConfig=FakeGenerateContentConfig,
        SafetySetting=FakeSafetySetting,
        HarmCategory=SimpleNamespace(
            HARM_CATEGORY_HARASSMENT="harassment",
            HARM_CATEGORY_HATE_SPEECH="hate",
            HARM_CATEGORY_SEXUALLY_EXPLICIT="sex",
            HARM_CATEGORY_DANGEROUS_CONTENT="danger",
        ),
        HarmBlockThreshold=SimpleNamespace(OFF="OFF"),
    )
    fake_google = cast(Any, types.ModuleType("google"))
    fake_genai = cast(Any, types.ModuleType("google.genai"))
    fake_genai.types = fake_types
    fake_google.genai = fake_genai
    monkeypatch.setitem(sys.modules, "google", fake_google)
    monkeypatch.setitem(sys.modules, "google.genai", fake_genai)

    captured: dict[str, Any] = {}

    class FakeGeminiError(Exception):
        def __init__(self) -> None:
            super().__init__(
                "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'status': 'RESOURCE_EXHAUSTED'}}"
            )
            self.code = 429
            self.details = [{"@type": "type.googleapis.com/google.rpc.RetryInfo"}]
            self.status = "RESOURCE_EXHAUSTED"

    class FakeModels:
        def generate_content(self, **kwargs: Any) -> Any:
            raise FakeGeminiError()

    class FakeClient:
        def __init__(self) -> None:
            self.models = FakeModels()

    adapter = GeminiAdapter(api_key="unused")
    monkeypatch.setattr(adapter, "_get_client", lambda: FakeClient())
    monkeypatch.setattr(
        "core.external_endpoints.adapters.gemini_adapter.log_cortex_request",
        lambda *args, **kwargs: None,
    )

    def _capture_response(*args: Any, **kwargs: Any) -> None:
        captured["log_args"] = args
        captured["log_kwargs"] = kwargs

    monkeypatch.setattr(
        "core.external_endpoints.adapters.gemini_adapter.log_cortex_response",
        _capture_response,
    )

    with pytest.raises(FakeGeminiError):
        await adapter.chat_completion(
            [{"role": "user", "content": "ping"}],
            model="gemini-3.1-flash-lite-preview",
        )

    assert captured["log_args"][0] == "gemini:default"
    assert captured["log_kwargs"]["status"] == 429
    assert captured["log_kwargs"]["body"] == {
        "status": "RESOURCE_EXHAUSTED",
        "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo"}],
    }


@pytest.mark.asyncio
async def test_external_cortex_engine_redacts_prompt_and_sends_multimodal_parts():
    from core.external_endpoints.bridges.cortex_bridge import ExternalCortexEngine

    endpoint = ExternalEndpoint(
        id=2,
        name="lmstudio",
        display_label="LM Studio",
        protocol=EndpointProtocol.OPENAI,
        base_url="http://localhost:1234",
        api_key_enc=None,
        enabled=True,
        capabilities={},
        subsystem_map={"cortex": True},
        available_models=["default"],
        default_model="default",
        probe_status="success",
        last_probe_at=None,
        extra_config={},
    )
    captured: dict[str, Any] = {}

    class FakeAdapter:
        async def chat_completion(self, messages, model=None, **kwargs):
            captured["messages"] = messages
            captured["model"] = model
            return SimpleNamespace(content="ok", model=model)

    engine = ExternalCortexEngine(endpoint, cast(Any, FakeAdapter()))
    prompt = {
        "instructions_verbose": "Use valid JSON.",
        "input": {
            "payload": {
                "text": "What is in this image?",
                "attachments": [
                    {
                        "mime_type": "image/png",
                        "filename": "img.png",
                        "data": "YWJjZA==",
                    }
                ],
            }
        },
    }

    response = await engine.handle_incoming_message(None, None, prompt)

    assert response == "ok"
    assert captured["model"] == "default"
    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][0]["content"] == "Use valid JSON."

    user_content = captured["messages"][1]["content"]
    assert isinstance(user_content, list)
    assert user_content[0]["type"] == "text"
    assert "<redacted: 8 chars>" in user_content[0]["text"]
    assert "YWJjZA==" not in user_content[0]["text"]
    assert user_content[1]["type"] == "image_url"
    assert user_content[1]["image_url"]["url"] == "data:image/png;base64,YWJjZA=="


@pytest.mark.asyncio
async def test_external_cortex_engine_forwards_openai_tool_declarations():
    from core.external_endpoints.bridges.cortex_bridge import ExternalCortexEngine
    from core.prompt_request import PromptRequest

    endpoint = ExternalEndpoint(
        id=31,
        name="openai-endpoint",
        display_label="OpenAI Endpoint",
        protocol=EndpointProtocol.OPENAI,
        base_url="https://example.com",
        api_key_enc=None,
        enabled=True,
        capabilities={"cortex": True},
        subsystem_map={"cortex": True},
        available_models=["qwen/qwen3.5-9b"],
        default_model="qwen/qwen3.5-9b",
        probe_status="success",
        last_probe_at=None,
        extra_config={},
    )
    captured: dict[str, Any] = {}

    class FakeAdapter:
        async def chat_completion(self, messages, model=None, **kwargs):
            captured["messages"] = messages
            captured["model"] = model
            captured["kwargs"] = kwargs
            return SimpleNamespace(content='{"actions": []}', model=model)

    manifest = SimpleNamespace(
        name="send_message",
        description="Send a reply",
        parameters=[
            SimpleNamespace(
                name="text",
                type="string",
                description="Reply text",
                enum=None,
                required=True,
            )
        ],
    )
    prompt = PromptRequest(
        system_instruction="Use valid JSON.",
        current_text="ping",
        tool_declarations=[manifest],
        mode="chat",
    )

    engine = ExternalCortexEngine(endpoint, cast(Any, FakeAdapter()))
    response = await engine.handle_incoming_message(None, None, prompt)

    assert response == '{"actions": []}'
    assert prompt.supports_tool_calling is True
    assert captured["kwargs"]["tool_choice"] == "auto"
    tools = captured["kwargs"]["tools"]
    assert len(tools) == 1
    assert tools[0]["type"] == "function"
    assert tools[0]["function"]["name"] == "send_message"


@pytest.mark.asyncio
async def test_anthropic_chat_completion_parses_tool_use_blocks(monkeypatch):
    adapter = AnthropicAdapter(api_key="test-key", base_url="https://anthropic.example")

    session = FakeAiohttpSession()
    session.responses = [
        FakeAiohttpResponse(
            status=200,
            payload={
                "model": "claude-sonnet-4-5",
                "content": [
                    {
                        "type": "tool_use",
                        "name": "send_message",
                        "input": {"text": "hello from anthropic tool"},
                    }
                ],
                "usage": {"input_tokens": 11, "output_tokens": 7},
                "stop_reason": "tool_use",
            },
        )
    ]

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)
    monkeypatch.setattr(
        "core.external_endpoints.adapters.anthropic_adapter.log_cortex_request",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "core.external_endpoints.adapters.anthropic_adapter.log_cortex_response",
        lambda *args, **kwargs: None,
    )

    tools = [
        {
            "name": "send_message",
            "description": "Send reply",
            "input_schema": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        }
    ]
    response = await adapter.chat_completion(
        [{"role": "user", "content": "ping"}],
        model="claude-sonnet-4-5",
        tools=tools,
        tool_choice={"type": "auto"},
    )

    assert response.finish_reason == "tool_use"
    assert response.content == (
        '{"actions": [{"type": "send_message", '
        '"payload": {"text": "hello from anthropic tool"}}]}'
    )
    posted_payload = session.request_kwargs[0]["json"]
    assert posted_payload["tools"] == tools
    assert posted_payload["tool_choice"] == {"type": "auto"}


@pytest.mark.asyncio
async def test_external_cortex_engine_forwards_anthropic_tool_declarations():
    from core.external_endpoints.bridges.cortex_bridge import ExternalCortexEngine
    from core.prompt_request import PromptRequest

    endpoint = ExternalEndpoint(
        id=32,
        name="anthropic-endpoint",
        display_label="Anthropic Endpoint",
        protocol=EndpointProtocol.ANTHROPIC,
        base_url="https://example.com",
        api_key_enc=None,
        enabled=True,
        capabilities={"cortex": True},
        subsystem_map={"cortex": True},
        available_models=["claude-sonnet-4-5"],
        default_model="claude-sonnet-4-5",
        probe_status="success",
        last_probe_at=None,
        extra_config={},
    )
    captured: dict[str, Any] = {}

    class FakeAdapter:
        async def chat_completion(self, messages, model=None, **kwargs):
            captured["kwargs"] = kwargs
            return SimpleNamespace(content='{"actions": []}', model=model)

    manifest = SimpleNamespace(
        name="send_message",
        description="Send a reply",
        parameters=[
            SimpleNamespace(
                name="text",
                type="string",
                description="Reply text",
                enum=None,
                required=True,
            )
        ],
    )
    prompt = PromptRequest(
        system_instruction="Use valid JSON.",
        current_text="ping",
        tool_declarations=[manifest],
        mode="chat",
    )

    engine = ExternalCortexEngine(endpoint, cast(Any, FakeAdapter()))
    await engine.handle_incoming_message(None, None, prompt)

    tools = captured["kwargs"]["tools"]
    assert len(tools) == 1
    assert tools[0]["name"] == "send_message"
    assert captured["kwargs"]["tool_choice"] == {"type": "auto"}


@pytest.mark.asyncio
async def test_external_cortex_engine_forwards_gemini_tool_declarations():
    from core.external_endpoints.bridges.cortex_bridge import ExternalCortexEngine
    from core.prompt_request import PromptRequest

    endpoint = ExternalEndpoint(
        id=3,
        name="gemini-endpoint",
        display_label="Gemini Endpoint",
        protocol=EndpointProtocol.GEMINI,
        base_url="https://generativelanguage.googleapis.com",
        api_key_enc=None,
        enabled=True,
        capabilities={"cortex": True},
        subsystem_map={"cortex": True},
        available_models=["gemini-3.1-flash-lite-preview"],
        default_model="gemini-3.1-flash-lite-preview",
        probe_status="success",
        last_probe_at=None,
        extra_config={},
    )
    captured: dict[str, Any] = {}

    class FakeAdapter:
        async def chat_completion(self, messages, model=None, **kwargs):
            captured["messages"] = messages
            captured["model"] = model
            captured["kwargs"] = kwargs
            return SimpleNamespace(content='{"actions": []}', model=model)

    manifest = SimpleNamespace(
        name="send_message",
        description="Send a reply",
        parameters=[
            SimpleNamespace(
                name="text",
                type="string",
                description="Reply text",
                enum=None,
                required=True,
            )
        ],
    )
    prompt = PromptRequest(
        system_instruction="Use valid JSON.",
        current_text="ping",
        tool_declarations=[manifest],
        mode="chat",
    )

    engine = ExternalCortexEngine(endpoint, cast(Any, FakeAdapter()))
    response = await engine.handle_incoming_message(None, None, prompt)

    assert response == '{"actions": []}'
    assert prompt.supports_tool_calling is True
    assert captured["model"] == "gemini-3.1-flash-lite-preview"
    assert captured["kwargs"]["timeout"] == 1800.0
    tools = captured["kwargs"]["tools"]
    assert len(tools) == 1
    declarations = tools[0]["function_declarations"]
    assert declarations[0]["name"] == "send_message"
    assert captured["messages"][0]["role"] == "system"
    assert captured["messages"][-1]["role"] == "user"


def _openai_endpoint(extra_config: dict) -> "ExternalEndpoint":
    return ExternalEndpoint(
        id=7,
        name="local-llama",
        display_label="Local llama.cpp",
        protocol=EndpointProtocol.OPENAI,
        base_url="http://127.0.0.1:8081/v1",
        api_key_enc=None,
        enabled=True,
        capabilities={"cortex": True},
        subsystem_map={"cortex": True},
        available_models=["local-model"],
        default_model="local-model",
        probe_status="success",
        last_probe_at=None,
        extra_config=extra_config,
    )


@pytest.mark.asyncio
async def test_force_json_object_sets_response_format():
    """force_json_object in extra_config asks the server for valid-JSON output."""
    from core.external_endpoints.bridges.cortex_bridge import ExternalCortexEngine
    from core.prompt_request import PromptRequest

    endpoint = _openai_endpoint({"force_json_object": True})
    captured: dict[str, Any] = {}

    class FakeAdapter:
        async def chat_completion(self, messages, model=None, **kwargs):
            captured["kwargs"] = kwargs
            return SimpleNamespace(content='{"actions": []}', model=model)

    # No tool declarations -> JSON-content path, response_format applies.
    prompt = PromptRequest(
        system_instruction="Use valid JSON.",
        current_text="ping",
        mode="chat",
    )
    engine = ExternalCortexEngine(endpoint, cast(Any, FakeAdapter()))
    await engine.handle_incoming_message(None, None, prompt)

    assert captured["kwargs"]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_response_format_dropped_when_tools_active():
    """response_format must not be sent alongside native tool-calling."""
    from core.external_endpoints.bridges.cortex_bridge import ExternalCortexEngine
    from core.prompt_request import PromptRequest

    endpoint = _openai_endpoint({"force_json_object": True})
    captured: dict[str, Any] = {}

    class FakeAdapter:
        async def chat_completion(self, messages, model=None, **kwargs):
            captured["kwargs"] = kwargs
            return SimpleNamespace(content='{"actions": []}', model=model)

    manifest = SimpleNamespace(
        name="send_message",
        description="Send a reply",
        parameters=[
            SimpleNamespace(
                name="text",
                type="string",
                description="Reply text",
                enum=None,
                required=True,
            )
        ],
    )
    prompt = PromptRequest(
        system_instruction="Use valid JSON.",
        current_text="ping",
        tool_declarations=[manifest],
        mode="chat",
    )
    engine = ExternalCortexEngine(endpoint, cast(Any, FakeAdapter()))
    await engine.handle_incoming_message(None, None, prompt)

    assert "tools" in captured["kwargs"]
    assert "response_format" not in captured["kwargs"]


def test_strip_thinking_handles_thought_and_dangling_close():
    """Reasoning leaks must be stripped: full blocks, <thought>, and a dangling
    closing tag (open tag dropped by the server)."""
    from core.external_endpoints.adapters.openai_compat import _strip_thinking

    assert _strip_thinking('<thought>reasoning</thought>{"a":1}') == '{"a":1}'
    assert _strip_thinking("<think>x</think>hi") == "hi"
    # open tag dropped: "reasoning … </thought>{json}"
    assert (
        _strip_thinking('reasoning here </thought>\n{"actions":[]}') == '{"actions":[]}'
    )
    # plain content is untouched
    assert _strip_thinking('{"actions":[]}') == '{"actions":[]}'


@pytest.mark.asyncio
async def test_extra_config_max_tokens_forwarded():
    """max_tokens in extra_config reaches the adapter (caps runaway generations)."""
    from core.external_endpoints.bridges.cortex_bridge import ExternalCortexEngine
    from core.prompt_request import PromptRequest

    endpoint = _openai_endpoint({"max_tokens": 1234})
    captured: dict[str, Any] = {}

    class FakeAdapter:
        async def chat_completion(self, messages, model=None, **kwargs):
            captured["kwargs"] = kwargs
            return SimpleNamespace(content='{"actions": []}', model=model)

    manifest = SimpleNamespace(
        name="send_message",
        description="Send a reply",
        parameters=[
            SimpleNamespace(
                name="text",
                type="string",
                description="Reply text",
                enum=None,
                required=True,
            )
        ],
    )
    prompt = PromptRequest(
        system_instruction="Use valid JSON.",
        current_text="ping",
        tool_declarations=[manifest],
        mode="chat",
    )
    engine = ExternalCortexEngine(endpoint, cast(Any, FakeAdapter()))
    await engine.handle_incoming_message(None, None, prompt)

    assert captured["kwargs"]["max_tokens"] == 1234


@pytest.mark.asyncio
async def test_disable_tools_uses_in_prompt_protocol():
    """disable_tools suppresses native tools and keeps the JSON-content protocol."""
    from core.external_endpoints.bridges.cortex_bridge import ExternalCortexEngine
    from core.prompt_request import PromptRequest

    endpoint = _openai_endpoint({"disable_tools": True, "force_json_object": True})
    captured: dict[str, Any] = {}

    class FakeAdapter:
        async def chat_completion(self, messages, model=None, **kwargs):
            captured["kwargs"] = kwargs
            return SimpleNamespace(content='{"actions": []}', model=model)

    manifest = SimpleNamespace(
        name="send_message",
        description="Send a reply",
        parameters=[
            SimpleNamespace(
                name="text",
                type="string",
                description="Reply text",
                enum=None,
                required=True,
            )
        ],
    )
    prompt = PromptRequest(
        system_instruction="Use valid JSON.",
        current_text="ping",
        tool_declarations=[manifest],
        mode="chat",
    )
    engine = ExternalCortexEngine(endpoint, cast(Any, FakeAdapter()))
    await engine.handle_incoming_message(None, None, prompt)

    # Native tools must be suppressed entirely…
    assert "tools" not in captured["kwargs"]
    assert "tool_choice" not in captured["kwargs"]
    # …and the renderer must stay on the content-JSON protocol (no tool_calls).
    assert prompt.supports_tool_calling is False
    # response_format survives because no tools are present to clash with it.
    assert captured["kwargs"]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_openai_compat_ping_test_http_error(monkeypatch):
    adapter = OpenAICompatAdapter(base_url="http://fake-host", api_key="x")

    fake_resp = FakePingResponse(status=500, body={})
    fake_session = FakePingSession(fake_resp)

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: fake_session)

    ok, echo = await adapter.ping_test()

    assert ok is False
    assert "500" in echo


@pytest.mark.asyncio
async def test_openai_compat_ping_test_defaults_to_adapter_timeout(monkeypatch):
    adapter = OpenAICompatAdapter(
        base_url="http://fake-host", api_key="x", timeout=300.0
    )

    fake_resp = FakePingResponse(
        status=200, body={"choices": [{"message": {"content": "pong"}}]}
    )
    fake_session = FakePingSession(fake_resp)

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: fake_session)

    ok, echo = await adapter.ping_test()

    assert ok is True
    assert echo == "pong"
    assert fake_session.last_kwargs is not None
    timeout = fake_session.last_kwargs["timeout"]
    assert timeout.connect == 300.0
    assert timeout.sock_connect == 300.0
    assert timeout.sock_read == 300.0


@pytest.mark.asyncio
async def test_openai_compat_ping_test_uses_listed_model_when_unspecified(monkeypatch):
    adapter = OpenAICompatAdapter(base_url="http://fake-host", api_key="x")

    fake_resp = FakePingResponse(
        status=200, body={"choices": [{"message": {"content": "pong"}}]}
    )
    fake_session = FakePingSession(fake_resp)

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: fake_session)

    async def fake_list_models():
        return [type("Model", (), {"id": "x-ai/grok-4.1-fast", "capabilities": {}})()]

    monkeypatch.setattr(adapter, "list_models", fake_list_models)

    ok, echo = await adapter.ping_test()

    assert ok is True
    assert echo == "pong"
    assert fake_session.last_kwargs is not None
    assert fake_session.last_kwargs["json"]["model"] == "x-ai/grok-4.1-fast"


@pytest.mark.asyncio
async def test_openai_compat_generate_tts_tries_v1_first(monkeypatch):
    adapter = OpenAICompatAdapter(base_url="http://localhost:14848", api_key="x")

    session = FakeAiohttpSession()
    session.responses = [
        FakeAiohttpResponse(status=404, payload={}),
        FakeAiohttpResponse(status=200, payload=None, body=b"audio bytes"),
    ]

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)

    result = await adapter.generate_tts("hello")

    assert result == b"audio bytes"
    assert session.calls[0].endswith("/v1/audio/speech")
    assert session.calls[1].endswith("/audio/speech")
