import atexit
import copy
import logging
import os
import queue
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler, QueueHandler
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

# Try to load environment variables from .env early so logging defaults reflect .env
try:
    # load_dotenv is optional; don't crash if package is missing
    from dotenv import load_dotenv

    # Support both local development (cwd) and Docker (/app/.env)
    # The default find_dotenv() logic works well for local dev
    load_dotenv(override=False)
    # Explicitly check /app/.env for Docker if not found above or for extra safety
    load_dotenv(dotenv_path="/app/.env", override=False)
except Exception:
    pass


_logger: Optional[logging.Logger] = None

# Default to a "logs" directory inside the repository rather than /config
# so running the tests does not attempt to write to restricted locations.
_DEFAULT_LOG_DIR = os.path.join(os.getcwd(), "logs")
_LOG_DIR = os.getenv("LOG_DIR", _DEFAULT_LOG_DIR)
_LOG_FILE = os.path.join(_LOG_DIR, "synth.log")
# Additional ERROR-only log: a short, low-rotation companion to synth.log so a
# quick "what broke?" scan doesn't require wading through the full runtime log.
# This is ADDITIVE — the main synth.log still records everything.
_ERROR_LOG_FILE = os.path.join(_LOG_DIR, "synth_errors.log")
_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
}

# Global variables for logging configuration
_LOGGING_LEVEL = os.getenv(
    "LOGGING_LEVEL", "INFO"
).upper()  # Default to INFO or env value
_LOGGING_LOGCHAT_LEVEL = "ERROR"


# ── Non-blocking file logging ────────────────────────────────────────────────
# File handlers write synchronously on the thread that logs — which is the
# asyncio event loop. A log destination that stalls (a full disk, a slow network
# share) therefore freezes the WHOLE process: every component goes silent and an
# in-flight turn is lost mid-way with no error anywhere (observed live: 6m46s of
# zero output starting immediately after a prompt was built, the turn never
# reaching the engine). The file handlers are consequently owned by a background
# writer thread and fed through a bounded queue; a full queue DROPS records and
# counts them instead of blocking the caller. Losing log lines is acceptable,
# freezing the entity is not.
#
# Console output stays synchronous (cheap, and it must still work when the file
# destination is broken). Set LOG_QUEUE_ENABLED=0 to restore direct writes when
# debugging the logger itself.
_LOG_QUEUE_MAXLEN_DEFAULT = 5000

_log_queue: Optional["queue.Queue[logging.LogRecord]"] = None
_log_listener_thread: Optional[threading.Thread] = None
_log_listener_handlers: list[logging.Handler] = []
_log_queue_stop = threading.Event()
_log_queue_lock = threading.Lock()
_drop_lock = threading.Lock()
_dropped_records = 0


def _env_flag(name: str, default: bool) -> bool:
    """Read a boolean environment flag ('0'/'false'/'no'/'off' disable)."""
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _queue_logging_enabled() -> bool:
    """Whether file records are written through the background writer thread."""
    return _env_flag("LOG_QUEUE_ENABLED", True)


def _queue_maxlen() -> int:
    """Bounded size of the log queue; beyond it records are dropped."""
    try:
        return max(
            1, int(os.getenv("LOG_QUEUE_MAXLEN", str(_LOG_QUEUE_MAXLEN_DEFAULT)))
        )
    except Exception:
        return _LOG_QUEUE_MAXLEN_DEFAULT


def _note_dropped_records(count: int = 1) -> None:
    """Count records dropped because the queue was full."""
    global _dropped_records
    with _drop_lock:
        _dropped_records += count


def _take_dropped_records() -> int:
    """Consume and return the number of records dropped since the last call."""
    global _dropped_records
    with _drop_lock:
        dropped = _dropped_records
        _dropped_records = 0
    return dropped


