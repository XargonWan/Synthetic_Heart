import asyncio
import time
from fastapi.testclient import TestClient
import pytest


@pytest.mark.asyncio
async def test_weather_plugin_scheduler_starts(monkeypatch):
    from plugins.weather_plugin import WeatherPlugin

    plugin = WeatherPlugin()

    calls = {"n": 0}

    async def fake_update():
        calls["n"] += 1
        plugin._cached_weather = "fake-weather"

    # Replace the real network-bound update with a no-op for test
    monkeypatch.setattr(plugin, "_update_weather", fake_update)

    await plugin.start()

    # Give the scheduler a moment to run the first update
    await asyncio.sleep(0.15)

    assert plugin._scheduler_task is not None
    assert not plugin._scheduler_task.done()
    assert calls["n"] >= 1

    await plugin.stop()


@pytest.mark.asyncio
async def test_weather_plugin_get_current_weather(monkeypatch):
    from plugins.weather_plugin import WeatherPlugin

    plugin = WeatherPlugin()

    async def fake_update_weather():
        plugin._cached_weather = "Kizugawa,Japan: ☀️ Sunny +14°C (test)"
        plugin._last_fetch = time.time()

    monkeypatch.setattr(plugin, "_update_weather", fake_update_weather)

    current = await plugin.get_current_weather()
    assert current["status"] == "ok"
    assert "Kizugawa" in current["weather"]


def test_weather_current_endpoint(monkeypatch):
    from core.webui import SynthWebUIInterface
    from core.core_initializer import PLUGIN_REGISTRY
    from plugins.weather_plugin import WeatherPlugin

    existing_plugins = dict(PLUGIN_REGISTRY)

    plugin = WeatherPlugin()
    plugin._cached_weather = "Kizugawa,Japan: ☀️ Sunny +14°C"
    plugin._last_fetch = time.time()
    PLUGIN_REGISTRY["weather"] = plugin

    client = TestClient(SynthWebUIInterface(autostart=False).app)
    r = client.get("/api/weather/current")

    assert r.status_code == 200
    assert r.json()["status"] == "ok"
    assert "Kizugawa" in r.json()["weather"]

    PLUGIN_REGISTRY.clear()
    PLUGIN_REGISTRY.update(existing_plugins)
