"""Forwarding a log record to the LogChat must never feed itself.

Live (2026-11-12): the Telegram bot was kicked from the LogChat supergroup. The
first forwarded ERROR failed with ``Forbidden``; the failure lines the send path
logged ("Failed to send text chunks", "send_with_thread_fallback caught error")
matched none of the keywords that used to be the only guard, so each was
forwarded in turn, failed, and logged again - ~50 MB of log every few minutes and
an event loop too busy to answer anyone.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from types import SimpleNamespace
from typing import Any

import pytest

import core.config as config_module
from core import logging_utils


@pytest.fixture(autouse=True)
def _clean_budget() -> Any:
    logging_utils._logchat_forward_times.clear()
    yield
    logging_utils._logchat_forward_times.clear()


def test_rate_budget_is_spent_then_recovers_after_the_window() -> None:
    limit = logging_utils._LOGCHAT_MAX_FORWARDS
    assert all(logging_utils._logchat_rate_ok(now=100.0) for _ in range(limit))
    assert logging_utils._logchat_rate_ok(now=100.0) is False
    later = 100.0 + logging_utils._LOGCHAT_WINDOW_SECONDS + 1
    assert logging_utils._logchat_rate_ok(now=later) is True


def _wire_logchat(monkeypatch: pytest.MonkeyPatch, sent: list[str]) -> None:
    """Point the forwarder at a fake interface whose send logs its own failure."""

    class FailingInterface:
        async def send_message(self, data: dict[str, Any]) -> None:
            sent.append(data["text"])
            # What the real send path does when the bot was kicked: log the
            # failure, with wording no keyword guard knows.
            logging_utils._log(
                "ERROR", "[cortex_response_send] Failed to send text chunks: Forbidden"
            )
            await asyncio.sleep(0)

    monkeypatch.setattr(logging_utils, "setup_logging", lambda: logging.getLogger("t"))
    monkeypatch.setattr(config_module, "get_log_chat_id_sync", lambda: "-1001")
    monkeypatch.setattr(config_module, "get_log_chat_thread_id_sync", lambda: 239)
    monkeypatch.setattr(
        config_module, "get_log_chat_interface_sync", lambda: "telegram_bot"
    )
    monkeypatch.setitem(
        sys.modules,
        "core.core_initializer",
        SimpleNamespace(INTERFACE_REGISTRY={"telegram_bot": FailingInterface()}),
    )


def test_a_failing_logchat_send_is_not_forwarded_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[str] = []
    _wire_logchat(monkeypatch, sent)

    async def scenario() -> None:
        logging_utils._log("ERROR", "something broke")
        for _ in range(10):
            await asyncio.sleep(0)

    asyncio.run(scenario())

    # Exactly the original record; the failure it caused was only logged.
    assert sent == ["[ERROR] something broke"]


def test_ordinary_errors_are_still_forwarded(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[str] = []
    _wire_logchat(monkeypatch, sent)

    async def scenario() -> None:
        logging_utils._log("ERROR", "first")
        for _ in range(5):
            await asyncio.sleep(0)
        logging_utils._log("ERROR", "second")
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(scenario())

    # Each is forwarded once; the failure each causes is only logged.
    assert sent == ["[ERROR] first", "[ERROR] second"]
