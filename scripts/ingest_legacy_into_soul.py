#!/usr/bin/env python3
"""File the legacy diary and ``memories`` rows into SOUL (idempotent).

Reads ``ai_diary``, ``ai_diary_archive`` and ``memories`` from the application
database and files them through ``SoulService.ingest_summary``. Running it again
updates nothing that is already filed. Observer copies of chat lines are skipped.

Uses the same DB environment as the application (``DB_HOST`` ... ``DB_NAME``).

Examples:
    uv run python scripts/ingest_legacy_into_soul.py --dry-run
    uv run python scripts/ingest_legacy_into_soul.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db import get_conn_ctx  # noqa: E402
from core.soul.compiler import NoopEmbedder  # noqa: E402
from core.soul.fastembed_embedder import FastEmbedder  # noqa: E402
from core.soul.legacy_ingest import ingest_legacy  # noqa: E402
from core.soul.repository import PostgresSoulRepository  # noqa: E402
from core.soul.service import SoulService  # noqa: E402


def _build_embedder(allow_noop: bool) -> FastEmbedder | NoopEmbedder:
    try:
        return FastEmbedder()
    except Exception as exc:  # pragma: no cover - environment dependent
        if not allow_noop:
            raise SystemExit(
                f"FastEmbedder is unavailable ({exc}). Vectors from the hashed "
                "fallback live in a different space from the real embedder's, so "
                "refusing to write them; pass --allow-noop-embedder to override."
            )
        return NoopEmbedder()


async def _run(args: argparse.Namespace) -> int:
    from core.db import build_runtime_postgres_dsn

    repository = PostgresSoulRepository(dsn=build_runtime_postgres_dsn())
    service = SoulService(repository, _build_embedder(args.allow_noop_embedder))
    async with get_conn_ctx() as conn:
        async with conn.cursor() as cur:
            report = await ingest_legacy(
                service, cur, dry_run=args.dry_run, limit=args.limit
            )
    print(
        f"created={report.created} updated={report.updated} "
        f"unchanged={report.unchanged}\nskipped={report.skipped}\n"
        f"origins={report.by_origin}"
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--allow-noop-embedder", action="store_true")
    return asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
