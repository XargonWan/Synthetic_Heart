"""LLM-compiled Digital Soul Profile (DSP) builder and extractor, plus the
LLM-distilled MemCell extractor.

This module implements :class:`LlmDspBuilder`, an LLM-backed implementation of the
``DspBuilder`` protocol (``core/soul/compiler.py``). It turns the daily DSP
extractions (``core/soul/models.py``) into a compact, natural third-person
biography of the user, wrapped in ``<user_profile>...</user_profile>``.

It also implements :class:`LlmDspExtractor`, an LLM-backed implementation of the
``DspExtractor`` protocol (``core/soul/compiler.py``) that reads the raw daily
transcript and pulls stable biographical facts with the same DSP-scope engine and
the same deterministic fallback guarantees. Because the transcript is judged by
an LLM, the aggressive roleplay regex filter is not required on this path.

And it implements :class:`LlmMemCellExtractor`, an LLM-backed implementation of
the ``MemCellExtractor`` protocol. The deterministic extractor can only store the
conversation text verbatim as a cell's ``episodic_trace``, so recall could only
ever return raw transcript; this one distils the session into self-contained
memory entries with ``subject|predicate|object`` facts, and falls back to the
deterministic extractor whenever the model is unavailable.

Design:

* **Structural stability, LLM phrasing.** Stability is decided by the same
  recurrence counting the deterministic :class:`RuleBasedDspBuilder` uses
  (``MIN_STABLE_OCCURRENCES``): facts are grouped by exact string equality and
  every distinct fact is surfaced to the LLM *with* its occurrence count, while
  the prompt tells the model that only facts with ``occurrences >= 2`` are
  standing attributes. The LLM never decides *what* is stable, only *how* to
  phrase it.
* **Self-heal on update.** ``build_update`` reviews the current profile and the
  new extractions together, drops conversation-shaped content (one-off
  "User says/wants/needs..." status speech, roleplay dialogue, filler), merges
  genuinely new stable facts and resolves contradictions toward the most recent
  evidence. If the LLM output is effectively identical to the current profile it
  is returned untouched; a model that wraps its own ``<user_profile>`` tags gets
  them stripped before re-wrapping.
* **Deterministic fallback on any failure.** Engine unavailable, an exception
  during the LLM call, a bad JSON parse or an empty biography all delegate to the
  rule-based builder, so the SOUL nightly rollup can never break. On quiet days
  with no stable signal the rule-based path sanitises (rather than wipes) the
  existing profile.
* **Determinism stays where it earns its keep.** The MemCell extractor takes only
  *content* from the model (the trace and the facts). Emotion and foresight are
  still inferred by the deterministic rules, and a structurally unusable answer
  (no engine, an exception, no JSON, nothing that survives the verbatim check)
  defers to the deterministic extractor, so distillation can never lose a session
  that the previous path would have stored.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from core.json_utils import extract_json_from_text
from core.logging_utils import log_debug, log_warning

from .models import (
    DspExtraction,
    emotional_intensity,
    emotional_valence,
    top_emotion,
)
from .schemas import DspExtractionModel, EmotionalTagModel, MemCellExtractionModel

__all__ = ["LlmDspBuilder", "LlmDspExtractor", "LlmMemCellExtractor"]


async def resolve_dsp_engine(resolve_engine: Any | None = None) -> Any | None:
    """Resolve the DSP-scope Cortex engine (fail-safe).

    Honors an injected async ``resolve_engine`` when provided; otherwise resolves
    ``scope="dsp"`` via ``get_active_cortex_engine`` + the Cortex registry,
    mirroring the vessel diary compactor. Returns ``None`` on ANY failure.
    """
    if resolve_engine is not None:
        try:
            return await resolve_engine()
        except Exception as exc:
            log_warning(f"[dsp_llm] injected engine resolver failed: {exc}")
            return None
    try:
        from core.config import get_active_cortex_engine
        from core.cortex_registry import get_cortex_registry
    except Exception as exc:
        log_debug(f"[dsp_llm] cortex imports unavailable: {exc}")
        return None
    try:
        active_cortex = await get_active_cortex_engine(scope="dsp")
    except Exception as exc:
        log_debug(f"[dsp_llm] get_active_cortex_engine failed: {exc}")
        return None
    registry = get_cortex_registry()
    engine = registry.get_engine(active_cortex)
    if engine is None:
        try:
            engine = registry.load_engine(active_cortex)
        except Exception as exc:
            log_warning(
                f"[dsp_llm] could not load Cortex engine '{active_cortex}': {exc}"
            )
            return None
    return engine


async def resolve_dsp_scope_model() -> str | None:
    """Resolve the DSP-scope per-engine model override, or ``None``.

    Honors the optional ``{"engine": ..., "model": ...}`` form of ``DSP_CORTEX``
    so a scope-pinned model is used for DSP compilation. Fail-safe: any error
    (config not loaded, engine gone) yields ``None`` and callers fall back to
    the endpoint's default model.
    """
    try:
        from core.config import get_active_cortex_scope

        _, model = await get_active_cortex_scope(scope="dsp")
        return model
    except Exception as exc:
        log_debug(f"[dsp_llm] scope model resolution failed: {exc}")
        return None


def _speaker_identity_block(declared: str) -> str:
    """Rules that stop one speaker's identity being carried onto another.

    The memcell extractor was told only that "the transcript labels every line
    with its speaker", and nothing about who those speakers ARE. Measured live on
    2026-09-20: a session whose human is a man came back paraphrased with him as
    "she/her" throughout (60 of the 260 cells carrying his name used feminine
    pronouns), because the model guessed a gender instead of reading one. The
    transcript that session handed over carried only the human's own lines, and
    although it said "my wifey", nothing in the prompt stated that the person
    being written about is a man, so the model invented a woman.

    A declaration from the operator (``SOUL_SPEAKER_IDENTITIES``) is authoritative
    and stated outright, which is the only way an extractor can be right about a
    person the transcript never genders.
    """
    block = (
        "SPEAKER IDENTITY (critical): every speaker in the transcript is a "
        "SEPARATE person with their own name, gender and relationship to the "
        "persona. A speaker's gender and pronouns are never the persona's and "
        "never another speaker's: use 'he' only for a man and 'she' only for a "
        "woman, and never guess which one a person is.\n"
        "Take a person's pronouns from what the transcript or the participant "
        "context actually establishes about them - the human's own line calling "
        "someone 'my wife' or 'my husband', or the persona addressing them, is "
        "evidence. Where nothing establishes a person's gender, refer to them by "
        "name rather than inventing one, and never change a person's gender "
        "between entries of the same session.\n"
    )
    cleaned = " ".join(str(declared or "").split())
    if cleaned:
        block += (
            "The people involved, as declared by the deployment (authoritative; "
            f"follow it exactly): {cleaned}.\n"
        )
    return block


class LlmDspBuilder:
    """LLM-compiled DSP builder with a deterministic rule-based fallback.

    Implements the ``DspBuilder`` protocol. The LLM compiles a natural biography
    from structurally-stable facts; any failure path (no engine, exception, bad
    JSON, empty output) delegates to ``RuleBasedDspBuilder``.
    """

    MIN_STABLE_OCCURRENCES = 2

    def __init__(
        self,
        *,
        fallback: Any | None = None,
        max_words: int = 150,
        resolve_engine: Any | None = None,
    ) -> None:
        """Build the LLM DSP builder.

        Args:
            fallback: rule-based builder used on any failure path. Defaults to a
                lazily-imported ``RuleBasedDspBuilder`` (avoiding an import
                cycle with ``core.soul.compiler``).
            max_words: word budget capping the LLM biography.
            resolve_engine: injectable async callable ``() -> engine | None``
                used for tests. ``None`` uses the DSP-scope Cortex resolver.
        """
        if fallback is None:
            from core.soul.compiler import RuleBasedDspBuilder

            fallback = RuleBasedDspBuilder()
        self._fallback: Any = fallback
        self.max_words: int = max_words
        self.resolve_engine: Any | None = resolve_engine

    async def _resolve_engine(self) -> Any | None:
        """Resolve the DSP-scope Cortex engine (fail-safe).

        Uses the injected ``resolve_engine`` when provided, otherwise resolves
        ``scope="dsp"`` via ``get_active_cortex_engine`` + the Cortex registry,
        mirroring the vessel diary compactor. Returns ``None`` on ANY failure.
        """
        if self.resolve_engine is not None:
            try:
                return await self.resolve_engine()
            except Exception as exc:
                log_warning(f"[dsp_llm] injected engine resolver failed: {exc}")
                return None
        try:
            from core.config import get_active_cortex_engine
            from core.cortex_registry import get_cortex_registry
        except Exception as exc:
            log_debug(f"[dsp_llm] cortex imports unavailable: {exc}")
            return None
        try:
            active_cortex = await get_active_cortex_engine(scope="dsp")
        except Exception as exc:
            log_debug(f"[dsp_llm] get_active_cortex_engine failed: {exc}")
            return None
        registry = get_cortex_registry()
        engine = registry.get_engine(active_cortex)
        if engine is None:
            try:
                engine = registry.load_engine(active_cortex)
            except Exception as exc:
                log_warning(
                    f"[dsp_llm] could not load Cortex engine '{active_cortex}': {exc}"
                )
                return None
        return engine

    def _stable_profile(
        self, extractions: list[DspExtraction]
    ) -> tuple[list[tuple[str, int]], list[str], list[tuple[str, int]]]:
        """Compute the stable profile evidence for the LLM.

        Returns ``(stable_facts, prefs, self_facts)`` where fact entries are
        ``(text, occurrence_count)`` tuples. ``user_facts`` and ``ai_self_facts``
        keep EVERY distinct fact with its count (recurrence is a hint for the
        prompt, not a filter); preferences are deduped without a recurrence
        requirement. Both are capped to a ``max_words`` word budget.
        """
        if not extractions:
            return [], [], []
        fact_counts: dict[str, int] = {}
        for item in extractions:
            for fact in item.user_facts:
                fact = str(fact or "").strip()
                if fact:
                    fact_counts[fact] = fact_counts.get(fact, 0) + 1
        stable_facts = self._cap_fact_tuples(list(fact_counts.items()), self.max_words)

        prefs: list[str] = []
        for item in extractions:
            for pref in item.user_preferences:
                pref = str(pref or "").strip()
                if pref and pref not in prefs:
                    prefs.append(pref)
        prefs = self._cap_words(prefs, self.max_words)

        self_counts: dict[str, int] = {}
        for item in extractions:
            for fact in item.ai_self_facts:
                fact = str(fact or "").strip()
                if fact:
                    self_counts[fact] = self_counts.get(fact, 0) + 1
        self_facts = self._cap_fact_tuples(list(self_counts.items()), self.max_words)

        return stable_facts, prefs, self_facts

    async def build_initial(self, *, extractions: list[DspExtraction]) -> str:
        """Compile the initial DSP biography from raw extractions."""
        stable_facts, prefs, self_facts = self._stable_profile(extractions)
        if not self._has_stable_signal(stable_facts, prefs):
            return await self._fallback_call(current_dsp=None, extractions=extractions)
        engine = await self._resolve_engine()
        if engine is None:
            return await self._fallback_call(current_dsp=None, extractions=extractions)
        model = await resolve_dsp_scope_model()
        prompt = {
            "input": {
                "type": "dsp_build_initial",
                "payload": {
                    "user_facts": [
                        {"fact": text, "occurrences": n} for text, n in stable_facts
                    ],
                    "user_preferences": prefs,
                    "ai_self_facts": [
                        {"fact": text, "occurrences": n} for text, n in self_facts
                    ],
                },
            },
            "context": {},
            "instructions": self._build_initial_instructions(),
        }
        bio = await self._generate_biography(engine, model, prompt)
        if not bio:
            return await self._fallback_call(current_dsp=None, extractions=extractions)
        bio = self._cap_to_words(bio, self.max_words)
        if not bio:
            return await self._fallback_call(current_dsp=None, extractions=extractions)
        return f"<user_profile>{bio}</user_profile>"

    async def build_update(
        self, *, current_dsp: str, extractions: list[DspExtraction]
    ) -> str:
        """Merge new extractions into the existing DSP, self-healing it."""
        stable_facts, prefs, self_facts = self._stable_profile(extractions)
        if not self._has_stable_signal(stable_facts, prefs):
            return await self._fallback_call(
                current_dsp=current_dsp, extractions=extractions
            )
        engine = await self._resolve_engine()
        if engine is None:
            return await self._fallback_call(
                current_dsp=current_dsp, extractions=extractions
            )
        model = await resolve_dsp_scope_model()
        prompt = {
            "input": {
                "type": "dsp_build_update",
                "payload": {
                    "current_profile": current_dsp,
                    "user_facts": [
                        {"fact": text, "occurrences": n} for text, n in stable_facts
                    ],
                    "user_preferences": prefs,
                    "ai_self_facts": [
                        {"fact": text, "occurrences": n} for text, n in self_facts
                    ],
                },
            },
            "context": {},
            "instructions": self._build_update_instructions(),
        }
        bio = await self._generate_biography(engine, model, prompt)
        if not bio:
            return await self._fallback_call(
                current_dsp=current_dsp, extractions=extractions
            )
        bio = bio.replace("<user_profile>", "").replace("</user_profile>", "").strip()
        bio = self._cap_to_words(bio, self.max_words)
        if not bio:
            return await self._fallback_call(
                current_dsp=current_dsp, extractions=extractions
            )
        rendered = f"<user_profile>{bio}</user_profile>"
        if self._normalize_ws(rendered) == self._normalize_ws(current_dsp or ""):
            return current_dsp
        return rendered

    async def _fallback_call(
        self, *, current_dsp: str | None, extractions: list[DspExtraction]
    ) -> str:
        """Delegate to the rule-based builder (initial or update)."""
        if current_dsp is None:
            return await self._fallback.build_initial(extractions=extractions)
        return await self._fallback.build_update(
            current_dsp=current_dsp, extractions=extractions
        )

    async def _generate_biography(
        self, engine: Any, model: str | None, prompt: dict[str, Any]
    ) -> str | None:
        """Call the engine (with the scope model override) and extract ``biography``."""
        try:
            from core.config import scope_model_override

            with scope_model_override(engine, model):
                raw = await engine.generate_response(prompt)
        except Exception as exc:
            log_warning(f"[dsp_llm] generate_response failed: {exc}")
            return None
        parsed = extract_json_from_text(raw)
        if isinstance(parsed, dict):
            bio = parsed.get("biography")
            if isinstance(bio, str):
                bio = bio.strip()
                if bio:
                    return bio
        log_debug("[dsp_llm] no 'biography' in LLM JSON response")
        return None

    def _build_initial_instructions(self) -> str:
        return (
            "You are compiling a standing user profile ('About the person you're "
            "talking to') for an AI persona.\n"
            "Below are structured facts extracted from recent conversations. Write a "
            "SHORT, natural, third-person biography of the person from the structured "
            "facts. Only facts with occurrences >= 2 are standing attributes - treat "
            "the occurrence count as a recurrence hint, not free text to quote. DROP "
            "anything that reads like a one-off status ('User says/wants/needs...'), "
            "roleplay dialogue, verbatim speech, or conversational filler. Never "
            f"invent anything. Keep it under {self.max_words} words. Plain prose, no "
            "bullet lists, no XML tags. "
            "ATTRIBUTION: this profile describes the HUMAN. Keep the name the human "
            "goes by. Remove any statement that describes the person as an android, "
            "AI, robot or machine, and any name or nickname that belongs to the "
            "persona (a synthetic nickname is the persona's, not the person's), "
            "unless the evidence shows the human saying it about himself. When in "
            "doubt about an attribute, drop it: a missing nickname is harmless, a "
            "wrong identity is not. "
            'Return ONLY a JSON object: {"biography": "<your biography>"}.'
        )

    def _build_update_instructions(self) -> str:
        return (
            "You are maintaining a standing user profile for an AI persona.\n"
            "Review the CURRENT profile below and the newly extracted facts. Keep "
            "stable, still-true facts (occurrences >= 2 are standing). DROP anything "
            "in the current profile that reads like a transcript quote, roleplay "
            "dialogue, one-off status speech ('User says/wants/needs...'), or "
            "conversational filler. Merge genuinely new stable facts. Resolve "
            "contradictions in favour of the most recent evidence. Never invent "
            "anything. Output ONE clean, concise, natural third-person biography "
            "ATTRIBUTION: this profile describes the HUMAN. Keep the name the human "
            "goes by. Remove any statement that describes the person as an android, "
            "AI, robot or machine, and any name or nickname that belongs to the "
            "persona (a synthetic nickname is the persona's, not the person's), "
            "unless the evidence shows the human saying it about himself. When in "
            "doubt about an attribute, drop it: a missing nickname is harmless, a "
            "wrong identity is not. "
            f"(plain prose, no bullet lists, no XML tags). Keep it under {self.max_words} "
            'words. Return ONLY a JSON object: {"biography": "<your biography>"}.'
        )

    @classmethod
    def _has_stable_signal(
        cls, stable_facts: list[tuple[str, int]], prefs: list[str]
    ) -> bool:
        """Return True when the evidence carries a standing profile signal.

        A standing signal is a user preference (an explicit standing request) or
        a user fact that recurred at least ``MIN_STABLE_OCCURRENCES`` times.
        One-off status speech ("User says/wants/needs...") and AI self-facts do
        NOT constitute a standing signal, so a quiet day delegates to the
        rule-based fallback (which sanitises rather than wipes the profile).
        """
        if prefs:
            return True
        return any(count >= cls.MIN_STABLE_OCCURRENCES for _, count in stable_facts)

    @classmethod
    def _cap_fact_tuples(
        cls, facts: list[tuple[str, int]], max_words: int
    ) -> list[tuple[str, int]]:
        capped: list[tuple[str, int]] = []
        word_count = 0
        for text, count in facts:
            fact_words = len(text.split())
            if fact_words <= 0:
                continue
            if word_count + fact_words > max_words:
                break
            capped.append((text, count))
            word_count += fact_words
        return capped

    @staticmethod
    def _cap_words(facts: list[str], max_words: int) -> list[str]:
        capped: list[str] = []
        word_count = 0
        for fact in facts:
            fact_words = len(fact.split())
            if fact_words <= 0:
                continue
            if word_count + fact_words > max_words:
                break
            capped.append(fact)
            word_count += fact_words
        return capped

    @staticmethod
    def _cap_to_words(text: str, max_words: int) -> str:
        """Truncate ``text`` at a word boundary to at most ``max_words`` words."""
        words = text.split()
        if len(words) <= max_words:
            return text
        return " ".join(words[:max_words])

    @staticmethod
    def _normalize_ws(text: str) -> str:
        """Remove all whitespace so effectively-identical profiles compare equal."""
        return "".join(text.split())


class LlmDspExtractor:
    """LLM-backed DSP evidence extractor with a rule-based fallback.

    Implements the ``DspExtractor`` protocol. Reads the raw (bounded) daily
    transcript and asks the DSP-scope Cortex engine to pull stable, factual
    biography about the person being spoken to: name, role, origin/residence,
    age, tastes and standing preferences. Because the transcript is judged by an
    LLM, the aggressive roleplay regex filter is not required on this path — the
    prompt instructs the model to ignore in-character roleplay, pet names and
    emote filler, and a structural post-filter keeps the stored facts clean. Any
    failure (no engine, exception, bad JSON) delegates to
    ``RuleBasedDspExtractor`` so the rollup can never break.
    """

    MAX_FACT_CHARS = 160
    MAX_FACTS = 24
    MAX_TRANSCRIPT_CHARS = 12000

    def __init__(
        self,
        *,
        fallback: Any | None = None,
        resolve_engine: Any | None = None,
        max_transcript_chars: int = 12000,
        speaker_identity: str = "",
    ) -> None:
        """Build the LLM DSP extractor.

        Args:
            fallback: rule-based extractor used on any failure path. Defaults to a
                lazily-imported ``RuleBasedDspExtractor``.
            resolve_engine: injectable async callable ``() -> engine | None``
                used for tests. ``None`` uses the DSP-scope Cortex resolver.
            max_transcript_chars: tail-budget for the transcript fed to the LLM
                (most recent characters are kept).
            speaker_identity: who the people in the log are, as declared by the
                deployment (``SOUL_SPEAKER_IDENTITIES``). This prompt already
                writes the human as "he"; the declaration is what keeps that right
                for a deployment whose human is not a man.
        """
        if fallback is None:
            from core.soul.strategies import RuleBasedDspExtractor

            fallback = RuleBasedDspExtractor()
        self._fallback: Any = fallback
        self.resolve_engine: Any | None = resolve_engine
        self.max_transcript_chars: int = max_transcript_chars
        self.speaker_identity: str = str(speaker_identity or "").strip()

    async def extract_dsp(
        self, *, transcript: str, current_date: date
    ) -> DspExtractionModel:
        """Extract stable biographical facts from the daily transcript."""
        text = str(transcript or "").strip()
        if not text:
            return DspExtractionModel(
                user_facts=[], user_preferences=[], ai_self_facts=[]
            )
        engine = await resolve_dsp_engine(self.resolve_engine)
        if engine is None:
            return await self._fallback.extract_dsp(
                transcript=transcript, current_date=current_date
            )
        model = await resolve_dsp_scope_model()
        bounded = text[-self.max_transcript_chars :]
        prompt = {
            "input": {
                "type": "dsp_extract",
                "payload": {
                    "current_date": str(current_date),
                    "transcript": bounded,
                },
            },
            "context": {},
            "instructions": self._build_extract_instructions(),
        }
        parsed = await self._generate_model(engine, model, prompt)
        if parsed is None:
            return await self._fallback.extract_dsp(
                transcript=transcript, current_date=current_date
            )
        return parsed

    async def _generate_model(
        self, engine: Any, model: str | None, prompt: dict[str, Any]
    ) -> DspExtractionModel | None:
        """Call the engine (with the scope model override) and build a model."""
        try:
            from core.config import scope_model_override

            with scope_model_override(engine, model):
                raw = await engine.generate_response(prompt)
        except Exception as exc:
            log_warning(f"[dsp_llm] extract generate_response failed: {exc}")
            return None
        parsed = extract_json_from_text(raw)
        if not isinstance(parsed, dict):
            log_debug("[dsp_llm] no JSON in extract response")
            return None
        return DspExtractionModel(
            user_facts=self._clean_facts(parsed.get("user_facts")),
            user_preferences=self._clean_prefs(parsed.get("user_preferences")),
            ai_self_facts=self._clean_facts(parsed.get("ai_self_facts")),
        )

    @staticmethod
    def _unwrap_item(item: Any) -> str:
        """Coerce an extracted item (str or single-value dict) to a plain string.

        Some models wrap values as ``{"preference": "..."}`` / ``{"fact": "..."}``
        objects instead of plain strings; known value keys are preferred, then any
        first string value, so both shapes normalize.
        """
        if isinstance(item, dict):
            for key in ("text", "value", "fact", "preference", "preferences"):
                val = item.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
            for val in item.values():
                if isinstance(val, str) and val.strip():
                    return val.strip()
                if isinstance(val, (int, float)):
                    return str(val)
            return ""
        if isinstance(item, (int, float)):
            return str(item)
        return str(item or "").strip()

    @classmethod
    def _clean_facts(cls, raw: Any) -> list[str]:
        """Clean and structurally guard extracted facts (speech-shaped drop)."""
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        for item in raw:
            fact = cls._unwrap_item(item)
            if not fact:
                continue
            # LLM output naturally ends sentences with punctuation; the structural
            # guard rejects trailing .?!, so normalize like the rule-based
            # extractor's _clean_fact_value before the check.
            fact = fact.rstrip(" .,;:!?").strip()
            if not fact:
                continue
            fact = fact[: cls.MAX_FACT_CHARS]
            try:
                from core.soul.strategies import RuleBasedDspExtractor

                if not RuleBasedDspExtractor.is_stable_user_fact(fact):
                    continue
            except Exception:
                pass
            if fact not in out:
                out.append(fact)
            if len(out) >= cls.MAX_FACTS:
                break
        return out

    @classmethod
    def _clean_prefs(cls, raw: Any) -> list[str]:
        """Clean and dedupe extracted preferences (no structural guard needed)."""
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        for item in raw:
            pref = cls._unwrap_item(item)
            if not pref:
                continue
            pref = pref.rstrip(" .,;:!?").strip()
            if not pref:
                continue
            pref = pref[: cls.MAX_FACT_CHARS]
            if pref not in out:
                out.append(pref)
            if len(out) >= cls.MAX_FACTS:
                break
        return out

    def _build_extract_instructions(self) -> str:
        return (
            "You are extracting a standing user profile from a chat log between an "
            "AI persona and its human. The log mixes ordinary conversation with "
            "in-character roleplay banter (pet names, emote fills like "
            "'mmwah'/'heheh', affectionate dialogue addressed at the persona).\n"
            "Extract biographical statements about the human: name, role/occupation, "
            "origin/residence, age, tastes, and standing preferences about how they "
            "want to be talked to or responded to. Extract genuine biography even if "
            "mentioned only once — later consolidation keeps only what recurs across "
            "days.\n"
            "RULES: IGNORE roleplay dialogue, pet names, emote filler, and one-off "
            "STATUS telemetry ('User says/wants/needs...', 'I am fixing it now', "
            "today's mood or plans) — those describe transient states, not who the "
            "user is. Never invent anything. Never copy verbatim quotes. Write each "
            "fact as a short third-person statement starting with 'User' (e.g. 'User "
            "works on SynthHeart', 'User lives in Berlin', 'User prefers concise "
            "technical responses').\n"
            "SPEAKER ATTRIBUTION (critical, the speakers are named in the log): "
            "decide which speaker is the HUMAN (the person this profile is about) "
            "and which is the PERSONA, then attribute each line to its own speaker. "
            "Only what the human says about himself can become a user fact. "
            "The human's own name IS a user fact: record the name he goes by (the "
            "label his own lines carry, or a name used for him by others). "
            "A name or nickname the human GIVES the persona ('you are X', 'X is "
            "you', 'I'll call you X') belongs to the PERSONA, never to the human, "
            "and pet names the human uses for the persona are not the human's own "
            "names. Attributes of the persona (being an android, an AI, a synth, a "
            "machine, having a core or a body, being someone's wife) are the "
            "PERSONA's, never the human's: never describe the user as an android, "
            "AI, robot or machine, and never give the user a name or nickname that "
            "belongs to the persona, unless the human states that about himself in "
            "his own line. When the transcript is in-character and the roles are "
            "ambiguous, extract NOTHING rather than guessing.\n"
            'Return ONLY a JSON object: {"user_facts": [...], "user_preferences": '
            '[...], "ai_self_facts": [...]} — each a list of short strings; empty '
            "lists when nothing biographical was said.\n"
            + _speaker_identity_block(self.speaker_identity)
        )


def _normalise_for_compare(text: Any) -> str:
    """Whitespace-collapsed, lowercased, punctuation-trimmed comparison key."""
    collapsed = " ".join(str(text or "").split()).strip().lower()
    return collapsed.strip(" \t\"'“”„«»…,;:!?-–—.")


def _text_of_item(item: Any) -> str:
    """Coerce an extracted value (string, number or single-value dict) to text.

    Models wrap values as ``{"fact": "..."}`` / ``{"text": "..."}`` objects
    instead of plain strings often enough to normalise both shapes.
    """
    if isinstance(item, dict):
        for key in ("fact", "text", "value", "statement", "memory"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in item.values():
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, (int, float)):
                return str(value)
        return ""
    if isinstance(item, (int, float)):
        return str(item)
    return str(item or "").strip()


class LlmMemCellExtractor:
    """LLM-distilled MemCell extractor with a deterministic fallback.

    Implements the ``MemCellExtractor`` protocol (``core/soul/compiler.py``).
    The deterministic extractor stores the conversation text verbatim as the
    cell's ``episodic_trace``, so a recalled memory could only ever be raw
    transcript, and two statements about the same subject — an old one and the
    one that corrected it — surfaced as equally current entries that read as
    contradictory. This extractor distils instead of copying:

    * **One entry per memory-worthy event**, so a long session does not collapse
      into a single truncated transcript chunk.
    * **The trace is a self-contained paraphrase** of what happened and of what
      changed, written so it never quotes the transcript. A trace that is
      literally contained in the transcript is discarded: that structural check
      is what makes "recall returns distilled knowledge" a property of the stored
      row rather than a trick of the renderer.
    * **Facts are ``subject|predicate|object`` triples**, the shape the
      knowledge-graph fold (``SoulCompiler.async_consolidate``) and the recall
      renderer already expect. A fact that only restates its own trace, and a
      fact that quotes the transcript, are both dropped at the source — so raw
      transcript cannot reach the memory block through the fact list either.
    * **Emotion and foresight stay deterministic**, inferred by the same rules
      the deterministic extractor uses, so distillation moves *content* only.

    Any unusable answer (no engine, an exception, no JSON, or a list of entries
    that are all quotes) defers to the deterministic extractor, so a session that
    the previous path would have stored is never silently dropped. An explicit
    empty answer is respected: if the model judged the session as holding nothing
    durable, no cell is written.
    """

    MAX_CELLS = 4
    MAX_TRACE_CHARS = 480
    MAX_FACT_CHARS = 200
    MAX_FACTS_PER_CELL = 4
    # Below this length a containment match is ambiguous — a short distilled
    # statement can share wording with the transcript by accident — so the
    # verbatim check only applies to traces long enough for a copy to be
    # deliberate.
    MIN_VERBATIM_CHECK_CHARS = 40
    MAX_TRANSCRIPT_CHARS = 12000
    # Marker read by ``SoulCompiler.post_session_compile``: cells this extractor
    # writes carry a distillation stamp, and the WebUI re-distil pass targets the
    # ones that do not. The deterministic extractor leaves the attribute absent.
    distils_content = True

    def __init__(
        self,
        *,
        fallback: Any | None = None,
        resolve_engine: Any | None = None,
        max_transcript_chars: int = 12000,
        speaker_identity: str = "",
    ) -> None:
        """Build the LLM MemCell extractor.

        Args:
            fallback: deterministic extractor used whenever the model cannot be
                trusted. Defaults to a lazily-imported
                ``RuleBasedMemCellExtractor``.
            resolve_engine: injectable async callable ``() -> engine | None``
                used for tests. ``None`` uses the DSP-scope Cortex resolver.
            max_transcript_chars: tail-budget for the transcript fed to the LLM
                (the most recent characters are kept).
            speaker_identity: who the people in the transcript are, as declared
                by the deployment (``SOUL_SPEAKER_IDENTITIES``). Stated to the
                model outright so a person the transcript never genders cannot be
                guessed at.
        """
        from core.soul.strategies import RuleBasedMemCellExtractor

        if fallback is None:
            fallback = RuleBasedMemCellExtractor()
        self._fallback: Any = fallback
        # Tagging stays deterministic and identical to the rule-based path.
        self._tagger = RuleBasedMemCellExtractor()
        self.resolve_engine: Any | None = resolve_engine
        self.max_transcript_chars: int = max_transcript_chars
        self.speaker_identity: str = str(speaker_identity or "").strip()

    async def extract_memcells(
        self, *, transcript: str, current_date: date
    ) -> list[MemCellExtractionModel]:
        """Distil the session transcript into memory cells."""
        text = str(transcript or "").strip()
        if not text:
            return []
        engine = await resolve_dsp_engine(self.resolve_engine)
        if engine is None:
            return await self._fallback_cells(
                transcript=transcript, current_date=current_date
            )
        # An injected engine means the caller owns the model choice (a standalone
        # pass, a test), so the scope lookup is skipped instead of failing loudly.
        model = (
            None if self.resolve_engine is not None else await resolve_dsp_scope_model()
        )
        prompt = {
            "input": {
                "type": "memcell_extract",
                "payload": {
                    "current_date": str(current_date),
                    "transcript": text[-self.max_transcript_chars :],
                },
            },
            "context": {},
            "instructions": self._build_extract_instructions(),
        }
        memories = await self._generate_cells(engine, model, prompt)
        if memories is None:
            return await self._fallback_cells(
                transcript=transcript, current_date=current_date
            )
        cells = self._clean_cells(memories, transcript=text, current_date=current_date)
        if not cells and memories:
            # The model answered, but nothing it wrote was a distillation (it
            # quoted the transcript, or every entry was empty). That is as
            # unusable as a failed call: record the session deterministically
            # rather than lose it.
            log_warning(
                "[soul_llm] memcell extraction returned no usable distillation "
                f"({len(memories)} entries); using the deterministic extractor"
            )
            return await self._fallback_cells(
                transcript=transcript, current_date=current_date
            )
        if not cells:
            log_debug(
                "[soul_llm] memcell extraction found nothing durable "
                f"({len(text)} transcript chars)"
            )
        return cells

    async def _fallback_cells(
        self, *, transcript: str, current_date: date
    ) -> list[MemCellExtractionModel]:
        """Deterministic cells, used whenever the model cannot be trusted."""
        try:
            return await self._fallback.extract_memcells(
                transcript=transcript, current_date=current_date
            )
        except Exception as exc:
            log_warning(f"[soul_llm] deterministic memcell fallback failed: {exc}")
            return []

    async def _generate_cells(
        self, engine: Any, model: str | None, prompt: dict[str, Any]
    ) -> Any | None:
        """Ask the engine for the session's memories; ``None`` when unusable."""
        try:
            from core.config import scope_model_override

            with scope_model_override(engine, model):
                raw = await engine.generate_response(prompt)
        except Exception as exc:
            log_warning(f"[soul_llm] memcell extract generate_response failed: {exc}")
            return None
        parsed = extract_json_from_text(raw)
        if isinstance(parsed, list):
            return parsed
        if isinstance(parsed, dict):
            for key in ("memories", "cells", "memcells", "entries"):
                value = parsed.get(key)
                if isinstance(value, list):
                    return value
        log_debug("[soul_llm] no usable JSON in memcell extract response")
        return None

    def _clean_cells(
        self,
        raw: list[Any],
        *,
        transcript: str,
        current_date: date,
    ) -> list[MemCellExtractionModel]:
        """Turn the model's entries into validated cells, dropping quotes."""
        transcript_key = _normalise_for_compare(transcript)
        # Only the DATED signals carry content ("Upcoming user event around
        # 2026-09-19"). The relative-time markers are boilerplate - "Potential
        # follow-up implied by phrase 'tonight'" - which the prompt then showed
        # verbatim in every block, so they are dropped here rather than injected.
        foresight = [
            signal
            for signal in self._tagger.extract_foresight_signals(
                transcript, current_date
            )
            if signal.trigger != "relative_time_mention"
        ]
        now = datetime.now(timezone.utc)
        cells: list[MemCellExtractionModel] = []
        for index, item in enumerate(raw):
            trace = self._clean_trace(item)
            if not trace:
                continue
            trace_key = _normalise_for_compare(trace)
            if self._is_verbatim(trace_key, transcript_key):
                log_debug(
                    "[soul_llm] dropped a memcell trace that quotes the transcript"
                )
                continue
            cells.append(
                MemCellExtractionModel(
                    episodic_trace=trace,
                    atomic_facts=self._clean_facts(item, trace_key, transcript_key),
                    emotional_tag=self._emotional_tag(trace),
                    # Session-level signals (they come from the transcript's own
                    # dates and relative-time cues) ride on the first cell.
                    foresight_signals=[] if cells else foresight,
                    # Distinct microsecond timestamps: the cell id is derived from
                    # the timestamp, so identical ones would collide and upsert
                    # over each other.
                    timestamp=now + timedelta(microseconds=index),
                )
            )
            if len(cells) >= self.MAX_CELLS:
                break
        return cells

    @classmethod
    def _clean_trace(cls, item: Any) -> str:
        """Extract and bound one memory's trace text (``""`` when unusable)."""
        raw_trace = item if isinstance(item, str) else ""
        if isinstance(item, dict):
            for key in ("trace", "episodic_trace", "summary", "memory", "text"):
                value = item.get(key)
                if isinstance(value, str) and value.strip():
                    raw_trace = value
                    break
        text = " ".join(str(raw_trace or "").split())
        if len(text) < 8:
            return ""
        return cls._cap_trace(text)

    @classmethod
    def _cap_trace(cls, text: str) -> str:
        """Truncate a trace at a sentence, else a word, boundary."""
        if len(text) <= cls.MAX_TRACE_CHARS:
            return text
        cut = text[: cls.MAX_TRACE_CHARS]
        for sep in (". ", "! ", "? "):
            index = cut.rfind(sep)
            if index >= cls.MAX_TRACE_CHARS // 2:
                return cut[: index + 1].strip()
        index = cut.rfind(" ")
        return (cut[:index] if index > 0 else cut).strip()

    @classmethod
    def _clean_facts(cls, item: Any, trace_key: str, transcript_key: str) -> list[str]:
        """Clean and dedupe a memory's facts, dropping restatements and quotes."""
        if not isinstance(item, dict):
            return []
        raw_facts: Any = None
        for key in ("facts", "atomic_facts", "key_facts"):
            value = item.get(key)
            if isinstance(value, list):
                raw_facts = value
                break
        if not raw_facts:
            return []
        facts: list[str] = []
        for entry in raw_facts:
            fact = _text_of_item(entry)
            if not fact:
                continue
            fact = fact[: cls.MAX_FACT_CHARS].strip()
            if not fact or cls._fact_restates_trace(fact, trace_key):
                continue
            # A fact that quotes the transcript is raw transcript surfacing in
            # the memory block, whatever shape the model delivers it in (the
            # deterministic extractor's old ``Conversation|summary|<line>`` is
            # exactly this).
            payload_key = _normalise_for_compare(cls._fact_payload(fact))
            if cls._is_verbatim(payload_key, transcript_key):
                log_debug(
                    "[soul_llm] dropped a memcell fact that quotes the transcript"
                )
                continue
            if fact not in facts:
                facts.append(fact)
            if len(facts) >= cls.MAX_FACTS_PER_CELL:
                break
        return facts

    @staticmethod
    def _fact_payload(fact: str) -> str:
        """The content part of a fact: ``object`` for a triple, else the whole."""
        parts = [part.strip() for part in str(fact or "").split("|") if part.strip()]
        return parts[2] if len(parts) == 3 else str(fact or "")

    @classmethod
    def _fact_restates_trace(cls, fact: str, trace_key: str) -> bool:
        """True when a fact only repeats the trace it is stored beside.

        The same rule the recall renderer applies
        (``SoulPlugin._fact_restates_trace``), enforced at the source and over
        the WHOLE trace — the renderer compares only its first 120 characters,
        which is why a short opening line escaped it live.
        """
        if not trace_key:
            return False
        payload_key = _normalise_for_compare(cls._fact_payload(fact))
        if not payload_key:
            return False
        return payload_key in trace_key or trace_key in payload_key

    @classmethod
    def _is_verbatim(cls, trace_key: str, transcript_key: str) -> bool:
        """True when the trace is lifted out of the transcript verbatim."""
        if len(trace_key) < cls.MIN_VERBATIM_CHECK_CHARS:
            return False
        return trace_key in transcript_key

    def _emotional_tag(self, trace: str) -> EmotionalTagModel:
        """Deterministic emotional tagging, identical to the rule-based path."""
        snapshot = self._tagger.infer_emotion_snapshot(trace)
        intensity = emotional_intensity(snapshot)
        # top_emotion() returns the largest axis, so an all-zero snapshot would
        # label the cell with whichever axis comes first (joy); with no signal at
        # all the honest label is neutral.
        dominant = top_emotion(snapshot) if intensity > 0 else "neutral"
        return EmotionalTagModel(
            state_snapshot=snapshot,
            dominant_emotion=dominant,
            intensity=intensity,
            valence=emotional_valence(snapshot),
        )

    def _build_extract_instructions(self) -> str:
        return (
            "You are distilling what an AI persona must REMEMBER from one session "
            "of chat. The transcript labels every line with its speaker.\n"
            "Write DISTILLED KNOWLEDGE, never a quote: each memory's trace is a "
            "self-contained paraphrase of what happened or was said, so a reader "
            "who never saw the transcript understands it and can tell who did or "
            "said what. Copying a line out of the transcript is a failure.\n"
            "ONE ENTRY PER DISTINCT THING WORTH REMEMBERING: a decision, an "
            "agreement, a correction, a change of state, a plan, a commitment, a "
            "realisation about a person, or a durable preference. Never split one "
            "event across entries and never merge unrelated ones; two entries that "
            "say the same thing are a mistake.\n"
            "WHEN THE SESSION CHANGES AN EARLIER BELIEF OR PLAN, say so in the "
            "trace: state the current truth plainly and name what it replaces "
            "(for example that something was understood one way before and is "
            "understood differently now, or that a plan was cancelled or "
            "superseded). A memory that repeats the old statement without noting "
            "the change is worse than no memory at all.\n"
            "NEVER INVENT: nothing that is not in the transcript, no speculation "
            "about feelings or intentions, no continuation of in-character "
            "roleplay, no decorative mood language.\n"
            "FACTS: each memory carries its facts as triples "
            "'subject|predicate|object', exactly three pipe-separated parts and no "
            "pipes inside a part. subject is who or what the fact is about ('User', "
            "'Synth', or a name as spelled in the transcript); predicate is a short "
            "snake_case verb phrase ('prefers', 'corrected', 'lives_in', "
            "'has_intention'); object is the distilled content, at most 200 "
            "characters, in the same third-person voice as the trace. Every fact "
            "must add something the trace does not already state — use an empty "
            "list when the trace says it all.\n"
            "Keep the persona's own statements and the human's separate; never "
            "attribute one to the other.\n"
            "Use the current_date in the payload to read relative time ('last "
            "night', 'tomorrow') and state absolute dates when a date matters.\n"
            'Return ONLY a JSON object: {"memories": [{"trace": "...", "facts": '
            '["..."]}]} with at most 4 entries, most durable first. Return '
            '{"memories": []} only when the session holds nothing that a later '
            "conversation could need.\n"
            + _speaker_identity_block(self.speaker_identity)
        )
