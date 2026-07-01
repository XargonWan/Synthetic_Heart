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
