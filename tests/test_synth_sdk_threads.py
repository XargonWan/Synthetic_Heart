"""The SDK from a worker thread: an extension runs blocking work with ``asyncio.to_thread`` and
logs from it. That used to raise "no current event loop" and hide the real failure."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from synth_sdk.runtime import Context


class RecordingPeer:
    def __init__(self) -> None:
        self.sent: list[tuple[str, Any]] = []

    async def notify(self, method: str, params: Any = None) -> None:
        self.sent.append((method, params))


@pytest.mark.asyncio
async def test_a_log_line_written_in_a_worker_thread_reaches_the_core(
    tmp_path: Path,
) -> None:
    peer = RecordingPeer()
    ctx = Context(peer, extension_id="dev/x", data_dir=tmp_path)  # type: ignore[arg-type]

    def work() -> None:
        ctx.log.error("from a thread")

    await asyncio.to_thread(work)
    for _ in range(100):
        if peer.sent:
            break
        await asyncio.sleep(0.01)
    assert peer.sent == [("log.write", {"level": "error", "message": "from a thread"})]


@pytest.mark.asyncio
async def test_a_log_line_written_in_the_loop_still_works(tmp_path: Path) -> None:
    peer = RecordingPeer()
    ctx = Context(peer, extension_id="dev/x", data_dir=tmp_path)  # type: ignore[arg-type]
    ctx.log.info("in the loop")
    for _ in range(100):
        if peer.sent:
            break
        await asyncio.sleep(0.01)
    assert peer.sent[0][0] == "log.write"


def test_logging_with_no_loop_at_all_is_silent(tmp_path: Path) -> None:
    ctx = Context(RecordingPeer(), extension_id="dev/x", data_dir=tmp_path)  # type: ignore[arg-type]
    ctx.log.info("nobody is listening")  # must not raise
