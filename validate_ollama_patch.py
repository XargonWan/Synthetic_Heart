import asyncio
import json

from interface.ollama_compat_server import OllamaCompatServer


async def fake_handle_incoming_message(
    interface, message_obj, context_memory, iface_id
):
    return None


async def fake_set_session_meta(interface_path, meta):
    pass


async def run_test():
    import core.plugin_instance as plugin_instance
    import core.session_meta as session_meta

    plugin_instance.handle_incoming_message = fake_handle_incoming_message
    session_meta.set_session_meta = fake_set_session_meta

    server = OllamaCompatServer()
    payload = {"messages": [{"role": "user", "content": "ciao"}], "stream": False}
    resp = await server._handle_chat_payload(payload)
    body = json.loads(resp.body.decode())
    print("NONSTREAM OK:", body.get("message", {}).get("content") == "", body)

    payload = {"messages": [{"role": "user", "content": "ciao"}], "stream": True}
    resp = await server._handle_chat_payload(payload)
    chunks = []
    async for raw in resp.body_iterator:
        chunks.append(json.loads(raw.decode()))
    print("STREAM OK:", chunks[-1].get("done") is True, chunks[-1])


asyncio.run(run_test())
