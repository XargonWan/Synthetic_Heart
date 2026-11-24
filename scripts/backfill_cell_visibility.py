#!/usr/bin/env python3
"""Mark MemCells that came from a Telegram private chat as ``private``.

``MemCell.visibility`` is stamped when a cell is compiled, from the chat kind the
interface reported. Cells written before that field existed are all ``shared``, so
what someone confided in a one-to-one chat could still be recalled in another
conversation. For Telegram the chat id in the session id says which it was
(positive = private chat, negative = group), the same rule
``core.history_engine.telegram_chat_kind`` applies to interface paths, so those cells
can be classified after the fact. Other interfaces carry no such signal in the
session id and are left as they are.

The preview is the default; ``--apply`` writes. Re-running is safe.

    uv run python scripts/backfill_cell_visibility.py
    uv run python scripts/backfill_cell_visibility.py --apply
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.db import build_runtime_postgres_dsn  # noqa: E402
from core.soul.repository import PostgresSoulRepository  # noqa: E402

# ``telegram_bot_<id>`` / ``telegram_<id>`` with a positive id and no topic suffix.
_PRIVATE_SESSION = r"^(telegram_bot|telegram)_[0-9]+$"


async def _run(apply: bool) -> int:
    repository = PostgresSoulRepository(dsn=build_runtime_postgres_dsn())
    pool = await repository._get_pool()
    try:
        candidates = await pool.fetchval(
            "SELECT COUNT(*) FROM mem_cells WHERE source_kind = 'chat' "
            "AND visibility = 'shared' AND session_id ~ $1",
            _PRIVATE_SESSION,
        )
        total = await pool.fetchval(
            "SELECT COUNT(*) FROM mem_cells WHERE source_kind = 'chat'"
        )
        print(f"{candidates} of {total} chat cells come from a Telegram private chat")
        if not apply:
            print("preview only; re-run with --apply to mark them private")
            return 0
        status = await pool.execute(
            "UPDATE mem_cells SET visibility = 'private' WHERE source_kind = 'chat' "
            "AND visibility = 'shared' AND session_id ~ $1",
            _PRIVATE_SESSION,
        )
        print(f"done: {status}")
        return 0
    finally:
        await repository.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    return asyncio.run(_run(parser.parse_args().apply))


if __name__ == "__main__":
    raise SystemExit(main())
