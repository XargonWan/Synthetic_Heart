from fastapi.testclient import TestClient

from core.webui import SynthWebUIInterface


def test_get_selkies_config_has_ports_and_host():
    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    resp = client.get("/api/selkies")
    assert resp.status_code == 200
    data = resp.json()
    assert "https_port" in data
    # http_port is optional (only present if explicitly configured)
    assert "host" in data
    assert "host" in data


def test_selkies_default_https_port_is_3006():
    """Ensure the default host-exposed Selkies HTTPS port (when not overridden)
    is the docker-compose default (3006) and is reported by the API."""
    webui = SynthWebUIInterface(autostart=False)
    assert getattr(webui, "selkies_https_port", None) == 3006
    client = TestClient(webui.app)
    resp = client.get("/api/selkies")
    assert resp.status_code == 200
    data = resp.json()
    assert data.get("https_port") == 3006
