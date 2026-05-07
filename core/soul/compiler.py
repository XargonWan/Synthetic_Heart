from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Protocol, cast

from .models import (
    CurationResult,
    CuratorDecision,
    DspExtraction,
    DspVersion,
    EmotionalTag,
    ForesightSignal,
    KgTriple,
    MemCell,
    MemCellSummary,
    MemScene,
    compute_memcell_salience,
    now_utc,
    new_memcell_id,
    new_scene_id,
)
from .observability import maybe_langfuse_trace
from .repository import SoulRepository
from .schemas import DspExtractionModel, MemCellExtractionModel, SummaryResultModel
from .time_resolution import AbsoluteTimeResolver


class MemCellExtractor(Protocol):
    async def extract_memcells(
        self, *, transcript: str, current_date: date
    ) -> list[MemCellExtractionModel]: ...


class DspExtractor(Protocol):
    async def extract_dsp(
        self, *, transcript: str, current_date: date
    ) -> DspExtractionModel: ...


class DspBuilder(Protocol):
    async def build_initial(self, *, extractions: list[DspExtraction]) -> str: ...

    async def build_update(
        self, *, current_dsp: str, extractions: list[DspExtraction]
    ) -> str: ...


class SummaryBuilder(Protocol):
    async def summarize_scene(self, *, cells: list[MemCell]) -> SummaryResultModel: ...


class MemCellCurator(Protocol):
    async def classify(
        self,
        summaries: list[MemCellSummary],
        current_date: date,
    ) -> list[tuple[str, CuratorDecision]]: ...


class Embedder(Protocol):
    async def embed(self, text: str) -> list[float]: ...


class LangfuseTraceLike(Protocol):
    def update(self, **kwargs: Any) -> None: ...


_FUTURE_DATE_RE = re.compile(r"\b(20\d{2}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01]))\b")
_CURATOR_SALIENCE_KEEP_THRESHOLD = 0.4
_CURATOR_HALF_LIFE_SECONDS = 14 * 24 * 3600


class RuleBasedMemCellCurator:
    """Deterministic curator using salience scores and foresight detection.

    KEEP_FUTURE  — has an active foresight signal or mentions a future ISO date.
    KEEP_IMPORTANT — salience >= threshold or explicit_importance > 0.
    REMOVE       — everything else.
    """

    async def classify(
        self,
        summaries: list[MemCellSummary],
        current_date: date,
    ) -> list[tuple[str, CuratorDecision]]:
        now = datetime(
            current_date.year,
            current_date.month,
            current_date.day,
            tzinfo=__import__("datetime").timezone.utc,
        )
        return [(s.id, self._classify_one(s, current_date, now)) for s in summaries]

    @staticmethod
    def _classify_one(
        summary: MemCellSummary,
        current_date: date,
        now: datetime,
    ) -> CuratorDecision:
        if summary.has_active_foresight:
            return CuratorDecision.KEEP_FUTURE

        for m in _FUTURE_DATE_RE.finditer(summary.episodic_trace):
            try:
                if date.fromisoformat(m.group(1)) > current_date:
                    return CuratorDecision.KEEP_FUTURE
            except ValueError:
                pass

        ts = summary.timestamp
        if ts.tzinfo is None:
            from datetime import timezone as _tz

            ts = ts.replace(tzinfo=_tz.utc)
        age_seconds = max(0.0, (now - ts.astimezone(now.tzinfo)).total_seconds())
        recency = 0.5 ** (age_seconds / _CURATOR_HALF_LIFE_SECONDS)

        salience = compute_memcell_salience(
            emotional_intensity=summary.emotional_intensity,
            retrieval_count=summary.retrieval_count,
            recency_score=recency,
            explicit_importance=summary.explicit_importance,
        )
        if (
            salience >= _CURATOR_SALIENCE_KEEP_THRESHOLD
            or summary.explicit_importance > 0
        ):
            return CuratorDecision.KEEP_IMPORTANT

        return CuratorDecision.REMOVE


