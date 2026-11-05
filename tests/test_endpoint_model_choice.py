"""Which model an external endpoint starts on, and keeping it that way.

Two properties, both from a live install:

1. An endpoint that lists a hundred and twenty three models has no meaningful
   "first". The auto-selection took whatever came first and a Venice endpoint came
   up on a Gemini model, so the starting model is now chosen by ordered patterns
   (``ENDPOINT_MODEL_PREFERENCES``) with the endpoint's own list deciding what is
   available.

2. Setting a model in the WebUI did not change the model actually used. The runtime
   resolves the model from the cortex *scope* config value and re-applies it around
   every engine call, so updating only ``external_endpoints.default_model`` left the
   engine on its previous model: a model set to ``deepseek-v4-1-flash`` still
   resolved as ``gemini-3-6-flash`` on the very next prompt.
"""

from __future__ import annotations

import pytest

from core.external_endpoints.adapters.base import ModelInfo
from core.external_endpoints.model_choice import (
    pick_preferred_model,
    preference_patterns,
    select_default_model,
)
from core.external_endpoints.registry import ExternalEndpointRegistry

# A Venice-shaped listing: the model that was picked before, and the one the user
# wanted, in the order the API returned them.
_LISTING = [
    "gemini-3-6-flash",
    "claude-4-5-sonnet",
    "deepseek-v4-1-flash",
    "qwen-3-5-max",
]


def test_a_preferred_model_beats_whatever_the_endpoint_lists_first() -> None:
    assert select_default_model(_LISTING, "deepseek*") == "deepseek-v4-1-flash"


def test_the_earliest_pattern_wins() -> None:
    assert select_default_model(_LISTING, "qwen*,deepseek*") == "qwen-3-5-max"
    assert select_default_model(_LISTING, "deepseek*,qwen*") == "deepseek-v4-1-flash"


def test_no_match_falls_back_to_the_endpoints_own_first_model() -> None:
    """No preference match keeps the previous behaviour: the first listed model."""
    assert select_default_model(_LISTING, "kimi*") == "gemini-3-6-flash"
    assert select_default_model(_LISTING, "") == "gemini-3-6-flash"


def test_an_endpoint_with_no_models_selects_nothing() -> None:
    assert select_default_model([], "deepseek*") is None


def test_model_objects_are_matched_by_their_id() -> None:
    """The adapter hands over ``ModelInfo`` objects, not strings."""
    models = [ModelInfo(id=name, name=name) for name in _LISTING]
    assert select_default_model(models, "deepseek*") == "deepseek-v4-1-flash"


def test_patterns_accept_commas_newlines_and_spacing() -> None:
    assert preference_patterns("deepseek*, qwen*\nllama*") == [
        "deepseek*",
        "qwen*",
        "llama*",
    ]
    assert preference_patterns("  ") == []
    assert pick_preferred_model(_LISTING, []) is None


# ---------------------------------------------------------------------------
# The scope value the runtime actually reads for the model
# ---------------------------------------------------------------------------


class _FakeConfigRegistry:
    def __init__(self, values: dict[str, str]) -> None:
        self.values = dict(values)
        self.writes: list[tuple[str, str]] = []

    def get_value(self, key: str, default: str = "") -> str:
        return self.values.get(key, default)

    async def set_value(self, key: str, value: str) -> None:
        self.writes.append((key, value))
        self.values[key] = value


@pytest.fixture()
def fake_config(monkeypatch: pytest.MonkeyPatch) -> _FakeConfigRegistry:
    registry = _FakeConfigRegistry(
        {
            "BASE_CORTEX": '{"engine": "Venice", "model": "gemini-3-6-flash"}',
            "GRILLO_CORTEX": '{"engine": "Venice", "model": "gemini-3-6-flash"}',
            "TRAINER_CORTEX": '{"engine": "OtherProvider", "model": "keep-me"}',
            "DSP_CORTEX": "Default",
        }
    )
    import core.config as config_module

    monkeypatch.setattr(config_module, "config_registry", registry)
    return registry


@pytest.mark.asyncio
async def test_every_scope_naming_the_engine_follows_the_new_model(
    fake_config: _FakeConfigRegistry,
) -> None:
    registry = ExternalEndpointRegistry()

    await registry._sync_scope_models("Venice", "deepseek-v4-1-flash")

    written = dict(fake_config.writes)
    assert written["BASE_CORTEX"] == (
        '{"engine": "Venice", "model": "deepseek-v4-1-flash"}'
    )
    assert written["GRILLO_CORTEX"] == (
        '{"engine": "Venice", "model": "deepseek-v4-1-flash"}'
    )
    assert "TRAINER_CORTEX" not in written, "another engine's scope must be left alone"
    assert "DSP_CORTEX" not in written, "an unset scope must be left alone"


