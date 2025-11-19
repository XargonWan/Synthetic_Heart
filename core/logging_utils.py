import logging
import os
import sys
import traceback
from logging.handlers import RotatingFileHandler
from typing import Optional


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
_LOGGING_LEVEL = "ERROR"
_LOGGING_LOGCHAT_LEVEL = "ERROR"


def _register_logging_config():
    """Register logging configuration with config_registry.
    
    This is called lazily to avoid circular imports.
    """
    global _LOGGING_LEVEL, _LOGGING_LOGCHAT_LEVEL
    
    try:
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
        
        _LOGGING_LEVEL = config_registry.get_value(
            "LOGGING_LEVEL",
            "ERROR",
            label="Logging Level",
            description="Minimum log level to record: DEBUG, INFO, WARNING, ERROR",
            group="logging",
            component="core",
            constraints={"choices": ["DEBUG", "INFO", "WARNING", "ERROR"]},
            tags=["logs_only"],
        ).upper()
        config_registry.add_listener("LOGGING_LEVEL", _update_logging_level)
        
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
        fh = RotatingFileHandler(
            _LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8"
        )
        fh.setFormatter(formatter)
        ch = logging.StreamHandler(sys.stdout)
        ch.setFormatter(formatter)
        logger.addHandler(fh)
        logger.addHandler(ch)

    _logger = logger
    return logger


def _log(level: str, message: str, exc: Optional[Exception] = None) -> None:
    logger = setup_logging()
    level = level.upper()
    if exc is not None:
        message = f"{message}\n{''.join(traceback.format_exception(exc))}".rstrip()
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


def log_debug(msg: str) -> None:
    _log("DEBUG", msg)


def log_info(msg: str) -> None:
    _log("INFO", msg)


def log_warning(msg: str) -> None:
    _log("WARNING", msg)


def log_error(msg: str, exc: Optional[Exception] = None) -> None:
    _log("ERROR", msg, exc)


# Initialize logging immediately when this module is imported
# This ensures the log file is created even if setup_logging() is called late
_register_logging_config()
setup_logging()
