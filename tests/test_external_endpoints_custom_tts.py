import pytest

from core.external_endpoints.models import EndpointProtocol, ExternalEndpoint
from core.external_endpoints.probe import get_adapter_for_endpoint
from core.external_endpoints.bridges.vox_bridge import ExternalVoxEngine
from core.external_endpoints.adapters.custom_tts_adapter import LegacyHttpTTSAdapter


def test_external_endpoint_effective_subsystem_map_includes_vision() -> None:
    endpoint = ExternalEndpoint(
        id=1,
        name="vision_ep",
        display_label="Vision Endpoint",
        protocol=EndpointProtocol.OPENAI,
        base_url="http://localhost:9999",
        api_key_enc=None,
        enabled=True,
        capabilities={"vision": True},
        subsystem_map={"auris": True},
        available_models=[],
        default_model=None,
        probe_status="success",
        last_probe_at=None,
        extra_config={},
    )

    merged = endpoint.effective_subsystem_map()

    assert merged["vision"] is True
    assert merged["auris"] is True
    assert merged["cortex"] is False
    assert merged["vox"] is False
    assert merged["live"] is False


class FakeResponse:
    def __init__(self, status: int, data: bytes) -> None:
        self.status = status
        self._data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def read(self) -> bytes:
        return self._data


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        return self._response


@pytest.mark.asyncio
async def test_get_adapter_for_custom_legacy_http_tts_returns_legacy_adapter():
    endpoint = ExternalEndpoint(
        id=1,
        name="legacy_tts",
        display_label="Legacy HTTP TTS",
        protocol=EndpointProtocol.CUSTOM,
        base_url="http://example.com/tts",
        api_key_enc=None,
        enabled=True,
        capabilities={},
        subsystem_map={"vox": True},
        available_models=[],
        default_model=None,
        probe_status="never",
        last_probe_at=None,
        extra_config={"legacy_http_tts": True},
    )

    adapter = get_adapter_for_endpoint(endpoint, api_key="")

    assert isinstance(adapter, LegacyHttpTTSAdapter)


@pytest.mark.asyncio
async def test_legacy_http_tts_adapter_generate_tts_posts_payload(monkeypatch):
    fake_response = FakeResponse(status=200, data=b"fake-audio")
    fake_session = FakeSession(fake_response)

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: fake_session)

    adapter = LegacyHttpTTSAdapter(
        base_url="http://example.com/tts",
        extra_config={"tts_voice_wav": "voice.wav"},
    )

    result = await adapter.generate_tts("Ciao", voice=None, language="it")

    assert result == b"fake-audio"


def test_external_vox_engine_uses_tts_extra_config():
    endpoint = ExternalEndpoint(
        id=2,
        name="legacy_tts_engine",
        display_label="Legacy HTTP TTS",
        protocol=EndpointProtocol.CUSTOM,
        base_url="http://example.com/tts",
        api_key_enc=None,
        enabled=True,
        capabilities={},
        subsystem_map={"vox": True},
        available_models=[],
        default_model=None,
        probe_status="never",
        last_probe_at=None,
        extra_config={
            "tts_output_format": "pcm",
            "tts_sample_rate": 16000,
            "tts_channels": 2,
        },
    )

    engine = ExternalVoxEngine(endpoint, adapter=None)

    assert engine.output_format == "pcm"
    assert engine.sample_rate == 16000
    assert engine.channels == 2


class _RecordingTTSAdapter:
    """Adapter stub that records the kwargs forwarded to ``generate_tts``."""

    def __init__(self) -> None:
        self.captured_kwargs: dict[str, object] = {}
        self._engine_label = ""

    async def generate_tts(
        self, text: str, voice: object = None, **kwargs: object
    ) -> bytes:
        self.captured_kwargs = {"voice": voice, **kwargs}
        return b"audio"


def test_endpoint_tts_language_overrides_caller_language():
    """A pinned ``tts_language`` must override the caller's auto-detected language.

    Regression: single-speaker models (e.g. kitten-tts-nano) accept only a fixed
    language set. The endpoint's configured ``tts_language`` is a hard model
    constraint and must win over the language the caller detected from the text.
    """
    endpoint = ExternalEndpoint(
        id=3,
        name="pinned_lang_tts",
        display_label="Pinned Language TTS",
        protocol=EndpointProtocol.CUSTOM,
        base_url="http://example.com/tts",
        api_key_enc=None,
        enabled=True,
        capabilities={},
        subsystem_map={"vox": True},
        available_models=[],
        default_model=None,
        probe_status="never",
        last_probe_at=None,
        extra_config={"tts_language": "default", "tts_model": "kitten-tts-nano"},
    )

    adapter = _RecordingTTSAdapter()
    engine = ExternalVoxEngine(endpoint, adapter=adapter)

    # Caller auto-detected Italian; the endpoint pins "default".
    result = engine.generate_tts("Ciao", language="it")

    assert result == b"audio"
    assert adapter.captured_kwargs["language"] == "default"
    assert adapter.captured_kwargs["model"] == "kitten-tts-nano"
