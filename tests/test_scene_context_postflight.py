"""Review regressions: private snapshots, correction batches and model scope."""

from types import SimpleNamespace

import pytest

from plugins.scene_context.scene_context import SceneContextPlugin
from plugins.scene_context.store import SceneStore


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


def message():
    return SimpleNamespace(
        interface_path="telegram_bot/1",
        text="I am in the private bedroom",
        message_id=17,
        chat=SimpleNamespace(type="private"),
        from_user=SimpleNamespace(id=1, is_bot=False),
    )


@pytest.mark.asyncio
async def test_snapshot_content_is_not_put_in_debug_logged_context(
    tmp_path, monkeypatch
):
    plugin = SceneContextPlugin(store=SceneStore(tmp_path / "scenes.sqlite3"))
    monkeypatch.setattr(plugin, "_enabled", lambda: True)
    msg, ctx = message(), {}
    await plugin.get_recon_instruction(message=msg, context_memory=ctx)
    await plugin.parse_recon_response(
        {
            "changes": [
                {"field": "location", "value": "private bedroom", "evidence": msg.text}
            ]
        },
        message=msg,
        context_memory=ctx,
        text=msg.text,
    )
    assert "private bedroom" not in repr(ctx)
    assert "private bedroom" not in __import__("json").dumps(ctx)
    assert msg.interface_path not in repr(ctx)
    # plugin_instance shallow-copies context before dispatch. The token must survive.
    copied = dict(ctx)
    injection = await plugin.get_static_injection(msg, copied)
    assert "private bedroom" in injection["scene_context"]


@pytest.mark.asyncio
async def test_legacy_and_correction_batches_are_tracked_without_duplicate_reprocessing(
    tmp_path, monkeypatch
):
    plugin = SceneContextPlugin(store=SceneStore(tmp_path / "scenes.sqlite3"))
    monkeypatch.setattr(plugin, "_enabled", lambda: True)
    msg, ctx = message(), {}
    await plugin.get_recon_instruction(message=msg, context_memory=ctx)
    seen = []

    async def extract(messages, **kwargs):
        seen.append(messages)
        return {
            "changes": [
                {
                    "field": "self.activity",
                    "value": f"movement {len(seen)}",
                    "evidence": "I move my head"
                    if len(seen) == 1
                    else "I turn my head",
                }
            ]
        }

    monkeypatch.setattr(plugin, "_extract_postflight", extract)

    async def batch(text, kind="send_message", path="telegram_bot/1"):
        await plugin.on_debrief(
            processed_actions=[
                {"type": kind, "payload": {"text": text, "interface_path": path}}
            ],
            failed_actions=[],
            results={},
            context=dict(ctx),
            original_message=msg,
        )

    await batch("I move my head", kind="message_telegram_bot")
    await batch("I move my head", kind="message_telegram_bot")
    await batch("I turn my head")
    await batch("I leave this scene", path="telegram_bot/2")
    assert len(seen) == 2
    assert (await plugin.store.load(msg.interface_path))["fields"]["self.activity"][
        "value"
    ] == "movement 2"


@pytest.mark.asyncio
async def test_postflight_uses_turn_scope_and_never_loads_an_engine(monkeypatch):
    import core.config as config
    import core.cortex_registry as registry

    scopes = []

    async def active(scope=None):
        scopes.append(scope)
        return "trainer-engine", None

    class Engine:
        async def generate_response(self, messages, **kwargs):
            return '{"changes":[]}'

    monkeypatch.setattr(config, "get_active_cortex_scope", active)
    monkeypatch.setattr(
        registry,
        "get_cortex_registry",
        lambda: SimpleNamespace(get_engine=lambda name: Engine()),
    )
    plugin = SceneContextPlugin()
    assert await plugin._extract_postflight([], context={"is_trainer": True}) == {
        "changes": []
    }
    assert scopes == ["trainer"]


@pytest.mark.asyncio
async def test_failed_postflight_and_foreign_legacy_reply_leave_scene_unchanged(
    tmp_path, monkeypatch
):
    plugin = SceneContextPlugin(store=SceneStore(tmp_path / "scenes.sqlite3"))
    monkeypatch.setattr(plugin, "_enabled", lambda: True)
    msg, ctx = message(), {}
    await plugin.get_recon_instruction(message=msg, context_memory=ctx)
    calls = []

    async def failed(messages, **kwargs):
        calls.append(messages)
        raise TimeoutError()

    monkeypatch.setattr(plugin, "_extract_postflight", failed)
    for action in [
        {
            "type": "message_telegram_bot",
            "payload": {"text": "I leave this scene", "chat_id": "2"},
        },
        {"type": "message_discord_bot", "payload": {"text": "I leave this scene"}},
        {"type": "send_message", "payload": {"text": "I move my head"}},
    ]:
        await plugin.on_debrief(
            processed_actions=[action],
            failed_actions=[],
            results={},
            context=ctx,
            original_message=msg,
        )
    assert len(calls) == 1
    assert (await plugin.store.load(msg.interface_path))["fields"] == {}


@pytest.mark.asyncio
async def test_private_turn_snapshots_are_bounded(tmp_path, monkeypatch):
    plugin = SceneContextPlugin(store=SceneStore(tmp_path / "scenes.sqlite3"))
    monkeypatch.setattr(plugin, "_enabled", lambda: True)
    for _ in range(65):
        await plugin.get_recon_instruction(message=message(), context_memory={})
    assert len(plugin._turns) == 64


def test_corrupt_source_and_oversized_time_do_not_hide_good_fields():
    from plugins.scene_context.state import active_fields

    good = {"value": "bed", "source": "human", "updated_at": 100}
    scene = {
        "location": good,
        "activity": {"value": "resting", "source": [], "updated_at": 100},
        "self.position": {
            "value": "lying down",
            "source": "human",
            "updated_at": 10**400,
        },
    }
    assert active_fields(scene, now=110) == {"location": good}


def test_patch_without_value_is_not_an_implicit_clear():
    from plugins.scene_context.state import apply_patch

    scene = {"location": {"value": "bed", "source": "human", "updated_at": 100}}
    patch = {"changes": [{"field": "location", "evidence": "I am here now"}]}
    assert (
        apply_patch(scene, patch, source_text="I am here now", phase="human", now=110)
        == scene
    )


@pytest.mark.parametrize(
    "subject,protected", [("human", set()), ("self", {"relation"})]
)
def test_assistant_cannot_clear_human_subject_or_protected_relation(subject, protected):
    from plugins.scene_context.state import apply_patch

    scene = {
        "relation": {
            "value": {
                "subject": subject,
                "predicate": "beside",
                "object": "self" if subject == "human" else "human",
            },
            "source": "human",
            "updated_at": 100,
        }
    }
    patch = {
        "changes": [{"field": "relation", "value": None, "evidence": "I move away"}]
    }
    assert (
        apply_patch(
            scene,
            patch,
            source_text="I move away",
            phase="assistant",
            now=110,
            protected_fields=protected,
        )
        == scene
    )
