"""KaradaStateServer — centralized VRM state service for SyntH.

This module manages the single source of truth for three independent streams:
the active VRM model, the animation state, and face blend-shape values.
Clients receive push-updates via WebSocket; only real state changes are broadcast.

Components trigger logical animation states (Think, Write, Talk, Idle) which are
mapped to actual FBX animation files with smooth intro/loop/outro transitions.
"""

from __future__ import annotations

import asyncio
import random
import uuid
from collections.abc import Callable
from enum import Enum
from typing import Dict, List, Optional, TYPE_CHECKING, Any
from datetime import datetime, timezone
from pathlib import Path
import json

from core.karada_transport import KaradaTransport
from core.logging_utils import log_debug, log_info, log_warning

if TYPE_CHECKING:
    from core.webui import SynthWebUIInterface


class AnimationState(Enum):
    """Logical animation states that components can trigger."""

    IDLE = "idle"
    THINK = "think"
    TOUCH = "touch"
    WRITE = "write"
    TALK = "talk"
    SKIN_CHANGE = "skin_change"


AnimationStateChangedCallback = Callable[
    [AnimationState, str, Optional[Dict[str, Any]]],
    Any,
]


# Priority levels for animation states (matching action_state_manager.py priorities)
# Higher = more important, cannot be interrupted by lower priority animations
ANIMATION_STATE_PRIORITIES = {
    AnimationState.IDLE: 0,  # Idle - lowest priority
    AnimationState.WRITE: 3,  # Writing - low priority
    AnimationState.TALK: 5,  # Talking - medium priority
    AnimationState.THINK: 10,  # Thinking - highest priority, cannot be interrupted
    AnimationState.TOUCH: 11,  # Touch reaction - temporary overlay above think/write/talk
    AnimationState.SKIN_CHANGE: 15,  # Skin change - always plays, overrides everything
}


