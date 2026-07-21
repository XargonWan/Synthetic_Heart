import pytest
import json
from dataclasses import dataclass
from typing import cast

from fastapi import Request

from core.webui import SynthWebUIInterface
from plugins.agent_core import AgentCorePlugin
from core.core_initializer import PLUGIN_REGISTRY


class FakeRequest:
    def __init__(self, payload=None):
        self._payload = payload or {}

    async def json(self):
        return self._payload


@dataclass
class _FakeParam:
    name: str
    type: str = "string"
    description: str = ""
    required: bool = False
    enum: list[str] | None = None


@dataclass
class _FakeTool:
    name: str
    description: str
    source: str
    security_level: str
    external_effects: list[str]
    server_name: str | None
    parameters: list[_FakeParam]


@pytest.mark.asyncio
async def test_approve_agent_proposal_calls_plugin(monkeypatch):
    plugin = AgentCorePlugin()

    # Register plugin in PLUGIN_REGISTRY
    prev = PLUGIN_REGISTRY.get("agent")
    PLUGIN_REGISTRY["agent"] = plugin

    try:
        webui = SynthWebUIInterface(autostart=False)

        called = {}

        async def fake_execute(action, context, bot, original_message):
            called["action"] = action
            called["original_message"] = original_message
            return {"status": "ok", "executed": True}

        monkeypatch.setattr(plugin, "execute_action", fake_execute)

        req = FakeRequest({"trainer": "webui"})
        resp = await webui.approve_agent_proposal(42, cast(Request, req))
        assert resp is not None
        # JSONResponse.body is bytes
        body = resp.body
        data = json.loads(body)
        assert "result" in data
        assert data["result"]["status"] == "ok"
        assert called["action"]["type"] == "approve_action"
        assert called["action"]["payload"]["proposal_id"] == 42
    finally:
        # restore
        if prev is None:
            PLUGIN_REGISTRY.pop("agent", None)
        else:
            PLUGIN_REGISTRY["agent"] = prev


@pytest.mark.asyncio
async def test_list_agent_tools_returns_catalog(monkeypatch):
    webui = SynthWebUIInterface(autostart=False)

    from core.tool_registry import tool_registry
    from core.mcp_bridge.client import mcp_client_bridge

    monkeypatch.setattr(tool_registry, "load_internal_actions", lambda actions: [])

    async def _fake_connect_all():
        return 0

    monkeypatch.setattr(mcp_client_bridge, "connect_all", _fake_connect_all)
    monkeypatch.setattr(
        tool_registry,
        "all_tools",
        lambda: [
            _FakeTool(
                name="search_current_knowledge",
                description="Search web",
                source="internal",
                security_level="low",
                external_effects=[],
                server_name=None,
                parameters=[_FakeParam(name="query", required=True)],
            ),
            _FakeTool(
                name="mcp_filesystem_read_file",
                description="Read file",
                source="mcp:filesystem",
                security_level="medium",
                external_effects=["mcp:filesystem"],
                server_name="filesystem",
                parameters=[_FakeParam(name="path", required=True)],
            ),
        ],
    )

    resp = await webui.list_agent_tools()
    data = json.loads(resp.body)
    assert "tools" in data
    names = [t["name"] for t in data["tools"]]
    assert "search_current_knowledge" in names
    assert "mcp_filesystem_read_file" in names


@pytest.mark.asyncio
async def test_run_agent_turn_executes_manager(monkeypatch):
    webui = SynthWebUIInterface(autostart=False)

    class _FakeBridge:
        async def connect_all(self):
            return 0

    class _FakeRegistry:
        def load_internal_actions(self, actions):
            return []

    class _FakeManager:
        async def run_agentic_turn(self, **kwargs):
            assert kwargs["goal"] == "test goal"
            return {
                "stop_reason": "model_done",
                "iterations": 1,
                "observations": [],
                "final_text": "done",
            }

    monkeypatch.setattr("core.webui.tool_registry", _FakeRegistry(), raising=False)
    monkeypatch.setattr("core.webui.mcp_client_bridge", _FakeBridge(), raising=False)
    monkeypatch.setattr(
        "core.webui.get_agent_loop_manager", lambda: _FakeManager(), raising=False
    )

    # Patch imports inside method to use our fakes
    monkeypatch.setattr(
        "core.tool_registry.tool_registry", _FakeRegistry(), raising=False
    )
    monkeypatch.setattr(
        "core.mcp_bridge.client.mcp_client_bridge", _FakeBridge(), raising=False
    )
    monkeypatch.setattr(
        "core.agent_core.get_agent_loop_manager", lambda: _FakeManager(), raising=False
    )

    req = FakeRequest({"prompt": "test goal", "max_iterations": 3})
    resp = await webui.run_agent_turn(cast(Request, req))
    data = json.loads(resp.body)
    assert data["result"]["stop_reason"] == "model_done"
    assert data["result"]["final_text"] == "done"
