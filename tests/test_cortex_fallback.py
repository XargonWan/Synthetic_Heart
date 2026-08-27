"""Tests for the staged cortex fallback chain (``core/cortex_fallback.py``).

These tests cover the pure, fail-open fallback ladder that wraps the single
chat-turn generation choke point:

* primary success short-circuits (``call_engine`` invoked exactly once);
* an empty/whitespace primary degrades to the configured fallback engine;
* config helpers degrade to safe defaults (empty fallback engine -> ``None``,
  local-engine list matching is case-insensitive, timeout clamp, cache TTL);
* the in-memory cached-response store round-trips;
* a raising fallback degrades to empty / re-raising (fail-open), never breaking
  the primary path.

No DB, no bridge, no LLM.
"""

from __future__ import annotations

from typing import Any

import pytest

import core.cortex_fallback as cf


_DEFAULT_CONFIG: dict[str, Any] = {
    "CORTEX_FALLBACK_ENABLED": True,
    "CORTEX_FALLBACK_ENGINE": "",
    "CORTEX_LOCAL_ENGINES": "selenium-llm-engine",
    "CORTEX_FALLBACK_TIMEOUT_SEC": 60,
    "CORTEX_CACHED_RESPONSE_ENABLED": True,
    "CORTEX_CACHE_TTL_SEC": 3600,
}


def _patch_config(monkeypatch: pytest.MonkeyPatch, values: dict[str, Any]) -> None:
    """Point ``config_registry.get_value`` at an in-memory dict."""
    import core.config_manager as cm

    merged = {**_DEFAULT_CONFIG, **values}

    def fake_get_value(key: str, default: Any = None, *args: Any, **kwargs: Any) -> Any:
        return merged.get(key, default)

    monkeypatch.setattr(cm.config_registry, "get_value", fake_get_value)


@pytest.fixture(autouse=True)
def _clear_cache() -> Any:
    cf._response_cache.clear()
    yield
    cf._response_cache.clear()


# ---------------------------------------------------------------------------
# Config helper resolution
# ---------------------------------------------------------------------------


def test_resolve_fallback_engine_none_on_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, {"CORTEX_FALLBACK_ENGINE": ""})
    assert cf.resolve_fallback_engine() is None


def test_resolve_fallback_engine_none_on_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch, {"CORTEX_FALLBACK_ENGINE": "   "})
    assert cf.resolve_fallback_engine() is None


def test_resolve_fallback_engine_returns_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch, {"CORTEX_FALLBACK_ENGINE": "selenium-llm-engine"})
    assert cf.resolve_fallback_engine() == "selenium-llm-engine"


def test_resolve_fallback_engine_degrades_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import core.config_manager as cm

    def boom(key: str, default: Any = None, *a: Any, **k: Any) -> Any:
        raise RuntimeError("config unavailable")

    monkeypatch.setattr(cm.config_registry, "get_value", boom)
    assert cf.resolve_fallback_engine() is None


def test_is_local_engine_case_insensitive(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, {"CORTEX_LOCAL_ENGINES": "Selenium-LLM-Engine, Foo"})
    assert cf.is_local_engine("SELENIUM-LLM-ENGINE") is True
    assert cf.is_local_engine("foo") is True
    assert cf.is_local_engine("bar") is False


def test_resolve_fallback_timeout_clamps(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, {"CORTEX_FALLBACK_TIMEOUT_SEC": 1})
    assert cf.resolve_fallback_timeout() == 5.0
    _patch_config(monkeypatch, {"CORTEX_FALLBACK_TIMEOUT_SEC": 9999})
    assert cf.resolve_fallback_timeout() == 600.0
    _patch_config(monkeypatch, {"CORTEX_FALLBACK_TIMEOUT_SEC": 60})
    assert cf.resolve_fallback_timeout() == 60.0


def test_resolve_fallback_timeout_degrades_on_bad_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch, {"CORTEX_FALLBACK_TIMEOUT_SEC": "not-a-number"})
    assert cf.resolve_fallback_timeout() == 60.0


# ---------------------------------------------------------------------------
# Cached safe response store
# ---------------------------------------------------------------------------


def test_cached_response_round_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, {})
    cf.set_cached_response("primary:sig", "hello")
    assert cf.get_cached_response("primary:sig") == "hello"
    assert cf.get_cached_response("missing") is None


def test_cached_response_does_not_store_blank(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, {})
    cf.set_cached_response("k", "   ")
    assert cf.get_cached_response("k") is None


def test_cached_response_never_raises_on_garbage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch, {})
    # Exercise the fail-open paths directly with odd inputs.
    assert cf.get_cached_response(None) is None  # type: ignore[arg-type]
    cf.set_cached_response("", "x")  # no-op, must not raise
    cf.set_cached_response("k", "v")
    cf._response_cache["k"] = (0.0, "expired")  # force an expired entry
    assert cf.get_cached_response("k") is None


# ---------------------------------------------------------------------------
# run_cortex_with_fallback — stage 0 (primary)
# ---------------------------------------------------------------------------


async def test_primary_success_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, {"CORTEX_FALLBACK_ENGINE": "selenium-llm-engine"})
    calls: list[str] = []

    async def call_engine(name: str) -> str:
        calls.append(name)
        return "hello"

    result = await cf.run_cortex_with_fallback(
        engine_name="primary",
        scope=None,
        call_engine=call_engine,
        prompt_signature="sig",
    )
    assert result == "hello"
    assert calls == ["primary"]


