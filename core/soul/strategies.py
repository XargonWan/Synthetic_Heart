from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime

from .models import emotional_intensity, emotional_valence, top_emotion
from .schemas import (
    DspExtractionModel,
    EmotionalTagModel,
    ForesightSignalModel,
    MemCellExtractionModel,
)


@dataclass(slots=True)
class RuleBasedMemCellExtractor:
    """Fallback extractor used when no dedicated LLM compiler is configured."""

    foresight_horizon_days: int = 14

    async def extract_memcells(
        self, *, transcript: str, current_date: date
    ) -> list[MemCellExtractionModel]:
        text = (transcript or "").strip()
        if not text:
            return []

        facts = self._extract_atomic_facts(text)
        emotion_snapshot = self._infer_emotion_snapshot(text)
        foresight = self._extract_foresight_signals(text, current_date)
        tag = EmotionalTagModel(
            state_snapshot=emotion_snapshot,
            dominant_emotion=top_emotion(emotion_snapshot),
            intensity=emotional_intensity(emotion_snapshot),
            valence=emotional_valence(emotion_snapshot),
        )

        return [
            MemCellExtractionModel(
                episodic_trace=text,
                atomic_facts=facts,
                emotional_tag=tag,
                foresight_signals=foresight,
                timestamp=datetime.now(UTC),
            )
        ]

    def _extract_atomic_facts(self, transcript: str) -> list[str]:
        facts: list[str] = []

        # Date/event mentions become low-confidence but structured triples.
        for match in re.finditer(r"\b(\d{4}-\d{2}-\d{2})\b", transcript):
            iso_date = match.group(1)
            facts.append(f"User|mentioned_date|{iso_date}")

        # Stable intentions.
        intent_patterns = [
            r"\bI (?:want|plan|intend|need) to ([^\.\n]+)",
            r"\bUser (?:wants|plans|intends|needs) to ([^\.\n]+)",
        ]
        for pattern in intent_patterns:
            for match in re.finditer(pattern, transcript, flags=re.IGNORECASE):
                intent = match.group(1).strip()
                if intent:
                    facts.append(f"User|has_intention|{intent}")

        # Fallback: keep one short deterministic summary fact so memcells
        # are not structurally empty when no explicit pattern is matched.
        if not facts:
            first_sentence = re.split(r"[\.\n!?]", transcript, maxsplit=1)[0].strip()
            if first_sentence:
                summary = first_sentence[:160]
                facts.append(f"Conversation|summary|{summary}")

        return list(dict.fromkeys(facts))

    def _infer_emotion_snapshot(self, transcript: str) -> dict[str, float]:
        text = transcript.lower()

        joy_words = ("happy", "excited", "great", "love", "glad")
        fear_words = ("anxious", "afraid", "worried", "nervous", "scared")
        sad_words = ("sad", "lonely", "down", "tired", "hurt")
        anger_words = ("angry", "mad", "frustrated", "annoyed")

        def _score(words: tuple[str, ...]) -> float:
            count = sum(1 for w in words if w in text)
            return min(1.0, count * 0.25)

        return {
            "joy": _score(joy_words),
            "fear": _score(fear_words),
            "sad": _score(sad_words),
            "anger": _score(anger_words),
        }

    def _extract_foresight_signals(
        self, transcript: str, current_date: date
    ) -> list[ForesightSignalModel]:
        foresight: list[ForesightSignalModel] = []
        lower = transcript.lower()

        for match in re.finditer(r"\b(\d{4}-\d{2}-\d{2})\b", transcript):
            iso = match.group(1)
            try:
                future_date = datetime.strptime(iso, "%Y-%m-%d").date()
            except ValueError:
                continue
            delta = (future_date - current_date).days
            if 0 <= delta <= self.foresight_horizon_days:
                foresight.append(
                    ForesightSignalModel(
                        content=f"Upcoming user event around {iso}",
                        valid_until=future_date,
                        trigger="date_mention",
                        emotional_implication={"fear": 0.1, "joy": 0.1},
                    )
                )

        # Relative-time cues that commonly indicate near-future intent.
        relative_markers: list[tuple[str, int]] = [
            ("tomorrow", 1),
            ("next week", 7),
            ("later today", 0),
            ("tonight", 0),
            ("soon", 2),
        ]
        for marker, delta_days in relative_markers:
            if marker not in lower:
                continue
            valid_until = (
                current_date
                if delta_days == 0
                else current_date.fromordinal(current_date.toordinal() + delta_days)
            )
            foresight.append(
                ForesightSignalModel(
                    content=f"Potential follow-up implied by phrase '{marker}'",
                    valid_until=valid_until,
                    trigger="relative_time_mention",
                    emotional_implication={"joy": 0.1, "fear": 0.05},
                )
            )

        return foresight


