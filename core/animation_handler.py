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
    # Default animations dir (Rei fallback)
    SKIN_DEFAULT_ANIMATIONS_DIR = SKINS_DIR / "Rei" / "animations"


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
        
        # Centralized animation state that syncs across all clients
        self._current_animation_file: Optional[str] = None  # Actual file being played
        self._current_animation_descriptor: Optional[Dict] = None  # Descriptor with frame info
        self._animation_state_changed_callbacks: List[callable] = []  # Callbacks when animation changes
        # Plugin/override state animations: state_name -> {'loop': [...], 'post': [...], 'other': [...]}
        self._registered_state_animations: Dict[str, Dict[str, List[str]]] = {}
        # State aliases map (normalized state -> list of alias names)
        self._state_aliases: Dict[str, List[str]] = {}
        # Additional search paths to consider (ordered)
        self._search_paths: List[Path] = []
        
    def set_webui(self, webui: SynthWebUIInterface) -> None:
        """Set or update the WebUI reference.
        
        Args:
            webui: The SynthWebUIInterface instance
        """
        self.webui = webui
        log_debug("[AnimationHandler] WebUI reference set")

    def register_animation_state_changed_callback(self, callback: callable) -> None:
        """Register a callback to be called when animation state changes.
        
        The callback will be called with (state, animation_file, descriptor) as arguments.
        
        Args:
            callback: Async function to call when animation changes
        """
        self._animation_state_changed_callbacks.append(callback)
        log_debug("[AnimationHandler] Registered animation state changed callback")

    async def _notify_animation_state_changed(
        self, 
        state: AnimationState, 
        animation_file: str, 
        descriptor: Optional[Dict]
    ) -> None:
        """Notify all callbacks that animation state has changed.
        
        Args:
            state: The new animation state
            animation_file: The animation file name
            descriptor: The animation descriptor
        """
        log_debug(f"[AnimationHandler] _notify_animation_state_changed CALLED: state={state.value}, animation={animation_file}, callbacks_count={len(self._animation_state_changed_callbacks)}")
        for callback in self._animation_state_changed_callbacks:
            try:
                log_debug(f"[AnimationHandler] Calling callback: {callback.__name__ if hasattr(callback, '__name__') else 'unknown'}")
                if asyncio.iscoroutinefunction(callback):
                    await callback(state, animation_file, descriptor)
                else:
                    callback(state, animation_file, descriptor)
            except Exception as exc:
                log_warning(f"[AnimationHandler] Error in animation state callback: {exc}")

    def get_current_animation_state(self) -> Dict[str, any]:
        """Get the current centralized animation state.
        
        Returns:
            Dict with 'state', 'animation_file', and 'descriptor' keys
        """
        return {
            "state": self.current_state.value,
            "animation_file": self._current_animation_file,
            "descriptor": self._current_animation_descriptor
        }

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

    def set_animation_search_paths(self, paths: List[Path]) -> None:
        """Set additional search paths (ordered) to resolve animation files.

        These are checked after the active persona skin and before the Rei fallback.
        """
        self._search_paths = list(paths)
        log_debug(f"[AnimationHandler] Animation search paths set: {self._search_paths}")

    def register_state_animations(self, state: str, animations: Dict[str, List[str]], sequential: bool = False) -> None:
        """Register override animations for a logical state.

        animations should be a dict with optional keys: 'loop', 'post', 'other'.
        """
        key = state.lower()
        self._registered_state_animations[key] = animations
        if sequential:
            self._sequential_states.add(key)
        log_debug(f"[AnimationHandler] Registered override animations for state {key}: {animations}")

    def register_state_aliases(self, aliases: Dict[str, List[str]]) -> None:
        """Register alias names for canonical states (e.g. THINK -> ['thinking','ponder'])."""
        for k, v in aliases.items():
            self._state_aliases[k.lower()] = [a.lower() for a in v]
        log_debug(f"[AnimationHandler] Registered state aliases: {self._state_aliases}")

    def _build_search_paths_for_state(self, state_name: str) -> List[Path]:
        """Return ordered list of paths to search for animations for a state."""
        paths: List[Path] = []
        # Active persona path
        try:
            from core.persona_manager import get_persona_manager
            pm = get_persona_manager()
            active_persona_folder = None
            if pm and hasattr(pm, '_current_persona') and pm._current_persona:
                active_persona_folder = getattr(pm._current_persona, 'id', None) or getattr(pm._current_persona, 'name', None)
        except Exception:
            active_persona_folder = None

        if active_persona_folder:
            persona_state_dir = self.SKINS_DIR / str(active_persona_folder) / 'animations' / state_name
            paths.append(persona_state_dir)

        # Additional configured search paths (state subfolder)
        for p in self._search_paths:
            candidate = Path(p) / state_name
            paths.append(candidate)

        # Rei fallback
        paths.append(self.SKIN_DEFAULT_ANIMATIONS_DIR / state_name)

        # Also include root animations folders (no state subfolder) as fallback
        if active_persona_folder:
            paths.append(self.SKINS_DIR / str(active_persona_folder) / 'animations')
        paths.append(self.SKIN_DEFAULT_ANIMATIONS_DIR)

        return paths

    def get_animation_variants(self, state: str) -> Dict[str, List[str]]:
        """Discover animation variants for a given state.

        Returns a dict with keys 'loop', 'post', 'other' containing file names (not full paths).
        Resolution order: registered overrides -> exact file match -> state folder contents -> aliases -> Rei fallback
        """
        key = state.lower()
        variants = {"loop": [], "post": [], "other": []}

        # 1) Registered overrides
        if key in self._registered_state_animations:
            reg = self._registered_state_animations[key]
            for cat in ("loop", "post", "other"):
                vals = reg.get(cat, [])
                if vals:
                    variants[cat].extend(vals)
            # If any registered overrides exist return them (highest priority)
            return variants

        # 2) Try exact file match <state>.fbx (case-insensitive) across search paths
        search_paths = self._build_search_paths_for_state(key)
        found_any = False
        for sp in search_paths:
            try:
                if not sp.exists() or not sp.is_dir():
                    continue
                # direct file
                candidate = sp / (f"{key}.fbx")
                if candidate.exists() and candidate.is_file():
                    found_any = True
                    _, desc = self._resolve_animation_descriptor_for_state(candidate.name, key)
                    structure = self._analyze_animation_structure(desc, candidate.name)
                    if structure["has_outro"] or (desc and desc.get("play_once")):
                        variants["post"].append(candidate.name)
                    elif structure["has_loop"]:
                        variants["loop"].append(candidate.name)
                    else:
                        variants["loop"].append(candidate.name)
                    # stop searching direct match
                    break
                # folder case: list all .fbx inside
                if sp.is_dir():
                    for f in sp.glob('*.fbx'):
                        found_any = True
                        _, desc = self._resolve_animation_descriptor_for_state(f.name, key)
                        structure = self._analyze_animation_structure(desc, f.name)
                        if structure["has_outro"] or (desc and desc.get("play_once")):
                            variants["post"].append(f.name)
                        elif structure["has_loop"]:
                            variants["loop"].append(f.name)
                        else:
                            variants["loop"].append(f.name)
            except Exception:
                continue

        # 3) Aliases
        if not found_any and key in self._state_aliases:
            for alias in self._state_aliases[key]:
                alias_variants = self.get_animation_variants(alias)
                for cat in variants:
                    variants[cat].extend(alias_variants.get(cat, []))

        # 4) Ensure uniqueness while preserving order
        for cat in variants:
            seen = set()
            unique = []
            for a in variants[cat]:
                if a not in seen:
                    seen.add(a)
                    unique.append(a)
            variants[cat] = unique

        return variants

    def _resolve_animation_descriptor(self, animation_file: str):
        """Resolve animation file path and optional JSON descriptor.

        Returns a tuple (resolved_rel_path, descriptor) where descriptor may be None.
        This centralizes the resolution logic so callers can inspect descriptor
        before deciding loop/rotation behavior.
        """
        # Use the current state if available to find descriptor in the right subdirectory
        # This is important because animations are organized by state (think/, write/, etc.)
        state_folder = self.current_state.value if self.current_state else None
        return self._resolve_animation_descriptor_for_state(animation_file, state_folder)
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

    def _resolve_animation_descriptor_for_state(self, animation_file: str, state_folder: Optional[str] = None):
        """Resolve animation file path and descriptor, knowing the state folder.
        
        Animations are organized by state: skins/Rei/animations/think/, skins/Rei/animations/write/, etc.
        This method searches for the animation in the correct state folder.
        
        Args:
            animation_file: The animation file name (e.g., "Thinking.fbx")
            state_folder: The state folder name (e.g., "think", "write"). If None, searches root animations.
        
        Returns:
            Tuple of (resolved_url_path, descriptor) where descriptor may be None
        """
        descriptor = None
        resolved_rel_path = None
        
        log_debug(f"[AnimationHandler._resolve_descriptor] Called with animation={animation_file}, state_folder={state_folder}")
        
        try:
            # Get active persona
            try:
                from core.persona_manager import get_persona_manager
                persona_manager = get_persona_manager()
                active_persona_folder = None
                if persona_manager and hasattr(persona_manager, '_current_persona') and persona_manager._current_persona:
                    active_persona_folder = getattr(persona_manager._current_persona, 'id', None) or getattr(persona_manager._current_persona, 'name', None)
            except Exception:
                active_persona_folder = None
            
            log_debug(f"[AnimationHandler._resolve_descriptor] Active persona: {active_persona_folder}")
            
            # Build candidate paths with state folder
            candidates = []
            if active_persona_folder and state_folder:
                p_anim_dir = self.SKINS_DIR / str(active_persona_folder) / 'animations' / state_folder
                candidates.append((p_anim_dir, f"/skins/{active_persona_folder}/animations/{state_folder}"))
            
            if state_folder:
                rei_dir = self.SKINS_DIR / 'Rei' / 'animations' / state_folder
                candidates.append((rei_dir, f"/skins/Rei/animations/{state_folder}"))
                log_debug(f"[AnimationHandler._resolve_descriptor] Will search state folder: {rei_dir}")
            
            # Also try root animations folder as fallback
            if active_persona_folder:
                p_anim_dir = self.SKINS_DIR / str(active_persona_folder) / 'animations'
                candidates.append((p_anim_dir, f"/skins/{active_persona_folder}/animations"))
            
            rei_root_dir = self.SKINS_DIR / 'Rei' / 'animations'
            candidates.append((rei_root_dir, f"/skins/Rei/animations"))
            
            # Search for animation file
            for dir_path, url_prefix in candidates:
                try:
                    if not dir_path.exists() or not dir_path.is_dir():
                        continue
                    
                    # Direct match
                    candidate_file = dir_path / animation_file
                    if candidate_file.exists() and candidate_file.is_file():
                        # Build URL with state folder if applicable
                        if state_folder and url_prefix.endswith(state_folder):
                            resolved_rel_path = f"{url_prefix}/{animation_file}"
                        else:
                            resolved_rel_path = f"{url_prefix}/{animation_file}"
                        
                        # Look for descriptor
                        descriptor_path = candidate_file.with_suffix(candidate_file.suffix + '.json')
                        if descriptor_path.exists():
                            try:
                                with descriptor_path.open('r', encoding='utf-8') as df:
                                    descriptor = json.load(df)
                                    log_debug(f"[AnimationHandler] Loaded descriptor for {animation_file}: {descriptor}")
                            except Exception as e:
                                log_debug(f"[AnimationHandler] Failed to load descriptor for {animation_file}: {e}")
                                descriptor = None
                        break
                    
                    # Case-insensitive match
                    for p in dir_path.iterdir():
                        if p.is_file() and p.name.lower() == animation_file.lower():
                            if state_folder and url_prefix.endswith(state_folder):
                                resolved_rel_path = f"{url_prefix}/{p.name}"
                            else:
                                resolved_rel_path = f"{url_prefix}/{p.name}"
                            
                            descriptor_path = p.with_suffix(p.suffix + '.json')
                            if descriptor_path.exists():
                                try:
                                    with descriptor_path.open('r', encoding='utf-8') as df:
                                        descriptor = json.load(df)
                                        log_debug(f"[AnimationHandler] Loaded descriptor for {p.name}: {descriptor}")
                                except Exception as e:
                                    log_debug(f"[AnimationHandler] Failed to load descriptor for {p.name}: {e}")
                                    descriptor = None
                            break
                    
                    if resolved_rel_path:
                        break
                        
                except Exception as e:
                    log_debug(f"[AnimationHandler] Error searching in {dir_path}: {e}")
                    continue
            
            # Fallback URL if not found
            if not resolved_rel_path:
                if state_folder:
                    resolved_rel_path = f"/skins/Rei/animations/{state_folder}/{animation_file}"
                else:
                    resolved_rel_path = f"/skins/Rei/animations/{animation_file}"
                    
        except Exception as e:
            log_warning(f"[AnimationHandler] Error resolving animation descriptor: {e}")
            if state_folder:
                resolved_rel_path = f"/skins/Rei/animations/{state_folder}/{animation_file}"
            else:
                resolved_rel_path = f"/skins/Rei/animations/{animation_file}"
        
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
        log_info(f"[AnimationHandler] ⭐ play_animation CALLED: state={state.value}, session={session_id}, loop={loop}, context={context_id}, priority={priority}", log_file="webui")
        
        # Check if we need to play outro before transitioning to new animation
        # This must be done BEFORE acquiring the lock to avoid deadlocks
        outro_duration = 0
        needs_outro_transition = False
        
        try:
            if self.current_state != state and self.current_animation:
                # Check if current animation has an outro that should be played
                _, current_descriptor = self._resolve_animation_descriptor(self.current_animation)
                current_structure = self._analyze_animation_structure(current_descriptor, self.current_animation)
                
                if current_structure["has_outro"]:
                    needs_outro_transition = True
                    log_debug(
                        f"[AnimationHandler] Preparing transition from {self.current_state.value} "
                        f"to {state.value}: playing outro for {self.current_animation}"
                    )
        except Exception as exc:
            log_warning(f"[AnimationHandler] Error checking outro during transition: {exc}")
        
        async with self._lock:
            # Use state priority if not explicitly provided
            if priority is None:
                priority = ANIMATION_STATE_PRIORITIES.get(state, 0)
            
            # If we have a context_id, mark it as active with priority
            if context_id:
                self._active_tasks[context_id] = priority

            
            # Select animation file
            # Prefer variants discovered via descriptors and overrides
            variants = self.get_animation_variants(state.value)
            animations = variants.get("loop", []) or variants.get("post", []) or variants.get("other", [])
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
                
                log_debug(
                    f"[AnimationHandler] Resolved animation {selected_animation}: "
                    f"descriptor={'found' if descriptor else 'NOT FOUND'}, "
                    f"structure=(intro:{structure['has_intro']}, loop:{structure['has_loop']}, outro:{structure['has_outro']})"
                )
                
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

                # If we need to play outro before transitioning, do it now (before releasing lock)
                if needs_outro_transition and self.webui:
                    log_debug(
                        f"[AnimationHandler] Sending outro command for {self.current_animation} "
                        f"before transitioning to {state.value}"
                    )
                    # Get the stored descriptor for the animation that will play outro
                    _, prev_descriptor = self._resolve_animation_descriptor(self.current_animation)
                    prev_structure = self._analyze_animation_structure(prev_descriptor, self.current_animation)
                    
                    # Send outro command
                    await self._send_animation_command(
                        session_id=session_id,
                        animation_file=self.current_animation,
                        loop=False,
                        state=self.current_state.value,
                        descriptor=prev_descriptor,
                        play_section="outro"
                    )
                    
                    # Calculate outro duration to wait before sending the new animation command
                    if prev_descriptor and "outro" in prev_descriptor:
                        outro_start = prev_descriptor["outro"].get("start_frame", 0)
                        outro_end = prev_descriptor["outro"].get("end_frame", 0)
                        outro_duration = max(0.3, (outro_end - outro_start) / 30.0)  # Assume 30fps, min 0.3s
                        log_debug(f"[AnimationHandler] Outro duration: {outro_duration:.2f}s")

                await self._send_animation_command(
                    session_id=session_id,
                    animation_file=selected_animation,
                    loop=effective_loop,
                    state=state.value,
                    descriptor=descriptor
                )
                
                # Update centralized animation state and notify all clients
                self._current_animation_file = selected_animation
                self._current_animation_descriptor = descriptor
                await self._notify_animation_state_changed(state, selected_animation, descriptor)
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
        
        # Wait for outro to complete outside the lock (so other operations can proceed)
        if needs_outro_transition and outro_duration > 0:
            log_debug(f"[AnimationHandler] Waiting {outro_duration:.2f}s for outro to complete...")
            await asyncio.sleep(outro_duration)

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
