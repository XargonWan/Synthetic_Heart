"""Unit tests for core/engine_config_presets.py."""

from unittest.mock import AsyncMock

import pytest

from core import engine_config_presets as ecp


class FakeConfigRegistry:
    """Minimal config_registry stand-in (sync get_value, async set_value)."""

    def __init__(self, initial: dict | None = None) -> None:
        self.store: dict = dict(initial or {})

    def get_value(self, key: str, default: object = None, **kwargs: object) -> object:
        return self.store.get(key, default)

    async def set_value(self, key: str, value: object, **kwargs: object) -> None:
        self.store[key] = value


class FakeEndpoint:
    def __init__(
        self,
        endpoint_id: int,
        name: str = "ep",
        extra_config: dict | None = None,
    ) -> None:
        self.id = endpoint_id
        self.name = name
        self.extra_config: dict = dict(extra_config or {})
        self.api_key_enc = None

    def engine_name(self) -> str:
        return self.name


class FakeEndpointRegistry:
    def __init__(self) -> None:
        self.endpoints: dict[int, FakeEndpoint] = {}
        self.models: dict[int, str] = {}
        self.updated: list[tuple[int, dict]] = []
        self.call_order: list[str] = []

    async def get_endpoint(self, endpoint_id: int) -> FakeEndpoint | None:
        return self.endpoints.get(endpoint_id)

    async def update_endpoint(
        self, endpoint_id: int, **fields: object
    ) -> FakeEndpoint | None:
        self.call_order.append("update_endpoint")
        ep = self.endpoints.get(endpoint_id)
        if ep is None:
            return None
        if "extra_config" in fields and isinstance(fields["extra_config"], dict):
            ep.extra_config = fields["extra_config"]
        self.updated.append((endpoint_id, fields))
        return ep

    async def set_default_model(self, endpoint_id: int, model: str) -> None:
        self.call_order.append("set_default_model")
        self.models[endpoint_id] = model


@pytest.fixture
def cfg_registry(monkeypatch: pytest.MonkeyPatch) -> FakeConfigRegistry:
    fake = FakeConfigRegistry()
    monkeypatch.setattr("core.config_manager.config_registry", fake)
    return fake


@pytest.fixture
def ep_registry(monkeypatch: pytest.MonkeyPatch) -> FakeEndpointRegistry:
    fake = FakeEndpointRegistry()
    monkeypatch.setattr(
        "core.external_endpoints.registry.get_external_endpoint_registry",
        lambda: fake,
    )
    return fake


def test_load_presets_empty(cfg_registry: FakeConfigRegistry) -> None:
    assert ecp.load_presets() == []


def test_load_presets_filters_junk(cfg_registry: FakeConfigRegistry) -> None:
    cfg_registry.store[ecp.PRESETS_CONFIG_KEY] = [
        {"name": "ok", "extra_config": {"max_tools": 20}},
        "not-a-dict",
        {"extra_config": {}},  # no name -> dropped
        None,
    ]
    presets = ecp.load_presets()
    assert len(presets) == 1
    assert presets[0]["name"] == "ok"


def test_load_presets_malformed_value(cfg_registry: FakeConfigRegistry) -> None:
    cfg_registry.store[ecp.PRESETS_CONFIG_KEY] = "not a list"
    assert ecp.load_presets() == []


@pytest.mark.asyncio
async def test_save_preset_requires_name(cfg_registry: FakeConfigRegistry) -> None:
    with pytest.raises(ValueError):
        await ecp.save_preset("   ")


@pytest.mark.asyncio
async def test_save_preset_creates_and_replaces(
    cfg_registry: FakeConfigRegistry,
) -> None:
    saved = await ecp.save_preset(
        "strict", model="model-b", extra_config={"max_tools": 40, "enable_tools": True}
    )
    assert saved["name"] == "strict"
    assert saved["model"] == "model-b"
    assert saved["extra_config"] == {"max_tools": 40, "enable_tools": True}
    assert cfg_registry.store[ecp.PRESETS_CONFIG_KEY] == [saved]

    # Replacing keeps a single entry
    saved2 = await ecp.save_preset("strict", model="", extra_config={"max_tools": 20})
    stored = cfg_registry.store[ecp.PRESETS_CONFIG_KEY]
    assert len(stored) == 1
    assert stored[0]["name"] == "strict"
    assert saved2["extra_config"] == {"max_tools": 20}


@pytest.mark.asyncio
async def test_delete_preset(cfg_registry: FakeConfigRegistry) -> None:
    await ecp.save_preset("a", extra_config={})
    await ecp.save_preset("b", extra_config={})
    assert await ecp.delete_preset("a") is True
    assert await ecp.delete_preset("a") is False  # already gone
    stored = cfg_registry.store[ecp.PRESETS_CONFIG_KEY]
    assert [p["name"] for p in stored] == ["b"]


@pytest.mark.asyncio
async def test_apply_preset_missing_preset(
    cfg_registry: FakeConfigRegistry,
) -> None:
    ep, preset = await ecp.apply_preset(1, "nope")
    assert ep is None
    assert preset is None


