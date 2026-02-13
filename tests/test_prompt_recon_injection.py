import asyncio
from datetime import datetime

import pytest

from core.prompt_engine import build_json_prompt
from core.core_initializer import PLUGIN_REGISTRY


class FakeMessage:
    def __init__(self, text="Hello world", interface_path="test/interface"):
        self.text = text
        self.interface_path = interface_path
        self.message_id = "msg-1"
        self.date = datetime.utcnow()
        self.from_user = type("U", (), {"username": "tester", "full_name": "Test User"})


class FakeReconPlugin:
    def __init__(self):
        self.recon_priority = 10

    async def get_recon_contributions(self, **kwargs):
        return [
            {"type": "memory", "content": {"source": "mem", "id": "m1", "snippet": "important memory"}, "priority": 5},
            {"type": "instruction", "content": "Prefer concise answers.", "priority": 3},
            {"type": "language_hint", "language_code": "it", "priority": 8},
            {"type": "tone_hint", "message_tone": "empathetic", "conversation_tone": "warm", "priority": 7},
        ]

    def get_recon_key(self):
        return "FAKE"

    def get_recon_instruction(self):
        return "Return a JSON object with keys for FAKE"

    async def parse_recon_response(self, data, **kwargs):
        # This plugin is only used via gather_recon_contributions in tests by monkeypatching PLUGIN_REGISTRY,
        # so parse_recon_response won't be called in this unit test. Return empty by default.
        return []


@pytest.mark.asyncio
async def test_build_json_prompt_includes_recon_contributions(monkeypatch):
    fake = FakeReconPlugin()
    # Register fake plugin in the global registry
    PLUGIN_REGISTRY["fake_recon_injection"] = fake

    # Monkeypatch gather_recon_contributions to return normalized contributions
    import core.recon as recon_mod

    async def fake_gather_recon_contributions(**kwargs):
        return await fake.get_recon_contributions(**kwargs)

    monkeypatch.setattr(recon_mod, "gather_recon_contributions", fake_gather_recon_contributions)

    msg = FakeMessage(text="Questo è un test")

    prompt = await build_json_prompt(message=msg, context_memory={}, interface_name="test")

    # Recon contributions should be present in context
    ctx = prompt.get("context", {})
    assert "recon" in ctx, "recon key missing from context"
    recon = ctx.get("recon")
    assert recon is not None
    contribs = recon.get("contributions", [])
    assert any(c.get("type") == "memory" for c in contribs)
    assert recon.get("language") == "it"
    assert recon.get("message_tone") == "empathetic"
    assert recon.get("conversation_tone") == "warm"

    # Instructions should include language and tone prefixes
    instr = prompt.get("instructions", "")
    assert "Use it language" in instr or "Use it" in instr or "Use it" in instr
    assert "empathetic" in instr

    # Clean up registry
    PLUGIN_REGISTRY.pop("fake_recon_injection", None)
