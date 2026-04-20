import os
import pytest


@pytest.fixture(autouse=True)
def _reset_animation_handler_singleton():
    """Ensure tests don't leak KaradaStateServer singleton state across modules."""
    import core.animation_handler as ah

    ah._karada_state_server = None
    yield
    ah._karada_state_server = None


@pytest.fixture(scope="session", autouse=True)
def _set_backups_dir(tmp_path_factory):
    """Ensure tests use a writable backups directory to avoid permission issues."""
    backups = tmp_path_factory.mktemp("backups")
    # monkeypatch is function-scoped and cannot be used here, set env directly
    os.environ["SYNTH_BACKUPS_DIR"] = str(backups)
    return backups