class _DroppingQueueHandler(QueueHandler):
    """Queue handler that never blocks: a full queue drops the record.

    ``QueueHandler.enqueue`` raises ``queue.Full`` (via ``put_nowait``) rather
    than blocking, which is exactly the behaviour we want; here the drop is
    counted so the writer thread can report it once it catches up.
    """

    def prepare(self, record: logging.LogRecord) -> logging.LogRecord:
        # Shallow copy, deliberately left UNFORMATTED: the real file handlers
        # keep their own formatter, so a record is rendered exactly once, in the
        # writer thread (and its traceback survives to be rendered there).
        return copy.copy(record)

    def enqueue(self, record: logging.LogRecord) -> None:
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            _note_dropped_records()
        except Exception:
            # A logging failure must never surface into application code.
            pass


def _emit_through_listener_handlers(
    record: logging.LogRecord, handlers: list[logging.Handler]
) -> None:
    """Dispatch one record to the owned handlers, preserving level filtering."""
    for handler in handlers:
        try:
            # ``Handler.handle`` applies filters but NOT levels (that normally
            # happens in ``Logger.callHandlers``), so the per-handler level check
            # is replicated here to keep error-only handlers error-only.
            if record.levelno >= handler.level:
                handler.handle(record)
        except Exception:
            pass


def _report_dropped_records() -> None:
    """Write one warning about dropped records through the owned handlers."""
    dropped = _take_dropped_records()
    if not dropped:
        return
    _write_listener_notice(
        f"[logging_utils] Dropped {dropped} log record(s): the log destination "
        f"is slower than the process (queue full, "
        f"LOG_QUEUE_MAXLEN={_queue_maxlen()})"
    )


# A single write taking longer than this is reported: it is the fingerprint of
# the failure this module exists to survive (a stalled disk / network share).
_SLOW_DESTINATION_WARN_SEC = 5.0
_SLOW_DESTINATION_WARN_INTERVAL_SEC = 60.0
_last_slow_warn_monotonic = 0.0


def _write_listener_notice(message: str) -> None:
    """Emit a synthetic WARNING through the owned handlers."""
    try:
        notice = logging.LogRecord(
            name="synth.logging_utils",
            level=logging.WARNING,
            pathname=__file__,
            lineno=0,
            msg=message,
            args=None,
            exc_info=None,
        )
    except Exception:
        return
    _emit_through_listener_handlers(notice, list(_log_listener_handlers))


def _report_slow_write(elapsed: float) -> None:
    """Report a write that took abnormally long (rate-limited)."""
    global _last_slow_warn_monotonic
    if elapsed < _SLOW_DESTINATION_WARN_SEC:
        return
    now = time.monotonic()
    if now - _last_slow_warn_monotonic < _SLOW_DESTINATION_WARN_INTERVAL_SEC:
        return
    _last_slow_warn_monotonic = now
    _write_listener_notice(
        f"[logging_utils] Slow log destination: one write took {elapsed:.1f}s "
        f"(a stalled disk or network share; writes are off the event loop, so "
        f"only log lines are affected)"
    )


def _log_listener_loop() -> None:
    """Background writer: drain the queue into the owned file handlers."""
    log_queue = _log_queue
    if log_queue is None:
        return
    while True:
        if _log_queue_stop.is_set() and log_queue.empty():
            break
        try:
            record = log_queue.get(timeout=0.2)
        except queue.Empty:
            _report_dropped_records()
            continue
        started = time.monotonic()
        _emit_through_listener_handlers(record, list(_log_listener_handlers))
        _report_slow_write(time.monotonic() - started)
    # Final drain so a graceful shutdown does not lose buffered records.
    while True:
        try:
            record = log_queue.get_nowait()
        except queue.Empty:
            break
        _emit_through_listener_handlers(record, list(_log_listener_handlers))
    _report_dropped_records()


def _ensure_log_listener() -> None:
    """Create the queue and start the writer thread if needed."""
    global _log_queue, _log_listener_thread
    if _log_queue is None:
        _log_queue = queue.Queue(maxsize=_queue_maxlen())
    thread = _log_listener_thread
    if thread is None or not thread.is_alive():
        _log_queue_stop.clear()
        thread = threading.Thread(
            target=_log_listener_loop, name="synth-log-writer", daemon=True
        )
        _log_listener_thread = thread
        thread.start()


