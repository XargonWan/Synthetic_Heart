"""MCP tools must pass the same safety gate as internal actions."""

import pytest

from core.agent_tool_executor import AgentToolExecutor
from core.tool_registry import ToolRegistry


@pytest.fixture()
def registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.add_mcp_tool("srv", "read", "read", [], security_level="low")
    reg.add_mcp_tool("srv", "write", "write", [], security_level="high")
    return reg


class _Bridge:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def call_tool(self, name: str, arguments: dict) -> str:
        self.calls.append(name)
        return "done"


@pytest.fixture()
def bridge(monkeypatch: pytest.MonkeyPatch) -> _Bridge:
    fake = _Bridge()
    monkeypatch.setattr("core.mcp_bridge.client.mcp_client_bridge", fake)
    return fake


@pytest.mark.asyncio
async def test_high_level_mcp_tool_blocked_in_suggest_mode(
    monkeypatch: pytest.MonkeyPatch, registry: ToolRegistry, bridge: _Bridge
) -> None:
    monkeypatch.setattr(
        "core.action_safety.config_registry.get_value",
        lambda key, default=None, **_: (
            "suggest" if key == "SYNTH_AUTONOMY_MODE" else default
        ),
    )
    out = await AgentToolExecutor(registry).execute("mcp_srv_write", {})
    assert out["ok"] is False and "not permitted" in out["error"]
    assert bridge.calls == []


@pytest.mark.asyncio
async def test_low_level_mcp_tool_still_runs(
    monkeypatch: pytest.MonkeyPatch, registry: ToolRegistry, bridge: _Bridge
) -> None:
    monkeypatch.setattr(
        "core.action_safety.config_registry.get_value",
        lambda key, default=None, **_: (
            "suggest" if key == "SYNTH_AUTONOMY_MODE" else default
        ),
    )
    out = await AgentToolExecutor(registry).execute("mcp_srv_read", {})
    assert out["ok"] is True
    assert bridge.calls == ["mcp_srv_read"]