@pytest.mark.asyncio
async def test_a_scope_already_on_the_new_model_is_not_rewritten(
    fake_config: _FakeConfigRegistry,
) -> None:
    fake_config.values["BASE_CORTEX"] = (
        '{"engine": "Venice", "model": "deepseek-v4-1-flash"}'
    )
    registry = ExternalEndpointRegistry()

    await registry._sync_scope_models("Venice", "deepseek-v4-1-flash")

    assert ["BASE_CORTEX"] not in [key for key, _ in fake_config.writes]


@pytest.mark.asyncio
async def test_clearing_the_model_lets_the_endpoint_default_apply(
    fake_config: _FakeConfigRegistry,
) -> None:
    """Clearing the WebUI's model field drops the override rather than freezing it."""
    registry = ExternalEndpointRegistry()

    await registry._sync_scope_models("Venice", None)

    assert dict(fake_config.writes)["BASE_CORTEX"] == "Venice"


@pytest.mark.asyncio
async def test_setting_a_model_is_what_drives_the_sync(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The fix has to be wired into the path the WebUI calls, not just available."""

    class _Cursor:
        async def execute(self, *args, **kwargs) -> None:
            return None

    class _CursorCtx:
        async def __aenter__(self):
            return _Cursor()

        async def __aexit__(self, *exc) -> None:
            return None

    class _Conn:
        def cursor(self):
            return _CursorCtx()

        async def commit(self) -> None:
            return None

    class _Ctx:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *exc) -> None:
            return None

    synced: list[tuple[str, str | None]] = []

    class _Endpoint:
        def engine_name(self) -> str:
            return "Venice"

    registry = ExternalEndpointRegistry()

    async def fake_ensure() -> None:
        return None

    async def fake_get_endpoint(endpoint_id: int):
        return _Endpoint()

    async def fake_sync(engine_name: str, model: str | None) -> None:
        synced.append((engine_name, model))

    import core.db as db_module

    monkeypatch.setattr(registry, "_ensure", fake_ensure)
    monkeypatch.setattr(registry, "get_endpoint", fake_get_endpoint)
    monkeypatch.setattr(registry, "_sync_scope_models", fake_sync)
    monkeypatch.setattr(db_module, "get_conn_ctx", lambda: _Ctx())

    await registry.set_default_model(1, "deepseek-v4-1-flash")

    assert synced == [("Venice", "deepseek-v4-1-flash")]


@pytest.mark.asyncio
async def test_a_config_failure_does_not_lose_the_model_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The row is already written; a scope hiccup must not raise into the WebUI."""
    import core.config as config_module

    class _Exploding:
        def get_value(self, key: str, default: str = "") -> str:
            raise RuntimeError("config store unavailable")

        async def set_value(self, key: str, value: str) -> None:
            raise RuntimeError("config store unavailable")

    monkeypatch.setattr(config_module, "config_registry", _Exploding())
    registry = ExternalEndpointRegistry()

    # Must not raise.
    await registry._sync_scope_models("Venice", "deepseek-v4-1-flash")


# ---------------------------------------------------------------------------
# The shipped default: flash, never the fat variant
# ---------------------------------------------------------------------------

# A listing that offers the *pro* variant first. Within one pattern the endpoint's own
# order decides, so a bare `deepseek*` picks whichever Venice happens to list first.
_KINDS_OF_DEEPSEEK = [
    "gemini-3-6-flash",
    "deepseek-v4-1-pro",
    "deepseek-v4-1-flash",
    "qwen-3-5-max",
]


def test_the_shipped_default_takes_flash_even_when_the_pro_variant_is_listed_first() -> (
    None
):
    """This project runs the fast DeepSeek variants, never the large ones."""
    from core.config import ENDPOINT_MODEL_PREFERENCES_DEFAULT

    picked = select_default_model(
        _KINDS_OF_DEEPSEEK, ENDPOINT_MODEL_PREFERENCES_DEFAULT
    )
    assert picked == "deepseek-v4-1-flash"


def test_the_shipped_default_stays_in_the_family_when_no_flash_variant_exists() -> None:
    from core.config import ENDPOINT_MODEL_PREFERENCES_DEFAULT

    listing = ["gemini-3-6-flash", "deepseek-v4-1-pro"]
    assert (
        select_default_model(listing, ENDPOINT_MODEL_PREFERENCES_DEFAULT)
        == "deepseek-v4-1-pro"
    )


def test_the_shipped_default_leaves_an_endpoint_without_deepseek_alone() -> None:
    """An endpoint that carries none of the preferred family keeps its first model."""
    from core.config import ENDPOINT_MODEL_PREFERENCES_DEFAULT

    listing = ["gemini-3-6-flash", "claude-4-5-sonnet"]
    assert (
        select_default_model(listing, ENDPOINT_MODEL_PREFERENCES_DEFAULT)
        == "gemini-3-6-flash"
    )
