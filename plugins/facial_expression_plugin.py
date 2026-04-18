from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from core.plugin_base import PluginBase
from core.animation_handler import get_karada_state_server
from core.facial_expression_parser import (
    FacialExpressionEvent,
    parse_facial_expressions,
)
from core.logging_utils import log_debug
from core.persona_manager import get_persona_manager

_LOG_PREFIX = "[facial_expression]"


@dataclass
class _TimelineEvent:
    delay: float
    name: Optional[str]
    intensity: float


class FacialExpressionPlugin(PluginBase):
    """Plugin that handles LLM facial expression tags.

    The plugin provides prompt instructions so that models know how to emit
    ``[em_name:intensity]`` tokens and it also runs a background timeline when a
    message containing tags is sent, driving the KaradaStateServer to push the
    corresponding WebSocket packets at the appropriate times.
    """

    def _build_expression_instructions(self) -> str:
        # Build static guidance from the active persona so expression names
        # stay in sync with skin-level overrides.
        persona_json: Optional[Dict] = None
        pm = get_persona_manager()
        current_persona = getattr(pm, "_current_persona", None) if pm else None
        if pm and current_persona and getattr(current_persona, "name", None):
            try:
                persona_json = pm._load_persona_json(current_persona.name)
            except Exception:
                persona_json = None
        expr_section = (
            persona_json.get("facial_expressions", {}) if persona_json else {}
        )
        # fallback defaults if skin doesn't provide any
        if not expr_section:
            expr_section = {
                name: {}
                for name in [
                    "smile",
                    "grin",
                    "sad",
                    "blush",
                    "surprised",
                    "angry",
                ]
            }
        expr_names = ", ".join(expr_section.keys()) or "<none>"

        instructions = (
            "You can embed facial expression tags in your message text: [em_NAME:INTENSITY]"
            "\nAvailable: %s"
            % expr_names
            + ("\nINTENSITY: float 0.0-1.0.")
            + (
                "\nEach expression persists until the next [em_...] tag or the end of the audio message."
            )
            + (
                "\nAt the end of the audio, the face automatically returns to the current base emotional state."
            )
            + ("\nUse [em] (bare) to return to base emotional state mid-sentence.")
            + (
                "\nDo NOT add a reset tag at the end of your message — it happens automatically."
            )
            + (
                '\nThese tags are invisible to users. Example: "Ciao! [em_grin:0.9] Come va?"'
            )
            + (
                "\nOnly use these when responding to interfaces that support face rendering."
            )
        )
        return instructions

    def get_supported_actions(self) -> Dict[str, Any]:
        # Advertise a valid static_inject schema so core action registration
        # can merge plugin fields without runtime type errors.
        return {
            "static_inject": {
                "description": "Provide facial-expression guidance for prompt injection",
                "required_params": {},
                "optional_params": {},
            }
        }

    def get_static_injection(
        self,
        message: Optional[Any] = None,
        context_memory: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        return {"facial_expression_guidance": self._build_expression_instructions()}

    async def process_message_text(
        self,
        text: str,
        session_id: str,
        audio_duration_s: Optional[float] = None,
    ) -> str:
        """Parse text for tags and schedule expression timeline.

        *audio_duration_s*, when provided, overrides the character-based
        timing estimate so that expressions stay synchronised with speech.

        Returns the cleaned text (tags stripped).
        """
        clean, events = parse_facial_expressions(text)
        if events and get_karada_state_server().has_connected_clients():
            total_chars = len(clean)
            # load persona.json to access char-rate settings
            persona_json: Optional[Dict] = None
            pm = get_persona_manager()
            current_persona = getattr(pm, "_current_persona", None) if pm else None
            if pm and current_persona and getattr(current_persona, "name", None):
                try:
                    persona_json = pm._load_persona_json(current_persona.name)
                except Exception:
                    persona_json = None
            chars_per_sec = (
                persona_json.get("facial_expression_chars_per_sec", 12)
                if persona_json
                else 12
            )
            expr_section: Optional[Dict[str, Any]] = (
                persona_json.get("facial_expressions", {}) if persona_json else {}
            )
            asyncio.create_task(
                self._play_expression_timeline(
                    events,
                    total_chars,
                    session_id,
                    chars_per_sec,
                    expr_section=expr_section,
                    audio_duration_s=audio_duration_s,
                )
            )
        return clean

    async def _play_expression_timeline(
        self,
        events: List[FacialExpressionEvent],
        total_chars: int,
        session_id: str,
        chars_per_sec: float,
        expr_section: Optional[Dict[str, Any]] = None,
        audio_duration_s: Optional[float] = None,
    ) -> None:
        """Drive KaradaStateServer through a sequence of expression events.

        *expr_section* is the ``facial_expressions`` dict from the active
        persona.json (``{name: {targets: {blendshape: value}}}``).
        When provided, each event's blendshape targets are resolved here
        (Python-side) and forwarded to the client already scaled by the
        event intensity.  This lets the JS pipeline apply the correct morphs
        without knowing the expression catalogue (GAP 1A).

        *audio_duration_s*, when provided, overrides the character-based
        timing estimate with the actual TTS audio duration so that facial
        expressions stay synchronised with speech output.

        Each expression persists until the next event.  After the last event
        the expression is held until the end of the audio/text duration,
        then a clear is sent so the avatar's base emotional state (managed
        by ``emotion_manager`` at a lower priority) takes over.
        """
        karada = get_karada_state_server()
        if not karada:
            return
        # Total duration: prefer real audio length over character estimate
        total_duration = (
            audio_duration_s
            if audio_duration_s is not None and audio_duration_s > 0
            else (total_chars / chars_per_sec if chars_per_sec > 0 else 1.0)
        )
        timeline: List[_TimelineEvent] = []
        for ev in events:
            # compute delay proportional to character position in the text
            frac = ev.position / total_chars if total_chars > 0 else 0.0
            delay = frac * total_duration
            timeline.append(_TimelineEvent(delay, ev.name, ev.intensity))
        log_debug(
            f"{_LOG_PREFIX} timeline start: {len(timeline)} events, "
            f"duration={total_duration:.2f}s"
        )
        start = asyncio.get_event_loop().time()
        for item in timeline:
            now = asyncio.get_event_loop().time()
            sleep_for = item.delay - (now - start)
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            # resolve blendshape targets from persona expression catalogue
            resolved_targets: Optional[Dict[str, float]] = None
            is_clear = False
            if item.name and expr_section:
                entry = expr_section.get(item.name)
                if entry is not None:
                    if not isinstance(entry, dict):
                        log_debug(
                            f"{_LOG_PREFIX} ignoring malformed expression '{item.name}': "
                            f"expected dict, got {type(entry).__name__}"
                        )
                    else:
                        raw = entry.get("targets", {})
                        if isinstance(raw, dict) and raw:
                            resolved_targets = {
                                str(k): float(v) * item.intensity
                                for k, v in raw.items()
                            }
                        else:
                            # Expression with empty/invalid targets (e.g. "neutral") → clear
                            is_clear = True
            if is_clear:
                log_debug(f"{_LOG_PREFIX} event '{item.name}' → clear (empty targets)")
                await karada.push_face_expression(None, 0)
            else:
                log_debug(
                    f"{_LOG_PREFIX} event '{item.name}' i={item.intensity} → "
                    f"targets={resolved_targets}"
                )
                await karada.push_face_expression(
                    item.name, item.intensity, targets=resolved_targets
                )
        # Hold last expression until end of audio/text duration, then clear
        # so the avatar returns to its base emotional state.
        elapsed = asyncio.get_event_loop().time() - start
        remaining = total_duration - elapsed
        if remaining > 0:
            await asyncio.sleep(remaining)
        log_debug(
            f"{_LOG_PREFIX} timeline done ({total_duration:.2f}s), returning to base emotional state"
        )
        await karada.push_face_expression(None, 0)

    def get_metadata(self) -> Dict[str, Any]:
        return {
            "name": "facial_expression_plugin",
            "description": "Parses [em_NAME:INTENSITY] tags in LLM output and drives VRM blendshape expressions via KaradaStateServer.",
            "version": "1.0.0",
        }


PLUGIN_CLASS = FacialExpressionPlugin
