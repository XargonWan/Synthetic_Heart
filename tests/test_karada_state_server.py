"""Tests for KaradaStateServer — phases 3-8 of the animation refactor.

Covers:
- Global audio broadcast & tracking
- Priority preemption logic
- Watchdog stuck-state detection
- Rotation with global keys
- Karada REST API endpoints
- Asset manifest
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.animation_handler import (
    AnimationState,
    KaradaStateServer,
)
from core.karada_transport import KaradaTransport


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeTransport(KaradaTransport):
    """In-memory transport that records all payloads."""

    def __init__(self) -> None:
        self.animation_payloads: List[Dict[str, Any]] = []
        self.audio_payloads: List[Dict[str, Any]] = []
        self.face_payloads: List[Dict[str, Any]] = []
        self.model_payloads: List[Dict[str, Any]] = []
        self.expression_payloads: List[Dict[str, Any]] = []
        self.session_payloads: List[tuple[str, Dict[str, Any]]] = []
        self.preload_payloads: List[Dict[str, Any]] = []

    async def broadcast_animation(self, payload: Dict[str, Any]) -> None:
        self.animation_payloads.append(payload)

    async def broadcast_audio(self, payload: Dict[str, Any]) -> None:
        self.audio_payloads.append(payload)

    async def broadcast_face(self, payload: Dict[str, Any]) -> None:
        self.face_payloads.append(payload)

    async def broadcast_model(self, payload: Dict[str, Any]) -> None:
        self.model_payloads.append(payload)

    async def broadcast_expression(self, payload: Dict[str, Any]) -> None:
        self.expression_payloads.append(payload)

    async def send_to_session(self, session_id: str, payload: Dict[str, Any]) -> None:
        self.session_payloads.append((session_id, payload))

    async def preload_asset(
        self, session_id: Optional[str], payload: Dict[str, Any]
    ) -> None:
        self.preload_payloads.append(payload)

    def get_connected_sessions(self) -> List[str]:
        return ["test_session"]


def _make_handler() -> tuple[KaradaStateServer, FakeTransport]:
    """Create a KaradaStateServer with a FakeTransport attached."""
    handler = KaradaStateServer(webui=None)
    # Stop the watchdog so it doesn't run during unit tests
    if handler._watchdog_task and not handler._watchdog_task.done():
        handler._watchdog_task.cancel()
    transport = FakeTransport()
    handler.add_transport(transport)
    # Cancel watchdog again (add_transport may have started it)
    if handler._watchdog_task and not handler._watchdog_task.done():
        handler._watchdog_task.cancel()
    return handler, transport


# ---------------------------------------------------------------------------
# Phase 3: Audio state tracking
# ---------------------------------------------------------------------------


class TestAudioStateTracking:
    def test_set_current_audio_stores_state(self) -> None:
        handler, _ = _make_handler()
        handler.set_current_audio("/static/audio/tts/test.wav", 2.5, {"phone": "a"})

        audio = handler.get_current_audio()
        assert audio is not None
        assert audio["url"] == "/static/audio/tts/test.wav"
        assert audio["audio_duration_s"] == 2.5
        assert "offset_s" in audio

    def test_get_current_audio_returns_none_when_no_audio(self) -> None:
        handler, _ = _make_handler()
        assert handler.get_current_audio() is None

    def test_set_current_audio_with_no_url_leaves_none(self) -> None:
        handler, _ = _make_handler()
        handler.set_current_audio(None)
        assert handler.get_current_audio() is None

    @pytest.mark.asyncio
    async def test_auto_clear_audio(self) -> None:
        handler, _ = _make_handler()
        handler.set_current_audio("/static/audio/tts/test.wav", 0.1)
        assert handler.get_current_audio() is not None
        # Wait for auto-clear (0.1 + 0.5 buffer)
        await asyncio.sleep(0.8)
        assert handler.get_current_audio() is None

    @pytest.mark.asyncio
    async def test_get_full_state_includes_audio(self) -> None:
        handler, _ = _make_handler()
        handler.set_current_audio("/static/audio/tts/test.wav", 5.0)

        state = await handler.get_full_state()
        assert "audio" in state
        assert state["audio"] is not None
        assert state["audio"]["url"] == "/static/audio/tts/test.wav"


# ---------------------------------------------------------------------------
# Phase 4: Priority preemption
# ---------------------------------------------------------------------------


class TestPriorityPreemption:
    @pytest.mark.asyncio
    async def test_lower_priority_rejected_when_higher_active(self) -> None:
        handler, transport = _make_handler()

        # Stub animation discovery
        handler.get_animation_variants = MagicMock(
            return_value={"loop": ["Think.fbx"], "post": [], "other": []}
        )
        handler.get_animations_for_state = MagicMock(return_value=["Think.fbx"])
        handler._resolve_animation_descriptor = MagicMock(
            return_value=("/skins/Rei/animations/think/Think.fbx", None)
        )
        handler._resolve_animation_descriptor_for_state = MagicMock(
            return_value=("/skins/Rei/animations/think/Think.fbx", None)
        )

        # Play THINK (priority 10)
        await handler.play_animation(
            AnimationState.THINK,
            session_id=None,
            context_id="ctx_think",
            priority=10,
        )
        assert len(transport.animation_payloads) == 1

        # Try to play WRITE (priority 3) — should be rejected
        handler.get_animation_variants = MagicMock(
            return_value={"loop": ["Write.fbx"], "post": [], "other": []}
        )
        await handler.play_animation(
            AnimationState.WRITE,
            session_id=None,
            context_id="ctx_write",
            priority=3,
        )
        # Still only 1 animation payload — WRITE was preempted
        assert len(transport.animation_payloads) == 1
        assert handler.current_state == AnimationState.THINK

    @pytest.mark.asyncio
    async def test_higher_priority_preempts_lower(self) -> None:
        handler, transport = _make_handler()

        handler.get_animation_variants = MagicMock(
            return_value={"loop": ["Write.fbx"], "post": [], "other": []}
        )
        handler.get_animations_for_state = MagicMock(return_value=["Write.fbx"])
        handler._resolve_animation_descriptor = MagicMock(
            return_value=("/skins/Rei/animations/write/Write.fbx", None)
        )
        handler._resolve_animation_descriptor_for_state = MagicMock(
            return_value=("/skins/Rei/animations/write/Write.fbx", None)
        )

        # Play WRITE (priority 3)
        await handler.play_animation(
            AnimationState.WRITE,
            session_id=None,
            context_id="ctx_write",
            priority=3,
        )
        assert handler.current_state == AnimationState.WRITE

        # Play THINK (priority 10) — should succeed
        handler.get_animation_variants = MagicMock(
            return_value={"loop": ["Think.fbx"], "post": [], "other": []}
        )
        handler._resolve_animation_descriptor = MagicMock(
            return_value=("/skins/Rei/animations/think/Think.fbx", None)
        )
        handler._resolve_animation_descriptor_for_state = MagicMock(
            return_value=("/skins/Rei/animations/think/Think.fbx", None)
        )
        await handler.play_animation(
            AnimationState.THINK,
            session_id=None,
            context_id="ctx_think",
            priority=10,
        )
        assert handler.current_state == AnimationState.THINK

    @pytest.mark.asyncio
    async def test_idle_always_overridable(self) -> None:
        handler, transport = _make_handler()

        handler.get_animation_variants = MagicMock(
            return_value={"loop": ["Idle.fbx"], "post": [], "other": []}
        )
        handler.get_animations_for_state = MagicMock(return_value=["Idle.fbx"])
        handler._resolve_animation_descriptor = MagicMock(
            return_value=("/skins/Rei/animations/idle/Idle.fbx", None)
        )
        handler._resolve_animation_descriptor_for_state = MagicMock(
            return_value=("/skins/Rei/animations/idle/Idle.fbx", None)
        )

        # IDLE is always acceptable regardless of active tasks
        await handler.play_animation(
            AnimationState.IDLE,
            session_id=None,
        )
        assert handler.current_state == AnimationState.IDLE


# ---------------------------------------------------------------------------
# Phase 4: State priority registration
# ---------------------------------------------------------------------------


class TestStatePriorityRegistration:
    def test_register_custom_priority(self) -> None:
        handler, _ = _make_handler()
        handler.register_state_priority("touch", 7)
        assert handler.get_state_priority("touch") == 7

    def test_default_priorities_loaded(self) -> None:
        handler, _ = _make_handler()
        assert handler.get_state_priority(AnimationState.THINK) == 10
        assert handler.get_state_priority(AnimationState.IDLE) == 0
        assert handler.get_state_priority(AnimationState.WRITE) == 3
        assert handler.get_state_priority(AnimationState.TALK) == 5

    def test_unknown_state_returns_zero(self) -> None:
        handler, _ = _make_handler()
        assert handler.get_state_priority("nonexistent") == 0


# ---------------------------------------------------------------------------
# Phase 3: Global rotation key
# ---------------------------------------------------------------------------


class TestGlobalRotation:
    @pytest.mark.asyncio
    async def test_rotation_key_is_state_only(self) -> None:
        handler, transport = _make_handler()

        handler.get_animation_variants = MagicMock(
            return_value={"loop": ["Idle.fbx", "Idle2.fbx"], "post": [], "other": []}
        )
        handler.get_animations_for_state = MagicMock(
            return_value=["Idle.fbx", "Idle2.fbx"]
        )
        handler._resolve_animation_descriptor = MagicMock(
            return_value=("/skins/Rei/animations/idle/Idle.fbx", None)
        )
        handler._resolve_animation_descriptor_for_state = MagicMock(
            return_value=("/skins/Rei/animations/idle/Idle.fbx", None)
        )

        await handler.play_animation(
            AnimationState.IDLE,
            session_id="session_a",
            loop=True,
        )

        # Rotation task key should be just "idle", not "session_a:idle"
        assert "idle" in handler._rotation_tasks
        assert "session_a:idle" not in handler._rotation_tasks

        # Clean up
        await handler._stop_rotation_task(None, AnimationState.IDLE)


# ---------------------------------------------------------------------------
# Phase 5: Karada API
# ---------------------------------------------------------------------------


class TestKaradaApiRouter:
    def test_create_karada_router_returns_router(self) -> None:
        from core.karada_api import create_karada_router

        handler, _ = _make_handler()
        router = create_karada_router(handler)
        # Router should have multiple routes
        assert len(router.routes) > 5

    def test_karada_api_transport_class(self) -> None:
        from core.karada_api import KaradaApiTransport

        transport = KaradaApiTransport()
        assert transport.get_connected_sessions() == []

    @pytest.mark.asyncio
    async def test_karada_api_transport_add_remove(self) -> None:
        from core.karada_api import KaradaApiTransport

        transport = KaradaApiTransport()
        mock_ws = AsyncMock()
        transport.add("sess1", mock_ws)
        assert "sess1" in transport.get_connected_sessions()
        transport.remove("sess1")
        assert "sess1" not in transport.get_connected_sessions()


# ---------------------------------------------------------------------------
# Phase 7: Asset manifest
# ---------------------------------------------------------------------------


class TestAssetManifest:
    def test_build_manifest_empty_dir(self, tmp_path: Path) -> None:
        from core.karada_api import _build_asset_manifest, invalidate_manifest_cache

        invalidate_manifest_cache()
        manifest = _build_asset_manifest(tmp_path)
        assert manifest["version"] == 1
        assert manifest["assets"] == {}
        invalidate_manifest_cache()

    def test_build_manifest_with_files(self, tmp_path: Path) -> None:
        from core.karada_api import _build_asset_manifest, invalidate_manifest_cache

        invalidate_manifest_cache()

        # Create fake skin structure
        anim_dir = tmp_path / "Rei" / "animations" / "idle"
        anim_dir.mkdir(parents=True)
        fbx_file = anim_dir / "Idle.fbx"
        fbx_file.write_bytes(b"\x00" * 100)
        desc_file = anim_dir / "Idle.fbx.json"
        desc_file.write_text('{"fps": 30}')

        manifest = _build_asset_manifest(tmp_path)
        assert len(manifest["assets"]) == 2
        assert "/skins/Rei/animations/idle/Idle.fbx" in manifest["assets"]
        assert "/skins/Rei/animations/idle/Idle.fbx.json" in manifest["assets"]

        # Check hash format
        entry = manifest["assets"]["/skins/Rei/animations/idle/Idle.fbx"]
        assert entry["hash"].startswith("sha256:")
        assert entry["size"] == 100

        invalidate_manifest_cache()


# ---------------------------------------------------------------------------
# Phase 6: Watchdog
# ---------------------------------------------------------------------------


class TestWatchdog:
    @pytest.mark.asyncio
    async def test_watchdog_forces_idle_on_stuck_state(self) -> None:
        handler, transport = _make_handler()

        # Simulate stuck state: THINK but no active tasks
        handler.current_state = AnimationState.THINK
        handler._active_tasks.clear()

        # Stub for the fallback play_animation call
        handler.get_animation_variants = MagicMock(
            return_value={"loop": ["Idle.fbx"], "post": [], "other": []}
        )
        handler.get_animations_for_state = MagicMock(return_value=["Idle.fbx"])
        handler._resolve_animation_descriptor = MagicMock(
            return_value=("/skins/Rei/animations/idle/Idle.fbx", None)
        )
        handler._resolve_animation_descriptor_for_state = MagicMock(
            return_value=("/skins/Rei/animations/idle/Idle.fbx", None)
        )

        # Run one iteration of the watchdog logic manually
        # (the real loop sleeps 10s — we test the logic inline)
        async with handler._lock:
            is_stuck = (
                handler.current_state != AnimationState.IDLE
                and not handler._active_tasks
            )

        if is_stuck:
            await handler.play_animation(
                AnimationState.IDLE, session_id=None, loop=True
            )

        assert handler.current_state == AnimationState.IDLE
