from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.plugin_base import PluginBase
from core.variables_engine import register_exposed_var
from core.db import get_conn_ctx
from core.soul.compiler import (
    NoopEmbedder,
    RuleBasedDspBuilder,
    RuleBasedMemCellCurator,
    RuleBasedSummaryBuilder,
    SoulCompiler,
    redistil_candidate_limit,
)
from core.soul.fastembed_embedder import FastEmbedder
from core.soul.emotion_engine import EmotionalEngine
from core.soul.models import (
    EmotionalEvent,
    EmotionalProfile,
    EmotionalState,
    MemCell,
    MemCellRecall,
)
from core.soul.repository import (
    InMemorySoulRepository,
    PostgresSoulRepository,
    SoulRepository,
)
from core.soul.service import (
    is_housekeeping_session,
    on_demand_session_prefixes,
    source_base,
)
from core.soul.situational import is_same_circumstance, subject_tokens
from core.soul.strategies import (
    RuleBasedDspExtractor,
    RuleBasedMemCellExtractor,
)

register_exposed_var(
    "SOUL_COMPILE_IDLE_SECONDS",
    label="SOUL Compile Idle Seconds",
    default=300,
    value_type=int,
    ui_type="number",
    description="Compile a buffered interface transcript after this many idle seconds.",
    scope="plugins",
    component="soul_plugin",
)

register_exposed_var(
    "SOUL_SCHEDULER_INTERVAL_SECONDS",
    label="SOUL Scheduler Interval",
    default=60,
    value_type=int,
    ui_type="number",
    description="Scheduler tick interval in seconds for compile/rollup checks.",
    scope="plugins",
    component="soul_plugin",
)

register_exposed_var(
    "SOUL_REPOSITORY_BACKEND",
    label="SOUL Repository Backend",
    default="memory",
    value_type=str,
    ui_type="text",
    description="Legacy compatibility flag. When the main runtime DB is PostgreSQL, SOUL uses that Postgres backend automatically.",
    scope="plugins",
    component="soul_plugin",
)

register_exposed_var(
    "SOUL_POSTGRES_DSN",
    label="Legacy SOUL Postgres DSN",
    default="",
    value_type=str,
    ui_type="text",
    description="Legacy SOUL PostgreSQL source DSN used only for one-time migration into the main runtime Postgres.",
    scope="plugins",
    component="soul_plugin",
)

register_exposed_var(
    "SOUL_DSP_INJECT_ENABLED",
    label="Inject DSP user profile into prompts",
    default=0,
    value_type=int,
    ui_type="bool",
    description=(
        "Inject the compiled SOUL user profile (DSP) into the user-role context "
        "of every turn so Synth always knows who she is talking to. Intended for "
        "a clean, LLM-compiled profile only — the rule-based extractor can still "
        "produce speech-shaped content. When off, only session-state and the "
        "per-turn emotion delta are injected."
    ),
    scope="plugins",
    component="soul_plugin",
)

register_exposed_var(
    "SOUL_DSP_LLM_ENABLED",
    label="LLM-compiled DSP profile",
    default=1,
    value_type=int,
    ui_type="bool",
    description=(
        "Compile the DSP user profile with an LLM: extract biography from the "
        "daily transcript and compile it into a clean, self-healing profile. "
        "Uses the DSP_CORTEX engine scope. Falls back to the rule-based "
        "extractor/builder on any failure."
    ),
    scope="plugins",
    component="soul_plugin",
)

register_exposed_var(
    "SOUL_REDISTIL_TIMEOUT_SEC",
    label="Re-distil timeout per memory (seconds)",
    default=300,
    value_type=int,
    ui_type="number",
    description=(
        "How long one memory's rewrite may take before the pass counts it as "
        "timed out and moves on to the next. Raise it for a slow engine (one that "
        "drives a browser, or a large local model); 0 removes the bound entirely. "
        "A timed-out memory is left untouched, so pressing again retries only those."
    ),
    scope="plugins",
    component="soul_plugin",
    advanced=True,
)

register_exposed_var(
    "SOUL_SPEAKER_IDENTITIES",
    label="People in the transcript (who is who)",
    default="",
    value_type=str,
    ui_type="text",
    description=(
        "Free text naming each speaker and their pronouns, e.g. "
        "'Scar - he/him, my husband; 2B - she/her, me'. The memory and profile "
        "extractors AND the profile compiler are told this outright, so a person "
        "the transcript never genders is never guessed at and a single day whose "
        "extraction swapped two roles cannot redefine who the profile is about "
        "(leaving it empty keeps the previous behaviour). The persona's own lines "
        "are labelled '<name> (the persona)' in the transcript the extractors "
        "read, whatever label the interface cached them under."
    ),
    scope="plugins",
    component="soul_plugin",
    advanced=True,
)

register_exposed_var(
    "SOUL_MEMCELL_LLM_ENABLED",
    label="LLM-distilled MemCells",
    default=1,
    value_type=int,
    ui_type="bool",
    description=(
        "Distil each compiled MemCell with an LLM instead of storing the "
        "conversation text verbatim as the cell's trace. Recall then returns "
        "paraphrased knowledge with subject|predicate|object facts, and a "
        "statement that was corrected later says so in its own trace. Uses the "
        "DSP_CORTEX engine scope. Falls back to the rule-based extractor (the "
        "transcript as the trace, no fabricated fact) on any failure."
    ),
    scope="plugins",
    component="soul_plugin",
)

register_exposed_var(
    "SOUL_CURATOR_MIN_AGE_HOURS",
    label="Curator grace period (hours)",
    default=168,
    value_type=int,
    ui_type="number",
    description=(
        "How long a freshly compiled memory is protected from the curator's "
        "low-salience removal. Recency is only 0.2 of the salience formula against "
        "a 0.4 removal threshold, so without a grace period a calm new memory can "
        "never survive the nightly pass and the day's ordinary events are deleted "
        "the night they are compiled. 168 = one week."
    ),
    scope="plugins",
    component="soul_plugin",
)

register_exposed_var(
    "SOUL_CURATOR_MAX_MEMORIES",
    label="Curator memory cap",
    default=5000,
    value_type=int,
    ui_type="number",
    description=(
        "How many MemCells the store may hold before the curator evicts the "
        "calmest ones. The previous hard-coded 500 was roughly a month of "
        "conversation. Evicted cells are archived (mem_cells_archive), never "
        "lost."
    ),
    scope="plugins",
    component="soul_plugin",
    advanced=True,
)

register_exposed_var(
    "SOUL_CURATOR_EVICT_ONLY_OVER_CAP",
    label="Curator evicts only over the cap",
    default=1,
    value_type=int,
    ui_type="bool",
    description=(
        "On: a calm memory is kept while the store is under its cap and is the "
        "first to go once it is over. Off: the old rule, where every memory "
        "below the salience threshold is deleted after its grace period "
        "whatever the store size (a live store lost ~95% of its memories that "
        "way, and kept mostly what had already been recalled)."
    ),
    scope="plugins",
    component="soul_plugin",
    advanced=True,
)

register_exposed_var(
    "MEMORY_LEGACY_TIERS_ENABLED",
    label="Keyword-search the legacy memories and diary",
    default=1,
    value_type=int,
    ui_type="bool",
    description=(
        "On: every turn also keyword-searches the legacy `memories` table and the "
        "AI diary. Turn it OFF once scripts/ingest_legacy_into_soul.py has filed "
        "them in SOUL: SOUL's recall then returns the same entries ranked by "
        "meaning, and leaving the keyword tiers on injects them a second time. "
        "Raw chat history is still searched either way."
    ),
    scope="plugins",
    component="soul_plugin",
    advanced=True,
)

# Day markers of the nightly jobs. A key must be registered to be persisted at all
# (``set_value`` refuses an unknown one), and an unpersisted marker made every
# restart repeat the rollup and the curator. Internal state, not a setting: hidden.
for _marker_key, _marker_label in (
    ("SOUL_LAST_ROLLUP_DATE", "Last nightly rollup (UTC date)"),
    ("SOUL_LAST_CURATOR_DATE", "Last curator pass (UTC date)"),
):
    register_exposed_var(
        _marker_key,
        label=_marker_label,
        default="",
        value_type=str,
        ui_type="string",
        description="Internal marker so a restart does not repeat the job today.",
        scope="plugins",
        component="soul_plugin",
        advanced=True,
        hidden=True,
    )

register_exposed_var(
    "SOUL_ARCHIVE_RETENTION_DAYS",
    label="Memory archive retention (days)",
    default=365,
    value_type=int,
    ui_type="number",
    description=(
        "How long a memory the curator evicted or a consolidation merged away stays "
        "in the archive before the daily job deletes it for good. The archive is a "
        "safety net, not storage: without an expiry it only grows. 0 keeps "
        "everything."
    ),
    scope="plugins",
    component="soul_plugin",
    advanced=True,
)

register_exposed_var(
    "SOUL_ARCHIVE_COPY_RETENTION_DAYS",
    label="Archive retention for repeats and system text (days)",
    default=30,
    value_type=int,
    ui_type="number",
    description=(
        "Archived cells that were repeats of another cell, or system text that "
        "should never have been a memory, hold nothing the surviving cell lacks, so "
        "they expire sooner. 0 keeps everything."
    ),
    scope="plugins",
    component="soul_plugin",
    advanced=True,
)

register_exposed_var(
    "SOUL_TEMPORAL_INJECT_LIMIT",
    label="Situational notes injected per prompt",
    default=8,
    value_type=int,
    ui_type="number",
    description=(
        "Maximum number of active situational notes rendered into one prompt "
        "(ranked by priority, then confidence, and one per subject-token set so "
        "near-duplicate notes about the same event do not crowd the block). The "
        "store keeps every note; this only bounds what the model is shown."
    ),
    scope="plugins",
    component="soul_plugin",
)

register_exposed_var(
    "SOUL_REDISTIL_LIMIT",
    label="Memory re-distil batch size",
    default=5000,
    value_type=int,
    ui_type="number",
    description=(
        "How many memories the manual re-distil button may rewrite in one press. "
        "Each one costs a model call, so the pass is capped and can be pressed "
        "again to continue with the rest."
    ),
    scope="plugins",
    component="soul_plugin",
)

register_exposed_var(
    "SOUL_TEMPORAL_ENABLED",
    label="Temporal situational context extraction",
    default=1,
    value_type=int,
    ui_type="bool",
    description=(
        "Extract short-lived, time-bounded user circumstances (e.g. 'on vacation "
        "next week', 'moving tomorrow') into the situational_notes store. "
        "These are rendered as relative temporal context in prompts and expire "
        "automatically. This is NOT a replacement for persistent MemCell memory "
        "— stable facts still go through DSP extraction."
    ),
    scope="plugins",
    component="soul_plugin",
)

