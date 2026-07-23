"""Tests for the /vessel and /minecraft slash commands (no DB, no LLM)."""

from __future__ import annotations

import os
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

os.environ.setdefault("BOTFATHER_TOKEN", "test")

from core.command_registry import execute_command, list_commands


def test_commands_registered() -> None:
    cmds = list_commands()
    assert "vessel" in cmds
    assert "minecraft" in cmds


@pytest.mark.asyncio
async def test_vessel_status_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "core.config_manager.config_registry.get_value",
        lambda key, default=None: "disabled",
    )
    monkeypatch.setattr(
        "core.vessel_registry.VESSEL_REGISTRY.get_available_connectors",
        lambda: ["minecraft"],
    )
    monkeypatch.setattr(
        "core.vessel_registry.VESSEL_REGISTRY.get_connector_meta",
        lambda name: {"label": "Minecraft (PoC)"},
    )
    out = await execute_command("vessel")
    assert "Rift Vessel" in out
    assert "disabled" in out
    assert "minecraft" in out


@pytest.mark.asyncio
async def test_vessel_bad_subcommand() -> None:
    out = await execute_command("vessel", "explode")
    assert "Use:" in out


@pytest.mark.asyncio
async def test_minecraft_provision_status(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeProv:
        def status(self) -> Dict[str, Any]:
            return {
                "ok": True,
                "enabled": True,
                "installed": False,
                "running": False,
                "pid": None,
            }

    monkeypatch.setattr(
        "interface.minecraft_provisioner.get_bridge_provisioner",
        lambda: _FakeProv(),
    )
    out = await execute_command("minecraft", "provision", "status")
    assert "Minecraft bridge" in out
    assert "Enabled" in out


@pytest.mark.asyncio
async def test_minecraft_provision_start(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeProv:
        start = AsyncMock(return_value={"ok": True, "detail": "started"})

    monkeypatch.setattr(
        "interface.minecraft_provisioner.get_bridge_provisioner",
        lambda: _FakeProv(),
    )
    out = await execute_command("minecraft", "provision", "start")
    assert "✅" in out
    assert "started" in out


@pytest.mark.asyncio
async def test_minecraft_provision_logs(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeProv:
        def logs(self, lines: int = 100) -> Dict[str, Any]:
            return {"ok": True, "lines": ["a", "b"]}

    monkeypatch.setattr(
        "interface.minecraft_provisioner.get_bridge_provisioner",
        lambda: _FakeProv(),
    )
    out = await execute_command("minecraft", "provision", "logs", "5")
    assert "logs" in out
    assert "a" in out


@pytest.mark.asyncio
async def test_minecraft_usage_without_provision() -> None:
    out = await execute_command("minecraft", "foo")
    assert "Use:" in out
