"""Tests for Drones — ephemeral, task-scoped sub-agents.

Covers:
* spawn_drone action delegates to AgentLoopManager.run_drone and returns final_text.
* Recursion guard: a Drone cannot spawn another Drone (handler + prompt filter).
* Engine override: an explicit engine in the payload is forwarded to the loop.
* Drone metadata is tagged on the persisted turn.
"""

from typing import Any

import pytest

from core.agent_core import AgentLoopManager
from plugins.agent_plugin import AgentPlugin


@pytest.mark.asyncio
async def test_spawn_drone_delegates_and_returns_final_text(monkeypatch):
    """spawn_drone runs a Drone loop and returns its final text."""
    captured: dict[str, Any] = {}

    async def fake_run_drone(
        *,
        goal,
        engine=None,
        context=None,
        parent_task_id=None,
        max_iterations=None,
        timeout_seconds=None,
        original_message=None,
    ):
        captured["goal"] = goal
        captured["engine"] = engine
        captured["parent_task_id"] = parent_task_id
        return {
            "iterations": 1,
            "observations": [],
            "final_text": "drone finished the sub-task",
            "stop_reason": "model_done",
            "task_id": 4242,
        }

    from core.agent_core import get_agent_loop_manager

    manager = get_agent_loop_manager()
    monkeypatch.setattr(manager, "run_drone", fake_run_drone)

    plugin = AgentPlugin(notify_fn=lambda m: None)
    res = await plugin.execute_action(
        {"type": "spawn_drone", "payload": {"goal": "look up the config value"}},
        {"agent_task_id": 7},
        None,
        None,
    )

    assert res["ok"] is True
    assert res["final_text"] == "drone finished the sub-task"
    assert res["task_id"] == 4242
    assert captured["goal"] == "look up the config value"
    assert captured["parent_task_id"] == 7


@pytest.mark.asyncio
async def test_spawn_drone_requires_goal():
    """spawn_drone without a goal returns an error, no drone spawned."""
    plugin = AgentPlugin(notify_fn=lambda m: None)
    res = await plugin.execute_action(
        {"type": "spawn_drone", "payload": {}},
        {},
        None,
        None,
    )
    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_drone_cannot_spawn_drone(monkeypatch):
    """A Drone attempting spawn_drone is blocked before any loop runs."""

    spawned = {"count": 0}

    async def fake_run_drone(**kwargs):
        spawned["count"] += 1
        return {"final_text": "", "iterations": 0, "stop_reason": "model_done"}

    from core.agent_core import get_agent_loop_manager

    manager = get_agent_loop_manager()
    monkeypatch.setattr(manager, "run_drone", fake_run_drone)

    plugin = AgentPlugin(notify_fn=lambda m: None)
    # Context marks this execution as running inside a Drone.
    res = await plugin.execute_action(
        {"type": "spawn_drone", "payload": {"goal": "spawn another drone"}},
        {"drone": {"is_drone": True, "parent_task_id": 1}},
        None,
        None,
    )

    assert res["ok"] is False
    assert res["error"] == "drones_cannot_spawn_drones"
    assert spawned["count"] == 0


def test_spawn_drone_hidden_from_drone_tool_list(monkeypatch):
    """_build_agent_prompt excludes spawn_drone from a Drone's tool list."""
    from core.tool_registry import tool_registry, UnifiedToolManifest, SOURCE_INTERNAL

    tool_registry._tools.clear()
    for name in ("spawn_drone", "agent_read_file"):
        tool_registry._tools[name] = UnifiedToolManifest(
            name=name,
            description="t",
            parameters=[],
            source=SOURCE_INTERNAL,
            security_level="medium",
            external_effects=[],
        )

    try:
        # Non-drone context: spawn_drone is visible.
        agent_prompt = AgentLoopManager._build_agent_prompt(
            "goal", [], engine=None, context={}
        )
        agent_text = agent_prompt["input"]["payload"]["text"]
        assert "spawn_drone" in agent_text

        # Drone context: spawn_drone is hidden.
        drone_prompt = AgentLoopManager._build_agent_prompt(
            "goal", [], engine=None, context={"drone": {"is_drone": True}}
        )
        drone_text = drone_prompt["input"]["payload"]["text"]
        assert "spawn_drone" not in drone_text
        assert "agent_read_file" in drone_text
    finally:
        tool_registry._tools.clear()


@pytest.mark.asyncio
async def test_drone_engine_override_forwarded(monkeypatch):
    """An explicit engine in spawn_drone payload is passed to run_agentic_turn."""
    captured: dict[str, Any] = {}

    async def fake_run_agentic_turn(
        *,
        goal,
        engine=None,
        context=None,
        max_iterations=None,
        timeout_seconds=None,
        original_message=None,
        preplanned_calls=None,
        cortex_scope="agent",
    ):
        captured["engine"] = engine
        captured["context"] = context
        captured["max_iterations"] = max_iterations
        captured["cortex_scope"] = cortex_scope
        return {
            "iterations": 1,
            "observations": [],
            "final_text": "ok",
            "stop_reason": "model_done",
            "task_id": None,
        }

    manager = AgentLoopManager()
    monkeypatch.setattr(manager, "run_agentic_turn", fake_run_agentic_turn)

    out = await manager.run_drone(
        goal="do it",
        engine="my-tool-cortex",
        parent_task_id=5,
        max_iterations=2,
        cortex_scope="vessel",
    )

    assert out["final_text"] == "ok"
    assert captured["engine"] == "my-tool-cortex"
    assert captured["context"]["drone"] == {"is_drone": True, "parent_task_id": 5}
    assert captured["max_iterations"] == 2
    assert captured["cortex_scope"] == "vessel"


