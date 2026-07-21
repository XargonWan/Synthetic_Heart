"""Synth runtime MCP bridge package.

Separate from the dev-only ``.mcp.json`` / ``mcp_servers/`` tooling.  This
package lets Synth *consume* external MCP tools (client) and later *expose*
its own actions as MCP tools (server).  See ``config.py`` for the isolated
configuration loader.
"""

from core.mcp_bridge.config import (
    SynthMcpServerConfig,
    get_synth_mcp_config_path,
    load_enabled_synth_mcp_servers,
    load_synth_mcp_servers,
)
from core.mcp_bridge.client import McpClientBridge, McpConnection, mcp_client_bridge

__all__ = [
    "SynthMcpServerConfig",
    "load_synth_mcp_servers",
    "load_enabled_synth_mcp_servers",
    "get_synth_mcp_config_path",
    "McpClientBridge",
    "McpConnection",
    "mcp_client_bridge",
]
