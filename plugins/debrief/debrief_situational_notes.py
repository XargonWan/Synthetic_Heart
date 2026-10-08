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
    # Not a setting: the key only places the notes editor in this component's
    # pane. The WebUI reads and writes the notes through
    # /api/soul/situational-notes and never saves a value under this key.
    register_exposed_var(
        "SITUATIONAL_NOTES_EDITOR",
        label="Current Situational Notes",
        default="",
        value_type=str,
        ui_type="situational-notes",
        description=(
            "The notes standing for the human right now, one per line. Edit, add "
            "or delete lines and press Save; a deleted line retires its note."
        ),
        scope="agent",
        component="debrief_situational_notes",
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
    "EVERY NOTE MUST BE ABOUT THE HUMAN'S CIRCUMSTANCES (or ones the human shares). Never write a note about the persona's own state, its moods or its plans, and never about a third party's state: those belong to the persona's diary, not to this store. A note whose subject is the persona or another character is wrong even when the exchange mentions them.\n"
    "subject is a SHORT canonical noun phrase (2-4 words) naming the specific circumstance, and it must be written the SAME way every time you describe that circumstance ('gathering at Sandro's', not 'gathering tonight' then 'Human' then 'upcoming outing'): the same situation re-described must reuse the same subject so it reads as one ongoing situation instead of a new one. Never use a bare person's name ('Scar', 'Human', 'Scarlet') as the subject.\n"
    "SEPARATELY, report the circumstances this exchange shows have ENDED or been "
    "CONTRADICTED, and every filed note this exchange shows is now STALE. When "
    "the human says a standing circumstance is over, has passed, or is no longer "
    "true ('I'm not sore any more', 'the appointment was yesterday', 'that got "
    "cancelled'), list that circumstance's subject under 'ended', worded exactly "
    "as it was filed before, so the standing note for it can be retired. The same "
    "channel is what drops a note that is simply OUT OF DATE: the notes "
    "currently filed are listed for you under 'filed notes', and any of them the "
    "exchange shows is wrong or already past (the event it describes has "
    "happened, the day it calls 'tomorrow' is over, the human corrects it: 'the "
    "wedding was two days ago, that note is stale') must be listed under "
    "'ended' with its subject copied from that list character for character. A "
    "note whose validity window has not run out yet is retired by NOTHING else, "
    "so a stale filed note that goes unreported keeps being told to you as "
    "current fact. Report it even when the turn has no new circumstance to note.\n"
    "A circumstance the human DENIES, corrects or merely jokes about ('I am not "
    "going on a trip', 'that was a joke') is not a circumstance: never write a "
    "note that asserts it, even when the persona's reply repeated it. Retire the "
    "filed note under 'ended' instead. Each filed note carries a reference like "
    "[n3]; giving that reference in 'ended' instead of the subject is the safest "
    "way to name it, because a reworded or translated subject cannot be matched.\n"
    "A summary outlives the turn that wrote it and is read again on later days, "
    "so it must carry the ABSOLUTE date or time it refers to ('the wedding took "
    "place on 2026-09-21'), and never a bare relative word ('today', 'tomorrow', "
    "'tonight', 'this morning', 'yesterday'): 'tomorrow' written today is a false "
    "claim tomorrow.\n"
    'Return ONLY a JSON object: {"notes": [{"note_type": ..., "subject": ..., '
    '"summary": ..., "priority": 0, "confidence": 0.5, '
    '"valid_from": "2026-05-05T00:00:00+00:00", '
    '"valid_until": "2026-05-12T00:00:00+00:00"}], "ended": ["<subject or [nN] reference '
    'of a note that is now over>"]} — return an empty notes list and an empty ended list '
    "when nothing time-bounded was said."
)


