from __future__ import annotations

import asyncio
import json
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


@dataclass(slots=True)
class _SessionState:
    emotional_state: EmotionalState = field(default_factory=EmotionalState)
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_decay_applied: datetime = field(
        default_factory=lambda: datetime.now(timezone.utc)
    )


_SOUL_RECALL_LIMIT = 5
_SOUL_RECALL_CANDIDATE_LIMIT = 24
_SOUL_CONSOLIDATE_COOLDOWN_SECONDS = 900
# A cell's retrieval count is evidence of usefulness, so it must not be inflated
# by the several prompt builds a single turn performs (recon, main reply,
# situational extractor, Grillo beats): live cells reached counts of 70 and 164
# within hours. One bump per cell per window is enough signal.
_SOUL_RETRIEVAL_BUMP_MIN_INTERVAL_SEC = 3600.0
_SOUL_RETRIEVAL_BUMP_TRACK_MAX = 512

# How many active situational notes may be rendered into ONE prompt. The debrief
# writes a fresh note every time it re-describes a circumstance, so the active set
# grows without bound (64 active notes on 2026-09-18, 28 of them in a single
# prompt, several contradicting each other about the same evening). What the model
# is shown is ranked and bounded here; the store keeps everything.
_SOUL_TEMPORAL_INJECT_LIMIT = 8