def _attach_queue_handler(
    target_logger: logging.Logger,
    file_handlers: list[logging.Handler],
    formatter: logging.Formatter,
) -> bool:
    """Route ``target_logger``'s file handlers through the writer thread.

    Returns True when the queue is in place, False when logging must stay
    synchronous (queue logging disabled, or the writer thread could not start).
    """
    if not file_handlers or not _queue_logging_enabled():
        return False
    try:
        _ensure_log_listener()
        log_queue = _log_queue
        if log_queue is None:
            return False
        with _log_queue_lock:
            for handler in file_handlers:
                if handler not in _log_listener_handlers:
                    _log_listener_handlers.append(handler)
        queue_handler = _DroppingQueueHandler(log_queue)
        queue_handler.setFormatter(formatter)
        # Never filter out a record a lower-level owned handler would keep.
        try:
            queue_handler.setLevel(min(h.level for h in file_handlers))
        except Exception:
            queue_handler.setLevel(logging.NOTSET)
        target_logger.addHandler(queue_handler)
        return True
    except Exception:
        return False


def _shutdown_log_queue(timeout: float = 5.0) -> None:
    """Flush and stop the writer thread (registered with ``atexit``)."""
    global _log_listener_thread
    thread = _log_listener_thread
    log_queue = _log_queue
    if thread is None or log_queue is None:
        return
    _log_queue_stop.set()
    try:
        thread.join(timeout=timeout)
    except Exception:
        pass
    for handler in list(_log_listener_handlers):
        try:
            handler.flush()
        except Exception:
            pass
    _log_listener_thread = None


def _drop_count_for_tests() -> int:
    """Expose the pending dropped-record count for tests."""
    return _take_dropped_records()


class TimeZoneFormatter(logging.Formatter):
    """Formatter that respects the configured timezone."""

    def __init__(self, fmt=None, datefmt=None):
        super().__init__(fmt, datefmt)
        try:
            tz_name = os.getenv("TZ", "UTC")
            self.tz = ZoneInfo(tz_name)
        except Exception:
            self.tz = timezone.utc

    def formatTime(self, record, datefmt=None):
        dt = datetime.fromtimestamp(record.created, timezone.utc)
        dt_local = dt.astimezone(self.tz)
        # Always include the UTC offset (e.g. "+0900") so every log line is
        # self-describing and directly comparable with UTC sources such as
        # Docker's `inspect .State.StartedAt`. If the caller-provided datefmt
        # already carries the offset (%z), respect it and don't double it.
        if datefmt:
            if "%z" in datefmt:
                return dt_local.strftime(datefmt)
            return dt_local.strftime(datefmt) + dt_local.strftime(" %z")
        return dt_local.strftime("%Y-%m-%d %H:%M:%S %z")