async def test_primary_success_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, {})

    async def call_engine(name: str) -> str:
        return "good"

    await cf.run_cortex_with_fallback(
        engine_name="primary",
        scope=None,
        call_engine=call_engine,
        prompt_signature="sig",
    )
    assert cf.get_cached_response("primary:sig") == "good"


# ---------------------------------------------------------------------------
# run_cortex_with_fallback — stage 1 (local fallback)
# ---------------------------------------------------------------------------


async def test_empty_primary_uses_local_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch, {"CORTEX_FALLBACK_ENGINE": "selenium-llm-engine"})
    calls: list[str] = []

    async def call_engine(name: str) -> str:
        calls.append(name)
        if name == "primary":
            return "   "
        return "fallback text"

    result = await cf.run_cortex_with_fallback(
        engine_name="primary",
        scope=None,
        call_engine=call_engine,
        prompt_signature="sig",
    )
    assert result == "fallback text"
    assert calls == ["primary", "selenium-llm-engine"]


async def test_none_primary_uses_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, {"CORTEX_FALLBACK_ENGINE": "selenium-llm-engine"})
    calls: list[str] = []

    async def call_engine(name: str) -> str | None:
        calls.append(name)
        if name == "primary":
            return None
        return "fallback text"

    result = await cf.run_cortex_with_fallback(
        engine_name="primary",
        scope=None,
        call_engine=call_engine,
        prompt_signature="sig",
    )
    assert result == "fallback text"
    assert calls == ["primary", "selenium-llm-engine"]


async def test_fallback_skipped_when_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(
        monkeypatch,
        {
            "CORTEX_FALLBACK_ENABLED": False,
            "CORTEX_FALLBACK_ENGINE": "selenium-llm-engine",
        },
    )
    calls: list[str] = []

    async def call_engine(name: str) -> str:
        calls.append(name)
        return ""

    result = await cf.run_cortex_with_fallback(
        engine_name="primary",
        scope=None,
        call_engine=call_engine,
        prompt_signature="sig",
    )
    assert result == ""
    assert calls == ["primary"]


async def test_fallback_skipped_when_same_as_primary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch, {"CORTEX_FALLBACK_ENGINE": "primary"})
    calls: list[str] = []

    async def call_engine(name: str) -> str:
        calls.append(name)
        return ""

    result = await cf.run_cortex_with_fallback(
        engine_name="primary",
        scope=None,
        call_engine=call_engine,
        prompt_signature="sig",
    )
    assert result == ""
    assert calls == ["primary"]


async def test_timeout_primary_uses_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_config(monkeypatch, {"CORTEX_FALLBACK_ENGINE": "selenium-llm-engine"})
    calls: list[str] = []

    async def call_engine(name: str) -> str:
        calls.append(name)
        if name == "primary":
            raise TimeoutError("primary timeout")
        return "recovered"

    result = await cf.run_cortex_with_fallback(
        engine_name="primary",
        scope=None,
        call_engine=call_engine,
        prompt_signature="sig",
    )
    assert result == "recovered"
    assert calls == ["primary", "selenium-llm-engine"]


# ---------------------------------------------------------------------------
# run_cortex_with_fallback — stage 2 (cached/safe) + fail-open
# ---------------------------------------------------------------------------


async def test_cached_response_returned_when_all_generation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch, {"CORTEX_FALLBACK_ENGINE": "selenium-llm-engine"})
    cf.set_cached_response("primary:sig", "cached text")

    async def call_engine(name: str) -> str:
        return ""

    result = await cf.run_cortex_with_fallback(
        engine_name="primary",
        scope=None,
        call_engine=call_engine,
        prompt_signature="sig",
    )
    assert result == "cached text"


async def test_raising_fallback_degrades_to_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch, {"CORTEX_FALLBACK_ENGINE": "selenium-llm-engine"})

    async def call_engine(name: str) -> str:
        if name == "primary":
            return ""
        raise RuntimeError("fallback boom")

    result = await cf.run_cortex_with_fallback(
        engine_name="primary",
        scope=None,
        call_engine=call_engine,
        prompt_signature="sig",
    )
    assert result == ""


async def test_timeout_primary_reraises_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch, {})  # fallback engine empty -> disabled

    async def call_engine(name: str) -> str:
        raise TimeoutError("primary timeout")

    with pytest.raises(TimeoutError):
        await cf.run_cortex_with_fallback(
            engine_name="primary",
            scope=None,
            call_engine=call_engine,
            prompt_signature="sig",
        )


async def test_timeout_primary_reraises_after_failed_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch, {"CORTEX_FALLBACK_ENGINE": "selenium-llm-engine"})

    async def call_engine(name: str) -> str:
        if name == "primary":
            raise TimeoutError("primary timeout")
        return ""

    with pytest.raises(TimeoutError):
        await cf.run_cortex_with_fallback(
            engine_name="primary",
            scope=None,
            call_engine=call_engine,
            prompt_signature="sig",
        )


async def test_primary_non_timeout_exception_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_config(monkeypatch, {"CORTEX_FALLBACK_ENGINE": "selenium-llm-engine"})
    calls: list[str] = []

    async def call_engine(name: str) -> str:
        calls.append(name)
        raise ValueError("primary broken")

    with pytest.raises(ValueError):
        await cf.run_cortex_with_fallback(
            engine_name="primary",
            scope=None,
            call_engine=call_engine,
            prompt_signature="sig",
        )
    # A non-timeout primary failure is the primary's own outcome and must not
    # trigger the fallback (per spec: only empty/whitespace or TimeoutError).
    assert calls == ["primary"]
