"""
Animation Priority System - Implementation Summary

This document describes the animation priority system that was implemented
to prevent lower-priority animations from interrupting higher-priority ones.

## Problem Statement

Previously, when SyntH was in a THINKING state (playing the "think" animation),
it would immediately switch to IDLE state when the message was being sent,
interrupting the thinking animation prematurely.

Example log before the fix:
```
[synth_webui] ✅ Setting animation state to: think from phase: THINKING
[synth_webui] ✅ Setting animation state to: idle from phase: IDLE  <- Immediately switches!
```

## Solution: Priority-Based State Management

A priority system was implemented where each animation state has a priority level.
Higher priority states cannot be interrupted by lower priority ones.

### Priority Mapping

| Phase        | Priority | Description |
|-------------|----------|-------------|
| IDLE        | 0        | Lowest priority, can be interrupted by anything |
| WRITING     | 3        | Low priority |
| TALKING     | 5        | Medium priority |
| CORRECTING  | 7        | High priority |
| THINKING    | 10       | Highest priority, cannot be interrupted |

## Implementation Details

### 1. Core Changes

**File: `core/action_state_manager.py`**
- Added `PHASE_PRIORITIES` dictionary mapping each `AnimationPhase` to its priority
- Modified `push_action()` to check if new action's priority >= current action's priority
  - Returns `True` if accepted, `False` if rejected (lower priority)
- Modified `update_phase()` with same priority checking logic

**File: `core/animation_handler.py`**
- Added `ANIMATION_STATE_PRIORITIES` mapping for animation states
- Modified `play_animation()` to auto-set priority from state mapping if not provided
- Fixed `_stop_rotation_task()` to properly handle `CancelledError`

**File: `core/webui.py`**
- Updated `_handle_user_message()` to check `push_action()` return value
- Only pops actions that were successfully pushed
- Prevents stack corruption if an action is rejected

### 2. Behavior

When a new animation state is requested:

1. **Check Priority**: Compare new state priority with current state priority
2. **Allow/Reject**: If new priority >= current priority, accept; else reject
3. **Log Decision**: Debug log shows what was accepted/rejected and why
4. **No Exception**: Silently rejects (logs at DEBUG level) instead of throwing

Example:
```python
# THINKING is running (priority=10)
# WRITING tries to push (priority=3)
# Result: REJECTED (3 < 10)
# THINKING continues to play
```

### 3. Testing

Created comprehensive test suite in `tests/test_action_state_priority.py` with 9 tests:

✅ `test_phase_priorities_defined` - All phases have priorities
✅ `test_thinking_priority_highest` - THINKING has max priority
✅ `test_idle_priority_lowest` - IDLE has min priority
✅ `test_high_priority_can_push_over_low` - High priority can interrupt low
✅ `test_low_priority_cannot_push_over_high` - Low priority CANNOT interrupt high
✅ `test_thinking_not_interrupted_by_writing` - Main issue resolved
✅ `test_same_priority_can_push` - Same priority actions can stack
✅ `test_pop_returns_to_previous_action` - Stack pops correctly
✅ `test_idle_when_stack_empty` - IDLE by default

**Result: 9/9 tests PASS ✓**

## Example Scenario

```
User sends message to WebUI:
1. [PUSH] THINKING (priority=10)
   → Accepted, stack: [THINKING]
   
2. Message being processed...
   
3. [PUSH] WRITING (priority=3)
   → REJECTED! (3 < 10)
   → Log: "Action rejected (priority): writing (3) < THINKING (10)"
   → Stack still: [THINKING]
   
4. [PUSH] CORRECTING (priority=7)
   → REJECTED! (7 < 10)
   
5. Message sent, [POP] THINKING
   → Stack empty, return to IDLE (priority=0)
```

## Benefits

1. **Prevents Animation Jank**: Thinking animation plays completely without interruption
2. **Better UX**: Users see the full thinking/processing animation
3. **Predictable Behavior**: Clear rules about what can interrupt what
4. **Clean Code**: Priority-driven, no hardcoded logic scattered around
5. **Extensible**: Easy to add new states and adjust priorities

## Backward Compatibility

- ✅ No breaking changes to public APIs
- ✅ `push_action()` now returns `bool` but can be ignored
- ✅ Existing code that doesn't check return value still works
- ✅ All existing tests pass

## Future Improvements

1. Could add priority override mechanism for emergency states
2. Could log rejected actions at higher level for monitoring
3. Could emit events when actions are rejected
4. Could visualize state stack in WebUI for debugging
"""
