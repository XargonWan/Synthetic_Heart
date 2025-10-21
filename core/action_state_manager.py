"""
Global action state manager for SyntH.

Tracks the current animation state (THINKING, WRITING, IDLE, etc.) globally
across all interfaces (WebUI, Telegram, Discord, etc.).

This is a simple in-memory state store - only holds the animation state name,
not the complete context. The client (frontend) decides which animation to play
based on the state rules we've established.
"""

from typing import Optional, List, Dict, Any
from datetime import datetime
from enum import Enum
import asyncio

from core.logging_utils import log_info, log_debug, log_warning

LOG_PREFIX = "ACTION_STATE"


class AnimationPhase(str, Enum):
    """Animation phases that SyntH can be in."""
    IDLE = "IDLE"
    THINKING = "THINKING"
    WRITING = "WRITING"
    CORRECTING = "CORRECTING"
    TALKING = "TALKING"


class ActionStackEntry:
    """Represents one action in the global stack."""
    
    def __init__(
        self,
        action_id: str,
        phase: AnimationPhase,
        component: str,
        parent_id: Optional[str] = None
    ):
        self.action_id = action_id
        self.phase = phase
        self.component = component
        self.parent_id = parent_id
        self.started_at = datetime.utcnow()
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "action_id": self.action_id,
            "phase": self.phase.value,
            "component": self.component,
            "parent_id": self.parent_id,
            "started_at": self.started_at.isoformat()
        }


class ActionStateManager:
    """
    Manages the global action state for the SyntH instance.
    
    This state is shared across ALL interfaces (WebUI, Telegram, Discord, etc).
    It's like SyntH being in a real room - everyone sees the same state.
    
    The stack allows for nested actions (e.g., corrector entering while original
    message is still being processed).
    """
    
    def __init__(self):
        self._action_stack: List[ActionStackEntry] = []
        self._lock = asyncio.Lock()
        self._state_changed_callbacks: List[callable] = []
        log_info(f"{LOG_PREFIX} ActionStateManager initialized")
    
    # ------------------------------------------------------------------
    # Stack management
    # ------------------------------------------------------------------
    async def push_action(
        self,
        action_id: str,
        phase: AnimationPhase,
        component: str,
        parent_id: Optional[str] = None
    ) -> None:
        """
        Add a new action to the stack.
        
        Args:
            action_id: Unique identifier for this action
            phase: Initial animation phase (THINKING, WRITING, etc)
            component: Component triggering this action (webui, telegram, corrector, etc)
            parent_id: If this is a nested action (e.g., corrector), reference parent
        """
        async with self._lock:
            entry = ActionStackEntry(action_id, phase, component, parent_id)
            self._action_stack.append(entry)
            log_info(
                f"{LOG_PREFIX} Action pushed: {action_id} phase={phase.value} "
                f"component={component} stack_depth={len(self._action_stack)}"
            )
            log_info(f"{LOG_PREFIX} About to notify state changed with {len(self._state_changed_callbacks)} callbacks")
            await self._notify_state_changed()
            log_info(f"{LOG_PREFIX} Notified state changed")
    
    async def update_phase(self, action_id: str, new_phase: AnimationPhase) -> bool:
        """
        Update the phase of an existing action.
        
        Args:
            action_id: ID of action to update
            new_phase: New phase to set
            
        Returns:
            True if updated, False if action not found
        """
        async with self._lock:
            for entry in self._action_stack:
                if entry.action_id == action_id:
                    old_phase = entry.phase
                    entry.phase = new_phase
                    log_info(
                        f"{LOG_PREFIX} Action phase updated: {action_id} "
                        f"{old_phase.value} -> {new_phase.value}"
                    )
                    await self._notify_state_changed()
                    return True
            
            log_warning(f"{LOG_PREFIX} Action not found for update: {action_id}")
            return False
    
    async def pop_action(self, action_id: str) -> bool:
        """
        Remove an action from the stack (when it completes).
        
        Args:
            action_id: ID of action to remove
            
        Returns:
            True if removed, False if not found
        """
        async with self._lock:
            for i, entry in enumerate(self._action_stack):
                if entry.action_id == action_id:
                    self._action_stack.pop(i)
                    log_info(
                        f"{LOG_PREFIX} Action popped: {action_id} "
                        f"stack_depth={len(self._action_stack)}"
                    )
                    await self._notify_state_changed()
                    return True
            
            log_warning(f"{LOG_PREFIX} Action not found for pop: {action_id}")
            return False
    
    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------
    async def get_current_action(self) -> Optional[Dict[str, Any]]:
        """
        Get the current (top of stack) action state.
        
        Returns:
            Dict with action_id, phase, component, or None if stack is empty
        """
        async with self._lock:
            if not self._action_stack:
                return None
            
            top = self._action_stack[-1]
            return top.to_dict()
    
    async def get_current_phase(self) -> AnimationPhase:
        """
        Get only the animation phase of the current action.
        
        Returns:
            AnimationPhase (THINKING, WRITING, IDLE, etc)
        """
        action = await self.get_current_action()
        if action:
            return AnimationPhase(action["phase"])
        return AnimationPhase.IDLE
    
    async def get_stack(self) -> List[Dict[str, Any]]:
        """
        Get the entire stack (for debugging/monitoring).
        
        Returns:
            List of action dictionaries from bottom to top
        """
        async with self._lock:
            return [entry.to_dict() for entry in self._action_stack]
    
    async def is_empty(self) -> bool:
        """Check if stack is empty."""
        async with self._lock:
            return len(self._action_stack) == 0
    
    # ------------------------------------------------------------------
    # State change notifications
    # ------------------------------------------------------------------
    def register_state_changed_callback(self, callback: callable) -> None:
        """
        Register a callback to be called when state changes.
        
        Callback signature: async callback(state: Dict[str, Any])
        """
        self._state_changed_callbacks.append(callback)
    
    async def _notify_state_changed(self) -> None:
        """
        Notify all listeners that state has changed.
        
        NOTE: This is called WHILE holding the lock, so we access _action_stack directly
        without acquiring the lock again (to avoid deadlock).
        """
        # Get current state WITHOUT acquiring lock (we already hold it)
        if not self._action_stack:
            current = None
        else:
            top = self._action_stack[-1]
            current = top.to_dict()
        
        log_info(f"{LOG_PREFIX} State changed - notifying {len(self._state_changed_callbacks)} callbacks with phase: {current.get('phase') if current else 'IDLE'}")
        
        for callback in self._state_changed_callbacks:
            try:
                if asyncio.iscoroutinefunction(callback):
                    log_debug(f"{LOG_PREFIX} Calling async callback")
                    await callback(current)
                else:
                    log_debug(f"{LOG_PREFIX} Calling sync callback")
                    callback(current)
            except Exception as exc:
                log_warning(f"{LOG_PREFIX} Error in state changed callback: {exc}")


# Singleton instance
_action_state_manager: Optional[ActionStateManager] = None


def get_action_state_manager() -> ActionStateManager:
    """Get or create the global ActionStateManager instance."""
    global _action_state_manager
    if _action_state_manager is None:
        _action_state_manager = ActionStateManager()
    return _action_state_manager


def init_action_state_manager() -> ActionStateManager:
    """Initialize the global ActionStateManager."""
    global _action_state_manager
    _action_state_manager = ActionStateManager()
    return _action_state_manager
