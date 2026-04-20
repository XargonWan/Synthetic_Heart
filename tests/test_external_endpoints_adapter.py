import pytest

from core.external_endpoints.adapters.openai_compat import OpenAICompatAdapter


class FakeModelsEndpoint:
    def __init__(self, data):
        self._data = data

    async def list(self):
        return {"data": self._data}


class FakeClient:
    def __init__(self, data):
        self.models = FakeModelsEndpoint(data)


@pytest.mark.asyncio
async def test_openai_compat_list_models_from_dict_response(monkeypatch):
    data = [
        {"id": "chatgpt", "object": "model", "owned_by": "selenium-llm-engine"},
        {"id": "claude", "object": "model", "owned_by": "selenium-llm-engine"},
    ]
    adapter = OpenAICompatAdapter(base_url="http://localhost:14848", api_key="x")

    monkeypatch.setattr(adapter, "_get_client", lambda: FakeClient(data))

    models = await adapter.list_models()

    assert isinstance(models, list)
    assert len(models) == 2
    assert models[0].id == "chatgpt"
    assert models[1].id == "claude"


@pytest.mark.asyncio
async def test_openai_compat_list_models_fallback_name(monkeypatch):
    data = [{"id": "grok", "object": "model", "name": "Grok"}]
    adapter = OpenAICompatAdapter(base_url="http://localhost:14848", api_key="x")

    monkeypatch.setattr(adapter, "_get_client", lambda: FakeClient(data))

    models = await adapter.list_models()

    assert len(models) == 1
    assert models[0].name == "Grok"


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
