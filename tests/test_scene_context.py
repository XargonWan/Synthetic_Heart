"""Scene continuity without live DB, provider or diary access."""

import importlib

import pytest


@pytest.fixture(autouse=True)
def opted_in_scene_config(monkeypatch):
    from core.config_manager import config_registry

    original = config_registry.get_value
    monkeypatch.setattr(
        config_registry,
        "get_value",
        lambda key, default=None, **kw: (
            True
            if key in {"SCENE_CONTEXT_ENABLED", "SCENE_CONTEXT_POSTFLIGHT_ENABLED"}
            else original(key, default, **kw)
        ),
    )


def test_directional_relation_survives_partial_update():
    state = importlib.import_module("plugins.scene_context.state")
    scene = state.apply_patch(
        {},
        {
            "changes": [
                {
                    "field": "relation",
                    "value": {
                        "subject": "self",
                        "predicate": "on top of",
                        "object": "human",
                    },
                    "evidence": "you are on top of me",
                }
            ]
        },
        source_text="you are on top of me",
        phase="human",
        now=100,
    )
    changed = state.apply_patch(
        scene,
        {
            "changes": [
                {
                    "field": "activity",
                    "value": "cuddling",
                    "evidence": "we are cuddling",
                }
            ]
        },
        source_text="we are cuddling",
        phase="human",
        now=110,
    )
    assert changed["relation"]["value"] == {
        "subject": "self",
        "predicate": "on top of",
        "object": "human",
    }
    assert "You (Synth) on top of Human" in state.render_scene(changed, now=110)
    assert changed["relation"]["updated_at"] == 100


def test_unsupported_evidence_and_assistant_human_changes_are_rejected():
    from plugins.scene_context.state import apply_patch

    assert (
        apply_patch(
            {},
            {"changes": [{"field": "location", "value": "bed", "evidence": "bed"}]},
            source_text="hello",
            phase="human",
            now=100,
        )
        == {}
    )
    assert (
        apply_patch(
            {},
            {
                "changes": [
                    {
                        "field": "human.position",
                        "value": "holding phone",
                        "evidence": "phone",
                    },
                    {"field": "location", "value": "bed", "evidence": "bed"},
                    {
                        "field": "relation",
                        "value": {
                            "subject": "human",
                            "predicate": "on top of",
                            "object": "self",
                        },
                        "evidence": "on top",
                    },
                ]
            },
            source_text="bed phone on top",
            phase="assistant",
            now=100,
        )
        == {}
    )


def test_field_expiry_reset_and_protected_correction():
    from plugins.scene_context.state import apply_patch, render_scene

    old = {"location": {"value": "bed", "updated_at": 100, "source": "human"}}
    assert render_scene(old, now=1000, max_age=60) == ""
    assert (
        apply_patch(
            old,
            {"reset": True, "reset_evidence": "new scene", "changes": []},
            source_text="new scene",
            phase="human",
            now=110,
        )
        == {}
    )
    assert (
        apply_patch(
            old,
            {"reset": True, "reset_evidence": "new scene", "changes": []},
            source_text="new scene",
            phase="assistant",
            now=110,
        )
        == old
    )
    assert (
        apply_patch(
            {},
            {
                "changes": [
                    {
                        "field": "self.position",
                        "value": "lying down",
                        "evidence": "lie down",
                    }
                ]
            },
            source_text="I lie down",
            phase="assistant",
            now=110,
            protected_fields={"self.position"},
        )
        == {}
    )


def test_relation_can_be_cleared_without_losing_location():
    from plugins.scene_context.state import apply_patch

    old = {
        "location": {"value": "bed", "updated_at": 100, "source": "human"},
        "relation": {
            "value": {"subject": "self", "predicate": "above", "object": "human"},
            "updated_at": 100,
            "source": "human",
        },
    }
    new = apply_patch(
        old,
        {"changes": [{"field": "relation", "value": None, "evidence": "I get up"}]},
        source_text="I get up",
        phase="human",
        now=110,
    )
    assert new == {"location": old["location"]}


def test_malformed_model_fields_do_not_throw():
    from plugins.scene_context.state import apply_patch

    assert (
        apply_patch(
            {},
            {"changes": [{"field": [], "value": "bed", "evidence": "bed"}]},
            source_text="bed",
            phase="human",
            now=110,
        )
        == {}
    )


