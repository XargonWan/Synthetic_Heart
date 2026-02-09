from fastapi.testclient import TestClient

from core.webui import SynthWebUIInterface


def test_selkies_health_endpoint_present():
    webui = SynthWebUIInterface(autostart=False)
    client = TestClient(webui.app)

    resp = client.get('/api/selkies/health')
    assert resp.status_code == 200
    data = resp.json()
    assert 'protocol' in data
    assert data['protocol'] in ('https', 'http', 'none')