from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol, cast

from .models import (
    DspExtraction,
    DspVersion,
    EmotionalTag,
    ForesightSignal,
    KgTriple,
    MemCell,
    MemScene,
)


class SoulRepository(Protocol):
    """Persistence contract for the soul subsystem."""

    async def upsert_memcell(self, cell: MemCell) -> None: ...

    async def list_unconsolidated_memcells(self, limit: int = 200) -> list[MemCell]: ...

    async def set_memcell_scene(self, cell_id: str, scene_id: str) -> None: ...

    async def upsert_scene(self, scene: MemScene) -> None: ...

    async def upsert_kg_triple(self, triple: KgTriple) -> None: ...

    async def list_active_foresight_signals(
        self, today: date
    ) -> list[ForesightSignal]: ...

    async def upsert_foresight_signal(self, signal: ForesightSignal) -> None: ...

    async def archive_expired_foresight_signals(self, today: date) -> int: ...

    async def add_dsp_extraction(self, extraction: DspExtraction) -> None: ...

    async def list_recent_dsp_extractions(self, limit: int) -> list[DspExtraction]: ...

    async def get_active_dsp(self) -> DspVersion | None: ...

    async def set_active_dsp(self, dsp: DspVersion) -> None: ...


@dataclass(slots=True)
class InMemorySoulRepository:
    """Simple in-memory repository for tests and local dry runs."""

    memcells: dict[str, MemCell] = field(default_factory=dict)
    scenes: dict[str, MemScene] = field(default_factory=dict)
    kg_triples: list[KgTriple] = field(default_factory=list)
    foresight_signals: list[ForesightSignal] = field(default_factory=list)
    dsp_extractions: list[DspExtraction] = field(default_factory=list)
    active_dsp: DspVersion | None = None

    async def upsert_memcell(self, cell: MemCell) -> None:
        self.memcells[cell.id] = cell

    async def list_unconsolidated_memcells(self, limit: int = 200) -> list[MemCell]:
        cells = [c for c in self.memcells.values() if not c.consolidated]
        cells.sort(key=lambda c: c.timestamp)
        return cells[:limit]

    async def set_memcell_scene(self, cell_id: str, scene_id: str) -> None:
        cell = self.memcells[cell_id]
        cell.scene_id = scene_id
        cell.consolidated = True
        self.memcells[cell_id] = cell

    async def upsert_scene(self, scene: MemScene) -> None:
        self.scenes[scene.id] = scene

    async def upsert_kg_triple(self, triple: KgTriple) -> None:
        self.kg_triples.append(triple)

    async def list_active_foresight_signals(self, today: date) -> list[ForesightSignal]:
        return [s for s in self.foresight_signals if s.valid_until >= today]

    async def upsert_foresight_signal(self, signal: ForesightSignal) -> None:
        for index, existing in enumerate(self.foresight_signals):
            if (
                existing.content == signal.content
                and existing.trigger == signal.trigger
                and existing.valid_until == signal.valid_until
            ):
                self.foresight_signals[index] = signal
                return
        self.foresight_signals.append(signal)

    async def archive_expired_foresight_signals(self, today: date) -> int:
        before = len(self.foresight_signals)
        self.foresight_signals = [
            s for s in self.foresight_signals if s.valid_until >= today
        ]
        return before - len(self.foresight_signals)

    async def add_dsp_extraction(self, extraction: DspExtraction) -> None:
        self.dsp_extractions.append(extraction)

    async def list_recent_dsp_extractions(self, limit: int) -> list[DspExtraction]:
        items = sorted(self.dsp_extractions, key=lambda x: x.extracted_at, reverse=True)
        return items[:limit]

    async def get_active_dsp(self) -> DspVersion | None:
        return self.active_dsp

    async def set_active_dsp(self, dsp: DspVersion) -> None:
        if self.active_dsp is not None:
            self.active_dsp.archived_at = dsp.created_at
        self.active_dsp = dsp


