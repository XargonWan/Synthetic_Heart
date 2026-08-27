"""LLM-compiled Digital Soul Profile (DSP) builder and extractor.

This module implements :class:`LlmDspBuilder`, an LLM-backed implementation of the
``DspBuilder`` protocol (``core/soul/compiler.py``). It turns the daily DSP
extractions (``core/soul/models.py``) into a compact, natural third-person
biography of the user, wrapped in ``<user_profile>...</user_profile>``.

It also implements :class:`LlmDspExtractor`, an LLM-backed implementation of the
``DspExtractor`` protocol (``core/soul/compiler.py``) that reads the raw daily
transcript and pulls stable biographical facts with the same DSP-scope engine and
the same deterministic fallback guarantees. Because the transcript is judged by
an LLM, the aggressive roleplay regex filter is not required on this path.

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
"""

from __future__ import annotations

from datetime import date
from typing import Any

from core.json_utils import extract_json_from_text
from core.logging_utils import log_debug, log_warning

from .models import DspExtraction
from .schemas import DspExtractionModel

__all__ = ["LlmDspBuilder", "LlmDspExtractor"]


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
    ) -> None:
        """Build the LLM DSP extractor.

        Args:
            fallback: rule-based extractor used on any failure path. Defaults to a
                lazily-imported ``RuleBasedDspExtractor``.
            resolve_engine: injectable async callable ``() -> engine | None``
                used for tests. ``None`` uses the DSP-scope Cortex resolver.
            max_transcript_chars: tail-budget for the transcript fed to the LLM
                (most recent characters are kept).
        """
        if fallback is None:
            from core.soul.strategies import RuleBasedDspExtractor

            fallback = RuleBasedDspExtractor()
        self._fallback: Any = fallback
        self.resolve_engine: Any | None = resolve_engine
        self.max_transcript_chars: int = max_transcript_chars

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
            "in-character roleplay (erotic fiction, pet names, emote fills like "
            "'mmwah'/'heheh', speech addressed at the persona).\n"
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
            'Return ONLY a JSON object: {"user_facts": [...], "user_preferences": '
            '[...], "ai_self_facts": [...]} — each a list of short strings; empty '
            "lists when nothing biographical was said."
        )