def _register_logging_config():
    """Register logging configuration with config_registry.

    This is called lazily to avoid circular imports.
    """
    global _LOGGING_LEVEL, _LOGGING_LOGCHAT_LEVEL

    try:
        # If environment variables are present, honor them first to avoid
        # connecting to DB during early startup (DB might not be available).
        env_level = os.getenv("LOGGING_LEVEL")
        if env_level:
            _LOGGING_LEVEL = env_level.upper()
        env_logchat = os.getenv("LOGGING_LOGCHAT_LEVEL")
        if env_logchat:
            _LOGGING_LOGCHAT_LEVEL = env_logchat.upper()

        # If env var was not set, fall back to config_registry defaults
        if not env_level or not env_logchat:
            from core.config_manager import config_registry

            def _update_logging_level(value: str | None) -> None:
                global _LOGGING_LEVEL
                _LOGGING_LEVEL = (value or "ERROR").upper()
                # Re-setup logging with new level
                if _logger:
                    _logger.setLevel(_LEVELS.get(_LOGGING_LEVEL, logging.ERROR))

            def _update_logchat_level(value: str | None) -> None:
                global _LOGGING_LOGCHAT_LEVEL
                _LOGGING_LOGCHAT_LEVEL = (value or "ERROR").upper()

            # Only query config_registry if needed (we didn't find env vars above)
            if not env_level:
                _LOGGING_LEVEL = config_registry.get_value(
                    "LOGGING_LEVEL",
                    "INFO",
                    label="Logging Level",
                    description="Minimum log level to record: DEBUG, INFO, WARNING, ERROR",
                    group="logging",
                    component="logging",
                    constraints={"choices": ["DEBUG", "INFO", "WARNING", "ERROR"]},
                    tags=["logs_only"],
                ).upper()
                config_registry.add_listener("LOGGING_LEVEL", _update_logging_level)

            if not env_logchat:
                _LOGGING_LOGCHAT_LEVEL = config_registry.get_value(
                    "LOGGING_LOGCHAT_LEVEL",
                    "ERROR",
                    label="LogChat Notification Level",
                    description="Send log notifications to LogChat (configure with /logchat command in your chat)",
                    group="logging",
                    component="logchat",
                    constraints={"choices": ["DEBUG", "INFO", "WARNING", "ERROR"]},
                    tags=["logs_only"],
                ).upper()
                config_registry.add_listener(
                    "LOGGING_LOGCHAT_LEVEL", _update_logchat_level
                )
    except ImportError:
        # If config_manager is not available yet, use defaults
        pass


