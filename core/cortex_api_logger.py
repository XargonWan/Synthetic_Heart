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
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from typing import Any

_DEFAULT_LOG_DIR = os.path.join(os.getcwd(), "logs")
_LOG_DIR = os.getenv("LOG_DIR", _DEFAULT_LOG_DIR)
_LOG_FILE = os.path.join(_LOG_DIR, "cortex_api.log")

_logger: logging.Logger | None = None

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


def sanitize_for_log(data: Any, *, max_str_len: int = 500) -> Any:
    """Deep-copy *data*, replacing large strings and bytes with size placeholders.

    Use before passing payloads to ``log_cortex_request`` to avoid dumping
    megabytes of base64 or binary content into the log.
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
    logger = _get_logger()
    tag = f"engine={engine} model={model}"
    sep = "═" * _WIDTH
    lines = [
        f"\n{sep}",
        f"  REQUEST  [{_ts()}]  {tag}",
        f"{sep}",
    ]
    if url:
        lines.append(f"URL: {url}")
    if headers:
        lines.append(f"Headers: {_pretty_json(headers)}")
    if payload:
        lines.append(f"Payload:\n{_pretty_json(payload)}")
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
    logger = _get_logger()
    parts = [f"engine={engine}"]
    if model:
        parts.append(f"model={model}")
    if status is not None:
        parts.append(f"status={status}")
    if elapsed_ms is not None:
        parts.append(f"elapsed={elapsed_ms:.0f}ms")
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
