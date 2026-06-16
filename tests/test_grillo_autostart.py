import asyncio

import pytest

# Some test environments (local dev) may not have DB deps like aiomysql installed.
# Skip this test module when aiomysql is unavailable to keep CI/dev runs stable.
try:
    pass  # type: ignore
except Exception:
    pytest.skip(
        "aiomysql not installed; skipping grillo autostart tests",
        allow_module_level=True,
    )

from core.core_initializer import core_initializer, PLUGIN_REGISTRY

import plugins.grillo.grillo_dream as gd


@pytest.mark.asyncio
async def test_grillo_dream_autostarts(monkeypatch):
    """Ensure that the Grillo Dream plugin is started when pending async plugins are processed."""

    # Replace the long-running dream loop with a short no-op to avoid sleeping
    async def fake_dream_loop(self):
        # Mark the scheduler as not running so the fake loop exits quickly
        gd.GrilloDreamPlugin._scheduler_running = False
        return

    monkeypatch.setattr(gd.GrilloDreamPlugin, "_dream_loop", fake_dream_loop)
    monkeypatch.setattr(core_initializer, "_pending_async_plugins", [], raising=False)

    plugin = gd.GrilloDreamPlugin()
    core_initializer._pending_async_plugins.append(
        ("plugins.grillo.grillo_dream", plugin)
    )

    # Start the pending async plugin and yield once so its scheduler task can run.
    await core_initializer.start_pending_async_plugins()
    await asyncio.sleep(0)

    assert "grillo_dream" in PLUGIN_REGISTRY
    assert isinstance(PLUGIN_REGISTRY["grillo_dream"], gd.GrilloDreamPlugin)

    # The plugin should have a scheduler task attribute (may be None if it finished quickly)
    assert hasattr(plugin, "_scheduler_task")

    # Stop the plugin to clean up background tasks
    try:
        await plugin.stop()
    except Exception:
        pass
