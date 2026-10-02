import asyncio
import pytest


@pytest.mark.asyncio
async def test_grillo_discovers_beat_plugin(monkeypatch):
    # Create a fake plugin with BEAT_TYPE and build_prompt
    class FakeBeat:
        BEAT_TYPE = "fake_beat"

        def get_supported_actions(self):
            return {}

        async def build_prompt(self):
            return "fake prompt"

    from core.core_initializer import PLUGIN_REGISTRY

    # Insert our fake plugin into the registry (remember previous if exists)
    prev = PLUGIN_REGISTRY.get("fake_beat")
    PLUGIN_REGISTRY["fake_beat"] = FakeBeat()

    from plugins.grillo.grillo_impl import GrilloPlugin

    grillo = GrilloPlugin()

    # Monkeypatch asyncio.create_task so start() doesn't spawn a real background task
    def _fake_create_task(coro):
        # Return a simple object with a done() method for stop() checks
        class Dummy:
            def done(self):
                return True

        return Dummy()

    monkeypatch.setattr(asyncio, "create_task", _fake_create_task)

    # Start then immediately stop - discovery should populate beat_plugins
    await grillo.start()
    assert "fake_beat" in grillo.beat_plugins
    # Ensure our plugin builder is callable and returns expected string
    builder = grillo.beat_plugins["fake_beat"].build_prompt
    assert asyncio.iscoroutinefunction(builder)
    got = await builder()
    assert got == "fake prompt"

    await grillo.stop()
    # Restore PLUGIN_REGISTRY
    if prev is None:
        del PLUGIN_REGISTRY["fake_beat"]
    else:
        PLUGIN_REGISTRY["fake_beat"] = prev


@pytest.mark.asyncio
async def test_second_start_is_a_no_op(monkeypatch):
    """The loader starts this plugin several times; only the first does work.

    The beat discovery, the recovery loop and the scheduler task are all
    once-per-process jobs. Guarding only the scheduler let every extra call log
    "starting lightweight scheduler" and build another recovery plugin, which is
    how four recovery loops came to run at boot and recover one failure four
    times.
    """
    from plugins.grillo.grillo_impl import GrilloPlugin

    class _PendingTask:
        def done(self) -> bool:
            return False

    monkeypatch.setattr(GrilloPlugin, "_scheduler_task", _PendingTask(), raising=False)
    monkeypatch.setattr(GrilloPlugin, "_scheduler_running", True, raising=False)

    grillo = GrilloPlugin()
    await grillo.start()

    # Nothing below the guard ran: no beat discovery, no recovery plugin, and the
    # caller's scheduler task was left exactly as it was.
    assert grillo.beat_plugins == {}
    assert getattr(grillo, "recovery_plugin", None) is None
    assert isinstance(GrilloPlugin._scheduler_task, _PendingTask)
