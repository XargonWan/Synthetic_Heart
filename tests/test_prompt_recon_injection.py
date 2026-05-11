from datetime import datetime, timezone

import pytest

from core.prompt_engine import build_json_prompt
from core.core_initializer import PLUGIN_REGISTRY


class FakeMessage:
    def __init__(self, text="Hello world", interface_path="test/interface"):
        self.text = text
        self.interface_path = interface_path
        self.message_id = "msg-1"
        self.date = datetime.now(timezone.utc)
        self.from_user = type("U", (), {"username": "tester", "full_name": "Test User"})


class FakeReconPlugin:
    def __init__(self):
        self.recon_priority = 10

    async def get_recon_contributions(self, **kwargs):
        return [
            {
                "type": "memory",
                "content": {"source": "mem", "id": "m1", "snippet": "important memory"},
                "priority": 5,
            },
            {
                "type": "instruction",
                "content": "Prefer concise answers.",
                "priority": 3,
            },
            {"type": "language_hint", "language_code": "it", "priority": 8},
            {
                "type": "tone_hint",
                "message_tone": "empathetic",
                "conversation_tone": "warm",
                "priority": 7,
            },
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

    monkeypatch.setattr(
        recon_mod, "gather_recon_contributions", fake_gather_recon_contributions
    )

    msg = FakeMessage(text="Questo è un test")

    prompt = await build_json_prompt(
        message=msg, context_memory={}, interface_name="test"
    )

    # Recon metadata should be present in context (raw contributions are
    # no longer included — their memories are merged into top-level memories)
    ctx = prompt.get("context", {})
    assert "recon" in ctx, "recon key missing from context"
    recon = ctx.get("recon")
    assert recon is not None
    assert "contributions" not in recon, (
        "raw contributions should no longer be in recon"
    )
    assert recon.get("language") == "it"
    assert recon.get("message_tone") == "empathetic"
    assert recon.get("conversation_tone") == "warm"

    # Recon memories should be merged into top-level memories
    memories = ctx.get("memories", [])
    assert any(
        isinstance(m, str) and m == "important memory" for m in memories
    ), "recon memory should appear in top-level memories"

    # Instructions should include language and tone prefixes
    instr = prompt.get("instructions", "")
    assert "Use it language" in instr or "Use it" in instr or "Use it" in instr
    assert "empathetic" in instr

    # Clean up registry
    PLUGIN_REGISTRY.pop("fake_recon_injection", None)


@pytest.mark.asyncio
async def test_recon_keyword_normalization(monkeypatch):
    """Keywords passed into gather_recon_contributions must be normalized into single-word tokens."""
    recorded = {}

    class KWPlugin:
        def get_recon_key(self):
            return "KW"

        def get_recon_instruction(self):
            return "Return keywords"

        async def parse_recon_response(self, data, **kwargs):
            # record what keywords the core passed to us
            recorded["keywords"] = kwargs.get("keywords")
            return []

    from core.core_initializer import PLUGIN_REGISTRY

    PLUGIN_REGISTRY["kw_plugin_test"] = KWPlugin()

    class FakeEngine:
        async def generate_response(self, messages):
            return "🕰️ Waiting for manual input."

    class FakeRegistry:
        def get_engine(self, name):
            return FakeEngine()

        def load_engine(self, name):
            return FakeEngine()

    async def fake_get_active_cortex_engine(scope=None):
        return "fake"

    monkeypatch.setattr(
        "core.config.get_active_cortex_engine", fake_get_active_cortex_engine
    )
    monkeypatch.setattr(
        "core.cortex_registry.get_cortex_registry", lambda: FakeRegistry()
    )

    import core.recon as recon_mod

    # Call gather_recon_contributions with compound keywords
    await recon_mod.gather_recon_contributions(
        message=None,
        context_memory=None,
        text="test",
        tags=None,
        keywords=["narrative_part", "behavior_change", "locale_update"],
        max_results=3,
    )

    # Plugin should have received normalized single-word tokens (split on '_' and lowercased)
    assert "keywords" in recorded, "plugin did not receive keywords"
    assert recorded["keywords"] == [
        "narrative",
        "part",
        "behavior",
        "change",
        "locale",
        "update",
    ]

    # cleanup
    PLUGIN_REGISTRY.pop("kw_plugin_test", None)
