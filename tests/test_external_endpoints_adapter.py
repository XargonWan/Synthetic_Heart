from typing import Any

import pytest

from core.external_endpoints.adapters.base import ModelInfo
from core.external_endpoints.adapters.openai_compat import OpenAICompatAdapter


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
        self.responses: list[FakeAiohttpResponse] = []

    def get(self, url: str, *args, **kwargs):
        self.calls.append(url)
        return self.responses.pop(0)

    def post(self, url: str, *args, **kwargs):
        self.calls.append(url)
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
        FakeAiohttpResponse(status=200, payload={"data": [{"id": "chat-only", "object": "model"}]}),
        FakeAiohttpResponse(status=200, payload={"choices": [{"message": {"content": "ok"}}]}),
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
async def test_openai_compat_probe_capabilities_detects_vision_for_gemini_flash(monkeypatch):
    adapter = OpenAICompatAdapter(base_url="http://localhost:14848", api_key="x")

    session = FakeAiohttpSession()
    session.responses = [
        FakeAiohttpResponse(status=200, payload={"data": [{"id": "gemini-2.5-flash", "object": "model"}]}),
        FakeAiohttpResponse(status=200, payload={"choices": [{"message": {"content": "ok"}}]}),
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

    def post(self, *args, **kwargs):
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