@pytest.mark.asyncio
async def test_agent_drone_uses_agent_budget_and_stays_single_level(monkeypatch):
    """run_agent_drone runs the agentic loop with the AGENT budget (not the tight
    Drone budget) while keeping every Drone safety property.

    It must:
    * default max_iterations to AGENT_MAX_ITERATIONS (30), not DRONE (3);
    * default timeout to AGENT_TURN_TIMEOUT_SEC (120), not DRONE (90);
    * tag the context as a Drone (is_drone) AND as an agent Drone so it can never
      spawn further Drones (single-level delegation);
    * forward the allowed_tools allow-list and the cortex_scope.
    """
    captured: dict[str, Any] = {}

    async def fake_run_agentic_turn(
        *,
        goal,
        engine=None,
        context=None,
        max_iterations=None,
        timeout_seconds=None,
        original_message=None,
        preplanned_calls=None,
        cortex_scope="agent",
    ):
        captured["max_iterations"] = max_iterations
        captured["timeout_seconds"] = timeout_seconds
        captured["context"] = context
        captured["cortex_scope"] = cortex_scope
        return {
            "iterations": 1,
            "observations": [],
            "final_text": "planned",
            "stop_reason": "model_done",
            "task_id": None,
        }

    from core.config_manager import config_registry

    manager = AgentLoopManager()
    monkeypatch.setattr(manager, "run_agentic_turn", fake_run_agentic_turn)
    monkeypatch.setattr(
        config_registry,
        "get_var",
        lambda key, default=None: {
            "AGENT_MAX_ITERATIONS": 30,
            "AGENT_TURN_TIMEOUT_SEC": 120,
            "DRONE_MAX_ITERATIONS": 3,
            "DRONE_TURN_TIMEOUT_SEC": 90,
        }.get(key, default),
    )

    out = await manager.run_agent_drone(
        goal="break the goal into a plan",
        parent_task_id=9,
        allowed_tools={
            "vessel_minecraft_lookup_knowledge",
            "vessel_minecraft_update_goal",
        },
        cortex_scope="vessel",
    )

    assert out["final_text"] == "planned"
    # Agent budget, not the tight Drone budget.
    assert captured["max_iterations"] == 30
    assert captured["timeout_seconds"] == 120.0
    assert captured["cortex_scope"] == "vessel"
    drone_meta = captured["context"]["drone"]
    # Single-level delegation: flagged as a Drone so it cannot spawn Drones.
    assert drone_meta["is_drone"] is True
    assert drone_meta["is_agent_drone"] is True
    assert drone_meta["parent_task_id"] == 9
    assert drone_meta["allowed_tools"] == [
        "vessel_minecraft_lookup_knowledge",
        "vessel_minecraft_update_goal",
    ]


@pytest.mark.asyncio
async def test_agent_drone_respects_explicit_budget_override(monkeypatch):
    """An explicit max_iterations/timeout on run_agent_drone wins over defaults."""
    captured: dict[str, Any] = {}

    async def fake_run_agentic_turn(**kwargs):
        captured.update(kwargs)
        return {"final_text": "ok", "iterations": 1, "stop_reason": "model_done"}

    manager = AgentLoopManager()
    monkeypatch.setattr(manager, "run_agentic_turn", fake_run_agentic_turn)

    await manager.run_agent_drone(
        goal="do it",
        max_iterations=12,
        timeout_seconds=45.0,
    )

    assert captured["max_iterations"] == 12
    assert captured["timeout_seconds"] == 45.0


@pytest.mark.asyncio
async def test_drone_metadata_tagged_on_persist(monkeypatch):
    """A Drone turn tags metadata.source=drone and records parent_task_id."""
    captured: dict[str, Any] = {}

    async def fake_persist(
        self,
        *,
        engine,
        goal,
        result,
        context=None,
        original_message=None,
        preplanned_calls=None,
        task_id=None,
    ):
        captured["context"] = context
        return None

    async def fake_handle(bot, message, context_memory_or_prompt):
        return "done"

    monkeypatch.setattr("core.plugin_instance.handle_incoming_message", fake_handle)
    monkeypatch.setattr(AgentLoopManager, "_persist_agentic_turn", fake_persist)

    manager = AgentLoopManager()
    await manager.run_drone(goal="sub-task", engine="e", parent_task_id=9)

    ctx = captured["context"]
    assert ctx["drone"]["is_drone"] is True
    assert ctx["drone"]["parent_task_id"] == 9
