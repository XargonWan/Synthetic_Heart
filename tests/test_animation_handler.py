"""Tests for the animation handler system.

This module tests the animation handler's ability to manage VRM animations
and coordinate with the WebUI.
"""

import pytest
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock
from core.animation_handler import (
    KaradaStateServer,
    AnimationState,
    get_karada_state_server,
    set_karada_state_server,
)
from core.karada_ws_transport import WebSocketTransport


@pytest.fixture
def mock_webui():
    """Create a mock WebUI interface."""
    webui = MagicMock()
    webui.connections = {}
    return webui


@pytest.fixture
def animation_handler(mock_webui):
    """Create a KaradaStateServer instance with mock WebUI."""
    handler = KaradaStateServer(mock_webui)
    ws_transport = WebSocketTransport(mock_webui.connections)
    handler.add_transport(ws_transport)
    return handler


@pytest.mark.asyncio
async def test_initialization():
    """Test KaradaStateServer initialization."""
    handler = KaradaStateServer()
    assert handler.current_state == AnimationState.IDLE
    assert handler.current_animation is None
    assert len(handler._active_tasks) == 0


@pytest.mark.asyncio
async def test_set_webui(animation_handler, mock_webui):
    """Test setting WebUI reference."""
    new_webui = MagicMock()
    animation_handler.set_webui(new_webui)
    assert animation_handler.webui == new_webui


@pytest.mark.asyncio
async def test_play_animation(animation_handler, mock_webui):
    """Test playing an animation."""
    session_id = "test_session"
    mock_ws = AsyncMock()
    mock_webui.connections[session_id] = mock_ws

    await animation_handler.play_animation(
        AnimationState.THINK,
        session_id=session_id,
        loop=True,
        context_id="test_context",
    )

    assert animation_handler.current_state == AnimationState.THINK
    assert animation_handler.current_animation == "Thinking.fbx"
    assert mock_ws.send_json.called


@pytest.mark.asyncio
async def test_animation_with_multiple_files(animation_handler, mock_webui):
    """Test animation state with multiple file options (random selection)."""
    session_id = "test_session"
    mock_ws = AsyncMock()
    mock_webui.connections[session_id] = mock_ws

    # Play idle animation multiple times to test random selection
    animations_used = set()
    for _ in range(10):
        await animation_handler.play_animation(
            AnimationState.IDLE, session_id=session_id, loop=True
        )
        animations_used.add(animation_handler.current_animation)

    # Should have used at least one of the idle animations
    # (actual animations may vary based on skins available)
    expected_animations = {"Idle.fbx", "Idle2.fbx"}
    assert animations_used.issubset(expected_animations)


@pytest.mark.asyncio
async def test_play_animation_fallback_to_idle_reports_actual_state(
    animation_handler, mock_webui, monkeypatch
):
    """If a requested state has no variants, the broadcasted state must match the
    actual fallback animation folder rather than the original requested state."""
    session_id = "test_session"
    mock_ws = AsyncMock()
    mock_webui.connections[session_id] = mock_ws

    original_get_variants = animation_handler.get_animation_variants

    def fake_get_variants(state_name: str):
        if state_name == AnimationState.SKIN_CHANGE.value:
            return {"loop": [], "post": [], "other": []}
        return original_get_variants(state_name)

    monkeypatch.setattr(animation_handler, "get_animation_variants", fake_get_variants)

    await animation_handler.play_animation(
        AnimationState.SKIN_CHANGE,
        session_id=session_id,
        loop=False,
    )

    assert animation_handler.current_state == AnimationState.IDLE
    assert animation_handler.current_animation in {"Idle.fbx", "Idle2.fbx"}

    sent = [c[0][0] for c in mock_ws.send_json.call_args_list]
    anim_msgs = [m for m in sent if m.get("type") == "vrm_animation"]
    assert anim_msgs
    msg = anim_msgs[-1]
    assert msg["state"] == "idle"
    assert "/animations/idle/" in msg["file"]


