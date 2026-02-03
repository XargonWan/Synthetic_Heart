import asyncio

async def test_list_agent_proposals(monkeypatch):
    # Patch DB conn to return a sample proposed row
    import core.db as dbmod

    class FakeCursor:
        def __init__(self):
            self.queries = []

        async def execute(self, sql, params=None):
            self.queries.append((sql, params))

        async def fetchall(self):
            import datetime
            return [(101, 'ls -la', 'system', 'proposed', datetime.datetime.utcnow())]

    class FakeConn:
        def __init__(self):
            self.cur = FakeCursor()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            parent = self

            class Ctx:
                async def __aenter__(self_inner):
                    return parent.cur

                async def __aexit__(self_inner, exc_type, exc, tb):
                    return False

            return Ctx()

    fake_conn = FakeConn()
    monkeypatch.setattr(dbmod, 'get_conn_ctx', lambda: fake_conn)

    from core.webui import WebUI
    ui = WebUI(autostart=False)
    res = await ui.list_agent_proposals(limit=10)
    body = res.body if hasattr(res, 'body') else res.body
    import json
    parsed = json.loads(res.body.decode()) if hasattr(res, 'body') else res
    assert parsed.get('proposals')
    assert parsed['proposals'][0]['id'] == 101
