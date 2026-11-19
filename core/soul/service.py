"""SoulService: the one door to SOUL's long-term memory for everything outside it.

Before this module, other components reached into SOUL through
``PLUGIN_REGISTRY["soul_plugin"].get_repository()`` and wrote or read its tables
directly, while a second, unrelated memory path (``search_memories`` over
``memories`` / ``ai_diary``) answered the same questions from different data. A
consumer that wants to *remember* something or *recall* something goes through
this service instead:

* :meth:`SoulService.ingest_summary` files a piece of text the persona should be
  able to recall later (a diary entry, a compacted day, a retired legacy memory)
  under a stable ``source_key``, so running it twice updates one cell instead of
  creating two.
* :meth:`SoulService.recall` ranks stored cells for a query, optionally limited to
  some sources and to a date range.

Internals (tables, scoring, curator) stay behind it. See ``AGENTS.md`` for the
routing rules.
"""

from __future__ import annotations

import re
from collections.abc import Collection
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from core.logging_utils import log_debug, log_warning

from .models import EmotionalTag, MemCell, MemCellRecall

#: Kinds a cell can be ingested as. ``chat`` is reserved for cells the compiler
#: writes from conversations and is never produced by :meth:`ingest_summary`.
INGEST_KINDS: frozenset[str] = frozenset({"journal", "diary_day", "legacy_memory"})
ALL_KINDS: frozenset[str] = INGEST_KINDS | {"chat"}

#: Where ingested cells live. They belong to no conversation, so recall treats
#: them as "other scope": they need a stronger match than a same-chat cell.
_SESSION_FOR_KIND: dict[str, str] = {
    "journal": "journal",
    "diary_day": "journal",
    "legacy_memory": "legacy",
}

# Sessions whose cells are real memories but only matter when the conversation is
# about their subject (the radio's chatter, 552 cells on the live store, would
# otherwise surface in unrelated chats). Passive recall skips them. The plugin that
# owns the subject declares the session-id prefixes (both spellings an interface
# path can take once stored) and asks for them back through
# ``recall(session_prefixes=...)`` when the turn is about it; nothing here names a
# plugin.
_on_demand_prefixes: set[str] = set()


def register_on_demand_session_prefixes(*prefixes: str) -> None:
    """Declare session-id prefixes that passive recall must leave out."""
    _on_demand_prefixes.update(p for p in prefixes if p)


# Sessions that hold system text, not a conversation: the rollup's own session, the
# diary consolidator's transcripts and the notices a finished web search injects. The
# id is matched in both spellings because an interface path is normalised before it
# becomes a session id (``diary_merge/-1`` is stored as ``diary_merge_-1``).
HOUSEKEEPING_SESSION_PREFIXES: tuple[str, ...] = (
    "diary_merge:",
    "diary_merge_",
    "web_search:",
    "web_search_",
)


def is_housekeeping_session(session_id: str | None) -> bool:
    """True for the sessions that must never be recalled."""
    sid = str(session_id or "").strip().lower()
    return sid == "nightly" or sid.startswith(HOUSEKEEPING_SESSION_PREFIXES)


def on_demand_session_prefixes() -> tuple[str, ...]:
    """The registered on-demand session prefixes, for the passive-recall exclusion."""
    return tuple(sorted(_on_demand_prefixes))


# A cell's trace is embedded and ranked; a diary day can run to many thousands of
# characters, far more than the embedder's window and than any prompt will show.
MAX_TRACE_CHARS = 4000

_WS_RE = re.compile(r"\s+")
_NEUTRAL_STATE: dict[str, float] = {"joy": 0.0, "fear": 0.0, "sad": 0.0, "anger": 0.0}


@dataclass(slots=True)
class IngestResult:
    """What an :meth:`SoulService.ingest_summary` call did."""

    cell_id: str
    #: ``created`` (new cell), ``updated`` (text changed) or ``unchanged``.
    status: str


