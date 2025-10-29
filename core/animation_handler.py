"""Animation handler for VRM avatar in the SyntH Web UI.

This module provides a centralized system for managing VRM avatar animations.
Components can trigger logical animation states (Think, Write, Talk, Idle) which
are mapped to actual FBX animation files. The handler ensures smooth transitions
and automatic fallback to Idle when no animations are active.

The animation system integrates with the WebUI to send animation commands via WebSocket.
"""

from __future__ import annotations

import asyncio
import random
from enum import Enum
from typing import Dict, List, Optional, TYPE_CHECKING
from pathlib import Path
import json

from core.logging_utils import log_debug, log_info, log_warning

if TYPE_CHECKING:
    from core.webui import SynthWebUIInterface


class AnimationState(Enum):
    """Logical animation states that components can trigger."""
    IDLE = "idle"
    THINK = "think"
    WRITE = "write"
    TALK = "talk"


class AnimationHandler:
    """Manages VRM avatar animations and their lifecycle.
    
    This handler:
    - Maps logical animation states to FBX files
    - Tracks the current animation state
    - Sends animation commands to the WebUI via WebSocket
    - Handles automatic fallback to Idle state
    - Supports random selection or sequential rotation from multiple animation files
    """

    # Animation mappings: logical state -> list of FBX files
    # ANIMATION_MAP: Dict[AnimationState, List[str]] = {
    #     AnimationState.THINK: ["Thinking.fbx"],
    #     AnimationState.WRITE: ["Texting While Standing.fbx", "Texting.fbx"],
    #     AnimationState.TALK: ["talking.fbx"],
    #     AnimationState.IDLE: ["Idle.fbx", "Idle2.fbx", "Happy Idle.fbx"],
    # }

    # Base path segment used when building URLs to per-skin animations
    ANIMATIONS_BASE_PATH = "animations"
    # Skins directory (contains skins like Rei)
    SKINS_DIR = Path(__file__).resolve().parent.parent / "skins"

    def __init__(self, webui: Optional[SynthWebUIInterface] = None):
        """Initialize the animation handler.
        
        Args:
            webui: Reference to the SynthWebUIInterface for sending animation commands
        """
        self.webui = webui
        self.current_state: AnimationState = AnimationState.IDLE
        self.current_animation: Optional[str] = None
        self._lock = asyncio.Lock()
        # Track active animation contexts -> map context_id to priority (int)
        # If a context_id maps to None, treat as priority 0
        self._active_tasks: Dict[str, Optional[int]] = {}
        # Rotation tasks per session+state key -> asyncio.Task
        self._rotation_tasks: Dict[str, asyncio.Task] = {}
        # Sequential animation indices per state -> map state.value to current index
        self._sequence_indices: Dict[str, int] = {}
        # States that use sequential rotation instead of random
        self._sequential_states = {AnimationState.IDLE.value}
        
    def set_webui(self, webui: SynthWebUIInterface) -> None:
        """Set or update the WebUI reference.
        
        Args:
            webui: The SynthWebUIInterface instance
        """
        self.webui = webui
        log_debug("[AnimationHandler] WebUI reference set")

    def get_animations_for_state(self, state: AnimationState) -> List[str]:
        """Get list of animation files for a given state by scanning skin folders.
        
        Scans subfolders in skins/<skin>/animations/<state.value>/ for .fbx files.
        Falls back to Rei skin if active persona has no animations.
        
        Args:
            state: The animation state
            
        Returns:
            List of animation filenames (without paths)
        """
        animations = []
        
        # Get active persona folder (similar to _send_animation_command logic)
        try:
            from core.persona_manager import get_persona_manager
            persona_manager = get_persona_manager()
            active_persona_folder = None
            if persona_manager and hasattr(persona_manager, '_current_persona') and persona_manager._current_persona:
                active_persona_folder = getattr(persona_manager._current_persona, 'id', None) or getattr(persona_manager._current_persona, 'name', None)
        except Exception:
            active_persona_folder = None
        
        # Candidate skin folders to check
        candidates = []
        if active_persona_folder:
            candidates.append(active_persona_folder)
        candidates.append('Rei')  # Fallback to Rei
        
        # Scan each candidate skin
        for skin_name in candidates:
            skin_anim_dir = self.SKINS_DIR / skin_name / 'animations' / state.value
            if skin_anim_dir.exists() and skin_anim_dir.is_dir():
                try:
                    for fbx_file in skin_anim_dir.glob('*.fbx'):
                        animations.append(fbx_file.name)
                except Exception as exc:
                    log_warning(f"[AnimationHandler] Error scanning animations in {skin_anim_dir}: {exc}")
        
        # Remove duplicates while preserving order
        seen = set()
        unique_animations = []
        for anim in animations:
            if anim not in seen:
                seen.add(anim)
                unique_animations.append(anim)
        
        return unique_animations

    async def play_animation(
        self,
        state: AnimationState,
        session_id: Optional[str],
        loop: bool = True,
        context_id: Optional[str] = None,
        priority: Optional[int] = None,
    ) -> None:
        """Play an animation for a specific state.
        
        Args:
            state: The animation state to play
            session_id: The WebUI session ID to send the animation to
            loop: Whether the animation should loop
            context_id: Optional identifier for this animation context (for tracking)
        """
        async with self._lock:
            # If we have a context_id, mark it as active with optional priority
            if context_id:
                self._active_tasks[context_id] = int(priority) if priority is not None else 0
            
            # Select animation file
            animations = self.get_animations_for_state(state)
            if not animations:
                # Fallback to idle if no animations found for this state
                animations = self.get_animations_for_state(AnimationState.IDLE)
            if not animations:
                log_warning(f"[AnimationHandler] No animations found for state {state.value}, skipping")
                return
            
            # Select animation based on rotation mode
            if state.value in self._sequential_states and len(animations) > 1:
                # Sequential mode: use current index or start from 0
                current_index = self._sequence_indices.get(state.value, -1)
                next_index = (current_index + 1) % len(animations)
                selected_animation = animations[next_index]
                self._sequence_indices[state.value] = next_index
            else:
                # Random mode or single animation
                selected_animation = random.choice(animations)
            
            # Update internal state
            self.current_state = state
            self.current_animation = selected_animation
            
            log_debug(
                f"[AnimationHandler] Playing {state.value} animation: {selected_animation} "
                f"(loop={loop}, session={session_id}, context={context_id}, priority={priority})"
            )
            
            # Send animation command to WebUI
            if self.webui:
                await self._send_animation_command(
                    session_id=session_id,
                    animation_file=selected_animation,
                    loop=loop,
                    state=state.value
                )
            else:
                log_warning("[AnimationHandler] WebUI not set, cannot send animation command")

            # If there are multiple animations for this state, start a background
            # rotation task that will randomly switch between them every 30-60s
            key = f"{session_id}:{state.value}"
            if len(animations) > 1:
                await self._start_rotation_task(session_id, state, context_id)
            else:
                # Ensure no leftover rotation task is running for this state
                await self._stop_rotation_task(session_id, state)

    async def stop_animation(self, context_id: str, session_id: str) -> None:
        """Stop an animation context and return to Idle if no other contexts are active.
        
        Args:
            context_id: The context identifier to stop
            session_id: The WebUI session ID
        """
        async with self._lock:
            # Remove the context from active tasks
            if context_id in self._active_tasks:
                self._active_tasks.pop(context_id, None)

            # Determine highest remaining priority among active contexts
            remaining_priorities = [p for p in self._active_tasks.values() if p is not None]
            highest = max(remaining_priorities) if remaining_priorities else 0

            # Define Idle priority as 0; only return to Idle when no active context has priority > 0
            if highest <= 0:
                # Return to Idle
                log_debug(f"[AnimationHandler] No high-priority contexts, returning to Idle (session={session_id})")
                await self.play_animation(
                    AnimationState.IDLE,
                    session_id=session_id,
                    loop=True,
                    context_id=None
                )
                # When returning to Idle, make sure other rotation tasks for the
                # previous contexts are cleaned up
                # (stop any rotation tasks for non-idle states tied to this session)
                for anim_state in [AnimationState.THINK, AnimationState.WRITE, AnimationState.TALK]:
                    await self._stop_rotation_task(session_id, anim_state)
            else:
                log_debug(f"[AnimationHandler] Context {context_id} stopped but other contexts still active")

    async def transition_to(
        self,
        state: AnimationState,
        session_id: str,
        context_id: Optional[str] = None
    ) -> None:
        """Transition to a new animation state.
        
        This is a convenience method that plays the animation with looping enabled.
        
        Args:
            state: The animation state to transition to
            session_id: The WebUI session ID
            context_id: Optional context identifier
        """
        await self.play_animation(
            state=state,
            session_id=session_id,
            loop=True,
            context_id=context_id
        )

    async def _send_animation_command(
        self,
        session_id: Optional[str],
        animation_file: str,
        loop: bool,
        state: str
    ) -> None:
        """Send animation command to the WebUI via WebSocket.
        
        Args:
            session_id: The WebUI session ID
            animation_file: The animation file name
            loop: Whether to loop the animation
            state: The logical state name
        """
        if not self.webui:
            return
            
        # Resolve animation file lookup with persona-aware fallback:
        # 1) Check active persona skin animations (personas/<skin>/animations)
        # 2) Check global animations (/animations)
        # 3) Fallback to Rei skin animations (personas/Rei/animations)
        descriptor = None
        resolved_rel_path = None
        try:
            # Try active persona first (local import to avoid circular dependency)
            try:
                from core.persona_manager import get_persona_manager
            except Exception:
                get_persona_manager = None
            persona_manager = None
            if callable(get_persona_manager):
                try:
                    persona_manager = get_persona_manager()
                except Exception:
                    persona_manager = None
            active_persona_folder = None
            try:
                # persona manager may expose a folder or a name; try common properties
                if persona_manager and hasattr(persona_manager, '_current_persona') and persona_manager._current_persona:
                    # If the persona_manager was loaded from a folder, try to find a persona folder name
                    # We assume persona folders live under PERSONAS_DIR and may be named after the skin (e.g., Rei)
                    active_persona_folder = getattr(persona_manager._current_persona, 'id', None) or getattr(persona_manager._current_persona, 'name', None)
            except Exception:
                active_persona_folder = None

            candidates = []
            if active_persona_folder:
                p_anim_dir = self.SKINS_DIR / str(active_persona_folder) / 'animations'
                candidates.append((p_anim_dir, f"/skins/{active_persona_folder}/animations"))

            # Rei fallback (per-skin animations only)
            rei_dir = self.SKINS_DIR / 'Rei' / 'animations'
            candidates.append((rei_dir, f"/skins/Rei/animations"))

            # Search candidate dirs for the animation file
            for dir_path, url_prefix in candidates:
                try:
                    if dir_path.exists() and dir_path.is_dir():
                        # direct filename
                        candidate_file = dir_path / animation_file
                        if candidate_file.exists():
                            resolved_rel_path = f"{url_prefix}/{animation_file}"
                            # attempt to load descriptor next to the animation file
                            descriptor_path = candidate_file.with_suffix(candidate_file.suffix + '.json')
                            if descriptor_path.exists():
                                try:
                                    with descriptor_path.open('r', encoding='utf-8') as df:
                                        descriptor = json.load(df)
                                except Exception as exc:
                                    log_warning(f"[AnimationHandler] Failed to load descriptor {descriptor_path}: {exc}")
                            break
                        # also allow for files without exact match (case-insensitive)
                        for p in dir_path.iterdir():
                            if p.is_file() and p.name.lower() == animation_file.lower():
                                resolved_rel_path = f"{url_prefix}/{p.name}"
                                descriptor_path = p.with_suffix(p.suffix + '.json')
                                if descriptor_path.exists():
                                    try:
                                        with descriptor_path.open('r', encoding='utf-8') as df:
                                            descriptor = json.load(df)
                                    except Exception as exc:
                                        log_warning(f"[AnimationHandler] Failed to load descriptor {descriptor_path}: {exc}")
                                break
                except Exception:
                    continue

            # If still unresolved, default to global path (may 404 in client)
            if not resolved_rel_path:
                resolved_rel_path = f"/{self.ANIMATIONS_BASE_PATH}/{animation_file}"
        except Exception as exc:
            log_warning(f"[AnimationHandler] Error resolving animation path for {animation_file}: {exc}")

        # If session_id is None, broadcast to all connected WebUI sessions
        try:
            if session_id is None:
                for sid, websocket in list(self.webui.connections.items()):
                    try:
                        payload = {
                            "type": "animation",
                            "animation": resolved_rel_path,
                            "loop": loop,
                            "state": state
                        }
                        if descriptor is not None:
                            payload["descriptor"] = descriptor
                        await websocket.send_json(payload)
                        log_debug(f"[AnimationHandler] Broadcast animation to session {sid}: {resolved_rel_path}")
                    except Exception as exc:
                        log_warning(f"[AnimationHandler] Failed to send animation to session {sid}: {exc}")
                return

            websocket = self.webui.connections.get(session_id)
            if not websocket:
                log_warning(f"[AnimationHandler] No active websocket for session {session_id}")
                return

            payload = {
                "type": "animation",
                "animation": resolved_rel_path,
                "loop": loop,
                "state": state
            }
            if descriptor is not None:
                payload["descriptor"] = descriptor
            await websocket.send_json(payload)
            log_debug(f"[AnimationHandler] Sent animation command to session {session_id}: {resolved_rel_path}")
        except Exception as exc:
            log_warning(f"[AnimationHandler] Failed to send animation command: {exc}")

    async def _rotation_loop(self, session_id: Optional[str], state: AnimationState, context_id: Optional[str]):
        """Background loop that switches animations sequentially or randomly every 30-60s.
        
        For sequential states, advances through the animation list in order.
        For random states, picks randomly while avoiding repetition when possible.
        """
        key = f"{session_id}:{state.value}"
        try:
            while True:
                # Choose a random delay between 30 and 60 seconds
                delay = random.randint(30, 60)
                await asyncio.sleep(delay)

                async with self._lock:
                    # If current state changed, stop the loop
                    if self.current_state != state:
                        break
                    animations = self.get_animations_for_state(state)
                    if not animations or len(animations) <= 1:
                        break
                    
                    # Choose next animation based on rotation mode
                    if state.value in self._sequential_states:
                        # Sequential mode: advance to next animation, skip current if possible
                        current_index = self._sequence_indices.get(state.value, -1)
                        next_index = (current_index + 1) % len(animations)
                        candidate = animations[next_index]
                        
                        # If the next animation is the same as current and we have alternatives, skip it
                        if candidate == self.current_animation and len(animations) > 1:
                            next_index = (next_index + 1) % len(animations)
                            candidate = animations[next_index]
                        
                        self._sequence_indices[state.value] = next_index
                    else:
                        # Random mode: pick a different animation than currently playing when possible
                        candidate = random.choice(animations)
                        if candidate == self.current_animation and len(animations) > 1:
                            # pick another one
                            choices = [a for a in animations if a != self.current_animation]
                            candidate = random.choice(choices) if choices else candidate
                    self.current_animation = candidate
                    # send new animation command (preserve loop and state)
                    await self._send_animation_command(session_id, candidate, True, state.value)
        except asyncio.CancelledError:
            # Normal cancellation path
            pass
        except Exception as exc:
            log_warning(f"[AnimationHandler] Rotation loop error for {key}: {exc}")
        finally:
            # Clean up rotation task entry
            self._rotation_tasks.pop(key, None)

    async def _start_rotation_task(self, session_id: Optional[str], state: AnimationState, context_id: Optional[str]) -> None:
        key = f"{session_id}:{state.value}"
        # Cancel existing rotation task for the same key
        await self._stop_rotation_task(session_id, state)
        # Start new rotation task
        loop = asyncio.get_running_loop()
        task = loop.create_task(self._rotation_loop(session_id, state, context_id))
        self._rotation_tasks[key] = task

    async def _stop_rotation_task(self, session_id: Optional[str], state: AnimationState) -> None:
        key = f"{session_id}:{state.value}"
        task = self._rotation_tasks.get(key)
        if task:
            try:
                task.cancel()
                await task
            except Exception:
                pass
            self._rotation_tasks.pop(key, None)

    def get_current_state(self) -> AnimationState:
        """Get the current animation state.
        
        Returns:
            The current AnimationState
        """
        return self.current_state

    def get_current_animation(self) -> Optional[str]:
        """Get the current animation file name.
        
        Returns:
            The current animation file name or None
        """
        return self.current_animation


# Global animation handler instance
_animation_handler: Optional[AnimationHandler] = None


def get_animation_handler() -> AnimationHandler:
    """Get the global animation handler instance.
    
    Returns:
        The AnimationHandler instance
    """
    global _animation_handler
    if _animation_handler is None:
        _animation_handler = AnimationHandler()
    return _animation_handler


def set_animation_handler(handler: AnimationHandler) -> None:
    """Set the global animation handler instance.
    
    Args:
        handler: The AnimationHandler instance to set
    """
    global _animation_handler
    _animation_handler = handler