class TimestampedRotatingFileHandler(RotatingFileHandler):
    """
    A file handler that rotates **daily** (at the first record of a new day),
    with a size / line count safety cap that produces intra-day shards.

    On rollover the active ``<stem>.log`` is renamed to a dated file following
    the shared archive naming scheme (see :mod:`core.log_archive`):

    * Daily rollover     -> ``<stem>.<YYYY-MM-DD>.log``
    * Intra-day size cap -> ``<stem>.<YYYY-MM-DD>.<N>.log``

    After every rollover it runs :func:`core.log_archive.enforce_retention` so
    older days are gzip-compressed and anything beyond the retention window is
    deleted. Retention also runs once at :func:`setup_logging`.

    Includes the 'Safe' logic for Windows permission errors (a locked file just
    keeps being written to rather than crashing).
    """

    def __init__(
        self,
        filename,
        maxBytes=0,
        backupCount=0,
        encoding=None,
        delay=False,
        maxLines=0,
        retentionDays=None,
    ):
        self.maxLines = maxLines
        self._line_count = None  # None indicates NOT INITIALIZED
        # The calendar day the current active file belongs to; used to trigger
        # the daily rollover lazily on the first record of a new day.
        self._current_day = self._file_day(filename)
        self.retentionDays = (
            retentionDays
            if retentionDays is not None
            else int(os.getenv("LOG_RETENTION_DAYS", "7"))
        )
        super().__init__(
            filename,
            mode="a",
            maxBytes=maxBytes,
            backupCount=backupCount,
            encoding=encoding,
            delay=delay,
        )

    @staticmethod
    def _file_day(filename):
        """Return the calendar day the existing active file was last written."""
        try:
            if os.path.exists(filename):
                mtime = os.path.getmtime(filename)
                return datetime.fromtimestamp(mtime).date()
        except Exception:
            pass
        return datetime.now().date()

    def _count_lines(self):
        """Count actual lines in baseFilename using buffered reading."""
        if not os.path.exists(self.baseFilename):
            return 0
        try:
            with open(self.baseFilename, "rb") as f:
                count = 0
                buf_size = 1024 * 1024
                buf = f.read(buf_size)
                while buf:
                    count += buf.count(b"\n")
                    buf = f.read(buf_size)
                return count
        except Exception:
            return 0

    def shouldRollover(self, record):
        """Rollover on a new calendar day, or when the size/line cap is hit."""
        if self.stream is None:
            self.stream = self._open()

        # Primary trigger: a new day has started.
        try:
            record_day = datetime.fromtimestamp(record.created).date()
        except Exception:
            record_day = datetime.now().date()
        if self._current_day is not None and record_day != self._current_day:
            return 1

        # Initialize line count lazily
        if self.maxLines > 0 and self._line_count is None:
            self._line_count = self._count_lines()

        # Safety cap: bytes
        if self.maxBytes > 0:
            msg = "%s\n" % self.format(record)
            self.stream.seek(0, 2)  # strict append
            if self.stream.tell() + len(msg) >= self.maxBytes:
                return 1

        # Safety cap: lines
        if self.maxLines > 0:
            msg = self.format(record)
            # Standard logging adds one newline per record
            msg_lines = msg.count("\n") + 1
            if (self._line_count or 0) + msg_lines >= self.maxLines:
                return 1

        return 0

    def emit(self, record):
        """Emit a record and update line count tracker."""
        super().emit(record)
        if self.maxLines > 0 and self._line_count is not None:
            try:
                msg = self.format(record)
                self._line_count += msg.count("\n") + 1
            except Exception:
                pass

    def _rotated_target(self, day):
        """Build a collision-free dated target name for *day*.

        Uses the shared naming scheme ``<stem>.<day>[.<N>].log``. The shard
        index increments only when a same-day file already exists (i.e. the
        size/line cap fired within the same calendar day).
        """
        from core import log_archive

        directory = os.path.dirname(self.baseFilename)
        stem = os.path.basename(self.baseFilename)
        if stem.endswith(".log"):
            stem = stem[: -len(".log")]

        shard = 0
        while True:
            candidate = os.path.join(
                directory, log_archive.dated_name(stem, day, shard)
            )
            if not os.path.exists(candidate):
                return candidate
            shard += 1

    def doRollover(self):
        """Perform the rollover (daily / size cap) and enforce retention."""
        if self.stream:
            try:
                self.stream.close()
                setattr(self, "stream", None)
            except Exception:
                pass

        # The dated file inherits the day the closed content belongs to.
        rollover_day = self._current_day or datetime.now().date()
        new_name = self._rotated_target(rollover_day)

        try:
            if os.path.exists(self.baseFilename):
                os.rename(self.baseFilename, new_name)
        except (PermissionError, OSError):
            # On Windows the file may be locked (e.g. by 'tail'); keep writing
            # to the original rather than crashing.
            pass

        # Advance the active day to now and reset the line counter.
        self._current_day = datetime.now().date()
        if self.maxLines > 0:
            self._line_count = 0

        # Compress old days + delete beyond the retention window. Best-effort.
        try:
            from core import log_archive

            log_archive.enforce_retention(
                Path(os.path.dirname(self.baseFilename)),
                retention_days=self.retentionDays,
            )
        except Exception:
            pass

        if not self.delay:
            self.stream = self._open()


class _SafeConsoleStreamHandler(logging.StreamHandler):
    """Stream handler that never raises on non-ASCII log lines.

    When SyntH runs directly on a Windows host, ``sys.stdout`` inherits the
    terminal's cp1252 encoding; logging a line containing emoji, ✓, etc.
    then raises ``UnicodeEncodeError`` inside ``emit``, which the logging
    machinery reports as a multi-line ``--- Logging error ---`` traceback
    for every such line. This handler re-encodes with ``errors="replace"``
    (also on the stream's own encoding) so non-ASCII degrades to ``?`` on
    the console instead of crashing the handler. File handlers are
    unaffected — they already use ``encoding="utf-8"``. The Linux container
    (UTF-8) never hits the fallback path.
    """

    def emit(self, record) -> None:
        try:
            msg = self.format(record)
            stream = self.stream
            stream.write(msg + self.terminator)
            self.flush()
        except UnicodeEncodeError:
            # Re-encode the formatted line with replacement characters using
            # the stream's declared encoding (cp1252 on Windows hosts).
            try:
                encoding = getattr(stream, "encoding", None) or "utf-8"
                safe = (
                    (msg + self.terminator)
                    .encode(encoding, errors="replace")
                    .decode(encoding, errors="replace")
                )
                stream.write(safe)
                self.flush()
            except Exception:
                self.handleError(record)
        except Exception:
            self.handleError(record)


