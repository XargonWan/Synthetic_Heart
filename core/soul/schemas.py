from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ForesightSignalModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    content: str = Field(min_length=1)
    valid_until: date
    trigger: str = Field(min_length=1)
    emotional_implication: dict[str, float] = Field(default_factory=dict)


class EmotionalTagModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    state_snapshot: dict[str, float]
    dominant_emotion: str
    intensity: float = Field(ge=0.0, le=1.0)
    valence: float = Field(ge=-1.0, le=1.0)


class MemCellExtractionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    episodic_trace: str = Field(min_length=1)
    atomic_facts: list[str] = Field(default_factory=list)
    emotional_tag: EmotionalTagModel
    foresight_signals: list[ForesightSignalModel] = Field(default_factory=list)
    timestamp: datetime

    @field_validator("atomic_facts")
    @classmethod
    def facts_not_empty_strings(cls, value: list[str]) -> list[str]:
        cleaned = [v.strip() for v in value if v and v.strip()]
        return cleaned


class DspExtractionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    user_facts: list[str] = Field(default_factory=list)
    user_preferences: list[str] = Field(default_factory=list)
    ai_self_facts: list[str] = Field(default_factory=list)


class SituationalNoteModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    note_type: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    summary: str = Field(min_length=1)
    priority: int = Field(ge=-3, le=3, default=0)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    valid_from: datetime | None = None
    valid_until: datetime
    effective_at: datetime | None = None
    expired_at: datetime | None = None
    source: str = Field(default="debrief")

    @field_validator(
        "valid_from", "valid_until", "effective_at", "expired_at", mode="before"
    )
    @classmethod
    def accept_iso_strings(cls, value: object) -> object:
        """Parse ISO-8601 strings before strict validation rejects them.

        The model is strict, but these values come from an LLM, which emits
        datetimes as strings. Without this the whole extraction failed schema
        validation every time and silently fell back to the rule-based path.
        """
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return value


class SituationalExtractionModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    notes: list[SituationalNoteModel] = Field(default_factory=list)


class SummaryResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    summary_text: str = Field(min_length=1)
