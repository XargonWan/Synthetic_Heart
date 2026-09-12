from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum


@dataclass(slots=True)
class EmotionalTag:
    """Emotional snapshot saved alongside a memory cell."""

    state_snapshot: dict[str, float]
    dominant_emotion: str
    intensity: float
    valence: float


@dataclass(slots=True)
class ForesightSignal:
    """Time-bounded future-oriented signal derived from memory."""

    content: str
    valid_until: date
    trigger: str
    emotional_implication: dict[str, float] = field(default_factory=dict)
    source_cell_id: str | None = None
    priority: float = 0.5


# Valid note types for SituationalNote.
SITUATIONAL_NOTE_TYPES = ("EVENT", "STATE", "INTERVAL", "INSTANT")


@dataclass(slots=True)
class SituationalNote:
    """Short-lived, time-sensitive user circumstance with a bounded validity window.

    Unlike a MemCell (persistent memory) or a ForesightSignal (future-only,
    date-level), a SituationalNote stores *absolute* timestamps for when a
    circumstance was true (``valid_from``) and when it expires
    (``valid_until``). Relative rendering ("today", "in 3 days") is a
    presentation concern resolved at injection time via ``TemporalRenderer``.
    """

    id: str
    note_type: str
    subject: str
    summary: str
    valid_from: datetime | None
    valid_until: datetime | None
    priority: int = 0
    confidence: float = 0.5
    effective_at: datetime | None = None
    expired_at: datetime | None = None
    source: str = "debrief"
    status: str = "active"
    session_id: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    resolved_at: datetime | None = None

    def is_active(self, now: datetime) -> bool:
        """Return True when ``now`` falls within [valid_from, valid_until)."""
        if self.valid_from and now < self.valid_from:
            return False
        if self.valid_until and now >= self.valid_until:
            return False
        return True

    def is_expired(self, now: datetime) -> bool:
        """Return True when the note has passed its validity window."""
        if self.valid_until is None:
            return False
        return now >= self.valid_until


@dataclass(slots=True)
class MemCell:
    """Atomic episodic memory unit."""

    id: str
    episodic_trace: str
    atomic_facts: list[str]
    emotional_tag: EmotionalTag
    foresight_signals: list[ForesightSignal]
    event_timestamp: datetime
    session_id: str
    embedding: list[float] | None = None
    retrieval_count: int = 0
    explicit_importance: float = 0.0
    consolidated: bool = False
    scene_id: str | None = None


@dataclass(slots=True)
class MemCellRecall:
    """Ranked memory recall candidate for prompt-time retrieval."""

    cell: MemCell
    similarity: float
    lexical_score: float
    score: float


@dataclass(slots=True)
class MemScene:
    """Thematic cluster of memory cells."""

    id: str
    title: str
    summary: str
    cell_ids: list[str]
    created_at: datetime
    updated_at: datetime


@dataclass(slots=True)
class KgTriple:
    """Temporal knowledge graph triple."""

    subject: str
    predicate: str
    object: str
    valid_from: datetime
    valid_until: datetime | None = None
    scene_id: str | None = None


@dataclass(slots=True)
class DspExtraction:
    """Daily extracted profile evidence used to build DSP versions."""

    id: str
    session_id: str
    extracted_at: datetime
    user_facts: list[str]
    user_preferences: list[str]
    ai_self_facts: list[str] = field(default_factory=list)


@dataclass(slots=True)
class DspVersion:
    """Compiled DSP snapshot."""

    id: str
    content: str
    created_at: datetime
    archived_at: datetime | None = None