def _write_to_separate_log(level: str, message: str, log_file: str) -> None:
    """Write log message to a separate log file.

    Args:
        level: Log level string
        message: Message to log
        log_file: Log file name without extension (e.g. 'webui' for logs/webui.log)
    """
    try:
        separate_log_path = os.path.join(_LOG_DIR, f"{log_file}.log")

        # Crea logger separato per questo file se non esiste
        logger_name = f"synth_{log_file}"
        separate_logger = logging.getLogger(logger_name)

        # Setup solo se non ha già handler
        if not separate_logger.handlers:
            separate_logger.setLevel(_LEVELS.get(_LOGGING_LEVEL, logging.ERROR))
            separate_logger.propagate = False

            formatter = TimeZoneFormatter(
                "[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s",
                "%Y-%m-%d %H:%M:%S",
            )

            # Daily rotation with a size safety cap (see log_archive naming).
            # Old days are gzip-compressed and pruned by enforce_retention.
            from core import log_archive

            fh = TimestampedRotatingFileHandler(
                separate_log_path,
                maxBytes=log_archive.DEFAULT_MAX_BYTES,
                maxLines=log_archive.DEFAULT_MAX_LINES,
                backupCount=0,
                encoding="utf-8",
            )
            fh.setFormatter(formatter)
            # Same non-blocking treatment as the main log: a stalled separate
            # log file must not block the caller either.
            if not _attach_queue_handler(separate_logger, [fh], formatter):
                separate_logger.addHandler(fh)

        # Check if we need to replace legacy handlers (if strictly needed)
        pass

        try:
            separate_logger.log(
                _LEVELS.get(level.upper(), logging.INFO), message, stacklevel=4
            )
        except Exception:
            pass
    except Exception:
        # Silent failure - non bloccare il logging principale
        pass


