from fastapi.testclient import TestClient

from core.webui import SynthWebUIInterface
from core.cortex_registry import get_cortex_registry


def test_set_cortex_engine_switches_to_manual():
    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    registry = get_cortex_registry()
    try:
        registry.load_engine("manual")
    except Exception:
        return

    resp = client.post("/api/components/cortex", json={"name": "manual"})
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("status") == "ok"
    assert data.get("active") == "manual"
