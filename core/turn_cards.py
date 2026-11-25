"""One expandable card per turn: live while it runs, kept when it is over.

A turn is one item the consumer takes off the message queue: a chat message, an action,
a Grillo beat, a Vessel reflection... Until now its story was spread over the Queue tab
(what is waiting), the Reason Trail (what drove the reply) and the Grillo tab (the beats).
This module keeps **one record per turn**, filled in as the turn moves:

* it is **born** when the item is queued up for the consumer (``trigger``, how long it
  waited) and is shown in the Queue view while it **runs**;
* each stage **adds its piece** (recon, the body, the corrector turns, the actions, the
  debrief, the emotion, the memories that were recalled) with how long it took;
* when the turn is over it is **processed** or **failed** (definitively), saved, and shown
  in History.

The rules, from the brief:

* **No erroneous messages.** :func:`attempt` keeps the *correct* LLM answer, or, if none
  was correct, the *last* one that arrived. Each failed try is counted, never stored.
* **Statistics on the card**: queue wait, total and per-stage seconds, and how many
  corrector turns ran (and whether they got a correct answer).
* **Fail-open.** Recording a turn must never touch the turn: every public function
  swallows its own errors and does nothing without a current card.

Stages find their card through a ``ContextVar``, so they need no plumbing: ``note()``,
``stage()`` and ``attempt()`` act on the turn the calling task is serving (a task created
from it, such as a detached agent turn, shares the same card).
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import itertools
import json
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from core.logging_utils import log_debug, log_warning

QUEUED = "queued"
PROCESSING = "processing"
PROCESSED = "processed"
FAILED = "failed"
STATUSES = (QUEUED, PROCESSING, PROCESSED, FAILED)

#: Caps that keep a card small enough to store and to draw.
MAX_STRING = 4000
MAX_REPLY = 20000
MAX_ITEMS = 50
MAX_DEPTH = 6
MAX_SECTION_BYTES = 24_000
MAX_SECTIONS = 40
#: How many finished cards stay in memory (the store keeps more).
MEMORY_RING = 200


def clean(value: Any, depth: int = 0) -> Any:
    """``value`` as JSON-safe data of bounded size (never raises)."""
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) != float("inf") else None
    if isinstance(value, str):
        return value if len(value) <= MAX_STRING else value[:MAX_STRING] + "…"
    if depth >= MAX_DEPTH:
        return "…"
    if isinstance(value, dict):
        return {
            str(k)[:100]: clean(v, depth + 1)
            for k, v in itertools.islice(value.items(), MAX_ITEMS)
        }
    if isinstance(value, (list, tuple, set, frozenset, deque)):
        return [clean(v, depth + 1) for v in itertools.islice(value, MAX_ITEMS)]
    try:
        return clean(repr(value), depth)
    except Exception:  # noqa: BLE001
        return "<unprintable>"


@dataclass
class Section:
    """One piece of the card (recon, body, debrief...)."""

    name: str
    seconds: float = 0.0
    count: int = 0
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "seconds": round(self.seconds, 3),
            "count": self.count,
            "data": self.data,
        }


@dataclass
class TurnCard:
    id: str
    kind: str = "chat"
    interface: str = "unknown"
    interface_path: str | None = None
    chat_name: str | None = None
    priority_label: str | None = None
    #: What started the turn: the message, the action or the beat (JSON-safe).
    trigger: dict[str, Any] = field(default_factory=dict)
    queued_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    status: str = QUEUED
    sections: dict[str, Section] = field(default_factory=dict)
    corrector_turns: int = 0
    corrected: bool | None = None  # did the corrector end with a correct answer
    attempts: int = 0
    reply: dict[str, Any] | None = None  # {"text", "valid"}
    error: str | None = None
    #: A merged batch: how many queued items this one turn answered.
    merged: int = 1
    #: Stages running right now, outermost first (not part of the output).
    active: list[str] = field(default_factory=list, repr=False)

    # ---------------------------------------------------------------- recording

    def section(self, name: str) -> Section:
        name = str(name)[:40]
        found = self.sections.get(name)
        if found is None:
            if len(self.sections) >= MAX_SECTIONS - 1:  # keep room for "other"
                name = "other"
                found = self.sections.get(name)
            if found is None:
                found = self.sections[name] = Section(name)
        return found

    def note(
        self,
        name: str,
        data: dict[str, Any] | None = None,
        seconds: float | None = None,
    ) -> None:
        """Add to a section: merge ``data`` (later keys win), add ``seconds``, count it."""
        piece = self.section(name)
        piece.count += 1
        if seconds is not None:
            piece.seconds += max(0.0, float(seconds))
        if data:
            merged = {**piece.data, **(clean(data) if isinstance(data, dict) else {})}
            if len(json.dumps(merged, default=str)) <= MAX_SECTION_BYTES:
                piece.data = merged
            else:
                piece.data = {**piece.data, "truncated": True}

    def attempt(self, text: Any, valid: bool) -> None:
        """An LLM answer arrived. Keep the correct one, else the last one."""
        self.attempts += 1
        shown = str(text if text is not None else "")[:MAX_REPLY]
        if valid:
            self.reply = {"text": shown, "valid": True}
        elif self.reply is None or not self.reply.get("valid"):
            self.reply = {"text": shown, "valid": False}

    def corrector_turn(self, converged: bool | None = None) -> None:
        self.corrector_turns += 1
        if converged is not None:
            self.corrected = converged

    # ------------------------------------------------------------------- output

    def queue_seconds(self) -> float | None:
        if self.started_at is None:
            return None
        return max(0.0, self.started_at - self.queued_at)

    def processing_seconds(self, now: float | None = None) -> float | None:
        if self.started_at is None:
            return None
        end = self.finished_at if self.finished_at is not None else (now or time.time())
        return max(0.0, end - self.started_at)

    def to_dict(self, now: float | None = None) -> dict[str, Any]:
        timings = {
            name: round(s.seconds, 3) for name, s in self.sections.items() if s.seconds
        }
        return {
            "id": self.id,
            "status": self.status,
            "kind": self.kind,
            "interface": self.interface,
            "interface_path": self.interface_path,
            "chat_name": self.chat_name,
            "priority_label": self.priority_label,
            "merged": self.merged,
            "queued_at": self.queued_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "queue_seconds": _round(self.queue_seconds()),
            "processing_seconds": _round(self.processing_seconds(now)),
            "trigger": self.trigger,
            "timings": timings,
            "corrector": {"turns": self.corrector_turns, "converged": self.corrected},
            "attempts": self.attempts,
            "reply": self.reply,
            "error": self.error,
            "sections": [s.to_dict() for s in self.sections.values()],
        }

    def summary(self) -> str:
        text = str(self.trigger.get("text") or self.trigger.get("description") or "")
        return " ".join(text.split())[:200]


def _round(value: float | None) -> float | None:
    return None if value is None else round(value, 3)


# ------------------------------------------------------------------------- store


class TurnStore(Protocol):
    async def save(self, card: dict[str, Any]) -> None: ...

    async def recent(
        self, limit: int, *, status: str | None = None, search: str | None = None
    ) -> list[dict[str, Any]]: ...

    async def get(self, card_id: str) -> dict[str, Any] | None: ...


class MemoryStore:
    """Finished cards kept in memory (the default; also what the DB store falls back to)."""

    def __init__(self, size: int = MEMORY_RING) -> None:
        self._cards: deque[dict[str, Any]] = deque(maxlen=size)

    async def save(self, card: dict[str, Any]) -> None:
        self._cards.append(card)

    async def recent(
        self, limit: int, *, status: str | None = None, search: str | None = None
    ) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        needle = (search or "").strip().lower()
        for card in reversed(self._cards):
            if status and card.get("status") != status:
                continue
            if needle and needle not in _haystack(card):
                continue
            out.append(card)
            if len(out) >= limit:
                break
        return out

    async def get(self, card_id: str) -> dict[str, Any] | None:
        return next((c for c in self._cards if c.get("id") == card_id), None)


def _haystack(card: dict[str, Any]) -> str:
    trigger = card.get("trigger") or {}
    parts = [
        card.get("interface_path"),
        card.get("chat_name"),
        card.get("kind"),
        trigger.get("text") if isinstance(trigger, dict) else None,
        (card.get("reply") or {}).get("text"),
    ]
    return " ".join(str(p) for p in parts if p).lower()


_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS turn_cards (
    id VARCHAR(40) PRIMARY KEY,
    created_at DOUBLE NOT NULL,
    kind VARCHAR(40),
    status VARCHAR(20),
    interface_path VARCHAR(512),
    summary VARCHAR(300),
    total_seconds DOUBLE,
    payload TEXT,
    INDEX idx_turn_cards_created_at (created_at),
    INDEX idx_turn_cards_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
"""


