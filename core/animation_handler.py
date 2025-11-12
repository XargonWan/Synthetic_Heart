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


# Priority levels for animation states (matching action_state_manager.py priorities)
# Higher = more important, cannot be interrupted by lower priority animations
ANIMATION_STATE_PRIORITIES = {
    AnimationState.IDLE: 0,     # Idle - lowest priority
    AnimationState.WRITE: 3,    # Writing - low priority
    AnimationState.TALK: 5,     # Talking - medium priority
    AnimationState.THINK: 10,   # Thinking - highest priority, cannot be interrupted
}


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

    def _resolve_animation_descriptor(self, animation_file: str):
        """Resolve animation file path and optional JSON descriptor.

        Returns a tuple (resolved_rel_path, descriptor) where descriptor may be None.
        This centralizes the resolution logic so callers can inspect descriptor
        before deciding loop/rotation behavior.
        """
        descriptor = None
        resolved_rel_path = None
        try:
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
                if persona_manager and hasattr(persona_manager, '_current_persona') and persona_manager._current_persona:
                    active_persona_folder = getattr(persona_manager._current_persona, 'id', None) or getattr(persona_manager._current_persona, 'name', None)
            except Exception:
                active_persona_folder = None

            candidates = []
            if active_persona_folder:
                p_anim_dir = self.SKINS_DIR / str(active_persona_folder) / 'animations'
                candidates.append((p_anim_dir, f"/skins/{active_persona_folder}/animations"))

            rei_dir = self.SKINS_DIR / 'Rei' / 'animations'
            candidates.append((rei_dir, f"/skins/Rei/animations"))

            for dir_path, url_prefix in candidates:
                try:
                    if dir_path.exists() and dir_path.is_dir():
                        candidate_file = dir_path / animation_file
                        if candidate_file.exists():
                            resolved_rel_path = f"{url_prefix}/{animation_file}"
                            descriptor_path = candidate_file.with_suffix(candidate_file.suffix + '.json')
                            if descriptor_path.exists():
                                try:
                                    with descriptor_path.open('r', encoding='utf-8') as df:
                                        descriptor = json.load(df)
                                except Exception:
                                    descriptor = None
                            break
                        for p in dir_path.iterdir():
                            if p.is_file() and p.name.lower() == animation_file.lower():
                                resolved_rel_path = f"{url_prefix}/{p.name}"
                                descriptor_path = p.with_suffix(p.suffix + '.json')
                                if descriptor_path.exists():
                                    try:
                                        with descriptor_path.open('r', encoding='utf-8') as df:
                                            descriptor = json.load(df)
                                    except Exception:
                                        descriptor = None
                                break
                except Exception:
                    continue

            if not resolved_rel_path:
                resolved_rel_path = f"/{self.ANIMATIONS_BASE_PATH}/{animation_file}"
        except Exception:
            resolved_rel_path = f"/{self.ANIMATIONS_BASE_PATH}/{animation_file}"

        return resolved_rel_path, descriptor

    def _analyze_animation_structure(self, descriptor: Optional[Dict], animation_file: str = "") -> Dict[str, bool]:
        """Analyze animation descriptor to determine available sections.
        
        Also validates play_once flag against structure and logs warnings if there are conflicts.
        
        Behavior:
        - If intro or outro exists + play_once flag: CONFLICT → ignore play_once, log warning
        - If only loop (no intro/outro) + play_once: loop plays once only (not really looped)
        
        Args:
            descriptor: Animation descriptor dict with potential intro/loop/outro sections
            animation_file: Animation filename (for logging purposes)
            
        Returns:
            Dict with keys: has_intro, has_loop, has_outro (all bool)
        """
        result = {
            "has_intro": False,
            "has_loop": False,
            "has_outro": False,
        }
        
        if not descriptor or not isinstance(descriptor, dict):
            return result
        
        # Check if sections exist and have valid frame ranges
        if "intro" in descriptor and isinstance(descriptor["intro"], dict):
            if "start_frame" in descriptor["intro"] and "end_frame" in descriptor["intro"]:
                result["has_intro"] = True
        
        if "loop" in descriptor and isinstance(descriptor["loop"], dict):
            if "start_frame" in descriptor["loop"] and "end_frame" in descriptor["loop"]:
                result["has_loop"] = True
        
        if "outro" in descriptor and isinstance(descriptor["outro"], dict):
            if "start_frame" in descriptor["outro"] and "end_frame" in descriptor["outro"]:
                result["has_outro"] = True
        
        # Validate play_once flag: it conflicts with intro/outro structure
        # (play_once means "play the whole animation once", but intro/outro define
        #  a structured animation that should execute its sections in order)
        if descriptor.get("play_once"):
            has_structured_sections = result["has_intro"] or result["has_outro"]
            if has_structured_sections:
                log_warning(
                    f"[AnimationHandler] Animation '{animation_file}' has both 'play_once' flag "
                    f"and structured sections (intro/outro). 'play_once' will be ignored because "
                    f"intro/outro structure takes precedence. "
                    f"Structure: intro={result['has_intro']}, loop={result['has_loop']}, outro={result['has_outro']}"
                )
        
        return result

    async def play_animation(
        self,
        state: AnimationState,
        session_id: Optional[str],
        loop: bool = True,
        context_id: Optional[str] = None,
        priority: Optional[int] = None,
    ) -> None:
        """Play an animation for a specific state.
        
        If the animation has an intro section in its descriptor, it will be played first,
        followed by the loop section on repeat. When stop_animation() is called, the outro
        section is played before returning to Idle.
        
        Args:
            state: The animation state to play
            session_id: The WebUI session ID to send the animation to
            loop: Whether the animation should loop (ignored if descriptor specifies intro/loop/outro)
            context_id: Optional identifier for this animation context (for tracking)
            priority: Optional priority level for this animation context.
                      If not provided, uses the priority from ANIMATION_STATE_PRIORITIES mapping.
        """
        async with self._lock:
            # Use state priority if not explicitly provided
            if priority is None:
                priority = ANIMATION_STATE_PRIORITIES.get(state, 0)
            
            # If we have a context_id, mark it as active with priority
            if context_id:
                self._active_tasks[context_id] = priority

            
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
                # Resolve descriptor for intelligent section handling
                resolved_path, descriptor = self._resolve_animation_descriptor(selected_animation)
                structure = self._analyze_animation_structure(descriptor, selected_animation)
                
                # Determine effective loop behavior based on descriptor structure:
                # 1. If has intro/outro (structured animation): loop=True if has loop section, else play once
                # 2. If only loop (no intro/outro) + play_once: play loop once only (don't really loop)
                # 3. Otherwise: use provided loop parameter
                has_intro_or_outro = structure["has_intro"] or structure["has_outro"]
                
                if has_intro_or_outro:
                    # Structured animation (intro/outro present)
                    # play_once flag is ignored (warning already logged in _analyze_animation_structure)
                    if structure["has_loop"]:
                        # intro/loop/outro or intro/outro - loop the middle section
                        effective_loop = True
                    else:
                        # intro only or intro/outro (no loop) - play once
                        effective_loop = False
                    start_rotation = False
                elif structure["has_loop"] and descriptor and descriptor.get("play_once"):
                    # Only loop section (no intro/outro) with play_once flag
                    # Loop plays once only - don't really loop, don't rotate
                    log_debug(
                        f"[AnimationHandler] Animation '{selected_animation}' has loop section "
                        f"with play_once flag: loop will play once only (no looping)"
                    )
                    effective_loop = False
                    start_rotation = False
                elif structure["has_loop"]:
                    # Only loop section - loop it normally
                    effective_loop = True
                    start_rotation = False
                elif descriptor and descriptor.get("play_once"):
                    # Legacy: play_once flag without structured sections
                    effective_loop = False
                    start_rotation = False
                else:
                    # No special structure, use provided loop parameter
                    effective_loop = loop
                    start_rotation = len(animations) > 1

                log_debug(
                    f"[AnimationHandler] Animation structure - intro: {structure['has_intro']}, "
                    f"loop: {structure['has_loop']}, outro: {structure['has_outro']}, "
                    f"play_once: {descriptor.get('play_once') if descriptor else False}, "
                    f"effective_loop: {effective_loop}"
                )

                await self._send_animation_command(
                    session_id=session_id,
                    animation_file=selected_animation,
                    loop=effective_loop,
                    state=state.value,
                    descriptor=descriptor
                )
            else:
                log_warning("[AnimationHandler] WebUI not set, cannot send animation command")
                start_rotation = False

            # If there are multiple animations for this state, start a background
            # rotation task that will randomly switch between them every 30-60s.
            # Skip rotation for animations with loop/intro/outro structure.
            if start_rotation:
                await self._start_rotation_task(session_id, state, context_id)
            else:
                await self._stop_rotation_task(session_id, state)

    async def stop_animation(self, context_id: str, session_id: str) -> None:
        """Stop an animation context and return to Idle if no other contexts are active.
        
        Intelligently handles animations with flexible outro sections:
        - If outro exists: play outro before transitioning to Idle
        - If no outro: transition immediately to Idle
        - Handles partial animations gracefully (intro-only, loop-only, etc.)
        
        Args:
            context_id: The context identifier to stop
            session_id: The WebUI session ID
        """
        async with self._lock:
            # Get current animation descriptor to check structure
            current_animation = self.current_animation
            descriptor = None
            if current_animation:
                _, descriptor = self._resolve_animation_descriptor(current_animation)
            
            # Analyze animation structure
            structure = self._analyze_animation_structure(descriptor)
            
            # If the animation has an outro, play it first
            if structure["has_outro"]:
                log_debug(
                    f"[AnimationHandler] Playing outro for {current_animation} "
                    f"before stopping (context={context_id}, session={session_id})"
                )
                # Play outro with loop=False (play once), explicitly requesting 'outro' section
                await self._send_animation_command(
                    session_id=session_id,
                    animation_file=current_animation,
                    loop=False,
                    state=self.current_state.value,
                    descriptor=descriptor,
                    play_section="outro"
                )
                # Estimate outro duration and wait before transitioning to Idle
                # Default: assume ~30 frames at 30fps = ~1 second per 30 frames
                outro_frames = descriptor["outro"].get("end_frame", 0) - descriptor["outro"].get("start_frame", 0)
                outro_duration = max(0.5, outro_frames / 30.0)  # Minimum 0.5s, assume 30fps
                log_debug(f"[AnimationHandler] Waiting {outro_duration:.1f}s for outro to complete")
                # Release lock during wait so other operations can proceed
                # But mark that we're in outro playback
                self._active_tasks.pop(context_id, None)
            else:
                # No outro section - transition immediately
                log_debug(
                    f"[AnimationHandler] No outro section for {current_animation}, "
                    f"stopping immediately (context={context_id})"
                )
                self._active_tasks.pop(context_id, None)
                outro_duration = 0

        # Wait for outro if needed (outside the lock)
        if outro_duration > 0:
            await asyncio.sleep(outro_duration)

        # After outro (or immediately if no outro), transition to Idle
        async with self._lock:
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
        state: str,
        descriptor: Optional[Dict] = None,
        play_section: Optional[str] = None
    ) -> None:
        """Send animation command to the WebUI via WebSocket.
        
        If descriptor contains intro/loop sections, the WebUI should play intro first,
        then loop the loop section. The descriptor is sent along for WebUI interpretation.
        
        Args:
            session_id: The WebUI session ID
            animation_file: The animation file name
            loop: Whether to loop the animation
            state: The logical state name
            descriptor: Optional animation descriptor with frame info (intro/loop/outro)
            play_section: Optional section to play - 'intro', 'loop', 'outro', or None for full animation
        """
        if not self.webui:
            return
            
        # Resolve path and descriptor (if not already provided)
        if descriptor is None:
            resolved_rel_path, descriptor = self._resolve_animation_descriptor(animation_file)
        else:
            resolved_rel_path, _ = self._resolve_animation_descriptor(animation_file)

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
                        if play_section is not None:
                            payload["play_section"] = play_section
                        await websocket.send_json(payload)
                        log_debug(f"[AnimationHandler] Broadcast animation to session {sid}: {resolved_rel_path}")
                    except Exception as exc:
                        log_warning(f"[AnimationHandler] Failed to send animation to session {sid}: {exc}")
                return

            websocket = self.webui.connections.get(session_id)
            if not websocket:
                log_debug(f"[AnimationHandler] No active websocket for session {session_id} (may be disconnected)")
                return

            payload = {
                "type": "animation",
                "animation": resolved_rel_path,
                "loop": loop,
                "state": state
            }
            if descriptor is not None:
                payload["descriptor"] = descriptor
            if play_section is not None:
                payload["play_section"] = play_section
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
                    # Resolve descriptor for candidate to respect play_once if present
                    _, candidate_descriptor = self._resolve_animation_descriptor(candidate)
                    candidate_loop = False if (candidate_descriptor and candidate_descriptor.get("play_once")) else True
                    # send new animation command (loop depends on descriptor)
                    await self._send_animation_command(session_id, candidate, candidate_loop, state.value)
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
            except asyncio.CancelledError:
                # Normal cancellation - task was cancelled successfully
                pass
            except Exception as exc:
                log_warning(f"[AnimationHandler] Error cancelling rotation task {key}: {exc}")
            finally:
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
