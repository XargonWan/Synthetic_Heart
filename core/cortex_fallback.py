# core/cortex_fallback.py
"""Staged cortex fallback chain: primary -> local -> cached/safe.

This module wraps the single chat-turn generation choke point with a fail-open,
three-stage degradation ladder so that an empty or timed-out primary generation
never leaves the persona silent when a usable alternative exists:

    Stage 0 (primary)    — call the resolved engine; return on success.
    Stage 1 (local)      — if the primary returned empty/whitespace or raised
                           ``TimeoutError``, retry once on ``CORTEX_FALLBACK_ENGINE``
                           under a shorter ``CORTEX_FALLBACK_TIMEOUT_SEC`` budget.
    Stage 2 (cached/safe)— if still no good text, replay a previously cached good
                           response for ``(engine_name, prompt_signature)`` when
                           ``CORTEX_CACHED_RESPONSE_ENABLED``; otherwise re-raise the
                           original exception / return empty so the terminal fallback
                           message in ``core.message_chain`` still applies.

Every helper is side-effect-free and catches all exceptions, degrading to a safe
default. Importing this module must never raise: config and logging access are
lazy and guarded. Engine selection is by registry name only — no keyword logic.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Awaitable, Callable

try:
    from core.logging_utils import log_info, log_warning
except Exception:  # pragma: no cover - import must never raise

    def log_info(*_args: Any, **_kwargs: Any) -> None:
        pass

    def log_warning(*_args: Any, **_kwargs: Any) -> None:
        pass


# In-memory cache of good responses keyed by "engine:signature". Values are
# (expiry_monotonic, text). Best-effort only; a cache miss is never an error.
_response_cache: dict[str, tuple[float, str]] = {}
_CACHE_MAX_ENTRIES = 512


def _get_config_value(key: str, default: Any) -> Any:
    """Read a config value lazily, degrading to ``default`` on any failure."""
    try:
        from core.config_manager import config_registry

        return config_registry.get_value(key, default)
    except Exception:
        return default


def _to_bool(value: Any) -> bool:
    """Coerce a config value to bool, fail-safe."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "enabled")
    return bool(value)


def _is_good_text(text: Any) -> bool:
    """Return True when *text* is a non-empty, non-whitespace response."""
    if text is None:
        return False
    try:
        return bool(str(text).strip())
    except Exception:
        return False


def resolve_fallback_engine() -> str | None:
    """Return the configured fallback engine name, or ``None`` when disabled/empty."""
    try:
        name = str(_get_config_value("CORTEX_FALLBACK_ENGINE", "") or "").strip()
        return name or None
    except Exception:
        return None


def is_local_engine(name: str) -> bool:
    """Return True when *name* is listed in ``CORTEX_LOCAL_ENGINES`` (case-insensitive)."""
    try:
        raw = str(
            _get_config_value("CORTEX_LOCAL_ENGINES", "selenium-llm-engine") or ""
        )
        local = {token.strip().lower() for token in raw.split(",") if token.strip()}
        return str(name or "").strip().lower() in local
    except Exception:
        return False


def _fallback_enabled() -> bool:
    try:
        return _to_bool(_get_config_value("CORTEX_FALLBACK_ENABLED", True))
    except Exception:
        return True


def _cached_enabled() -> bool:
    try:
        return _to_bool(_get_config_value("CORTEX_CACHED_RESPONSE_ENABLED", True))
    except Exception:
        return True


def resolve_fallback_timeout() -> float:
    """Return ``CORTEX_FALLBACK_TIMEOUT_SEC`` clamped to [5, 600] seconds."""
    try:
        value = float(_get_config_value("CORTEX_FALLBACK_TIMEOUT_SEC", 60))
    except (TypeError, ValueError):
        value = 60.0
    return max(5.0, min(600.0, value))


def _cache_ttl() -> float:
    """Return ``CORTEX_CACHE_TTL_SEC`` clamped to [60, 86400] seconds."""
    try:
        value = float(_get_config_value("CORTEX_CACHE_TTL_SEC", 3600))
    except (TypeError, ValueError):
        value = 3600.0
    return max(60.0, min(86400.0, value))