def setup_logging() -> logging.Logger:
    """Initialize the logger once and return it."""
    global _logger
    if _logger:
        return _logger

    # Register config if not already done
    if _LOGGING_LEVEL == "ERROR" and _LOGGING_LOGCHAT_LEVEL == "ERROR":
        _register_logging_config()

    os.makedirs(_LOG_DIR, exist_ok=True)

    logger = logging.getLogger("synth")
    logger.setLevel(_LEVELS.get(_LOGGING_LEVEL, logging.ERROR))
    logger.propagate = False

    if not logger.handlers:
        formatter = TimeZoneFormatter(
            "[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s",
            "%Y-%m-%d %H:%M:%S",
        )
        # Always add a stream handler so logs are available on stdout/stderr
        ch = _SafeConsoleStreamHandler(sys.stdout)
        ch.setFormatter(formatter)
        logger.addHandler(ch)

        # Try to add file handlers. If they can't be created due to permission
        # errors or other IO problems, fallback to stream logging so the
        # application can still start and emit useful logs.
        # Daily rotation; retention/compression handled by log_archive.
        file_handlers: list[logging.Handler] = []
        try:
            from core import log_archive

            fh = TimestampedRotatingFileHandler(
                _LOG_FILE,
                maxBytes=log_archive.DEFAULT_MAX_BYTES,
                maxLines=log_archive.DEFAULT_MAX_LINES,
                backupCount=0,
                encoding="utf-8",
            )
            fh.setFormatter(formatter)
            file_handlers.append(fh)
        except Exception as e:  # pragma: no cover - environment dependent
            # If file handler fails, write a warning to stdout via stream handler
            try:
                ch.stream.write(
                    f"[logging_utils] Could not open log file '{_LOG_FILE}': {e}. Falling back to stdout\n"
                )
            except Exception:
                # As a last resort print to stdout directly
                print(
                    f"[logging_utils] Could not open log file '{_LOG_FILE}': {e}. Falling back to stdout",
                    file=sys.stderr,
                )

        # Additional ERROR-only log file: short, low rotation, easy to scan.
        # Additive to synth.log (which keeps everything). Best-effort — a
        # failure here must never block startup or the main log handlers.
        try:
            error_fh = TimestampedRotatingFileHandler(
                _ERROR_LOG_FILE,
                maxBytes=log_archive.DEFAULT_MAX_BYTES,
                maxLines=log_archive.DEFAULT_MAX_LINES,
                backupCount=0,
                encoding="utf-8",
            )
            error_fh.setLevel(logging.ERROR)
            error_fh.setFormatter(formatter)
            file_handlers.append(error_fh)
        except Exception as e:  # pragma: no cover - environment dependent
            try:
                ch.stream.write(
                    f"[logging_utils] Could not open error log file '{_ERROR_LOG_FILE}': {e}\n"
                )
            except Exception:
                pass

        # Hand the file handlers to the background writer thread so a stalled or
        # full log destination (a network share, a full disk) can never block the
        # calling thread — which in this application is the asyncio event loop.
        # Direct attachment stays the fallback when queue logging is disabled or
        # the writer thread cannot start.
        if _attach_queue_handler(logger, file_handlers, formatter):
            atexit.register(_shutdown_log_queue)
        else:
            for handler in file_handlers:
                logger.addHandler(handler)

    # Compress old days + prune beyond the retention window at startup, so a
    # freshly-started process immediately reflects the retention policy even if
    # it never rolls over during its lifetime. Best-effort.
    try:
        from core import log_archive

        log_archive.enforce_retention(
            Path(_LOG_DIR),
            retention_days=int(os.getenv("LOG_RETENTION_DAYS", "7")),
        )
    except Exception:
        pass

    _logger = logger

    # Suppress the recurring CryptoError noise from discord-ext-voice-recv.
    # Discord periodically sends RTCP Payload-Specific Feedback (PSFB) packets
    # (second byte 0xcd = 205) that the library tries to decrypt but can't —
    # it's a known upstream limitation.  The library already drops these packets
    # silently (returns on line 151 of reader.py), so the ERROR log is false
    # noise.  Downgrade it to DEBUG so the main log stays clean.
    try:
        _voice_recv_reader_logger = logging.getLogger("discord.ext.voice_recv.reader")

        class _CryptoErrorFilter(logging.Filter):
            def filter(self, record: logging.LogRecord) -> bool:
                # Drop the ERROR "CryptoError decoding packet data" line and its
                # accompanying DEBUG detail line — both are noise from RTCP PSFB.
                msg = record.getMessage()
                if "CryptoError" in msg:
                    return False
                return True

        _voice_recv_reader_logger.addFilter(_CryptoErrorFilter())
    except Exception:
        pass  # Never block startup over a log filter

    # Log effective logging configuration at startup
    try:
        logger.log(
            _LEVELS.get(_LOGGING_LEVEL, logging.INFO),
            f"[logging_utils] Started synth logger with level={_LOGGING_LEVEL}, "
            f"log_file={_LOG_FILE}, error_log_file={_ERROR_LOG_FILE}",
        )
    except Exception:
        pass
    return logger