@pytest.mark.asyncio
async def test_store_persists_isolates_and_rejects_stale_writers(tmp_path):
    from plugins.scene_context.store import SceneStore

    store = SceneStore(tmp_path / "scenes.sqlite3")
    before = await store.load("telegram_bot/1")
    scene = {"location": {"value": "bed", "updated_at": 100, "source": "human"}}
    assert await store.save(
        "telegram_bot/1", scene, expected_revision=before["revision"]
    )
    assert (await SceneStore(store.path).load("telegram_bot/1"))["fields"] == scene
    assert (await store.load("telegram_bot/2"))["fields"] == {}
    assert not await store.save(
        "telegram_bot/1", {}, expected_revision=before["revision"]
    )
    assert (await store.load("telegram_bot/1"))["fields"] == scene


def _message(text="you are on top of me", path="telegram_bot/1", chat_type="private"):
    from datetime import datetime, timezone
    from types import SimpleNamespace

    return SimpleNamespace(
        text=text,
        interface_path=path,
        message_id=17,
        date=datetime(2026, 10, 29, tzinfo=timezone.utc),
        chat=SimpleNamespace(type=chat_type),
        from_user=SimpleNamespace(id=1, is_bot=False),
    )


@pytest.mark.asyncio
async def test_preflight_correction_reaches_both_anchor_surfaces(tmp_path, monkeypatch):
    from plugins.scene_context.scene_context import SceneContextPlugin
    from plugins.scene_context.store import SceneStore
    from core.prompt_engine import _build_context_summary, _build_current_turn_anchor

    plugin = SceneContextPlugin(store=SceneStore(tmp_path / "scenes.sqlite3"))
    monkeypatch.setattr(plugin, "_enabled", lambda: True)
    msg, context = _message(), {}
    await plugin.get_recon_instruction(message=msg, context_memory=context)
    await plugin.parse_recon_response(
        {
            "changes": [
                {
                    "field": "relation",
                    "value": {
                        "subject": "self",
                        "predicate": "on top of",
                        "object": "human",
                    },
                    "evidence": msg.text,
                }
            ]
        },
        message=msg,
        context_memory=context,
        text=msg.text,
    )
    injection = await plugin.get_static_injection(msg, context)
    assert "scene_context" in injection
    assert "on top of Human" in injection["scene_context"]
    for render in (_build_context_summary, _build_current_turn_anchor):
        result = render(injection)
        assert "[Current scene]" in result
        assert "on top of Human" in result
    assert context["_scene_context"]["protected_fields"] == ["relation"]
    other = await plugin.get_static_injection(_message(path="telegram_bot/2"), {})
    assert other == {}


@pytest.mark.asyncio
async def test_scene_scope_excludes_internal_group_bot_and_vessel(
    tmp_path, monkeypatch
):
    from plugins.scene_context.scene_context import SceneContextPlugin
    from plugins.scene_context.store import SceneStore

    plugin = SceneContextPlugin(store=SceneStore(tmp_path / "scenes.sqlite3"))
    monkeypatch.setattr(plugin, "_enabled", lambda: True)
    for msg, ctx in [
        (_message(chat_type="group"), {}),
        (_message(path="vessel/minecraft"), {}),
        (_message(), {"beat_type": "observer"}),
        (_message(), {"is_grillo_internal": True}),
    ]:
        assert not plugin.is_recon_eligible(msg, ctx)
        assert await plugin.get_static_injection(msg, ctx) == {}
    bot = _message()
    bot.from_user.is_bot = True
    assert not plugin.is_recon_eligible(bot, {})
    monkeypatch.setattr(plugin, "_enabled", lambda: False)
    assert await plugin.get_static_injection(_message(), {}) == {}


def test_system_events_are_not_humans_and_abstract_user_is_supported(
    tmp_path, monkeypatch
):
    from plugins.scene_context.scene_context import SceneContextPlugin
    from plugins.scene_context.store import SceneStore
    from core.abstract_context import AbstractMessage, AbstractUser

    plugin = SceneContextPlugin(store=SceneStore(tmp_path / "scenes.sqlite3"))
    monkeypatch.setattr(plugin, "_enabled", lambda: True)
    system = _message()
    system.from_user.id = 0
    assert not plugin.is_recon_eligible(system, {})
    assert not plugin.is_recon_eligible(
        _message(), {"system_message": {"type": "event"}}
    )
    abstract = AbstractMessage(
        id=10, text="hello", user=AbstractUser(id=1), interface_path="telegram_bot/1"
    )
    assert plugin.is_recon_eligible(abstract, {})