@dataclass(slots=True)
class EmotionalProfile:
    """Fixed personality factors for emotional sensitivity."""

    anxiety: float = 0.15
    self_preservation: float = 0.05
    concern_for_user: float = 0.90
    social_connection: float = 0.80
    achievement: float = 0.60
    sensory_pleasure: float = 0.40
    loss: float = 0.70
    disappointment: float = 0.55
    loneliness: float = 0.85
    isolation: float = 0.75
    pain: float = 0.30
    frustration: float = 0.35

    def as_dict(self) -> dict[str, float]:
        return {
            "anxiety": self.anxiety,
            "self_preservation": self.self_preservation,
            "concern_for_user": self.concern_for_user,
            "social_connection": self.social_connection,
            "achievement": self.achievement,
            "sensory_pleasure": self.sensory_pleasure,
            "loss": self.loss,
            "disappointment": self.disappointment,
            "loneliness": self.loneliness,
            "isolation": self.isolation,
            "pain": self.pain,
            "frustration": self.frustration,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "EmotionalProfile":
        """Build from a partial dict; missing keys fall back to spec defaults, values clamped to [0, 1]."""

        def _clamp(val: object, default: float) -> float:
            try:
                return max(0.0, min(1.0, float(val)))  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return default

        return cls(
            anxiety=_clamp(data.get("anxiety", 0.15), 0.15),
            self_preservation=_clamp(data.get("self_preservation", 0.05), 0.05),
            concern_for_user=_clamp(data.get("concern_for_user", 0.90), 0.90),
            social_connection=_clamp(data.get("social_connection", 0.80), 0.80),
            achievement=_clamp(data.get("achievement", 0.60), 0.60),
            sensory_pleasure=_clamp(data.get("sensory_pleasure", 0.40), 0.40),
            loss=_clamp(data.get("loss", 0.70), 0.70),
            disappointment=_clamp(data.get("disappointment", 0.55), 0.55),
            loneliness=_clamp(data.get("loneliness", 0.85), 0.85),
            isolation=_clamp(data.get("isolation", 0.75), 0.75),
            pain=_clamp(data.get("pain", 0.30), 0.30),
            frustration=_clamp(data.get("frustration", 0.35), 0.35),
        )


@dataclass(slots=True)
class EmotionalState:
    """Core runtime emotional state in four dimensions."""

    joy: float = 0.0
    fear: float = 0.0
    sad: float = 0.0
    anger: float = 0.0
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_dict(self) -> dict[str, float]:
        return {
            "joy": self.joy,
            "fear": self.fear,
            "sad": self.sad,
            "anger": self.anger,
        }


@dataclass(slots=True)
class EmotionalEvent:
    """Single emotional update input."""

    source: str
    factor_deltas: dict[str, float]
    intensity: float
    context: str


class CuratorDecision(str, Enum):
    """Classification verdict from the Memory Curator."""

    KEEP_FUTURE = "KEEP_FUTURE"
    KEEP_IMPORTANT = "KEEP_IMPORTANT"
    REMOVE = "REMOVE"


@dataclass(slots=True)
class MemCellSummary:
    """Lightweight MemCell descriptor passed to the Memory Curator.

    Only the fields needed for classification are fetched — embeddings and
    full atomic facts are intentionally excluded to keep the operation cheap.
    """

    id: str
    episodic_trace: str
    event_timestamp: datetime
    retrieval_count: int
    explicit_importance: float
    emotional_intensity: float
    has_active_foresight: bool


@dataclass(slots=True)
class CurationResult:
    """Result summary returned by SoulCompiler.run_curator()."""

    inspected: int
    removed: int
    retained: int
    kept_future: int
    kept_important: int


def compute_memcell_salience(
    *,
    emotional_intensity: float,
    retrieval_count: int,
    recency_score: float,
    explicit_importance: float,
) -> float:
    """Compute salience score using v3 weighting from SOUL specification."""

    retrieval_score = min(max(float(retrieval_count), 0.0) / 10.0, 1.0)
    return (
        max(0.0, min(1.0, emotional_intensity)) * 0.4
        + retrieval_score * 0.3
        + max(0.0, min(1.0, recency_score)) * 0.2
        + max(0.0, min(1.0, explicit_importance)) * 0.1
    )


def new_memcell_id(session_id: str, timestamp: datetime) -> str:
    """Build deterministic memcell id from session and timestamp."""

    ts = timestamp.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"{session_id}:{ts}"


def new_scene_id(anchor: datetime) -> str:
    """Build scene id from UTC timestamp."""

    ts = anchor.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    return f"scene:{ts}"


def situational_note_from_extraction(
    *,
    note_type: str,
    subject: str,
    summary: str,
    priority: int = 0,
    confidence: float = 0.5,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    effective_at: datetime | None = None,
    expired_at: datetime | None = None,
    source: str = "debrief",
    session_id: str | None = None,
    now: datetime | None = None,
    default_ttl_hours: int = 24,
) -> "SituationalNote":
    """Build a persistable note from extracted fields, normalising the window.

    Every datetime is forced to UTC-aware, and a missing validity window falls
    back to ``default_ttl_hours`` from ``now``. The id is deliberately left
    empty: the repository derives a stable one from the circumstance so the same
    note seen twice updates a single row instead of accumulating duplicates.
    """

    anchor = now or now_utc()
    if anchor.tzinfo is None:
        anchor = anchor.replace(tzinfo=timezone.utc)

    def _utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)

    resolved_until = _utc(valid_until) or anchor + timedelta(hours=default_ttl_hours)
    resolved_from = _utc(valid_from) or anchor

    return SituationalNote(
        id="",
        note_type=note_type,
        subject=subject,
        summary=summary,
        priority=priority,
        confidence=confidence,
        valid_from=resolved_from,
        valid_until=resolved_until,
        effective_at=_utc(effective_at),
        expired_at=_utc(expired_at),
        source=source,
        status="active",
        session_id=session_id,
        created_at=anchor,
        updated_at=anchor,
        resolved_at=None,
    )


def situational_note_id(note_type: str, subject: str, summary: str) -> str:
    """Build a stable id identifying one situational circumstance.

    The id has to come from the circumstance itself rather than being generated
    fresh: the debrief re-reads the same recent transcript on every run, so a
    random id left ``ON CONFLICT (id) DO UPDATE`` unreachable and each compile
    inserted another copy of a note the store already held. With a derived id
    the second mention refreshes the existing row's validity window instead.
    """

    raw = "|".join(
        str(part or "").strip().casefold() for part in (note_type, subject, summary)
    )
    return f"tsc-{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"


def now_utc() -> datetime:
    """Return timezone-aware UTC now."""

    return datetime.now(timezone.utc)


def top_emotion(state: dict[str, float]) -> str:
    """Return emotion key with highest absolute magnitude."""

    if not state:
        return "neutral"
    return max(state.keys(), key=lambda k: abs(float(state.get(k, 0.0))))


def emotional_valence(state: dict[str, float]) -> float:
    """Estimate valence from joy vs. negative axes."""

    joy = float(state.get("joy", 0.0))
    neg = (
        abs(float(state.get("fear", 0.0)))
        + abs(float(state.get("sad", 0.0)))
        + abs(float(state.get("anger", 0.0)))
    ) / 3.0
    return max(-1.0, min(1.0, joy - neg))


def emotional_intensity(state: dict[str, float]) -> float:
    """Estimate normalized intensity from emotion magnitudes."""

    if not state:
        return 0.0
    total = sum(abs(float(v)) for v in state.values())
    return max(0.0, min(1.0, total / 4.0))