def _log(
    level: str,
    message: str,
    exc: Optional[Exception] = None,
    log_file: Optional[str] = None,
) -> None:
    """Log a message to the specified log file or default synth.log.

    Args:
        level: Log level (DEBUG, INFO, WARNING, ERROR)
        message: Message to log
        exc: Optional exception to include
        log_file: Optional log file name (without extension), e.g. 'webui' -> logs/webui.log
              If specified, writes ONLY to that file, not to synth.log
    """
    level = level.upper()
    if exc is not None:
        message = f"{message}\n{''.join(traceback.format_exception(exc))}".rstrip()

    # Se specificato un log_file separato, scrivi SOLO lì
    if log_file:
        _write_to_separate_log(level, message, log_file)
    else:
        # Altrimenti scrivi nel log principale
        logger = setup_logging()
        logger.log(_LEVELS.get(level, logging.INFO), message, stacklevel=3)

    # Skip notification for interface errors and transport errors to avoid recursion
    if (
        "Failed to send message" in message
        or "Unknown channel" in message
        or "interface" in message.lower()
        or "transport" in message
    ):
        return

    # Check if this level should trigger notifications
    logchat_threshold = _LEVELS.get(_LOGGING_LOGCHAT_LEVEL, logging.ERROR)
    current_level = _LEVELS.get(level, logging.INFO)

    if current_level >= logchat_threshold:
        try:
            from core.config import (
                get_log_chat_id_sync,
                get_log_chat_thread_id_sync,
                get_log_chat_interface_sync,
            )
            from core.core_initializer import INTERFACE_REGISTRY
            import asyncio

            notification_message = f"[{level}] {message}"

            # Try LogChat first - use the specific interface saved in DB
            log_chat_id = get_log_chat_id_sync()
            log_chat_interface = get_log_chat_interface_sync()

            if (
                log_chat_id
                and log_chat_interface
                and log_chat_interface in INTERFACE_REGISTRY
            ):
                iface = INTERFACE_REGISTRY.get(log_chat_interface)
                if iface and hasattr(iface, "send_message"):

                    async def send_to_logchat():
                        try:
                            message_data = {
                                "text": notification_message,
                                "target": log_chat_id,
                                # LogChat notifications are diagnostic output, not
                                # Synth utterances: they must NEVER be stored in
                                # chat history, or every ERROR/WARNING pollutes the
                                # LLM context of that interface_path and degrades
                                # (dumbs down) every subsequent turn there.
                                "skip_history": True,
                            }
                            thread_id = get_log_chat_thread_id_sync()
                            if thread_id:
                                message_data["thread_id"] = thread_id
                            await iface.send_message(message_data)
                        except Exception:
                            pass  # No fallback to trainer to avoid spam

                    try:
                        loop = asyncio.get_running_loop()
                        if loop and loop.is_running():
                            loop.create_task(send_to_logchat())
                        else:
                            try:
                                loop = asyncio.get_event_loop()
                                if not loop.is_closed():
                                    loop.run_until_complete(send_to_logchat())
                                # If no running loop, just skip async send in logging context
                            except RuntimeError:
                                pass  # No event loop available in this context
                    except RuntimeError:
                        pass  # No event loop available in this context
                    return

            # No fallback to trainer here to prevent error spam. Configure LogChat if needed.

        except Exception:
            # Silent failure - no recursive logging
            pass


def log_debug(msg: str, log_file: Optional[str] = None) -> None:
    """Log debug message.

    Args:
        msg: Message to log
        log_file: Optional separate log file name (without extension)
    """
    _log("DEBUG", msg, log_file=log_file)


def log_info(msg: str, log_file: Optional[str] = None) -> None:
    """Log info message.

    Args:
        msg: Message to log
        log_file: Optional separate log file name (without extension)
    """
    _log("INFO", msg, log_file=log_file)


def log_warning(msg: str, log_file: Optional[str] = None) -> None:
    """Log warning message.

    Args:
        msg: Message to log
        log_file: Optional separate log file name (without extension)
    """
    _log("WARNING", msg, log_file=log_file)


def log_error(
    msg: str, exc: Optional[Exception] = None, log_file: Optional[str] = None
) -> None:
    """Log error message.

    Args:
        msg: Message to log
        exc: Optional exception to include
        log_file: Optional separate log file name (without extension)
    """
    _log("ERROR", msg, exc, log_file=log_file)


# Initialize logging immediately when this module is imported
# This ensures the log file is created even if setup_logging() is called late
_register_logging_config()
setup_logging()
