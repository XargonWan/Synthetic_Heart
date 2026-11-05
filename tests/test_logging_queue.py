"""Tests for non-blocking (queue-based) file logging.

A log destination that stalls — a full disk, a slow network share — must never
block the thread that logs, because in this application that thread is the
asyncio event loop: a blocked ``emit`` freezes the whole entity and loses the
in-flight turn. These tests pin that behaviour: the caller never blocks, drops
are counted and reported, records still reach the file with the same formatting,
error-only handlers stay error-only, and a graceful shutdown drains the queue.
"""

from __future__ import annotations

import logging
import os
import queue
import time
from pathlib import Path

import pytest

from core import logging_utils


def _wait_for(predicate, timeout: float = 5.0, interval: float = 0.02) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class _StalledHandler(logging.Handler):
    """Handler that simulates a stuck log destination (never completes)."""

    def __init__(self, delay: float = 0.2):
        super().__init__()
        self.delay = delay
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        time.sleep(self.delay)
        self.records.append(record)


@pytest.fixture(autouse=True)
def _clean_logging_state():
    """Isolate the module-level queue machinery between tests."""
    saved = (
        logging_utils._log_queue,
        logging_utils._log_listener_thread,
        list(logging_utils._log_listener_handlers),
        logging_utils._logger,
    )
    logging_utils._log_queue = None
    logging_utils._log_listener_thread = None
    logging_utils._log_listener_handlers = []
    logging_utils._log_queue_stop.clear()
    logging_utils._take_dropped_records()
    yield
    logging_utils._log_queue_stop.set()
    thread = logging_utils._log_listener_thread
    if thread is not None and thread.is_alive():
        thread.join(timeout=5.0)
    (
        logging_utils._log_queue,
        logging_utils._log_listener_thread,
        handlers,
        logging_utils._logger,
    ) = saved
    logging_utils._log_listener_handlers = handlers
    logging_utils._log_queue_stop.clear()
    logging_utils._take_dropped_records()


def _make_record(msg: str = "hello", level: int = logging.INFO) -> logging.LogRecord:
    return logging.LogRecord(
        name="test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=None,
        exc_info=None,
    )


# ── The core guarantee: emit never blocks ───────────────────────────────────


def test_emit_never_blocks_when_queue_is_full():
    """A full queue drops records instead of waiting for the consumer.

    This is the whole point of the change: with the previous synchronous file
    handler, a stalled destination blocked the caller forever.
    """
    stalled_queue: queue.Queue = queue.Queue(maxsize=2)
    handler = logging_utils._DroppingQueueHandler(stalled_queue)

    start = time.monotonic()
    for i in range(50):
        handler.emit(_make_record(f"line {i}"))
    elapsed = time.monotonic() - start

    assert elapsed < 1.0, f"emit blocked for {elapsed:.2f}s with a full queue"
    assert stalled_queue.qsize() == 2
    assert logging_utils._take_dropped_records() == 48


def test_dropped_records_are_reported_once_the_writer_catches_up(tmp_path: Path):
    """Drops are not silent: the writer thread reports the loss."""
    log_path = tmp_path / "drops.log"
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(
        logging_utils.TimeZoneFormatter(
            "[%(levelname)s] [%(filename)s:%(lineno)d] %(message)s",
            "%Y-%m-%d %H:%M:%S",
        )
    )

    logging_utils._log_queue = queue.Queue(maxsize=2)
    logging_utils._log_listener_handlers = [file_handler]

    stalled_queue = logging_utils._log_queue
    handler = logging_utils._DroppingQueueHandler(stalled_queue)
    # Fill beyond the cap before the writer thread exists: these are the drops.
    for i in range(10):
        handler.emit(_make_record(f"line {i}"))

    logging_utils._ensure_log_listener()

    assert _wait_for(
        lambda: "Dropped 8 log record(s)" in log_path.read_text(encoding="utf-8")
    ), log_path.read_text(encoding="utf-8")


# ── Records still arrive, with the same formatting ──────────────────────────


