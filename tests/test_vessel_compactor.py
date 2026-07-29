"""Tests for the Rift Vessel Compactor.

Two layers, no real DB / LLM:

* the operational-recap core in :mod:`core.vessel_diary_compactor`
  (``_stringify_metadata`` / ``_activity_row_to_line`` / ``_recap_fallback`` /
  ``compact_activity_recap``);
* the plugin ``VesselCompactorPlugin`` (handler registration in start/stop, the
  ENDED-only enqueue, the worker draining onto ``compact_activity_recap`` and the
  manual ``compact_now`` run).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from core import vessel_diary_compactor as vdc


# ======================================================================
# Operational-recap core
# ======================================================================


class _FakeEngine:
    """Engine stub that echoes recap chunk/fold payloads back as JSON."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def generate_response(self, prompt: dict[str, Any]) -> str:
        self.calls.append(prompt)
        payload = prompt["input"]["payload"]
        if "activity" in payload:
            n = len(payload["activity"])
            return json.dumps({"partial": f"recap-of-{n}"})
        frags = payload["fragments"]
        return json.dumps({"entry": "FOLDED:" + "|".join(frags)})


class _BrokenEngine:
    async def generate_response(self, prompt: dict[str, Any]) -> str:
        raise RuntimeError("boom")


def _patch_engine(monkeypatch: Any, engine: Any) -> None:
    async def _resolve() -> Any:
        return engine

    monkeypatch.setattr(vdc, "_resolve_engine", _resolve)


def test_stringify_metadata_renders_factual_pairs() -> None:
    out = vdc._stringify_metadata({"x": 12, "y": -3, "block": "oak_log"})
    assert "x=12" in out
    assert "y=-3" in out
    assert "block=oak_log" in out


def test_stringify_metadata_parses_json_string() -> None:
    out = vdc._stringify_metadata('{"count": 4}')
    assert out == "count=4"


def test_stringify_metadata_failsafe() -> None:
    assert vdc._stringify_metadata(None) == ""
    assert vdc._stringify_metadata("") == ""
    # Non-JSON string is returned verbatim (stripped).
    assert vdc._stringify_metadata("  raw text  ") == "raw text"


def test_activity_row_to_line_third_person_format() -> None:
    line = vdc._activity_row_to_line(
        {
            "event_type": "mine",
            "summary": "mined a block",
            "metadata": {"block": "stone", "x": 1},
        }
    )
    assert line.startswith("[mine] mined a block")
    assert "block=stone" in line
    assert "x=1" in line
    # Operational recap must NOT be first-person.
    assert "I " not in line


def test_recap_fallback_is_deterministic_join() -> None:
    lines = ["[mine] a", "[goto] b"]
    out = vdc._recap_fallback("minecraft", lines, "session_ended")
    assert "a" in out and "b" in out
    # Deterministic: same input → same output.
    assert out == vdc._recap_fallback("minecraft", lines, "session_ended")


@pytest.mark.asyncio
async def test_compact_activity_recap_empty_returns_none(monkeypatch: Any) -> None:
    async def _no_lines(_sid: str) -> list[str]:
        return []

    monkeypatch.setattr(vdc, "load_activity_lines", _no_lines)
    result = await vdc.compact_activity_recap(
        session_id="s", environment="mc", interface_path=None, reason="session_ended"
    )
    assert result is None


@pytest.mark.asyncio
async def test_compact_activity_recap_stores_activity_recap_reason(
    monkeypatch: Any,
) -> None:
    async def _lines(_sid: str) -> list[str]:
        return [f"[mine] block {i}" for i in range(3)]

    monkeypatch.setattr(vdc, "load_activity_lines", _lines)
    monkeypatch.setattr(vdc, "_resolve_chunk_config", lambda: (40, 6000))
    _patch_engine(monkeypatch, _FakeEngine())

    saved: dict[str, Any] = {}

    async def _save(**kwargs: Any) -> int:
        saved.update(kwargs)
        return 99

    monkeypatch.setattr(vdc, "save_vessel_diary", _save)

    entry_id = await vdc.compact_activity_recap(
        session_id="s", environment="mc", interface_path="vessel/mc", reason="logout"
    )
    assert entry_id == 99
    # DEST reason is ALWAYS the recap constant, regardless of the arg passed.
    assert saved["reason"] == vdc.ACTIVITY_RECAP_REASON
    assert saved["session_id"] == "s"
    assert saved["moments_count"] == 3


