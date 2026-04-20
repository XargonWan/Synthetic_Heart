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

    async def extract_dsp(
        self, *, transcript: str, current_date: date
    ) -> DspExtractionModel:
        text = (transcript or "").strip()
        if not text:
            return DspExtractionModel(
                user_facts=[], user_preferences=[], ai_self_facts=[]
            )

        facts = self._extract_user_facts(text)
        prefs = self._extract_preferences(text)
        return DspExtractionModel(
            user_facts=facts,
            user_preferences=prefs,
            ai_self_facts=[],
        )

    def _extract_user_facts(self, text: str) -> list[str]:
        facts: list[str] = []

        for match in re.finditer(r"\bI am\s+([^\.\n]+)", text, flags=re.IGNORECASE):
            value = match.group(1).strip()
            if value:
                facts.append(f"User says they are {value}")

        for match in re.finditer(
            r"\bI work on\s+([^\.\n]+)", text, flags=re.IGNORECASE
        ):
            value = match.group(1).strip()
            if value:
                facts.append(f"User works on {value}")

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
                    prefs.append(value)

        return list(dict.fromkeys(prefs))
