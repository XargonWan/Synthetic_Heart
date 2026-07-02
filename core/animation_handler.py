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

        # Centralized animation state that syncs across all clients (Karada v2)
        # Server ONLY sends: state + descriptor + started_at.
        # NO phase control, NO timing control, NO frame-level logic
        self._current_animation_file: Optional[str] = None  # Actual file being played
        self._current_animation_descriptor: Optional[Dict[str, Any]] = (
            None  # Descriptor with frame info (intro/loop/outro)
        )
        self._current_animation_started_at: Optional[datetime] = (
            None  # UTC timestamp for current animation start (authoritative)
        )
        # Animation clock: server sends started_at, client computes animation_time locally
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

    @staticmethod
    def _started_at_epoch(started_at: Optional[datetime]) -> Optional[float]:
        """Convert a datetime to UTC epoch seconds for the Karada v2 contract."""
        if started_at is None:
            return None

        try:
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=timezone.utc)
            return float(started_at.astimezone(timezone.utc).timestamp())
        except Exception:
            return None

    @staticmethod
    def _slug_contract_part(value: Optional[str]) -> str:
        """Normalize a contract segment into a stable ASCII slug."""
        raw = str(value or "").strip().lower()
        if not raw:
            return "unknown"

        out: List[str] = []
        last_was_sep = False
        for char in raw:
            if char.isalnum():
                out.append(char)
                last_was_sep = False
            elif not last_was_sep:
                out.append("_")
                last_was_sep = True

        normalized = "".join(out).strip("_")
        return normalized or "unknown"

    def _extract_contract_identity(
        self,
        state_name: Optional[str],
        resolved_path: Optional[str],
        animation_file: Optional[str],
    ) -> tuple[str, str, str]:
        """Return ``(skin, state, stem)`` for contract and manifest generation."""
        resolved = str(resolved_path or "").split("?", 1)[0].split("#", 1)[0]
        segments = [segment for segment in resolved.split("/") if segment]

        skin_name = "local"
        state_value = str(state_name or "").strip() or "unknown"

        if (
            len(segments) >= 5
            and segments[0] == "skins"
            and segments[2] == "animations"
        ):
            skin_name = segments[1] or skin_name
            if not state_name:
                state_value = segments[3] or state_value

        stem_source = (
            animation_file or (segments[-1] if segments else "") or state_value
        )
        stem = Path(stem_source).stem or stem_source or state_value
        return skin_name, state_value, stem

    def _build_descriptor_id(
        self,
        state_name: Optional[str],
        animation_file: Optional[str],
        resolved_path: Optional[str] = None,
    ) -> Optional[str]:
        """Build the logical Karada v2 descriptor identifier for an animation."""
        if not state_name and not animation_file and not resolved_path:
            return None

        skin_name, state_value, stem = self._extract_contract_identity(
            state_name=state_name,
            resolved_path=resolved_path,
            animation_file=animation_file,
        )
        return (
            f"{self._slug_contract_part(skin_name)}/"
            f"{self._slug_contract_part(state_value)}/"
            f"{self._slug_contract_part(stem)}"
        )

    def get_animation_manifest_entry(
        self,
        state_name: Optional[str],
        animation_file: Optional[str],
        descriptor: Optional[Dict[str, Any]] = None,
        resolved_path: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Build one reusable Karada animation manifest entry."""
        if not animation_file:
            return None

        resolved = resolved_path
        descriptor_data = descriptor if isinstance(descriptor, dict) else None
        effective_state = state_name or (
            self.current_state.value if self.current_state else None
        )

        if resolved is None:
            try:
                resolved, resolved_descriptor = (
                    self._resolve_animation_descriptor_for_state(
                        animation_file,
                        effective_state,
                    )
                )
            except Exception:
                resolved = animation_file
                resolved_descriptor = None
            if descriptor_data is None and isinstance(resolved_descriptor, dict):
                descriptor_data = resolved_descriptor

        descriptor_id = self._build_descriptor_id(
            state_name=effective_state,
            animation_file=animation_file,
            resolved_path=resolved,
        )
        if not descriptor_id:
            return None

        skin_name, state_value, _ = self._extract_contract_identity(
            state_name=effective_state,
            resolved_path=resolved,
            animation_file=animation_file,
        )

        return {
            "id": descriptor_id,
            "state": state_value,
            "skin": skin_name,
            "animation_file": animation_file,
            "animation_url": resolved,
            "descriptor_url": (
                f"/api/karada/animations/resolve?descriptor_id={descriptor_id}"
            ),
            "descriptor_data": descriptor_data,
        }

    def get_animation_manifest(self) -> Dict[str, Any]:
        """Return the client-facing Karada animation manifest keyed by descriptor id."""
        animations: Dict[str, Dict[str, Any]] = {}
        state_names = sorted(
            {
                *(state.value for state in AnimationState),
                *self._registered_state_animations.keys(),
            }
        )

        for state_name in state_names:
            try:
                variants = self.get_animation_variants(state_name)
            except Exception:
                continue

            for category, files in variants.items():
                for animation_file in files:
                    entry = self.get_animation_manifest_entry(
                        state_name=state_name,
                        animation_file=animation_file,
                    )
                    if not entry:
                        continue
                    manifest_entry = dict(entry)
                    manifest_entry["category"] = category
                    animations.setdefault(entry["id"], manifest_entry)

        return {"version": 2, "animations": animations}

    def get_animation_manifest_entry_by_id(
        self, descriptor_id: str
    ) -> Optional[Dict[str, Any]]:
        """Resolve one manifest entry by descriptor id."""
        if not descriptor_id:
            return None
        return self.get_animation_manifest().get("animations", {}).get(descriptor_id)

    def get_current_animation_state(self) -> Dict[str, Any]:
        """Get the current centralized animation state (Karada v2).

        Karada v2: Returns ONLY state + descriptor + started_at.
        NO phase control, NO timing control, NO frame-level logic.

        Returns:
            Dict with state information for clients
        """
        anim = self._current_animation_file
        state_name = self.current_state.value if self.current_state else None

        resolved_path: Optional[str] = None
        if anim:
            try:
                resolved_path, _ = self._resolve_animation_descriptor_for_state(
                    anim, state_name
                )
            except Exception:
                resolved_path = anim

        return {
            "state": state_name,
            "descriptor": self._build_descriptor_id(
                state_name=state_name,
                animation_file=anim,
                resolved_path=resolved_path,
            ),
            "started_at": self._started_at_epoch(self._current_animation_started_at),
        }

    async def get_full_state(self) -> Dict[str, Any]:
        """Return the complete VRM state for clients on WebSocket connect (Karada v2).

        Karada v2: Returns simplified state with started_at timestamp.
        Client uses started_at to compute animation_time locally.
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

        # Animation state (Karada v2 protocol)
        animation: Dict[str, Any] = {}
        anim_file = self._current_animation_file
        if anim_file:
            try:
                resolved, _ = self._resolve_animation_descriptor(anim_file)
            except Exception:
                resolved = anim_file
            animation = {
                "state": self.current_state.value if self.current_state else None,
                "descriptor": self._build_descriptor_id(
                    state_name=self.current_state.value if self.current_state else None,
                    animation_file=anim_file,
                    resolved_path=resolved,
                ),
                "started_at": self._started_at_epoch(
                    self._current_animation_started_at
                ),
            }

        # Face values
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

    # ------------------------------------------------------------------
    # Karada v2: Phase control methods REMOVED
    # Server no longer controls animation phases (intro/loop/outro).
    # Client handles all phase transitions locally using descriptor + started_at.
    # ------------------------------------------------------------------

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
        """Play an animation for a specific state (Karada v2).

        Karada v2: Server ONLY sends state + descriptor + started_at.
        NO phase control, NO timing control, NO frame-level logic.
        Client handles intro→loop→outro locally using the descriptor.

        Args:
            state: The animation state to play
            session_id: The WebUI session ID to send the animation to
            loop: Whether the animation should loop (hint for client)
            context_id: Optional identifier for this animation context (for tracking)
            priority: Optional priority level for this animation context.
            source: Optional string describing the origin of the animation request.
            animation_file: Optional concrete animation file override.
            resume_section: Deprecated - ignored in Karada v2.
        """
        log_info(
            f"[KaradaStateServer] ⭐ play_animation (v2) CALLED: state={state.value}, session={session_id}",
            log_file="webui",
        )

        async with self._lock:
            # Use state priority if not explicitly provided
            if priority is None:
                priority = self.get_state_priority(state)

            # Step 1: Select animation file and finalize resolved_state
            resolved_state = state
            variants = self.get_animation_variants(state.value)
            if state == AnimationState.IDLE:
                animations = variants.get("loop", []) or variants.get("other", [])
            else:
                animations = (
                    variants.get("loop", [])
                    or variants.get("post", [])
                    or variants.get("other", [])
                )
            if not animations:
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
                current_index = self._sequence_indices.get(resolved_state.value, -1)
                next_index = (current_index + 1) % len(animations)
                selected_animation = animations[next_index]
                self._sequence_indices[resolved_state.value] = next_index
            else:
                selected_animation = random.choice(animations)

            log_debug(
                f"[KaradaStateServer] Playing {resolved_state.value} animation: {selected_animation}"
            )

            # Step 3: Update context tracking
            _KARADA_AUTO_CTX = "__karada_auto"

            # Step 2: Compute should_update_state
            #
            # Priority comparison must only guard against a *different*, explicit
            # context (e.g. a plugin-driven touch/emote overlay). Successive
            # lifecycle phases (think -> write -> idle) all arrive as the
            # auto-context with no explicit context_id: they are sequential
            # phases of the same flow, not competing overlays, so a later phase
            # must always replace the previous one regardless of its nominal
            # priority. Otherwise a lower-priority phase (e.g. write, priority 3)
            # would be deferred behind the still-active higher-priority phase
            # (think, priority 10), freezing the avatar on the earlier state.
            should_update_state = True
            is_auto_request = context_id is None
            current_is_auto = self._current_context_id == _KARADA_AUTO_CTX
            same_lifecycle_lane = is_auto_request and current_is_auto
            if (
                not same_lifecycle_lane
                and self._current_context_id
                and self._current_context_id in self._active_tasks
            ):
                current_priority = self._active_tasks.get(self._current_context_id) or 0
                if priority < current_priority:
                    should_update_state = False

            # Create context metadata with FINALIZED resolved_state
            context_meta = {
                "state": resolved_state,
                "session_id": session_id,
                "loop": loop,
                "priority": priority,
                "source": source,
                "animation_file": selected_animation,
                "sequence": getattr(self, "_context_sequence", 0),
            }
            self._context_sequence = getattr(self, "_context_sequence", 0) + 1

            if context_id:
                self._active_tasks.pop(_KARADA_AUTO_CTX, None)
                self._active_tasks[context_id] = priority
                self._active_context_meta[context_id] = context_meta
                if should_update_state:
                    self._current_context_id = context_id
            elif resolved_state != AnimationState.IDLE:
                self._active_tasks[_KARADA_AUTO_CTX] = priority
                self._active_context_meta[_KARADA_AUTO_CTX] = context_meta
                if should_update_state:
                    self._current_context_id = _KARADA_AUTO_CTX
            else:
                self._active_tasks.pop(_KARADA_AUTO_CTX, None)
                self._forget_active_context(_KARADA_AUTO_CTX)
                self._current_context_id = None

            if not should_update_state:
                log_debug(
                    "[KaradaStateServer] Higher-priority animation active; deferring "
                    f"{resolved_state.value} until the dominant context ends"
                )
                return

            # Step 4: Pre-load animation before sending command
            if self._has_any_transport():
                try:
                    if resolved_state != AnimationState.IDLE:
                        await self.ensure_idle_preloaded(session_id=session_id)
                    await self._preload_animation(
                        session_id=session_id,
                        animation_file=selected_animation,
                        state_folder=resolved_state.value,
                    )
                except Exception as exc:
                    log_warning(
                        f"[KaradaStateServer] Preload failed for {selected_animation}: {exc}"
                    )
                    if resolved_state != AnimationState.IDLE:
                        if context_id:
                            self._active_tasks.pop(context_id, None)
                            self._forget_active_context(context_id)
                        return

            # Step 5: Karada v2 - Update state and broadcast
            self._current_animation_started_at = datetime.now(timezone.utc)
            if should_update_state:
                self.current_state = resolved_state
            self.current_animation = selected_animation
            self._current_animation_file = selected_animation

            # Step 6: Resolve descriptor
            resolved_path, descriptor = self._resolve_animation_descriptor_for_state(
                selected_animation, resolved_state.value
            )

            # Sanitize descriptor for idle (no outro/play_once)
            if resolved_state == AnimationState.IDLE:
                self._current_animation_descriptor = self._sanitize_idle_descriptor(
                    descriptor
                )
            else:
                self._current_animation_descriptor = descriptor

            # Step 7: Broadcast Karada v2 payload only
            if self._has_any_transport():
                await self._send_animation_command_v2(
                    session_id=session_id,
                    state=resolved_state.value,
                    animation_file=selected_animation,
                    started_at=self._current_animation_started_at,
                )

            # Step 8: Notify callbacks
            await self._notify_animation_state_changed(
                resolved_state,
                selected_animation,
                self._current_animation_descriptor,
            )

            # Step 9: Handle rotation for idle
            if resolved_state == AnimationState.IDLE and len(animations) > 1:
                await self._start_rotation_task(session_id, resolved_state, context_id)
            else:
                await self._stop_rotation_task(session_id, resolved_state)

        # Background: pre-load idle for non-idle animations
        if resolved_state != AnimationState.IDLE:
            try:
                asyncio.create_task(self.ensure_idle_preloaded(session_id=session_id))
            except Exception:
                pass

    async def stop_animation(self, context_id: str, session_id: Optional[str]) -> None:
        """Stop an animation context and return to Idle if no other contexts are active.

        Karada v2: Server simply sends idle state.
        Client handles any outro transitions locally using the descriptor.

        Args:
            context_id: The context identifier to stop
            session_id: The WebUI session ID
        """
        async with self._lock:
            # Clear context tracking
            self._active_tasks.pop(context_id, None)
            self._forget_active_context(context_id)

            is_current_context = self._current_context_id == context_id
            if is_current_context:
                self._current_context_id = None

            # Find the context to restore (inside the lock)
            restore = self._select_restore_context_locked(
                excluded_context_id=context_id
            )
            should_restore = restore is not None
            restore_state = restore.get("state") if restore else None
            restore_session_id = restore.get("session_id") if restore else None
            restore_loop = restore.get("loop") if restore else True
            restore_priority = restore.get("priority") if restore else None
            restore_source = restore.get("source") if restore else None
            restore_animation_file = restore.get("animation_file") if restore else None
            restore_context_id = restore.get("context_id") if restore else None

        # Outside the lock, perform the restore or idle transition
        if should_restore and isinstance(restore_state, AnimationState):
            await self.play_animation(
                restore_state,
                session_id=restore_session_id or session_id,
                loop=bool(restore_loop),
                context_id=restore_context_id,
                priority=restore_priority,
                source=restore_source,
                animation_file=restore_animation_file,
            )
        else:
            # No context to restore, transition to idle
            await self.play_animation(
                AnimationState.IDLE,
                session_id=session_id,
                loop=True,
                context_id=None,
            )

        # Clean up rotation tasks for non-idle states
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

    # ------------------------------------------------------------------
    # Karada v2 Protocol - Simplified Animation Command
    # Server ONLY sends: state + descriptor + started_at.
    # NO phase control, NO timing control, NO frame-level logic
    # ------------------------------------------------------------------

    async def _send_animation_command_v2(
        self,
        session_id: Optional[str],
        state: str,
        animation_file: str,
        started_at: datetime,
    ) -> None:
        """Send Karada v2 animation command to clients.

        Karada v2 Protocol:
        - Server sends WHAT to play (state, descriptor id)
        - Server sends authoritative timestamp (started_at)
        - Client decides HOW and WHEN to play it locally
        - No phase control, no timing control, no frame-level logic

        Args:
            session_id: Target session or None for broadcast
            state: The logical state name (e.g., 'think', 'idle')
            animation_file: The animation file name
            started_at: Authoritative UTC timestamp when animation started
        """
        if not self._has_any_transport():
            return

        # Resolve descriptor id for the animation file
        try:
            resolved_path, _ = self._resolve_animation_descriptor_for_state(
                animation_file, state
            )
        except Exception:
            resolved_path = None

        descriptor_id = self._build_descriptor_id(
            state_name=state,
            animation_file=animation_file,
            resolved_path=resolved_path,
        )

        # Build Karada v2 payload
        payload: Dict[str, Any] = {
            "type": "vrm_animation_v2",
            "state": state,
            "descriptor": descriptor_id,
            "started_at": self._started_at_epoch(started_at),
        }

        # Send to clients
        try:
            if session_id is None:
                for transport in self._transports:
                    try:
                        await transport.broadcast_animation(payload)
                        log_debug(
                            f"[KaradaStateServer] Broadcast v2 animation via "
                            f"{type(transport).__name__}: {state}"
                        )
                    except Exception as exc:
                        log_warning(
                            f"[KaradaStateServer] Failed to broadcast v2 animation via "
                            f"{type(transport).__name__}: {exc}"
                        )
                return

            for transport in self._transports:
                try:
                    await transport.send_to_session(session_id, payload)
                    log_debug(
                        f"[KaradaStateServer] Sent v2 animation to session {session_id}: {state}"
                    )
                except Exception as exc:
                    log_warning(
                        f"[KaradaStateServer] Failed to send v2 animation to session "
                        f"{session_id} via {type(transport).__name__}: {exc}"
                    )
        except Exception as exc:
            log_warning(
                f"[KaradaStateServer] Failed to send v2 animation command: {exc}"
            )

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

                    variants = self.get_animation_variants(state.value)
                    if state == AnimationState.IDLE:
                        animations = variants.get("loop", []) or variants.get(
                            "other", []
                        )
                    else:
                        animations = (
                            variants.get("loop", [])
                            or variants.get("post", [])
                            or variants.get("other", [])
                        )
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
                    self.current_state = state
                    self.current_animation = candidate
                    self._current_animation_file = candidate
                    self._current_animation_started_at = datetime.now(timezone.utc)

                    _, candidate_descriptor = (
                        self._resolve_animation_descriptor_for_state(
                            candidate, state.value
                        )
                    )
                    if state == AnimationState.IDLE:
                        self._current_animation_descriptor = (
                            self._sanitize_idle_descriptor(candidate_descriptor)
                        )
                    else:
                        self._current_animation_descriptor = candidate_descriptor

                    await self._send_animation_command_v2(
                        session_id=session_id,
                        state=state.value,
                        animation_file=candidate,
                        started_at=self._current_animation_started_at,
                    )
                    await self._notify_animation_state_changed(
                        state,
                        candidate,
                        self._current_animation_descriptor,
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
