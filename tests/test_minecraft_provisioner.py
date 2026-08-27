"""Tests for the Minecraft bridge provisioner (mock config + subprocess)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import pytest

from interface.minecraft_provisioner import (
    BridgeProvisioner,
    get_bridge_provisioner,
)


def _make_prov(tmp_path: Path) -> BridgeProvisioner:
    return BridgeProvisioner(bridge_root=str(tmp_path / "bridge"))


def _mock_config(monkeypatch: pytest.MonkeyPatch, values: Dict[str, Any]) -> None:
    monkeypatch.setattr(
        "interface.minecraft_provisioner.config_registry.get_value",
        lambda key, default=None, **kwargs: values.get(key, default),
    )


def test_singleton() -> None:
    assert get_bridge_provisioner() is get_bridge_provisioner()


def test_is_enabled_reads_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prov = _make_prov(tmp_path)
    _mock_config(monkeypatch, {"PLUGIN_ENABLED__minecraft_vessel": True})
    assert prov._is_enabled() is True
    _mock_config(monkeypatch, {"PLUGIN_ENABLED__minecraft_vessel": False})
    assert prov._is_enabled() is False


def test_bridge_env_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    prov = _make_prov(tmp_path)
    _mock_config(monkeypatch, {})
    env = prov._bridge_env()
    assert env["BRIDGE_HOST"] == "127.0.0.1"
    assert env["BRIDGE_PORT"] == "8137"
    assert env["MC_SERVER_PORT"] == "44383"
    # Empty override falls back to Synth's configured name (SYNTH_NAME).
    assert env["MC_BOT_USERNAME"] == "Synth"
    assert env["MC_AUTH"] == "offline"


def test_bridge_env_username_fallback_to_synth_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prov = _make_prov(tmp_path)
    # No override set -> the in-world username uses SYNTH_NAME.
    _mock_config(monkeypatch, {"SYNTH_NAME": "Rekku"})
    env = prov._bridge_env()
    assert env["MC_BOT_USERNAME"] == "Rekku"


def test_bridge_env_from_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prov = _make_prov(tmp_path)
    _mock_config(
        monkeypatch,
        {
            "MINECRAFT_BRIDGE_HOST": "0.0.0.0",
            "MINECRAFT_BRIDGE_PORT": 9999,
            "MINECRAFT_BOT_USERNAME_OVERRIDE": "Rei",
            "SYNTH_NAME": "Rekku",
        },
    )
    env = prov._bridge_env()
    assert env["BRIDGE_HOST"] == "0.0.0.0"
    assert env["BRIDGE_PORT"] == "9999"
    # Explicit override wins over SYNTH_NAME.
    assert env["MC_BOT_USERNAME"] == "Rei"


def test_status_when_not_running(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prov = _make_prov(tmp_path)
    _mock_config(monkeypatch, {"PLUGIN_ENABLED__minecraft_vessel": True})
    status = prov.status()
    assert status["ok"] is True
    assert status["enabled"] is True
    assert status["running"] is False
    assert status["pid"] is None
    assert status["installed"] is False


def test_logs_no_file(tmp_path: Path) -> None:
    prov = _make_prov(tmp_path)
    res = prov.logs()
    assert res["ok"] is True
    assert res["lines"] == []


def test_logs_reads_tail(tmp_path: Path) -> None:
    prov = _make_prov(tmp_path)
    prov._bridge_root.mkdir(parents=True, exist_ok=True)
    prov._log_file.write_text(
        "\n".join(f"line {i}" for i in range(10)), encoding="utf-8"
    )
    res = prov.logs(lines=3)
    assert res["ok"] is True
    assert res["lines"] == ["line 7", "line 8", "line 9"]


def test_logs_clamps_count(tmp_path: Path) -> None:
    prov = _make_prov(tmp_path)
    prov._bridge_root.mkdir(parents=True, exist_ok=True)
    prov._log_file.write_text("only\n", encoding="utf-8")
    # Absurdly large request is clamped; absurdly small is clamped up to 1.
    assert prov.logs(lines=99999)["ok"] is True
    assert prov.logs(lines=0)["ok"] is True


@pytest.mark.asyncio
async def test_install_refused_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prov = _make_prov(tmp_path)
    _mock_config(monkeypatch, {"PLUGIN_ENABLED__minecraft_vessel": False})
    res = await prov.install()
    assert res["ok"] is False
    assert "disabled" in res["detail"]


@pytest.mark.asyncio
async def test_start_refused_when_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prov = _make_prov(tmp_path)
    _mock_config(monkeypatch, {"PLUGIN_ENABLED__minecraft_vessel": False})
    res = await prov.start()
    assert res["ok"] is False
    assert "disabled" in res["detail"]


@pytest.mark.asyncio
async def test_start_reports_missing_node(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    prov = _make_prov(tmp_path)
    _mock_config(monkeypatch, {"PLUGIN_ENABLED__minecraft_vessel": True})
    monkeypatch.setattr(
        "interface.minecraft_provisioner.shutil.which", lambda name: None
    )
    res = await prov.start()
    assert res["ok"] is False
    assert "node" in res["detail"].lower()


@pytest.mark.asyncio
async def test_stop_idempotent_when_not_running(tmp_path: Path) -> None:
    prov = _make_prov(tmp_path)
    res = await prov.stop()
    assert res["ok"] is True
    assert res["detail"] == "not running"
