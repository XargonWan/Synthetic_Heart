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


class SummaryResultModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    summary_text: str = Field(min_length=1)
