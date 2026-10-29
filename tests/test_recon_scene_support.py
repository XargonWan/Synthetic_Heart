"""Non-hint Recon work must not disappear behind language/tone shortcuts."""

from types import SimpleNamespace

import pytest


@pytest.mark.asyncio
@pytest.mark.parametrize("cached", [False, True])
async def test_scene_work_runs_even_when_language_is_already_known(monkeypatch, cached):
    import core.config as config
    import core.core_initializer as ci
    import core.cortex_registry as registry
    import core.recon as recon

    seen = []

    class Scene:
        def get_recon_key(self):
            return "scene_context"

        async def get_recon_instruction(self, **kwargs):
            return "CURRENT SCENE: bed; extract corrections"

        async def parse_recon_response(self, data, **kwargs):
            seen.append(data)
            return []

    class Language:
        def get_recon_key(self):
            return "language_hint"

        def get_recon_instruction(self):
            return "Detect language"

        async def parse_recon_response(self, data, **kwargs):
            return []

    calls = []

    class Engine:
        async def generate_response(self, messages):
            calls.append(messages)
            return '{"scene_context":{"changes":[]},"language_hint":{"language_code":"en"}}'

    engine = Engine()
    monkeypatch.setattr(ci, "PLUGIN_REGISTRY", {"scene": Scene(), "lang": Language()})
    monkeypatch.setattr(recon.config_registry, "get_var", lambda k, d=None, **kw: d)
    monkeypatch.setattr(recon.config_registry, "get_value", lambda k, d=None, **kw: d)
    monkeypatch.setattr(recon, "_detect_language_locally", lambda text: "en")
    monkeypatch.setattr(
        recon,
        "_get_cached_hint",
        lambda key: {"language_code": "en"} if cached else None,
    )

    async def active(**kwargs):
        return "fake", None

    monkeypatch.setattr(config, "get_active_cortex_scope", active)
    monkeypatch.setattr(
        registry,
        "get_cortex_registry",
        lambda: SimpleNamespace(get_engine=lambda name: engine),
    )
    await recon.gather_recon_contributions(
        message=SimpleNamespace(interface_path="telegram_bot/1"),
        context_memory={},
        text="you are on top of me",
    )
    assert len(calls) == 1
    assert "CURRENT SCENE: bed" in calls[0][0]["content"]
    assert "<coroutine" not in calls[0][0]["content"]
    assert seen == [{"changes": []}]