class SoulPlugin(PluginBase):
    """Runtime integration plugin for SOUL architecture.

    This plugin keeps implementation isolated from high-risk core prompt code by
    injecting context through existing static injection plumbing.
    """

    display_name = "SOUL Plugin"
    allow_static_injection_stale_fallback = True
    static_injection_cache_ttl_seconds = 300.0

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
        self._sessions: dict[str, _SessionState] = {}
        self._retrieval_bump_at: dict[str, float] = {}
        self._scheduler_task: asyncio.Task[None] | None = None
        self._last_rollup_date: date | None = None
        self._last_consolidated_at: datetime | None = None

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

            return LlmDspExtractor()
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

            return LlmMemCellExtractor()
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
        """
        if not SoulPlugin._is_dsp_llm_enabled():
            return RuleBasedDspBuilder()
        try:
            from core.soul.llm_strategies import LlmDspBuilder

            return LlmDspBuilder()
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
            model_id = "BAAI/bge-base-en-v1.5"
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
        asyncio.create_task(self._run_curator_background())
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
            event = self._infer_emotional_event(incoming_text)
            session.emotional_state = self._emotion_engine.apply_event(
                session.emotional_state, event
            )

        session.last_seen = now

        active_dsp = await self._repo.get_active_dsp()
        foresight = await self._repo.list_active_foresight_signals(now.date())
        turn_delta = self._emotion_engine.to_turn_delta_payload(session.emotional_state)
        recalled_memories: list[str] = []

        if incoming_text:
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

        session_state = (
            "<session_state>\n"
            f"interface_path: {interface_path}\n"
            f"active_foresight:\n{foresight_text}\n"
            f"emotion_snapshot: {json.dumps(turn_delta['e'])}\n"
            "</session_state>"
        )

        return {
            "soul_user_profile": active_dsp.content
            if active_dsp
            else "<user_profile>No profile compiled yet.</user_profile>",
            "soul_session_state": session_state,
            "soul_turn_emotion_delta": json.dumps(turn_delta),
            "soul_active_foresight": [
                {
                    "content": signal.content,
                    "valid_until": signal.valid_until.isoformat(),
                    "trigger": signal.trigger,
                }
                for signal in foresight[:8]
            ],
            "soul_recalled_memories": recalled_memories,
            "soul_temporal_context": await self._get_temporal_context(now),
        }

    async def _get_temporal_context(self, now: datetime) -> list[dict[str, Any]]:
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

    async def _tick_scheduler(self) -> None:
        now = datetime.now(timezone.utc)

        idle_cutoff_seconds = self._get_compile_idle_seconds()
        for interface_path, session in list(self._sessions.items()):
            idle_seconds = (now - session.last_seen).total_seconds()
            if idle_seconds >= idle_cutoff_seconds and self._buffers.get(
                interface_path
            ):
                await self._compile_interface(interface_path)

        backfilled = await self._compiler.backfill_embeddings(limit=50)
        if backfilled > 0:
            log_info(
                f"[soul_plugin] Backfilled {backfilled} missing memcell embeddings"
            )

        today = now.date()
        if self._last_rollup_date != today:
            await self._run_rollup_now()
            self._last_rollup_date = today

    async def _compile_interface(
        self,
        interface_path: str,
        *,
        force_consolidate: bool = False,
    ) -> int:
        lines = self._buffers.get(interface_path, [])
        if not lines:
            return 0

        # Roleplay/explicit turns stay in the buffer (they still drive emotion
        # and memory recall) but are excluded from what gets compiled into
        # memcells — in-character fiction is not a durable event record.
        from core.soul.roleplay import strip_roleplay_lines

        transcript = strip_roleplay_lines("\n".join(lines))
        if not transcript.strip():
            self._buffers[interface_path] = []
            return 0

        safe_session_id = self._normalize_session_id(interface_path)

        created = await self._compiler.post_session_compile(
            current_date=datetime.now(timezone.utc).date(),
            transcript=transcript,
            session_id=safe_session_id,
        )
        consolidated = await self._maybe_consolidate(force=force_consolidate)

        self._buffers[interface_path] = []
        log_info(
            f"[soul_plugin] Compiled {len(created)} memcells for {interface_path} "
            f"(consolidated {consolidated} scene(s))"
        )
        return len(created)

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

    async def _run_curator_now(self, *, max_memories: int = 500) -> dict[str, int]:
        result = await self._compiler.run_curator(
            current_date=datetime.now(timezone.utc).date(),
            max_memories=max_memories,
        )
        log_info(
            f"[soul_plugin] Memory Curator: inspected={result.inspected} "
            f"removed={result.removed} retained={result.retained} "
            f"(future={result.kept_future} important={result.kept_important})"
        )
        return {
            "inspected": result.inspected,
            "removed": result.removed,
            "retained": result.retained,
            "kept_future": result.kept_future,
            "kept_important": result.kept_important,
        }

    async def _run_curator_background(self) -> None:
        try:
            await self._run_curator_now()
        except Exception as exc:
            log_warning(f"[soul_plugin] Background curator run failed: {exc}")

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
                        ORDER BY created_at ASC
                        LIMIT 500
                        """,
                        (cutoff,),
                    )
                    rows = await cur.fetchall()
                    parts: list[str] = []
                    for row in rows:
                        if not row or not row[2]:
                            continue
                        speaker = str(row[0] or row[1] or "user").strip() or "user"
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
    def _labelled_buffer_line(cls, message: Any, text: str) -> str:
        """Prefix a buffered line with who said it.

        The rule-based extractor stores the transcript verbatim as the memory
        cell's episodic trace, so this label is what later tells the synth whose
        words a recalled memory actually holds.
        """
        return f"{cls._buffer_speaker(message)}: {text.strip()}"

    def _append_buffer(self, interface_path: str, text: str) -> None:
        self._buffers.setdefault(interface_path, []).append(text.strip())
        # Keep bounded memory per interface.
        if len(self._buffers[interface_path]) > 200:
            self._buffers[interface_path] = self._buffers[interface_path][-200:]

    async def _recall_memories(
        self,
        *,
        interface_path: str,
        incoming_text: str,
        session: _SessionState,
    ) -> list[str]:
        normalized_query = self._normalize_query_text(incoming_text)
        if len(normalized_query) < 5:
            return []

        query_embedding = await self._compiler.embedder.embed(normalized_query)
        safe_session_id = self._normalize_session_id(interface_path)
        candidates = await self._repo.recall_memories(
            query_text=normalized_query,
            query_embedding=query_embedding,
            session_id=safe_session_id,
            limit=_SOUL_RECALL_LIMIT,
            candidate_limit=_SOUL_RECALL_CANDIDATE_LIMIT,
        )
        if not candidates:
            return []

        reranked: list[MemCellRecall] = []
        seen_ids: set[str] = set()
        for match in candidates:
            cell = match.cell
            if cell.id in seen_ids:
                continue
            if self._should_exclude_recalled_memory(cell):
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
            if memory_emotion is not None:
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
        for match in reranked:
            # Two cells can hold the same line (a turn compiled twice, a scene
            # folded back in). Injecting it twice wastes prompt space and is what
            # makes a memory block read as repetitive.
            key = self._trace_key(match.cell.episodic_trace)
            if key and key in seen_traces:
                continue
            seen_traces.add(key)
            selected.append(match)
            if len(selected) >= _SOUL_RECALL_LIMIT:
                break

        now_monotonic = time.monotonic()
        for match in selected:
            try:
                cell_id = str(match.cell.id)
                last_bump = self._retrieval_bump_at.get(cell_id, 0.0)
                if now_monotonic - last_bump < _SOUL_RETRIEVAL_BUMP_MIN_INTERVAL_SEC:
                    # Already counted recently: a turn builds several prompts, so
                    # an unthrottled bump counts one decision many times.
                    continue
                self._retrieval_bump_at[cell_id] = now_monotonic
                if len(self._retrieval_bump_at) > _SOUL_RETRIEVAL_BUMP_TRACK_MAX:
                    self._retrieval_bump_at = {
                        cid: ts
                        for cid, ts in self._retrieval_bump_at.items()
                        if now_monotonic - ts < _SOUL_RETRIEVAL_BUMP_MIN_INTERVAL_SEC
                    }
                match.cell.retrieval_count += 1
                await self._repo.upsert_memcell(match.cell)
            except Exception as exc:
                log_debug(
                    f"[soul_plugin] Failed to persist retrieval count for {match.cell.id}: {exc}"
                )

        return [
            self._format_recalled_memory(match, active_session_id=safe_session_id)
            for match in selected
        ]

    @staticmethod
    def _should_exclude_recalled_memory(cell: MemCell) -> bool:
        session_id = str(cell.session_id or "").strip().lower()
        trace = " ".join(str(cell.episodic_trace or "").split()).lower()

        if session_id == "nightly" or session_id.startswith("diary_merge:"):
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
        if cell.session_id == active_session_id:
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

    def get_repository(self) -> SoulRepository:
        """Return the SOUL store for other plugins that feed or read it.

        The situational-notes debrief plugin extracts notes but does not own the
        store; this is the sanctioned way in, so it never has to reach for a
        private attribute.
        """
        return self._repo

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