class NoopEmbedder:
    async def embed(self, text: str) -> list[float]:
        # Deterministic lightweight embedding that matches pgvector(768).
        # This avoids model downloads and runs well on low-resource devices.
        dimensions = 768
        vector: list[float] = [0.0] * dimensions
        normalized_text = text.strip().lower()
        if not normalized_text:
            return vector

        for token in normalized_text.split():
            digest = hashlib.sha256(token.encode("utf-8")).digest()
            index = int.from_bytes(digest[:2], "little") % dimensions
            sign = 1.0 if digest[2] % 2 == 0 else -1.0
            weight = 0.25 + (digest[3] / 255.0) * 0.75
            vector[index] += sign * weight

        norm = sum(value * value for value in vector) ** 0.5
        if norm > 0:
            vector = [value / norm for value in vector]
        return vector


@dataclass(slots=True)
class SoulCompiler:
    repository: SoulRepository
    memcell_extractor: MemCellExtractor
    dsp_extractor: DspExtractor
    dsp_builder: DspBuilder
    summary_builder: SummaryBuilder
    embedder: Embedder
    dsp_bootstrap_min_extractions: int = 5
    dsp_update_batch_size: int = 3
    curator: MemCellCurator | None = None

    async def post_session_compile(
        self,
        *,
        current_date: date,
        transcript: str,
        session_id: str,
    ) -> list[str]:
        """Extract and persist MemCells after a session.

        Relative temporal language is converted to absolute dates before storing.
        """

        resolver = AbsoluteTimeResolver(current_date=current_date)
        created_ids: list[str] = []

        with maybe_langfuse_trace("post_session_compile") as trace:
            extracted = await self.memcell_extractor.extract_memcells(
                transcript=transcript,
                current_date=current_date,
            )

            for raw_cell in extracted:
                episodic_trace = resolver.resolve_text(raw_cell.episodic_trace)
                atomic_facts = [resolver.resolve_text(f) for f in raw_cell.atomic_facts]
                if not atomic_facts:
                    atomic_facts = self._fallback_atomic_facts(episodic_trace)
                embedding = await self.embedder.embed(episodic_trace)

                cell_timestamp = raw_cell.timestamp
                if cell_timestamp.tzinfo is None:
                    cell_timestamp = cell_timestamp.replace(tzinfo=now_utc().tzinfo)

                memcell = MemCell(
                    id=new_memcell_id(session_id, cell_timestamp),
                    episodic_trace=episodic_trace,
                    atomic_facts=atomic_facts,
                    emotional_tag=self._to_emotional_tag(raw_cell),
                    foresight_signals=self._to_foresight_signals(raw_cell),
                    timestamp=cell_timestamp,
                    session_id=session_id,
                    embedding=embedding,
                )
                await self.repository.upsert_memcell(memcell)

                for signal in memcell.foresight_signals:
                    await self.repository.upsert_foresight_signal(signal)

                created_ids.append(memcell.id)

            if trace is not None:
                try:
                    typed_trace = cast(LangfuseTraceLike, trace)
                    typed_trace.update(output={"memcells_created": len(created_ids)})
                except Exception:
                    pass

        return created_ids

    async def backfill_embeddings(self, *, limit: int = 200) -> int:
        """Backfill vector embeddings for existing MemCells missing vectors."""

        updated = 0
        missing_cells = await self.repository.list_memcells_missing_embeddings(
            limit=limit
        )
        for cell in missing_cells:
            text = cell.episodic_trace.strip()
            if not text:
                continue
            cell.embedding = await self.embedder.embed(text)
            await self.repository.upsert_memcell(cell)
            updated += 1
        return updated

    async def async_consolidate(self) -> list[str]:
        """Consolidate unconsolidated MemCells into MemScenes."""

        updated_scene_ids: list[str] = []
        pending_cells = await self.repository.list_unconsolidated_memcells(limit=200)
        if not pending_cells:
            return updated_scene_ids

        # Coarse clustering by first atomic fact fallbacking to episodic trace prefix.
        clusters: dict[str, list[MemCell]] = {}
        for cell in pending_cells:
            if cell.atomic_facts:
                key = cell.atomic_facts[0].strip().lower()
            else:
                key = cell.episodic_trace[:80].strip().lower()
            clusters.setdefault(key, []).append(cell)

        with maybe_langfuse_trace("async_consolidate"):
            for cells in clusters.values():
                anchor = min(c.timestamp for c in cells)
                scene_id = new_scene_id(anchor)
                summary = await self.summary_builder.summarize_scene(cells=cells)
                scene = MemScene(
                    id=scene_id,
                    title=cells[0].episodic_trace[:72],
                    summary=summary.summary_text,
                    cell_ids=[c.id for c in cells],
                    created_at=now_utc(),
                    updated_at=now_utc(),
                )
                await self.repository.upsert_scene(scene)
                updated_scene_ids.append(scene_id)

                for cell in cells:
                    await self.repository.set_memcell_scene(cell.id, scene_id)
                    # Minimal KG extraction rule: only ingest explicit atomic facts of
                    # form "A|predicate|B" for deterministic behavior.
                    for fact in cell.atomic_facts:
                        parts = [p.strip() for p in fact.split("|")]
                        if len(parts) != 3:
                            continue
                        triple = KgTriple(
                            subject=parts[0],
                            predicate=parts[1],
                            object=parts[2],
                            valid_from=cell.timestamp,
                            valid_until=None,
                            scene_id=scene_id,
                        )
                        await self.repository.upsert_kg_triple(triple)

        return updated_scene_ids

    async def nightly_rollup(
        self,
        *,
        current_date: date,
        transcript: str,
        session_id: str,
    ) -> dict[str, int]:
        """Run nightly DSP + foresight lifecycle tasks.

        Summary rollups (daily/weekly/monthly) are data-source dependent and are
        expected to be implemented by repository-backed analytics queries.
        This method focuses on deterministic lifecycle steps needed immediately.
        """

        with maybe_langfuse_trace("nightly_rollup"):
            expired = await self.repository.archive_expired_foresight_signals(
                current_date
            )

            dsp_raw = await self.dsp_extractor.extract_dsp(
                transcript=transcript,
                current_date=current_date,
            )
            extraction = DspExtraction(
                id=f"dsp-extract:{session_id}:{int(datetime.now().timestamp())}",
                session_id=session_id,
                extracted_at=now_utc(),
                user_facts=dsp_raw.user_facts,
                user_preferences=dsp_raw.user_preferences,
                ai_self_facts=dsp_raw.ai_self_facts,
            )
            await self.repository.add_dsp_extraction(extraction)

            recent = await self.repository.list_recent_dsp_extractions(limit=30)
            active_dsp = await self.repository.get_active_dsp()
            dsp_updated = 0

            if active_dsp is None and len(recent) >= self.dsp_bootstrap_min_extractions:
                content = await self.dsp_builder.build_initial(extractions=recent)
                await self.repository.set_active_dsp(
                    DspVersion(
                        id=f"dsp:{int(datetime.now().timestamp())}",
                        content=content,
                        created_at=now_utc(),
                    )
                )
                dsp_updated = 1
            elif active_dsp is not None and len(recent) >= self.dsp_update_batch_size:
                content = await self.dsp_builder.build_update(
                    current_dsp=active_dsp.content,
                    extractions=recent[: self.dsp_update_batch_size],
                )
                if content.strip() != active_dsp.content.strip():
                    await self.repository.set_active_dsp(
                        DspVersion(
                            id=f"dsp:{int(datetime.now().timestamp())}",
                            content=content,
                            created_at=now_utc(),
                        )
                    )
                    dsp_updated = 1

            return {
                "expired_foresight_signals": expired,
                "dsp_updated": dsp_updated,
            }

    async def run_curator(
        self,
        *,
        current_date: date,
        max_memories: int = 500,
    ) -> CurationResult:
        """Classify all MemCells and delete low-value ones.

        Runs the configured curator (or RuleBasedMemCellCurator by default).
        If the retained count still exceeds max_memories, the lowest-salience
        KEEP_IMPORTANT entries are evicted until the limit is met.
        KEEP_FUTURE entries are never evicted by the overflow pass.
        """
        effective_curator: MemCellCurator = self.curator or RuleBasedMemCellCurator()

        with maybe_langfuse_trace("memory_curator") as trace:
            summaries = await self.repository.list_memcell_summaries(today=current_date)
            if not summaries:
                return CurationResult(
                    inspected=0,
                    removed=0,
                    retained=0,
                    kept_future=0,
                    kept_important=0,
                )

            decisions = await effective_curator.classify(summaries, current_date)
            summary_by_id = {s.id: s for s in summaries}

            to_remove: list[str] = []
            kept_future: list[tuple[str, MemCellSummary]] = []
            kept_important: list[tuple[str, MemCellSummary]] = []

            for cell_id, decision in decisions:
                if decision == CuratorDecision.REMOVE:
                    to_remove.append(cell_id)
                elif decision == CuratorDecision.KEEP_FUTURE:
                    kept_future.append((cell_id, summary_by_id[cell_id]))
                else:
                    kept_important.append((cell_id, summary_by_id[cell_id]))

            # Evict lowest-salience KEEP_IMPORTANT entries when over the cap.
            # KEEP_FUTURE is protected and never evicted in this pass.
            retained_count = len(kept_future) + len(kept_important)
            if retained_count > max_memories:
                from datetime import timezone as _tz

                now = datetime(
                    current_date.year,
                    current_date.month,
                    current_date.day,
                    tzinfo=_tz.utc,
                )

                def _salience(s: MemCellSummary) -> float:
                    ts = s.timestamp
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=_tz.utc)
                    age_s = max(0.0, (now - ts.astimezone(_tz.utc)).total_seconds())
                    recency = 0.5 ** (age_s / _CURATOR_HALF_LIFE_SECONDS)
                    return compute_memcell_salience(
                        emotional_intensity=s.emotional_intensity,
                        retrieval_count=s.retrieval_count,
                        recency_score=recency,
                        explicit_importance=s.explicit_importance,
                    )

                kept_important.sort(key=lambda t: _salience(t[1]))
                overage = retained_count - max_memories
                evicted = kept_important[:overage]
                kept_important = kept_important[overage:]
                to_remove.extend(cell_id for cell_id, _ in evicted)

            removed = await self.repository.delete_memcells(to_remove)
            result = CurationResult(
                inspected=len(summaries),
                removed=removed,
                retained=len(kept_future) + len(kept_important),
                kept_future=len(kept_future),
                kept_important=len(kept_important),
            )

            if trace is not None:
                try:
                    typed_trace = cast(LangfuseTraceLike, trace)
                    typed_trace.update(
                        output={
                            "inspected": result.inspected,
                            "removed": result.removed,
                            "retained": result.retained,
                        }
                    )
                except Exception:
                    pass

        return result

    @staticmethod
    def _to_emotional_tag(raw_cell: MemCellExtractionModel) -> EmotionalTag:
        tag = raw_cell.emotional_tag
        return EmotionalTag(
            state_snapshot=dict(tag.state_snapshot),
            dominant_emotion=tag.dominant_emotion,
            intensity=tag.intensity,
            valence=tag.valence,
        )

    @staticmethod
    def _to_foresight_signals(
        raw_cell: MemCellExtractionModel,
    ) -> list[ForesightSignal]:
        signals: list[ForesightSignal] = []
        for item in raw_cell.foresight_signals:
            signals.append(
                ForesightSignal(
                    content=item.content,
                    valid_until=item.valid_until,
                    trigger=item.trigger,
                    emotional_implication=dict(item.emotional_implication),
                )
            )
        return signals

    @staticmethod
    def _fallback_atomic_facts(episodic_trace: str) -> list[str]:
        first_sentence = re.split(r"[\.\n!?]", episodic_trace, maxsplit=1)[0].strip()
        if not first_sentence:
            return []
        return [f"Conversation|summary|{first_sentence[:160]}"]


