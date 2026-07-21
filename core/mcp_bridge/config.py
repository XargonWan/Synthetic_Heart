"""Synth runtime MCP server configuration.

This module loads the **Synth-owned** MCP server registry from
``config/synth_mcp.json``.  It is intentionally SEPARATE from the repository
root ``.mcp.json`` file, which is *development-only* tooling used by external
agents (Claude Code, Copilot, …) to inspect Synth.  The two must never be
confused:

* ``.mcp.json``  -> servers that WE / external dev agents connect to.
* ``config/synth_mcp.json`` -> servers that **Synth itself** connects to at
  runtime to consume external tools (and, later, to expose its own actions).

The loader is read-only and fail-safe: a missing or malformed file yields an
empty registry rather than raising, so the rest of Synth boots normally.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.logging_utils import log_debug, log_info, log_warning

# Repo-relative default location.  Override with the SYNTH_MCP_CONFIG env var.
_DEFAULT_CONFIG_PATH = (
    Path(__file__).resolve().parent.parent.parent / "config" / "synth_mcp.json"
)

_CONFIG_PATH = Path(
    __import__("os").getenv("SYNTH_MCP_CONFIG", str(_DEFAULT_CONFIG_PATH))
).expanduser()

# Top-level key inside the JSON document that holds the server map.
_SERVERS_KEY = "synthMcpServers"


@dataclass
class SynthMcpServerConfig:
    """A single MCP server entry from ``config/synth_mcp.json``."""

    name: str
    transport: str = "stdio"  # "stdio" | "sse" | "http" | "streamable_http"
    command: str | None = None
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    url: str | None = None
    enabled: bool = True
    security_level: str = "medium"
    description: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Serialize back to the JSON shape (used for diagnostics)."""
        data: dict[str, Any] = {
            "transport": self.transport,
            "enabled": self.enabled,
            "security_level": self.security_level,
            "description": self.description,
        }
        if self.command is not None:
            data["command"] = self.command
        if self.args:
            data["args"] = list(self.args)
        if self.env:
            data["env"] = dict(self.env)
        if self.url is not None:
            data["url"] = self.url
        if self.extra:
            data.update(self.extra)
        return data


def _coerce_server(name: str, raw: Any) -> SynthMcpServerConfig | None:
    """Validate and coerce one raw JSON entry into a config object."""
    if not isinstance(raw, dict):
        log_warning(
            f"[synth_mcp_config] Skipping server '{name}': entry is not an object."
        )
        return None

    transport = str(raw.get("transport", "stdio")).lower()
    if transport not in ("stdio", "sse", "http", "streamable_http"):
        log_warning(
            f"[synth_mcp_config] Server '{name}' has unknown transport "
            f"'{transport}', defaulting to 'stdio'."
        )
        transport = "stdio"

    if transport == "stdio" and not raw.get("command"):
        log_warning(
            f"[synth_mcp_config] Server '{name}' uses stdio but has no "
            f"'command'; disabling."
        )
        return None

    if transport in ("sse", "http", "streamable_http") and not raw.get("url"):
        log_warning(
            f"[synth_mcp_config] Server '{name}' uses {transport} but has no "
            f"'url'; disabling."
        )
        return None

    return SynthMcpServerConfig(
        name=name,
        transport=transport,
        command=raw.get("command"),  # type: ignore[arg-type]
        args=list(raw.get("args", []) or []),
        env=dict(raw.get("env", {}) or {}),
        url=raw.get("url"),  # type: ignore[arg-type]
        enabled=bool(raw.get("enabled", True)),
        security_level=str(raw.get("security_level", "medium")),
        description=str(raw.get("description", "")),
        extra={
            k: v
            for k, v in raw.items()
            if k
            not in (
                "transport",
                "command",
                "args",
                "env",
                "url",
                "enabled",
                "security_level",
                "description",
            )
        },
    )


def load_synth_mcp_servers(
    config_path: Path | None = None,
) -> dict[str, SynthMcpServerConfig]:
    """Load and validate the Synth MCP server registry.

    Args:
        config_path: Optional override path.  Defaults to the env var
            ``SYNTH_MCP_CONFIG`` or ``config/synth_mcp.json``.

    Returns:
        Mapping of server name -> validated config.  Disabled servers are
        included (with ``enabled=False``) so callers can report them; use
        :func:`load_enabled_synth_mcp_servers` to filter them out.
    """
    path = config_path or _CONFIG_PATH

    if not path.exists():
        log_info(
            f"[synth_mcp_config] No Synth MCP config at {path}; "
            f"returning empty registry."
        )
        return {}

    try:
        with path.open("r", encoding="utf-8") as fh:
            raw_doc = json.load(fh)
    except (json.JSONDecodeError, OSError) as exc:
        log_warning(
            f"[synth_mcp_config] Failed to read {path}: {exc}; "
            f"returning empty registry."
        )
        return {}

    if not isinstance(raw_doc, dict):
        log_warning(
            f"[synth_mcp_config] {path} top-level is not an object; "
            f"returning empty registry."
        )
        return {}

    raw_servers = raw_doc.get(_SERVERS_KEY)
    if not isinstance(raw_servers, dict):
        log_debug(
            f"[synth_mcp_config] No '{_SERVERS_KEY}' key in {path}; "
            f"returning empty registry."
        )
        return {}

    servers: dict[str, SynthMcpServerConfig] = {}
    for name, raw in raw_servers.items():
        cfg = _coerce_server(str(name), raw)
        if cfg is not None:
            servers[cfg.name] = cfg

    log_info(
        f"[synth_mcp_config] Loaded {len(servers)} Synth MCP server(s) from {path}."
    )
    return servers


def load_enabled_synth_mcp_servers(
    config_path: Path | None = None,
) -> dict[str, SynthMcpServerConfig]:
    """Like :func:`load_synth_mcp_servers` but drops disabled entries."""
    return {
        name: cfg
        for name, cfg in load_synth_mcp_servers(config_path).items()
        if cfg.enabled
    }


def get_synth_mcp_config_path() -> Path:
    """Return the resolved config path used by this module."""
    return _CONFIG_PATH