class KaradaStateServer:
    """Centralized VRM state service — single source of truth for model, animations and face values.

    This server:
    - Maps logical animation states to FBX files with intro/loop/outro support
    - Tracks and broadcasts the current animation state to all connected clients
    - Manages VRM model state and pushes it to clients on connect/change
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

    def __init__(self, webui: Optional[SynthWebUIInterface] = None) -> None:
        """Initialize the animation handler.

        Args:
            webui: Reference to the SynthWebUIInterface for sending animation commands.
                   Kept for backward compatibility; prefer ``add_transport()``.
        """
        self.webui = webui
        # Transport layer — the primary mechanism for reaching clients.
        self._transports: List[KaradaTransport] = []
        self.current_state: AnimationState = AnimationState.IDLE
        self.current_animation: Optional[str] = None
        self._current_context_id: Optional[str] = None
        self._lock = asyncio.Lock()
        # Track active animation contexts -> map context_id to priority (int)
        # If a context_id maps to None, treat as priority 0
        self._active_tasks: Dict[str, Optional[int]] = {}
        self._active_context_meta: Dict[str, Dict[str, Any]] = {}
        self._context_sequence: int = 0
        # Rotation tasks per session+state key -> asyncio.Task
        self._rotation_tasks: Dict[str, asyncio.Task] = {}
        # Sequential animation indices per state -> map state.value to current index
        self._sequence_indices: Dict[str, int] = {}
        # States that use sequential rotation instead of random
        self._sequential_states = {AnimationState.IDLE.value}

        # Centralized animation state that syncs across all clients
        self._current_animation_file: Optional[str] = None  # Actual file being played
        self._current_animation_descriptor: Optional[Dict[str, Any]] = (
            None  # Descriptor with frame info
        )
        self._current_animation_started_at: Optional[datetime] = (
            None  # UTC timestamp for current animation start
        )
        self._current_animation_phase: str = "loop"
        self._current_animation_frame_range: Optional[Dict[str, int]] = None
        self._current_phase_authoritative: bool = False
        self._current_animation_session_id: Optional[str] = None
        self._phase_task: Optional[asyncio.Task] = None
        self._phase_generation: int = 0
        # Stable identifier for the currently playing animation; changes only when
        # a genuinely new animation file starts (not during outro or re-send of the
        # same file).  All interfaces use this to avoid restarting an already-running
        # animation after a reconnect or periodic re-sync.
        self._current_animation_id: str = ""
        self._animation_state_changed_callbacks: List[
            AnimationStateChangedCallback
        ] = []
        # Callbacks when animation changes

        # new methods inserted here (will add after class header)
        # Plugin/override state animations: state_name -> {'loop': [...], 'post': [...], 'other': [...]}
        self._registered_state_animations: Dict[str, Dict[str, List[str]]] = {}
        # State aliases map (normalized state -> list of alias names)
        self._state_aliases: Dict[str, List[str]] = {}
        # Additional search paths to consider (ordered)
        self._search_paths: List[Path] = []
        # Temporary search paths managed via uploads/helpers
        self._temporary_search_paths: List[Path] = []
        # VRM model state: set via set_vrm_model() and read by get_full_state()
        self._vrm_model_url: Optional[str] = None
        self._vrm_model_name: Optional[str] = None
        self._vrm_model_hash: Optional[str] = None

        # Audio state: tracks the currently-playing TTS audio so that
        # late-joining clients can catch up.  Cleared automatically when
        # the estimated duration elapses.
        self._current_audio_url: Optional[str] = None
        self._current_audio_duration_s: Optional[float] = None
        self._current_audio_lipsync: Optional[Dict] = None
        self._current_audio_started_at: Optional[datetime] = None
        self._audio_clear_task: Optional[asyncio.Task] = None
        # Face state: authoritative snapshot of the last face values broadcast
        # to clients. Keys are normalized blendshape/emotion values in the 0..1
        # range and reused for reconnect/full-state sync.
        self._current_face_values: Dict[str, float] = {}
        self._face_values_initialized: bool = False

        # Priority registration: maps state names to their priority values.
        # Starts from ANIMATION_STATE_PRIORITIES and can be extended at runtime.
        self._state_priorities: Dict[str, int] = {
            s.value: p for s, p in ANIMATION_STATE_PRIORITIES.items()
        }

        # Watchdog: periodic background task to detect stuck states
        self._watchdog_task: Optional[asyncio.Task] = None

    def set_webui(self, webui: SynthWebUIInterface) -> None:
        """Set or update the WebUI reference.

        Args:
            webui: The SynthWebUIInterface instance
        """
        self.webui = webui
        log_debug("[KaradaStateServer] WebUI reference set")
        try:
            connections = getattr(webui, "connections", None)
            if isinstance(connections, dict):
                from core.karada_ws_transport import WebSocketTransport

                self.add_transport(WebSocketTransport(connections))
        except Exception:
            pass
        # Register a lightweight summary callback so the WebUI can broadcast
        # the canonical animation state to all connected clients whenever it
        # changes. This is different from the full 'animation' command (which
        # includes playback instruction) and allows multiple clients to observe
        # the central state even if they missed the immediate play command.
        try:
            cb = getattr(webui, "_broadcast_animation_state_summary", None)
            if cb and cb not in self._animation_state_changed_callbacks:
                self.register_animation_state_changed_callback(cb)
                log_debug(
                    "[KaradaStateServer] Registered WebUI animation state summary callback"
                )
        except Exception:
            pass
        # Also register the authoritative broadcast callback so that when the
        # centralized animation state changes we explicitly push an
        # 'animation' command to all connected WebUI clients. This helps
        # ensure clients that treat the lightweight 'animation_state' as
        # informational will still receive a playback command to apply.
        try:
            cb2 = getattr(webui, "_broadcast_animation_state", None)
            if cb2 and cb2 not in self._animation_state_changed_callbacks:
                self.register_animation_state_changed_callback(cb2)
                log_debug(
                    "[KaradaStateServer] Registered WebUI authoritative animation broadcast callback"
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Transport management
    # ------------------------------------------------------------------

    def _is_same_transport_target(
        self, existing: KaradaTransport, candidate: KaradaTransport
    ) -> bool:
        if existing is candidate:
            return True

        existing_connections = getattr(existing, "_connections", None)
        candidate_connections = getattr(candidate, "_connections", None)
        return (
            type(existing) is type(candidate)
            and existing_connections is not None
            and existing_connections is candidate_connections
        )

    def add_transport(self, transport: KaradaTransport) -> None:
        """Register a transport for delivering payloads to clients.

        Args:
            transport: A concrete :class:`KaradaTransport` implementation.
        """
        for existing in self._transports:
            if self._is_same_transport_target(existing, transport):
                return

        self._transports.append(transport)
        log_debug(f"[KaradaStateServer] Transport added: {type(transport).__name__}")
        # Start the watchdog when the first transport arrives
        if len(self._transports) == 1:
            self.start_watchdog()

    def remove_transport(self, transport: KaradaTransport) -> None:
        """Un-register a previously added transport."""
        try:
            self._transports.remove(transport)
            log_debug(
                f"[KaradaStateServer] Transport removed: {type(transport).__name__}"
            )
        except ValueError:
            pass

    def _has_any_transport(self) -> bool:
        """Return True if at least one transport is registered."""
        return bool(self._transports)

    # ------------------------------------------------------------------
    # Face / expression helpers
    # ------------------------------------------------------------------

    async def set_face_values(self, values: Dict[str, float]) -> None:
        """Broadcast raw face values to all connected clients.

        This is a thin wrapper used by EmotionManager.  The values map is sent
        as-is in a ``vrm_face`` WebSocket packet.  Any exceptions are logged
        but not re-raised since this call is often invoked inside a task.
        """
        if not isinstance(values, dict):
            return

        merged = dict(self._current_face_values)
        for key, raw_value in values.items():
            name = str(key).strip()
            if not name:
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError):
                continue
            if value > 1.0:
                value = value / 10.0
            value = max(0.0, min(1.0, value))
            if value <= 0.0001:
                merged.pop(name, None)
            else:
                merged[name] = value

        self._current_face_values = merged
        self._face_values_initialized = True

        if not self._has_any_transport():
            return
        payload = {"type": "vrm_face", "values": dict(self._current_face_values)}
        for transport in self._transports:
            try:
                await transport.broadcast_face(payload)
            except Exception as exc:  # pragma: no cover - best effort
                log_warning(
                    f"[KaradaStateServer] failed to broadcast face_values via {type(transport).__name__}: {exc}"
                )

    async def clear_face_values(self) -> None:
        """Clear all tracked face values and broadcast a neutral snapshot."""
        self._current_face_values = {}
        self._face_values_initialized = True

        if not self._has_any_transport():
            return

        payload = {"type": "vrm_face", "values": {}}
        for transport in self._transports:
            try:
                await transport.broadcast_face(payload)
            except Exception as exc:  # pragma: no cover - best effort
                log_warning(
                    f"[KaradaStateServer] failed to clear face_values via {type(transport).__name__}: {exc}"
                )

    async def push_face_expression(
        self,
        name: Optional[str],
        intensity: float,
        targets: Optional[Dict[str, float]] = None,
    ) -> None:
        """Emit a high-priority facial expression packet.

        If ``name`` is None or empty, a ``vrm_expression_clear`` packet is
        broadcast which instructs the client to remove the
        ``facial_expression`` source from its pipeline.

        If *targets* is provided (a pre-resolved mapping of blendshape key
        → intensity already scaled by the caller), a ``vrm_expression_set``
        packet with an explicit ``targets`` dict is sent.  The JS tick-
        pipeline can then apply the correct morphs without needing to look up
        the expression catalogue client-side.

        When *targets* is ``None`` the bare *name* and *intensity* are
        forwarded as a backward-compatible fallback.
        """
        if not self._has_any_transport():
            return
        payload: Dict[str, Any]
        if not name:
            payload = {"type": "vrm_expression_clear"}
        elif targets is not None:
            payload = {"type": "vrm_expression_set", "targets": targets}
        else:
            payload = {
                "type": "vrm_expression_set",
                "name": name,
                "intensity": intensity,
            }
        for transport in self._transports:
            try:
                await transport.broadcast_expression(payload)
            except Exception as exc:  # pragma: no cover
                log_warning(
                    f"[KaradaStateServer] failed to push expression via {type(transport).__name__}: {exc}"
                )

    def has_connected_clients(self) -> bool:
        """Return True if any clients are connected via any transport."""
        for transport in self._transports:
            if transport.has_connected_clients():
                return True
        return False

    @staticmethod
    def _is_overlay_state(state: AnimationState | str | None) -> bool:
        """Return True when a state is a temporary overlay over another state."""
        if state is None:
            return False
        name = state.value if isinstance(state, AnimationState) else str(state)
        return name == AnimationState.TOUCH.value

    def _remember_active_context(
        self,
        context_id: str,
        state: AnimationState,
        session_id: Optional[str],
        loop: bool,
        priority: Optional[int],
        source: Optional[str],
        animation_file: Optional[str] = None,
        resume_section: Optional[str] = None,
    ) -> None:
        """Persist enough metadata to restore a covered context later."""
        self._context_sequence += 1
        self._active_context_meta[context_id] = {
            "state": state,
            "session_id": session_id,
            "loop": bool(loop),
            "priority": int(priority) if isinstance(priority, int) else None,
            "source": source,
            "animation_file": animation_file,
            "resume_section": resume_section,
            "sequence": self._context_sequence,
        }

    def _forget_active_context(self, context_id: Optional[str]) -> None:
        """Drop saved metadata for a context that is no longer active."""
        if not context_id:
            return
        self._active_context_meta.pop(context_id, None)

    def _select_restore_context_locked(
        self, excluded_context_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """Pick the highest-priority remaining context, preferring the most recent."""
        best: Optional[Dict[str, Any]] = None
        for context_id, meta in self._active_context_meta.items():
            if context_id == excluded_context_id:
                continue
            if context_id not in self._active_tasks:
                continue
            priority = self._active_tasks.get(context_id)
            if not isinstance(priority, int):
                priority = meta.get("priority")
            sequence = meta.get("sequence", 0)
            if not isinstance(priority, int):
                priority = 0
            if (
                best is None
                or priority > best["priority"]
                or (priority == best["priority"] and sequence > best["sequence"])
            ):
                best = {
                    "context_id": context_id,
                    "state": meta.get("state"),
                    "session_id": meta.get("session_id"),
                    "loop": bool(meta.get("loop", True)),
                    "priority": priority,
                    "source": meta.get("source"),
                    "animation_file": meta.get("animation_file"),
                    "resume_section": meta.get("resume_section"),
                    "sequence": sequence,
                }
        return best

    async def _resume_context_or_idle(
        self,
        session_id: Optional[str],
        excluded_context_id: Optional[str] = None,
    ) -> None:
        """Resume the highest-priority remaining context, or fall back to idle."""
        async with self._lock:
            restore = self._select_restore_context_locked(excluded_context_id)

        if restore and isinstance(restore.get("state"), AnimationState):
            await self.play_animation(
                restore["state"],
                session_id=restore.get("session_id")
                if restore.get("session_id") is not None
                else session_id,
                loop=bool(restore.get("loop", True)),
                context_id=restore.get("context_id"),
                priority=restore.get("priority"),
                source=restore.get("source"),
                animation_file=restore.get("animation_file"),
                resume_section=restore.get("resume_section"),
            )
            return

        await self.play_animation(
            AnimationState.IDLE,
            session_id=session_id,
            loop=True,
            context_id=None,
        )

    async def ensure_idle_preloaded(self, session_id: Optional[str] = None) -> None:
        """Pre-load IDLE animation in advance to ensure smooth fallback.

        This is called when a session connects or when starting a non-idle animation,
        ensuring that IDLE is always ready to play when animations stop.

        Args:
            session_id: The WebUI session ID (or None for broadcast)
        """
        try:
            if not self._has_any_transport():
                return

            # Get IDLE animation variants
            idle_variants = self.get_animation_variants(AnimationState.IDLE.value)
            idle_animations = idle_variants.get("loop", []) or idle_variants.get(
                "other", []
            )

            if not idle_animations:
                idle_animations = self.get_animations_for_state(AnimationState.IDLE)

            if not idle_animations:
                log_debug("[KaradaStateServer] No IDLE animations available to preload")
                return

            # Pre-load IDLE variants using the *idle* folder, regardless of current_state.
            for anim in idle_animations[:3]:  # Pre-load up to 3 idle variants
                try:
                    await self._preload_animation(
                        session_id=session_id,
                        animation_file=anim,
                        state_folder=AnimationState.IDLE.value,
                    )
                except Exception as exc:
                    log_debug(
                        f"[KaradaStateServer] Failed to preload IDLE variant {anim}: {exc}"
                    )
        except Exception as exc:
            log_warning(f"[KaradaStateServer] ensure_idle_preloaded failed: {exc}")

    def register_animation_state_changed_callback(
        self, callback: AnimationStateChangedCallback
    ) -> None:
        """Register a callback to be called when animation state changes.

        The callback will be called with (state, animation_file, descriptor) as arguments.

        Args:
            callback: Async function to call when animation changes
        """
        self._animation_state_changed_callbacks.append(callback)
        log_debug("[KaradaStateServer] Registered animation state changed callback")

    async def _notify_animation_state_changed(
        self,
        state: AnimationState,
        animation_file: str,
        descriptor: Optional[Dict[str, Any]],
    ) -> None:
        """Notify all callbacks that animation state has changed.

        Args:
            state: The new animation state
            animation_file: The animation file name
            descriptor: The animation descriptor
        """
        log_debug(
            f"[KaradaStateServer] _notify_animation_state_changed CALLED: state={state.value}, animation={animation_file}, callbacks_count={len(self._animation_state_changed_callbacks)}"
        )
        for callback in self._animation_state_changed_callbacks:
            try:
                log_debug(
                    f"[KaradaStateServer] Calling callback: {callback.__name__ if hasattr(callback, '__name__') else 'unknown'}"
                )
                if asyncio.iscoroutinefunction(callback):
                    await callback(state, animation_file, descriptor)
                else:
                    callback(state, animation_file, descriptor)
            except Exception as exc:
                log_warning(
                    f"[KaradaStateServer] Error in animation state callback: {exc}"
                )

    def get_current_animation_state(self) -> Dict[str, Any]:
        """Get the current centralized animation state.

        Returns:
            Dict with 'state', 'animation_file', and 'descriptor' keys
        """
        # Minimal informative structure; richer payload is assembled when sending commands
        anim = self._current_animation_file
        desc = self._current_animation_descriptor
        try:
            resolved, _ = (
                self._resolve_animation_descriptor(anim) if anim else (None, None)
            )
        except Exception:
            resolved = anim

        started_at = self._current_animation_started_at
        timing = {
            "started_at": started_at.isoformat() if started_at else None,
            "time_in_clip": 0.0,
            "current_frame": 0,
        }

        clip = None
        phase = self._current_animation_phase or "loop"
        frame_range: Optional[Dict[str, int]] = self._current_animation_frame_range
        try:
            if isinstance(desc, dict):
                fps = (
                    desc.get("fps") if isinstance(desc.get("fps"), (int, float)) else 30
                )
                duration = None
                # Prefer the canonical tracked phase for timing calculations, fall back to loop/intros/outros.
                section = None
                try:
                    if phase in {"intro", "loop", "outro"}:
                        section = (
                            desc.get(phase)
                            if isinstance(desc.get(phase), dict)
                            else None
                        )
                    if not section:
                        section = (
                            desc.get("loop")
                            if isinstance(desc.get("loop"), dict)
                            else None
                        )
                    if not section:
                        section = (
                            desc.get("intro")
                            if isinstance(desc.get("intro"), dict)
                            else None
                        )
                    if not section:
                        section = (
                            desc.get("outro")
                            if isinstance(desc.get("outro"), dict)
                            else None
                        )
                    if (
                        phase not in {"intro", "loop", "outro"}
                        and desc.get("play_once")
                        and not isinstance(desc.get("loop"), dict)
                    ):
                        phase = "clip"
                    if frame_range is None and (
                        section
                        and isinstance(section.get("start_frame"), (int, float))
                        and isinstance(section.get("end_frame"), (int, float))
                    ):
                        frame_range = {
                            "start_frame": int(section.get("start_frame")),
                            "end_frame": int(section.get("end_frame")),
                        }
                        frames = max(
                            0.0,
                            float(section.get("end_frame"))
                            - float(section.get("start_frame")),
                        )
                        duration = frames / float(fps) if fps else None
                except Exception:
                    duration = None

                try:
                    clip = {
                        "name": Path(resolved).stem
                        if isinstance(resolved, str) and resolved
                        else (Path(anim).stem if anim else None),
                        "duration": duration,
                        "fps": float(fps) if fps else 30,
                    }
                except Exception:
                    clip = None

                # If we have a start timestamp, compute elapsed time and current_frame
                try:
                    if started_at and isinstance(started_at, datetime):
                        elapsed = max(
                            0.0,
                            (
                                datetime.now(tz=timezone.utc) - started_at
                            ).total_seconds(),
                        )
                        timing["time_in_clip"] = elapsed
                        if clip and clip.get("fps"):
                            fps_val = float(clip.get("fps") or 30)
                            if (
                                section
                                and isinstance(section.get("start_frame"), (int, float))
                                and isinstance(section.get("end_frame"), (int, float))
                            ):
                                start = float(section.get("start_frame"))
                                end = float(section.get("end_frame"))
                                span = max(1.0, end - start)
                                tframes = int(elapsed * fps_val)
                                if phase == "loop":
                                    current_frame = int(start + (tframes % span))
                                else:
                                    current_frame = int(start + min(span, tframes))
                                timing["current_frame"] = current_frame
                            else:
                                timing["current_frame"] = int(elapsed * fps_val)
                except Exception:
                    # best-effort; keep defaults on failure
                    pass
        except Exception:
            clip = None

        state = {
            "state": self.current_state.value,
            "animation_file": anim,
            "animation": resolved,
            "descriptor": desc,
            "play_section": phase if self._current_phase_authoritative else None,
            "frame_range": frame_range,
            "phase_authoritative": self._current_phase_authoritative,
            "animation_id": self._current_animation_id,
            "animation_state": {
                "action": self.current_state.value,
                "phase": phase,
                "phase_authoritative": self._current_phase_authoritative,
                "animation": resolved,
                "descriptor": desc,
                "timing": timing,
                "frame_range": frame_range,
                "expressions": desc.get("expressions")
                if isinstance(desc, dict)
                else None,
                "blink": desc.get("blink") if isinstance(desc, dict) else None,
                "eye_movement": desc.get("eye_movement")
                if isinstance(desc, dict)
                else None,
                "emotions": None,
                "lipsync": (
                    desc.get("lipsync")
                    if isinstance(desc, dict) and "lipsync" in desc
                    else False
                ),
            },
        }

        return state

    async def get_full_state(self) -> Dict[str, Any]:
        """Return the complete VRM state for clients on WebSocket connect.

        Same keys as before, but this version awaits the emotion manager.
        """
        # VRM model: prefer explicitly stored state, fall back to reading from webui
        vrm_model: Dict[str, Any] = {}
        if self._vrm_model_url:
            vrm_model = {
                "name": self._vrm_model_name,
                "url": self._vrm_model_url,
                "hash": self._vrm_model_hash,
            }
        else:
            try:
                if self.webui and getattr(self.webui, "active_vrm", None):
                    active_vrm: str = self.webui.active_vrm  # type: ignore[attr-defined]
                    if active_vrm.startswith("/"):
                        url = active_vrm
                        name = active_vrm.split("/")[-1]
                    else:
                        vrm_dir = getattr(self.webui, "vrm_dir", None)
                        if vrm_dir:
                            vrm_path = Path(vrm_dir) / active_vrm
                            try:
                                root = Path(__file__).resolve().parent.parent
                                url = f"/{vrm_path.relative_to(root).as_posix()}"
                            except Exception:
                                url = f"/avatars/{active_vrm}"
                        else:
                            url = f"/avatars/{active_vrm}"
                        name = active_vrm
                    vrm_model = {"name": name, "url": url, "hash": None}
            except Exception:
                vrm_model = {}

        animation: Dict[str, Any] = {}
        anim_file = self._current_animation_file
        if anim_file:
            try:
                resolved, desc = self._resolve_animation_descriptor(anim_file)
            except Exception:
                resolved = anim_file
                desc = self._current_animation_descriptor
            animation = {
                "file": anim_file,
                "url": resolved,
                "state": self.current_state.value,
                "loop": True,
                "descriptor": desc or self._current_animation_descriptor,
                "animation_id": self._current_animation_id,
            }

        face_values: Dict[str, float] = dict(self._current_face_values)
        if not self._face_values_initialized:
            try:
                from core.core_initializer import PLUGIN_REGISTRY

                mgr = (
                    PLUGIN_REGISTRY.get("emotion_manager")
                    if isinstance(PLUGIN_REGISTRY, dict)
                    else None
                )
                if mgr is not None and hasattr(mgr, "get_emotion_state"):
                    raw = await mgr.get_emotion_state()  # awaitable now
                    if isinstance(raw, dict):
                        normalized: Dict[str, float] = {}
                        for key, raw_value in raw.items():
                            name = str(key).strip()
                            if not name:
                                continue
                            try:
                                value = float(raw_value)
                            except (TypeError, ValueError):
                                continue
                            if value > 1.0:
                                value = value / 10.0
                            value = max(0.0, min(1.0, value))
                            if value > 0.0001:
                                normalized[name] = value
                        self._current_face_values = normalized
                        face_values = dict(normalized)
                self._face_values_initialized = True
            except Exception:
                face_values = {}
                self._current_face_values = {}
                self._face_values_initialized = True

        return {
            "vrm_model": vrm_model,
            "animation": animation,
            "face_values": face_values,
            "audio": self.get_current_audio(),
        }

    async def set_vrm_model(
        self,
        url: str,
        name: str,
        hash_: Optional[str] = None,
    ) -> None:
        """Store the active VRM model info and broadcast a ``vrm_model`` message to all clients.

        Args:
            url: Web-accessible URL for the VRM file (e.g. ``/avatars/SyntH.vrm``).
            name: Human-readable model name / filename.
            hash_: Optional content hash for client-cache validation.
        """
        self._vrm_model_url = url
        self._vrm_model_name = name
        self._vrm_model_hash = hash_
        log_debug(f"[KaradaStateServer] VRM model state updated: {name} -> {url}")

        if not self._has_any_transport():
            return

        payload: Dict[str, Any] = {"type": "vrm_model", "name": name, "url": url}
        if hash_ is not None:
            payload["hash"] = hash_

        for transport in self._transports:
            try:
                await transport.broadcast_model(payload)
                log_debug(
                    f"[KaradaStateServer] Broadcast vrm_model via {type(transport).__name__}"
                )
            except Exception as exc:
                log_warning(
                    f"[KaradaStateServer] Failed to broadcast vrm_model via "
                    f"{type(transport).__name__}: {exc}"
                )

    def get_missing_assets(
        self,
        has_assets: List[str],
    ) -> List[str]:
        """Return server-known asset URLs that the client reports it does not already have.

        Args:
            has_assets: List of asset URLs/identifiers the client already has cached.

        Returns:
            List of asset URLs the client is missing.
        """
        missing: List[str] = []
        # Check the active VRM model
        if self._vrm_model_url and self._vrm_model_url not in has_assets:
            missing.append(self._vrm_model_url)
        return missing

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
            import sys

            persona_manager = None
            pm_mod = sys.modules.get("core.persona_manager")
            if pm_mod and hasattr(pm_mod, "get_persona_manager"):
                persona_manager = pm_mod.get_persona_manager()
            active_persona_folder = None
            if (
                persona_manager
                and hasattr(persona_manager, "_current_persona")
                and persona_manager._current_persona
            ):
                active_persona_folder = getattr(
                    persona_manager._current_persona, "id", None
                ) or getattr(persona_manager._current_persona, "name", None)
        except Exception:
            active_persona_folder = None

        # Candidate skin folders to check
        candidates = []
        if active_persona_folder:
            candidates.append(active_persona_folder)
        candidates.append("Rei")  # Fallback to Rei

        # Scan each candidate skin
        for skin_name in candidates:
            skin_anim_dir = self.SKINS_DIR / skin_name / "animations" / state.value
            if skin_anim_dir.exists() and skin_anim_dir.is_dir():
                try:
                    for fbx_file in skin_anim_dir.glob("*.fbx"):
                        animations.append(fbx_file.name)
                except Exception as exc:
                    log_warning(
                        f"[KaradaStateServer] Error scanning animations in {skin_anim_dir}: {exc}"
                    )

        # Remove duplicates while preserving order
        seen = set()
        unique_animations = []
        for anim in animations:
            if anim not in seen:
                seen.add(anim)
                unique_animations.append(anim)

        return unique_animations

    def set_animation_search_paths(self, paths: List[Path | str]) -> None:
        """Set additional search paths (ordered) to resolve animation files.

        These are checked after the active persona skin and before the Rei fallback.
        """
        self._search_paths = [Path(path) for path in paths]
        log_debug(
            f"[KaradaStateServer] Animation search paths set: {self._search_paths}"
        )

    def add_temporary_search_path(self, path: Path) -> None:
        """Add a temporary search path with high priority (prepended)."""
        try:
            p = Path(path)
        except Exception:
            return
        if p not in self._search_paths:
            self._search_paths.insert(0, p)
        if p not in self._temporary_search_paths:
            self._temporary_search_paths.append(p)
        log_debug(f"[KaradaStateServer] Added temporary search path: {p}")

    def remove_temporary_search_path(self, path: Path) -> None:
        """Remove a temporary search path if present."""
        try:
            p = Path(path)
        except Exception:
            return
        self._search_paths = [sp for sp in self._search_paths if sp != p]
        self._temporary_search_paths = [
            sp for sp in self._temporary_search_paths if sp != p
        ]
        log_debug(f"[KaradaStateServer] Removed temporary search path: {p}")

    def get_animation_search_paths(self) -> List[Path]:
        """Return a copy of the configured search paths."""
        return list(self._search_paths)

    def _build_search_url_prefix(self, root: Path) -> str:
        """Build a URL prefix for a search path.

        If the search path is under the skins directory, expose it as /skins/<relative>.
        Otherwise, fall back to /animations.
        """
        try:
            rel = root.relative_to(self.SKINS_DIR)
            return f"/skins/{rel.as_posix()}"
        except Exception:
            return f"/{self.ANIMATIONS_BASE_PATH}"

    def register_state_animations(
        self, state: str, animations: Dict[str, List[str]], sequential: bool = False
    ) -> None:
        """Register override animations for a logical state.

        animations should be a dict with optional keys: 'loop', 'post', 'other'.
        """
        key = state.lower()
        self._registered_state_animations[key] = animations
        if sequential:
            self._sequential_states.add(key)
        log_debug(
            f"[KaradaStateServer] Registered override animations for state {key}: {animations}"
        )

    def register_temporary_state_override(
        self,
        upload_id: str,
        state: str,
        animations: List[str],
        sequential: bool = False,
    ) -> None:
        """Register a temporary override list for a state (used by uploads).

        This helper mirrors register_state_animations but tags the source in logs.
        """
        if not animations:
            return
        payload = {"loop": list(animations)}
        self.register_state_animations(state, payload, sequential=sequential)
        log_debug(
            f"[KaradaStateServer] Registered temporary override for upload={upload_id} state={state}: {animations}"
        )

    def register_state_aliases(self, aliases: Dict[str, List[str]]) -> None:
        """Register alias names for canonical states (e.g. THINK -> ['thinking','ponder'])."""
        for k, v in aliases.items():
            self._state_aliases[k.lower()] = [a.lower() for a in v]
        log_debug(
            f"[KaradaStateServer] Registered state aliases: {self._state_aliases}"
        )

    def _build_search_paths_for_state(self, state_name: str) -> List[Path]:
        """Return ordered list of paths to search for animations for a state."""
        paths: List[Path] = []
        # Active persona path
        try:
            import sys

            pm = None
            pm_mod = sys.modules.get("core.persona_manager")
            if pm_mod and hasattr(pm_mod, "get_persona_manager"):
                pm = pm_mod.get_persona_manager()
            active_persona_folder = None
            if pm and hasattr(pm, "_current_persona") and pm._current_persona:
                active_persona_folder = getattr(
                    pm._current_persona, "id", None
                ) or getattr(pm._current_persona, "name", None)
        except Exception:
            active_persona_folder = None

        if active_persona_folder:
            persona_state_dir = (
                self.SKINS_DIR / str(active_persona_folder) / "animations" / state_name
            )
            paths.append(persona_state_dir)

        # Additional configured search paths (state subfolder)
        for p in self._search_paths:
            candidate = Path(p) / state_name
            paths.append(candidate)

        # Rei fallback
        paths.append(self.SKIN_DEFAULT_ANIMATIONS_DIR / state_name)

        # Also include root animations folders (no state subfolder) as fallback
        if active_persona_folder:
            paths.append(self.SKINS_DIR / str(active_persona_folder) / "animations")
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

        def _load_local_descriptor(fbx_path: Path) -> Optional[Dict]:
            try:
                descriptor_path = fbx_path.with_suffix(fbx_path.suffix + ".json")
                if not descriptor_path.exists() or not descriptor_path.is_file():
                    return None
                with descriptor_path.open("r", encoding="utf-8") as df:
                    return json.load(df)
            except Exception:
                return None

        def _classify_and_add(fbx_path: Path) -> None:
            try:
                # For discovery/classification, read descriptor next to the FBX file.
                # This allows classification to work with custom search paths too.
                desc = _load_local_descriptor(fbx_path)
                structure = self._analyze_animation_structure(desc, fbx_path.name)

                # Classification rules:
                # - play_once => post
                # - has_loop => loop (even if it also has outro; outro is a section)
                # - has_outro without loop => post
                # - otherwise => loop
                if desc and desc.get("play_once"):
                    variants["post"].append(fbx_path.name)
                elif structure.get("has_loop"):
                    variants["loop"].append(fbx_path.name)
                elif structure.get("has_outro") and not structure.get("has_loop"):
                    variants["post"].append(fbx_path.name)
                else:
                    variants["loop"].append(fbx_path.name)
            except Exception:
                # If descriptor parsing fails, treat as loopable by default
                variants["loop"].append(fbx_path.name)

        # 2) Resolve folder variants by scanning ONLY <dir>/<state>/ (and Rei fallback)
        found_any = False
        active_persona_folder = None
        try:
            # Avoid importing persona_manager here: it can initialize DB during tests.
            import sys

            pm_mod = sys.modules.get("core.persona_manager")
            if pm_mod and hasattr(pm_mod, "get_persona_manager"):
                pm = pm_mod.get_persona_manager()
                if pm and hasattr(pm, "_current_persona") and pm._current_persona:
                    active_persona_folder = getattr(
                        pm._current_persona, "id", None
                    ) or getattr(pm._current_persona, "name", None)
        except Exception:
            active_persona_folder = None

        state_dirs: List[Path] = []
        if active_persona_folder:
            state_dirs.append(
                self.SKINS_DIR / str(active_persona_folder) / "animations" / key
            )
        for p in self._search_paths:
            state_dirs.append(Path(p) / key)
        state_dirs.append(self.SKIN_DEFAULT_ANIMATIONS_DIR / key)

        for sd in state_dirs:
            if not (sd.exists() and sd.is_dir()):
                continue
            try:
                found_in_dir = False
                for f in sd.glob("*.fbx"):
                    found_any = True
                    found_in_dir = True
                    _classify_and_add(f)
                # Precedence: once we found variants in a higher-priority directory,
                # do not mix in fallback directories.
                if found_in_dir:
                    break
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
        # Use the current state if available to find descriptor in the right subdirectory.
        state_folder = self.current_state.value if self.current_state else None
        return self._resolve_animation_descriptor_for_state(
            animation_file, state_folder
        )

    def _resolve_animation_descriptor_for_state(
        self, animation_file: str, state_folder: Optional[str] = None
    ):
        """Resolve animation file path and descriptor, knowing the state folder.

        Animations are organized by state: skins/Rei/animations/think/, skins/Rei/animations/write/, etc.
        This method searches for the animation in the correct state folder.

        Args:
            animation_file: The animation file name (e.g., "Thinking.fbx")
            state_folder: The state folder name (e.g., "think", "write"). If None, searches root animations.

        Returns:
            Tuple of (resolved_url_path, descriptor) where descriptor may be None
        """
        descriptor: Optional[Dict[str, Any]] = None
        resolved_rel_path: Optional[str] = None

        # Detect current active persona (best-effort)
        active_persona_folder: Optional[str] = None
        try:
            from core.persona_manager import get_persona_manager

            pm = get_persona_manager()
            if pm and getattr(pm, "_current_persona", None):
                p = pm._current_persona
                active_persona_folder = getattr(p, "id", None) or getattr(
                    p, "name", None
                )
        except Exception:
            active_persona_folder = None

        search_roots: List[tuple[Path, str]] = []

        # Extra search paths (used by tests and plugin overrides)
        for p in self._search_paths or []:
            try:
                root = Path(p)
                url = self._build_search_url_prefix(root)
                if state_folder:
                    search_roots.append((root / state_folder, f"{url}/{state_folder}"))
                else:
                    search_roots.append((root, url))
            except Exception:
                continue

        # Active persona skin
        if active_persona_folder:
            base = self.SKINS_DIR / str(active_persona_folder) / "animations"
            if state_folder:
                search_roots.append(
                    (
                        base / state_folder,
                        f"/skins/{active_persona_folder}/animations/{state_folder}",
                    )
                )
            else:
                search_roots.append(
                    (base, f"/skins/{active_persona_folder}/animations")
                )

        # Default Rei fallback
        rei_base = self.SKINS_DIR / "Rei" / "animations"
        if state_folder:
            search_roots.append(
                (rei_base / state_folder, f"/skins/Rei/animations/{state_folder}")
            )
        else:
            search_roots.append((rei_base, "/skins/Rei/animations"))

        def _try_load_descriptor(fpath: Path) -> Optional[Dict[str, Any]]:
            dpath = fpath.with_suffix(fpath.suffix + ".json")
            if not dpath.exists():
                return None
            try:
                with dpath.open("r", encoding="utf-8") as df:
                    return json.load(df)
            except Exception as e:
                log_debug(
                    f"[KaradaStateServer] Failed to load descriptor for {fpath.name}: {e}"
                )
                return None

        # Search
        for dir_path, url_prefix in search_roots:
            try:
                if not (dir_path.exists() and dir_path.is_dir()):
                    continue

                # Exact match first
                candidate = dir_path / animation_file
                if candidate.exists() and candidate.is_file():
                    resolved_rel_path = f"{url_prefix}/{candidate.name}"
                    descriptor = _try_load_descriptor(candidate)
                    break

                # Case-insensitive match
                for p in dir_path.iterdir():
                    if p.is_file() and p.name.lower() == animation_file.lower():
                        resolved_rel_path = f"{url_prefix}/{p.name}"
                        descriptor = _try_load_descriptor(p)
                        break

                if resolved_rel_path:
                    break
            except Exception as e:
                log_debug(f"[KaradaStateServer] Error searching in {dir_path}: {e}")
                continue

        # Fallback URL if not found (descriptor None)
        if not resolved_rel_path:
            if state_folder:
                resolved_rel_path = (
                    f"/skins/Rei/animations/{state_folder}/{animation_file}"
                )
            else:
                resolved_rel_path = f"/skins/Rei/animations/{animation_file}"

        return resolved_rel_path, descriptor

    def _analyze_animation_structure(
        self, descriptor: Optional[Dict], animation_file: str = ""
    ) -> Dict[str, bool]:
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
            if (
                "start_frame" in descriptor["intro"]
                and "end_frame" in descriptor["intro"]
            ):
                result["has_intro"] = True
            else:
                log_warning(
                    f"[KaradaStateServer] Descriptor for '{animation_file}' has 'intro' but missing start_frame or end_frame - will treat as non-structured"
                )

        if "loop" in descriptor and isinstance(descriptor["loop"], dict):
            if (
                "start_frame" in descriptor["loop"]
                and "end_frame" in descriptor["loop"]
            ):
                result["has_loop"] = True

        if "outro" in descriptor and isinstance(descriptor["outro"], dict):
            if (
                "start_frame" in descriptor["outro"]
                and "end_frame" in descriptor["outro"]
            ):
                result["has_outro"] = True
            else:
                log_warning(
                    f"[KaradaStateServer] Descriptor for '{animation_file}' has 'outro' but missing start_frame or end_frame - will treat as non-structured"
                )

        # Validate play_once flag: it conflicts with intro/outro structure
        # (play_once means "play the whole animation once", but intro/outro define
        #  a structured animation that should execute its sections in order)
        if descriptor.get("play_once"):
            has_structured_sections = result["has_intro"] or result["has_outro"]
            if has_structured_sections:
                log_warning(
                    f"[KaradaStateServer] Animation '{animation_file}' has both 'play_once' flag "
                    f"and structured sections (intro/outro). 'play_once' will be ignored because "
                    f"intro/outro structure takes precedence. "
                    f"Structure: intro={result['has_intro']}, loop={result['has_loop']}, outro={result['has_outro']}"
                )

        return result

    def _sanitize_idle_descriptor(self, descriptor: Optional[Dict]) -> Optional[Dict]:
        """Return a safe descriptor subset for IDLE.

        IDLE must never clamp or behave like a play-once transition. We allow
        descriptors mainly to define loop frame ranges (and optionally intro),
        while intentionally dropping outro/play_once to keep idle stable.
        """
        if not descriptor or not isinstance(descriptor, dict):
            return None

        sanitized: Dict[str, Dict] = {}
        if isinstance(descriptor.get("intro"), dict):
            sanitized["intro"] = descriptor["intro"]
        if isinstance(descriptor.get("loop"), dict):
            sanitized["loop"] = descriptor["loop"]

        return sanitized or None

    def _is_server_authoritative_structure(
        self,
        state: AnimationState,
        structure: Dict[str, bool],
    ) -> bool:
        """Return True when the server should drive explicit phase progression."""
        return bool(
            state != AnimationState.IDLE
            and not self._is_overlay_state(state)
            and structure.get("has_intro")
            and structure.get("has_loop")
            and structure.get("has_outro")
        )

    def _get_phase_frame_range(
        self,
        descriptor: Optional[Dict[str, Any]],
        play_section: str,
    ) -> Optional[Dict[str, int]]:
        """Return the inclusive frame range for a descriptor section."""
        if not isinstance(descriptor, dict):
            return None
        section = descriptor.get(play_section)
        if not isinstance(section, dict):
            return None
        if not isinstance(section.get("start_frame"), (int, float)):
            return None
        if not isinstance(section.get("end_frame"), (int, float)):
            return None
        return {
            "start_frame": int(section.get("start_frame")),
            "end_frame": int(section.get("end_frame")),
        }

    def _get_phase_duration_seconds(
        self,
        descriptor: Optional[Dict[str, Any]],
        play_section: str,
    ) -> Optional[float]:
        """Return the wall-clock duration of a descriptor section."""
        frame_range = self._get_phase_frame_range(descriptor, play_section)
        if frame_range is None:
            return None
        fps = 30.0
        if isinstance(descriptor, dict) and isinstance(
            descriptor.get("fps"), (int, float)
        ):
            fps = float(descriptor.get("fps") or 30.0)
        if fps <= 0:
            fps = 30.0
        frames = max(
            0.0, float(frame_range["end_frame"]) - float(frame_range["start_frame"])
        )
        return frames / fps

    async def _cancel_phase_task(self) -> None:
        """Cancel any pending authoritative phase transition task."""
        task = self._phase_task
        self._phase_task = None
        self._phase_generation += 1
        if task is None or task is asyncio.current_task():
            return
        try:
            task.cancel()
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log_warning(f"[KaradaStateServer] Error cancelling phase task: {exc}")

    def _schedule_phase_task(
        self, delay_s: float, callback: Callable[[int], Any]
    ) -> int:
        """Schedule a callback tied to the current authoritative phase generation."""
        generation = self._phase_generation

        async def _runner() -> None:
            try:
                await asyncio.sleep(max(0.0, float(delay_s)))
                async with self._lock:
                    if generation != self._phase_generation:
                        return
                result = callback(generation)
                if asyncio.iscoroutine(result):
                    await result
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                log_warning(
                    f"[KaradaStateServer] Phase task error (generation={generation}): {exc}"
                )
            finally:
                if self._phase_task is asyncio.current_task():
                    self._phase_task = None

        self._phase_task = asyncio.get_running_loop().create_task(_runner())
        return generation

    def _commit_animation_phase(
        self,
        state: AnimationState,
        animation_file: str,
        descriptor: Optional[Dict[str, Any]],
        play_section: str,
        frame_range: Optional[Dict[str, int]],
        session_id: Optional[str],
        phase_authoritative: bool,
        context_id: Optional[str],
    ) -> None:
        """Persist the canonical animation phase exposed to all clients."""
        self.current_state = state
        self.current_animation = animation_file
        self._current_context_id = context_id
        self._current_animation_file = animation_file
        self._current_animation_descriptor = descriptor
        self._current_animation_phase = play_section
        self._current_animation_frame_range = frame_range
        self._current_phase_authoritative = phase_authoritative
        self._current_animation_session_id = session_id

    async def play_animation(
        self,
        state: AnimationState,
        session_id: Optional[str],
        loop: bool = True,
        context_id: Optional[str] = None,
        priority: Optional[int] = None,
        source: Optional[str] = None,
        animation_file: Optional[str] = None,
        resume_section: Optional[str] = None,
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
            source: Optional string describing the origin of the animation request (e.g. 'core', 'plugin').
                    This is informational and is forwarded to WebUI when provided.
            animation_file: Optional concrete animation file override to replay for this state.
            resume_section: Optional descriptor section to start from when resuming a covered context.
        """
        log_info(
            f"[KaradaStateServer] ⭐ play_animation CALLED: state={state.value}, session={session_id}, loop={loop}, context={context_id}, priority={priority}, source={source}",
            log_file="webui",
        )

        # Check if we need to play outro before transitioning to new animation
        # This must be done BEFORE acquiring the lock to avoid deadlocks.
        # We capture the previous animation/state/descriptor here so that the
        # outro is played for the CORRECT (old) animation, not the new one.
        outro_duration = 0
        needs_outro_transition = False
        prev_animation_for_outro: Optional[str] = None
        prev_state_for_outro: Optional[str] = None
        prev_descriptor_for_outro: Optional[Dict] = None
        incoming_is_overlay = self._is_overlay_state(state)

        try:
            if (
                self.current_state != state
                and self.current_animation
                and not incoming_is_overlay
                and not self._is_overlay_state(self.current_state)
                and not (
                    self._current_phase_authoritative
                    and self._current_animation_phase == "outro"
                )
            ):
                prev_animation_for_outro = self.current_animation
                prev_state_for_outro = self.current_state.value
                # Resolve descriptor using the OLD state folder explicitly
                _, current_descriptor = self._resolve_animation_descriptor_for_state(
                    self.current_animation, self.current_state.value
                )
                current_structure = self._analyze_animation_structure(
                    current_descriptor, self.current_animation
                )

                if current_structure["has_outro"]:
                    needs_outro_transition = True
                    prev_descriptor_for_outro = current_descriptor
                    log_debug(
                        f"[KaradaStateServer] Preparing transition from {self.current_state.value} "
                        f"to {state.value}: playing outro for {self.current_animation}"
                    )
        except Exception as exc:
            log_warning(
                f"[KaradaStateServer] Error checking outro during transition: {exc}"
            )

        async with self._lock:
            # Use state priority if not explicitly provided
            if priority is None:
                priority = self.get_state_priority(state)

            # --- Preemption check ---
            # If a higher-priority context is already active, reject this request
            # (IDLE is always overridable regardless of current priority).
            if state != AnimationState.IDLE and self._active_tasks:
                max_active = max(
                    (p for p in self._active_tasks.values() if p is not None),
                    default=0,
                )
                if max_active > priority:
                    log_debug(
                        f"[KaradaStateServer] Preemption: rejecting {state.value} "
                        f"(priority={priority}) because active priority={max_active}"
                    )
                    return

            # If we have a context_id, mark it as active with priority.
            # When context_id is None and state != IDLE, generate a synthetic
            # context so the watchdog doesn't kill the animation prematurely.
            _KARADA_AUTO_CTX = "__karada_auto"
            resolved_context_id: Optional[str] = None
            if context_id:
                # Clear any previous synthetic context when an explicit one arrives
                self._active_tasks.pop(_KARADA_AUTO_CTX, None)
                self._active_tasks[context_id] = priority
                resolved_context_id = context_id
            elif state != AnimationState.IDLE:
                self._active_tasks[_KARADA_AUTO_CTX] = priority
                resolved_context_id = _KARADA_AUTO_CTX
            else:
                # Transitioning to IDLE: clear the synthetic context
                self._active_tasks.pop(_KARADA_AUTO_CTX, None)
                self._forget_active_context(_KARADA_AUTO_CTX)
                resolved_context_id = None

            # Select animation file
            # Prefer variants discovered via descriptors and overrides
            resolved_state = state
            variants = self.get_animation_variants(state.value)
            if state == AnimationState.IDLE:
                # Never pick `post` variants for idle: they are often play-once/clamped.
                animations = variants.get("loop", []) or variants.get("other", [])
            else:
                animations = (
                    variants.get("loop", [])
                    or variants.get("post", [])
                    or variants.get("other", [])
                )
            if not animations:
                # Fallback to idle if no animations found for this state
                resolved_state = AnimationState.IDLE
                idle_variants = self.get_animation_variants(AnimationState.IDLE.value)
                animations = idle_variants.get("loop", []) or idle_variants.get(
                    "other", []
                )
                if not animations:
                    animations = self.get_animations_for_state(AnimationState.IDLE)
            if not animations:
                log_warning(
                    f"[KaradaStateServer] No animations found for state {state.value}, skipping"
                )
                return

            # Select animation based on rotation mode
            if animation_file:
                selected_animation = animation_file
            elif (
                resolved_state.value in self._sequential_states and len(animations) > 1
            ):
                # Sequential mode: use current index or start from 0
                current_index = self._sequence_indices.get(resolved_state.value, -1)
                next_index = (current_index + 1) % len(animations)
                selected_animation = animations[next_index]
                self._sequence_indices[resolved_state.value] = next_index
            else:
                # Random mode or single animation
                selected_animation = random.choice(animations)

            log_debug(
                f"[KaradaStateServer] Playing {resolved_state.value} animation: {selected_animation} "
                f"(requested={state.value}, loop={loop}, session={session_id}, "
                f"context={context_id}, priority={priority})"
            )

            # Send animation command to clients
            if self._has_any_transport():
                # Resolve descriptor for intelligent section handling
                resolved_path, descriptor = (
                    self._resolve_animation_descriptor_for_state(
                        selected_animation, resolved_state.value
                    )
                )
                structure = self._analyze_animation_structure(
                    descriptor, selected_animation
                )

                log_debug(
                    f"[KaradaStateServer] Resolved animation {selected_animation}: "
                    f"descriptor={'found' if descriptor else 'NOT FOUND'}, "
                    f"structure=(intro:{structure['has_intro']}, loop:{structure['has_loop']}, outro:{structure['has_outro']})"
                )

                is_idle_state = resolved_state == AnimationState.IDLE

                # Determine effective loop behavior based on descriptor structure:
                # 1. If has intro/outro (structured animation): loop=True if has loop section, else play once
                # 2. If only loop (no intro/outro) + play_once: play loop once only (don't really loop)
                # 3. Otherwise: use provided loop parameter
                has_intro_or_outro = structure["has_intro"] or structure["has_outro"]

                if is_idle_state:
                    # IDLE is always looped; descriptor (if any) is used only to define a safe loop range.
                    effective_loop = True
                    start_rotation = len(animations) > 1
                elif has_intro_or_outro:
                    # Structured animation (intro/outro present)
                    # play_once flag is ignored (warning already logged in _analyze_animation_structure)
                    if structure["has_loop"]:
                        # intro/loop/outro or intro/outro - loop the middle section
                        effective_loop = True
                    else:
                        # intro only or intro/outro (no loop) - play once
                        effective_loop = False
                    start_rotation = False
                elif (
                    structure["has_loop"] and descriptor and descriptor.get("play_once")
                ):
                    # Only loop section (no intro/outro) with play_once flag
                    # Loop plays once only - don't really loop, don't rotate
                    log_debug(
                        f"[KaradaStateServer] Animation '{selected_animation}' has loop section "
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
                elif not descriptor:
                    # IMPLICIT DESCRIPTOR:
                    # If no descriptor exists, we cannot infer intro/loop/outro. Respect the requested
                    # loop parameter so core states (THINK/WRITE/TALK) can remain stable (loop=True)
                    # and avoid ending/clamping into T-pose.
                    log_debug(
                        f"[KaradaStateServer] Animation '{selected_animation}' has NO descriptor: "
                        f"respecting requested loop={loop} for non-idle states"
                    )
                    effective_loop = bool(loop)
                    start_rotation = False
                else:
                    # Has descriptor but no special flags
                    # Use provided loop parameter
                    effective_loop = loop
                    start_rotation = len(animations) > 1

                log_debug(
                    f"[KaradaStateServer] Animation structure - intro: {structure['has_intro']}, "
                    f"loop: {structure['has_loop']}, outro: {structure['has_outro']}, "
                    f"play_once: {descriptor.get('play_once') if descriptor else False}, "
                    f"effective_loop: {effective_loop}"
                )

                authoritative_structure = (
                    self._is_server_authoritative_structure(
                        resolved_state,
                        structure,
                    )
                    if isinstance(descriptor, dict)
                    else False
                )
                play_section_override: Optional[str] = None
                if resume_section in {"intro", "loop", "outro"} and isinstance(
                    descriptor, dict
                ):
                    requested_section = descriptor.get(resume_section)
                    if isinstance(requested_section, dict):
                        play_section_override = resume_section
                elif authoritative_structure:
                    play_section_override = "intro"

                if resolved_context_id:
                    self._remember_active_context(
                        resolved_context_id,
                        resolved_state,
                        session_id,
                        effective_loop,
                        priority,
                        source,
                        animation_file=selected_animation,
                        resume_section="loop" if structure["has_loop"] else None,
                    )

                # CRITICAL: Pre-load animation before sending play command
                # This ensures the client has the FBX/VRM data loaded before playback starts,
                # preventing T-pose due to missing animation data.
                # session_id can be None for broadcast; _preload_animation handles that.
                preload_ok = True
                try:
                    if self._has_any_transport():
                        # For non-idle animations, pre-load IDLE first to ensure smooth fallback
                        if resolved_state != AnimationState.IDLE:
                            await self.ensure_idle_preloaded(session_id=session_id)

                        # Then pre-load the requested animation
                        await self._preload_animation(
                            session_id=session_id,
                            animation_file=selected_animation,
                            state_folder=resolved_state.value,
                        )
                except Exception as exc:
                    log_warning(
                        f"[KaradaStateServer] Preload failed for {selected_animation}: {exc}. "
                        f"Falling back to IDLE to prevent T-pose."
                    )
                    preload_ok = False

                if not preload_ok and resolved_state != AnimationState.IDLE:
                    # Preload failed — do NOT play the animation (would T-pose).
                    # Fall back to IDLE instead.
                    log_warning(
                        f"[KaradaStateServer] Skipping {resolved_state.value} animation due to preload failure"
                    )
                    if context_id:
                        self._active_tasks.pop(context_id, None)
                        self._forget_active_context(context_id)
                    return

                committed_descriptor = (
                    self._sanitize_idle_descriptor(descriptor)
                    if is_idle_state
                    else descriptor
                )

                # If we need to play an outgoing outro first, do that and only start the
                # next animation after the outro duration has elapsed.
                if (
                    needs_outro_transition
                    and self._has_any_transport()
                    and prev_animation_for_outro
                ):
                    log_debug(
                        f"[KaradaStateServer] Sending outro command for {prev_animation_for_outro} "
                        f"before transitioning to {state.value}"
                    )
                    await self._cancel_phase_task()
                    await self._send_animation_command(
                        session_id=session_id,
                        animation_file=prev_animation_for_outro,
                        loop=False,
                        state=prev_state_for_outro or self.current_state.value,
                        descriptor=prev_descriptor_for_outro,
                        play_section="outro",
                        priority=priority,
                        source=source,
                        state_for_resolution=prev_state_for_outro,
                    )
                    prev_outro_range = self._get_phase_frame_range(
                        prev_descriptor_for_outro,
                        "outro",
                    )
                    try:
                        prev_state_enum = AnimationState(prev_state_for_outro)
                    except Exception:
                        prev_state_enum = self.current_state
                    self._commit_animation_phase(
                        prev_state_enum,
                        prev_animation_for_outro,
                        prev_descriptor_for_outro,
                        "outro",
                        prev_outro_range,
                        session_id,
                        True,
                        self._current_context_id,
                    )
                    await self._notify_animation_state_changed(
                        prev_state_enum,
                        prev_animation_for_outro,
                        prev_descriptor_for_outro,
                    )
                    outro_duration = (
                        self._get_phase_duration_seconds(
                            prev_descriptor_for_outro, "outro"
                        )
                        or 0.3
                    )
                    log_debug(
                        f"[KaradaStateServer] Outro duration: {outro_duration:.2f}s"
                    )

                    async def _start_after_outro(_: int) -> None:
                        await self.play_animation(
                            resolved_state,
                            session_id=session_id,
                            loop=loop,
                            context_id=context_id,
                            priority=priority,
                            source=source,
                            animation_file=selected_animation,
                            resume_section=resume_section,
                        )

                    self._schedule_phase_task(outro_duration, _start_after_outro)
                    return

                if authoritative_structure:
                    await self._cancel_phase_task()
                    authoritative_section = play_section_override or "intro"
                    phase_range = self._get_phase_frame_range(
                        committed_descriptor,
                        authoritative_section,
                    )
                    await self._send_animation_command(
                        session_id=session_id,
                        animation_file=selected_animation,
                        loop=authoritative_section == "loop",
                        state=resolved_state.value,
                        descriptor=committed_descriptor,
                        play_section=authoritative_section,
                        priority=priority,
                        source=source,
                        state_for_resolution=resolved_state.value,
                    )
                    self._commit_animation_phase(
                        resolved_state,
                        selected_animation,
                        committed_descriptor,
                        authoritative_section,
                        phase_range,
                        session_id,
                        True,
                        resolved_context_id,
                    )
                    await self._notify_animation_state_changed(
                        resolved_state,
                        selected_animation,
                        committed_descriptor,
                    )

                    if authoritative_section == "intro":
                        intro_duration = (
                            self._get_phase_duration_seconds(
                                committed_descriptor, "intro"
                            )
                            or 0.0
                        )

                        async def _advance_to_loop(generation: int) -> None:
                            async with self._lock:
                                if generation != self._phase_generation:
                                    return
                                if (
                                    self.current_state != resolved_state
                                    or self.current_animation != selected_animation
                                    or self._current_animation_phase != "intro"
                                ):
                                    return
                            loop_range = self._get_phase_frame_range(
                                committed_descriptor,
                                "loop",
                            )
                            await self._send_animation_command(
                                session_id=session_id,
                                animation_file=selected_animation,
                                loop=True,
                                state=resolved_state.value,
                                descriptor=committed_descriptor,
                                play_section="loop",
                                priority=priority,
                                source=source,
                                state_for_resolution=resolved_state.value,
                            )
                            async with self._lock:
                                if generation != self._phase_generation:
                                    return
                                self._commit_animation_phase(
                                    resolved_state,
                                    selected_animation,
                                    committed_descriptor,
                                    "loop",
                                    loop_range,
                                    session_id,
                                    True,
                                    resolved_context_id,
                                )
                            await self._notify_animation_state_changed(
                                resolved_state,
                                selected_animation,
                                committed_descriptor,
                            )

                        self._schedule_phase_task(intro_duration, _advance_to_loop)

                    await self._stop_rotation_task(session_id, resolved_state)
                    return

                await self._cancel_phase_task()
                await self._send_animation_command(
                    session_id=session_id,
                    animation_file=selected_animation,
                    loop=effective_loop,
                    state=resolved_state.value,
                    descriptor=committed_descriptor,
                    play_section=play_section_override,
                    priority=priority,
                    source=source,
                    state_for_resolution=resolved_state.value,
                )

                self._commit_animation_phase(
                    resolved_state,
                    selected_animation,
                    committed_descriptor,
                    play_section_override or ("loop" if effective_loop else "clip"),
                    self._get_phase_frame_range(
                        committed_descriptor,
                        play_section_override,
                    )
                    if play_section_override
                    else None,
                    session_id,
                    play_section_override is not None,
                    resolved_context_id,
                )
                await self._notify_animation_state_changed(
                    resolved_state,
                    selected_animation,
                    committed_descriptor,
                )
                # If animation is not looping and is not idle, schedule a fallback transition
                # to Idle after the clip duration in case the client doesn't trigger a transition.
                # The backend fallback is ONLY a safety net — the frontend already handles the
                # outro→idle pipeline via the mixer 'finished' event.  The key requirement is
                # that this fires AFTER the full clip (intro + loop + outro) has had time to
                # finish on the client, plus a generous buffer.  Using a single section's
                # duration (old behaviour) caused the fallback to fire while the outro was
                # still playing, interrupting it and producing a T-Pose.
                #
                # Fallback duration is calculated by summing the lengths of all descriptor
                # sections (intro, loop, outro) converted from frames to seconds and then
                # adding 1.5 s of extra slack.  If there is no descriptor or no frame info,
                # a conservative default of 3 s is used before the buffer.  This avoids
                # premature transitions when metadata is missing.
                try:
                    if not effective_loop and resolved_state != AnimationState.IDLE:
                        # Sum durations of ALL descriptor sections so we never fire mid-animation.
                        fallback_duration: float = 0.0
                        is_overlay_state = self._is_overlay_state(resolved_state)
                        if descriptor and isinstance(descriptor, dict):
                            fps_val = float(descriptor.get("fps") or 30)
                            for sec_key in ("intro", "loop", "outro"):
                                sec = descriptor.get(sec_key)
                                if (
                                    isinstance(sec, dict)
                                    and isinstance(sec.get("start_frame"), (int, float))
                                    and isinstance(sec.get("end_frame"), (int, float))
                                ):
                                    fallback_duration += (
                                        max(
                                            0.0,
                                            float(sec["end_frame"])
                                            - float(sec["start_frame"]),
                                        )
                                        / fps_val
                                    )
                        if fallback_duration <= 0.0:
                            # No descriptor or no frame info — use a conservative default so
                            # the frontend's own transition has time to complete.
                            fallback_duration = 1.2 if is_overlay_state else 3.0
                        # Add a buffer so the frontend mixer 'finished' + 140 ms timer
                        # always fires before the backend safety-net does.
                        fallback_duration += 0.2 if is_overlay_state else 1.5

                        # Schedule background task to return to Idle after duration if nothing else changed
                        running_loop = asyncio.get_running_loop()
                        running_loop.create_task(
                            self._non_loop_fallback(
                                session_id,
                                resolved_state,
                                selected_animation,
                                fallback_duration,
                                resolved_context_id,
                            )
                        )
                except Exception:
                    pass
            else:
                log_warning(
                    "[KaradaStateServer] No transports registered, cannot send animation command"
                )
                try:
                    _, fallback_descriptor = (
                        self._resolve_animation_descriptor_for_state(
                            selected_animation,
                            resolved_state.value,
                        )
                    )
                except Exception:
                    fallback_descriptor = None
                fallback_is_idle = resolved_state == AnimationState.IDLE
                fallback_structure = self._analyze_animation_structure(
                    fallback_descriptor,
                    selected_animation,
                )
                fallback_play_section: Optional[str] = None
                if resume_section in {"intro", "loop", "outro"} and isinstance(
                    fallback_descriptor, dict
                ):
                    requested_section = fallback_descriptor.get(resume_section)
                    if isinstance(requested_section, dict):
                        fallback_play_section = resume_section
                elif self._is_server_authoritative_structure(
                    resolved_state,
                    fallback_structure,
                ):
                    fallback_play_section = "intro"
                committed_descriptor = (
                    self._sanitize_idle_descriptor(fallback_descriptor)
                    if fallback_is_idle
                    else fallback_descriptor
                )
                self._commit_animation_phase(
                    resolved_state,
                    selected_animation,
                    committed_descriptor,
                    fallback_play_section or ("loop" if loop else "clip"),
                    self._get_phase_frame_range(
                        committed_descriptor,
                        fallback_play_section,
                    )
                    if fallback_play_section
                    else None,
                    session_id,
                    fallback_play_section is not None,
                    resolved_context_id,
                )
                start_rotation = False

            # If there are multiple animations for this state, start a background
            # rotation task that will randomly switch between them every 30-60s.
            # Skip rotation for animations with loop/intro/outro structure.
            if start_rotation:
                await self._start_rotation_task(session_id, resolved_state, context_id)
            else:
                await self._stop_rotation_task(session_id, resolved_state)

        # Wait for outro to complete outside the lock (so other operations can proceed)
        if needs_outro_transition and outro_duration > 0:
            log_debug(
                f"[KaradaStateServer] Waiting {outro_duration:.2f}s for outro to complete..."
            )
            await asyncio.sleep(outro_duration)

        # If this is a non-idle animation, pre-load IDLE in the background
        # so that when the animation stops, the fallback to IDLE is instant
        if resolved_state != AnimationState.IDLE:
            try:
                asyncio.create_task(self.ensure_idle_preloaded(session_id=session_id))
            except Exception:
                pass

    async def stop_animation(self, context_id: str, session_id: Optional[str]) -> None:
        """Stop an animation context and return to Idle if no other contexts are active.

        Intelligently handles animations with flexible outro sections:
        - If outro exists: play outro before transitioning to Idle
        - If no outro: transition immediately to Idle
        - Handles partial animations gracefully (intro-only, loop-only, etc.)

        Args:
            context_id: The context identifier to stop
            session_id: The WebUI session ID
        """
        outro_duration = 0.0
        should_restore_underlying = False
        async with self._lock:
            is_current_context = self._current_context_id == context_id
            # Get current animation descriptor to check structure
            current_animation = self.current_animation
            current_state_value = (
                self.current_state.value if self.current_state else None
            )
            descriptor = None
            if current_animation:
                _, descriptor = self._resolve_animation_descriptor(current_animation)

            # Analyze animation structure
            structure = self._analyze_animation_structure(descriptor)

            # If the animation has an outro, play it first
            outro_section = (
                descriptor.get("outro") if isinstance(descriptor, dict) else None
            )
            if (
                is_current_context
                and structure["has_outro"]
                and current_animation is not None
                and isinstance(outro_section, dict)
            ):
                log_debug(
                    f"[KaradaStateServer] Playing outro for {current_animation} "
                    f"before stopping (context={context_id}, session={session_id})"
                )
                await self._cancel_phase_task()
                # Play outro with loop=False (play once), explicitly requesting 'outro' section
                await self._send_animation_command(
                    session_id=session_id,
                    animation_file=current_animation,
                    loop=False,
                    state=self.current_state.value,
                    descriptor=descriptor,
                    play_section="outro",
                    state_for_resolution=current_state_value,
                )
                outro_range = self._get_phase_frame_range(descriptor, "outro")
                self._commit_animation_phase(
                    self.current_state,
                    current_animation,
                    descriptor,
                    "outro",
                    outro_range,
                    session_id,
                    True,
                    context_id,
                )
                await self._notify_animation_state_changed(
                    self.current_state,
                    current_animation,
                    descriptor,
                )
                outro_duration = (
                    self._get_phase_duration_seconds(descriptor, "outro") or 0.5
                )
                log_debug(
                    f"[KaradaStateServer] Waiting {outro_duration:.1f}s for outro to complete"
                )
                # Release lock during wait so other operations can proceed
                # But mark that we're in outro playback
                self._active_tasks.pop(context_id, None)
                self._forget_active_context(context_id)
            else:
                # No outro section - transition immediately
                log_debug(
                    f"[KaradaStateServer] No outro section for {current_animation}, "
                    f"stopping immediately (context={context_id})"
                )
                self._active_tasks.pop(context_id, None)
                self._forget_active_context(context_id)
                outro_duration = 0.0

            if is_current_context:
                self._current_context_id = None
                should_restore_underlying = True

        # Wait for outro if needed (outside the lock)
        if outro_duration > 0:
            await asyncio.sleep(outro_duration)

        if not should_restore_underlying:
            return

        # After outro (or immediately if no outro), decide whether to transition to Idle.
        await self._resume_context_or_idle(session_id, excluded_context_id=context_id)

        # When returning to Idle, make sure other rotation tasks for the previous
        # contexts are cleaned up (stop any rotation tasks for non-idle states tied to this session)
        async with self._lock:
            has_remaining_context = self._select_restore_context_locked() is not None
        if not has_remaining_context:
            for anim_state in [
                AnimationState.THINK,
                AnimationState.WRITE,
                AnimationState.TALK,
                AnimationState.TOUCH,
            ]:
                await self._stop_rotation_task(session_id, anim_state)

    async def transition_to(
        self, state: AnimationState, session_id: str, context_id: Optional[str] = None
    ) -> None:
        """Transition to a new animation state.

        This is a convenience method that plays the animation with looping enabled.

        Args:
            state: The animation state to transition to
            session_id: The WebUI session ID
            context_id: Optional context identifier
        """
        log_debug(
            f"[KaradaStateServer] transition_to called: state={state.value}, session_id={session_id}, context_id={context_id}"
        )
        await self.play_animation(
            state=state,
            session_id=session_id,
            loop=True,
            context_id=context_id,
        )

    async def _send_animation_command(
        self,
        session_id: Optional[str],
        animation_file: str,
        loop: bool,
        state: str,
        descriptor: Optional[Dict] = None,
        play_section: Optional[str] = None,
        priority: Optional[int] = None,
        source: Optional[str] = None,
        state_for_resolution: Optional[str] = None,
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
            priority: Optional numeric priority for client-side preemption.
            source: Optional origin string forwarded to the WebUI.
            state_for_resolution: Optional state folder override for path resolution.
                When playing an outro, pass the OLD state so the file is found in
                the correct subdirectory (e.g. 'think' not 'idle').
        """
        if not self._has_any_transport():
            return

        # Resolve path and descriptor (if not already provided)
        # When state_for_resolution is given (e.g. during outro), resolve against
        # that state folder instead of self.current_state (which has already
        # been updated to the NEW state at this point).
        if state_for_resolution:
            if descriptor is None:
                resolved_rel_path, descriptor = (
                    self._resolve_animation_descriptor_for_state(
                        animation_file, state_for_resolution
                    )
                )
            else:
                resolved_rel_path, _ = self._resolve_animation_descriptor_for_state(
                    animation_file, state_for_resolution
                )
        else:
            if descriptor is None:
                resolved_rel_path, descriptor = self._resolve_animation_descriptor(
                    animation_file
                )
            else:
                resolved_rel_path, _ = self._resolve_animation_descriptor(
                    animation_file
                )

        # Prepare optional rich animation_state payload
        animation_state: Dict[str, Any] = {}
        started_at = datetime.now(tz=timezone.utc)
        try:
            resolved_path = resolved_rel_path
            phase = (
                play_section
                if play_section is not None
                else ("loop" if loop else "clip")
            )
            timing = {
                "started_at": started_at.isoformat(),
                "time_in_clip": 0.0,
                "current_frame": 0,
            }

            # Try to fetch emotions from the runtime EmotionManager plugin instance if available
            # (fallback to constructing a local instance).
            emotions = None
            try:
                mgr = None
                try:
                    from core.core_initializer import PLUGIN_REGISTRY

                    mgr = (
                        PLUGIN_REGISTRY.get("emotion_manager")
                        if isinstance(PLUGIN_REGISTRY, dict)
                        else None
                    )
                except Exception:
                    mgr = None

                if mgr is None:
                    # No EmotionManager plugin available; skip emotion enrichment
                    mgr = None

                emotions_raw = None
                if mgr is not None and hasattr(mgr, "get_emotion_state"):
                    emotions_raw_maybe = mgr.get_emotion_state()
                    emotions_raw = (
                        await emotions_raw_maybe
                        if asyncio.iscoroutine(emotions_raw_maybe)
                        else emotions_raw_maybe
                    )

                if isinstance(emotions_raw, dict) and emotions_raw:
                    # Filter out near-zero values (decay tail) to avoid sending meaningless noise.
                    emotions_filtered = {
                        k: v
                        for k, v in emotions_raw.items()
                        if isinstance(v, (int, float)) and v >= 0.1
                    }
                    if emotions_filtered:
                        dominant, max_intensity = max(
                            emotions_filtered.items(), key=lambda x: x[1]
                        )
                        emotions = {"dominant": dominant, "values": emotions_filtered}
                        log_debug(
                            f"[KaradaStateServer] Attached emotions to animation_state: dominant={dominant}, max={max_intensity}"
                        )
            except Exception:
                emotions = None

            lipsync_flag = False
            if descriptor and isinstance(descriptor, dict):
                lipsync_flag = bool(descriptor.get("lipsync", False))

            clip = None
            phase_authoritative = play_section is not None
            frame_range: Optional[Dict[str, int]] = None
            if isinstance(descriptor, dict):
                fps = (
                    descriptor.get("fps")
                    if isinstance(descriptor.get("fps"), (int, float))
                    else 30
                )
                duration = None
                try:
                    section_key = play_section or ("loop" if loop else None)
                    section = (
                        descriptor.get(section_key)
                        if section_key and isinstance(descriptor.get(section_key), dict)
                        else None
                    )
                    if (
                        section
                        and isinstance(section.get("start_frame"), (int, float))
                        and isinstance(section.get("end_frame"), (int, float))
                    ):
                        frame_range = {
                            "start_frame": int(section.get("start_frame")),
                            "end_frame": int(section.get("end_frame")),
                        }
                        frames = max(
                            0.0,
                            float(section.get("end_frame"))
                            - float(section.get("start_frame")),
                        )
                        duration = frames / float(fps) if fps else None
                except Exception:
                    duration = None
                try:
                    clip = {
                        "name": Path(resolved_path).stem
                        if isinstance(resolved_path, str) and resolved_path
                        else Path(animation_file).stem,
                        "duration": duration,
                        "fps": float(fps) if fps else 30,
                    }
                except Exception:
                    clip = None

            # Only attach rich animation_state when we have descriptor and/or emotions.
            if descriptor is None and emotions is None:
                animation_state = {}
            else:
                animation_state = {
                    "action": state,
                    "phase": phase,
                    "phase_authoritative": phase_authoritative,
                    "animation": resolved_path,
                    "descriptor": descriptor,
                    "clip": clip,
                    "timing": timing,
                    "expressions": descriptor.get("expressions")
                    if isinstance(descriptor, dict)
                    else None,
                    "blink": descriptor.get("blink")
                    if isinstance(descriptor, dict)
                    else None,
                    "eye_movement": descriptor.get("eye_movement")
                    if isinstance(descriptor, dict)
                    else None,
                    "emotions": emotions,
                    "lipsync": lipsync_flag,
                    "priority": int(priority) if isinstance(priority, int) else None,
                    "source": source,
                }
        except Exception:
            animation_state = {}

        # Keep track of start time for summaries
        try:
            self._current_animation_started_at = started_at
        except Exception:
            pass

        # Generate a new stable animation_id only when a genuinely new animation
        # starts (different file, and not the outro section of the current one).
        # The ID is kept unchanged for re-sends of the same animation so that
        # any interface that already has the correct ID can skip the restart.
        try:
            if (
                animation_file != self._current_animation_file
                and play_section != "outro"
            ):
                self._current_animation_id = str(uuid.uuid4())
        except Exception:
            pass

        # If session_id is None, broadcast to all connected clients via transports
        try:
            payload: Dict[str, Any] = {
                "type": "vrm_animation",
                "file": resolved_path,
                "loop": loop,
                "state": state,
                "animation_id": self._current_animation_id,
                "reset_eyes": True,
            }
            if isinstance(priority, int):
                payload["priority"] = int(priority)
            if source:
                payload["source"] = str(source)
            if descriptor is not None:
                payload["descriptor"] = descriptor
            if animation_state:
                payload["animation_state"] = animation_state
            if play_section is not None:
                payload["play_section"] = play_section
            if frame_range is not None:
                payload["frame_range"] = frame_range
            payload["phase_authoritative"] = phase_authoritative

            if session_id is None:
                for transport in self._transports:
                    try:
                        await transport.broadcast_animation(payload)
                        log_debug(
                            f"[KaradaStateServer] Broadcast animation via "
                            f"{type(transport).__name__}: {resolved_rel_path}"
                        )
                    except Exception as exc:
                        log_warning(
                            f"[KaradaStateServer] Failed to broadcast animation via "
                            f"{type(transport).__name__}: {exc}"
                        )
                return

            for transport in self._transports:
                try:
                    await transport.send_to_session(session_id, payload)
                    log_debug(
                        f"[KaradaStateServer] Sent animation to session {session_id}: {resolved_rel_path}"
                    )
                except Exception as exc:
                    log_warning(
                        f"[KaradaStateServer] Failed to send animation to session "
                        f"{session_id} via {type(transport).__name__}: {exc}"
                    )
        except Exception as exc:
            log_warning(f"[KaradaStateServer] Failed to send animation command: {exc}")

    async def _preload_animation(
        self,
        session_id: Optional[str],
        animation_file: str,
        state_folder: Optional[str] = None,
    ) -> None:
        """Pre-load an animation file to connected client(s).

        This sends a ``vrm_preload`` message instructing clients to load
        the FBX and descriptor data *before* the play command is sent.

        Args:
            session_id: Target session or ``None`` for broadcast.
            animation_file: The animation file name (e.g. ``'Thinking.fbx'``).
            state_folder: Optional state subfolder for descriptor resolution.
        """
        if not self._has_any_transport():
            log_debug("[KaradaStateServer] No transports registered, skipping preload")
            return

        try:
            # Resolve path (important: resolve using the requested state folder,
            # not the current_state, otherwise preloading can point to the wrong
            # directory, e.g. /think/Idle.fbx).
            if state_folder:
                resolved_path, descriptor = (
                    self._resolve_animation_descriptor_for_state(
                        animation_file, state_folder
                    )
                )
            else:
                resolved_path, descriptor = self._resolve_animation_descriptor(
                    animation_file
                )

            preload_payload: Dict[str, Any] = {
                "type": "vrm_preload",
                "file": resolved_path,
                "state": state_folder or "",
                "descriptor": descriptor,
            }

            for transport in self._transports:
                try:
                    await transport.preload_asset(session_id, preload_payload)
                    log_debug(
                        f"[KaradaStateServer] Preload {animation_file} via "
                        f"{type(transport).__name__} (session={session_id})"
                    )
                except Exception as exc:
                    log_warning(
                        f"[KaradaStateServer] Preload failed via "
                        f"{type(transport).__name__} for {animation_file}: {exc}"
                    )
        except Exception as exc:
            log_warning(
                f"[KaradaStateServer] Preload failed for {animation_file}: {exc}"
            )

    async def _rotation_loop(
        self,
        session_id: Optional[str],
        state: AnimationState,
        context_id: Optional[str],
    ):
        """Background loop that switches animations sequentially or randomly every 30-60s.

        For sequential states, advances through the animation list in order.
        For random states, picks randomly while avoiding repetition when possible.
        """
        key = state.value  # global key — one rotation per state
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
                            choices = [
                                a for a in animations if a != self.current_animation
                            ]
                            candidate = random.choice(choices) if choices else candidate
                    self.current_animation = candidate
                    # Resolve descriptor for candidate to respect play_once if present
                    _, candidate_descriptor = self._resolve_animation_descriptor(
                        candidate
                    )
                    candidate_loop = (
                        False
                        if (
                            candidate_descriptor
                            and candidate_descriptor.get("play_once")
                        )
                        else True
                    )
                    # send new animation command (loop depends on descriptor)
                    await self._send_animation_command(
                        session_id=session_id,
                        animation_file=candidate,
                        loop=candidate_loop,
                        state=state.value,
                        priority=ANIMATION_STATE_PRIORITIES.get(state, 0),
                    )
        except asyncio.CancelledError:
            # Normal cancellation path
            pass
        except Exception as exc:
            log_warning(f"[KaradaStateServer] Rotation loop error for {key}: {exc}")
        finally:
            # Clean up rotation task entry — only if this task is still the
            # registered one (a replacement may have been started).
            if self._rotation_tasks.get(key) is asyncio.current_task():
                del self._rotation_tasks[key]

    async def _non_loop_fallback(
        self,
        session_id: Optional[str],
        state: AnimationState,
        animation_file: str,
        duration: float,
        context_id: Optional[str] = None,
    ):
        """Wait for duration and revert to Idle if the animation completed and no other contexts are active.

        The ``duration`` parameter is computed by ``play_animation()`` and normally
        represents the total clip length (intro+loop+outro) plus a 1.5‑second safety
        buffer.  When descriptor data is unavailable the caller will supply a default
        of 3 s (which also gets the 1.5 s buffer added).
        """
        try:
            await asyncio.sleep(duration + 0.05)
            should_resume = False
            async with self._lock:
                # Only act if the current animation/state match what we scheduled for
                if (
                    self.current_state != state
                    or self.current_animation != animation_file
                ):
                    return
                if context_id and self._current_context_id != context_id:
                    return
                if context_id:
                    self._active_tasks.pop(context_id, None)
                    self._forget_active_context(context_id)
                    if self._current_context_id == context_id:
                        self._current_context_id = None
                should_resume = True

            if should_resume:
                await self._resume_context_or_idle(
                    session_id, excluded_context_id=context_id
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Audio state tracking (for late-joining clients)
    # ------------------------------------------------------------------

    def set_current_audio(
        self,
        url: Optional[str],
        duration_s: Optional[float] = None,
        lipsync_data: Optional[Dict] = None,
    ) -> None:
        """Record the currently-playing TTS audio for catch-up.

        Called by ``SynthWebUIInterface.send_tts_audio()`` after broadcast.
        A background task automatically clears the state after *duration_s*
        seconds so that late joiners don't replay stale audio.
        """
        self._current_audio_url = url
        self._current_audio_duration_s = duration_s
        self._current_audio_lipsync = lipsync_data
        self._current_audio_started_at = datetime.now(tz=timezone.utc)

        # Cancel previous auto-clear task
        if self._audio_clear_task and not self._audio_clear_task.done():
            self._audio_clear_task.cancel()

        if url and duration_s and duration_s > 0:
            try:
                loop = asyncio.get_running_loop()
                self._audio_clear_task = loop.create_task(
                    self._auto_clear_audio(duration_s + 0.5)
                )
            except RuntimeError:
                pass

    async def _auto_clear_audio(self, delay: float) -> None:
        """Clear audio state after the clip has finished playing."""
        try:
            await asyncio.sleep(delay)
            self._current_audio_url = None
            self._current_audio_duration_s = None
            self._current_audio_lipsync = None
            self._current_audio_started_at = None
        except asyncio.CancelledError:
            pass

    def get_current_audio(self) -> Optional[Dict[str, Any]]:
        """Return current audio state for late-joining clients, or ``None``."""
        if not self._current_audio_url or not self._current_audio_started_at:
            return None
        elapsed = (
            datetime.now(tz=timezone.utc) - self._current_audio_started_at
        ).total_seconds()
        dur = self._current_audio_duration_s or 0
        if dur > 0 and elapsed >= dur:
            return None
        return {
            "type": "tts-play",
            "url": self._current_audio_url,
            "audio_duration_s": dur,
            "lipsync": self._current_audio_lipsync,
            "offset_s": elapsed,
        }

    # ------------------------------------------------------------------
    # Priority registration
    # ------------------------------------------------------------------

    def register_state_priority(self, state_name: str, priority: int) -> None:
        """Register or update the priority for a named animation state.

        This allows plugins to introduce custom states (e.g. ``touch``,
        ``emote``) with explicit priority levels.
        """
        self._state_priorities[state_name] = priority
        log_debug(
            f"[KaradaStateServer] Registered priority {priority} for state '{state_name}'"
        )

    def get_state_priority(self, state: AnimationState | str) -> int:
        """Return the priority for a state (name or enum), defaulting to 0."""
        name = state.value if isinstance(state, AnimationState) else str(state)
        return self._state_priorities.get(name, 0)

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------

    def start_watchdog(self) -> None:
        """Start a periodic background task that detects stuck animation states."""
        if self._watchdog_task and not self._watchdog_task.done():
            return  # already running
        try:
            loop = asyncio.get_running_loop()
            self._watchdog_task = loop.create_task(self._watchdog_loop())
        except RuntimeError:
            pass

    async def _watchdog_loop(self) -> None:
        """Every 10 s, verify state coherence and force-reset to IDLE if stuck."""
        try:
            while True:
                await asyncio.sleep(10)
                async with self._lock:
                    # If no transports, nothing to watch
                    if not self._has_any_transport():
                        continue
                    # If state is not IDLE but no active tasks exist, force idle
                    if (
                        self.current_state != AnimationState.IDLE
                        and not self._active_tasks
                    ):
                        log_warning(
                            "[KaradaStateServer] Watchdog: state is "
                            f"{self.current_state.value} but no active tasks. "
                            "Forcing IDLE."
                        )
                # play_animation must be called outside the lock
                if self.current_state != AnimationState.IDLE and not self._active_tasks:
                    await self.play_animation(
                        AnimationState.IDLE,
                        session_id=None,
                        loop=True,
                        context_id=None,
                    )
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log_warning(f"[KaradaStateServer] Watchdog error: {exc}")

    # ------------------------------------------------------------------
    # Rotation tasks  (GLOBAL: keyed by state only, not per-session)
    # ------------------------------------------------------------------

    async def _start_rotation_task(
        self,
        session_id: Optional[str],
        state: AnimationState,
        context_id: Optional[str],
    ) -> None:
        key = state.value
        # Cancel existing rotation task for the same key
        await self._stop_rotation_task(session_id, state)
        # Start new rotation task — session_id is passed through for compat
        # but the key is global so only ONE rotation runs per state.
        running_loop = asyncio.get_running_loop()
        task = running_loop.create_task(
            self._rotation_loop(session_id, state, context_id)
        )
        self._rotation_tasks[key] = task

    async def _stop_rotation_task(
        self, session_id: Optional[str], state: AnimationState
    ) -> None:
        key = state.value
        task = self._rotation_tasks.get(key)
        if task:
            try:
                task.cancel()
                await task
            except asyncio.CancelledError:
                # Normal cancellation - task was cancelled successfully
                pass
            except Exception as exc:
                log_warning(
                    f"[KaradaStateServer] Error cancelling rotation task {key}: {exc}"
                )
            finally:
                # Only remove when it's still the same task — a concurrent
                # _start_rotation_task may have already replaced the entry.
                if self._rotation_tasks.get(key) is task:
                    del self._rotation_tasks[key]

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
_karada_state_server: Optional[KaradaStateServer] = None


def get_karada_state_server() -> KaradaStateServer:
    """Get the global KaradaStateServer instance.

    Returns:
        The KaradaStateServer instance
    """
    global _karada_state_server
    if _karada_state_server is None:
        _karada_state_server = KaradaStateServer()
    return _karada_state_server


# Backward-compatible aliases (deprecated — use get_karada_state_server)
def get_animation_handler() -> KaradaStateServer:
    """Deprecated alias for get_karada_state_server()."""
    return get_karada_state_server()


def set_karada_state_server(handler: KaradaStateServer) -> None:
    """Set the global KaradaStateServer instance.

    Args:
        handler: The KaradaStateServer instance to set
    """
    global _karada_state_server
    _karada_state_server = handler


# Backward-compatible alias (deprecated — use set_karada_state_server)
def set_animation_handler(handler: KaradaStateServer) -> None:
    """Deprecated alias for set_karada_state_server()."""
    set_karada_state_server(handler)
