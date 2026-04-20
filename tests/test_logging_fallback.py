import importlib
import sys
import logging


def test_logging_fallback_to_stdout(tmp_path, monkeypatch, capsys):
    # Create a directory that is not writable to simulate bind-mount owned by root
    log_dir = tmp_path / "logs_no_write"
    log_dir.mkdir()
    # Remove write permissions for current user
    log_dir.chmod(0o500)

    monkeypatch.setenv("LOG_DIR", str(log_dir))

    # Ensure fresh import state
    if "core.logging_utils" in sys.modules:
        importlib.reload(sys.modules["core.logging_utils"])
    else:
        import core.logging_utils as _lu

        importlib.reload(_lu)

    from core import logging_utils

    # Reset logger state to force re-creation
    logging_utils._logger = None

    # Call setup_logging; it should not raise even if file handler can't be created
    logger = logging_utils.setup_logging()

    # We should have at least a StreamHandler
    assert any(isinstance(h, logging.StreamHandler) for h in logger.handlers)

    # The fallback warning should be printed to stdout/stderr
    captured = capsys.readouterr()
    out = (captured.out or "") + (captured.err or "")
    assert "Falling back to stdout" in out
