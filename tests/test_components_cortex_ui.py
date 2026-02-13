from fastapi.testclient import TestClient

from core.webui import SynthWebUIInterface


def test_components_payload_includes_cortex_mapping():
    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    resp = client.get("/api/components")
    assert resp.status_code == 200
    data = resp.json()

    assert "cortex" in data
    cortex = data["cortex"]
    assert "by_cortex" in cortex
    assert isinstance(cortex["by_cortex"], dict)

    # Ensure 'agent' is always an available cortex kind
    assert "agent" in (cortex.get("available_kinds") or []), (
        "Expected 'agent' to be present in available cortex kinds"
    )

    # If there are any engines, each should include the optional 'label' field
    engines = cortex.get("engines", [])
    for e in engines:
        assert "name" in e
        assert "cortex" in e
        assert "label" in e, "Each engine entry should include 'label' (possibly empty)"
