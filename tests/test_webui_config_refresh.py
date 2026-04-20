from fastapi.testclient import TestClient

from core.webui import SynthWebUIInterface
from core.config_manager import config_registry


def test_api_config_returns_db_values_after_reload():
    key = "TEST_REFRESH_RELOAD"

    # Ensure clean state
    if key in config_registry._definitions:
        del config_registry._definitions[key]

    # Register the variable with a default
    config_registry.get_var(
        key, "default-value", label="Test reload var", value_type=str
    )

    # Simulate a process that has the definition loaded from defaults
    # (what can happen during import-time when the event loop prevented DB load)
    defn = config_registry._definitions.get(key)
    assert defn is not None
    defn.raw_value = config_registry._serialize_value(defn, defn.default)
    defn.value = defn.default
    defn.loaded = True

    # Monkeypatch `load_all_from_db` so the endpoint's reload attempt will
    # populate the DB-backed value (tests don't have aiomysql available).
    async def _fake_load_all_from_db():
        # Simulate DB-loaded value
        defn.raw_value = "db-value"
        defn.value = "db-value"
        defn.loaded = True

    original_loader = config_registry.load_all_from_db
    config_registry.load_all_from_db = _fake_load_all_from_db

    try:
        webui = SynthWebUIInterface(autostart=False)
        client = TestClient(webui.app)
        r = client.get("/api/config")
        assert r.status_code == 200
        payload = r.json()
        items = payload.get("items", [])
        found = [it for it in items if it.get("key") == key]
        assert found, "Config key not present in /api/config response"
        # After the fix the API must show the value provided by the fake DB loader
        assert found[0].get("value") == "db-value"
    finally:
        config_registry.load_all_from_db = original_loader
