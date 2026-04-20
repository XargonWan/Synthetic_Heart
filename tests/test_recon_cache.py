from __future__ import annotations

import pytest


class _Msg:
    def __init__(self, text: str, interface_path: str = "test/interface") -> None:
        self.text = text
        self.interface_path = interface_path


class _LangPlugin:
    recon_priority = 7

    def get_recon_key(self) -> str:
        return "language_hint"

    def get_recon_instruction(self) -> str:
        return "Return language_hint"

    async def parse_recon_response(self, data, **kwargs):
        if not isinstance(data, dict):
            return []
        code = str(data.get("language_code") or "").strip()
        if not code:
            return []
        return [{"type": "language_hint", "language_code": code, "priority": 7}]

    async def get_recon_contributions(self, **kwargs):
        return []


class _TonePlugin:
    recon_priority = 6

    def get_recon_key(self) -> str:
        return "tone_hint"

    def get_recon_instruction(self) -> str:
        return "Return tone_hint"

    async def parse_recon_response(self, data, **kwargs):
        if not isinstance(data, dict):
            return []
        msg_tone = str(data.get("message_tone") or "").strip()
        convo_tone = str(data.get("conversation_tone") or "").strip()
        if not msg_tone and not convo_tone:
            return []
        return [
            {
                "type": "tone_hint",
                "message_tone": msg_tone or None,
                "conversation_tone": convo_tone or None,
                "priority": 6,
            }
        ]

    async def get_recon_contributions(self, **kwargs):
        return []


class _CountingEngine:
    def __init__(self) -> None:
        self.calls = 0

    async def generate_response(self, messages):
        self.calls += 1
        return (
            '{"language_hint":{"language_code":"it"},'
            '"tone_hint":{"message_tone":"warm","conversation_tone":"friendly"}}'
        )


class _DummyRegistry:
    def __init__(self, engine) -> None:
        self.engine = engine

    def get_engine(self, name):
        return self.engine

    def load_engine(self, name):
        return self.engine


@pytest.mark.asyncio
async def test_recon_hint_cache_skips_second_llm_call(monkeypatch):
    import core.recon as recon_mod
    import core.config as config_mod
    import core.cortex_registry as registry_mod
    import core.core_initializer as ci

    recon_mod._RECON_HINT_CACHE.clear()

    plugins_backup = dict(ci.PLUGIN_REGISTRY)
    ci.PLUGIN_REGISTRY.clear()
    ci.PLUGIN_REGISTRY["lang"] = _LangPlugin()
    ci.PLUGIN_REGISTRY["tone"] = _TonePlugin()

    engine = _CountingEngine()

    async def fake_active_cortex(scope=None):
        return "dummy"

    monkeypatch.setattr(config_mod, "get_active_cortex_engine", fake_active_cortex)
    monkeypatch.setattr(config_mod, "derive_cortex_scope", lambda *_: "chat")
    monkeypatch.setattr(
        registry_mod, "get_cortex_registry", lambda: _DummyRegistry(engine)
    )

    monkeypatch.setattr(recon_mod.config_registry, "get_var", lambda k, d=None, **_: d)

    def fake_get_value(key, default=None, value_type=None):
        if key == "ENABLE_RECON":
            return True
        if key == "RECON_LOCAL_LANGUAGE_PRECHECK":
            return True
        if key == "RECON_HINT_CACHE_TTL_SECONDS":
            return 300
        return default

    monkeypatch.setattr(recon_mod.config_registry, "get_value", fake_get_value)

    msg = _Msg("Questo e un test")

    try:
        first = await recon_mod.gather_recon_contributions(
            message=msg,
            context_memory={},
            text=msg.text,
        )
        second = await recon_mod.gather_recon_contributions(
            message=msg,
            context_memory={},
            text=msg.text,
        )
    finally:
        ci.PLUGIN_REGISTRY.clear()
        ci.PLUGIN_REGISTRY.update(plugins_backup)
        recon_mod._RECON_HINT_CACHE.clear()

    assert engine.calls == 1
    assert any(c.get("type") == "language_hint" for c in first)
    assert any(c.get("type") == "tone_hint" for c in first)
    assert any(c.get("type") == "language_hint" for c in second)
    assert any(c.get("type") == "tone_hint" for c in second)


@pytest.mark.asyncio
async def test_local_language_precheck_can_bypass_llm(monkeypatch):
    import core.recon as recon_mod
    import core.core_initializer as ci

    recon_mod._RECON_HINT_CACHE.clear()

    plugins_backup = dict(ci.PLUGIN_REGISTRY)
    ci.PLUGIN_REGISTRY.clear()
    ci.PLUGIN_REGISTRY["lang"] = _LangPlugin()

    monkeypatch.setattr(recon_mod, "_detect_language_locally", lambda _t: "pt")
    monkeypatch.setattr(recon_mod.config_registry, "get_var", lambda k, d=None, **_: d)

    def fake_get_value(key, default=None, value_type=None):
        if key == "ENABLE_RECON":
            return True
        if key == "RECON_LOCAL_LANGUAGE_PRECHECK":
            return True
        if key == "RECON_HINT_CACHE_TTL_SECONDS":
            return 300
        return default

    monkeypatch.setattr(recon_mod.config_registry, "get_value", fake_get_value)

    msg = _Msg("Bom dia")

    try:
        contribs = await recon_mod.gather_recon_contributions(
            message=msg,
            context_memory={},
            text=msg.text,
        )
    finally:
        ci.PLUGIN_REGISTRY.clear()
        ci.PLUGIN_REGISTRY.update(plugins_backup)
        recon_mod._RECON_HINT_CACHE.clear()

    assert any(
        c.get("type") == "language_hint" and c.get("language_code") == "pt"
        for c in contribs
    )
