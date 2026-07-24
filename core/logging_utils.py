import logging
import os
import sys
import traceback
import glob
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
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
        if datefmt:
            return dt_local.strftime(datefmt)
        return dt_local.strftime("%Y-%m-%d %H:%M:%S")


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
    A file handler that rotates based on size, but renames the existing log
    with a timestamp instead of shifting indices (log.1 -> log.2).
    This avoids the O(N) renaming cost of standard RotatingFileHandler,
    which usually causes "chugging" with high backup counts.

    It also includes the 'Safe' logic for Windows permission errors.
    """

    def __init__(
        self,
        filename,
        maxBytes=0,
        backupCount=0,
        encoding=None,
        delay=False,
        maxLines=0,
    ):
        self.maxLines = maxLines
        self._line_count = None  # None indicates NOT INITIALIZED
        super().__init__(
            filename,
            mode="a",
            maxBytes=maxBytes,
            backupCount=backupCount,
            encoding=encoding,
            delay=delay,
        )

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
        """Determine if rollover should occur based on size or line count."""
        if self.stream is None:
            self.stream = self._open()

        # Initialize line count lazily
        if self.maxLines > 0 and self._line_count is None:
            self._line_count = self._count_lines()

        # Check bytes (safety fallback)
        if self.maxBytes > 0:
            msg = "%s\n" % self.format(record)
            self.stream.seek(0, 2)  # strict append
            if self.stream.tell() + len(msg) >= self.maxBytes:
                return 1

        # Check lines
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

    def doRollover(self):
        """Perform the rollover."""
        if self.stream:
            try:
                self.stream.close()
                setattr(self, "stream", None)
            except Exception:
                pass

        # Construct new filename with timestamp
        # Format: filename.YYYY-MM-DD_HH-MM-SS.log
        t = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        base, ext = os.path.splitext(self.baseFilename)
        new_name = f"{base}.{t}{ext}"

        # Avoid collision (highly unlikely with second precision)
        if os.path.exists(new_name):
            i = 1
            while os.path.exists(f"{base}.{t}_{i}{ext}"):
                i += 1
            new_name = f"{base}.{t}_{i}{ext}"

        try:
            os.rename(self.baseFilename, new_name)
        except (PermissionError, OSError):
            # On Windows, renaming might fail if file is locked (e.g. by 'tail').
            # In this case, we just reopen the original file and keep writing.
            # It will exceed maxBytes, but better than crashing.
            pass

        # Cleanup: remove old timestamped backups beyond `backupCount` to
        # avoid unbounded accumulation of rotated files. Sort by mtime and
        # delete the oldest files while keeping the most recent `backupCount`.
        try:
            if getattr(self, "backupCount", 0) > 0:
                pattern = f"{base}.*{ext}"
                rotated = [
                    p
                    for p in glob.glob(pattern)
                    if os.path.abspath(p) != os.path.abspath(self.baseFilename)
                ]
                # Sort newest first by modification time
                rotated.sort(key=os.path.getmtime, reverse=True)
                # Remove files older than the requested backupCount
                for old in rotated[self.backupCount :]:
                    try:
                        os.remove(old)
                    except Exception:
                        pass
        except Exception:
            # Never raise from rollover cleanup
            pass

        if self.maxLines > 0:
            self._line_count = 0  # Reset line count after successful rotation

        if not self.delay:
            self.stream = self._open()


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

            # Rotate at 5MB, keep 100 backups
            # Use TimestampedRotatingFileHandler for smooth rotation
            # Rotate at 5MB or 2000 lines, keep 100 backups
            # Use TimestampedRotatingFileHandler for smooth rotation
            fh = TimestampedRotatingFileHandler(
                separate_log_path,
                maxBytes=5_000_000,
                maxLines=2000,
                backupCount=100,
                encoding="utf-8",
            )
            fh.setFormatter(formatter)
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
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(formatter)
        logger.addHandler(ch)

        # Try to add a file handler. If the file handler can't be created
        # due to permission errors or other IO problems, fallback to stream
        # logging so the application can still start and emit useful logs.
        # NOTE: backupCount=9999 means files rotate (timestamp) check but never auto-delete
        try:
            # Added 2000 line limit as requested
            fh = TimestampedRotatingFileHandler(
                _LOG_FILE,
                maxBytes=5_000_000,
                maxLines=2000,
                backupCount=10,  # keep a reasonable default number of backups
                encoding="utf-8",
            )
            fh.setFormatter(formatter)
            logger.addHandler(fh)
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
                maxBytes=1_000_000,
                maxLines=500,
                backupCount=5,
                encoding="utf-8",
            )
            error_fh.setLevel(logging.ERROR)
            error_fh.setFormatter(formatter)
            logger.addHandler(error_fh)
        except Exception as e:  # pragma: no cover - environment dependent
            try:
                ch.stream.write(
                    f"[logging_utils] Could not open error log file '{_ERROR_LOG_FILE}': {e}\n"
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
