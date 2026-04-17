#!/usr/bin/env python3
"""Migrate legacy MariaDB memories into SOUL PostgreSQL storage.

This script imports historical data from legacy tables:
- chat_history_cache
- memories
- ai_diary

into SOUL's `mem_cells` table via `PostgresSoulRepository`.

The migration is idempotent because memcell IDs are deterministic:
`legacy:<table>:<source_id>`.

Examples:
    uv run python scripts/migrate_legacy_to_soul.py --dry-run
    uv run python scripts/migrate_legacy_to_soul.py --days 180
    uv run python scripts/migrate_legacy_to_soul.py --sources memories,ai_diary
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import aiomysql

from core.soul.models import EmotionalTag, MemCell
from core.soul.repository import PostgresSoulRepository


@dataclass(slots=True)
class MigrationStats:
    chat_history_cache: int = 0
    memories: int = 0
    ai_diary: int = 0
    skipped: int = 0

    @property
    def total(self) -> int:
        return self.chat_history_cache + self.memories + self.ai_diary


@dataclass(slots=True)
class MigrationConfig:
    maria_host: str
    maria_port: int
    maria_user: str
    maria_password: str
    maria_database: str
    soul_postgres_dsn: str
    soul_postgres_schema: str
    days: int
    batch_size: int
    max_rows: int | None
    dry_run: bool
    sources: set[str]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate legacy MariaDB data into SOUL mem_cells"
    )
    parser.add_argument("--days", type=int, default=120, help="Lookback window in days")
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Rows fetched per batch from MariaDB",
    )
    parser.add_argument(
        "--max-rows",
        type=int,
        default=None,
        help="Optional global cap for migrated rows",
    )
    parser.add_argument(
        "--sources",
        type=str,
        default="chat_history_cache,memories,ai_diary",
        help="Comma-separated sources: chat_history_cache, memories, ai_diary",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not write to SOUL DB")
    return parser.parse_args()


def build_config(args: argparse.Namespace) -> MigrationConfig:
    sources = {
        item.strip().lower()
        for item in args.sources.split(",")
        if item and item.strip()
    }

    return MigrationConfig(
        maria_host=os.getenv("DB_HOST", "synth-db"),
        maria_port=int(os.getenv("DB_PORT", "3306")),
        maria_user=os.getenv("DB_USER", "synth"),
        maria_password=os.getenv("DB_PASSWORD", "synth"),
        maria_database=os.getenv("DB_NAME", "synth"),
        soul_postgres_dsn=os.getenv(
            "SOUL_POSTGRES_DSN",
            "postgresql://soul:soul@synth-soul-db:5432/soul_memory",
        ),
        soul_postgres_schema=os.getenv("SOUL_POSTGRES_SCHEMA", "public"),
        days=max(1, int(args.days)),
        batch_size=max(1, int(args.batch_size)),
        max_rows=args.max_rows if args.max_rows and args.max_rows > 0 else None,
        dry_run=bool(args.dry_run),
        sources=sources,
    )


def _as_utc(timestamp: datetime | None) -> datetime:
    if timestamp is None:
        return datetime.now(UTC)
    if timestamp.tzinfo is None:
        return timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _json_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()]
        except Exception:
            pass
        return [raw]
    return [str(value).strip()]


def _emotion_snapshot_from_labels(labels: list[str], intensity: float) -> dict[str, float]:
    joy_words = {"joy", "happy", "glad", "excited", "love"}
    fear_words = {"fear", "anxious", "worried", "nervous", "scared"}
    sad_words = {"sad", "lonely", "depressed", "down"}
    anger_words = {"anger", "angry", "mad", "frustrated", "annoyed"}

    joy = 0.0
    fear = 0.0
    sad = 0.0
    anger = 0.0

    for label in labels:
        token = label.lower().strip()
        if token in joy_words:
            joy = max(joy, intensity)
        if token in fear_words:
            fear = max(fear, intensity)
        if token in sad_words:
            sad = max(sad, intensity)
        if token in anger_words:
            anger = max(anger, intensity)

    return {"joy": joy, "fear": fear, "sad": sad, "anger": anger}


def _dominant_emotion(snapshot: dict[str, float]) -> str:
    if not snapshot:
        return "neutral"
    top = max(snapshot.keys(), key=lambda key: float(snapshot[key]))
    if float(snapshot[top]) <= 0.0:
        return "neutral"
    return top


def _build_emotional_tag(labels: list[str], intensity: float, valence: float) -> EmotionalTag:
    norm_intensity = max(0.0, min(1.0, intensity))
    norm_valence = max(-1.0, min(1.0, valence))
    snapshot = _emotion_snapshot_from_labels(labels, norm_intensity)
    return EmotionalTag(
        state_snapshot=snapshot,
        dominant_emotion=_dominant_emotion(snapshot),
        intensity=norm_intensity,
        valence=norm_valence,
    )


class LegacyToSoulMigrator:
    def __init__(self, config: MigrationConfig) -> None:
        self.config = config
        self.stats = MigrationStats()

    async def run(self) -> None:
        self._print_header()

        maria_conn = await aiomysql.connect(
            host=self.config.maria_host,
            port=self.config.maria_port,
            user=self.config.maria_user,
            password=self.config.maria_password,
            db=self.config.maria_database,
            charset="utf8mb4",
            autocommit=True,
            cursorclass=aiomysql.DictCursor,
        )

        repo = PostgresSoulRepository(
            dsn=self.config.soul_postgres_dsn,
            schema=self.config.soul_postgres_schema,
        )

        try:
            await self._assert_soul_schema(repo)
            if "chat_history_cache" in self.config.sources:
                await self._migrate_chat_history_cache(maria_conn, repo)
            if "memories" in self.config.sources:
                await self._migrate_memories(maria_conn, repo)
            if "ai_diary" in self.config.sources:
                await self._migrate_ai_diary(maria_conn, repo)
        finally:
            maria_conn.close()
            await repo.close()

        self._print_summary()

    async def _assert_soul_schema(self, repo: PostgresSoulRepository) -> None:
        pool = await repo._get_pool()  # noqa: SLF001 - internal check for migration script.
        async with pool.acquire() as conn:
            regclass = await conn.fetchval("SELECT to_regclass('mem_cells')")
            if regclass is None:
                raise RuntimeError(
                    "SOUL schema not found. Run scripts/bootstrap_soul_postgres.sh or .ps1 first."
                )

    async def _migrate_chat_history_cache(
        self, maria_conn: aiomysql.Connection, repo: PostgresSoulRepository
    ) -> None:
        print("\n[1/3] Migrating chat_history_cache ...")
        since = datetime.now() - timedelta(days=self.config.days)
        query = (
            "SELECT id, interface_path, sender_name, sender_id, message_text, timestamp "
            "FROM chat_history_cache "
            "WHERE timestamp >= %s "
            "ORDER BY timestamp ASC"
        )
        async with maria_conn.cursor() as cur:
            await cur.execute(query, (since,))
            rows = await cur.fetchall()

        for row in rows:
            if self._max_rows_reached():
                break

            message_text = _text(row.get("message_text"))
            if not message_text:
                self.stats.skipped += 1
                continue

            interface_path = _text(row.get("interface_path")) or "legacy/chat_history"
            sender_name = _text(row.get("sender_name")) or "unknown"
            sender_id = _text(row.get("sender_id"))
            source_id = row.get("id")

            episodic_trace = f"[{sender_name}] {message_text}"
            atomic_facts = [
                "Legacy|source_table|chat_history_cache",
                f"Legacy|interface_path|{interface_path}",
            ]
            if sender_id:
                atomic_facts.append(f"Legacy|sender_id|{sender_id}")

            memcell = MemCell(
                id=f"legacy:chat_history_cache:{source_id}",
                session_id=interface_path,
                episodic_trace=episodic_trace,
                atomic_facts=atomic_facts,
                emotional_tag=_build_emotional_tag([], intensity=0.1, valence=0.0),
                foresight_signals=[],
                timestamp=_as_utc(row.get("timestamp")),
                embedding=None,
                explicit_importance=0.1,
            )

            await self._upsert(repo, memcell)
            self.stats.chat_history_cache += 1

        print(f"  migrated: {self.stats.chat_history_cache}")

    async def _migrate_memories(
        self, maria_conn: aiomysql.Connection, repo: PostgresSoulRepository
    ) -> None:
        print("\n[2/3] Migrating memories ...")
        since = datetime.now() - timedelta(days=self.config.days)
        query = (
            "SELECT id, timestamp, content, author, source, tags, scope, emotion, intensity "
            "FROM memories "
            "WHERE timestamp >= %s "
            "ORDER BY timestamp ASC"
        )
        async with maria_conn.cursor() as cur:
            await cur.execute(query, (since,))
            rows = await cur.fetchall()

        for row in rows:
            if self._max_rows_reached():
                break

            content = _text(row.get("content"))
            if not content:
                self.stats.skipped += 1
                continue

            source_id = row.get("id")
            author = _text(row.get("author"))
            source = _text(row.get("source"))
            scope = _text(row.get("scope")) or "legacy"
            emotion = _text(row.get("emotion"))
            tags = _json_list(row.get("tags"))

            intensity_raw = row.get("intensity")
            if isinstance(intensity_raw, (int, float)):
                intensity = max(0.0, min(1.0, float(intensity_raw) / 10.0))
            else:
                intensity = 0.35

            labels: list[str] = []
            if emotion:
                labels.append(emotion)

            atomic_facts = [
                "Legacy|source_table|memories",
                f"Legacy|scope|{scope}",
            ]
            if author:
                atomic_facts.append(f"Legacy|author|{author}")
            if source:
                atomic_facts.append(f"Legacy|source|{source}")
            for tag in tags[:8]:
                atomic_facts.append(f"Legacy|tag|{tag}")

            memcell = MemCell(
                id=f"legacy:memories:{source_id}",
                session_id=f"legacy/memories/{scope}",
                episodic_trace=content,
                atomic_facts=atomic_facts,
                emotional_tag=_build_emotional_tag(labels, intensity=intensity, valence=0.0),
                foresight_signals=[],
                timestamp=_as_utc(row.get("timestamp")),
                embedding=None,
                explicit_importance=0.4,
            )

            await self._upsert(repo, memcell)
            self.stats.memories += 1

        print(f"  migrated: {self.stats.memories}")

    async def _migrate_ai_diary(
        self, maria_conn: aiomysql.Connection, repo: PostgresSoulRepository
    ) -> None:
        print("\n[3/3] Migrating ai_diary ...")
        since = datetime.now() - timedelta(days=self.config.days)
        query = (
            "SELECT id, timestamp, content, user_message, interaction_summary, emotions, "
            "context_tags, interface, chat_id, thread_id "
            "FROM ai_diary "
            "WHERE timestamp >= %s "
            "ORDER BY timestamp ASC"
        )
        async with maria_conn.cursor() as cur:
            await cur.execute(query, (since,))
            rows = await cur.fetchall()

        for row in rows:
            if self._max_rows_reached():
                break

            content = _text(row.get("content"))
            if not content:
                self.stats.skipped += 1
                continue

            source_id = row.get("id")
            interface_name = _text(row.get("interface"))
            chat_id = _text(row.get("chat_id"))
            thread_id = _text(row.get("thread_id"))
            user_message = _text(row.get("user_message"))
            interaction_summary = _text(row.get("interaction_summary"))
            emotion_labels = _json_list(row.get("emotions"))
            context_tags = _json_list(row.get("context_tags"))

            if user_message:
                episodic_trace = f"User: {user_message}\nSynth: {content}"
            else:
                episodic_trace = content

            session_bits = [value for value in [interface_name, chat_id, thread_id] if value]
            session_suffix = "/".join(session_bits) if session_bits else "legacy"

            atomic_facts = [
                "Legacy|source_table|ai_diary",
            ]
            if interaction_summary:
                atomic_facts.append(f"Legacy|summary|{interaction_summary}")
            if interface_name:
                atomic_facts.append(f"Legacy|interface|{interface_name}")
            for tag in context_tags[:8]:
                atomic_facts.append(f"Legacy|context_tag|{tag}")

            default_intensity = 0.45 if emotion_labels else 0.2
            memcell = MemCell(
                id=f"legacy:ai_diary:{source_id}",
                session_id=f"legacy/ai_diary/{session_suffix}",
                episodic_trace=episodic_trace,
                atomic_facts=atomic_facts,
                emotional_tag=_build_emotional_tag(
                    emotion_labels,
                    intensity=default_intensity,
                    valence=0.0,
                ),
                foresight_signals=[],
                timestamp=_as_utc(row.get("timestamp")),
                embedding=None,
                explicit_importance=0.5,
            )

            await self._upsert(repo, memcell)
            self.stats.ai_diary += 1

        print(f"  migrated: {self.stats.ai_diary}")

    async def _upsert(self, repo: PostgresSoulRepository, memcell: MemCell) -> None:
        if self.config.dry_run:
            return
        await repo.upsert_memcell(memcell)

    def _max_rows_reached(self) -> bool:
        if self.config.max_rows is None:
            return False
        return self.stats.total >= self.config.max_rows

    def _print_header(self) -> None:
        mode = "DRY RUN" if self.config.dry_run else "WRITE"
        print("=" * 72)
        print("Legacy -> SOUL migration")
        print("=" * 72)
        print(f"Mode: {mode}")
        print(f"Days lookback: {self.config.days}")
        print(f"Batch size: {self.config.batch_size}")
        print(f"Sources: {', '.join(sorted(self.config.sources))}")
        print(f"MariaDB: {self.config.maria_user}@{self.config.maria_host}:{self.config.maria_port}/{self.config.maria_database}")
        print(f"SOUL Postgres DSN: {self.config.soul_postgres_dsn}")

    def _print_summary(self) -> None:
        print("\n" + "=" * 72)
        print("Migration summary")
        print("=" * 72)
        print(f"chat_history_cache: {self.stats.chat_history_cache}")
        print(f"memories: {self.stats.memories}")
        print(f"ai_diary: {self.stats.ai_diary}")
        print(f"skipped: {self.stats.skipped}")
        print(f"total migrated: {self.stats.total}")
        if self.config.dry_run:
            print("\nDry run completed. Re-run without --dry-run to write records.")


async def _main() -> None:
    args = parse_args()
    config = build_config(args)

    allowed_sources = {"chat_history_cache", "memories", "ai_diary"}
    unknown = config.sources - allowed_sources
    if unknown:
        raise SystemExit(f"Unsupported sources: {', '.join(sorted(unknown))}")

    migrator = LegacyToSoulMigrator(config)
    await migrator.run()


if __name__ == "__main__":
    asyncio.run(_main())