class DbStore:
    """Finished cards in the Core database, with a memory copy when the DB is down."""

    def __init__(self, max_rows: int = 2000) -> None:
        self.max_rows = max_rows
        self._ready = False
        self._fallback = MemoryStore()

    async def _ensure(self, cur: Any) -> None:
        if not self._ready:
            await cur.execute(_TABLE_SQL)
            self._ready = True

    async def save(self, card: dict[str, Any]) -> None:
        await self._fallback.save(card)
        try:
            from core.db import get_conn_ctx

            payload = json.dumps(card, ensure_ascii=False, default=str)
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await self._ensure(cur)
                    await cur.execute(
                        "INSERT INTO turn_cards (id, created_at, kind, status, interface_path, "
                        "summary, total_seconds, payload) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                        [
                            card["id"],
                            card.get("queued_at") or time.time(),
                            card.get("kind"),
                            card.get("status"),
                            (card.get("interface_path") or "")[:512],
                            _summary_of(card),
                            card.get("processing_seconds"),
                            payload,
                        ],
                    )
                    await cur.execute(
                        "DELETE FROM turn_cards WHERE created_at < ("
                        "SELECT MIN(created_at) FROM (SELECT created_at FROM turn_cards "
                        "ORDER BY created_at DESC LIMIT %s) AS keep)",
                        [self.max_rows],
                    )
                try:
                    await conn.commit()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001 - the memory copy still serves it
            log_warning(f"[turn_cards] could not store a card in the database: {exc}")

    async def recent(
        self, limit: int, *, status: str | None = None, search: str | None = None
    ) -> list[dict[str, Any]]:
        try:
            from core.db import get_conn_ctx

            clauses: list[str] = []
            params: list[Any] = []
            if status:
                clauses.append("status = %s")
                params.append(status)
            if search:
                like = f"%{search.strip().lower()}%"
                clauses.append(
                    "(LOWER(summary) LIKE %s OR LOWER(interface_path) LIKE %s)"
                )
                params += [like, like]
            where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await self._ensure(cur)
                    await cur.execute(
                        f"SELECT payload FROM turn_cards {where} ORDER BY created_at DESC LIMIT %s",
                        [*params, int(limit)],
                    )
                    rows = await cur.fetchall()
            cards = [
                json.loads(r["payload"] if isinstance(r, dict) else r[0])
                for r in rows or []
            ]
            if cards or not (status or search):
                return cards
        except Exception as exc:  # noqa: BLE001
            log_debug(f"[turn_cards] database read failed, using memory: {exc}")
        return await self._fallback.recent(limit, status=status, search=search)

    async def get(self, card_id: str) -> dict[str, Any] | None:
        try:
            from core.db import get_conn_ctx

            async with get_conn_ctx() as conn:
                async with conn.cursor() as cur:
                    await self._ensure(cur)
                    await cur.execute(
                        "SELECT payload FROM turn_cards WHERE id = %s", [card_id]
                    )
                    row = await cur.fetchone()
            if row:
                return json.loads(row["payload"] if isinstance(row, dict) else row[0])
        except Exception as exc:  # noqa: BLE001
            log_debug(f"[turn_cards] database read failed, using memory: {exc}")
        return await self._fallback.get(card_id)