@pytest.mark.asyncio
async def test_compact_activity_recap_llm_failure_falls_back(monkeypatch: Any) -> None:
    async def _lines(_sid: str) -> list[str]:
        return ["[mine] a", "[goto] b"]

    monkeypatch.setattr(vdc, "load_activity_lines", _lines)
    monkeypatch.setattr(vdc, "_resolve_chunk_config", lambda: (40, 6000))
    _patch_engine(monkeypatch, _BrokenEngine())

    saved: dict[str, Any] = {}

    async def _save(**kwargs: Any) -> int:
        saved.update(kwargs)
        return 7

    monkeypatch.setattr(vdc, "save_vessel_diary", _save)

    entry_id = await vdc.compact_activity_recap(
        session_id="s", environment="mc", interface_path=None, reason="session_ended"
    )
    assert entry_id == 7
    # A recap was still stored (deterministic fallback), still with recap reason.
    assert saved["reason"] == vdc.ACTIVITY_RECAP_REASON
    assert saved["summary"]


# ======================================================================
# Plugin
# ======================================================================


@pytest.fixture()
def plugin(monkeypatch: Any) -> Any:
    """A VesselCompactorPlugin with register_plugin / config stubbed out."""
    import core.config_manager as cm
    import core.core_initializer as ci

    monkeypatch.setattr(ci, "register_plugin", lambda name, inst: None)
    monkeypatch.setattr(cm.config_registry, "get_value", lambda *a, **k: True)
    monkeypatch.setattr(cm.config_registry, "add_listener", lambda *a, **k: None)

    from plugins.rift_vessel.vessel_compactor.vessel_compactor import (
        VesselCompactorPlugin,
    )

    return VesselCompactorPlugin()


def test_metadata_declares_runnable(plugin: Any) -> None:
    meta = plugin.get_metadata()
    assert meta["runnable"] is True
    assert meta["run_action"] == "compact_now"
    assert meta["category"] == "Vessels"
    assert meta["display_name"] == "Rift Vessel Compactor"


def test_no_supported_actions(plugin: Any) -> None:
    assert plugin.get_supported_actions() == {}


def test_on_session_ended_enqueues_when_enabled(plugin: Any) -> None:
    plugin.enabled = True
    plugin._on_session_ended("sess-1", "minecraft", "vessel/minecraft", "disconnected")
    assert plugin._queue.get_nowait() == "sess-1"


def test_on_session_ended_noop_when_disabled(plugin: Any) -> None:
    plugin.enabled = False
    plugin._on_session_ended("sess-1", "minecraft", None, "disconnected")
    assert plugin._queue.empty()


def test_on_session_ended_noop_without_session_id(plugin: Any) -> None:
    plugin.enabled = True
    plugin._on_session_ended("", "minecraft", None, "disconnected")
    assert plugin._queue.empty()


@pytest.mark.asyncio
async def test_start_registers_handler_stop_deregisters(
    plugin: Any, monkeypatch: Any
) -> None:
    import core.vessel_session_manager as vsm

    registered: list[Any] = []
    monkeypatch.setattr(
        vsm.vessel_session_manager,
        "set_compaction_handler",
        lambda h: registered.append(h),
    )

    await plugin.start()
    # Bound methods compare equal by (__self__, __func__).
    assert registered[-1] == plugin._on_session_ended
    assert plugin._worker_task is not None

    await plugin.stop()
    # Deregistered on stop.
    assert registered[-1] is None
    assert plugin._worker_task is None


@pytest.mark.asyncio
async def test_start_noop_when_disabled(plugin: Any, monkeypatch: Any) -> None:
    import core.vessel_session_manager as vsm

    called: list[Any] = []
    monkeypatch.setattr(
        vsm.vessel_session_manager,
        "set_compaction_handler",
        lambda h: called.append(h),
    )
    plugin.enabled = False
    await plugin.start()
    assert called == []
    assert plugin._worker_task is None


