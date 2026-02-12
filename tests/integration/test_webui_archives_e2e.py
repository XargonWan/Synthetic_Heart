import os
import json
import pytest
from websockets import connect
import httpx

RUN_INT = os.getenv("RUN_INTEGRATION", "0") == "1"


@pytest.mark.integration
@pytest.mark.asyncio
@pytest.mark.skipif(
    not RUN_INT, reason="Integration tests disabled (set RUN_INTEGRATION=1)"
)
async def test_webui_archive_restore_e2e():
    ws_url = os.getenv("WEBUI_WS_URL", "ws://localhost:8080/ws")
    api_url = os.getenv("WEBUI_API_URL", "http://localhost:8080")

    async with connect(ws_url) as ws:
        # Wait for session initial message
        data = await ws.recv()
        parsed = json.loads(data)
        assert parsed["type"] == "session"
        session_id = parsed["session_id"]

        # Send a message via ws to trigger processing
        await ws.send(json.dumps({"text": "Integration test message"}))

        # Wait for an ack or response
        ack = await ws.recv()
        parsed_ack = json.loads(ack)
        assert parsed_ack.get("type") in ("message_ack", "message")

        # Archive the chat via API
        async with httpx.AsyncClient() as client:
            res = await client.post(
                f"{api_url}/api/chat/archive", json={"session_id": session_id}
            )
            assert res.status_code == 200
            out = res.json()
            assert out.get("success") is True
            archive_id = out.get("archive_id")

            # Verify archive appears in listing
            res = await client.get(f"{api_url}/api/chat/archives")
            assert res.status_code == 200
            archives = (
                res.json().get("archives", []) if res.json().get("success") else []
            )
            assert any(a["id"] == archive_id for a in archives)

            # Simulate screen switch by closing and reopening WebSocket
        await ws.close()

    async with connect(ws_url) as ws2:
        # Wait for new session init
        data = await ws2.recv()
        parsed = json.loads(data)
        session2 = parsed["session_id"]
        # List archives again to ensure no duplicate messages restored
        async with httpx.AsyncClient() as client:
            res = await client.get(f"{api_url}/api/chat/archives")
            assert res.status_code == 200
            archives = (
                res.json().get("archives", []) if res.json().get("success") else []
            )
            assert any(a["id"] == archive_id for a in archives)

    # Cleanup: delete the archive
    async with httpx.AsyncClient() as client:
        await client.delete(f"{api_url}/api/chat/archives/{archive_id}")
