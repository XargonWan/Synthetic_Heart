import pytest
import asyncio
from plugins.grillo.grillo_impl import GrilloPlugin

import plugins.grillo.grillo_impl as gi

class FakeHistoryEvaluator:
    def __init__(self):
        self.called = []
    async def evaluate_history(self, path, entries=5):
        self.called.append((path, entries))
        return f"[History Evaluator] Recent messages for {path}\n1. User: hi"


@pytest.mark.asyncio
async def test_grillo_skips_chats_with_last_message_from_synth(monkeypatch):
    # Prepare recent_chats to return two chats
    import core.recent_chats as recent_chats

    async def fake_last_active(n=5):
        return [(1, 'Chat One'), (2, 'Chat Two')]
    monkeypatch.setattr(recent_chats, 'get_last_active_chats_verbose', fake_last_active)
    monkeypatch.setattr(recent_chats, 'get_chat_path', lambda cid: f"telegram_bot/{cid}")

    # get_last_message: synth for first, user for second
    async def fake_get_last_message(path):
        if path.endswith('/1') or path.endswith('/1'):
            return {'sender_name': 'self', 'sender_id': 'self', 'text': 'I said this'}
        return {'sender_name': 'Alice', 'sender_id': '42', 'text': 'Hello'}
    monkeypatch.setattr('core.chat_history_cache.get_last_message', fake_get_last_message)

    gp = GrilloPlugin()
    gp.history_evaluator = FakeHistoryEvaluator()

    prompt = await gp._create_memory_consolidation_prompt()
    # Should have used chat 2's history
    assert 'Chat Two' in prompt or 'Recent messages for telegram_bot/2' in prompt


@pytest.mark.asyncio
async def test_grillo_uses_trainer_chat_even_if_last_is_synth(monkeypatch):
    import core.recent_chats as recent_chats
    from core.interfaces_registry import get_interface_registry

    async def fake_last_active(n=5):
        return [(10, 'Trainer Chat')]
    monkeypatch.setattr(recent_chats, 'get_last_active_chats_verbose', fake_last_active)
    monkeypatch.setattr(recent_chats, 'get_chat_path', lambda cid: f"telegram_bot/{cid}")

    async def fake_get_last_message(path):
        return {'sender_name': 'self', 'sender_id': 'self', 'text': 'I said this'}
    monkeypatch.setattr('core.chat_history_cache.get_last_message', fake_get_last_message)

    # Mark chat 10 as trainer for telegram_bot
    reg = get_interface_registry()
    reg.set_trainer_id('telegram_bot', 10)

    gp = GrilloPlugin()
    gp.history_evaluator = FakeHistoryEvaluator()

    prompt = await gp._create_memory_consolidation_prompt()
    assert 'Trainer Chat' in prompt or 'telegram_bot/10' in prompt


@pytest.mark.asyncio
async def test_grillo_allows_webui_chat(monkeypatch):
    import core.recent_chats as recent_chats

    async def fake_last_active(n=5):
        return [(99, 'WebUI chat')]
    monkeypatch.setattr(recent_chats, 'get_last_active_chats_verbose', fake_last_active)
    monkeypatch.setattr(recent_chats, 'get_chat_path', lambda cid: f"synth_webui/{cid}")

    async def fake_get_last_message(path):
        return {'sender_name': 'self', 'sender_id': 'self', 'text': 'I said this'}
    monkeypatch.setattr('core.chat_history_cache.get_last_message', fake_get_last_message)

    gp = GrilloPlugin()
    gp.history_evaluator = FakeHistoryEvaluator()

    prompt = await gp._create_memory_consolidation_prompt()
    assert 'WebUI chat' in prompt or 'synth_webui/99' in prompt