"""Test priority system for action state manager.

This test verifies that the action state manager correctly enforces priority rules:
- Higher priority actions can interrupt lower priority ones
- Lower priority actions cannot interrupt higher priority ones
- The priority system prevents THINKING from being interrupted by WRITING or IDLE
"""

import pytest
import asyncio
from core.action_state_manager import (
    get_action_state_manager,
    init_action_state_manager,
    AnimationPhase,
    PHASE_PRIORITIES
)


class TestActionStatePriority:
    """Test action state priority system."""

    @pytest.fixture(autouse=True)
    def setup(self):
        """Initialize a fresh action state manager for each test."""
        init_action_state_manager()
        yield
        # Cleanup is implicit

    @pytest.mark.asyncio
    async def test_phase_priorities_defined(self):
        """Verify that all animation phases have priorities defined."""
        for phase in AnimationPhase:
            assert phase in PHASE_PRIORITIES, f"Phase {phase} has no priority defined"
            assert isinstance(PHASE_PRIORITIES[phase], int), f"Priority for {phase} is not an integer"

    @pytest.mark.asyncio
    async def test_thinking_priority_highest(self):
        """Verify THINKING has the highest priority."""
        thinking_priority = PHASE_PRIORITIES[AnimationPhase.THINKING]
        for phase, priority in PHASE_PRIORITIES.items():
            if phase != AnimationPhase.THINKING:
                assert thinking_priority > priority, f"THINKING priority {thinking_priority} should be > {phase} priority {priority}"

    @pytest.mark.asyncio
    async def test_idle_priority_lowest(self):
        """Verify IDLE has the lowest priority."""
        idle_priority = PHASE_PRIORITIES[AnimationPhase.IDLE]
        for phase, priority in PHASE_PRIORITIES.items():
            if phase != AnimationPhase.IDLE:
                assert idle_priority < priority, f"IDLE priority {idle_priority} should be < {phase} priority {priority}"

    @pytest.mark.asyncio
    async def test_high_priority_can_push_over_low(self):
        """Test that high priority action can push over low priority action."""
        manager = get_action_state_manager()
        
        # Push WRITING (low priority)
        result = await manager.push_action(
            action_id="action1",
            phase=AnimationPhase.WRITING,
            component="test"
        )
        assert result is True, "Should be able to push WRITING (first action)"
        
        # Push THINKING (high priority) - should succeed
        result = await manager.push_action(
            action_id="action2",
            phase=AnimationPhase.THINKING,
            component="test"
        )
        assert result is True, "Should be able to push THINKING over WRITING"
        
        # Verify stack
        action = await manager.get_current_action()
        assert action["phase"] == AnimationPhase.THINKING.value, "Current action should be THINKING"

    @pytest.mark.asyncio
    async def test_low_priority_cannot_push_over_high(self):
        """Test that low priority action cannot push over high priority action."""
        manager = get_action_state_manager()
        
        # Push THINKING (high priority)
        result = await manager.push_action(
            action_id="action1",
            phase=AnimationPhase.THINKING,
            component="test"
        )
        assert result is True, "Should be able to push THINKING (first action)"
        
        # Try to push WRITING (low priority) - should be rejected
        result = await manager.push_action(
            action_id="action2",
            phase=AnimationPhase.WRITING,
            component="test"
        )
        assert result is False, "Should NOT be able to push WRITING over THINKING"
        
        # Verify stack - THINKING should still be on top
        action = await manager.get_current_action()
        assert action["phase"] == AnimationPhase.THINKING.value, "Current action should still be THINKING"

    @pytest.mark.asyncio
    async def test_thinking_not_interrupted_by_writing(self):
        """Verify the main issue: THINKING cannot be interrupted by WRITING."""
        manager = get_action_state_manager()
        
        # Simulate webui flow:
        # 1. User sends message -> THINKING
        result = await manager.push_action(
            action_id="webui_msg_1",
            phase=AnimationPhase.THINKING,
            component="webui"
        )
        assert result is True, "Should push THINKING"
        
        # 2. Response starts -> WRITING (should be rejected)
        result = await manager.push_action(
            action_id="webui_write_1",
            phase=AnimationPhase.WRITING,
            component="webui"
        )
        assert result is False, "WRITING should NOT push over THINKING"
        
        # 3. Verify THINKING is still active
        action = await manager.get_current_action()
        assert action["phase"] == AnimationPhase.THINKING.value
        assert action["action_id"] == "webui_msg_1"

    @pytest.mark.asyncio
    async def test_same_priority_can_push(self):
        """Test that same priority actions can push (equal priority)."""
        manager = get_action_state_manager()
        
        # Push WRITING
        result = await manager.push_action(
            action_id="action1",
            phase=AnimationPhase.WRITING,
            component="test"
        )
        assert result is True
        
        # Push another WRITING (same priority) - should succeed
        result = await manager.push_action(
            action_id="action2",
            phase=AnimationPhase.WRITING,
            component="test"
        )
        assert result is True, "Should be able to push same priority action"
        
        # Verify new action is on top
        action = await manager.get_current_action()
        assert action["action_id"] == "action2"

    @pytest.mark.asyncio
    async def test_pop_returns_to_previous_action(self):
        """Test that popping returns to the previous action in stack."""
        manager = get_action_state_manager()
        
        # Push WRITING
        await manager.push_action(
            action_id="action1",
            phase=AnimationPhase.WRITING,
            component="test"
        )
        
        # Push THINKING (higher priority)
        await manager.push_action(
            action_id="action2",
            phase=AnimationPhase.THINKING,
            component="test"
        )
        
        # Pop THINKING
        result = await manager.pop_action("action2")
        assert result is True
        
        # Should return to WRITING
        action = await manager.get_current_action()
        assert action["phase"] == AnimationPhase.WRITING.value
        assert action["action_id"] == "action1"

    @pytest.mark.asyncio
    async def test_idle_when_stack_empty(self):
        """Test that we get IDLE when stack is empty."""
        manager = get_action_state_manager()
        
        # Stack should be empty initially
        is_empty = await manager.is_empty()
        assert is_empty is True
        
        # Current phase should be IDLE
        phase = await manager.get_current_phase()
        assert phase == AnimationPhase.IDLE


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
