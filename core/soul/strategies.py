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

    _SELF_SPEAKER_LABELS = frozenset({"self", "synth", "assistant", "2b", "raine"})

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
        facts: list[str] = []

        facts.extend(self._extract_first_person_facts(text, subject="User"))

        leading_status_patterns = [
            (r"^\s*doing\s+([^,\n!?]+)", "User reports doing {value}"),
            (r"^\s*feeling\s+([^,\n!?]+)", "User reports feeling {value}"),
            (r"^\s*staying\s+([^,\n!?]+)", "User is staying {value}"),
            (r"^\s*recovering\s+([^,\n!?]+)", "User reports recovering {value}"),
        ]
        for line in text.splitlines():
            for pattern, template in leading_status_patterns:
                match = re.search(pattern, line, flags=re.IGNORECASE)
                if match is None:
                    continue
                value = self._clean_fact_value(match.group(1))
                if value:
                    facts.append(template.format(value=value))

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
        return list(dict.fromkeys(self._extract_first_person_facts(text, subject="AI")))

    def _extract_first_person_facts(self, text: str, *, subject: str) -> list[str]:
        patterns = [
            (r"\bI(?:'m| am)\s+([^\.\n!?]+)", f"{subject} says they are {{value}}"),
            (r"\bI feel\s+([^\.\n!?]+)", f"{subject} feels {{value}}"),
            (r"\bI(?:'ve| have) been\s+([^\.\n!?]+)", f"{subject} has been {{value}}"),
            (r"\bI work on\s+([^\.\n!?]+)", f"{subject} works on {{value}}"),
            (
                r"\bI(?:'m| am) working on\s+([^\.\n!?]+)",
                f"{subject} is working on {{value}}",
            ),
            (r"\bI(?:'m| am) making\s+([^\.\n!?]+)", f"{subject} is making {{value}}"),
            (r"\bI need to\s+([^\.\n!?]+)", f"{subject} needs to {{value}}"),
            (r"\bI want to\s+([^\.\n!?]+)", f"{subject} wants to {{value}}"),
            (r"\bI (?:like|love)\s+([^\.\n!?]+)", f"{subject} likes {{value}}"),
        ]

        facts: list[str] = []
        for pattern, template in patterns:
            for match in re.finditer(pattern, text, flags=re.IGNORECASE):
                value = self._clean_fact_value(match.group(1))
                if value:
                    facts.append(template.format(value=value))
        return facts

    @staticmethod
    def _clean_fact_value(value: str) -> str:
        cleaned = (value or "").strip().strip("\"'")
        cleaned = re.sub(r"\s+", " ", cleaned)
        cleaned = cleaned.rstrip(" .,;:!?")
        return cleaned[:160]
