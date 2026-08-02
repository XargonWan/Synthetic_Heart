import pytest

from core.external_endpoints.adapters.fish_audio_adapter import (
    DEFAULT_MODEL,
    FishAudioAdapter,
)
from core.external_endpoints.models import EndpointProtocol, ExternalEndpoint
from core.external_endpoints.preset_registry import load_presets
from core.external_endpoints.probe import get_adapter_for_endpoint


def _make_endpoint(**overrides: object) -> ExternalEndpoint:
    base: dict = dict(
        id=1,
        name="fish-audio",
        display_label="Fish Audio",
        protocol=EndpointProtocol.FISH,
        base_url="https://api.fish.audio/v1/tts",
        api_key_enc=None,
        enabled=True,
        capabilities={},
        subsystem_map={"vox": True},
        available_models=[],
        default_model=None,
        probe_status="never",
        last_probe_at=None,
        extra_config={},
    )
    base.update(overrides)
    return ExternalEndpoint(**base)


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


class RecordingSession:
    """aiohttp.ClientSession stub that records the last post() call."""

    def __init__(self, response: FakeResponse) -> None:
        self._response = response
        self.post_args: tuple = ()
        self.post_kwargs: dict = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def post(self, *args, **kwargs):
        self.post_args = args
        self.post_kwargs = kwargs
        return self._response


def test_get_adapter_for_fish_returns_fish_adapter() -> None:
    adapter = get_adapter_for_endpoint(_make_endpoint(), api_key="fa_test")

    assert isinstance(adapter, FishAudioAdapter)


def test_get_adapter_for_fish_requires_api_key() -> None:
    with pytest.raises(ValueError):
        get_adapter_for_endpoint(_make_endpoint(), api_key="")


@pytest.mark.asyncio
async def test_fish_generate_tts_uses_reference_id_schema(monkeypatch) -> None:
    session = RecordingSession(FakeResponse(status=200, data=b"fake-audio"))

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)

    adapter = FishAudioAdapter(
        base_url="https://api.fish.audio/v1/tts",
        api_key="fa_test",
        extra_config={
            "tts_model": "s2.1-pro-free",
            "tts_output_format": "wav",
            "tts_reference_id": "abc123",
        },
    )

    result = await adapter.generate_tts("Ciao")

    assert result == b"fake-audio"
    assert session.post_args[0] == "https://api.fish.audio/v1/tts"
    payload = session.post_kwargs["json"]
    assert payload == {"text": "Ciao", "format": "wav", "reference_id": "abc123"}
    headers = session.post_kwargs["headers"]
    assert headers["Authorization"] == "Bearer fa_test"
    assert headers["model"] == "s2.1-pro-free"


@pytest.mark.asyncio
async def test_fish_generate_tts_defaults_and_voice_override(monkeypatch) -> None:
    session = RecordingSession(FakeResponse(status=200, data=b"audio"))

    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", lambda: session)

    adapter = FishAudioAdapter(
        base_url="https://api.fish.audio/v1/tts",
        api_key="fa_test",
    )

    # Explicit voice wins over (missing) extra_config; unknown format → wav.
    await adapter.generate_tts("Hi", voice="voice-42", format="ogg")

    payload = session.post_kwargs["json"]
    assert payload["reference_id"] == "voice-42"
    assert payload["format"] == "wav"
    assert session.post_kwargs["headers"]["model"] == DEFAULT_MODEL


@pytest.mark.asyncio
async def test_fish_probe_and_models() -> None:
    adapter = FishAudioAdapter(
        base_url="https://api.fish.audio/v1/tts", api_key="fa_test"
    )

    caps = await adapter.probe_capabilities()
    assert caps["vox"] is True
    assert caps["cortex"] is False

    models = await adapter.list_models()
    model_ids = [m.id for m in models]
    assert "s2.1-pro-free" in model_ids
    assert all(m.capabilities.get("vox") for m in models)


def test_fish_audio_preset_is_loaded_with_tts_category() -> None:
    presets = load_presets()
    fish = next((p for p in presets if p["provider_id"] == "fish_audio"), None)

    assert fish is not None
    assert fish["category"] == "tts"
    assert fish["protocol"] == "fish"
    assert fish["base_url"] == "https://api.fish.audio/v1/tts"
    field_keys = {f["key"] for f in fish["extra_fields"]}
    assert field_keys == {"tts_model", "tts_output_format", "tts_reference_id"}
    # Non-TTS presets keep the default category so they stay in the main grid.
    assert all(
        p.get("category", "llm") == "llm"
        for p in presets
        if p["provider_id"] != "fish_audio"
    )