def _summary_of(card: dict[str, Any]) -> str:
    trigger = card.get("trigger") or {}
    text = ""
    if isinstance(trigger, dict):
        text = str(trigger.get("text") or trigger.get("description") or "")
    return " ".join(text.split())[:300]


# ------------------------------------------------------------------------ module

_live: dict[str, TurnCard] = {}
_current: contextvars.ContextVar[TurnCard | None] = contextvars.ContextVar(
    "synth_turn_card", default=None
)
_store: TurnStore = MemoryStore()
_pending: set[asyncio.Task[None]] = set()


def set_store(store: TurnStore) -> None:
    global _store
    _store = store


def get_store() -> TurnStore:
    return _store


def current() -> TurnCard | None:
    return _current.get()


def begin(
    *,
    kind: str,
    interface: str,
    interface_path: str | None = None,
    chat_name: str | None = None,
    priority_label: str | None = None,
    trigger: dict[str, Any] | None = None,
    queued_at: float | None = None,
    merged: int = 1,
) -> TurnCard | None:
    """Start the card of the turn the calling task is about to serve."""
    try:
        card = TurnCard(
            id=uuid.uuid4().hex[:20],
            kind=kind,
            interface=interface,
            interface_path=interface_path,
            chat_name=chat_name,
            priority_label=priority_label,
            trigger=clean(trigger or {}),
            queued_at=queued_at or time.time(),
            started_at=time.time(),
            status=PROCESSING,
            merged=max(1, int(merged)),
        )
        _live[card.id] = card
        _current.set(card)
        return card
    except Exception as exc:  # noqa: BLE001
        log_debug(f"[turn_cards] begin skipped: {exc}")
        return None


def finish(failed: bool = False, error: str | None = None) -> None:
    """The current turn is over: processed, or definitively failed. Saves the card."""
    try:
        card = _current.get()
        if card is None:
            return
        _current.set(None)
        _live.pop(card.id, None)
        card.finished_at = time.time()
        failed = failed or card.status == FAILED
        card.status = FAILED if failed else PROCESSED
        if failed and error:
            card.error = str(error)[:300]
        data = card.to_dict()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(_save(data))
        _pending.add(task)
        task.add_done_callback(_pending.discard)
    except Exception as exc:  # noqa: BLE001
        log_debug(f"[turn_cards] finish skipped: {exc}")


