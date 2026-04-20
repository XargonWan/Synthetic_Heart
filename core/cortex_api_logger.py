# core/cortex_api_logger.py
"""Human-readable logger for cortex LLM API requests and responses.

Writes a dedicated log file (``logs/cortex_api.log``) that records every
request/response cycle for any cortex engine.  The format is designed to
be easy to read when tailing or opening in an editor:

    ═══ REQUEST  [2026-03-03 14:05:12] engine=openrouter model=x-ai/grok-4.1-fast ═══
    ... payload ...
    ─── RESPONSE [2026-03-03 14:05:14] status=200 tokens=483 ────────────────────────
    ... response body ...

Usage from any cortex engine::

    from core.cortex_api_logger import log_cortex_request, log_cortex_response

    log_cortex_request("openrouter", model=model, payload=payload)
    # ... perform HTTP call ...
    log_cortex_response("openrouter", model=model, status=200, body=data, usage=usage)
"""

from __future__ import annotations

import json
import logging
import os
import textwrap
import contextvars
import time
from importlib import import_module
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any
from uuid import uuid4

_DEFAULT_LOG_DIR = os.path.join(os.getcwd(), "logs")
_LOG_DIR = os.getenv("LOG_DIR", _DEFAULT_LOG_DIR)
_LOG_FILE = os.path.join(_LOG_DIR, "cortex_api.log")

_logger: logging.Logger | None = None
_langfuse_client: Any | None = None
_langfuse_ctx: contextvars.ContextVar[list[dict[str, Any]]] = contextvars.ContextVar(
    "cortex_api_langfuse_ctx", default=[]
)

# Separator widths
_WIDTH = 90


def _get_logger() -> logging.Logger:
    """Lazily initialise and return the cortex-API file logger."""
    global _logger
    if _logger is not None:
        return _logger

    os.makedirs(_LOG_DIR, exist_ok=True)

    logger = logging.getLogger("synth.cortex_api")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # don't bubble into the main synth logger

    if not logger.handlers:
        handler = RotatingFileHandler(
            _LOG_FILE,
            maxBytes=10 * 1024 * 1024,  # 10 MB per file
            backupCount=5,
            encoding="utf-8",
        )
        handler.setLevel(logging.DEBUG)
        # Minimal formatter — the visual separators already carry timestamps.
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)

    _logger = logger
    return logger