class DebriefSituationalNotesPlugin:
    display_name = display_name

    # How many filed notes the extraction prompt is shown. The list exists so a
    # correction can name a standing note by its own wording; it is bounded
    # because it rides on every debrief turn.
    _FILED_NOTES_IN_PROMPT = 12

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

    @staticmethod
    def _extract_ended_subjects(parsed: Any) -> List[str]:
        """Pull the subjects this exchange shows have ended.

        A separate channel from ``notes`` on purpose: an ended circumstance needs
        no new note, and expressing it as one would leave a row to store and a
        degenerate validity window to invent. The subject is matched against the
        active notes in the store, and that match is what retires them.
        """
        if not isinstance(parsed, dict):
            return []
        raw = parsed.get("ended")
        if raw is None:
            raw = parsed.get("resolved")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return []
        subjects: List[str] = []
        for item in raw:
            if isinstance(item, str) and item.strip():
                subjects.append(item.strip())
            elif isinstance(item, dict):
                subject = item.get("subject") or item.get("ref") or item.get("id")
                if isinstance(subject, str) and subject.strip():
                    subjects.append(subject.strip())
        return subjects

    async def _parse_notes_output(
        self,
        *,
        llm_text: str,
        context: Dict[str, Any],
        original_message: Any,
    ) -> tuple[List[Dict[str, Any]], List[str]]:
        """Extract note dicts and ended subjects, correcting broken JSON once."""
        try:
            parsed, metadata = extract_json_from_text(llm_text, return_metadata=True)
        except Exception as exc:
            log_debug(
                f"[debrief_situational_notes] JSON extraction failed, "
                f"will ask corrector: {exc}"
            )
            parsed, metadata = None, {}

        candidates = self._extract_note_candidates(parsed)
        ended = self._extract_ended_subjects(parsed)
        if candidates or ended:
            return candidates, ended
        # An empty but well-formed answer is the normal "nothing temporal was
        # said" case; only bother the corrector when the payload looked broken.
        if isinstance(parsed, (dict, list)) and not (
            metadata.get("had_errors") or metadata.get("had_extra_text")
        ):
            return [], []

        corrected_text = await run_corrector_middleware(
            text=llm_text,
            bot=None,
            context=context,
            chat_id=getattr(original_message, "chat_id", None),
            thread_id=getattr(original_message, "thread_id", None),
        )
        if not isinstance(corrected_text, str) or not corrected_text.strip():
            return [], []
        try:
            corrected = extract_json_from_text(corrected_text, return_metadata=False)
        except Exception as exc:
            log_warning(
                f"[debrief_situational_notes] corrected output still not JSON: {exc}"
            )
            return [], []
        return self._extract_note_candidates(corrected), self._extract_ended_subjects(
            corrected
        )

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

    @staticmethod
    def _short_summary(summary: Any, limit: int = 200) -> str:
        """One line, bounded: the filed list is prompt budget, not an archive."""
        text = " ".join(str(summary or "").split())
        if len(text) <= limit:
            return text
        return text[: limit - 3].rstrip() + "..."

    @staticmethod
    def _filed_window(note: Any) -> str:
        """The note's own validity window, absolute, as the extractor reads it."""
        start = (
            note.valid_from.isoformat() if getattr(note, "valid_from", None) else "?"
        )
        end = (
            note.valid_until.isoformat()
            if getattr(note, "valid_until", None)
            else "open"
        )
        return f"{start} -> {end}"

    @classmethod
    def _select_filed_notes(
        cls, active: List[Any], exchange_text: str
    ) -> tuple[List[Any], int]:
        """Pick the filed notes the extractor is shown, most relevant first.

        The store can hold far more active notes than the prompt may carry (64 were
        active live), and the extractor can only retire a note it is shown. Taking
        the first N by priority therefore hid exactly the stale notes a correction
        was about: ranking by token overlap with the exchange keeps the notes the
        human and the persona just talked about in the list, whatever their
        priority. Ties keep the store's own order. Returns ``(shown, hidden_count)``.
        """
        from core.soul.situational import subject_tokens

        exchange = subject_tokens(exchange_text)
        scored = []
        for index, note in enumerate(active):
            note_tokens = subject_tokens(f"{note.subject} {note.summary}")
            scored.append((-len(exchange & note_tokens), index, note))
        scored.sort(key=lambda item: (item[0], item[1]))
        shown = [note for _score, _index, note in scored[: cls._FILED_NOTES_IN_PROMPT]]
        return shown, max(len(active) - len(shown), 0)

    async def _filed_notes_block(
        self, exchange_text: str = ""
    ) -> tuple[str, Dict[str, str]]:
        """Render the notes standing for the human, for the extraction prompt.

        Returns ``(block, refs)`` where ``refs`` maps the reference printed in
        front of each line (``n1``, ``n2`` ...) to the note's id.

        The prompt asked the model to report a circumstance that has ended
        "worded exactly as it was filed before" while never showing it the filed
        wording, so a correction could only ever be a guess. Live (2026-09-23):
        the human said the wedding had happened two days earlier, the debrief
        filed the correction under "wedding ceremony in the kitchen", and the
        standing rows "wedding day" and "wedding tomorrow" ("The wedding is
        tomorrow (2026-09-23)") stayed active and were injected on the very day
        they still called tomorrow, because nothing in the store could connect
        the correction to them.

        The references exist for the same reason one level up: a subject copied
        "character for character" still failed whenever the model reworded or
        translated it (an Italian conversation about a note filed in English), and
        that failure was silent. A reference cannot be misspelt into a mismatch.

        Fail-safe: no repository, or a failing lookup, renders no block and the
        extraction carries on exactly as it did before.
        """
        repository = self._get_soul_repository()
        if repository is None:
            return "", {}
        from core.soul.models import now_utc

        try:
            active = await repository.list_active_situational_notes(now=now_utc())
        except Exception as exc:
            log_debug(f"[debrief_situational_notes] filed-note lookup failed: {exc}")
            return "", {}
        if not active:
            return "", {}
        shown, hidden = self._select_filed_notes(active, exchange_text)
        refs: Dict[str, str] = {}
        lines = []
        for position, note in enumerate(shown, start=1):
            ref = f"n{position}"
            refs[ref] = note.id
            lines.append(
                f"- [{ref}] {note.subject} | {self._filed_window(note)} | "
                f"{self._short_summary(note.summary)}"
            )
        if hidden:
            lines.append(f"({hidden} more filed notes not shown)")
        return (
            "filed notes (what is currently standing for the human; a note this "
            "exchange shows is out of date must be retired by copying its subject "
            "exactly, or its [nN] reference, into 'ended'):\n" + "\n".join(lines)
        ), refs

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
                keep_id = await repository.upsert_situational_note(note)
                stored += 1
            except Exception as exc:
                log_warning(
                    f"[debrief_situational_notes] could not store a note: {exc}"
                )
                continue
            superseded = await self._supersede_older_accounts(
                repository, note, keep_id=keep_id
            )
            if superseded:
                log_info(
                    f"[debrief_situational_notes] superseded {superseded} older "
                    f"note(s) about '{note.subject}'"
                )
        return stored

    async def _supersede_older_accounts(
        self, repository: Any, note: Any, *, keep_id: Any = None
    ) -> int:
        """Retire the active notes that this new account replaces.

        Every debrief cycle re-describes the circumstances of the last turn, and a
        note's id is derived from its own text (note_type + subject + summary), so
        a rephrasing used to add a row and leave the older account active: the
        store reached twelve active notes for one evening gathering, contradicting
        each other ("happened last night and went fine" next to "expected
        tonight"). The newest account of a circumstance wins; the older rows are
        marked ``superseded`` (never deleted) and stop being injected, because both
        the active-note query and the prompt block read ``status = 'active'`` only.

        Fail-safe: a lookup or update error is logged and never breaks the store.
        """
        from core.soul.models import now_utc
        from core.soul.situational import is_same_circumstance, subject_tokens

        tokens = subject_tokens(note.subject)
        if not tokens:
            return 0
        try:
            active = await repository.list_active_situational_notes(now=now_utc())
        except Exception as exc:
            log_debug(f"[debrief_situational_notes] supersede lookup failed: {exc}")
            return 0

        superseded = 0
        for other in active:
            # Never retire the note we just stored: by identity in-process, and by
            # the id the repository derived for it.
            if other is note:
                continue
            if keep_id and other.id == keep_id:
                continue
            if note.id and other.id == note.id:
                continue
            if not is_same_circumstance(tokens, subject_tokens(other.subject)):
                continue
            try:
                await repository.resolve_situational_note(
                    other.id, new_status="superseded"
                )
                superseded += 1
            except Exception as exc:
                log_warning(
                    f"[debrief_situational_notes] could not supersede {other.id}: {exc}"
                )
        return superseded

    @staticmethod
    def _as_reference(item: str, refs: Dict[str, str]) -> str | None:
        """The note id a reference such as ``[n3]``, ``n3`` or ``#3`` names."""
        import re

        match = re.fullmatch(r"\[?\s*[#n]?\s*(\d{1,3})\s*\]?", item.strip(), re.I)
        if not match:
            return None
        return refs.get(f"n{int(match.group(1))}")

    async def _retire_ended(
        self,
        repository: Any,
        subjects: List[str],
        refs: Dict[str, str] | None = None,
    ) -> tuple[int, List[set[str]]]:
        """Resolve the active notes whose circumstance this turn shows has ended.

        Nothing retired a note when the human contradicted it: a note's id is
        derived from its own text, so re-describing a circumstance writes a new
        row, and ``resolve_situational_note`` only ran when a replacement note
        happened to be written about the same subject. Live consequence
        (2026-09-19): a soreness STATE note was contradicted twice, at 11:27 and
        again at 13:20, stayed ``active`` with ten hours of validity left, and was
        asserted at 15:16 as present-tense fact ("you're sore, remember? So it's
        hands and mouth only tonight").

        An item naming a ``[nN]`` reference resolves that note directly. Any other
        item is matched by the same blunt subject-token containment as
        superseding, so a subject naming a person can never resolve a real
        circumstance. The store keeps every row; only the status changes, so
        nothing is deleted. Fail-safe: a lookup or update error is logged and never
        raised.

        Returns ``(retired_count, blocked)`` where ``blocked`` holds the subject
        tokens of everything this turn declared over, so the caller can refuse to
        re-file the same circumstance in the same turn. An item that matched no
        active note is logged: that used to be completely silent, which is how a
        correction repeated "several times" could change nothing.
        """
        from core.soul.models import now_utc
        from core.soul.situational import is_same_circumstance, subject_tokens

        refs = refs or {}
        by_ref: set[str] = set()
        by_subject: List[tuple[str, set[str]]] = []
        for item in subjects:
            ref_id = self._as_reference(item, refs)
            if ref_id:
                by_ref.add(ref_id)
                continue
            tokens = subject_tokens(item)
            if tokens:
                by_subject.append((item, tokens))
        if not by_ref and not by_subject:
            return 0, []

        try:
            active = await repository.list_active_situational_notes(now=now_utc())
        except Exception as exc:
            log_debug(f"[debrief_situational_notes] retire lookup failed: {exc}")
            return 0, []

        retired = 0
        blocked: List[set[str]] = [tokens for _item, tokens in by_subject]
        matched_items: set[str] = set()
        for note in active:
            note_tokens = subject_tokens(note.subject)
            hit = note.id in by_ref
            for item, tokens in by_subject:
                if is_same_circumstance(tokens, note_tokens):
                    hit = True
                    matched_items.add(item)
            if not hit:
                continue
            try:
                await repository.resolve_situational_note(
                    note.id, new_status="resolved"
                )
                retired += 1
                if note_tokens:
                    blocked.append(note_tokens)
            except Exception as exc:
                log_warning(
                    f"[debrief_situational_notes] could not resolve {note.id}: {exc}"
                )
        unmatched = [item for item, _tokens in by_subject if item not in matched_items]
        stale_refs = by_ref - {note.id for note in active}
        if unmatched or stale_refs:
            log_warning(
                "[debrief_situational_notes] ended item(s) matched no active note: "
                f"{unmatched + sorted(stale_refs)!r} "
                f"(active subjects: {[note.subject for note in active][:20]!r})"
            )
        return retired, blocked

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
        filed_block, filed_refs = await self._filed_notes_block(
            f"{user_message}\n{llm_response}"
        )
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
        if filed_block:
            user_prompt = f"{user_prompt}\n\n{filed_block}"

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

        candidates, ended_subjects = await self._parse_notes_output(
            llm_text=llm_text,
            context=context,
            original_message=original_message,
        )
        if not candidates and not ended_subjects:
            return None

        session_id = context.get("session_id") or getattr(
            original_message, "session_id", None
        )
        # Retire before storing: a circumstance the turn showed has ended must not
        # survive as an active note, even if storing the new notes then fails.
        blocked: List[set[str]] = []
        if ended_subjects:
            repository = self._get_soul_repository()
            if repository is not None:
                retired, blocked = await self._retire_ended(
                    repository, ended_subjects, filed_refs
                )
                if retired:
                    log_info(
                        f"[debrief_situational_notes] retired {retired} note(s) this "
                        "turn showed have ended"
                    )
        if blocked:
            # The persona's reply usually repeats the very claim the human just
            # denied, so the extractor can re-file it in the same breath it
            # retires it. A circumstance declared over this turn is not stored.
            from core.soul.situational import is_same_circumstance, subject_tokens

            kept = []
            for candidate in candidates:
                tokens = subject_tokens(candidate.get("subject"))
                if tokens and any(is_same_circumstance(tokens, b) for b in blocked):
                    log_info(
                        "[debrief_situational_notes] not re-filing a circumstance "
                        f"this turn declared over: {candidate.get('subject')!r}"
                    )
                    continue
                kept.append(candidate)
            candidates = kept
        stored = await self._store_notes(candidates, session_id)
        if stored:
            log_info(f"[debrief_situational_notes] Stored {stored} situational note(s)")
        return None


PLUGIN_CLASS = DebriefSituationalNotesPlugin