# Lightweight default strategy implementations for tests and local dry-runs.
class RuleBasedSummaryBuilder:
    async def summarize_scene(self, *, cells: list[MemCell]) -> SummaryResultModel:
        snippets = [c.episodic_trace.strip() for c in cells if c.episodic_trace.strip()]
        summary = " ".join(snippets[:3]).strip()
        if not summary:
            summary = "No significant events recorded."
        return SummaryResultModel(summary_text=summary)


class RuleBasedDspBuilder:
    async def build_initial(self, *, extractions: list[DspExtraction]) -> str:
        facts: list[str] = []
        prefs: list[str] = []
        for item in extractions:
            facts.extend(item.user_facts)
            prefs.extend(item.user_preferences)
        facts_text = "; ".join(dict.fromkeys(facts)) or "No stable facts yet."
        prefs_text = "; ".join(dict.fromkeys(prefs))
        if prefs_text:
            return f"<user_profile>{facts_text}\nCOMMUNICATION PREFERENCES: {prefs_text}</user_profile>"
        return f"<user_profile>{facts_text}</user_profile>"

    async def build_update(
        self, *, current_dsp: str, extractions: list[DspExtraction]
    ) -> str:
        updated = await self.build_initial(extractions=extractions)
        if updated.strip() == "<user_profile>No stable facts yet.</user_profile>":
            return current_dsp
        return updated