def _ts() -> str:
    """Return a human-readable UTC timestamp."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _pretty_json(obj: Any, *, redact_keys: set[str] | None = None) -> str:
    """Pretty-print a JSON-serialisable object, optionally redacting secrets."""
    if redact_keys is None:
        redact_keys = {"authorization", "api_key", "apikey", "token"}

    def _redact(o: Any) -> Any:
        if isinstance(o, dict):
            return {
                k: ("***" if redact_keys and k.lower() in redact_keys else _redact(v))
                for k, v in o.items()
            }
        if isinstance(o, list):
            return [_redact(i) for i in o]
        return o

    try:
        return json.dumps(_redact(obj), indent=2, ensure_ascii=False, default=str)
    except Exception:
        return str(obj)


def _langfuse_enabled() -> bool:
    # CORTEX_LANGFUSE_ENABLED takes precedence for API-call tracing.
    # Falls back to LANGFUSE_ENABLED to remain backward compatible.
    value = os.getenv("CORTEX_LANGFUSE_ENABLED")
    if value is None:
        value = os.getenv("LANGFUSE_ENABLED", "false")
    return str(value).lower() == "true"


def _cortex_api_log_enabled() -> bool:
    return os.getenv("CORTEX_API_LOG_ENABLED", "false").lower() == "true"


def _langfuse_flush_each_call() -> bool:
    return os.getenv("LANGFUSE_FLUSH_EACH_CALL", "false").lower() == "true"


def _langfuse_redact_payloads() -> bool:
    return os.getenv("CORTEX_LANGFUSE_REDACT_PAYLOADS", "true").lower() == "true"


def _langfuse_capture_headers() -> bool:
    return os.getenv("CORTEX_LANGFUSE_CAPTURE_HEADERS", "false").lower() == "true"


def _langfuse_capture_generations() -> bool:
    return os.getenv("CORTEX_LANGFUSE_CAPTURE_GENERATIONS", "true").lower() == "true"


def _redact_sensitive_fields(data: Any) -> Any:
    redact_keys = {
        "authorization",
        "proxy-authorization",
        "api_key",
        "apikey",
        "x-api-key",
        "token",
        "access_token",
        "refresh_token",
        "secret",
        "password",
    }

    if isinstance(data, dict):
        return {
            key: (
                "***" if key.lower() in redact_keys else _redact_sensitive_fields(value)
            )
            for key, value in data.items()
        }
    if isinstance(data, list):
        return [_redact_sensitive_fields(item) for item in data]
    return data


def _coerce_for_langfuse(data: Any, *, redact_sensitive: bool = False) -> Any:
    if _langfuse_redact_payloads():
        coerced = sanitize_for_log(data)
    else:
        # Keep full text payloads while still normalizing non-JSON-safe bytes.
        coerced = sanitize_for_log(data, max_str_len=10_000_000)

    if redact_sensitive:
        return _redact_sensitive_fields(coerced)
    return coerced


def _get_langfuse_client() -> Any | None:
    global _langfuse_client
    if _langfuse_client is not None:
        return _langfuse_client

    if not _langfuse_enabled():
        return None

    try:
        langfuse_module = import_module("langfuse")
        langfuse_cls = getattr(langfuse_module, "Langfuse", None)
        if langfuse_cls is None:
            return None
        _langfuse_client = langfuse_cls()
        return _langfuse_client
    except Exception:
        return None


def _push_langfuse_request(
    *,
    engine: str,
    model: str,
    url: str,
    headers: dict[str, str] | None,
    payload: dict[str, Any] | None,
) -> str:
    request_id = uuid4().hex[:12]
    trace: Any | None = None

    client = _get_langfuse_client()
    if client is not None:
        try:
            trace = client.trace(
                name=f"cortex_api:{engine}",
                session_id=f"{engine}:{request_id}",
                metadata={
                    "request_id": request_id,
                    "engine": engine,
                    "model": model,
                    "url": url,
                },
            )
            if trace is not None and hasattr(trace, "update"):
                update_kwargs: dict[str, Any] = {
                    "input": _coerce_for_langfuse(payload or {}),
                    "metadata": {
                        "request_id": request_id,
                        "engine": engine,
                        "model": model,
                        "url": url,
                    },
                }
                if _langfuse_capture_headers() and headers:
                    update_kwargs["metadata"]["headers"] = _coerce_for_langfuse(
                        headers,
                        redact_sensitive=True,
                    )
                trace.update(**update_kwargs)
        except Exception:
            trace = None

    stack = list(_langfuse_ctx.get())
    stack.append(
        {
            "request_id": request_id,
            "trace": trace,
            "engine": engine,
            "model": model,
            "url": url,
            "headers": headers,
            "input_payload": payload,
            "started_at_monotonic": time.monotonic(),
            "started_at_utc": datetime.now(timezone.utc),
        }
    )
    _langfuse_ctx.set(stack)
    return request_id


def _pop_langfuse_request(*, engine: str, model: str) -> dict[str, Any] | None:
    stack = list(_langfuse_ctx.get())
    if not stack:
        return None

    # First pass: exact engine+model match.
    for idx in range(len(stack) - 1, -1, -1):
        item = stack[idx]
        if item.get("engine") == engine and (not model or item.get("model") == model):
            popped = stack.pop(idx)
            _langfuse_ctx.set(stack)
            return popped

    # Second pass: same engine regardless of model to tolerate model-label drift
    # between request and response call sites.
    for idx in range(len(stack) - 1, -1, -1):
        item = stack[idx]
        if item.get("engine") == engine:
            popped = stack.pop(idx)
            _langfuse_ctx.set(stack)
            return popped

    # Do not pop unrelated requests; keep stack stable for the correct responder.
    return None


def sanitize_for_log(data: Any, *, max_str_len: int = 500) -> Any:
    """Deep-copy *data*, replacing large strings and bytes with size placeholders.

    Called automatically by ``log_cortex_request``; callers may also call it
    directly to produce compact dicts for other purposes.
    """
    if isinstance(data, dict):
        return {
            k: sanitize_for_log(v, max_str_len=max_str_len) for k, v in data.items()
        }
    if isinstance(data, (list, tuple)):
        return [sanitize_for_log(item, max_str_len=max_str_len) for item in data]
    if isinstance(data, str) and len(data) > max_str_len:
        return f"<string: {len(data)} chars>"
    if isinstance(data, bytes):
        return f"<bytes: {len(data)} bytes>"
    return data


# ── Public helpers ────────────────────────────────────────────────────────


def log_cortex_request(
    engine: str,
    *,
    model: str = "",
    url: str = "",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    """Log a cortex API request in human-readable form."""
    file_log_enabled = _cortex_api_log_enabled()
    lf_enabled = _langfuse_enabled()
    if not file_log_enabled and not lf_enabled:
        return

    logger = _get_logger() if file_log_enabled else None
    request_id = _push_langfuse_request(
        engine=engine,
        model=model,
        url=url,
        headers=headers,
        payload=payload,
    )

    if logger is None:
        return

    tag = f"engine={engine} model={model}"
    sep = "═" * _WIDTH
    lines = [
        f"\n{sep}",
        f"  REQUEST  [{_ts()}]  {tag} request_id={request_id}",
        f"{sep}",
    ]
    if url:
        lines.append(f"URL: {url}")
    if headers:
        lines.append(f"Headers: {_pretty_json(headers)}")
    if payload:
        lines.append(f"Payload:\n{_pretty_json(sanitize_for_log(payload))}")
    logger.debug("\n".join(lines))


def log_cortex_response(
    engine: str,
    *,
    model: str = "",
    status: int | None = None,
    body: dict[str, Any] | str | None = None,
    usage: dict[str, Any] | None = None,
    error: str | None = None,
    elapsed_ms: float | None = None,
) -> None:
    """Log a cortex API response in human-readable form."""
    file_log_enabled = _cortex_api_log_enabled()
    lf_enabled = _langfuse_enabled()
    if not file_log_enabled and not lf_enabled:
        return

    logger = _get_logger() if file_log_enabled else None
    lf_item = _pop_langfuse_request(engine=engine, model=model)
    request_id = lf_item.get("request_id") if isinstance(lf_item, dict) else None

    trace = lf_item.get("trace") if isinstance(lf_item, dict) else None
    input_payload = lf_item.get("input_payload") if isinstance(lf_item, dict) else None
    headers = lf_item.get("headers") if isinstance(lf_item, dict) else None
    url = lf_item.get("url") if isinstance(lf_item, dict) else ""
    started = lf_item.get("started_at_monotonic") if isinstance(lf_item, dict) else None
    started_utc = lf_item.get("started_at_utc") if isinstance(lf_item, dict) else None

    output_payload: dict[str, Any] | str | None = body
    if output_payload is None and not error:
        output_payload = {"empty_response": True, "reason": "body_none"}
    elif isinstance(output_payload, str) and not output_payload.strip() and not error:
        output_payload = {"empty_response": True, "reason": "body_empty_string"}

    # Backfill elapsed if caller did not provide it.
    if elapsed_ms is None and isinstance(started, float):
        elapsed_ms = max(0.0, (time.monotonic() - started) * 1000.0)

    if trace is not None:
        try:
            if hasattr(trace, "update"):
                trace.update(
                    output=_coerce_for_langfuse(output_payload),
                    metadata={
                        "status": status,
                        "error": error,
                        "elapsed_ms": elapsed_ms,
                        "usage": usage or {},
                        "engine": engine,
                        "model": model,
                        "url": url,
                    },
                )

            # Emit a generation record to populate model/token columns.
            if _langfuse_capture_generations() and hasattr(trace, "generation"):
                gen_kwargs: dict[str, Any] = {
                    "name": f"completion:{engine}",
                    "model": model or None,
                    "input": _coerce_for_langfuse(input_payload or {}),
                    "output": _coerce_for_langfuse(output_payload),
                    "metadata": {
                        "request_id": request_id,
                        "engine": engine,
                        "status": status,
                        "error": error,
                        "url": url,
                    },
                }
                if isinstance(usage, dict) and usage:
                    gen_kwargs["usage_details"] = usage
                if elapsed_ms is not None:
                    gen_kwargs["metadata"]["elapsed_ms"] = elapsed_ms
                if isinstance(started_utc, datetime):
                    gen_kwargs["start_time"] = started_utc
                    gen_kwargs["end_time"] = datetime.now(timezone.utc)
                if _langfuse_capture_headers() and headers:
                    gen_kwargs["metadata"]["headers"] = _coerce_for_langfuse(
                        headers,
                        redact_sensitive=True,
                    )
                try:
                    trace.generation(**gen_kwargs)
                except TypeError:
                    # Keep compatibility with slightly different SDK signatures.
                    gen_kwargs.pop("usage_details", None)
                    try:
                        trace.generation(**gen_kwargs)
                    except TypeError:
                        gen_kwargs.pop("start_time", None)
                        gen_kwargs.pop("end_time", None)
                        trace.generation(**gen_kwargs)
        except Exception:
            pass

    client = _get_langfuse_client()
    if client is not None and _langfuse_flush_each_call():
        try:
            flush_fn = getattr(client, "flush", None)
            if callable(flush_fn):
                flush_fn()
        except Exception:
            pass

    if logger is None:
        return

    parts = [f"engine={engine}"]
    if model:
        parts.append(f"model={model}")
    if status is not None:
        parts.append(f"status={status}")
    if elapsed_ms is not None:
        parts.append(f"elapsed={elapsed_ms:.0f}ms")
    if request_id:
        parts.append(f"request_id={request_id}")
    if usage:
        prompt_tok = usage.get("prompt_tokens", "?")
        comp_tok = usage.get("completion_tokens", "?")
        cache_read = usage.get("prompt_tokens_details", {}).get(
            "cached_tokens"
        ) or usage.get("cache_read_input_tokens")
        tok_str = f"prompt={prompt_tok} completion={comp_tok}"
        if cache_read:
            tok_str += f" cache_read={cache_read}"
        parts.append(f"tokens=[{tok_str}]")

    tag = "  ".join(parts)
    sep = "─" * _WIDTH
    lines = [
        f"{sep}",
        f"  RESPONSE [{_ts()}]  {tag}",
        f"{sep}",
    ]
    if error:
        lines.append(f"ERROR: {error}")
    if body:
        if isinstance(body, str):
            # Likely the raw LLM content string — wrap for readability
            lines.append(textwrap.fill(body, width=120))
        else:
            lines.append(_pretty_json(body))
    logger.debug("\n".join(lines))
