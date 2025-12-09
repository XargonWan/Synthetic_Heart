import asyncio
import pytest


@pytest.mark.asyncio
async def test_weather_plugin_scheduler_starts(monkeypatch):
    from plugins.weather_plugin import WeatherPlugin

    plugin = WeatherPlugin()

    calls = {'n': 0}

    async def fake_update():
        calls['n'] += 1
        plugin._cached_weather = 'fake-weather'

    # Replace the real network-bound update with a no-op for test
    monkeypatch.setattr(plugin, '_update_weather', fake_update)

    await plugin.start()

    # Give the scheduler a moment to run the first update
    await asyncio.sleep(0.15)

    assert plugin._scheduler_task is not None
    assert not plugin._scheduler_task.done()
    assert calls['n'] >= 1

    await plugin.stop()
