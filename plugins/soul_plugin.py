from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
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
from core.soul.emotion_engine import EmotionalEngine
from core.soul.models import EmotionalEvent, EmotionalState
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
    description="SOUL persistence backend: memory or postgres.",
    scope="plugins",
    component="soul_plugin",
)

register_exposed_var(
    "SOUL_POSTGRES_DSN",
    label="SOUL Postgres DSN",
    default="",
    value_type=str,
    ui_type="text",
    description="PostgreSQL DSN used when SOUL_REPOSITORY_BACKEND=postgres.",
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


class SoulPlugin(PluginBase):
    """Runtime integration plugin for SOUL architecture.

    This plugin keeps implementation isolated from high-risk core prompt code by
    injecting context through existing static injection plumbing.
    """

    display_name = "SOUL Plugin"

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__(config)
        self._repo = self._build_repository()
        self._emotion_engine = EmotionalEngine()
        self._compiler = SoulCompiler(
            repository=self._repo,
            memcell_extractor=RuleBasedMemCellExtractor(),
            dsp_extractor=RuleBasedDspExtractor(),
            dsp_builder=RuleBasedDspBuilder(),
            summary_builder=RuleBasedSummaryBuilder(),
            embedder=NoopEmbedder(),
        )
        self._buffers: dict[str, list[str]] = {}
        self._sessions: dict[str, _SessionState] = {}
        self._scheduler_task: asyncio.Task[None] | None = None
        self._last_rollup_date: date | None = None

    async def start(self) -> None:
        if not self._is_enabled():
            log_info("[soul_plugin] Disabled by config")
            return
        if self._scheduler_task and not self._scheduler_task.done():
            return
        self._scheduler_task = asyncio.create_task(self._scheduler_loop())
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
            "soul_force_compile": {
                "description": "Force compile pending SOUL transcript buffer",
                "required_params": {},
                "optional_params": {"interface_path": "Optional interface path"},
            },
            "soul_force_rollup": {
                "description": "Force SOUL nightly rollup now",
                "required_params": {},
                "optional_params": {},
            },
            "soul_get_status": {
                "description": "Return SOUL plugin runtime status",
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
        payload = action.get("payload") or {}

        if action_type == "static_inject":
            return await self.get_static_injection(original_message, context)
        if action_type == "soul_force_compile":
            interface_path = str(payload.get("interface_path") or "")
            return await self._force_compile(interface_path=interface_path or None)
        if action_type == "soul_force_rollup":
            return await self._run_rollup_now()
        if action_type == "soul_get_status":
            return await self._get_status()
        return None

    async def get_static_injection(
        self, message: Any = None, context_memory: dict[str, Any] | None = None
    ) -> dict[str, object]:
        if not self._is_enabled():
            return {}

        interface_path = self._extract_interface_path(message, context_memory)
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

        today = now.date()
        if self._last_rollup_date != today:
            await self._run_rollup_now()
            self._last_rollup_date = today

    async def _compile_interface(self, interface_path: str) -> int:
        lines = self._buffers.get(interface_path, [])
        if not lines:
            return 0

        transcript = "\n".join(lines)
        safe_session_id = re.sub(r"[^a-zA-Z0-9_:\-]", "_", interface_path)

        created = await self._compiler.post_session_compile(
            current_date=datetime.now(timezone.utc).date(),
            transcript=transcript,
            session_id=safe_session_id,
        )
        await self._compiler.async_consolidate()

        self._buffers[interface_path] = []
        log_info(f"[soul_plugin] Compiled {len(created)} memcells for {interface_path}")
        return len(created)

    async def _force_compile(self, interface_path: str | None = None) -> dict[str, int]:
        if interface_path:
            count = await self._compile_interface(interface_path)
            return {"compiled_memcells": count}

        total = 0
        for key in list(self._buffers.keys()):
            total += await self._compile_interface(key)
        return {"compiled_memcells": total}

    async def _run_rollup_now(self) -> dict[str, int]:
        transcript = await self._build_daily_transcript()
        result = await self._compiler.nightly_rollup(
            current_date=datetime.now(timezone.utc).date(),
            transcript=transcript,
            session_id="nightly",
        )
        log_info(f"[soul_plugin] Nightly rollup result: {result}")
        return result

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
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        SELECT message_text
                        FROM chat_history_cache
                        WHERE timestamp >= (UTC_TIMESTAMP() - INTERVAL 1 DAY)
                        ORDER BY timestamp ASC
                        LIMIT 500
                        """
                    )
                    rows = await cur.fetchall()
                    parts = [str(r[0]) for r in rows if r and r[0]]
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
                "[soul_plugin] SOUL_REPOSITORY_BACKEND=postgres but SOUL_POSTGRES_DSN is empty; falling back to memory"
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
            from core.config_manager import config_registry

            value = str(
                config_registry.get_value(
                    "SOUL_REPOSITORY_BACKEND", "memory", value_type=str
                )
                or "memory"
            )
            value = value.strip().lower()
            if value in {"memory", "postgres"}:
                return value
            return "memory"
        except Exception:
            return "memory"

    @staticmethod
    def _get_postgres_dsn() -> str:
        try:
            from core.config_manager import config_registry

            return str(
                config_registry.get_value("SOUL_POSTGRES_DSN", "", value_type=str) or ""
            )
        except Exception:
            return ""


PLUGIN_CLASS = SoulPlugin
