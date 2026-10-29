"""Plugin-local SQLite persistence, off the event loop and outside SOUL/diary."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path


def _decode_fields(raw) -> dict:
    """Corrupt or non-object payloads recover as empty, keeping the revision."""
    try:
        fields = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return fields if isinstance(fields, dict) else {}


class SceneStore:
    def __init__(self, path: Path | None = None):
        self.path = (
            path
            or Path(__file__).resolve().parents[2] / "data" / "scene_context.sqlite3"
        )

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=1)
        try:
            with conn:
                conn.execute("""CREATE TABLE IF NOT EXISTS scene_context (
                    interface_path TEXT PRIMARY KEY,
                    revision INTEGER NOT NULL,
                    fields_json TEXT NOT NULL
                )""")
                yield conn
        finally:
            conn.close()

    def _load(self, scope: str) -> dict:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT revision, fields_json FROM scene_context WHERE interface_path = ?",
                (scope,),
            ).fetchone()
        if not row:
            return {"revision": 0, "fields": {}}
        return {"revision": row[0], "fields": _decode_fields(row[1])}

    async def load(self, scope: str) -> dict:
        return await asyncio.to_thread(self._load, scope)

    def _save(self, scope: str, fields: dict, expected_revision: int) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT revision FROM scene_context WHERE interface_path = ?", (scope,)
            ).fetchone()
            current = row[0] if row else 0
            if current != expected_revision:
                return False
            conn.execute(
                "INSERT INTO scene_context (interface_path, revision, fields_json) VALUES (?, ?, ?) "
                "ON CONFLICT(interface_path) DO UPDATE SET revision=excluded.revision, fields_json=excluded.fields_json",
                (scope, current + 1, json.dumps(fields, ensure_ascii=False)),
            )
        return True

    async def save(self, scope: str, fields: dict, *, expected_revision: int) -> bool:
        return await asyncio.to_thread(self._save, scope, fields, expected_revision)