@pytest.mark.asyncio
async def test_apply_preset_missing_endpoint(
    cfg_registry: FakeConfigRegistry,
    ep_registry: FakeEndpointRegistry,
) -> None:
    await ecp.save_preset("p", extra_config={"max_tools": 30})
    ep, preset = await ecp.apply_preset(99, "p")
    assert ep is None
    assert preset is not None


@pytest.mark.asyncio
async def test_apply_preset_replaces_config_and_sets_model(
    cfg_registry: FakeConfigRegistry,
    ep_registry: FakeEndpointRegistry,
) -> None:
    ep = FakeEndpoint(1, name="venice", extra_config={"provider_id": "venice"})
    ep_registry.endpoints[1] = ep
    await ecp.save_preset(
        "strict",
        model="model-b",
        extra_config={"max_tools": 40, "enable_tools": True},
    )

    updated, preset = await ecp.apply_preset(1, "strict")
    assert preset is not None
    assert updated is ep
    # Config replaced wholesale, but the system key survives
    assert ep.extra_config == {
        "max_tools": 40,
        "enable_tools": True,
        "provider_id": "venice",
    }
    assert ep_registry.models.get(1) == "model-b"


@pytest.mark.asyncio
async def test_apply_preset_without_model_leaves_default_model(
    cfg_registry: FakeConfigRegistry,
    ep_registry: FakeEndpointRegistry,
) -> None:
    ep = FakeEndpoint(2, name="ep2")
    ep_registry.endpoints[2] = ep
    await ecp.save_preset("minimal", extra_config={"timeout": 60})

    updated, preset = await ecp.apply_preset(2, "minimal")
    assert preset is not None
    assert updated is ep
    assert ep.extra_config == {"timeout": 60}
    assert ep_registry.models == {}


@pytest.mark.asyncio
async def test_apply_preset_persists_model_before_resync(
    cfg_registry: FakeConfigRegistry,
    ep_registry: FakeEndpointRegistry,
) -> None:
    """The preset model must reach the DB before ``update_endpoint`` re-syncs.

    ``update_endpoint`` rebuilds the live bridge from the DB row, so a model
    written afterwards would be invisible to the running engine until some
    later re-sync.  Ordering matters even for models absent from the probed
    ``available_models`` list.
    """
    ep = FakeEndpoint(3, name="ep3")
    ep_registry.endpoints[3] = ep
    await ecp.save_preset("custom-model", model="my-custom-model", extra_config={})

    updated, preset = await ecp.apply_preset(3, "custom-model")
    assert preset is not None
    assert updated is ep
    assert ep_registry.models.get(3) == "my-custom-model"
    assert ep_registry.call_order == ["set_default_model", "update_endpoint"]


# ---------------------------------------------------------------------------
# Preset scopes ("Apply to scopes" toggle)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_save_preset_stores_validated_scopes(
    cfg_registry: FakeConfigRegistry,
) -> None:
    saved = await ecp.save_preset(
        "deep",
        model="m",
        extra_config={},
        scopes=["Agent", "agent", "dsp", "bogus", "agent"],
    )
    assert saved["scopes"] == ["agent", "dsp"]


@pytest.mark.asyncio
async def test_save_preset_without_scopes_stores_empty(
    cfg_registry: FakeConfigRegistry,
) -> None:
    saved = await ecp.save_preset("plain", extra_config={})
    assert saved["scopes"] == []


@pytest.mark.asyncio
async def test_apply_preset_to_scopes_calls_scope_setters(
    cfg_registry: FakeConfigRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_scope = AsyncMock()
    set_base = AsyncMock()
    monkeypatch.setattr("core.config.set_scope_cortex", set_scope)
    monkeypatch.setattr("core.config.set_base_cortex", set_base)

    applied = await ecp.apply_preset_to_scopes(
        "venice", "deepseek-v4-flash", ["agent", "base", "dsp", "bogus"]
    )

    assert applied == ["agent", "base", "dsp"]
    set_scope.assert_any_await("agent", "venice", "deepseek-v4-flash")
    set_scope.assert_any_await("dsp", "venice", "deepseek-v4-flash")
    set_base.assert_awaited_once_with("venice", "deepseek-v4-flash")


@pytest.mark.asyncio
async def test_apply_preset_to_scopes_ignores_failing_scope(
    cfg_registry: FakeConfigRegistry,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(*args: object, **kwargs: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("core.config.set_scope_cortex", boom)
    monkeypatch.setattr("core.config.set_base_cortex", AsyncMock())

    applied = await ecp.apply_preset_to_scopes("venice", None, ["agent", "grillo"])

    assert applied == []


@pytest.mark.asyncio
async def test_apply_preset_to_scopes_empty_input(
    cfg_registry: FakeConfigRegistry,
) -> None:
    assert await ecp.apply_preset_to_scopes("venice", None, None) == []
    assert await ecp.apply_preset_to_scopes("venice", None, []) == []
