"""Debrief plugin extracting temporal situational notes from a finished turn.

Sibling of :mod:`debrief_action_intent`. Where that plugin looks at what the
persona *promised* and proposes recovery actions, this one looks at what the
human *said about their circumstances* and stores short-lived, time-bounded
notes so later prompts can take them into account ("has a dentist appointment
tomorrow", "moving house this week", "flying to Osaka on the 14th").

The notes live in the SOUL store (``situational_notes``): this plugin owns the
extraction, SOUL owns the model, the persistence and the prompt injection. When
the SOUL plugin is absent the extraction is skipped and the debrief continues —
removing either plugin never breaks the other.

Stable biographical facts are explicitly out of scope: those belong to the DSP
user profile, which is compiled separately.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_info, log_warning
from core.transport_layer import extract_json_from_text, run_corrector_middleware


display_name = "Debrief Situational Notes"


try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "SITUATIONAL_NOTES_DEBRIEF_ENABLED",
        label="Situational Notes Debrief Enabled",
        default=True,
        value_type=bool,
        ui_type="bool",
        description="Extract temporal situational notes during Debrief",
        scope="agent",
        component="debrief_situational_notes",
        advanced=True,
        needs_component_reload=False,
    )
    register_exposed_var(
        "SITUATIONAL_NOTES_MAX",
        label="Situational Notes Max",
        default=8,
        value_type=int,
        ui_type="number",
        description="Maximum number of situational notes stored per turn",
        scope="agent",
        component="debrief_situational_notes",
        advanced=True,
        needs_component_reload=False,
    )
except Exception:
    pass


_EXTRACT_INSTRUCTIONS = (
    "You extract TEMPORAL SITUATIONAL CONTEXT from one exchange between an AI "
    "persona and its human. This store is for SHORT-LIVED, time-bounded "
    "circumstances only — things that are true now but will expire (on vacation "
    "this week, traveling tomorrow, an appointment on a specific date, moving "
    "house next month).\n"
    "Stable biographical facts (name, job, residence, standing preferences, "
    "tastes) must NOT go here — they belong to the separate persistent user "
    "profile. Only extract circumstances with a clear expiry window.\n"
    "For each circumstance, resolve relative phrases ('tomorrow', 'next week', "
    "'this Friday') to ABSOLUTE ISO datetimes for valid_from and valid_until "
    "using the 'now' value provided. If the human says 'next week' and now is "
    "2026-05-05, valid_from could be 2026-05-05 and valid_until 2026-05-12. "
    "Never invent precise dates the human did not anchor — if unsure, use a "
    "short window and set confidence below 1.0.\n"
    "note_type is one of: EVENT (point-in-time occurrence), STATE (temporary "
    "condition), INTERVAL (time range), INSTANT (past occurrence with short "
    "relevance).\n"
    "priority: -3..3 (higher = more urgent). confidence: 0.0-1.0.\n"
    'Return ONLY a JSON object: {"notes": [{"note_type": ..., "subject": ..., '
    '"summary": ..., "priority": 0, "confidence": 0.5, '
    '"valid_from": "2026-05-05T00:00:00+00:00", '
    '"valid_until": "2026-05-12T00:00:00+00:00"}]} — return an empty notes list '
    "when nothing time-bounded was said."
)


class DebriefSituationalNotesPlugin:
    display_name = display_name

    def get_supported_actions(self) -> dict:
        return {}

    def _is_enabled(self) -> bool:
        try:
            return bool(
                config_registry.get_value(
                    "SITUATIONAL_NOTES_DEBRIEF_ENABLED", True, value_type=bool
                )
            )
        except Exception:
            return True

    def _get_max_notes(self) -> int:
        try:
            value = int(
                config_registry.get_value("SITUATIONAL_NOTES_MAX", 8, value_type=int)
            )
        except Exception:
            return 8
        return max(1, min(value, 20))

    @staticmethod
    def _normalize_assistant_response(text: str) -> str:
        """Unwrap the persona's message from a JSON cortex reply."""
        if not isinstance(text, str) or not text.strip():
            return ""
        try:
            parsed = extract_json_from_text(text, return_metadata=False)
        except Exception:
            parsed = None
        if isinstance(parsed, dict):
            message = parsed.get("message")
            if isinstance(message, str) and message.strip():
                return message.strip()
        return text.strip()

    @staticmethod
    def _extract_note_candidates(parsed: Any) -> List[Dict[str, Any]]:
        """Pull the note dicts out of whatever shape the model returned."""
        if isinstance(parsed, list):
            return [item for item in parsed if isinstance(item, dict)]
        if not isinstance(parsed, dict):
            return []
        for key in ("notes", "situational_notes"):
            if isinstance(parsed.get(key), list):
                return [item for item in parsed[key] if isinstance(item, dict)]
        # A model that returned a single bare note.
        if parsed.get("note_type") and parsed.get("summary"):
            return [parsed]
        return []

    async def _parse_notes_output(
        self,
        *,
        llm_text: str,
        context: Dict[str, Any],
        original_message: Any,
    ) -> List[Dict[str, Any]]:
        """Extract note dicts, asking the corrector once when the JSON is broken."""
        try:
            parsed, metadata = extract_json_from_text(llm_text, return_metadata=True)
        except Exception as exc:
            log_debug(
                f"[debrief_situational_notes] JSON extraction failed, "
                f"will ask corrector: {exc}"
            )
            parsed, metadata = None, {}

        candidates = self._extract_note_candidates(parsed)
        if candidates:
            return candidates
        # An empty but well-formed answer is the normal "nothing temporal was
        # said" case; only bother the corrector when the payload looked broken.
        if isinstance(parsed, (dict, list)) and not (
            metadata.get("had_errors") or metadata.get("had_extra_text")
        ):
            return []

        corrected_text = await run_corrector_middleware(
            text=llm_text,
            bot=None,
            context=context,
            chat_id=getattr(original_message, "chat_id", None),
            thread_id=getattr(original_message, "thread_id", None),
        )
        if not isinstance(corrected_text, str) or not corrected_text.strip():
            return []
        try:
            corrected = extract_json_from_text(corrected_text, return_metadata=False)
        except Exception as exc:
            log_warning(
                f"[debrief_situational_notes] corrected output still not JSON: {exc}"
            )
            return []
        return self._extract_note_candidates(corrected)

    @staticmethod
    def _get_soul_repository() -> Any | None:
        """Return the SOUL repository, or ``None`` when the plugin is absent.

        The note store belongs to SOUL; this plugin only feeds it. Everything is
        guarded so a Synth running without the SOUL plugin still debriefs.
        """
        try:
            from core.core_initializer import PLUGIN_REGISTRY

            soul = PLUGIN_REGISTRY.get("soul_plugin")
        except Exception as exc:
            log_debug(f"[debrief_situational_notes] plugin registry unavailable: {exc}")
            return None
        if soul is None:
            return None
        getter = getattr(soul, "get_repository", None)
        if not callable(getter):
            log_debug(
                "[debrief_situational_notes] soul_plugin exposes no repository accessor"
            )
            return None
        try:
            return getter()
        except Exception as exc:
            log_warning(f"[debrief_situational_notes] repository lookup failed: {exc}")
            return None

    async def _store_notes(
        self, candidates: List[Dict[str, Any]], session_id: str | None
    ) -> int:
        """Validate and persist the extracted notes. Returns how many were stored."""
        repository = self._get_soul_repository()
        if repository is None:
            return 0

        from core.soul.models import situational_note_from_extraction
        from core.soul.schemas import SituationalNoteModel

        stored = 0
        for raw in candidates[: self._get_max_notes()]:
            try:
                model = SituationalNoteModel.model_validate(raw)
            except Exception as exc:
                log_debug(f"[debrief_situational_notes] discarded a note: {exc}")
                continue
            note = situational_note_from_extraction(
                note_type=model.note_type,
                subject=model.subject,
                summary=model.summary,
                priority=model.priority,
                confidence=model.confidence,
                valid_from=model.valid_from,
                valid_until=model.valid_until,
                effective_at=model.effective_at,
                expired_at=model.expired_at,
                source="debrief",
                session_id=session_id,
            )
            try:
                await repository.upsert_situational_note(note)
                stored += 1
            except Exception as exc:
                log_warning(
                    f"[debrief_situational_notes] could not store a note: {exc}"
                )
        return stored

    async def on_debrief(
        self,
        processed_actions: List[Dict],
        failed_actions: List[Dict],
        results: Dict,
        context: Dict,
        original_message: Any,
    ) -> Dict | None:
        if not self._is_enabled():
            return None

        if not isinstance(context, dict):
            context = {}

        llm_response = self._normalize_assistant_response(
            context.get("llm_response_text")
            or (results or {}).get("llm_response_text")
            or ""
        )
        if not llm_response:
            return None

        if not (
            context.get("from_cortex")
            or getattr(original_message, "from_cortex", False)
        ):
            return None

        user_message = (
            context.get("original_user_message")
            or getattr(original_message, "text", "")
            or ""
        )
        if not isinstance(user_message, str) or not user_message.strip():
            return None

        now = datetime.now(timezone.utc)
        # Plain labelled text, deliberately not a JSON blob. An OpenAI-compatible
        # backend may try to read a JSON string as structured content and keep
        # only the keys it recognises (text/content/parts), silently dropping
        # everything else: the turn then never reaches the model and every
        # extraction comes back empty with no error anywhere.
        user_prompt = (
            f"now: {now.isoformat()}\n"
            f"max_notes: {self._get_max_notes()}\n\n"
            f"The human said:\n{user_message.strip()}\n\n"
            f"The persona replied:\n{llm_response}"
        )

        try:
            from core.config import derive_cortex_scope, get_active_cortex_engine
            from core.cortex_registry import get_cortex_registry

            scope = derive_cortex_scope(context)
            active_cortex = await get_active_cortex_engine(scope=scope)
            registry = get_cortex_registry()
            engine = registry.get_engine(active_cortex) or registry.load_engine(
                active_cortex
            )
        except Exception as exc:
            log_warning(
                f"[debrief_situational_notes] could not load Cortex engine: {exc}"
            )
            return None

        if not engine or not hasattr(engine, "generate_response"):
            return None

        try:
            llm_text = await asyncio.wait_for(
                engine.generate_response(
                    [
                        {"role": "system", "content": _EXTRACT_INSTRUCTIONS},
                        {"role": "user", "content": user_prompt},
                    ]
                ),
                timeout=120,
            )
        except Exception as exc:
            # repr, not str: a bare asyncio.TimeoutError stringifies to "",
            # which made this line read as a failure with no reason at all.
            log_warning(f"[debrief_situational_notes] LLM generation failed: {exc!r}")
            return None

        candidates = await self._parse_notes_output(
            llm_text=llm_text,
            context=context,
            original_message=original_message,
        )
        if not candidates:
            return None

        session_id = context.get("session_id") or getattr(
            original_message, "session_id", None
        )
        stored = await self._store_notes(candidates, session_id)
        if stored:
            log_info(f"[debrief_situational_notes] Stored {stored} situational note(s)")
        return None


PLUGIN_CLASS = DebriefSituationalNotesPlugin
