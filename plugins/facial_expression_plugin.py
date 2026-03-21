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
from core.persona_manager import get_persona_manager


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

    def get_supported_actions(self) -> Dict[str, Any]:
        # produce a static injection with instructions and the available
        # expressions read from the default persona (Rei) to make the list
        # dynamic and overridable by skins.
        from core.persona_manager import get_persona_manager

        persona_json: Optional[Dict] = None
        pm = get_persona_manager()
        if pm and getattr(pm, "_current_persona", None):
            try:
                persona_json = pm._load_persona_json(pm._current_persona.name)
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
            + ("\nINTENSITY: float 0.0-1.0. Use [em] to reset immediately.")
            + (
                '\nThese tags are invisible to users. Example: "Ciao! [em_grin:0.9] Come va?"'
            )
            + (
                "\nOnly use these when responding to interfaces that support face rendering."
            )
        )
        return {"facial_expression_helper": instructions}

    async def process_message_text(self, text: str, session_id: str) -> str:
        """Parse text for tags and schedule expression timeline.

        Returns the cleaned text (tags stripped).
        """
        clean, events = parse_facial_expressions(text)
        if events and get_karada_state_server().has_connected_clients():
            total_chars = len(clean)
            # load persona.json to access cooldown/char-rate settings
            persona_json: Optional[Dict] = None
            pm = get_persona_manager()
            if pm and getattr(pm, "_current_persona", None):
                try:
                    persona_json = pm._load_persona_json(pm._current_persona.name)
                except Exception:
                    persona_json = None
            cooldown = (
                persona_json.get("facial_expression_cooldown_s", 3)
                if persona_json
                else 3
            )
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
                    cooldown,
                    chars_per_sec,
                    expr_section=expr_section,
                )
            )
        return clean

    async def _play_expression_timeline(
        self,
        events: List[FacialExpressionEvent],
        total_chars: int,
        session_id: str,
        cooldown_s: float,
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
        start = asyncio.get_event_loop().time()
        for item in timeline:
            now = asyncio.get_event_loop().time()
            sleep_for = item.delay - (now - start)
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
            # resolve blendshape targets from persona expression catalogue
            resolved_targets: Optional[Dict[str, float]] = None
            if item.name and expr_section:
                raw = expr_section.get(item.name, {}).get("targets", {})
                if raw:
                    resolved_targets = {
                        k: float(v) * item.intensity for k, v in raw.items()
                    }
            await karada.push_face_expression(
                item.name, item.intensity, targets=resolved_targets
            )
        # after all events, schedule cooldown reset
        await asyncio.sleep(cooldown_s)
        await karada.push_face_expression(None, 0)

    def get_metadata(self) -> Dict[str, Any]:
        return {
            "name": "facial_expression_plugin",
            "description": "Parses [em_NAME:INTENSITY] tags in LLM output and drives VRM blendshape expressions via KaradaStateServer.",
            "version": "1.0.0",
        }


PLUGIN_CLASS = FacialExpressionPlugin
