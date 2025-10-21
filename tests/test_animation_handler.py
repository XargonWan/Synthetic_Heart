"""Tests for the animation handler system.

This module tests the animation handler's ability to manage VRM animations
and coordinate with the WebUI.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from core.animation_handler import (
    AnimationHandler,
    AnimationState,
    get_animation_handler,
    set_animation_handler,
)


@pytest.fixture
def mock_webui():
    """Create a mock WebUI interface."""
    webui = MagicMock()
    webui.connections = {}
    return webui


@pytest.fixture
def animation_handler(mock_webui):
    """Create an animation handler with mock WebUI."""
    handler = AnimationHandler(mock_webui)
    return handler


@pytest.mark.asyncio
async def test_initialization():
    """Test animation handler initialization."""
    handler = AnimationHandler()
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
        context_id="test_context"
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
            AnimationState.IDLE,
            session_id=session_id,
            loop=True
        )
        animations_used.add(animation_handler.current_animation)
    
    # Should have used at least one of the idle animations
    expected_animations = {"Idle.fbx", "Idle2.fbx", "Happy Idle.fbx"}
    assert animations_used.issubset(expected_animations)


@pytest.mark.asyncio
async def test_stop_animation_single_context(animation_handler, mock_webui):
    """Test stopping animation returns to Idle when no contexts are active."""
    session_id = "test_session"
    mock_ws = AsyncMock()
    mock_webui.connections[session_id] = mock_ws
    
    context_id = "test_context"
    
    # Start animation with context
    await animation_handler.play_animation(
        AnimationState.THINK,
        session_id=session_id,
        context_id=context_id
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
    
    # Start two contexts
    await animation_handler.play_animation(
        AnimationState.THINK,
        session_id=session_id,
        context_id=context1
    )
    
    await animation_handler.play_animation(
        AnimationState.WRITE,
        session_id=session_id,
        context_id=context2
    )
    
    # Stop first context
    await animation_handler.stop_animation(context1, session_id)
    
    # Should NOT return to Idle because context2 is still active
    assert animation_handler._active_tasks[context2] is True
    
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
        AnimationState.WRITE,
        session_id=session_id,
        context_id="test"
    )
    
    assert animation_handler.current_state == AnimationState.WRITE
    assert mock_ws.send_json.called


@pytest.mark.asyncio
async def test_animation_without_webui():
    """Test animation handler without WebUI reference."""
    handler = AnimationHandler()
    
    # Should not raise exception, just log warning
    await handler.play_animation(
        AnimationState.THINK,
        session_id="test",
        loop=True
    )
    
    assert handler.current_state == AnimationState.THINK


@pytest.mark.asyncio
async def test_animation_without_websocket(animation_handler, mock_webui):
    """Test animation when WebSocket is not connected."""
    session_id = "nonexistent_session"
    
    # Should not raise exception, just log warning
    await animation_handler.play_animation(
        AnimationState.THINK,
        session_id=session_id,
        loop=True
    )
    
    assert animation_handler.current_state == AnimationState.THINK


@pytest.mark.asyncio
async def test_get_current_state(animation_handler, mock_webui):
    """Test getting current animation state."""
    assert animation_handler.get_current_state() == AnimationState.IDLE
    
    session_id = "test_session"
    mock_ws = AsyncMock()
    mock_webui.connections[session_id] = mock_ws
    
    await animation_handler.play_animation(
        AnimationState.THINK,
        session_id=session_id
    )
    
    assert animation_handler.get_current_state() == AnimationState.THINK


@pytest.mark.asyncio
async def test_get_current_animation(animation_handler, mock_webui):
    """Test getting current animation file."""
    assert animation_handler.get_current_animation() is None
    
    session_id = "test_session"
    mock_ws = AsyncMock()
    mock_webui.connections[session_id] = mock_ws
    
    await animation_handler.play_animation(
        AnimationState.THINK,
        session_id=session_id
    )
    
    assert animation_handler.get_current_animation() == "Thinking.fbx"


@pytest.mark.asyncio
async def test_global_handler():
    """Test global animation handler singleton."""
    handler1 = get_animation_handler()
    handler2 = get_animation_handler()
    
    assert handler1 is handler2
    
    # Test setting global handler
    new_handler = AnimationHandler()
    set_animation_handler(new_handler)
    
    handler3 = get_animation_handler()
    assert handler3 is new_handler


@pytest.mark.asyncio
async def test_idle_animation_rotation_task_created(animation_handler, mock_webui):
    """Test that a rotation task is created for idle animations with multiple files."""
    session_id = "test_session"
    mock_ws = AsyncMock()
    mock_webui.connections[session_id] = mock_ws
    
    # Play idle animation (has multiple files)
    await animation_handler.play_animation(
        AnimationState.IDLE,
        session_id=session_id,
        loop=True
    )
    
    # Check that a rotation task was created
    key = f"{session_id}:idle"
    assert key in animation_handler._rotation_tasks
    assert animation_handler._rotation_tasks[key] is not None
    
    # Clean up the task
    await animation_handler._stop_rotation_task(session_id, AnimationState.IDLE)


@pytest.mark.asyncio
async def test_websocket_message_format(animation_handler, mock_webui):
    """Test WebSocket message format."""
    session_id = "test_session"
    mock_ws = AsyncMock()
    mock_webui.connections[session_id] = mock_ws
    
    await animation_handler.play_animation(
        AnimationState.THINK,
        session_id=session_id,
        loop=True
    )
    
    # Check that send_json was called with correct format
    mock_ws.send_json.assert_called_once()
    call_args = mock_ws.send_json.call_args[0][0]
    
    assert call_args["type"] == "animation"
    assert call_args["animation"] == "animations/Thinking.fbx"
    assert call_args["loop"] is True
    assert call_args["state"] == "think"
