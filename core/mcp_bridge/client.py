"""Synth MCP client bridge — lets Synth *consume* external MCP tools.

This is the runtime counterpart to the dev-only ``.mcp.json`` servers.  It
connects to the MCP servers declared in ``config/synth_mcp.json`` (loaded by
:mod:`core.mcp_bridge.config`), discovers their tools via ``list_tools()``,
and registers each one into the unified :class:`core.tool_registry.ToolRegistry`
so they become first-class tools for the agent loop.

Transports supported:
* ``stdio``   — launch a local process (``command`` + ``args`` + ``env``).
* ``sse`` / ``http`` / ``streamable_http`` — connect to a remote MCP endpoint.

The bridge is async and connection-oriented: call :meth:`McpClientBridge.connect_all`
once at startup (or on demand), then :meth:`McpClientBridge.call_tool` to invoke
a discovered tool.  Connections are cached per server and reused.
"""

from __future__ import annotations

import asyncio
import os
import time
from typing import Any

from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.mcp_bridge.config import (
    SynthMcpServerConfig,
    load_enabled_synth_mcp_servers,
)

# Lazy imports of the mcp client API — kept inside functions so that a missing
# or partial mcp install does not break import of this module in lightweight
# environments (e.g. build-time checks, unit tests that never touch MCP).


class McpConnection:
    """Holds an open MCP client session for one server."""

    def __init__(self, server_name: str) -> None:
        self.server_name = server_name
        self.session: Any = None
        self._stack: Any = None  # AsyncExitStack context
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        if self._stack is not None:
            try:
                await self._stack.aclose()
            except BaseExceptionGroup as exc:  # pragma: no cover - SDK teardown
                # anyio/MCP SDK: "Attempted to exit cancel scope in a different
                # task than it was entered in" fires when the stdio client's
                # task group was cancelled (e.g. loop teardown / task GC) and
                # the AsyncExitStack is then closed from the caller's task.
                # The connection is already unusable; swallow it so a teardown
                # of an optional MCP client can never crash the event loop or
                # cancel unrelated tasks (observed: shutdown cancelled the DB
                # query in _recover_interrupted_agent_tasks and every
                # background loop). Best-effort cleanup only.
                log_debug(
                    f"[mcp_client] Suppressed anyio teardown error closing "
                    f"'{self.server_name}': {exc}"
                )
            except RuntimeError as exc:  # pragma: no cover - same SDK issue
                if "cancel scope" in str(exc):
                    log_debug(
                        f"[mcp_client] Suppressed cancel-scope teardown error "
                        f"closing '{self.server_name}': {exc}"
                    )
                else:
                    log_debug(
                        f"[mcp_client] Error closing session for "
                        f"'{self.server_name}': {exc}"
                    )
            except Exception as exc:  # pragma: no cover - best effort cleanup
                log_debug(
                    f"[mcp_client] Error closing session for "
                    f"'{self.server_name}': {exc}"
                )
            finally:
                self._stack = None
                self.session = None