@pytest.mark.asyncio
async def test_stop_animation_single_context(animation_handler, mock_webui):
    """Test stopping animation returns to Idle when no contexts are active."""
    session_id = "test_session"
    mock_ws = AsyncMock()
    mock_webui.connections[session_id] = mock_ws

    context_id = "test_context"

    # Start animation with context
    await animation_handler.play_animation(
        AnimationState.THINK, session_id=session_id, context_id=context_id
    )

    assert animation_handler.current_state == AnimationState.THINK

    # Stop context
    await animation_handler.stop_animation(context_id, session_id)

    # Should return to Idle
    assert animation_handler.current_state == AnimationState.IDLE


@pytest.mark.asyncio
async def test_stop_animation_multiple_contexts(animation_handler, mock_webui):
    """Test that stopping one context doesn't affect others."""
    session_id = "test_session"
    mock_ws = AsyncMock()
    mock_webui.connections[session_id] = mock_ws

    context1 = "context1"
    context2 = "context2"

    # Start two contexts — WRITE first (priority 3), then THINK (priority 10).
    # THINK has higher priority so it will not be preempted by WRITE.
    await animation_handler.play_animation(
        AnimationState.WRITE, session_id=session_id, context_id=context1
    )

    await animation_handler.play_animation(
        AnimationState.THINK, session_id=session_id, context_id=context2
    )

    # Stop first context
    await animation_handler.stop_animation(context1, session_id)

    # Should NOT return to Idle because context2 is still active
    assert context2 in animation_handler._active_tasks

    # Stop second context
    await animation_handler.stop_animation(context2, session_id)

    # Now should return to Idle
    assert animation_handler.current_state == AnimationState.IDLE


@pytest.mark.asyncio
async def test_transition_to(animation_handler, mock_webui):
    """Test transition_to convenience method."""
    session_id = "test_session"
    mock_ws = AsyncMock()
    mock_webui.connections[session_id] = mock_ws

    await animation_handler.transition_to(
        AnimationState.WRITE, session_id=session_id, context_id="test"
    )

    assert animation_handler.current_state == AnimationState.WRITE
    assert mock_ws.send_json.called


@pytest.mark.asyncio
async def test_animation_without_webui():
    """Test KaradaStateServer without WebUI reference."""
    handler = KaradaStateServer()

    # Should not raise exception, just log warning
    await handler.play_animation(AnimationState.THINK, session_id="test", loop=True)

    assert handler.current_state == AnimationState.THINK


@pytest.mark.asyncio
async def test_animation_without_websocket(animation_handler, mock_webui):
    """Test animation when WebSocket is not connected."""
    session_id = "nonexistent_session"

    # Should not raise exception, just log warning
    await animation_handler.play_animation(
        AnimationState.THINK, session_id=session_id, loop=True
    )

    assert animation_handler.current_state == AnimationState.THINK


@pytest.mark.asyncio
async def test_get_current_state(animation_handler, mock_webui):
    """Test getting current animation state."""
    assert animation_handler.get_current_state() == AnimationState.IDLE

    session_id = "test_session"
    mock_ws = AsyncMock()
    mock_webui.connections[session_id] = mock_ws

    await animation_handler.play_animation(AnimationState.THINK, session_id=session_id)

    assert animation_handler.get_current_state() == AnimationState.THINK


@pytest.mark.asyncio
async def test_get_current_animation(animation_handler, mock_webui):
    """Test getting current animation file."""
    assert animation_handler.get_current_animation() is None

    def test_incomplete_intro_outro_warns():
        ah = KaradaStateServer()
        # intro without start_frame, outro without end_frame
        desc = {
            "intro": {"end_frame": 10},
            "loop": {"start_frame": 11, "end_frame": 20},
            "outro": {"start_frame": 21},
        }
        # Should not raise, but return has_intro=False, has_outro=False
        res = ah._analyze_animation_structure(desc, "incomplete.fbx")
        assert res["has_intro"] is False
        assert res["has_outro"] is False

    def test_thinking_descriptor_classified_as_loop():
        from core.animation_handler import KaradaStateServer

        ah = KaradaStateServer()
        # Ensure search paths include the skins/Rei animations directory
        ah.set_animation_search_paths([ah.SKIN_DEFAULT_ANIMATIONS_DIR])
        variants = ah.get_animation_variants("think")
        # Our Thinking.fbx should be discovered and classified as loop variant
        found = any("Thinking.fbx" == a for a in variants.get("loop", []))
        assert found, f"Thinking.fbx not found in loop variants: {variants}"


