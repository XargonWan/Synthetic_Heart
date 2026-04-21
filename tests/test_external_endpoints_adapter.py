from types import SimpleNamespace
from typing import Any

import pytest

from core.external_endpoints.adapters.base import ModelInfo
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
    adapter._get_client = lambda: fake_client

    response = await adapter.chat_completion(
        [{"role": "user", "content": "ping"}], model="qwen/qwen3.5-9b"
    )

    assert response.content == 'Okay, the user just typed "ping".'
    assert response.model == "qwen/qwen3.5-9b"


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

    engine = ExternalCortexEngine(endpoint, FakeAdapter())
    response = await engine.generate_response([{"role": "user", "content": "ping"}])

    assert response == "ok"
    assert called["model"] == "qwen/qwen3.5-9b"


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

    engine = ExternalCortexEngine(endpoint, FakeAdapter())
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