class McpClientBridge:
    """Discovers and invokes tools from Synth's configured MCP servers."""

    def __init__(self, registry: Any | None = None) -> None:
        from core.tool_registry import tool_registry

        self.registry = registry or tool_registry
        self._connections: dict[str, McpConnection] = {}
        self._connected = False
        self._connect_lock = asyncio.Lock()
        self._last_connect_attempt = 0.0
        self._retry_backoff_seconds = 20.0

    # -- connection management --------------------------------------------

    async def connect_all(
        self,
        config_path: Any | None = None,
        *,
        force: bool = False,
    ) -> int:
        """Connect to every enabled server and register its tools.

        Returns the number of servers successfully connected.
        """
        async with self._connect_lock:
            now = time.monotonic()
            if self._connected:
                if self._connections:
                    log_debug(
                        "[mcp_client] connect_all() called again; reusing existing MCP "
                        "connections."
                    )
                    return len(self._connections)

                retry_due = (
                    force
                    or (now - self._last_connect_attempt) >= self._retry_backoff_seconds
                )
                if not retry_due:
                    log_debug(
                        "[mcp_client] connect_all() called again after previous "
                        "attempt with no active MCP connections; skipping retry "
                        "until backoff expires."
                    )
                    return len(self._connections)

                log_info(
                    "[mcp_client] Retrying MCP server connections after prior "
                    "failed/empty attempt."
                )

            servers = load_enabled_synth_mcp_servers(config_path)
            self._last_connect_attempt = now
            if not servers:
                log_info(
                    "[mcp_client] No enabled Synth MCP servers configured; "
                    "skipping connection."
                )
                self._connected = True
                return 0

            connected = 0
            for cfg in servers.values():
                try:
                    await self.connect_server(cfg)
                    connected += 1
                except Exception as exc:
                    log_error(
                        f"[mcp_client] Failed to connect to MCP server '{cfg.name}': {exc}"
                    )
            self._connected = True
            log_info(
                f"[mcp_client] Connected {connected}/{len(servers)} Synth MCP server(s)."
            )
            return connected

    async def connect_server(self, cfg: SynthMcpServerConfig) -> McpConnection:
        """Connect to a single server and register its tools."""
        existing = self._connections.get(cfg.name)
        if existing is not None:
            await existing.close()
            self.registry.clear_mcp_tools(cfg.name)

        conn = McpConnection(cfg.name)
        async with conn._lock:
            # Bound the connect: a hung stdio spawn (server not reading) would
            # otherwise leave the anyio task group half-open, and a later
            # cancellation during shutdown then corrupts the loop with
            # "Attempted to exit cancel scope in a different task than it was
            # entered in" (observed live: MCP teardown cancelled the DB query
            # in _recover_interrupted_agent_tasks and every background loop).
            try:
                stack, session = await asyncio.wait_for(
                    self._open_session(cfg), timeout=15
                )
            except asyncio.TimeoutError:
                log_warning(
                    f"[mcp_client] Timed out connecting to MCP server "
                    f"'{cfg.name}' (transport {cfg.transport}); skipping."
                )
                return conn
            conn._stack = stack
            conn.session = session
            self._connections[cfg.name] = conn

        tools = await self._list_and_register(cfg, session)
        log_info(f"[mcp_client] Server '{cfg.name}' exposed {len(tools)} tool(s).")
        return conn

    async def _open_session(self, cfg: SynthMcpServerConfig) -> tuple[Any, Any]:
        """Open a client session for the given server config."""
        from contextlib import AsyncExitStack

        from mcp.client.session import ClientSession

        stack = AsyncExitStack()
        url = cfg.url
        command = cfg.command

        try:
            if cfg.transport == "stdio":
                if not isinstance(command, str) or not command:
                    raise ValueError(
                        f"MCP server '{cfg.name}' transport 'stdio' requires a "
                        "non-empty command"
                    )
                from mcp.client.stdio import StdioServerParameters, stdio_client

                server_params = StdioServerParameters(
                    command=command,
                    args=list(cfg.args),
                    env={**os.environ, **cfg.env} if cfg.env else None,
                )
                read, write = await stack.enter_async_context(
                    stdio_client(server_params)
                )
                session = await stack.enter_async_context(ClientSession(read, write))
            elif cfg.transport in ("sse", "http", "streamable_http"):
                if not isinstance(url, str) or not url:
                    raise ValueError(
                        f"MCP server '{cfg.name}' transport '{cfg.transport}' "
                        "requires a non-empty url"
                    )
                if cfg.transport == "sse":
                    from mcp.client.sse import sse_client
                    from mcp.client.session import (
                        SseServerParameters,  # type: ignore[attr-defined]
                    )

                    params = SseServerParameters(url=url)
                    read, write = await stack.enter_async_context(sse_client(params))
                else:
                    from mcp.client.streamable_http import (
                        streamablehttp_client,
                    )

                    read, write, _ = await stack.enter_async_context(
                        streamablehttp_client(url)
                    )
                session = await stack.enter_async_context(ClientSession(read, write))
            else:  # pragma: no cover - guarded earlier in config loader
                raise ValueError(f"Unsupported transport: {cfg.transport}")

            await session.initialize()
            return stack, session
        except Exception:
            # A failed spawn/connect (e.g. the stdio server exits immediately ->
            # McpError: Connection closed) leaves the AsyncExitStack with
            # partially-entered anyio contexts. Abandoning it lets the async
            # generator be garbage-collected in a DIFFERENT task than the one
            # that entered it, which anyio reports as "Attempted to exit cancel
            # scope in a different task than it was entered in" and corrupts
            # the whole event loop (observed live: MCP teardown cancelled the
            # DB query in _recover_interrupted_agent_tasks and every background
            # loop). Close the partial stack HERE, in the task that entered it,
            # before re-raising so the caller sees the real connect error.
            try:
                await stack.aclose()
            except Exception as close_exc:  # pragma: no cover - best effort
                log_debug(
                    f"[mcp_client] Best-effort close of failed session for "
                    f"'{cfg.name}' also failed: {close_exc}"
                )
            raise

    async def _list_and_register(
        self, cfg: SynthMcpServerConfig, session: Any
    ) -> list[Any]:
        """Call ``list_tools`` and register each tool into the registry."""
        from core.live_tool_registry import ToolParameter

        result = await session.list_tools()
        tools = getattr(result, "tools", []) or []

        # Clear stale tools from this server before re-registering.
        self.registry.clear_mcp_tools(cfg.name)

        for tool in tools:
            parameters: list[ToolParameter] = []
            schema = getattr(tool, "inputSchema", None) or {}
            properties = schema.get("properties", {})
            required = set(schema.get("required", []) or [])
            for pname, pmeta in properties.items():
                if not isinstance(pmeta, dict):
                    continue
                parameters.append(
                    ToolParameter(
                        name=str(pname),
                        type=str(pmeta.get("type", "string")).lower(),
                        description=str(pmeta.get("description", "")),
                        required=str(pname) in required,
                        enum=pmeta.get("enum") or None,
                        schema=pmeta,
                    )
                )
            self.registry.add_mcp_tool(
                server_name=cfg.name,
                name=getattr(tool, "name", "unknown"),
                description=getattr(tool, "description", "") or "",
                parameters=parameters,
                security_level=cfg.security_level,
            )
        return tools

    # -- tool invocation --------------------------------------------------

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any] | None = None,
    ) -> Any:
        """Invoke a registered MCP tool by its unified (namespaced) name.

        Args:
            tool_name: The namespaced name as stored in the registry
                (``mcp_<server>_<tool>``).
            arguments: Arguments to pass to the underlying MCP tool.

        Returns:
            The raw MCP tool result content (list of content blocks).

        Raises:
            KeyError: if the tool is not a known MCP tool.
            RuntimeError: if the server connection is not open.
        """
        tool = self.registry.get_tool(tool_name)
        if tool is None or not tool.is_external():
            raise KeyError(f"Not a registered MCP tool: {tool_name}")

        server_name = tool.server_name
        if not isinstance(server_name, str) or not server_name:
            raise RuntimeError(
                f"MCP tool '{tool_name}' has invalid server_name '{server_name}'"
            )
        conn = self._connections.get(server_name)
        if conn is None or conn.session is None:
            raise RuntimeError(f"MCP server '{server_name}' is not connected.")

        # Strip the registry namespace to recover the real MCP tool name.
        real_name = tool_name
        prefix = f"mcp_{server_name}_"
        if real_name.startswith(prefix):
            real_name = real_name[len(prefix) :]

        log_debug(
            f"[mcp_client] Calling MCP tool '{real_name}' on "
            f"'{server_name}' with args={arguments}"
        )
        result = await conn.session.call_tool(real_name, arguments or {})
        return result

    # -- teardown ----------------------------------------------------------

    async def disconnect_all(self) -> None:
        """Close all open server connections and clear MCP tools."""
        for conn in list(self._connections.values()):
            # Closing a cancelled/failed stdio session can raise the anyio
            # cancel-scope error; never let MCP teardown crash app shutdown.
            try:
                await asyncio.wait_for(conn.close(), timeout=5)
            except asyncio.TimeoutError:
                log_debug(
                    f"[mcp_client] Timed out closing session for '{conn.server_name}'"
                )
            except Exception as exc:
                log_debug(
                    f"[mcp_client] Suppressed error closing session for "
                    f"'{conn.server_name}': {exc}"
                )
        self._connections.clear()
        self.registry.clear_mcp_tools()
        self._connected = False
        log_info("[mcp_client] Disconnected all Synth MCP servers.")


# Module-level singleton.
mcp_client_bridge = McpClientBridge()