@pytest.mark.asyncio
async def test_vrm_animation_broadcast_on_play():
    """Ensure that play_animation broadcasts a vrm_animation message to all
    connected WebSocket clients (KaradaStateServer architecture)."""
    handler = KaradaStateServer()

    sent_messages: list[dict[str, Any]] = []

    class FakeWs:
        async def send_json(self, data: dict[str, Any]) -> None:
            sent_messages.append(data)

    class FakeWebUI:
        def __init__(self) -> None:
            self.connections = {"sess-1": FakeWs()}

    fake = FakeWebUI()
    handler.set_webui(cast(Any, fake))
    ws_transport = WebSocketTransport(cast(Any, fake.connections))
    handler.add_transport(ws_transport)

    # Trigger an animation change
    await handler.play_animation(
        AnimationState.THINK, session_id=None, loop=True, context_id="ctx"
    )

    # At least one vrm_animation message must have been sent
    anim_msgs = [m for m in sent_messages if m.get("type") == "vrm_animation"]
    assert anim_msgs, f"Expected vrm_animation message, got: {sent_messages}"


@pytest.mark.asyncio
async def test_global_handler():
    """Test global KaradaStateServer singleton."""
    handler1 = get_karada_state_server()
    handler2 = get_karada_state_server()

    assert handler1 is handler2

    # Test setting global handler
    new_handler = KaradaStateServer()
    set_karada_state_server(new_handler)

    handler3 = get_karada_state_server()
    assert handler3 is new_handler


@pytest.mark.asyncio
async def test_idle_animation_rotation_task_created(animation_handler, mock_webui):
    """Test that a rotation task is created for idle animations with multiple files."""
    session_id = "test_session"
    mock_ws = AsyncMock()
    mock_webui.connections[session_id] = mock_ws

    # Play idle animation (has multiple files)
    await animation_handler.play_animation(
        AnimationState.IDLE, session_id=session_id, loop=True
    )

    # Check that a rotation task was created (global key, not per-session)
    key = "idle"
    assert key in animation_handler._rotation_tasks
    assert animation_handler._rotation_tasks[key] is not None

    # Clean up the task
    await animation_handler._stop_rotation_task(session_id, AnimationState.IDLE)


@pytest.mark.asyncio
async def test_websocket_message_format(animation_handler, mock_webui):
    """Test WebSocket message format for vrm_animation broadcast."""
    session_id = "test_session"
    mock_ws = AsyncMock()
    mock_webui.connections[session_id] = mock_ws

    await animation_handler.play_animation(
        AnimationState.THINK, session_id=session_id, loop=True
    )

    # send_json may be called multiple times (preloads + animation command)
    assert mock_ws.send_json.called, "Expected send_json to be called at least once"

    # Find the vrm_animation command among all calls
    sent = [c[0][0] for c in mock_ws.send_json.call_args_list]
    anim_msgs = [m for m in sent if m.get("type") == "vrm_animation"]
    assert anim_msgs, (
        f"Expected a vrm_animation message, got types: {[m.get('type') for m in sent]}"
    )

    msg = anim_msgs[-1]  # Last vrm_animation is the final play command
    assert isinstance(msg["file"], str)
    assert msg["file"].endswith("Thinking.fbx")
    assert "animations/" in msg["file"]
    assert msg["loop"] is True
    assert msg["state"] == "think"
    assert isinstance(msg.get("descriptor"), dict)
    assert "intro" in msg["descriptor"]
    assert "loop" in msg["descriptor"]
    assert "outro" in msg["descriptor"]
