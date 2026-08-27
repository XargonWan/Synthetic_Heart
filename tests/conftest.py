import os
from pathlib import Path

import pytest


def _load_repo_env_defaults() -> None:
    """Load the repository ``.env`` into the process environment (setdefault).

    Mirrors ``main.py::_load_repo_env_defaults`` so test runs resolve the same
    DB host / credentials the synth uses. Without this, pytest processes that
    import core modules (config_manager reads the DB at import time) fall back
    to the docker-compose default hostname ``synth-db`` — which does not exist
    on a bare host — and spam ``synth.log`` with ``getaddrinfo failed`` errors
    that look exactly like a synth DB outage. Existing environment variables
    win (``setdefault``), so CI overrides are preserved.
    """
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        if not key:
            continue

        value = value.strip()
        if value[:1] in {'"', "'"} and value[-1:] == value[:1]:
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_repo_env_defaults()


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


@pytest.fixture(autouse=True)
def _block_live_ai_diary_db(monkeypatch):
    """Prevent tests from writing to the real ai_diary table.

    ``plugins.ai_diary.get_db()`` is the single choke point for every read
    and write that module performs. Without this guard, any test that
    exercises message_chain/action_parser without explicitly mocking the
    diary layer silently appends real rows into the live ``ai_diary`` table
    (see AGENTS.md §12, "Unit tests can write real rows into the live
    ai_diary table"). Tests that need real diary DB behavior already
    monkeypatch ``ai_diary.get_db`` themselves (e.g.
    tests/test_ai_diary_db_usage.py) — that overrides this default for the
    duration of that test.
    """
    import plugins.ai_diary as ai_diary

    def _blocked_get_db():
        raise RuntimeError(
            "plugins.ai_diary.get_db() was called without being mocked in "
            "this test. Monkeypatch ai_diary.get_db (or substitute the whole "
            "plugins.ai_diary module) before exercising code paths that "
            "create or update diary entries."
        )

    monkeypatch.setattr(ai_diary, "get_db", _blocked_get_db)
