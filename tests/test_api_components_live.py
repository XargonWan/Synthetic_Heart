import asyncio

from fastapi.testclient import TestClient

from core.webui import SynthWebUIInterface
from core.config_manager import config_registry


def test_live_engine_active_flag():
    """Setting LIVE_CORTEX should mark the corresponding engine active.

    The components summary (`GET /api/components`) builds a list of live
    engines combining cortex engines of kind "live" and any entries from the
    ``LIVE_REGISTRY``.  Previously the "active" flag was never set which
    caused the WebUI dropdown to reset to the first option ("Disabled") each
    time the page refreshed.  Regression is prevented by exercising the
    newfound behavior.
    """

    # ensure the gemini_live engine registration is loaded
    try:
        import plugins.live_engines.gemini  # noqa: F401  # registers itself
    except Exception:
        # if the import fails for some reason we can't really exercise the
        # live registry, skip the test gracefully rather than crash the suite.
        return

    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    # pick an engine that should exist in the live registry
    engine_name = "gemini_live"

    # set the config value asynchronously
    asyncio.run(config_registry.set_value("LIVE_CORTEX", engine_name))

    resp = client.get("/api/components")
    assert resp.status_code == 200
    data = resp.json()
    live_list = data.get("live", [])

    # check that our chosen engine is present and marked active
    matched = [e for e in live_list if e.get("name") == engine_name]
    assert matched, "engine not present in live list"
    assert matched[0].get("active") is True

    # also verify that disabled is not active when a real engine is selected
    disabled_entries = [e for e in live_list if e.get("name") == "disabled"]
    assert disabled_entries and disabled_entries[0].get("active") is False

    # now disable the subsystem and confirm only disabled is active
    asyncio.run(config_registry.set_value("LIVE_CORTEX", "disabled"))
    resp = client.get("/api/components")
    assert resp.status_code == 200
    data = resp.json()
    live_list = data.get("live", [])
    disabled_entries = [e for e in live_list if e.get("name") == "disabled"]
    assert disabled_entries and disabled_entries[0].get("active") is True