def _prompt_key(engine_name: str, prompt_signature: str | None) -> str | None:
    """Build the cache key, or ``None`` to disable caching for this call."""
    if not prompt_signature:
        return None
    return f"{str(engine_name)}:{str(prompt_signature)}"


def get_cached_response(prompt_key: str) -> str | None:
    """Return a live cached response for *prompt_key*, or ``None``. Never raises."""
    try:
        key = str(prompt_key)
        entry = _response_cache.get(key)
        if entry is None:
            return None
        expiry, text = entry
        if time.monotonic() >= expiry:
            _response_cache.pop(key, None)
            return None
        return text
    except Exception:
        return None


def set_cached_response(prompt_key: str, text: str) -> None:
    """Store a good response for *prompt_key* (best-effort, TTL + max-size). Never raises."""
    try:
        key = str(prompt_key)
        value = str(text)
        if not key or not value.strip():
            return
        _response_cache[key] = (time.monotonic() + _cache_ttl(), value)
        while len(_response_cache) > _CACHE_MAX_ENTRIES:
            try:
                oldest = min(_response_cache, key=lambda k: _response_cache[k][0])
                _response_cache.pop(oldest, None)
            except Exception:
                break
    except Exception:
        pass


async def _call_with_timeout(
    call_engine: Callable[[str], Awaitable[Any]],
    engine_name: str,
    timeout: float,
) -> Any:
    """Call *call_engine* under a bounded ``asyncio.wait_for`` budget."""
    return await asyncio.wait_for(call_engine(engine_name), timeout=timeout)


async def run_cortex_with_fallback(
    *,
    engine_name: str,
    scope: str | None,
    call_engine: Callable[[str], Awaitable[Any]],
    prompt_signature: str | None = None,
) -> Any:
    """Run the staged cortex fallback chain and return the first good response.

    ``call_engine(name)`` must return an awaitable resolving to the generated
    text for ``name``. The primary engine is always attempted first and its
    result is returned untouched on success, so the primary path is
    indistinguishable from a direct call. Fail-open throughout: any failure in
    the fallback machinery degrades to the primary outcome (or empty).
    """
    # Stage 0 — primary engine.
    primary_text: Any = None
    primary_timeout: TimeoutError | None = None
    try:
        primary_text = await call_engine(engine_name)
    except TimeoutError as exc:
        primary_timeout = exc
        primary_text = None

    if _is_good_text(primary_text):
        key = _prompt_key(engine_name, prompt_signature)
        if key is not None and _cached_enabled():
            set_cached_response(key, str(primary_text))
        return primary_text

    # Stage 1 — local fallback engine (only for empty/whitespace or timeout).
    fallback_engine = resolve_fallback_engine()
    if _fallback_enabled() and fallback_engine and fallback_engine != engine_name:
        try:
            fallback_text = await _call_with_timeout(
                call_engine, fallback_engine, resolve_fallback_timeout()
            )
        except Exception as exc:
            log_warning(
                f"[cortex_fallback] Fallback engine {fallback_engine!r} failed "
                f"(scope={scope!r}): {exc}"
            )
            fallback_text = None
        if _is_good_text(fallback_text):
            log_info(
                f"[cortex_fallback] Primary engine {engine_name!r} produced no "
                f"good text (scope={scope!r}); served by fallback engine "
                f"{fallback_engine!r}"
            )
            key = _prompt_key(engine_name, prompt_signature)
            if key is not None and _cached_enabled():
                set_cached_response(key, str(fallback_text))
            return fallback_text

    # Stage 2 — cached/safe response.
    if _cached_enabled():
        key = _prompt_key(engine_name, prompt_signature)
        if key is not None:
            cached = get_cached_response(key)
            if cached is not None:
                log_info(
                    f"[cortex_fallback] Primary engine {engine_name!r} and "
                    f"fallback produced no good text (scope={scope!r}); replaying "
                    f"a cached safe response"
                )
                return cached

    # No good text anywhere. Preserve the primary outcome so the terminal
    # fallback in core.message_chain still applies.
    if primary_timeout is not None:
        raise primary_timeout
    return primary_text