register_exposed_var(
    "SOUL_TEMPORAL_LOOKBACK_DAYS",
    label="Temporal context lookback window (days)",
    default=1,
    value_type=int,
    ui_type="number",
    description=(
        "How many days back a situational note must have been created to still be "
        "injected into the prompt context."
    ),
    scope="plugins",
    component="soul_plugin",
)

register_exposed_var(
    "SOUL_TEMPORAL_CATEGORY_DEFAULT_TTL",
    label="Default TTL per note type (hours)",
    default='{"event": 168, "state": 24, "interval": 48, "instant": 1}',
    value_type=str,
    ui_type="text",
    description=(
        "JSON map of note_type → default validity window in hours, used when the "
        "user's temporal language does not specify a precise end. "
        "event=168 (1 week), state=24 (1 day), interval=48 (2 days), "
        "instant=1 (1 hour)."
    ),
    scope="plugins",
    component="soul_plugin",
    advanced=True,
)

register_exposed_var(
    "SOUL_RECALL_COOLDOWN_SEC",
    label="Recall cooldown per memory (seconds)",
    default=900,
    value_type=int,
    ui_type="number",
    description=(
        "How long a memory cell stays out of the recalled set after it has been "
        "injected into a prompt. Rotation only: it changes which memories are "
        "shown, never what is stored. 0 disables the cooldown."
    ),
    scope="plugins",
    component="soul_plugin",
    advanced=True,
)

register_exposed_var(
    "SOUL_RECALL_LINKED_SESSIONS",
    label="Chats that share one memory scope",
    default="",
    value_type=str,
    ui_type="text",
    description=(
        "Comma-separated chat paths that should count as ONE conversation for "
        "recall, e.g. 'telegram_bot/-5293915984,telegram_bot/5208932647' for a "
        "group and the DM of the same household. A cell written in a linked "
        "chat is then recalled as if it belonged to the current conversation: "
        "it keeps the same-chat boost and the looser admission floor instead of "
        "having to clear the stricter cross-chat bar. Only the listed chats are "
        "linked, and only for a turn that is itself in one of them, so an "
        "unlisted chat is unaffected. Empty keeps the previous behaviour."
    ),
    scope="plugins",
    component="soul_plugin",
    advanced=True,
)


@dataclass(slots=True)
class _SessionState:
    emotional_state: EmotionalState = field(default_factory=EmotionalState)
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_decay_applied: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    # ``dm`` / ``group`` as the interface reported it for the latest message
    # (``core.chat_context.resolve_chat_kind``), or None when it could not tell.
    chat_kind: str | None = None
    # Set by the Debrief when it judges the current exchange to have been shared in
    # confidence. Everything compiled from the current buffer window is then
    # ``private`` whatever kind of chat it was; cleared once that window compiles.
    force_private: bool = False
    # Humans who spoke in the current buffer window, by id (name as last seen). It
    # is stamped on the cells compiled from the window as ``participants``.
    window_participants: dict[str, str] = field(default_factory=dict)


_SOUL_RECALL_LIMIT = 5
# At most this many of those slots go to ingested cells (diary entries, day
# summaries, retired memories). They hold the persona's own narrative and rank well
# against almost any question, so uncapped they filled the whole block and no
# conversation memory got in (seen on the first A/B pair: 5 of 5 diary cells).
# Unused slots still fall through to them, so a store with few chat cells is not
# left empty.
_SOUL_RECALL_INGESTED_MAX = 2
_SOUL_RECALL_CANDIDATE_LIMIT = 24
_SOUL_CONSOLIDATE_COOLDOWN_SECONDS = 900
# How far before a buffer's first line a compile looks in `chat_history_cache`.
# The transcript is trimmed to that line's own row, so the slack only has to
# cover the moment between a message arriving and the injection that buffers it
# (the two can land on either side of a clock tick, and the row is written by
# the interface, not by us).
_SOUL_COMPILE_TRANSCRIPT_LOOKBACK_SECONDS = 1800
# A cell's retrieval count is evidence of usefulness, so it must not be inflated
# by the several prompt builds a single turn performs (recon, main reply,
# situational extractor, Grillo beats): live cells reached counts of 70 and 164
# within hours, then 2,536 and 906. The count is "on how many days did a person's
# turn bring this memory back": one bump per cell per day. At one an hour a cell
# that matched every conversation counted up to 24 a day (live: 190 to 393 within
# weeks), which measured how often it matches, not how useful it is. The interval
# is kept in the row (``mem_cells.last_retrieved_at``), so a restart does not reset it.
_SOUL_RETRIEVAL_BUMP_MIN_INTERVAL_SEC = 86400.0

# Recall rotation. Semantic similarity is 58% of the recall score and it does not
# move while the same person keeps talking about the same things, so without a
# cooldown the same handful of cells is injected turn after turn for days: live,
# 26 cells tagged "joy" carried 77% of all recall traffic and 8 cells from June
# to August carried 58%, while 436 neutral cells shared 9%. Stepping a
# just-recalled cell aside for a while lets the next-most-similar cells through;
# the store keeps everything, only what is SHOWN rotates.
#
# The cooldown is keyed per CONVERSATION, not per cell. Keyed by the cell alone
# it was global to the process, so a Grillo beat or a second chat recalling a
# cell held it back in a conversation that had never seen it: on 2026-09-22 no
# prompt at all carried a cell from that day while the cells sat in the store,
# because the beats and the other chats kept spending them. What still holds a
# cell back is its own conversation recalling it inside the window, which is the
# rotation working as intended.
_SOUL_RECALL_COOLDOWN_SEC = 900
_SOUL_RECALL_TRACK_MAX = 512


def _soul_recall_cooldown_seconds() -> float:
    """Return how long a recalled cell stays out of the set (0 disables it)."""

    try:
        from core.config_manager import config_registry

        seconds = int(
            config_registry.get_value(
                "SOUL_RECALL_COOLDOWN_SEC",
                _SOUL_RECALL_COOLDOWN_SEC,
                value_type=int,
            )
        )
    except Exception:
        return float(_SOUL_RECALL_COOLDOWN_SEC)
    return float(max(0, min(seconds, 86_400)))


def _recall_cooldown_key(session_id: str, cell_id: object) -> str:
    """Scope a recall-cooldown entry to the conversation that saw the cell.

    Keyed by the cell alone, one chat's recall held that cell back in every
    other chat, and Grillo beats share the process, so a beat could spend a
    conversation's own memories on its behalf.
    """

    return f"{session_id}\x00{cell_id}"


# How many active situational notes may be rendered into ONE prompt. The debrief
# writes a fresh note every time it re-describes a circumstance, so the active set
# grows without bound (64 active notes on 2026-09-18, 28 of them in a single
# prompt, several contradicting each other about the same evening). What the model
# is shown is ranked and bounded here; the store keeps everything.
_SOUL_TEMPORAL_INJECT_LIMIT = 8

# Backstop for the manual re-distil pass: one model call per legacy cell, so a
# single press is capped rather than allowed to grind through a huge store in one
# go. ``SOUL_REDISTIL_LIMIT`` sets the batch size, this is the ceiling on it.
_SOUL_REDISTIL_HARD_CAP = 20000

# How long the "one press would spend this many model calls" preview is reused
# before it is measured again. The Settings panel polls the status endpoint, and
# the count has to examine the candidates to apply the same filter the pass
# applies, so it is not free.
_REDISTIL_WORKABLE_CACHE_SEC = 60.0


def _soul_speaker_identities() -> str:
    """Who the people in a session are, declared by the operator.

    ``SOUL_SPEAKER_IDENTITIES`` (advanced) is free text naming each speaker and
    their pronouns, for example ``Scar - he/him, the human; 2B - she/her, the
    persona``. Both extractors state it to the model outright, because a
    transcript that never genders a person leaves the model guessing: measured
    live on 2026-09-20, a session whose human is a man was distilled with him as
    "she/her" throughout, and recall then handed that back as fact.
    """
    try:
        from core.config_manager import config_registry

        return str(
            config_registry.get_value("SOUL_SPEAKER_IDENTITIES", "", value_type=str)
            or ""
        ).strip()
    except Exception:
        return ""


def _soul_redistil_timeout() -> float:
    """Seconds to allow ONE memory's rewrite before it is counted as timed out.

    ``SOUL_REDISTIL_TIMEOUT_SEC`` (default 300; ``0`` removes the bound) exists
    because engines are not equally fast. One that drives a browser, or a large
    local model, can take minutes per memory, and an unbounded pass gives the
    operator no way to tell a slow engine from a stalled one: the counters simply
    stop moving. A timed-out memory is left untouched and unstamped, so raising the
    value and pressing again retries exactly the ones that were cut off.
    """
    try:
        from core.config_manager import config_registry

        raw = config_registry.get_value(
            "SOUL_REDISTIL_TIMEOUT_SEC", 300, value_type=int
        )
        return max(0.0, min(float(raw), 3600.0))
    except Exception:
        return 300.0


def _soul_redistil_limit() -> int:
    """Return how many cells one re-distil press may process."""
    try:
        from core.config_manager import config_registry

        return int(
            config_registry.get_value("SOUL_REDISTIL_LIMIT", 5000, value_type=int)
        )
    except Exception:
        return 5000