@dataclass(slots=True)
class RuleBasedDspExtractor:
    """Extract lightweight DSP facts from transcript text."""

    _SELF_SPEAKER_LABELS = frozenset({"self", "synth", "assistant"})

    async def extract_dsp(
        self, *, transcript: str, current_date: date
    ) -> DspExtractionModel:
        del current_date
        text = (transcript or "").strip()
        if not text:
            return DspExtractionModel(
                user_facts=[], user_preferences=[], ai_self_facts=[]
            )

        user_lines, self_lines = self._split_transcript_by_speaker(text)
        user_text = "\n".join(user_lines) if user_lines else text
        self_text = "\n".join(self_lines)

        facts = self._extract_user_facts(user_text)
        prefs = self._extract_preferences(user_text)
        return DspExtractionModel(
            user_facts=facts,
            user_preferences=prefs,
            ai_self_facts=self._extract_ai_self_facts(self_text),
        )

    def _split_transcript_by_speaker(self, text: str) -> tuple[list[str], list[str]]:
        user_lines: list[str] = []
        self_lines: list[str] = []
        structured_lines = 0

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line:
                continue

            match = re.match(
                r"^(?:\[[^\]]+\]\s*)?(?P<speaker>[^:\n]{1,40}):\s*(?P<content>.+?)\s*$",
                line,
            )
            if match is None:
                continue

            structured_lines += 1
            speaker = match.group("speaker").strip().strip("\"'").lower()
            content = self._clean_fact_value(match.group("content"))
            if not content:
                continue

            if speaker in self._SELF_SPEAKER_LABELS:
                self_lines.append(content)
            else:
                user_lines.append(content)

        if structured_lines == 0:
            return ([text] if text else []), []

        return user_lines, self_lines

    def _extract_user_facts(self, text: str) -> list[str]:
        facts = list(self._extract_biographical_facts(text, subject="User"))
        return list(dict.fromkeys(facts))

    def _extract_preferences(self, text: str) -> list[str]:
        prefs: list[str] = []
        preference_patterns = [
            r"\bplease be\s+([^\.\n]+)",
            r"\bI prefer\s+([^\.\n]+)",
            r"\brespond\s+([^\.\n]+)",
        ]

        for pattern in preference_patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                value = match.group(1).strip()
                if value:
                    cleaned = self._clean_fact_value(value)
                    if pattern == r"\bI prefer\s+([^\.\n]+)":
                        cleaned = re.split(
                            r",\s*(?:please\b|respond\b)",
                            cleaned,
                            maxsplit=1,
                            flags=re.IGNORECASE,
                        )[0].strip()
                    if cleaned:
                        prefs.append(cleaned)

        return list(dict.fromkeys(prefs))

    def _extract_ai_self_facts(self, text: str) -> list[str]:
        if not text.strip():
            return []
        return list(dict.fromkeys(self._extract_biographical_facts(text, subject="AI")))

    def _extract_biographical_facts(self, text: str, *, subject: str) -> list[str]:
        """Extract short, bounded biographical facts from ``text``.

        Only *stable* identity facts are worth placing in a standing <user_profile>:
        name, role/occupation, origin, residence, age, and clear tastes. Each
        predicate is anchored and reads only up to the first clause boundary
        (`, . ! ? \\n`), so a long conversational clause — roleplay dialogue,
        one-off status, or stream-of-consciousness — never becomes a standing
        "fact". This is what keeps the DSP a cheat-sheet about the person, not a
        dump of what they recently said.

        ``subject`` labels the fact ("User" or "AI"). Finite-state structural
        matching only; no keyword salience.
        """
        _R = r"[^.,;\n!?]"
        templates: list[tuple[str, str]] = [
            # name
            (
                r"\bmy name is\s+({R}{2,48})".replace("{R}", _R),
                "{subject}'s name is {value}",
            ),
            (
                r"\bI am called\s+({R}{2,48})".replace("{R}", _R),
                "{subject}'s name is {value}",
            ),
            # role / occupation
            (
                r"\bI work on\s+({R}{2,60})".replace("{R}", _R),
                "{subject} works on {value}",
            ),
            (
                r"\bI work as\s+({R}{2,40})".replace("{R}", _R),
                "{subject} works as {value}",
            ),
            (r"\bI am a\s+({R}{2,40})".replace("{R}", _R), "{subject} is a {value}"),
            (r"\bI'm a\s+({R}{2,40})".replace("{R}", _R), "{subject} is a {value}"),
            # origin / residence
            (
                r"\bI am from\s+({R}{2,40})".replace("{R}", _R),
                "{subject} is from {value}",
            ),
            (
                r"\bI'm from\s+({R}{2,40})".replace("{R}", _R),
                "{subject} is from {value}",
            ),
            (
                r"\bI live in\s+({R}{2,40})".replace("{R}", _R),
                "{subject} lives in {value}",
            ),
            # age
            (
                r"\bI am\s+(\d{1,3})\s+years old".replace("{R}", _R),
                "{subject} is {value} years old",
            ),
            (
                r"\bI'm\s+(\d{1,3})\s+years old".replace("{R}", _R),
                "{subject} is {value} years old",
            ),
            # tastes
            (r"\bI like\s+({R}{2,40})".replace("{R}", _R), "{subject} likes {value}"),
            (r"\bI love\s+({R}{2,40})".replace("{R}", _R), "{subject} loves {value}"),
            (
                r"\bI prefer\s+({R}{2,40})".replace("{R}", _R),
                "{subject} prefers {value}",
            ),
        ]

        facts: list[str] = []
        for pattern, template in templates:
            rendered_template = template.replace("{subject}", subject)
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                value = self._clean_fact_value(match.group(1))
                value = self._first_clause(value)
                if value and self._is_bound_fact(value):
                    facts.append(rendered_template.format(value=value))
        return facts

    @staticmethod
    def _first_clause(value: str) -> str:
        """Cut a captured value at a coordinated second clause, if any.

        Biographical predicates should yield one short fact ("User lives in
        Berlin"), not a run-on ("User lives in Berlin and I am from Germany").
        We stop before a coordinating conjunction that starts a new finite
        clause (" and I/we/you …", " so I …", " but I …", " then I …"). This is
        a clause-boundary parse on the extracted fragment, not a keyword filter.
        """
        match = re.search(
            r"\s+(?:and|so|but|then)\s+(?:i|we|you)\b", value, flags=re.IGNORECASE
        )
        if match:
            return value[: match.start()].strip()
        return value

    # A stable DSP fact describes *the person* in the third person ("User works
    # on X"). A captured value that still contains first- or second-person
    # pronouns ("I love you too baby", "I'm from you", "keep me safe") is
    # speech *directed at someone*, not a self-description, so it is not a
    # profile fact. Grammatical-person rejection is structural, not a keyword
    # filter: it matches the same person category the templates already assume.
    _PERSON_ADDRESS_RE = re.compile(
        r"\b(?:i|i'?m|i'?ve|i'?ll|me|my|mine|we|our|you|you'?re|you'?ve|"
        r"you'?ll|your|yours|u)\b",
        flags=re.IGNORECASE,
    )

    # Conversational filler that signals speech rather than description:
    # vocative endearments and laughter/emote markers. These are data-cleaning
    # heuristics for the profile extractor only (the DSP is meant to hold who
    # the person *is*, not how they talk to Synth) — they are not used for
    # routing, intent, salience, or any other product logic.
    _CONVERSATIONAL_FILLER_WORDS = frozenset(
        {
            "baby",
            "babe",
            "daddy",
            "mommy",
            "mama",
            "papa",
            "sweetheart",
            "princess",
            "dear",
            "honey",
            "heheh",
            "hehe",
            "heh",
            "mmmwah",
            "mwah",
            "mmm",
            "mm",
            "hmm",
            "lol",
            "haha",
            "ahh",
        }
    )

    # Emote clusters ("mmmmwah", "heheheh", "ahhhh", "mmm") are roleplay/chat
    # fillers, not biographical noun-phrases. Any variant spelling with one or
    # more repeated letters from {m, h, w, a} qualifies. A fact value that ends
    # in such a cluster — or is *entirely* one — is speech, not description.
    # Anchored to the end so legitimate values with mid-word double letters
    # ("User likes swimming") are preserved.
    _EMOTE_TAIL_RE = re.compile(r"(?i)([mhw])\1{1,}(?:wah|ah|hh)*\s*$")

    # Alternating laughter ("heheheh", "hehehe", "hahaha") is a chat filler
    # that repeated-letter matching cannot catch; match the alternating run.
    _LAUGHTER_RE = re.compile(r"(?i)(?:he){2,}h?|(?:ha){2,}")

    @classmethod
    def _is_bound_fact(cls, value: str) -> bool:
        """Reject captured fragments that are clearly not a short biographical fact.

        Structural guards:
        - empty / too-short / implausibly long fragments are dropped;
        - trailing question/imperative punctuation is dropped;
        - a value that still addresses a person (first/second-person pronouns)
          or is filled with conversational endearments/laughter is speech
          directed at someone, not a stable self-description, so it is dropped;
        - a value that ends in (or is entirely) an emote cluster is speech.
        """
        stripped = (value or "").strip()
        if not stripped or len(stripped) < 2:
            return False
        if len(stripped) > 120:
            return False
        if stripped[-1] in "?!.":
            return False
        if cls._PERSON_ADDRESS_RE.search(stripped):
            return False
        lowered = stripped.lower()
        if re.search(
            r"\b(?:"
            + "|".join(re.escape(word) for word in cls._CONVERSATIONAL_FILLER_WORDS)
            + r")\b",
            lowered,
        ):
            return False
        # Pure emote / emote-tailed values are speech, not description.
        if cls._EMOTE_TAIL_RE.search(lowered):
            return False
        if cls._LAUGHTER_RE.search(lowered):
            return False
        return True

    @staticmethod
    def _clean_fact_value(value: str) -> str:
        cleaned = (value or "").strip().strip("\"'")
        cleaned = re.sub(r"\s+", " ", cleaned)
        cleaned = cleaned.rstrip(" .,;:!?")
        return cleaned[:160]