@pytest.mark.asyncio
async def test_postflight_only_reads_delivered_reply_and_preserves_human_correction(
    tmp_path, monkeypatch
):
    from plugins.scene_context.scene_context import SceneContextPlugin
    from plugins.scene_context.store import SceneStore

    plugin = SceneContextPlugin(store=SceneStore(tmp_path / "scenes.sqlite3"))
    monkeypatch.setattr(plugin, "_enabled", lambda: True)
    msg, context = _message(), {}
    await plugin.get_recon_instruction(message=msg, context_memory=context)
    await plugin.parse_recon_response(
        {
            "changes": [
                {
                    "field": "relation",
                    "value": {
                        "subject": "self",
                        "predicate": "on top of",
                        "object": "human",
                    },
                    "evidence": msg.text,
                }
            ]
        },
        message=msg,
        context_memory=context,
        text=msg.text,
    )
    seen = []

    async def extract(messages, **kwargs):
        seen.append(messages)
        return {
            "changes": [
                {
                    "field": "self.activity",
                    "value": "rests head",
                    "evidence": "I rest my head",
                },
                {
                    "field": "relation",
                    "value": {
                        "subject": "self",
                        "predicate": "under",
                        "object": "human",
                    },
                    "evidence": "I rest my head",
                },
            ]
        }

    monkeypatch.setattr(plugin, "_extract_postflight", extract)
    delivered = {
        "type": "send_message",
        "payload": {"text": "I rest my head", "interface_path": msg.interface_path},
    }
    failed = {"type": "send_message", "payload": {"text": "you hold a phone"}}
    await plugin.on_debrief(
        processed_actions=[delivered],
        failed_actions=[failed],
        results={},
        context=context,
        original_message=msg,
    )
    assert len(seen) == 1
    assert "you hold a phone" not in str(seen)
    fields = (await plugin.store.load(msg.interface_path))["fields"]
    assert fields["relation"]["value"]["predicate"] == "on top of"
    assert fields["self.activity"]["value"] == "rests head"
    await plugin.on_debrief(
        processed_actions=[],
        failed_actions=[failed],
        results={},
        context=context,
        original_message=msg,
    )
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_real_prompt_pipeline_orders_recon_before_scene_injection(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace
    import core.action_parser as actions
    import core.config as config
    import core.core_initializer as ci
    import core.cortex_registry as registry
    import core.history_engine as history
    import core.prompt_engine as prompts
    import core.recon as recon
    from core.prompt_renderers import OpenAIRenderer
    from plugins.scene_context.scene_context import SceneContextPlugin
    from plugins.scene_context.store import SceneStore

    plugin = SceneContextPlugin(store=SceneStore(tmp_path / "scenes.sqlite3"))
    monkeypatch.setattr(ci, "PLUGIN_REGISTRY", {"scene_context": plugin})
    monkeypatch.setattr(actions, "_load_action_plugins", lambda: [plugin])
    monkeypatch.setattr(prompts, "extract_tags", lambda text: [])
    monkeypatch.setattr(
        config.config_registry,
        "get_value",
        lambda key, default=None, **kw: (
            True
            if key in {"SCENE_CONTEXT_ENABLED", "SCENE_CONTEXT_POSTFLIGHT_ENABLED"}
            else default
        ),
    )
    monkeypatch.setattr(
        config.config_registry, "get_var", lambda key, default=None, **kw: default
    )

    async def empty_context(*args, **kwargs):
        return {"date": "2026-10-29", "time": "10:00"}

    monkeypatch.setattr(history.HistoryEngine, "build_context", empty_context)

    async def histories(**kwargs):
        return "", ""

    monkeypatch.setattr(recon, "_build_recon_history_texts_async", histories)
    monkeypatch.setattr(recon, "_detect_language_locally", lambda text: "en")

    async def active(**kwargs):
        return "fake", None

    monkeypatch.setattr(config, "get_active_cortex_scope", active)

    class Engine:
        async def generate_response(self, messages):
            return '{"scene_context":{"changes":[{"field":"relation","value":{"subject":"self","predicate":"on top of","object":"human"},"evidence":"you are on top of me"}]}}'

    monkeypatch.setattr(
        registry,
        "get_cortex_registry",
        lambda: SimpleNamespace(get_engine=lambda name: Engine()),
    )
    msg, context = _message(), {}
    payload = await prompts.build_prompt_request(
        msg, context, interface_name="telegram_bot", max_chars=100000
    )
    request = payload["__prompt_request"]
    wire = OpenAIRenderer(request).render()
    assert "on top of Human" in wire[0]["content"]
    assert "on top of Human" in wire[-1]["content"]
    assert context["_scene_context"]["protected_fields"] == ["relation"]
