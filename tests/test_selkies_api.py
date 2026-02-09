from fastapi.testclient import TestClient

from core.webui import SynthWebUIInterface


def test_get_selkies_config_has_ports_and_host():
    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    resp = client.get('/api/selkies')
    assert resp.status_code == 200
    data = resp.json()
    assert 'https_port' in data
    # http_port is optional (only present if explicitly configured)
    assert 'host' in data
    assert 'host' in data