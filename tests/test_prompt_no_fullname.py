import asyncio
import sys
import types
from types import SimpleNamespace

# Create stubs for core.db and aiomysql to avoid DB/LLM dependencies
sys.modules['aiomysql'] = types.SimpleNamespace()

async def get_conn_ctx():
    class DummyCtx:
        async def __aenter__(self):
            return None
        async def __aexit__(self, exc, val, tb):
            return False
    return DummyCtx()

sys.modules['core.db'] = types.SimpleNamespace(get_conn_ctx=get_conn_ctx)

from core.prompt_engine import build_json_prompt
from core.user_utils import get_user_display_name, get_user_usertag

async def _run_test():
    msg = SimpleNamespace(
        message_id=1,
        chat_id=-1,
        interface_path='grillo/-1',
        text='Test beat without full name',
        date=__import__('datetime').datetime.utcnow(),
        from_user=SimpleNamespace(id=-1, username='grillo', first_name='G.R.I.L.L.O.'),
        reply_to_message=None
    )

    prompt = await build_json_prompt(msg, {}, interface_name='grillo', image_data=None, max_chars=2000)
    payload = prompt['input']['payload']
    assert payload['source']['username'] == get_user_display_name(msg.from_user)
    assert payload['source']['usertag'] == get_user_usertag(msg.from_user)


def test_build_prompt_no_fullname():
    asyncio.run(_run_test())
