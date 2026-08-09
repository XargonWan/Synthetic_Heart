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


@pytest.mark.asyncio
async def test_daily_report_anchors_delivery_to_configured_recipient(monkeypatch):
    """The daily report must be routed to the configured interface_path.

    Regression guard: previously the report was delivered via a synthetic
    message with chat_id = -1, so it was routed to 'telegram_bot/-1' instead of
    the configured 'telegram_bot/31321637' and never reached the trainer.
    """
    from plugins.weather_plugin import WeatherPlugin
    import core.auto_response as auto_response

    plugin = WeatherPlugin()
    plugin._cached_weather = "Kizugawa,Japan: ☀️ Sunny +14°C"

    # Pin the configured interface to a full interface_path with an explicit
    # recipient so no trainer lookup is required.
    monkeypatch.setattr(
        type(plugin),
        "daily_report_interface",
        property(lambda self: "telegram_bot/31321637"),
    )
    monkeypatch.setattr(
        type(plugin),
        "daily_report_language",
        property(lambda self: "italian"),
    )

    class _FakeInterface:
        @staticmethod
        def get_interface_id() -> str:
            return "telegram_bot"

        @staticmethod
        def send_message(*args, **kwargs):
            return None

    import core.core_initializer as core_initializer

    monkeypatch.setitem(
        core_initializer.INTERFACE_REGISTRY, "telegram_bot", _FakeInterface()
    )

    captured = {}

    async def fake_delivery(message=None, interface=None, context=None, reason=None):
        captured["message"] = message
        captured["reason"] = reason
        return True

    monkeypatch.setattr(auto_response, "request_llm_delivery", fake_delivery)

    ok = await plugin._trigger_daily_report()

    assert ok is True
    msg = captured["message"]
    assert msg is not None, "delivery must carry an anchored synthetic message"
    assert msg.chat_id == 31321637
    assert msg.interface_path == "telegram_bot/31321637"
    assert msg.chat.id == 31321637


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