async def _save(data: dict[str, Any]) -> None:
    try:
        await _store.save(data)
    except Exception as exc:  # noqa: BLE001
        log_debug(f"[turn_cards] save skipped: {exc}")


def mark_failed(error: str | None = None) -> None:
    """Remember that the current turn did not get through (``finish`` still ends it)."""
    card = _current.get()
    if card is not None:
        card.status = FAILED
        if error:
            card.error = str(error)[:300]


def note(
    section: str, data: dict[str, Any] | None = None, seconds: float | None = None
) -> None:
    card = _current.get()
    if card is not None:
        try:
            card.note(section, data, seconds)
        except Exception as exc:  # noqa: BLE001
            log_debug(f"[turn_cards] note skipped: {exc}")


def attempt(text: Any, valid: bool) -> None:
    card = _current.get()
    if card is not None:
        try:
            card.attempt(text, valid)
        except Exception as exc:  # noqa: BLE001
            log_debug(f"[turn_cards] attempt skipped: {exc}")


def note_reason(reason: dict[str, Any]) -> None:
    """Add what drove the turn (``turn_reason.build_reason_summary``) to its card: the
    memories that were recalled and the emotional context."""
    note(
        "memories",
        {
            "recalled": reason.get("memories"),
            "diary_sources": reason.get("diary_sources"),
        },
    )
    note(
        "emotions",
        {
            "emotion": reason.get("emotion"),
            "goal": reason.get("goal"),
            "beat_type": reason.get("beat_type"),
            "history_scope": reason.get("history_scope"),
        },
    )


def corrector_turn(converged: bool | None = None) -> None:
    card = _current.get()
    if card is not None:
        card.corrector_turn(converged)


class stage:  # noqa: N801 - used like a context manager: ``with stage("recon"):``
    """Time a stage and add it to the card; works with ``with`` and ``async with``."""

    def __init__(self, section: str, data: dict[str, Any] | None = None) -> None:
        self.section, self.data = section, data
        self._start = 0.0

    def __enter__(self) -> "stage":
        self._start = time.monotonic()
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        note(self.section, self.data, time.monotonic() - self._start)

    async def __aenter__(self) -> "stage":
        return self.__enter__()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.__exit__(exc_type, exc, tb)


def timed(section: str, *, corrector: bool = False) -> Callable[[Any], Any]:
    """Decorator: time an ``async`` function as a stage of the current turn.

    The wrapper changes nothing about the call: same arguments, same result, same
    exceptions; recording is fail-open. With ``corrector=True`` the call is also counted as
    a corrector turn (converged when it returned a correction), except inside the
    debrief, whose own corrections belong to the debrief, not to the reply.
    """

    def decorate(fn: Any) -> Any:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            card = _current.get()
            if card is None:
                return await fn(*args, **kwargs)
            started = time.monotonic()
            result: Any = None
            nested = "debrief" in card.active
            card.active.append(section)
            try:
                result = await fn(*args, **kwargs)
                return result
            finally:
                try:
                    card.active.remove(section)
                    seconds = time.monotonic() - started
                    if corrector and not nested:
                        card.corrector_turn(converged=bool(result))
                        card.note(section, None, seconds)
                    elif corrector:
                        card.note("debrief", {"corrector_turns": 1}, None)
                    else:
                        card.note(section, None, seconds)
                except Exception as exc:  # noqa: BLE001
                    log_debug(f"[turn_cards] timing skipped: {exc}")

        return wrapper

    return decorate


def live(now: float | None = None) -> list[dict[str, Any]]:
    """The cards of the turns running now, newest first."""
    cards = sorted(_live.values(), key=lambda c: c.queued_at, reverse=True)
    return [c.to_dict(now) for c in cards]


async def recent(
    limit: int = 100, *, status: str | None = None, search: str | None = None
) -> list[dict[str, Any]]:
    limit = max(1, min(500, int(limit)))
    return await _store.recent(limit, status=status, search=search)


async def get(card_id: str) -> dict[str, Any] | None:
    for card in _live.values():
        if card.id == card_id:
            return card.to_dict()
    return await _store.get(card_id)


async def drain() -> None:
    """Wait for card saves in progress (tests, shutdown)."""
    if _pending:
        await asyncio.gather(*list(_pending), return_exceptions=True)


def reset_for_tests() -> None:
    global _store
    _live.clear()
    _current.set(None)
    _store = MemoryStore()