def _clean(text: str) -> str:
    return _WS_RE.sub(" ", str(text or "")).strip()[:MAX_TRACE_CHARS]


#: A long text is filed as blocks of about this many characters. The embedder reads
#: roughly the first 500 tokens, so a long diary day filed as one cell is searchable
#: only by its opening; blocks are each searchable by their own content.
CHUNK_TARGET_CHARS = 1500
#: Upper bound of blocks filed for one source, and of the stale ones looked for.
MAX_CHUNKS = 40
_PARAGRAPH_RE = re.compile(r"\n\s*\n(?:-{3,}\s*\n\s*\n)?")


def chunk_text(text: str, target: int = CHUNK_TARGET_CHARS) -> list[str]:
    """Split ``text`` into blocks of about ``target`` characters.

    Paragraphs (blank-line separated) are kept whole and packed up to ``target``; a
    paragraph longer than ``target`` is cut at whitespace. Language-neutral: nothing
    but whitespace and length is looked at. Short text comes back as one block.
    """
    paragraphs = [
        " ".join(part.split())
        for part in _PARAGRAPH_RE.split(str(text or ""))
        if part.strip()
    ]
    pieces: list[str] = []
    for paragraph in paragraphs:
        while len(paragraph) > target * 1.5:
            cut = paragraph.rfind(" ", 0, target)
            cut = cut if cut > target // 2 else target
            pieces.append(paragraph[:cut].strip())
            paragraph = paragraph[cut:].strip()
        pieces.append(paragraph)
    blocks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + 1 + len(piece) > target:
            blocks.append(current)
            current = ""
        current = f"{current} {piece}".strip()
    if current:
        blocks.append(current)
    return blocks[:MAX_CHUNKS] or ([_clean(text)] if _clean(text) else [])


