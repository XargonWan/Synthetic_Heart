import logging
import os
import sys
import traceback
from logging.handlers import RotatingFileHandler
from typing import Optional

# Try to load environment variables from .env early so logging defaults reflect .env
try:
    # load_dotenv is optional; don't crash if package is missing
    from dotenv import load_dotenv
    load_dotenv(dotenv_path="/app/.env", override=False)
except Exception:
    pass


_logger: Optional[logging.Logger] = None

# Default to a "logs" directory inside the repository rather than /config
# so running the tests does not attempt to write to restricted locations.
_DEFAULT_LOG_DIR = os.path.join(os.getcwd(), "logs")
_LOG_DIR = os.getenv("LOG_DIR", _DEFAULT_LOG_DIR)
_LOG_FILE = os.path.join(_LOG_DIR, "synth.log")
_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "WARN": logging.WARNING,
    "ERROR": logging.ERROR,
}

# Global variables for logging configuration
_LOGGING_LEVEL = os.getenv("LOGGING_LEVEL", "INFO").upper()  # Default to INFO or env value
_LOGGING_LOGCHAT_LEVEL = "ERROR"


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
                    component="core",
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
                    component="core",
                    constraints={"choices": ["DEBUG", "INFO", "WARNING", "ERROR"]},
                    tags=["logs_only"],
                ).upper()
                config_registry.add_listener("LOGGING_LOGCHAT_LEVEL", _update_logchat_level)
    except ImportError:
        # If config_manager is not available yet, use defaults
        pass


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
            
            formatter = logging.Formatter(
                "[%(asctime)s] [%(levelname)s] [%(filename)s:%(lineno)d] %(message)s",
                "%Y-%m-%d %H:%M:%S",
            )
            
            fh = RotatingFileHandler(
                separate_log_path, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
            )
            fh.setFormatter(formatter)
            separate_logger.addHandler(fh)
        
        separate_logger.log(_LEVELS.get(level.upper(), logging.INFO), message, stacklevel=4)
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
        formatter = logging.Formatter(
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
        try:
            fh = RotatingFileHandler(
                _LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
            )
            fh.setFormatter(formatter)
            logger.addHandler(fh)
        except Exception as e:  # pragma: no cover - environment dependent
            # If file handler fails, write a warning to stdout via stream handler
            try:
                logger.warning(f"[logging_utils] Could not open log file '{_LOG_FILE}': {e}. Falling back to stdout")
            except Exception:
                # As a last resort print to stdout directly
                print(f"[logging_utils] Could not open log file '{_LOG_FILE}': {e}. Falling back to stdout", file=sys.stderr)

    _logger = logger
    # Log effective logging configuration at startup
    try:
        logger.log(_LEVELS.get(_LOGGING_LEVEL, logging.INFO), f"[logging_utils] Started synth logger with level={_LOGGING_LEVEL}, log_file={_LOG_FILE}")
    except Exception:
        pass
    return logger


def _log(level: str, message: str, exc: Optional[Exception] = None, log_file: Optional[str] = None) -> None:
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
    if ("Failed to send message" in message or 
        "Unknown channel" in message or
        "interface" in message.lower() or
        "transport" in message):
        return
    
    # Check if this level should trigger notifications
    logchat_threshold = _LEVELS.get(_LOGGING_LOGCHAT_LEVEL, logging.ERROR)
    current_level = _LEVELS.get(level, logging.INFO)
    
    if current_level >= logchat_threshold:
        try:
            from core.config import get_log_chat_id_sync, get_log_chat_thread_id_sync, get_log_chat_interface_sync, get_trainer_id
            from core.core_initializer import INTERFACE_REGISTRY
            import asyncio
            
            notification_message = f"[{level}] {message}"
            
            # Try LogChat first - use the specific interface saved in DB
            log_chat_id = get_log_chat_id_sync()
            log_chat_interface = get_log_chat_interface_sync()
            
            if log_chat_id and log_chat_interface and log_chat_interface in INTERFACE_REGISTRY:
                iface = INTERFACE_REGISTRY.get(log_chat_interface)
                if iface and hasattr(iface, 'send_message'):
                    async def send_to_logchat():
                        try:
                            message_data = {"text": notification_message, "target": log_chat_id}
                            thread_id = get_log_chat_thread_id_sync()
                            if thread_id:
                                message_data["thread_id"] = thread_id
                            await iface.send_message(message_data)
                        except Exception:
                            # Silent fallback to trainer for the same interface
                            trainer_id = get_trainer_id(log_chat_interface)
                            if trainer_id:
                                trainer_data = {"text": notification_message, "target": trainer_id}
                                await iface.send_message(trainer_data)

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
            
            # Fallback to trainer - use any available interface
            for interface_name, iface in INTERFACE_REGISTRY.items():
                trainer_id = get_trainer_id(interface_name)
                if trainer_id and hasattr(iface, 'send_message'):
                    async def send_to_trainer():
                        try:
                            trainer_data = {"text": notification_message, "target": trainer_id}
                            await iface.send_message(trainer_data)
                        except Exception:
                            pass  # Silent failure

                    try:
                        loop = asyncio.get_running_loop()
                        if loop and loop.is_running():
                            loop.create_task(send_to_trainer())
                        else:
                            try:
                                loop = asyncio.get_event_loop()
                                if not loop.is_closed():
                                    loop.run_until_complete(send_to_trainer())
                                # If no running loop, just skip async send in logging context
                            except RuntimeError:
                                pass  # No event loop available in this context
                    except RuntimeError:
                        pass  # No event loop available in this context
                    return
                        
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


def log_error(msg: str, exc: Optional[Exception] = None, log_file: Optional[str] = None) -> None:
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
