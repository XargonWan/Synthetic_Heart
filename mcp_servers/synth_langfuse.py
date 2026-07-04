#!/usr/bin/env python3
"""MCP server for full-verbosity Langfuse trace inspection.

The third-party `langfuse` MCP server (`@therealsachin/langfuse-mcp`) is great
for metrics/costs but hides payloads: its `get_trace_detail` strips every
observation down to name/tokens/cost (no input/output at all) and its
`get_observations` hard-caps input/output at 2000 chars. SyntH system prompts
alone are 16k+ chars, so debugging prompt assembly through it is impossible.

This server complements it with three tools that return the *actual* payloads:

Tools
-----
traces_recent     -- compact list of recent traces (ids to drill into)
trace_full        -- one trace + ALL its observations with full input/output
observation_full  -- one observation, raw, nothing removed

Configuration comes from the workspace `.env` (per-workspace by design, so
parallel checkouts like D15/B15 each talk to their own Langfuse project):
LANGFUSE_HOST (or LANGFUSE_BASE_URL / LANGFUSE_BASEURL), LANGFUSE_PUBLIC_KEY,
LANGFUSE_SECRET_KEY. Real environment variables override the file.

Usage (stdio transport, registered in .mcp.json)
-------------------------------------------------
    uv run python mcp_servers/synth_langfuse.py
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from mcp.server.fastmcp import FastMCP

# ---------------------------------------------------------------------------
# Config — workspace .env is the canonical source (keys never live in git)
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parent.parent
_ENV_FILE = _REPO_ROOT / ".env"

_HTTP_TIMEOUT_S = 30.0


def _load_repo_env() -> dict[str, str]:
    """Parse the repo .env into a dict (values may be quoted)."""
    values: dict[str, str] = {}
    if not _ENV_FILE.exists():
        return values
    for raw_line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


_REPO_ENV = _load_repo_env()


def _env_value(key: str) -> str:
    """Real environment overrides the .env file."""
    return os.environ.get(key) or _REPO_ENV.get(key, "")


def _base_url() -> str:
    for key in ("LANGFUSE_HOST", "LANGFUSE_BASE_URL", "LANGFUSE_BASEURL"):
        value = _env_value(key)
        if value:
            return value.rstrip("/")
    return ""


def _auth() -> tuple[str, str]:
    return (_env_value("LANGFUSE_PUBLIC_KEY"), _env_value("LANGFUSE_SECRET_KEY"))


def _api_get(path: str, params: Optional[dict[str, Any]] = None) -> Any:
    base = _base_url()
    public_key, secret_key = _auth()
    if not base or not public_key or not secret_key:
        raise RuntimeError(
            "Langfuse not configured: need LANGFUSE_HOST (or LANGFUSE_BASE_URL) "
            f"+ LANGFUSE_PUBLIC_KEY + LANGFUSE_SECRET_KEY in {_ENV_FILE} or the environment."
        )
    response = requests.get(
        f"{base}/api/public{path}",
        params={k: v for k, v in (params or {}).items() if v not in (None, "")},
        auth=(public_key, secret_key),
        timeout=_HTTP_TIMEOUT_S,
    )
    response.raise_for_status()
    return response.json()


mcp = FastMCP("synth-langfuse")

# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _render_payload(value: Any, max_chars: int) -> str:
    """Render an input/output payload as readable text, truncating per-field.

    max_chars <= 0 means unlimited. Truncation is always explicit so an agent
    knows to re-call with a bigger budget instead of trusting a cut payload.
    """
    if value is None:
        return "(empty)"
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, indent=2, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            text = str(value)
    if max_chars > 0 and len(text) > max_chars:
        return (
            f"{text[:max_chars]}\n"
            f"...[TRUNCATED: showing {max_chars} of {len(text)} chars — "
            f"re-call with max_chars_per_field=0 for the full payload]"
        )
    return text


def _fmt_usage(observation: dict[str, Any]) -> str:
    usage = observation.get("usage") or {}
    parts: list[str] = []
    for label, key in (("in", "input"), ("out", "output"), ("total", "total")):
        if usage.get(key) is not None:
            parts.append(f"{label}={usage[key]}")
    cost = observation.get("calculatedTotalCost")
    if cost is not None:
        parts.append(f"cost={cost}")
    return " ".join(parts) if parts else "n/a"


def _render_observation(
    observation: dict[str, Any], max_chars: int, index: Optional[int] = None
) -> str:
    header = f"OBSERVATION {index}" if index is not None else "OBSERVATION"
    lines = [
        f"--- {header}: {observation.get('type', '?')} "
        f"'{observation.get('name') or 'unnamed'}' ---",
        f"id: {observation.get('id')}",
        f"time: {observation.get('startTime')} -> {observation.get('endTime')}",
        f"model: {observation.get('model') or 'n/a'}   tokens: {_fmt_usage(observation)}",
    ]
    if observation.get("level") and observation.get("level") != "DEFAULT":
        lines.append(f"level: {observation['level']}")
    if observation.get("statusMessage"):
        lines.append(f"status: {observation['statusMessage']}")
    if observation.get("metadata"):
        lines.append(
            f"metadata:\n{_render_payload(observation['metadata'], max_chars)}"
        )
    if observation.get("modelParameters"):
        lines.append(
            f"modelParameters: {json.dumps(observation['modelParameters'], default=str)}"
        )
    lines.append(f"INPUT:\n{_render_payload(observation.get('input'), max_chars)}")
    lines.append(f"OUTPUT:\n{_render_payload(observation.get('output'), max_chars)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def traces_recent(
    limit: int = 20,
    minutes: int = 0,
    name: str = "",
    user_id: str = "",
    session_id: str = "",
) -> str:
    """Compact list of recent traces (newest first) with the ids needed to drill in.

    Args:
        limit: max traces to return (1-100).
        minutes: only traces from the last N minutes (0 = no time filter).
        name: filter by exact trace name (e.g. an interface or beat name).
        user_id: filter by Langfuse userId.
        session_id: filter by Langfuse sessionId.
    """
    params: dict[str, Any] = {"limit": max(1, min(int(limit), 100)), "page": 1}
    if minutes > 0:
        from_ts = datetime.now(timezone.utc) - timedelta(minutes=minutes)
        params["fromTimestamp"] = from_ts.isoformat().replace("+00:00", "Z")
    params["name"] = name
    params["userId"] = user_id
    params["sessionId"] = session_id
    data = _api_get("/traces", params)
    traces = data.get("data", [])
    if not traces:
        return "No traces matched."
    lines = [f"{len(traces)} trace(s), newest first:"]
    for trace in traces:
        lines.append(
            f"- {trace.get('timestamp')}  id={trace.get('id')}  "
            f"name={trace.get('name') or 'unnamed'}  "
            f"user={trace.get('userId') or '-'}  "
            f"session={trace.get('sessionId') or '-'}  "
            f"latency={trace.get('latency')}  cost={trace.get('totalCost')}"
        )
    lines.append("Drill in with trace_full(trace_id=...).")
    return "\n".join(lines)


@mcp.tool()
def trace_full(trace_id: str, max_chars_per_field: int = 30000) -> str:
    """One trace with ALL observations including full input/output payloads.

    Unlike the `langfuse` server's get_trace_detail (which strips payloads
    entirely) this returns the actual prompt/response text of every
    observation, truncated per field only past max_chars_per_field.

    Args:
        trace_id: the Langfuse trace id.
        max_chars_per_field: per-field truncation budget; 0 = unlimited.
    """
    trace = _api_get(f"/traces/{trace_id}")
    observations = sorted(
        trace.get("observations") or [],
        key=lambda o: o.get("startTime") or "",
    )
    lines = [
        f"TRACE {trace.get('id')}  '{trace.get('name') or 'unnamed'}'",
        f"timestamp: {trace.get('timestamp')}   user: {trace.get('userId') or '-'}   "
        f"session: {trace.get('sessionId') or '-'}",
        f"latency: {trace.get('latency')}   totalCost: {trace.get('totalCost')}   "
        f"tags: {trace.get('tags') or []}",
    ]
    if trace.get("metadata"):
        lines.append(
            f"trace metadata:\n{_render_payload(trace['metadata'], max_chars_per_field)}"
        )
    if trace.get("input") is not None:
        lines.append(
            f"trace INPUT:\n{_render_payload(trace['input'], max_chars_per_field)}"
        )
    if trace.get("output") is not None:
        lines.append(
            f"trace OUTPUT:\n{_render_payload(trace['output'], max_chars_per_field)}"
        )
    scores = trace.get("scores") or []
    if scores:
        lines.append(f"scores: {json.dumps(scores, default=str)}")
    lines.append(f"\n{len(observations)} observation(s):")
    for position, observation in enumerate(observations, start=1):
        lines.append("")
        lines.append(
            _render_observation(observation, max_chars_per_field, index=position)
        )
    return "\n".join(lines)


@mcp.tool()
def observation_full(observation_id: str, max_chars_per_field: int = 0) -> str:
    """One observation, raw and complete (full input/output, no default truncation).

    Args:
        observation_id: the Langfuse observation id (from trace_full output).
        max_chars_per_field: optional per-field truncation budget; 0 = unlimited.
    """
    observation = _api_get(f"/observations/{observation_id}")
    lines = [
        f"trace: {observation.get('traceId')}",
        _render_observation(observation, max_chars_per_field),
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="stdio")