def source_base(cell_source_key: str | None) -> str:
    """The key of the text a block came from (``ai_diary:7#2`` -> ``ai_diary:7``)."""
    return str(cell_source_key or "").split("#", 1)[0]


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class SoulService:
    """Stable read/write API over a SOUL repository and its embedder."""

    def __init__(self, repository: Any, embedder: Any) -> None:
        self._repo = repository
        self._embedder = embedder

    @classmethod
    def from_registry(cls) -> "SoulService | None":
        """The service for the running SOUL plugin, or ``None`` when it is off.

        ``None`` is a supported answer: SOUL can be disabled, and a caller (the
        diary, for one) must carry on without it rather than fail.
        """
        try:
            from core.core_initializer import PLUGIN_REGISTRY

            plugin = PLUGIN_REGISTRY.get("soul_plugin")
            get_service = getattr(plugin, "get_service", None)
            return get_service() if callable(get_service) else None
        except Exception as exc:
            log_debug(f"[soul_service] SOUL is not available: {exc}")
            return None

    async def ingest_summary(
        self,
        *,
        kind: str,
        source_key: str,
        text: str,
        event_timestamp: datetime,
        emotion: str = "neutral",
        intensity: float = 0.0,
        importance: float = 0.0,
        atomic_facts: list[str] | None = None,
    ) -> IngestResult | None:
        """File ``text`` as a recallable cell, idempotently.

        The cell id is ``<kind>:<source_key>``. Ingesting the same key again with
        the same text changes nothing; with different text it replaces the trace
        and embedding but keeps the cell's retrieval history and importance, so a
        diary day that is appended to during the day stays one cell.

        Returns ``None`` when the text is empty or the kind is not ingestable.
        """
        # TODO(privacy): ingested cells are ``shared``. A diary entry can repeat
        # something told in confidence; it needs the audience of the day it
        # summarises (or a redaction pass) before it is allowed to leave that chat.
        if kind not in INGEST_KINDS:
            raise ValueError(
                f"cannot ingest kind {kind!r}; expected {sorted(INGEST_KINDS)}"
            )
        trace = _clean(text)
        key = str(source_key or "").strip()
        if not trace or not key:
            return None

        cell_id = f"{kind}:{key}"
        existing = await self._repo.get_memcell(cell_id)
        if existing is not None and existing.episodic_trace == trace:
            return IngestResult(cell_id=cell_id, status="unchanged")

        embedding = await self._embedder.embed(trace)
        cell = MemCell(
            id=cell_id,
            episodic_trace=trace,
            atomic_facts=list(atomic_facts or []),
            emotional_tag=EmotionalTag(
                state_snapshot=dict(_NEUTRAL_STATE),
                dominant_emotion=(emotion or "neutral"),
                intensity=max(0.0, min(1.0, float(intensity))),
                valence=0.0,
            ),
            foresight_signals=[],
            event_timestamp=_aware(event_timestamp),
            session_id=_SESSION_FOR_KIND[kind],
            embedding=embedding,
            retrieval_count=existing.retrieval_count if existing else 0,
            explicit_importance=(
                existing.explicit_importance
                if existing
                else max(0.0, min(1.0, float(importance)))
            ),
            # Ingested text is already first-person prose or an already-distilled
            # summary: stamping it keeps recall (which hides unstamped cells while
            # a distilling extractor is active) from filtering it out.
            distilled_at=datetime.now(timezone.utc),
            consolidated=True,
            source_kind=kind,
            source_key=key,
        )
        await self._repo.upsert_memcell(cell)
        return IngestResult(
            cell_id=cell_id, status="created" if existing is None else "updated"
        )

    @staticmethod
    def render_hit(match: MemCellRecall, *, label: str, limit: int = 300) -> str:
        """One line for a prompt: ``<date> <label>: <trace>``, the trace shortened."""
        trace = _WS_RE.sub(" ", match.cell.episodic_trace).strip()
        if len(trace) > limit:
            trace = trace[:limit].rstrip() + "..."
        when = _aware(match.cell.event_timestamp).date().isoformat()
        return f"{when} {label}: {trace}"

    async def ingest_chunked(
        self,
        *,
        kind: str,
        source_key: str,
        text: str,
        event_timestamp: datetime,
        emotion: str = "neutral",
        intensity: float = 0.0,
        importance: float = 0.0,
        atomic_facts: list[str] | None = None,
        chunk_chars: int = CHUNK_TARGET_CHARS,
    ) -> list[IngestResult]:
        """File ``text`` as one cell, or as several blocks when it is long.

        A text of about ``chunk_chars`` or less is filed exactly as
        :meth:`ingest_summary` files it (cell ``<kind>:<source_key>``). A longer one
        becomes blocks ``<kind>:<source_key>#0``, ``#1``, ... each embedded on its
        own, so a detail from the middle of a long diary day can be found. Whichever
        shape the text had before, the other shape is retired (archived, not
        destroyed), so growing or shrinking an entry never leaves both.
        """
        if kind not in INGEST_KINDS:
            raise ValueError(
                f"cannot ingest kind {kind!r}; expected {sorted(INGEST_KINDS)}"
            )
        key = str(source_key or "").strip()
        blocks = chunk_text(text, chunk_chars)
        if not key or not blocks:
            return []
        common: dict[str, Any] = {
            "kind": kind,
            "event_timestamp": event_timestamp,
            "emotion": emotion,
            "intensity": intensity,
            "importance": importance,
            "atomic_facts": atomic_facts,
        }
        results: list[IngestResult] = []
        if len(blocks) == 1:
            result = await self.ingest_summary(source_key=key, text=blocks[0], **common)
            if result is not None:
                results.append(result)
            await self._retire_blocks(kind, key, start=0)
            return results
        for index, block in enumerate(blocks):
            result = await self.ingest_summary(
                source_key=f"{key}#{index}", text=block, **common
            )
            if result is not None:
                results.append(result)
        await self._repo.delete_memcells([f"{kind}:{key}"], reason="rechunked")
        await self._retire_blocks(kind, key, start=len(blocks))
        return results

    async def _retire_blocks(self, kind: str, key: str, *, start: int) -> int:
        """Archive blocks ``#start``, ``#start+1``, ... of ``kind:key`` that exist."""
        stale: list[str] = []
        for index in range(start, MAX_CHUNKS + start):
            cell_id = f"{kind}:{key}#{index}"
            if await self._repo.get_memcell(cell_id) is None:
                break
            stale.append(cell_id)
        if not stale:
            return 0
        return int(await self._repo.delete_memcells(stale, reason="rechunked"))

    async def retire_source(self, *, kind: str, source_key: str) -> bool:
        """Remove the cell (or every block) filed under ``kind``/``source_key``.

        The repository archives what it deletes, so this is reversible. Returns
        whether anything was removed.
        """
        if kind not in INGEST_KINDS:
            raise ValueError(f"cannot retire kind {kind!r}")
        removed = await self._repo.delete_memcells([f"{kind}:{source_key}"])
        removed += await self._retire_blocks(kind, source_key, start=0)
        return bool(removed)

    async def recall(
        self,
        query: str,
        *,
        sources: Collection[str] | None = None,
        date_from: datetime | None = None,
        date_to: datetime | None = None,
        session_id: str | None = None,
        session_prefixes: Collection[str] | None = None,
        limit: int = 5,
    ) -> list[MemCellRecall]:
        """Rank stored cells for ``query``.

        ``session_id`` is the chat the answer is for: it is what lets a memory told
        in confidence in that chat come back there and nowhere else (leave it ``None``
        and no private cell is returned).

        ``sources`` limits the result to those kinds (``chat``, ``journal``,
        ``diary_day``, ``legacy_memory``); ``session_prefixes`` limits it to cells
        whose session id starts with one of them (how a caller asks for the
        on-demand sessions the passive recall leaves out, e.g. the radio's);
        ``date_from`` / ``date_to`` bound the event time. A date bound widens the candidate window, because the bound is
        applied to ranked candidates and a narrow window could hold none inside it.
        """
        unknown = set(sources or ()) - ALL_KINDS
        if unknown:
            log_warning(
                f"[soul_service] ignoring unknown recall sources {sorted(unknown)}"
            )
            sources = set(sources or ()) & ALL_KINDS or None
        text = " ".join(str(query or "").split())[:400]
        if len(text) < 3:
            return []
        embedding = await self._embedder.embed(text)
        windowed = date_from is not None or date_to is not None
        candidates = await self._repo.recall_memories(
            query_text=text,
            query_embedding=embedding,
            session_id=session_id,
            limit=limit,
            candidate_limit=max(limit * 8, 120) if windowed else max(limit * 6, 30),
            source_kinds=set(sources) if sources else None,
            session_prefixes=set(session_prefixes) if session_prefixes else None,
        )
        lower = _aware(date_from) if date_from else None
        upper = _aware(date_to) if date_to else None
        on_demand = on_demand_session_prefixes()
        kept: list[MemCellRecall] = []
        seen_sources: set[str] = set()
        for match in candidates:
            cell = match.cell
            # The same rules as passive recall: system sessions never; sessions that
            # only matter on their subject only when the caller asked for them by
            # prefix; and what was told in confidence in another chat, never.
            if is_housekeeping_session(cell.session_id):
                continue
            sid = str(cell.session_id or "").lower()
            if sid.startswith(on_demand) and not session_prefixes:
                continue
            if cell.visibility == "private" and cell.session_id != session_id:
                continue
            when = _aware(match.cell.event_timestamp)
            if lower and when < lower:
                continue
            if upper and when > upper:
                continue
            if cell.source_kind != "chat" and cell.source_key:
                # Blocks of one diary day all match the same topic; one of them is
                # enough, the rest would be the same day told again.
                base = f"{cell.source_kind}:{source_base(cell.source_key)}"
                if base in seen_sources:
                    continue
                seen_sources.add(base)
            kept.append(match)
            if len(kept) >= limit:
                break
        return kept