def test_records_reach_the_file_through_the_writer_thread(tmp_path: Path):
    log_path = tmp_path / "queued.log"
    logger = logging.getLogger("test_queued_writer")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.handlers.clear()

    formatter = logging_utils.TimeZoneFormatter(
        "[%(levelname)s] [%(filename)s:%(lineno)d] %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    try:
        assert logging_utils._attach_queue_handler(logger, [file_handler], formatter)
        logger.info("first line")
        logger.warning("second line")

        assert _wait_for(
            lambda: "second line" in log_path.read_text(encoding="utf-8")
        ), log_path.read_text(encoding="utf-8")
        content = log_path.read_text(encoding="utf-8")

        assert "first line" in content
        # The formatter is applied exactly once (no doubled prefix).
        assert content.count("test_logging_queue.py") == 2
        assert content.count("[INFO]") == 1
        assert content.count("[WARNING]") == 1
    finally:
        logger.handlers.clear()


def test_error_only_handler_stays_error_only(tmp_path: Path):
    """Level filtering survives the queue: error files stay error-only."""
    all_path = tmp_path / "all.log"
    errors_path = tmp_path / "errors.log"

    logger = logging.getLogger("test_level_filter")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.handlers.clear()

    formatter = logging_utils.TimeZoneFormatter(
        "[%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    all_handler = logging.FileHandler(all_path, encoding="utf-8")
    all_handler.setFormatter(formatter)
    error_handler = logging.FileHandler(errors_path, encoding="utf-8")
    error_handler.setLevel(logging.ERROR)
    error_handler.setFormatter(formatter)

    try:
        assert logging_utils._attach_queue_handler(
            logger, [all_handler, error_handler], formatter
        )
        logger.info("informational")
        logger.error("broken")

        assert _wait_for(lambda: "broken" in errors_path.read_text(encoding="utf-8")), (
            errors_path.read_text(encoding="utf-8")
        )
        errors_content = errors_path.read_text(encoding="utf-8")

        assert "informational" not in errors_content
        assert "informational" in all_path.read_text(encoding="utf-8")
    finally:
        logger.handlers.clear()


# ── Escape hatch and shutdown ───────────────────────────────────────────────


def test_disabled_queue_attaches_handlers_directly(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOG_QUEUE_ENABLED", "0")
    log_path = tmp_path / "direct.log"
    logger = logging.getLogger("test_queue_disabled")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.handlers.clear()

    formatter = logging_utils.TimeZoneFormatter("%(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    try:
        assert (
            logging_utils._attach_queue_handler(logger, [file_handler], formatter)
            is False
        )
        assert logging_utils._log_listener_thread is None
        assert logging_utils._attach_queue_handler(logger, [], formatter) is False
    finally:
        logger.handlers.clear()
        file_handler.close()


def test_shutdown_drains_pending_records(tmp_path: Path):
    log_path = tmp_path / "drain.log"
    logger = logging.getLogger("test_drain")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.handlers.clear()

    formatter = logging_utils.TimeZoneFormatter("%(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)

    try:
        assert logging_utils._attach_queue_handler(logger, [file_handler], formatter)
        for i in range(25):
            logger.info("drained %d", i)

        logging_utils._shutdown_log_queue()

        content = log_path.read_text(encoding="utf-8")
        for i in range(25):
            assert f"drained {i}" in content
    finally:
        logger.handlers.clear()


def test_setup_logging_uses_queue_by_default(tmp_path: Path, monkeypatch):
    """setup_logging wires file handlers through the writer, not the caller."""
    monkeypatch.delenv("LOG_QUEUE_ENABLED", raising=False)
    monkeypatch.setattr(logging_utils, "_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(logging_utils, "_LOG_FILE", str(tmp_path / "synth.log"))
    monkeypatch.setattr(
        logging_utils, "_ERROR_LOG_FILE", str(tmp_path / "synth_errors.log")
    )
    monkeypatch.setattr(logging_utils, "_logger", None)
    monkeypatch.setenv("LOG_RETENTION_DAYS", "1")

    # The "synth" logger is a process-wide singleton: reset its handlers so the
    # setup path actually runs (an already-configured logger is returned as-is).
    synth_logger = logging.getLogger("synth")
    saved_handlers = list(synth_logger.handlers)
    synth_logger.handlers.clear()

    logger = None
    try:
        logger = logging_utils.setup_logging()
        assert any(
            isinstance(h, logging_utils._DroppingQueueHandler) for h in logger.handlers
        ), [type(h).__name__ for h in logger.handlers]
        assert not any(
            isinstance(h, logging_utils.TimestampedRotatingFileHandler)
            for h in logger.handlers
        ), "file handlers must be owned by the writer thread"

        logging_utils.log_info("queued startup line")
        logging_utils._shutdown_log_queue()

        assert "queued startup line" in (tmp_path / "synth.log").read_text(
            encoding="utf-8"
        )
    finally:
        if logger is not None:
            logger.handlers.clear()
        synth_logger.handlers.extend(saved_handlers)
        logging_utils._logger = None


def test_setup_logging_falls_back_to_direct_handlers(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("LOG_QUEUE_ENABLED", "0")
    monkeypatch.setattr(logging_utils, "_LOG_DIR", str(tmp_path))
    monkeypatch.setattr(logging_utils, "_LOG_FILE", str(tmp_path / "synth.log"))
    monkeypatch.setattr(
        logging_utils, "_ERROR_LOG_FILE", str(tmp_path / "synth_errors.log")
    )
    monkeypatch.setattr(logging_utils, "_logger", None)

    synth_logger = logging.getLogger("synth")
    saved_handlers = list(synth_logger.handlers)
    synth_logger.handlers.clear()

    logger = None
    try:
        logger = logging_utils.setup_logging()
        assert any(
            isinstance(h, logging_utils.TimestampedRotatingFileHandler)
            for h in logger.handlers
        )
        assert not any(
            isinstance(h, logging_utils._DroppingQueueHandler) for h in logger.handlers
        )
        logging_utils.log_info("direct startup line")
        for handler in logger.handlers:
            handler.flush()
        assert "direct startup line" in (tmp_path / "synth.log").read_text(
            encoding="utf-8"
        )
    finally:
        if logger is not None:
            logger.handlers.clear()
        synth_logger.handlers.extend(saved_handlers)
        logging_utils._logger = None


def test_queue_maxlen_env_is_honoured(monkeypatch):
    monkeypatch.setenv("LOG_QUEUE_MAXLEN", "7")
    assert logging_utils._queue_maxlen() == 7
    monkeypatch.setenv("LOG_QUEUE_MAXLEN", "nonsense")
    assert logging_utils._queue_maxlen() == logging_utils._LOG_QUEUE_MAXLEN_DEFAULT
    monkeypatch.delenv("LOG_QUEUE_MAXLEN", raising=False)
    assert logging_utils._queue_maxlen() == logging_utils._LOG_QUEUE_MAXLEN_DEFAULT


def test_slow_destination_is_reported(tmp_path: Path, monkeypatch):
    """A write that stalls long enough is reported (the incident's fingerprint)."""
    log_path = tmp_path / "slow.log"
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging_utils.TimeZoneFormatter("%(message)s", "%s"))

    monkeypatch.setattr(logging_utils, "_SLOW_DESTINATION_WARN_SEC", 0.05)
    monkeypatch.setattr(logging_utils, "_SLOW_DESTINATION_WARN_INTERVAL_SEC", 0.0)
    monkeypatch.setattr(logging_utils, "_last_slow_warn_monotonic", 0.0)

    logging_utils._log_queue = queue.Queue(maxsize=10)
    logging_utils._log_listener_handlers = [file_handler]
    logging_utils._ensure_log_listener()

    class _SlowHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            time.sleep(0.2)
            file_handler.emit(record)

    slow = _SlowHandler()
    logging_utils._log_listener_handlers = [slow]

    logging_utils._log_queue.put_nowait(_make_record("trigger"))

    assert _wait_for(
        lambda: "Slow log destination" in log_path.read_text(encoding="utf-8")
    ), log_path.read_text(encoding="utf-8")


def test_separate_log_writes_are_queued(tmp_path: Path, monkeypatch):
    """Per-file logs (webui, live_api, …) are non-blocking too."""
    monkeypatch.delenv("LOG_QUEUE_ENABLED", raising=False)
    monkeypatch.setattr(logging_utils, "_LOG_DIR", str(tmp_path))

    logging_utils._write_to_separate_log("INFO", "separate line", "probe_log")

    separate_logger = logging.getLogger("synth_probe_log")
    assert any(
        isinstance(h, logging_utils._DroppingQueueHandler)
        for h in separate_logger.handlers
    )
    try:
        assert _wait_for(
            lambda: (
                "separate line"
                in (tmp_path / "probe_log.log").read_text(encoding="utf-8")
            )
        ), (tmp_path / "probe_log.log").read_text(encoding="utf-8")
    finally:
        for handler in list(separate_logger.handlers):
            if isinstance(handler, logging_utils._DroppingQueueHandler):
                separate_logger.removeHandler(handler)
        # Leave no OS file handle open on the shared writer list.
        logging_utils._log_listener_handlers = [
            h
            for h in logging_utils._log_listener_handlers
            if not isinstance(h, logging.FileHandler)
            or os.path.dirname(getattr(h, "baseFilename", "")) != str(tmp_path)
        ]