@pytest.mark.asyncio
async def test_compact_one_calls_recap_with_session_facts(
    plugin: Any, monkeypatch: Any
) -> None:
    async def _facts(_sid: str) -> tuple[str, str | None]:
        return "minecraft", "vessel/minecraft"

    monkeypatch.setattr(plugin, "_session_facts", _facts)

    seen: dict[str, Any] = {}

    async def _recap(**kwargs: Any) -> int:
        seen.update(kwargs)
        return 42

    monkeypatch.setattr(vdc, "compact_activity_recap", _recap)

    entry_id = await plugin._compact_one("sess-7")
    assert entry_id == 42
    assert seen["session_id"] == "sess-7"
    assert seen["environment"] == "minecraft"
    assert seen["interface_path"] == "vessel/minecraft"


@pytest.mark.asyncio
async def test_run_action_explicit_session(plugin: Any, monkeypatch: Any) -> None:
    async def _one(sid: str) -> int:
        assert sid == "target"
        return 5

    monkeypatch.setattr(plugin, "_compact_one", _one)
    out = await plugin.run_action("compact_now", {"session_id": "target"})
    assert out == {"status": "ok", "session_id": "target", "vessel_diary_id": 5}


@pytest.mark.asyncio
async def test_run_action_backlog_when_no_payload(
    plugin: Any, monkeypatch: Any
) -> None:
    processed: list[str] = []

    async def _pending(limit: int = 200) -> list[str]:
        return ["s1", "s2", "s3"]

    async def _one(sid: str) -> int:
        processed.append(sid)
        return len(processed)

    monkeypatch.setattr(plugin, "_pending_ended_sessions", _pending)
    monkeypatch.setattr(plugin, "_compact_one", _one)
    out = await plugin.run_action("compact_now", None)
    assert out["status"] == "ok"
    assert out["pending"] == 3
    assert out["compacted"] == 3
    assert processed == ["s1", "s2", "s3"]


@pytest.mark.asyncio
async def test_run_action_empty_when_no_ended_session(
    plugin: Any, monkeypatch: Any
) -> None:
    async def _pending(limit: int = 200) -> list[str]:
        return []

    monkeypatch.setattr(plugin, "_pending_ended_sessions", _pending)
    out = await plugin.run_action("compact_now", {})
    assert out["status"] == "empty"
    assert out["reason"] == "no_ended_session"


@pytest.mark.asyncio
async def test_run_action_backlog_counts_only_recapped(
    plugin: Any, monkeypatch: Any
) -> None:
    async def _pending(limit: int = 200) -> list[str]:
        return ["s1", "s2"]

    async def _one(sid: str) -> int | None:
        # s2 has an empty activity log → compact_activity_recap returns None.
        return 7 if sid == "s1" else None

    monkeypatch.setattr(plugin, "_pending_ended_sessions", _pending)
    monkeypatch.setattr(plugin, "_compact_one", _one)
    out = await plugin.run_action("compact_now", None)
    assert out["status"] == "ok"
    assert out["pending"] == 2
    assert out["compacted"] == 1


@pytest.mark.asyncio
async def test_run_action_rejects_unknown(plugin: Any) -> None:
    with pytest.raises(ValueError):
        await plugin.run_action("nope")


@pytest.mark.asyncio
async def test_worker_drains_queue(plugin: Any, monkeypatch: Any) -> None:
    processed: list[str] = []

    async def _one(sid: str) -> int:
        processed.append(sid)
        return 1

    monkeypatch.setattr(plugin, "_compact_one", _one)
    plugin._running = True
    plugin._queue.put_nowait("a")
    plugin._queue.put_nowait("b")
    worker = asyncio.create_task(plugin._worker_loop())
    await plugin._queue.join()
    plugin._running = False
    worker.cancel()
    try:
        await worker
    except asyncio.CancelledError:
        pass
    assert processed == ["a", "b"]
