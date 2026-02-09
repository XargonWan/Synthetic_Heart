import pytest
import asyncio
import os
import json
from plugins.grillo.grillo_impl import GrilloPlugin


@pytest.mark.asyncio
async def test_fallback_write_and_read(monkeypatch, tmp_path):
    # Point fallback path to tmp logs dir by changing cwd
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        # Simulate DB failure by monkeypatching get_conn_ctx to raise
        async def fake_get_conn_ctx():
            raise Exception('db down')
        monkeypatch.setattr('core.db.get_conn_ctx', fake_get_conn_ctx)

        # Call create_action_exec which should fall back to file
        exec_obj = {
            'activity_log_id': 555,
            'action_index': 0,
            'action_type': 'schedule_message',
            'payload': {'text': 'Ciao'},
            'status': 'pending',
            'error_text': None,
            'result': None,
            'created_at': '2026-01-26T00:00:00Z'
        }
        # Use private method to write fallback (simulate failed DB)
        await GrilloPlugin._fallback_write_action_exec(exec_obj)

        # Now read using fetch_action_execs
        res = await GrilloPlugin.fetch_action_execs([555])
        assert isinstance(res, dict)
        assert 555 in res
        assert res[555][0]['action_type'] == 'schedule_message'
    finally:
        os.chdir(cwd)


@pytest.mark.asyncio
async def test_grillo_fire_and_forget_writes_fallback_when_activity_missing(monkeypatch, tmp_path):
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        # Force checker to return a suggested action
        async def fake_inspect(self, llm_reply, original_user_message, context, message):
            return [{'type': 'schedule_message', 'payload': {'text': 'Ciao', 'send_in': '10 seconds'}}]
        monkeypatch.setattr('plugins.grillo.grillo_action_checker.GrilloActionChecker.inspect_reply_and_suggest_actions', fake_inspect)

        # Simulate create_activity_log failing by returning None
        async def fake_create_activity_log(*args, **kwargs):
            return None
        from plugins.grillo.grillo_impl import GrilloPlugin
        monkeypatch.setattr(GrilloPlugin, 'create_activity_log', fake_create_activity_log)

        # Call the transport helper
        from core.transport_layer import _grillo_fire_and_forget
        from types import SimpleNamespace
        message = SimpleNamespace(chat_id=12345)

        await _grillo_fire_and_forget(None, message, 'user text', 'llm reply', {})

        # Check that fallback files were created
        logs_dir = tmp_path / 'logs'
        assert logs_dir.exists()
        act_file = logs_dir / 'grillo_activity_fallback.jsonl'
        exec_file = logs_dir / 'grillo_action_execs_fallback.jsonl'
        assert act_file.exists(), f"Expected {act_file} to exist"
        assert exec_file.exists(), f"Expected {exec_file} to exist"

        # Read and assert contents
        with open(act_file, 'r') as f:
            lines = [json.loads(l) for l in f if l.strip()]
        assert len(lines) >= 1
        # Last entry should have suggested_actions metadata
        assert 'metadata' in lines[-1]
        assert 'suggested_actions' in lines[-1]['metadata']

        with open(exec_file, 'r') as f:
            lines = [json.loads(l) for l in f if l.strip()]
        assert len(lines) >= 1
        assert lines[-1]['action_type'] == 'schedule_message'
    finally:
        os.chdir(cwd)