@dataclass(slots=True)
class PostgresSoulRepository:
    """PostgreSQL-backed SOUL repository.

    Uses lazy asyncpg initialization so environments without PostgreSQL support
    can still import this module without hard runtime failures.
    """

    dsn: str
    schema: str = "public"
    min_pool_size: int = 1
    max_pool_size: int = 5
    _pool: Any | None = field(default=None, init=False, repr=False)

    async def _get_pool(self) -> Any:
        if self._pool is not None:
            return self._pool

        from importlib import import_module

        asyncpg = import_module("asyncpg")
        self._pool = await asyncpg.create_pool(
            dsn=self.dsn,
            min_size=self.min_pool_size,
            max_size=self.max_pool_size,
            server_settings={"search_path": self.schema},
        )
        return self._pool

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def upsert_memcell(self, cell: MemCell) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO mem_cells (
                    id, session_id, episodic_trace, atomic_facts, emotional_tag,
                    foresight_signals, timestamp, retrieval_count, explicit_importance,
                    consolidated, scene_id, updated_at
                )
                VALUES (
                    $1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb, $7,
                    $8, $9, $10, $11, NOW()
                )
                ON CONFLICT (id)
                DO UPDATE SET
                    session_id = EXCLUDED.session_id,
                    episodic_trace = EXCLUDED.episodic_trace,
                    atomic_facts = EXCLUDED.atomic_facts,
                    emotional_tag = EXCLUDED.emotional_tag,
                    foresight_signals = EXCLUDED.foresight_signals,
                    timestamp = EXCLUDED.timestamp,
                    retrieval_count = EXCLUDED.retrieval_count,
                    explicit_importance = EXCLUDED.explicit_importance,
                    consolidated = EXCLUDED.consolidated,
                    scene_id = EXCLUDED.scene_id,
                    updated_at = NOW()
                """,
                cell.id,
                cell.session_id,
                cell.episodic_trace,
                json.dumps(cell.atomic_facts),
                json.dumps(
                    {
                        "state_snapshot": cell.emotional_tag.state_snapshot,
                        "dominant_emotion": cell.emotional_tag.dominant_emotion,
                        "intensity": cell.emotional_tag.intensity,
                        "valence": cell.emotional_tag.valence,
                    }
                ),
                json.dumps(
                    [
                        {
                            "content": s.content,
                            "valid_until": s.valid_until.isoformat(),
                            "trigger": s.trigger,
                            "emotional_implication": s.emotional_implication,
                            "source_cell_id": s.source_cell_id,
                            "priority": s.priority,
                        }
                        for s in cell.foresight_signals
                    ]
                ),
                cell.timestamp,
                cell.retrieval_count,
                cell.explicit_importance,
                cell.consolidated,
                cell.scene_id,
            )

            if cell.embedding:
                vector_literal = self._vector_literal(cell.embedding)
                await conn.execute(
                    """
                    INSERT INTO mem_cell_vectors (mem_cell_id, embedding)
                    VALUES ($1, $2::vector)
                    ON CONFLICT (mem_cell_id)
                    DO UPDATE SET embedding = EXCLUDED.embedding
                    """,
                    cell.id,
                    vector_literal,
                )

    async def list_unconsolidated_memcells(self, limit: int = 200) -> list[MemCell]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT
                    id, session_id, episodic_trace, atomic_facts, emotional_tag,
                    foresight_signals, timestamp, retrieval_count, explicit_importance,
                    consolidated, scene_id
                FROM mem_cells
                WHERE consolidated = FALSE
                ORDER BY timestamp ASC
                LIMIT $1
                """,
                limit,
            )
        return [self._row_to_memcell(row) for row in rows]

    async def set_memcell_scene(self, cell_id: str, scene_id: str) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                UPDATE mem_cells
                SET scene_id = $2, consolidated = TRUE, updated_at = NOW()
                WHERE id = $1
                """,
                cell_id,
                scene_id,
            )

    async def upsert_scene(self, scene: MemScene) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO mem_scenes (id, title, summary, cell_ids, created_at, updated_at)
                VALUES ($1, $2, $3, $4::jsonb, $5, $6)
                ON CONFLICT (id)
                DO UPDATE SET
                    title = EXCLUDED.title,
                    summary = EXCLUDED.summary,
                    cell_ids = EXCLUDED.cell_ids,
                    updated_at = EXCLUDED.updated_at
                """,
                scene.id,
                scene.title,
                scene.summary,
                json.dumps(scene.cell_ids),
                scene.created_at,
                scene.updated_at,
            )

    async def upsert_kg_triple(self, triple: KgTriple) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO kg_triples (
                    subject, predicate, object, valid_from, valid_until, scene_id
                )
                VALUES ($1, $2, $3, $4, $5, $6)
                """,
                triple.subject,
                triple.predicate,
                triple.object,
                triple.valid_from,
                triple.valid_until,
                triple.scene_id,
            )

    async def list_active_foresight_signals(self, today: date) -> list[ForesightSignal]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT content, valid_until, trigger, emotional_implication,
                       source_cell_id, priority
                FROM foresight_signals
                WHERE archived = FALSE AND valid_until >= $1
                ORDER BY valid_until ASC, priority DESC
                """,
                today,
            )
        return [
            ForesightSignal(
                content=str(row["content"]),
                valid_until=cast(date, row["valid_until"]),
                trigger=str(row["trigger"]),
                emotional_implication=self._as_dict(row["emotional_implication"]),
                source_cell_id=cast(str | None, row["source_cell_id"]),
                priority=float(row["priority"]),
            )
            for row in rows
        ]

    async def upsert_foresight_signal(self, signal: ForesightSignal) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            existing = await conn.fetchval(
                """
                SELECT id
                FROM foresight_signals
                WHERE content = $1 AND trigger = $2 AND valid_until = $3
                  AND archived = FALSE
                LIMIT 1
                """,
                signal.content,
                signal.trigger,
                signal.valid_until,
            )
            if existing is not None:
                await conn.execute(
                    """
                    UPDATE foresight_signals
                    SET emotional_implication = $2::jsonb,
                        source_cell_id = $3,
                        priority = $4,
                        updated_at = NOW()
                    WHERE id = $1
                    """,
                    existing,
                    json.dumps(signal.emotional_implication),
                    signal.source_cell_id,
                    signal.priority,
                )
                return

            await conn.execute(
                """
                INSERT INTO foresight_signals (
                    content, valid_until, trigger, emotional_implication,
                    source_cell_id, priority, archived
                )
                VALUES ($1, $2, $3, $4::jsonb, $5, $6, FALSE)
                """,
                signal.content,
                signal.valid_until,
                signal.trigger,
                json.dumps(signal.emotional_implication),
                signal.source_cell_id,
                signal.priority,
            )

    async def archive_expired_foresight_signals(self, today: date) -> int:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            status = await conn.execute(
                """
                UPDATE foresight_signals
                SET archived = TRUE, updated_at = NOW()
                WHERE archived = FALSE AND valid_until < $1
                """,
                today,
            )
        # asyncpg returns command tags like "UPDATE 3".
        parts = str(status).split()
        if len(parts) == 2 and parts[0].upper() == "UPDATE":
            return int(parts[1])
        return 0

    async def add_dsp_extraction(self, extraction: DspExtraction) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO dsp_extractions (
                    id, session_id, extracted_at,
                    user_facts, user_preferences, ai_self_facts
                )
                VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb)
                ON CONFLICT (id)
                DO UPDATE SET
                    session_id = EXCLUDED.session_id,
                    extracted_at = EXCLUDED.extracted_at,
                    user_facts = EXCLUDED.user_facts,
                    user_preferences = EXCLUDED.user_preferences,
                    ai_self_facts = EXCLUDED.ai_self_facts
                """,
                extraction.id,
                extraction.session_id,
                extraction.extracted_at,
                json.dumps(extraction.user_facts),
                json.dumps(extraction.user_preferences),
                json.dumps(extraction.ai_self_facts),
            )

    async def list_recent_dsp_extractions(self, limit: int) -> list[DspExtraction]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """
                SELECT id, session_id, extracted_at,
                       user_facts, user_preferences, ai_self_facts
                FROM dsp_extractions
                ORDER BY extracted_at DESC
                LIMIT $1
                """,
                limit,
            )
        return [
            DspExtraction(
                id=str(row["id"]),
                session_id=str(row["session_id"]),
                extracted_at=row["extracted_at"],
                user_facts=self._as_list(row["user_facts"]),
                user_preferences=self._as_list(row["user_preferences"]),
                ai_self_facts=self._as_list(row["ai_self_facts"]),
            )
            for row in rows
        ]

    async def get_active_dsp(self) -> DspVersion | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT id, content, created_at, archived_at
                FROM dsp_versions
                WHERE active = TRUE
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
        if row is None:
            return None
        return DspVersion(
            id=str(row["id"]),
            content=str(row["content"]),
            created_at=row["created_at"],
            archived_at=row["archived_at"],
        )

    async def set_active_dsp(self, dsp: DspVersion) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    UPDATE dsp_versions
                    SET active = FALSE,
                        archived_at = COALESCE(archived_at, $1)
                    WHERE active = TRUE
                    """,
                    dsp.created_at,
                )
                await conn.execute(
                    """
                    INSERT INTO dsp_versions (id, content, created_at, archived_at, active)
                    VALUES ($1, $2, $3, $4, TRUE)
                    ON CONFLICT (id)
                    DO UPDATE SET
                        content = EXCLUDED.content,
                        created_at = EXCLUDED.created_at,
                        archived_at = EXCLUDED.archived_at,
                        active = TRUE
                    """,
                    dsp.id,
                    dsp.content,
                    dsp.created_at,
                    dsp.archived_at,
                )

    @staticmethod
    def _vector_literal(values: list[float]) -> str:
        return "[" + ",".join(f"{float(v):.10g}" for v in values) + "]"

    @staticmethod
    def _as_list(value: Any) -> list[str]:
        if value is None:
            return []
        if isinstance(value, list):
            return [str(item) for item in value]
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return [str(item) for item in parsed]
            except Exception:
                return []
        return []

    @staticmethod
    def _as_dict(value: Any) -> dict[str, float]:
        if value is None:
            return {}
        if isinstance(value, dict):
            return {str(k): float(v) for k, v in value.items()}
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return {str(k): float(v) for k, v in parsed.items()}
            except Exception:
                return {}
        return {}

    def _row_to_memcell(self, row: Any) -> MemCell:
        tag_raw = self._as_dict_container(row["emotional_tag"])
        foresight_raw = self._as_list_container(row["foresight_signals"])
        signals: list[ForesightSignal] = []
        for item in foresight_raw:
            valid_until_raw = item.get("valid_until")
            if isinstance(valid_until_raw, str):
                valid_until = date.fromisoformat(valid_until_raw)
            else:
                valid_until = cast(date, valid_until_raw)
            signals.append(
                ForesightSignal(
                    content=str(item.get("content", "")),
                    valid_until=valid_until,
                    trigger=str(item.get("trigger", "")),
                    emotional_implication=self._as_dict(
                        item.get("emotional_implication")
                    ),
                    source_cell_id=cast(str | None, item.get("source_cell_id")),
                    priority=float(item.get("priority", 0.5)),
                )
            )

        return MemCell(
            id=str(row["id"]),
            session_id=str(row["session_id"]),
            episodic_trace=str(row["episodic_trace"]),
            atomic_facts=self._as_list(row["atomic_facts"]),
            emotional_tag=EmotionalTag(
                state_snapshot=self._as_dict(tag_raw.get("state_snapshot")),
                dominant_emotion=str(tag_raw.get("dominant_emotion", "neutral")),
                intensity=float(tag_raw.get("intensity", 0.0)),
                valence=float(tag_raw.get("valence", 0.0)),
            ),
            foresight_signals=signals,
            timestamp=row["timestamp"],
            retrieval_count=int(row["retrieval_count"]),
            explicit_importance=float(row["explicit_importance"]),
            consolidated=bool(row["consolidated"]),
            scene_id=cast(str | None, row["scene_id"]),
        )

    @staticmethod
    def _as_dict_container(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                return {}
        return {}

    @staticmethod
    def _as_list_container(value: Any) -> list[dict[str, Any]]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, list):
                    return [item for item in parsed if isinstance(item, dict)]
            except Exception:
                return []
        return []