class SoulPlugin(PluginBase):
    """Runtime integration plugin for SOUL architecture.

    This plugin keeps implementation isolated from high-risk core prompt code by
    injecting context through existing static injection plumbing.
    """

    display_name = "SOUL Plugin"
    allow_static_injection_stale_fallback = True
    static_injection_cache_ttl_seconds = 300.0
    # The stale-fallback payload is scoped to the conversation it was built for
    # (recalled memories, session state, the emotion delta and the situational notes,
    # some of which may be private to that chat). Nothing is shared across chats on a
    # timeout; on the normal path shared notes still reach every chat.
    static_injection_global_keys: frozenset[str] = frozenset()

    def static_injection_scope(
        self, message: Any = None, context_memory: dict[str, Any] | None = None
    ) -> str:
        """Cache scope for ``gather_static_injections``: the chat's interface path."""
        return self._extract_interface_path(message, context_memory)

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._repo = self._build_repository()
        self._emotion_engine = self._build_emotion_engine()
        self._compiler = SoulCompiler(
            repository=self._repo,
            memcell_extractor=self._build_memcell_extractor(),
            dsp_extractor=self._build_dsp_extractor(),
            dsp_builder=self._build_dsp_builder(),
            summary_builder=RuleBasedSummaryBuilder(),
            embedder=self._build_embedder(),
            curator=self._build_memcell_curator(),
        )
        self._buffers: dict[str, list[str]] = {}
        # When the current (not yet compiled) buffer of an interface started.
        # A compile reads the cached conversation from this moment on, so the
        # window holds exactly what the buffer holds — plus the other side.
        self._buffer_started: dict[str, datetime] = {}
        self._sessions: dict[str, _SessionState] = {}
        # When each cell was last injected into a prompt, used to rotate the
        # recalled set. In-process only: an ordering hint, never state.
        self._recall_cooldown_at: dict[str, float] = {}
        self._scheduler_task: asyncio.Task[None] | None = None
        self._last_rollup_date: date | None = None
        self._last_curator_date: date | None = None
        self._schedule_state_loaded = False
        self._last_consolidated_at: datetime | None = None
        # Manual re-distil pass (WebUI button): the task and its live counters.
        self._redistil_task: asyncio.Task[None] | None = None
        self._redistil_state: dict[str, Any] = {}
        # Brief cache for the "what would one press actually spend" preview, so
        # the Settings panel can poll it without re-examining the candidates on
        # every request.
        self._redistil_workable_cache: tuple[float, int] | None = None

    @staticmethod
    def _is_dsp_llm_enabled() -> bool:
        """Return whether the LLM DSP extractor/builder path is active.

        ``SOUL_DSP_LLM_ENABLED`` defaults on. The LLM strategies internally fall
        back to their rule-based counterparts on any failure, so enabling the
        LLM path can never break the nightly rollup.
        """
        try:
            from core.config_manager import config_registry

            return bool(
                config_registry.get_value("SOUL_DSP_LLM_ENABLED", 1, value_type=int)
            )
        except Exception:
            return True

    @staticmethod
    def _build_dsp_extractor() -> Any:
        """Return the DSP extractor: LLM-backed when enabled, else rule-based."""
        if not SoulPlugin._is_dsp_llm_enabled():
            return RuleBasedDspExtractor()
        try:
            from core.soul.llm_strategies import LlmDspExtractor

            return LlmDspExtractor(speaker_identity=_soul_speaker_identities())
        except Exception as exc:
            log_warning(
                f"[soul_plugin] LLM DSP extractor unavailable ({exc}); using rule-based"
            )
            return RuleBasedDspExtractor()

    @staticmethod
    def _is_memcell_llm_enabled() -> bool:
        """Return whether memcell content is distilled by an LLM.

        ``SOUL_MEMCELL_LLM_ENABLED`` defaults on. The extractor falls back to its
        rule-based counterpart on any failure, so enabling the LLM path can never
        break the compile.
        """
        try:
            from core.config_manager import config_registry

            return bool(
                config_registry.get_value("SOUL_MEMCELL_LLM_ENABLED", 1, value_type=int)
            )
        except Exception:
            return True

    @staticmethod
    def _build_memcell_extractor() -> Any:
        """Return the memcell extractor: LLM-distilled when enabled, else rule-based.

        The rule-based extractor stores the conversation text verbatim as the
        cell's trace, which is what made recall return raw transcript instead of
        distilled knowledge; the LLM path paraphrases each memory and writes real
        ``subject|predicate|object`` facts.
        """
        if not SoulPlugin._is_memcell_llm_enabled():
            return RuleBasedMemCellExtractor()
        try:
            from core.soul.llm_strategies import LlmMemCellExtractor

            return LlmMemCellExtractor(speaker_identity=_soul_speaker_identities())
        except Exception as exc:
            log_warning(
                "[soul_plugin] LLM memcell extractor unavailable "
                f"({exc}); using rule-based"
            )
            return RuleBasedMemCellExtractor()

    @staticmethod
    def _build_memcell_curator() -> Any:
        """Return the memory curator with its grace period from config.

        ``SOUL_CURATOR_MIN_AGE_HOURS`` (default 168 = one week) protects freshly
        compiled cells from the low-salience removal: without it a calm new cell
        scores 0.2 against a 0.4 threshold and is deleted the first time the
        curator runs, so the day's ordinary memories never survive the night.
        """
        try:
            from core.config_manager import config_registry

            hours = int(
                config_registry.get_value(
                    "SOUL_CURATOR_MIN_AGE_HOURS", 168, value_type=int
                )
            )
        except Exception:
            hours = 168
        return RuleBasedMemCellCurator(min_age_seconds=max(0, hours) * 3600.0)

    @staticmethod
    def _build_dsp_builder() -> Any:
        """Return the DSP builder: LLM-compiled when enabled, else rule-based.

        ``SOUL_DSP_LLM_ENABLED`` defaults on; the LLM builder internally falls
        back to the rule-based builder on any failure (no engine, exception,
        bad JSON), so enabling it can never break the nightly rollup.

        ``SOUL_SPEAKER_IDENTITIES`` reaches the builder as well as the
        extractors: the builder is the stage that decides which name and gender
        the standing profile carries, so it needs the declaration to rule a
        misattributed day out instead of letting it win as the newest evidence.
        """
        if not SoulPlugin._is_dsp_llm_enabled():
            return RuleBasedDspBuilder()
        try:
            from core.soul.llm_strategies import LlmDspBuilder

            return LlmDspBuilder(speaker_identity=_soul_speaker_identities())
        except Exception as exc:
            log_warning(
                f"[soul_plugin] LLM DSP builder unavailable ({exc}); using rule-based"
            )
            return RuleBasedDspBuilder()

    @staticmethod
    def _is_temporal_enabled() -> bool:
        """Return whether temporal situational context is active.

        ``SOUL_TEMPORAL_ENABLED`` defaults on. When disabled the stored notes
        are no longer injected into the prompt. Extraction lives in the
        ``debrief_situational_notes`` plugin and has its own toggle.
        """
        try:
            from core.config_manager import config_registry

            return bool(
                config_registry.get_value("SOUL_TEMPORAL_ENABLED", 1, value_type=int)
            )
        except Exception:
            return True

    def _build_embedder(self) -> Any:
        from importlib.util import find_spec

        backend = self._get_repository_backend()
        if backend == "postgres":
            # Honour the setup-wizard pick; default matches VECTOR(768).
            model_id = os.environ.get("SOUL_EMBEDDER_ID", "BAAI/bge-base-en-v1.5")
            try:
                if find_spec("fastembed") is None:
                    raise ModuleNotFoundError("fastembed")
                log_info(f"[soul_plugin] Using FastEmbedder model={model_id}")
                return FastEmbedder(model_id=model_id)
            except Exception as exc:
                log_warning(
                    f"[soul_plugin] FastEmbedder unavailable ({exc}), falling back to NoopEmbedder"
                )
        return NoopEmbedder()

    def _build_emotion_engine(self) -> EmotionalEngine:
        return EmotionalEngine(profile=self._load_emotional_profile())

    @staticmethod
    def _load_emotional_profile() -> EmotionalProfile:
        try:
            from core.config_manager import config_registry

            skin = str(
                config_registry.get_value("SYNTH_NAME", "SyntH", value_type=str)
                or "SyntH"
            )
            persona_path = Path("skins") / skin / "persona.json"
            if persona_path.is_file():
                data = json.loads(persona_path.read_text(encoding="utf-8"))
                ep_data = data.get("emotional_profile")
                if isinstance(ep_data, dict):
                    return EmotionalProfile.from_dict(ep_data)
        except Exception:
            pass
        return EmotionalProfile()

    async def start(self) -> None:
        if self._scheduler_task and not self._scheduler_task.done():
            return
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
        # No curator pass here: the scheduler tick runs it once a day, and it only
        # does so after the config store is readable. A run at start() read the
        # "last ran" day before the store was ready, saw none, and repeated the
        # pass on every restart.
        if isinstance(self._repo, InMemorySoulRepository):
            # Everything SOUL learns would be gone at the next restart. Say so at
            # error level: a deployment on a non-PostgreSQL main database used to
            # fall back to this without a word.
            log_error(
                "[soul_plugin] Long-term memory is NOT persistent: the repository is "
                "in memory (the main database is not PostgreSQL, or its DSN is empty). "
                "Memories, notes and retractions will be lost on restart."
            )
        log_info("[soul_plugin] Started")

    async def stop(self) -> None:
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        close_fn = getattr(self._repo, "close", None)
        if close_fn is not None:
            try:
                await close_fn()
            except Exception as exc:
                log_warning(f"[soul_plugin] Repository close failed: {exc}")
        self._scheduler_task = None
        log_info("[soul_plugin] Stopped")

    def get_supported_actions(self) -> dict[str, dict[str, object]]:
        return {
            "static_inject": {
                "description": "Inject SOUL DSP/session-state/foresight context",
                "required_params": {},
                "optional_params": {},
            },
        }

    async def execute_action(
        self,
        action: dict[str, Any],
        context: dict[str, Any],
        bot: Any,
        original_message: Any,
    ) -> Any:
        action_type = action.get("type")

        if action_type == "static_inject":
            return await self.get_static_injection(original_message, context)
        return None

    async def get_static_injection(
        self, message: Any = None, context_memory: dict[str, Any] | None = None
    ) -> dict[str, object]:
        interface_path = self._extract_interface_path(message, context_memory)
        if interface_path.startswith("grillo/"):
            return await self._get_grillo_beat_context(message)

        # Vessel-focus turns suppress global memory (history_engine does the
        # same for the core tier): recall here would leak it back in via
        # soul_recalled_memories. Session state/foresight below still apply.
        try:
            from core.vessel_focus import is_vessel_turn

            _vessel_turn = bool(is_vessel_turn(message, context_memory, interface_path))
        except Exception:
            _vessel_turn = False

        now = datetime.now(timezone.utc)

        session = self._sessions.get(interface_path)
        if session is None:
            session = _SessionState()
            self._sessions[interface_path] = session

        hours_elapsed = max(
            0.0,
            (now - session.last_decay_applied).total_seconds() / 3600.0,
        )
        if hours_elapsed > 0:
            session.emotional_state = self._emotion_engine.apply_time_decay(
                session.emotional_state, hours_elapsed
            )
            session.last_decay_applied = now

        # The emotional subsystem can be switched off for a whole deployment
        # (README: Plugins tab -> Emotion Manager -> off, needed to comply with the
        # anthropomorphic-AI rules some jurisdictions apply). SOUL's own mood engine
        # is part of that subsystem: left running it would still tag turns with
        # joy/fear/sad/anger and bias recall by mood.
        emotions_on = self._emotions_enabled()

        if message is not None:
            try:
                from core.chat_context import resolve_chat_kind

                kind = resolve_chat_kind(message, interface_path)
                if kind:
                    session.chat_kind = kind
            except Exception:
                pass

        incoming_text = self._extract_message_text(message)
        if incoming_text:
            # Buffer the line WITH its speaker. The rule-based extractor stores
            # the transcript verbatim as the cell's episodic trace, so an
            # unattributed line made the user's own words come back later as an
            # unattributed "recalled memory" — the synth read what the human said
            # as something it recalled. The label survives into the cell.
            self._append_buffer(
                interface_path, self._labelled_buffer_line(message, incoming_text)
            )
            self._note_participant(session, message)
            if emotions_on:
                event = self._infer_emotional_event(incoming_text)
                session.emotional_state = self._emotion_engine.apply_event(
                    session.emotional_state, event
                )

        session.last_seen = now

        active_dsp = await self._repo.get_active_dsp()
        foresight = await self._repo.list_active_foresight_signals(now.date())
        turn_delta = self._emotion_engine.to_turn_delta_payload(session.emotional_state)
        recalled_memories: list[str] = []

        if incoming_text and not _vessel_turn:
            try:
                recalled_memories = await self._recall_memories(
                    interface_path=interface_path,
                    incoming_text=incoming_text,
                    session=session,
                )
            except Exception as exc:
                log_warning(f"[soul_plugin] Memory recall failed: {exc}")

        foresight_lines = [
            f"- {signal.content} (until {signal.valid_until.isoformat()})"
            for signal in foresight[:8]
        ]
        foresight_text = "\n".join(foresight_lines) if foresight_lines else "- None"

        emotion_line = (
            f"emotion_snapshot: {json.dumps(turn_delta['e'])}\n" if emotions_on else ""
        )
        session_state = (
            "<session_state>\n"
            f"interface_path: {interface_path}\n"
            f"active_foresight:\n{foresight_text}\n"
            f"{emotion_line}"
            "</session_state>"
        )

        return {
            "soul_user_profile": active_dsp.content
            if active_dsp
            else "<user_profile>No profile compiled yet.</user_profile>",
            "soul_session_state": session_state,
            "soul_turn_emotion_delta": json.dumps(turn_delta) if emotions_on else "{}",
            "soul_active_foresight": [
                {
                    "content": signal.content,
                    "valid_until": signal.valid_until.isoformat(),
                    "trigger": signal.trigger,
                }
                for signal in foresight[:8]
            ],
            "soul_recalled_memories": recalled_memories,
            "soul_temporal_context": await self._get_temporal_context(
                now, session_id=self._normalize_session_id(interface_path)
            ),
        }

    async def _get_temporal_context(
        self, now: datetime, session_id: str | None = None
    ) -> list[dict[str, Any]]:
        """Return active situational notes as renderable dicts.

        Gated on ``SOUL_TEMPORAL_ENABLED``; fail-safe on any error.
        """
        if not SoulPlugin._is_temporal_enabled():
            return []
        try:
            from core.soul.time_resolution import TemporalRenderer

            renderer = TemporalRenderer(now=now)
            lookback = self._get_lookback_days()
            cutoff = now - timedelta(days=lookback)
            notes = await self._repo.list_active_situational_notes(now=now)
            result: list[dict[str, Any]] = []
            for note in notes:
                if note.created_at and note.created_at < cutoff:
                    continue
                if note.visibility == "private" and note.session_id != session_id:
                    # Told in confidence in another chat: not shown here.
                    continue
                result.append(
                    {
                        "note_type": note.note_type,
                        "subject": note.subject,
                        "summary": note.summary,
                        "priority": note.priority,
                        "confidence": note.confidence,
                        "valid_from_relative": renderer.render_relative(
                            note.valid_from
                        ),
                        "valid_until_relative": renderer.render_relative(
                            note.valid_until
                        ),
                        # Absolute bounds as well: the summary is prose written on
                        # the day the note was filed and can still say "tomorrow"
                        # days later, so the renderer prints the window the note
                        # was filed for next to it.
                        "valid_from": note.valid_from.isoformat()
                        if note.valid_from
                        else None,
                        "valid_until": note.valid_until.isoformat()
                        if note.valid_until
                        else None,
                        "source": note.source,
                    }
                )
            return self.select_temporal_notes(
                result, limit=SoulPlugin._get_temporal_inject_limit()
            )
        except Exception as exc:
            log_debug(f"[soul_plugin] Temporal context injection failed: {exc}")
            return []

    @staticmethod
    def _get_temporal_inject_limit() -> int:
        """How many active notes may be rendered into one prompt."""
        try:
            from core.config_manager import config_registry

            value = int(
                config_registry.get_value(
                    "SOUL_TEMPORAL_INJECT_LIMIT", 8, value_type=int
                )
            )
        except Exception:
            return _SOUL_TEMPORAL_INJECT_LIMIT
        return max(1, min(value, 40))

    @staticmethod
    def select_temporal_notes(
        notes: list[dict[str, Any]], *, limit: int
    ) -> list[dict[str, Any]]:
        """Rank active notes and drop near-duplicates about the same thing.

        The debrief writes a fresh note every time it re-describes a
        circumstance, so the active set accumulates several accounts of one event
        (measured live: 12 active notes about a single evening gathering, some
        of them contradicting each other). The store keeps them all; what the
        model is shown is bounded here, ranked by ``priority`` then
        ``confidence`` (a stable sort, so the repository's soonest-expiry-first
        order survives ties), skipping a note whose subject tokens are contained
        in an already-selected subject. Single-token subjects ("Scar",
        "Human") never take part in the containment test: they name a person,
        not a circumstance, so they cannot stand in for another note.
        """
        ranked = sorted(
            notes,
            key=lambda note: (
                -int(note.get("priority") or 0),
                -float(note.get("confidence") or 0.0),
            ),
        )
        selected: list[dict[str, Any]] = []
        seen: list[set[str]] = []
        for note in ranked:
            tokens = subject_tokens(note.get("subject"))
            # Single-token subjects ("Scar", "Human", "gathering") name a person
            # or a bare topic, so they neither take part in the containment test
            # nor become a reference that could absorb a richer note.
            if any(is_same_circumstance(tokens, other) for other in seen):
                continue
            if len(tokens) >= 2:
                seen.append(tokens)
            selected.append(note)
            if len(selected) >= max(1, limit):
                break
        return selected

    @staticmethod
    def _get_lookback_days() -> int:
        try:
            from core.config_manager import config_registry

            return max(
                1,
                int(
                    config_registry.get_value(
                        "SOUL_TEMPORAL_LOOKBACK_DAYS", 1, value_type=int
                    )
                    or 1
                ),
            )
        except Exception:
            return 1

    async def _get_grillo_beat_context(self, message: Any) -> dict[str, object]:
        """Return passive SOUL context for Grillo beats.

        Provides recalled memories and DSP without session side-effects:
        no buffer append, no emotional tracking, no session mutation.
        """
        active_dsp = None
        recalled_memories: list[str] = []
        foresight: list[Any] = []

        try:
            active_dsp = await self._repo.get_active_dsp()
        except Exception:
            pass

        try:
            foresight = await self._repo.list_active_foresight_signals(
                datetime.now(timezone.utc).date()
            )
        except Exception:
            pass

        incoming_text = self._extract_message_text(message)
        if incoming_text:
            try:
                recalled_memories = await self._recall_memories(
                    interface_path="grillo/beat",
                    incoming_text=incoming_text,
                    session=_SessionState(),
                    count_retrieval=False,
                )
            except Exception as exc:
                log_debug(f"[soul_plugin] Grillo beat memory recall failed: {exc}")

        foresight_lines = [
            f"- {signal.content} (until {signal.valid_until.isoformat()})"
            for signal in foresight[:8]
        ]
        foresight_text = "\n".join(foresight_lines) if foresight_lines else "- None"

        return {
            "soul_user_profile": active_dsp.content
            if active_dsp
            else "<user_profile>No profile compiled yet.</user_profile>",
            "soul_session_state": (
                "<session_state>\ninterface_path: grillo/beat\n"
                f"active_foresight:\n{foresight_text}\n</session_state>"
            ),
            "soul_turn_emotion_delta": "{}",
            "soul_active_foresight": [
                {
                    "content": signal.content,
                    "valid_until": signal.valid_until.isoformat(),
                    "trigger": signal.trigger,
                }
                for signal in foresight[:8]
            ],
            "soul_recalled_memories": recalled_memories,
            "soul_temporal_context": await self._get_temporal_context(
                datetime.now(timezone.utc)
            ),
        }

    async def _scheduler_loop(self) -> None:
        while True:
            try:
                await self._tick_scheduler()
            except Exception as exc:
                log_error(f"[soul_plugin] Scheduler tick failed: {exc}")
            await asyncio.sleep(max(5, self._get_scheduler_interval()))

    _ROLLUP_DATE_KEY = "SOUL_LAST_ROLLUP_DATE"
    _CURATOR_DATE_KEY = "SOUL_LAST_CURATOR_DATE"

    @staticmethod
    async def _read_persisted_date(key: str) -> date | None:
        # ``get_persisted_value`` reads the database itself. ``get_value`` called
        # from the event loop answers with the default until the async load has
        # run, which at startup is always: a restart then looked like "never ran
        # today" and repeated the rollup and the curator.
        try:
            from core.config_manager import config_registry

            raw = str(await config_registry.get_persisted_value(key, "") or "").strip()
            return date.fromisoformat(raw) if raw else None
        except Exception:
            return None

    @staticmethod
    async def _persist_date(key: str, value: date) -> None:
        try:
            from core.config_manager import config_registry

            await config_registry.set_value(
                key, value.isoformat(), require_persist=True
            )
        except Exception as exc:
            log_debug(f"[soul_plugin] Could not persist {key}: {exc}")

    async def _load_schedule_state(self) -> None:
        """Restore the last rollup/curator day so a restart does not repeat them.

        ``_last_rollup_date`` used to live only in memory: every process start ran
        the nightly rollup again, writing a fresh evidence row from the same 24 h
        window each time. The DSP builders count a fact as stable after two
        occurrences, so restarts inflated that count (40 profile rebuilds in
        four months on the live store).
        """
        if self._schedule_state_loaded:
            return
        self._schedule_state_loaded = True
        if self._last_rollup_date is None:
            self._last_rollup_date = await self._read_persisted_date(
                self._ROLLUP_DATE_KEY
            )
        if self._last_curator_date is None:
            self._last_curator_date = await self._read_persisted_date(
                self._CURATOR_DATE_KEY
            )

    async def _tick_scheduler(self) -> None:
        now = datetime.now(timezone.utc)
        await self._load_schedule_state()

        idle_cutoff_seconds = self._get_compile_idle_seconds()
        for interface_path, session in list(self._sessions.items()):
            idle_seconds = (now - session.last_seen).total_seconds()
            if idle_seconds >= idle_cutoff_seconds and self._buffers.get(
                interface_path
            ):
                # One interface failing must not skip the others, the backfill or
                # the daily jobs below: the failure is retried on the next tick.
                try:
                    await self._compile_interface(interface_path)
                except Exception as exc:
                    log_warning(
                        f"[soul_plugin] Compile failed for {interface_path}: {exc}"
                    )

        backfilled = await self._compiler.backfill_embeddings(limit=50)
        if backfilled > 0:
            log_info(
                f"[soul_plugin] Backfilled {backfilled} missing memcell embeddings"
            )

        today = now.date()
        if self._last_rollup_date != today:
            await self._run_rollup_now()
            self._last_rollup_date = today
            await self._persist_date(self._ROLLUP_DATE_KEY, today)

        if self._last_curator_date != today:
            await self._run_curator_now()
            self._last_curator_date = today
            await self._persist_date(self._CURATOR_DATE_KEY, today)
            await self._purge_archive_now(now)

    async def _compile_interface(
        self,
        interface_path: str,
        *,
        force_consolidate: bool = False,
    ) -> int:
        lines = list(self._buffers.get(interface_path, []))
        if not lines:
            return 0
        # Lines arrive on the live path (``get_static_injection``) while this
        # compile awaits the LLM, the embedder and the consolidation. Only the
        # lines compiled here are dropped afterwards; anything appended meanwhile
        # stays queued for the next pass instead of being wiped with the buffer.

        # Roleplay/explicit turns stay in the buffer (they still drive emotion
        # and memory recall) but are excluded from what gets compiled into
        # memcells — in-character fiction is not a durable event record.
        from core.soul.roleplay import strip_roleplay_lines

        if is_housekeeping_session(self._normalize_session_id(interface_path)):
            # System text (the diary consolidator's transcripts, web-search notices)
            # is not a conversation: compiled, it filled a live store with 354
            # near-identical cells that recall then had to be told to ignore.
            self._drop_compiled_lines(interface_path, lines)
            return 0

        transcript = strip_roleplay_lines(
            await self._compile_transcript(interface_path, lines)
        )
        if not transcript.strip():
            self._drop_compiled_lines(interface_path, lines)
            return 0

        safe_session_id = self._normalize_session_id(interface_path)

        # What was said in a one-to-one chat stays in that chat (MemCell.visibility).
        session = self._sessions.get(interface_path)
        private_window = bool(session and session.force_private)
        visibility = (
            "private"
            if session and (session.chat_kind == "dm" or session.force_private)
            else "shared"
        )
        created = await self._compiler.post_session_compile(
            current_date=datetime.now(timezone.utc).date(),
            transcript=transcript,
            session_id=safe_session_id,
            visibility=visibility,
            participants=[
                {"id": pid, "name": name}
                for pid, name in (
                    session.window_participants.items() if session else ()
                )
            ],
        )
        consolidated = await self._maybe_consolidate(force=force_consolidate)

        self._drop_compiled_lines(interface_path, lines)
        if session is not None:
            session.window_participants.clear()
        if private_window and session is not None:
            session.force_private = False
        log_info(
            f"[soul_plugin] Compiled {len(created)} memcells for {interface_path} "
            f"(consolidated {consolidated} scene(s))"
        )
        return len(created)

    def _drop_compiled_lines(self, interface_path: str, compiled: list[str]) -> None:
        """Remove the lines that were compiled, keep anything that arrived since.

        The buffer is trimmed from the front once it passes 200 lines, so while a
        slow compile ran the compiled lines may have moved: they are matched by
        content (the longest tail of the compiled lines that still opens the
        buffer), not by position.
        """
        current = list(self._buffers.get(interface_path, []))
        dropped = 0
        for offset in range(len(compiled) + 1):
            tail = compiled[offset:]
            if tail and current[: len(tail)] == tail:
                dropped = len(tail)
                break
        remaining = current[dropped:]
        self._buffers[interface_path] = remaining
        if remaining:
            # Re-anchor on the leftovers: they arrived after the compiled window.
            # (The cache read still reaches back a 30-minute margin before this
            # anchor, so the next compile may repeat a few already-compiled lines
            # rather than drop any.)
            self._buffer_started[interface_path] = datetime.now(timezone.utc)
        else:
            self._buffer_started.pop(interface_path, None)

    async def _compile_transcript(self, interface_path: str, lines: list[str]) -> str:
        """Both sides of the conversation, or the buffered lines when they are all
        the cache has.

        ``self._buffers`` is fed by the INCOMING path only (``get_static_injection``
        is its sole writer), so a compile distilled from it alone saw the human
        talking into a void: measured live 2026-09-23, the 12:12 compile extracted
        three ``Scar:`` lines while the persona's own 12:07/12:05/12:02 replies sat
        in ``chat_history_cache``, and the cells it produced filed the persona's own
        act as the user's ("The user shared a message from a woman they call their
        mother"). The extractor is asked to say who did or said what, which a
        one-sided transcript cannot answer.

        So the cached conversation from the moment the buffer STARTED is preferred:
        it holds the same lines the buffer holds, plus the persona's own lines,
        ordered by time and labelled by the same speaker rule the DSP transcript
        uses. The buffer remains the fallback for an interface that does not write
        the cache (or a cache read that fails), and the cache is only trusted when
        it actually covers every buffered line — a partial window must not silently
        drop what the buffer holds.
        """
        cached = await self._cached_compile_lines(interface_path, lines)
        if cached and self._cache_covers_buffer(cached, lines):
            return "\n".join(cached)
        return "\n".join(lines)

    async def _cached_compile_lines(
        self, interface_path: str, lines: list[str]
    ) -> list[str]:
        """The cached conversation for this interface, from the first buffered line.

        The window is anchored at the buffer start with a short backward grace (a
        cache row can be written a moment before the injection that buffers the
        line), and then TRIMMED to the first buffered line's own row: everything
        before it belongs to a compile that already ran, and re-feeding it would
        duplicate cells.
        """
        started = self._buffer_started.get(interface_path)
        if started is None:
            return []
        cutoff = started - timedelta(seconds=_SOUL_COMPILE_TRANSCRIPT_LOOKBACK_SECONDS)
        try:
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT sender_name, sender_id, message_text, created_at
                        FROM chat_history_cache
                        WHERE created_at >= %s AND interface_path = %s
                        ORDER BY created_at ASC
                        LIMIT 500
                        """,
                        (cutoff, interface_path),
                    )
                    rows = await cur.fetchall()
        except Exception as exc:
            log_debug(
                f"[soul_plugin] Compile transcript fell back to the buffer: {exc}"
            )
            return []

        parts: list[str] = []
        for row in rows:
            if not row or not row[2]:
                continue
            speaker = self._transcript_speaker_label(str(row[0] or row[1] or "user"))
            parts.append(f"{speaker}: {' '.join(str(row[2]).split())}")

        if lines:
            first = self._buffer_body(lines[0])
            if first:
                for index, part in enumerate(parts):
                    if first in " ".join(part.split()).casefold():
                        return parts[index:]
        return parts

    @staticmethod
    def _buffer_body(line: str) -> str:
        """One buffered line's message text, without its speaker label."""
        head, sep, tail = str(line or "").partition(": ")
        return " ".join((tail if sep else head).split()).casefold()

    @classmethod
    def _cache_covers_buffer(cls, cached: list[str], lines: list[str]) -> bool:
        """Whether every buffered line is present in the cached transcript."""
        bodies = [" ".join(line.split()).casefold() for line in cached]
        for line in lines:
            wanted = cls._buffer_body(line)
            if not wanted:
                continue
            if not any(wanted in body or body.endswith(wanted) for body in bodies):
                return False
        return True

    async def _maybe_consolidate(self, *, force: bool = False) -> int:
        now = datetime.now(timezone.utc)

        if not force and self._last_consolidated_at is not None:
            elapsed = (now - self._last_consolidated_at).total_seconds()
            if elapsed < _SOUL_CONSOLIDATE_COOLDOWN_SECONDS:
                log_debug(
                    "[soul_plugin] Skipping async_consolidate: cooldown active "
                    f"({elapsed:.0f}s < {_SOUL_CONSOLIDATE_COOLDOWN_SECONDS}s)"
                )
                return 0

        scene_ids = await self._compiler.async_consolidate()
        self._last_consolidated_at = now
        return len(scene_ids)

    async def _force_compile(self, interface_path: str | None = None) -> dict[str, int]:
        if interface_path:
            count = await self._compile_interface(
                interface_path,
                force_consolidate=True,
            )
            return {"compiled_memcells": count}

        total = 0
        for key in list(self._buffers.keys()):
            total += await self._compile_interface(key, force_consolidate=True)
        return {"compiled_memcells": total}

    async def _run_rollup_now(self) -> dict[str, int]:
        # On the LLM DSP path the transcript is judged by the model itself, so
        # the aggressive roleplay regex filter is not needed there — the model is
        # instructed to ignore in-character speech. The deterministic rule-based
        # path keeps the filter so its structural guards never see RP content.
        transcript = await self._build_daily_transcript(
            filter_roleplay=not SoulPlugin._is_dsp_llm_enabled()
        )
        backfilled = await self._compiler.backfill_embeddings(limit=500)
        result = await self._compiler.nightly_rollup(
            current_date=datetime.now(timezone.utc).date(),
            transcript=transcript,
            session_id="nightly",
        )
        result["embeddings_backfilled"] = backfilled
        log_info(f"[soul_plugin] Nightly rollup result: {result}")
        return result

    @staticmethod
    def _get_curator_max_memories() -> int:
        try:
            from core.config_manager import config_registry

            return max(
                1,
                int(
                    config_registry.get_value(
                        "SOUL_CURATOR_MAX_MEMORIES", 5000, value_type=int
                    )
                ),
            )
        except Exception:
            return 5000

    @staticmethod
    def _is_curator_over_cap_only() -> bool:
        try:
            from core.config_manager import config_registry

            return bool(
                int(
                    config_registry.get_value(
                        "SOUL_CURATOR_EVICT_ONLY_OVER_CAP", 1, value_type=int
                    )
                )
            )
        except Exception:
            return True

    @staticmethod
    def _archive_retention_days(key: str, default: int) -> int:
        try:
            from core.config_manager import config_registry

            return max(0, int(config_registry.get_value(key, default, value_type=int)))
        except Exception:
            return default

    async def _purge_archive_now(self, now: datetime) -> dict[str, int]:
        """Delete archived cells past their retention (once a day, after the curator)."""
        removed = {"copies": 0, "evicted": 0}
        copy_days = self._archive_retention_days("SOUL_ARCHIVE_COPY_RETENTION_DAYS", 30)
        if copy_days:
            for reason in ("duplicate", "housekeeping"):
                removed["copies"] += await self._repo.purge_archive(
                    older_than=now - timedelta(days=copy_days), reason=reason
                )
        days = self._archive_retention_days("SOUL_ARCHIVE_RETENTION_DAYS", 365)
        if days:
            removed["evicted"] = await self._repo.purge_archive(
                older_than=now - timedelta(days=days)
            )
        if removed["copies"] or removed["evicted"]:
            log_info(f"[soul_plugin] Archive purge: {removed}")
        return removed

    async def _run_curator_now(
        self, *, max_memories: int | None = None
    ) -> dict[str, int]:
        result = await self._compiler.run_curator(
            current_date=datetime.now(timezone.utc).date(),
            max_memories=(
                max_memories
                if max_memories is not None
                else self._get_curator_max_memories()
            ),
            evict_only_over_cap=self._is_curator_over_cap_only(),
        )
        log_info(
            f"[soul_plugin] Memory Curator: inspected={result.inspected} "
            f"removed={result.removed} retained={result.retained} "
            f"(future={result.kept_future} important={result.kept_important} "
            f"calm={result.kept_calm})"
        )
        return {
            "inspected": result.inspected,
            "removed": result.removed,
            "retained": result.retained,
            "kept_future": result.kept_future,
            "kept_important": result.kept_important,
        }

    async def _get_status(self) -> dict[str, object]:
        dsp = await self._repo.get_active_dsp()
        return {
            "enabled": True,
            "tracked_sessions": len(self._sessions),
            "buffered_sessions": sum(1 for v in self._buffers.values() if v),
            "active_dsp": bool(dsp),
            "foresight_active": len(
                await self._repo.list_active_foresight_signals(
                    datetime.now(timezone.utc).date()
                )
            ),
        }

    async def _build_daily_transcript(self, *, filter_roleplay: bool = True) -> str:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=1)
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT sender_name, sender_id, message_text, created_at
                        FROM chat_history_cache
                        WHERE created_at >= %s
                          AND (interface_path IS NULL OR NOT (
                                interface_path LIKE 'vessel/%'
                             OR interface_path LIKE 'grillo/%'
                          ))
                        ORDER BY created_at DESC
                        LIMIT 500
                        """,
                        (cutoff,),
                    )
                    # Newest 500 of the window, replayed oldest-first: ASC + LIMIT
                    # kept the OLDEST 500 and dropped the end of a busy day.
                    rows = list(reversed(await cur.fetchall()))
                    parts: list[str] = []
                    for row in rows:
                        if not row or not row[2]:
                            continue
                        speaker = self._transcript_speaker_label(
                            str(row[0] or row[1] or "user")
                        )
                        message_text = " ".join(str(row[2]).split())
                        timestamp = row[3]
                        prefix = f"[{timestamp.isoformat()}] " if timestamp else ""
                        parts.append(
                            f"{prefix}{speaker}: {json.dumps(message_text, ensure_ascii=False)}"
                        )
                    transcript = "\n".join(parts)
                    # Roleplay/explicit turns are in-character fiction, not a
                    # stable record of the user — keep them out of the DSP
                    # profile input on the deterministic path (structural
                    # data-cleaning, never routing). The LLM DSP extractor
                    # receives the unfiltered transcript and judges it itself.
                    if filter_roleplay:
                        from core.soul.roleplay import strip_roleplay_lines

                        transcript = strip_roleplay_lines(transcript)
                    return transcript
        except Exception as exc:
            log_debug(f"[soul_plugin] Falling back to buffered transcript: {exc}")

        parts: list[str] = []
        for lines in self._buffers.values():
            parts.extend(lines)
        return "\n".join(parts)

    # The interfaces cache the persona's OWN messages under the canonical label
    # "self" (`sender_name="self"`, the same convention Discord, Telegram and the
    # Vessel use). Handed to an extractor as-is, "self" is an unnamed third party
    # in a conversation that may hold several people, and the DSP extractor then
    # has to GUESS which speaker is the human from the other labels alone.
    # Measured live on 2026-09-22: the 2D deployment's transcript carried exactly
    # `self:`, `Scar:` and `2B:`, the model read `self` as the human, and the
    # profile it compiled said "Dee goes by the name Scar and is a grown woman"
    # — the persona's name and gender handed to the human. Naming the persona's
    # lines outright removes the guess.
    _SELF_SPEAKER_LABELS = frozenset(
        {"self", "me", "assistant", "synt", "synth", "bot"}
    )

    @classmethod
    def _transcript_speaker_label(cls, speaker: str) -> str:
        """Render one cached sender label so the persona's own lines say so.

        The persona's own lines (its configured ``SYNTH_NAME``, or the canonical
        "self" label) become ``"<name> (the persona)"``; every other label is
        left exactly as the interface stored it, because a name is evidence about
        whoever carries it and rewriting one would be worse than leaving it.
        """
        label = " ".join(str(speaker or "").split()) or "user"
        persona = cls._persona_display_name()
        folded = label.casefold()
        is_self = folded in cls._SELF_SPEAKER_LABELS or (
            bool(persona) and folded == persona.casefold()
        )
        if not is_self:
            return label
        return f"{persona} (the persona)" if persona else "the persona"

    @staticmethod
    def _persona_display_name() -> str:
        """The persona's configured display name (``SYNTH_NAME``), or ``''``."""
        try:
            from core.config_manager import config_registry

            return str(
                config_registry.get_value("SYNTH_NAME", "", value_type=str) or ""
            ).strip()
        except Exception:
            return ""

    @staticmethod
    def _buffer_speaker(message: Any) -> str:
        """Best-effort name of the buffered line's author ('user' when unknown)."""
        for attr in ("sender_name", "speaker", "author"):
            value = getattr(message, attr, None)
            if value and str(value).strip():
                return str(value).strip()
        try:
            from core.user_utils import get_user_display_name

            user = getattr(message, "from_user", None)
            if user is not None:
                label = str(get_user_display_name(user) or "").strip()
                if label:
                    return label
        except Exception:
            pass
        return "user"

    @classmethod
    def _note_participant(cls, session: "_SessionState", message: Any) -> None:
        """Remember who spoke in this window (humans only, never a bot)."""
        user = getattr(message, "from_user", None)
        identity = getattr(user, "id", None)
        if user is None or identity is None or getattr(user, "is_bot", False):
            return
        session.window_participants[str(identity)] = cls._buffer_speaker(message)

    @classmethod
    def _labelled_buffer_line(cls, message: Any, text: str) -> str:
        """Prefix a buffered line with who said it.

        The rule-based extractor stores the transcript verbatim as the memory
        cell's episodic trace, so this label is what later tells the synth whose
        words a recalled memory actually holds.
        """
        return f"{cls._buffer_speaker(message)}: {text.strip()}"

    def _append_buffer(self, interface_path: str, text: str) -> None:
        self._buffers.setdefault(interface_path, []).append(text.strip())
        # Anchor the compile window at the FIRST line of this buffer: the cached
        # conversation from here on is what a compile distils (see
        # `_compile_transcript`), and the previous window has already been both
        # compiled and cleared.
        self._buffer_started.setdefault(interface_path, datetime.now(timezone.utc))
        # Keep bounded memory per interface.
        if len(self._buffers[interface_path]) > 200:
            self._buffers[interface_path] = self._buffers[interface_path][-200:]

    async def _recall_memories(
        self,
        *,
        interface_path: str,
        incoming_text: str,
        session: _SessionState,
        count_retrieval: bool = True,
    ) -> list[str]:
        """Recall memories for a prompt.

        ``count_retrieval`` is False for turns nobody asked for (a Grillo beat): a
        memory those bring back says nothing about its usefulness to a person.
        """
        normalized_query = self._normalize_query_text(incoming_text)
        if len(normalized_query) < 5:
            return []

        query_embedding = await self._compiler.embedder.embed(normalized_query)
        safe_session_id = self._normalize_session_id(interface_path)
        linked_sessions = self._linked_recall_sessions(safe_session_id)
        candidates = await self._repo.recall_memories(
            query_text=normalized_query,
            query_embedding=query_embedding,
            session_id=safe_session_id,
            linked_session_ids=linked_sessions,
            limit=_SOUL_RECALL_LIMIT,
            candidate_limit=_SOUL_RECALL_CANDIDATE_LIMIT,
        )
        if not candidates:
            return []

        # A cell the compiler wrote before the distilling extractor existed still
        # holds the raw session transcript, and it cannot be paraphrased on read,
        # so it is not recalled until the re-distil pass rewrites it. Gated on the
        # active extractor: with the rule-based one nothing is ever stamped, so
        # "unstamped" would describe every cell and the block would go empty.
        skip_undistilled = self._memcell_distillation_active()

        reranked: list[MemCellRecall] = []
        seen_ids: set[str] = set()
        for match in candidates:
            cell = match.cell
            if cell.id in seen_ids:
                continue
            if self._should_exclude_recalled_memory(cell):
                continue
            # TODO(privacy): ``visibility`` only covers cells compiled from a chat the
            # interface reports as one-to-one (and the Telegram cells backfilled by
            # scripts/backfill_cell_visibility.py). Cells from other interfaces are
            # still ``shared`` unless the Debrief judges the exchange confidential,
            # and journal cells carry no audience at all.
            if (
                cell.visibility == "private"
                and cell.session_id != safe_session_id
                and cell.session_id not in (linked_sessions or ())
            ):
                # A memory of someone else's private chat: not repeated here.
                continue
            if skip_undistilled and cell.distilled_at is None:
                continue
            # Roleplay/explicit exchanges are in-character fiction, not a stable
            # record of the user or an event (the compile path already strips
            # them via strip_roleplay_lines — this keeps recall consistent).
            # Feeding them back into a Grillo reflection beat made the beat
            # elaborate the explicit content into ever-more-explicit diary
            # entries (observed in tag_elaboration langfuse 36cb0aca). Structural
            # detector from core.soul.roleplay; data-cleaning only, never routing.
            try:
                from core.soul.roleplay import is_roleplay_turn

                if is_roleplay_turn(cell.episodic_trace):
                    continue
            except Exception:
                pass
            memory_emotion = self._normalize_memory_emotion(
                cell.emotional_tag.dominant_emotion
            )
            if memory_emotion is not None and self._emotions_enabled():
                match.score = self._emotion_engine.mood_congruent_boost(
                    session.emotional_state,
                    memory_emotion,
                    match.score,
                )
            reranked.append(match)
            seen_ids.add(cell.id)

        reranked.sort(
            key=lambda match: (
                match.score,
                match.similarity,
                match.cell.event_timestamp,
            ),
            reverse=True,
        )
        selected: list[MemCellRecall] = []
        seen_traces: set[str] = set()
        seen_sources: set[str] = set()
        # Cells held back only because they were injected moments ago. They are
        # kept as a fallback so a small or very repetitive store still fills the
        # block rather than going empty.
        cooling: list[MemCellRecall] = []
        # Ingested cells beyond their share; only used if slots are left over.
        overflow: list[MemCellRecall] = []
        ingested_selected = 0
        cooldown_seconds = _soul_recall_cooldown_seconds()
        now_cooldown = time.monotonic()
        for match in reranked:
            # Two cells can hold the same line (a turn compiled twice, a scene
            # folded back in). Injecting it twice wastes prompt space and is what
            # makes a memory block read as repetitive.
            key = self._trace_key(match.cell.episodic_trace)
            if key and key in seen_traces:
                continue
            seen_traces.add(key)
            is_ingested = match.cell.source_kind != "chat"
            if is_ingested and match.cell.source_key:
                # Blocks of one long diary entry all match the same topic: one of
                # them is enough, the rest would tell the same day again.
                source = (
                    f"{match.cell.source_kind}:{source_base(match.cell.source_key)}"
                )
                if source in seen_sources:
                    continue
                seen_sources.add(source)
            if is_ingested and ingested_selected >= _SOUL_RECALL_INGESTED_MAX:
                overflow.append(match)
                continue
            if cooldown_seconds > 0.0:
                cooldown_key = _recall_cooldown_key(safe_session_id, match.cell.id)
                # ``time.monotonic()`` counts from the machine's boot, so a default of
                # 0.0 made every never-seen cell look "recalled just now" during the
                # first ``cooldown_seconds`` after a boot and recall returned nothing.
                last_recall = self._recall_cooldown_at.get(cooldown_key)
                if (
                    last_recall is not None
                    and now_cooldown - last_recall < cooldown_seconds
                ):
                    cooling.append(match)
                    continue
            selected.append(match)
            if is_ingested:
                ingested_selected += 1
            if len(selected) >= _SOUL_RECALL_LIMIT:
                break

        # Nothing (or too little) new to show: fall back to the freshest held-back
        # cells instead of recalling nothing at all.
        for match in cooling:
            if len(selected) >= _SOUL_RECALL_LIMIT:
                break
            selected.append(match)
        for match in overflow:
            if len(selected) >= _SOUL_RECALL_LIMIT:
                break
            selected.append(match)

        if cooldown_seconds > 0.0:
            for match in selected:
                self._recall_cooldown_at[
                    _recall_cooldown_key(safe_session_id, match.cell.id)
                ] = now_cooldown
            if len(self._recall_cooldown_at) > _SOUL_RECALL_TRACK_MAX:
                self._recall_cooldown_at = {
                    cell_id: seen_at
                    for cell_id, seen_at in self._recall_cooldown_at.items()
                    if now_cooldown - seen_at < cooldown_seconds
                }

        if count_retrieval:
            for match in selected:
                try:
                    await self._repo.touch_memcell_retrieval(
                        str(match.cell.id), _SOUL_RETRIEVAL_BUMP_MIN_INTERVAL_SEC
                    )
                except Exception as exc:
                    log_debug(
                        f"[soul_plugin] Failed to persist retrieval count for {match.cell.id}: {exc}"
                    )

        return [
            self._format_recalled_memory(match, active_session_id=safe_session_id)
            for match in selected
        ]

    def _memcell_distillation_active(self) -> bool:
        """True when the compiler stamps the cells it writes.

        ``distilled_at IS NULL`` means "written before distillation existed" only
        while the active extractor paraphrases what it stores. With the
        rule-based extractor nothing is ever stamped, so the stamp carries no
        information and gating recall on it would empty the memory block.
        """

        try:
            return bool(
                getattr(self._compiler.memcell_extractor, "distils_content", False)
            )
        except Exception:
            return False

    def _redistil_is_waste(self, cell: MemCell) -> bool:
        """True when distilling this cell could never pay off.

        The pass costs one model call per cell, so a cell whose content recall
        will never inject is a call that can never be earned back. Recall already
        refuses two families outright, and this asks the same questions:

        * ``_should_exclude_recalled_memory``: housekeeping sessions (``nightly``,
          ``diary_merge:``) and traces that are a self-initiated routing preamble
          rather than conversation;
        * ``is_roleplay_turn``: in-character fiction and explicit exchanges, which
          are not a stable record of the user or an event.

        Measured on the live store on 2026-09-19: 31 of the 84 unstamped cells
        were roleplay, so 37% of a press would have been spent on memories the
        prompt can never carry. Fail-safe: if the roleplay detector cannot be
        imported the cell is treated as worth distilling, because spending a call
        is better than silently dropping work.
        """

        if self._should_exclude_recalled_memory(cell):
            return True
        try:
            from core.soul.roleplay import is_roleplay_turn

            return bool(is_roleplay_turn(cell.episodic_trace))
        except Exception:
            return False

    @staticmethod
    def _should_exclude_recalled_memory(cell: MemCell) -> bool:
        session_id = str(cell.session_id or "").strip().lower()
        trace = " ".join(str(cell.episodic_trace or "").split()).lower()

        # Housekeeping sessions hold system text, not a conversation (see
        # ``core.soul.service.is_housekeeping_session``); on-demand sessions only
        # matter when the conversation is about their subject.
        if is_housekeeping_session(session_id) or session_id.startswith(
            on_demand_session_prefixes()
        ):
            return True

        # Grillo self-initiated proactive entries store the routing preamble as
        # the episodic trace. That preamble is pure system noise — it contains
        # no conversation memory. Any real content from those sessions is
        # captured by normal diary entries from the same turn.
        # Multiple preamble formats exist across the plugin's history: the
        # legacy outreach plugin (now removed) and the current chat observer.
        if (
            trace.startswith("[self-initiated outreach]")
            or trace.startswith("[g.r.i.l.l.o. outreach]")
            or trace.startswith("[g.r.i.l.l.o. chat observer]")
        ):
            return True

        return (
            "[diary consolidation" in trace
            or "performed update_diary_entry action" in trace
        )

    def _format_recalled_memory(
        self,
        match: MemCellRecall,
        *,
        active_session_id: str,
    ) -> str:
        cell = match.cell
        trace = re.sub(r"\s+", " ", cell.episodic_trace).strip()
        if len(trace) > 220:
            trace = trace[:220].rstrip() + "..."

        trace_key = self._trace_key(cell.episodic_trace)

        # The extractor currently stores the conversation line itself as the
        # cell's only "fact" (``Conversation|summary|<the same text>``), so
        # rendering it prints the trace twice and lengthens every prompt for no
        # added information. Keep only facts that say something the trace does
        # not already say.
        fact_parts: list[str] = []
        for fact in cell.atomic_facts[:2]:
            rendered = self._render_atomic_fact(fact)
            if not rendered:
                continue
            if self._fact_restates_trace(fact, trace_key):
                continue
            fact_parts.append(rendered)
        fact_text = "; ".join(fact_parts)
        if fact_text:
            trace = f"{trace} Key facts: {fact_text}"

        header_parts = [
            "SOUL recalled memory",
            cell.event_timestamp.astimezone(timezone.utc).date().isoformat(),
        ]
        kind_label = {
            "journal": "my diary",
            "diary_day": "my diary, day summary",
            "legacy_memory": "older memory",
        }.get(cell.source_kind)
        if kind_label:
            # An ingested cell belongs to no conversation. Say what it is: a diary
            # line is what the persona itself thought, not something a user said.
            header_parts.append(kind_label)
        elif cell.session_id == active_session_id:
            header_parts.append("same chat")
        else:
            # Recall is not scoped to the active conversation, so say which
            # conversation a memory came from. Without it, lines from another
            # chat (or from a Grillo beat) read as if they belonged to the current
            # one — the cross-chat confusion the prompt's privacy rule warns about.
            session_label = str(cell.session_id or "").strip()
            if session_label:
                header_parts.append(f"other chat: {session_label}")

        memory_emotion = self._normalize_memory_emotion(
            cell.emotional_tag.dominant_emotion
        )
        if memory_emotion and memory_emotion != "neutral":
            header_parts.append(f"emotion={memory_emotion}")

        return f"[{' | '.join(header_parts)}] {trace}".strip()

    def _extract_interface_path(
        self, message: Any, context_memory: dict[str, Any] | None
    ) -> str:
        if message is not None and getattr(message, "interface_path", None):
            return str(message.interface_path)
        if isinstance(context_memory, dict) and context_memory.get("interface_path"):
            return str(context_memory["interface_path"])
        return "unknown/unknown"

    @staticmethod
    def _extract_message_text(message: Any) -> str:
        if message is None:
            return ""
        text = getattr(message, "text", None) or getattr(message, "caption", None)
        return str(text or "").strip()

    @staticmethod
    def _normalize_query_text(text: str) -> str:
        return " ".join((text or "").split())[:400]

    @staticmethod
    def _normalize_session_id(interface_path: str) -> str:
        return re.sub(r"[^a-zA-Z0-9_:\-]", "_", interface_path)

    @classmethod
    def _linked_recall_sessions(cls, session_id: str) -> frozenset[str] | None:
        """Other sessions that share this one's recall scope, or None.

        Read from ``SOUL_RECALL_LINKED_SESSIONS``. The list is honoured only
        when the session being answered is itself in it, so linking a group and
        a DM never widens what an unrelated chat is allowed to recall.
        """
        from core.config_manager import config_registry

        try:
            raw = config_registry.get_value(
                "SOUL_RECALL_LINKED_SESSIONS", "", value_type=str
            )
        except Exception:
            return None
        entries = {
            cls._normalize_session_id(part.strip())
            for part in str(raw or "").replace(";", ",").split(",")
            if part.strip()
        }
        if len(entries) < 2 or session_id not in entries:
            return None
        return frozenset(entries)

    @staticmethod
    def _normalize_memory_emotion(label: str | None) -> str | None:
        normalized = str(label or "").strip().lower()
        if not normalized:
            return None
        emotion_map = {
            "joy": "joy",
            "happy": "joy",
            "love": "joy",
            "fear": "fear",
            "afraid": "fear",
            "anxious": "fear",
            "worried": "fear",
            "sad": "sad",
            "loss": "sad",
            "lonely": "sad",
            "anger": "anger",
            "angry": "anger",
            "frustrated": "anger",
            "frustration": "anger",
            "neutral": "neutral",
        }
        return emotion_map.get(normalized)

    @staticmethod
    def _trace_key(text: Any) -> str:
        """Normalised comparison key for a memory's text."""
        return " ".join(str(text or "").split()).strip().lower()[:160]

    @classmethod
    def _fact_restates_trace(cls, fact: str, trace_key: str) -> bool:
        """True when an atomic fact only repeats the cell's own trace.

        The compiler currently writes the conversation line into the fact list
        verbatim, so this is the common case for every cell produced by it.
        """
        if not trace_key:
            return False
        parts = [part.strip() for part in str(fact or "").split("|") if part.strip()]
        subject = parts[2] if len(parts) == 3 else str(fact or "")
        return trace_key[:120] in " ".join(subject.split()).strip().lower()

    @staticmethod
    def _render_atomic_fact(fact: str) -> str:
        parts = [part.strip() for part in str(fact or "").split("|") if part.strip()]
        if len(parts) == 3:
            predicate = parts[1].replace("_", " ")
            return f"{parts[0]} {predicate} {parts[2]}"
        return str(fact or "").strip()

    def _infer_emotional_event(self, text: str) -> EmotionalEvent:
        lower = text.lower()

        deltas = {
            "social_connection": 0.0,
            "concern_for_user": 0.0,
            "anxiety": 0.0,
            "frustration": 0.0,
            "loneliness": 0.0,
            "achievement": 0.0,
            "loss": 0.0,
            "disappointment": 0.0,
            "pain": 0.0,
            "sensory_pleasure": 0.0,
            "isolation": 0.0,
            "self_preservation": 0.0,
        }

        if any(w in lower for w in ("thank", "love", "glad", "happy", "great")):
            deltas["social_connection"] += 0.6
            deltas["concern_for_user"] += 0.3
            deltas["achievement"] += 0.2

        if any(
            w in lower for w in ("anxious", "worried", "nervous", "afraid", "scared")
        ):
            deltas["anxiety"] += 0.8
            deltas["concern_for_user"] += 0.2

        if any(w in lower for w in ("angry", "mad", "annoyed", "frustrated")):
            deltas["frustration"] += 0.8
            deltas["disappointment"] += 0.4

        if any(w in lower for w in ("lonely", "alone", "miss")):
            deltas["loneliness"] += 0.8
            deltas["loss"] += 0.4

        if any(w in lower for w in ("pain", "hurt", "sick")):
            deltas["pain"] += 0.7
            deltas["concern_for_user"] += 0.2

        if any(w in lower for w in ("music", "beautiful", "aesthetic", "cozy")):
            deltas["sensory_pleasure"] += 0.6

        intensity = min(1.0, max(0.1, sum(abs(v) for v in deltas.values()) / 4.0))
        return EmotionalEvent(
            source="user_message",
            factor_deltas=deltas,
            intensity=intensity,
            context=text[:120],
        )

    # ------------------------------------------------------------------
    # Memory re-distillation (the WebUI "Re-distil memories" button)
    # ------------------------------------------------------------------
    async def redistil_status(self) -> dict[str, Any]:
        """Return the state of the memory re-distillation pass.

        ``pending`` is the live count of cells that predate the distilling
        extractor, so the panel can say how much work is left before anyone
        presses the button. A finished run keeps its counters until the next run
        starts, so the last result stays readable.
        """
        running = self._redistil_task is not None and not self._redistil_task.done()
        state: dict[str, Any] = {
            "running": running,
            "limit": int(self._redistil_state.get("limit") or _soul_redistil_limit()),
            "total": int(self._redistil_state.get("total") or 0),
            "inspected": int(self._redistil_state.get("inspected") or 0),
            "rewritten": int(self._redistil_state.get("rewritten") or 0),
            "skipped": int(self._redistil_state.get("skipped") or 0),
            "skipped_unusable": int(self._redistil_state.get("skipped_unusable") or 0),
            "failed": int(self._redistil_state.get("failed") or 0),
            "timed_out": int(self._redistil_state.get("timed_out") or 0),
            "cell_timeout": _soul_redistil_timeout(),
            "started_at": self._redistil_state.get("started_at"),
            "finished_at": self._redistil_state.get("finished_at"),
            "error": self._redistil_state.get("error"),
            "distilling_extractor": bool(
                getattr(self._compiler.memcell_extractor, "distils_content", False)
            ),
        }
        try:
            state["pending"] = await self._repo.count_memcells_needing_distillation()
        except Exception as exc:
            state["pending"] = None
            state["error"] = state.get("error") or f"count failed: {exc}"
        state["workable"] = await self._redistil_workable(state.get("limit"))
        return state

    async def _redistil_workable(self, limit: int | None) -> int | None:
        """How many model calls one press would actually spend right now.

        ``pending`` is what the store says is unstamped; this is what the pass
        would hand to the extractor after its free skips, which is the number the
        operator is really paying for. Measured the same way the pass measures it
        (same filter, same candidate window), so the panel cannot promise one
        figure and then spend another.
        """

        now = time.monotonic()
        cached = self._redistil_workable_cache
        if cached is not None and now - cached[0] < _REDISTIL_WORKABLE_CACHE_SEC:
            return cached[1]

        batch_limit = int(limit or _soul_redistil_limit())
        batch_limit = max(1, min(batch_limit, _SOUL_REDISTIL_HARD_CAP))
        try:
            candidates = await self._repo.list_memcells_needing_distillation(
                limit=redistil_candidate_limit(batch_limit)
            )
        except Exception as exc:
            log_debug(f"[soul_plugin] re-distil cost preview failed: {exc}")
            return None

        workable = 0
        for cell in candidates:
            if workable >= batch_limit:
                break
            if not self._redistil_is_waste(cell):
                workable += 1
        self._redistil_workable_cache = (now, workable)
        return workable

    async def start_redistil(self, *, limit: int | None = None) -> dict[str, Any]:
        """Start the re-distil pass in the background and return immediately.

        One model call per legacy cell means a full pass runs for minutes to
        hours, far longer than a WebUI request, so the request only starts the
        task: the caller polls :meth:`redistil_status` for progress. A press while
        a pass is running is refused rather than queued, so two passes can never
        work over the same rows at once, and the pass itself is idempotent (a cell
        is stamped when it is rewritten, and only unstamped cells are offered).
        """
        if self._redistil_task is not None and not self._redistil_task.done():
            return {
                "started": False,
                "reason": "already_running",
                **await self.redistil_status(),
            }

        batch_limit = _soul_redistil_limit() if limit is None else int(limit)
        batch_limit = max(1, min(batch_limit, _SOUL_REDISTIL_HARD_CAP))
        self._redistil_workable_cache = None
        self._redistil_state = {
            "limit": batch_limit,
            "total": 0,
            "inspected": 0,
            "rewritten": 0,
            "skipped": 0,
            "skipped_unusable": 0,
            "failed": 0,
            "timed_out": 0,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "error": None,
        }
        log_info(f"[soul_plugin] re-distil pass started (limit={batch_limit})")
        self._redistil_task = asyncio.create_task(self._redistil_worker(batch_limit))
        return {"started": True, "reason": None, **await self.redistil_status()}

    async def _redistil_worker(self, limit: int) -> None:
        try:
            result = await self._compiler.redistil_pending(
                current_date=date.today(),
                limit=limit,
                on_progress=self._redistil_progress,
                skip=self._redistil_is_waste,
                cell_timeout=_soul_redistil_timeout(),
            )
            self._redistil_state.update(result)
        except Exception as exc:
            self._redistil_state["error"] = str(exc)
            log_error(f"[soul_plugin] re-distil pass failed: {exc}")
        finally:
            # The pending set just changed, so the cost preview must be measured
            # again rather than served from the cache.
            self._redistil_workable_cache = None
            self._redistil_state["finished_at"] = datetime.now(timezone.utc).isoformat()
            log_info(
                "[soul_plugin] re-distil pass finished: "
                f"inspected={self._redistil_state.get('inspected')} "
                f"rewritten={self._redistil_state.get('rewritten')} "
                f"skipped={self._redistil_state.get('skipped')} "
                f"skipped_unusable={self._redistil_state.get('skipped_unusable')} "
                f"failed={self._redistil_state.get('failed')} "
                f"timed_out={self._redistil_state.get('timed_out')}"
                + (
                    f" error={self._redistil_state['error']}"
                    if self._redistil_state.get("error")
                    else ""
                )
            )

    def _redistil_progress(self, progress: dict[str, int]) -> None:
        """Keep the counters the WebUI polls up to date between log lines."""
        for key in (
            "total",
            "inspected",
            "rewritten",
            "skipped",
            "skipped_unusable",
            "failed",
            "timed_out",
        ):
            if key in progress:
                self._redistil_state[key] = int(progress[key])

    def get_repository(self) -> SoulRepository:
        """Return the SOUL store for other plugins that feed or read it.

        The situational-notes debrief plugin extracts notes but does not own the
        store; this is the sanctioned way in, so it never has to reach for a
        private attribute.
        """
        return self._repo

    @staticmethod
    def _emotions_enabled() -> bool:
        """False when the Emotion Manager plugin has been switched off."""
        try:
            from core.config_manager import config_registry

            return bool(
                config_registry.get_value(
                    "PLUGIN_ENABLED__emotion_manager", True, value_type=bool
                )
            )
        except Exception:
            return True

    def session_id_for(self, interface_path: str) -> str:
        """The session id SOUL files an interface path under (what cells and private
        notes carry as ``session_id``)."""
        return self._normalize_session_id(interface_path)

    def mark_conversation_private(self, interface_path: str) -> None:
        """Mark what is being said in this chat as shared in confidence.

        Called by the Debrief when it judges the exchange confidential. The cells
        compiled from the current buffer window become ``private`` (recalled only in
        this chat), which covers interfaces that cannot say whether a chat is one to
        one. Conservative on purpose: the whole window is marked, not one line.
        """
        session = self._sessions.get(interface_path)
        if session is None:
            session = _SessionState()
            self._sessions[interface_path] = session
        session.force_private = True

    def get_service(self) -> Any:
        """The :class:`core.soul.service.SoulService` over this plugin's store.

        What other components use to file text for later recall or to rank stored
        cells; prefer it to ``get_repository`` for anything but notes/retractions.
        """
        from core.soul.service import SoulService

        return SoulService(self._repo, self._compiler.embedder)

    def _build_repository(self) -> SoulRepository:
        backend = self._get_repository_backend()
        if backend == "postgres":
            dsn = self._get_postgres_dsn().strip()
            if dsn:
                return PostgresSoulRepository(dsn=dsn)
            log_warning(
                "[soul_plugin] Runtime Postgres DSN is empty; falling back to memory"
            )
        return InMemorySoulRepository()

    @staticmethod
    def _get_compile_idle_seconds() -> int:
        try:
            from core.config_manager import config_registry

            return int(
                config_registry.get_value(
                    "SOUL_COMPILE_IDLE_SECONDS", 300, value_type=int
                )
                or 300
            )
        except Exception:
            return 300

    @staticmethod
    def _get_scheduler_interval() -> int:
        try:
            from core.config_manager import config_registry

            return int(
                config_registry.get_value(
                    "SOUL_SCHEDULER_INTERVAL_SECONDS", 60, value_type=int
                )
                or 60
            )
        except Exception:
            return 60

    @staticmethod
    def _get_repository_backend() -> str:
        try:
            from core.db import _get_db_type

            return "postgres" if _get_db_type() == "postgres" else "memory"
        except Exception:
            return "memory"

    @staticmethod
    def _get_postgres_dsn() -> str:
        try:
            from core.db import build_runtime_postgres_dsn

            return build_runtime_postgres_dsn()
        except Exception:
            try:
                from core.db import build_runtime_postgres_dsn

                return build_runtime_postgres_dsn()
            except Exception:
                return ""


PLUGIN_CLASS = SoulPlugin
