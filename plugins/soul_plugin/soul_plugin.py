from __future__ import annotations

import asyncio
import json
import re
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
from core.soul.strategies import RuleBasedDspExtractor, RuleBasedMemCellExtractor

register_exposed_var(
    "SOUL_PLUGIN_ENABLED",
    label="SOUL Plugin Enabled",
    default=1,
    value_type=int,
    ui_type="bool",
    description="Enable runtime SOUL memory/emotion orchestration plugin.",
    scope="plugins",
    component="soul_plugin",
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
            memcell_extractor=RuleBasedMemCellExtractor(),
            dsp_extractor=RuleBasedDspExtractor(),
            dsp_builder=RuleBasedDspBuilder(),
            summary_builder=RuleBasedSummaryBuilder(),
            embedder=self._build_embedder(),
        )
        self._buffers: dict[str, list[str]] = {}
        self._sessions: dict[str, _SessionState] = {}
        self._scheduler_task: asyncio.Task[None] | None = None
        self._last_rollup_date: date | None = None
        self._last_consolidated_at: datetime | None = None

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
        if not self._is_enabled():
            log_info("[soul_plugin] Disabled by config")
            return
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

    def is_enabled(self) -> bool:
        return self._is_enabled()

    async def execute_action(
        self,
        action: dict[str, Any],
        context: dict[str, Any],
        bot: Any,
        original_message: Any,
    ) -> Any:
        action_type = action.get("type")
        payload = action.get("payload") or {}

        if action_type == "static_inject":
            return await self.get_static_injection(original_message, context)
        return None

    async def get_static_injection(
        self, message: Any = None, context_memory: dict[str, Any] | None = None
    ) -> dict[str, object]:
        if not self._is_enabled():
            return {}

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
            self._append_buffer(interface_path, incoming_text)
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
        }

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

        transcript = "\n".join(lines)
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
        transcript = await self._build_daily_transcript()
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
            "enabled": self._is_enabled(),
            "tracked_sessions": len(self._sessions),
            "buffered_sessions": sum(1 for v in self._buffers.values() if v),
            "active_dsp": bool(dsp),
            "foresight_active": len(
                await self._repo.list_active_foresight_signals(
                    datetime.now(timezone.utc).date()
                )
            ),
        }

    async def _build_daily_transcript(self) -> str:
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(days=1)
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT sender_name, sender_id, message_text, created_at
                        FROM chat_history_cache
                        WHERE created_at >= %s
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
                    return "\n".join(parts)
        except Exception as exc:
            log_debug(f"[soul_plugin] Falling back to buffered transcript: {exc}")

        parts: list[str] = []
        for lines in self._buffers.values():
            parts.extend(lines)
        return "\n".join(parts)

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
        selected = reranked[:_SOUL_RECALL_LIMIT]

        for match in selected:
            try:
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

        fact_text = "; ".join(
            self._render_atomic_fact(fact)
            for fact in cell.atomic_facts[:2]
            if self._render_atomic_fact(fact)
        )
        if fact_text:
            trace = f"{trace} Key facts: {fact_text}"

        header_parts = [
            "SOUL recalled memory",
            cell.event_timestamp.astimezone(timezone.utc).date().isoformat(),
        ]
        if cell.session_id == active_session_id:
            header_parts.append("same chat")

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
    def _is_enabled() -> bool:
        try:
            from core.config_manager import config_registry

            return bool(
                config_registry.get_value("SOUL_PLUGIN_ENABLED", 1, value_type=int)
            )
        except Exception:
            return True

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
