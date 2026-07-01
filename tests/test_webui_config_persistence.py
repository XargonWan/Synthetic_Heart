from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from core.config_manager import config_registry
from core.webui import SynthWebUIInterface
import core.persona_manager as persona_manager_module


def test_update_config_entry_returns_503_when_required_persist_fails(monkeypatch):
    async def fake_persist(_key: str, _value: str) -> bool:
        return False

    pending_before = dict(config_registry._pending_persona_updates)
    monkeypatch.setattr(config_registry, "_persist_to_db", fake_persist)

    client = TestClient(SynthWebUIInterface(autostart=False).app)
    response = client.post(
        "/api/config",
        json={"key": "SYNTH_NAME", "value": "Unpersisted Synth"},
    )

    assert response.status_code == 503
    assert (
        response.json()["detail"]
        == "Failed to persist configuration 'SYNTH_NAME' to DB"
    )
    assert config_registry._pending_persona_updates == pending_before


@pytest.mark.asyncio
async def test_set_persona_name_persists_and_updates_runtime_state(monkeypatch):
    async def fake_persist(key: str, _value: str) -> bool:
        persisted_keys.append(key)
        return True

    manager = persona_manager_module.PersonaManager()
    manager._current_persona = persona_manager_module.PersonaData(
        id="default",
        name="SyntH",
        aliases=["Echo"],
        profile="Initial profile",
        created_at=datetime.now(timezone.utc).isoformat(),
        last_updated=datetime.now(timezone.utc).isoformat(),
    )
    manager._persona_loaded = True

    persisted_keys: list[str] = []
    monkeypatch.setattr(persona_manager_module, "_persona_manager_instance", manager)
    monkeypatch.setattr(config_registry, "_persist_to_db", fake_persist)

    await config_registry.set_value(
        "SYNTH_NAME", "Persisted Synth", require_persist=True
    )

    current_persona = manager.get_current_persona()
    assert current_persona is not None
    assert current_persona.name == "Persisted Synth"
    assert persisted_keys == ["SYNTH_NAME"]
    assert config_registry.get_value("SYNTH_NAME", "") == "Persisted Synth"


@pytest.mark.asyncio
async def test_save_persona_routes_changes_through_config_registry(monkeypatch):
    recorded_calls: list[tuple[str, object, bool]] = []

    async def fake_set_value(
        key: str, value: object, *, require_persist: bool = False
    ) -> None:
        recorded_calls.append((key, value, require_persist))

    manager = persona_manager_module.PersonaManager()
    persona = persona_manager_module.PersonaData(
        id="default",
        name="Save Persona Synth",
        aliases=["Echo"],
        profile="Saved profile",
        likes=["tea"],
        dislikes=["noise"],
        created_at=datetime.now(timezone.utc).isoformat(),
        last_updated=datetime.now(timezone.utc).isoformat(),
    )

    monkeypatch.setattr(config_registry, "set_value", fake_set_value)
    monkeypatch.setattr(
        persona_manager_module, "_update_persona_configs", lambda _p: None
    )

    ok = await manager.save_persona(persona)

    assert ok is True
    assert manager.get_current_persona() is persona
    assert recorded_calls == [
        ("SYNTH_NAME", "Save Persona Synth", False),
        ("SYNTH_PROFILE", "Saved profile", False),
        (
            "SYNTH_ALIASES",
            ["SyntH", "Synthetic Heart", "Save Persona Synth", "Echo"],
            False,
        ),
        ("SYNTH_LIKES", ["tea"], False),
        ("SYNTH_DISLIKES", ["noise"], False),
    ]


@pytest.mark.asyncio
async def test_webui_set_value_for_likes_updates_current_persona(monkeypatch):
    """Regression: webui POST SYNTH_LIKES must update _current_persona.likes so that
    _save_to_config_registry (called on every LLM response) doesn't stomp the edit."""

    async def fake_persist(_key: str, _value: str) -> bool:
        return True

    manager = persona_manager_module.PersonaManager()
    manager._current_persona = persona_manager_module.PersonaData(
        id="default",
        name="SyntH",
        aliases=[],
        profile="",
        likes=["old-like"],
        dislikes=["old-dislike"],
        created_at=datetime.now(timezone.utc).isoformat(),
        last_updated=datetime.now(timezone.utc).isoformat(),
    )
    manager._persona_loaded = True

    monkeypatch.setattr(persona_manager_module, "_persona_manager_instance", manager)
    monkeypatch.setattr(config_registry, "_persist_to_db", fake_persist)

    await config_registry.set_value(
        "SYNTH_LIKES", ["new-like-1", "new-like-2"], require_persist=True
    )
    await config_registry.set_value(
        "SYNTH_DISLIKES", ["new-dislike"], require_persist=True
    )

    current = manager.get_current_persona()
    assert current is not None
    assert current.likes == ["new-like-1", "new-like-2"], (
        "_current_persona.likes was not updated — _save_to_config_registry would overwrite webui edits"
    )
    assert current.dislikes == ["new-dislike"], (
        "_current_persona.dislikes was not updated — _save_to_config_registry would overwrite webui edits"
    )


@pytest.mark.asyncio
async def test_load_persona_first_call_reads_db_not_getter_defaults(monkeypatch):
    """Regression: on the very first load_persona() call (e.g. during async_init),
    _current_persona is still None. SYNTH_NAME/SYNTH_ALIASES/SYNTH_LIKES/
    SYNTH_DISLIKES all have getters that read _current_persona — with nothing to
    read, those getters silently return hard-coded defaults instead of the DB
    value, so the persona gets bootstrapped wrong and that wrong state then risks
    being written back to the DB. load_persona() must bypass those getters via
    get_persisted_value() so the first call always reflects the real DB content.
    """
    persisted = {
        "SYNTH_NAME": "2D",
        "SYNTH_ALIASES": ["SyntH", "Synthetic Heart", "2D", "Dee", "Angel"],
        "SYNTH_PROFILE": "You are 2D, also called Dee.",
        "SYNTH_LIKES": ["board games", "sunny days"],
        "SYNTH_DISLIKES": ["loud noises"],
    }

    async def fake_get_persisted_value(key, default):
        return persisted.get(key, default)

    manager = persona_manager_module.PersonaManager()
    manager._current_persona = None  # cold start — nothing to read from yet
    monkeypatch.setattr(persona_manager_module, "_persona_manager_instance", manager)
    monkeypatch.setattr(
        config_registry, "get_persisted_value", fake_get_persisted_value
    )

    result = await manager.load_persona("default")

    assert result is not None
    assert result.name == "2D"
    assert result.aliases == ["SyntH", "Synthetic Heart", "2D", "Dee", "Angel"]
    assert result.profile == "You are 2D, also called Dee."
    assert result.likes == ["board games", "sunny days"]
    assert result.dislikes == ["loud noises"]


@pytest.mark.asyncio
async def test_load_persona_does_not_overwrite_persisted_profile_with_skin_json(
    monkeypatch,
):
    """Regression: once SYNTH_PROFILE has been saved (webui edit, persona action,
    etc.) it must stay authoritative. load_persona() previously re-assembled the
    profile from the skin's persona.json on every call whenever a matching skin
    folder existed, silently reverting any saved edit on the next restart."""
    persisted = {
        "SYNTH_NAME": "2D",
        "SYNTH_ALIASES": [],
        "SYNTH_PROFILE": "Custom hand-edited profile that must survive reloads.",
        "SYNTH_LIKES": [],
        "SYNTH_DISLIKES": [],
    }

    async def fake_get_persisted_value(key, default):
        return persisted.get(key, default)

    manager = persona_manager_module.PersonaManager()
    manager._current_persona = None
    monkeypatch.setattr(persona_manager_module, "_persona_manager_instance", manager)
    monkeypatch.setattr(
        config_registry, "get_persisted_value", fake_get_persisted_value
    )
    monkeypatch.setattr(
        manager,
        "_load_persona_json",
        lambda skin_name: pytest.fail(
            "_load_persona_json must not be called when a profile is already persisted"
        ),
    )

    result = await manager.load_persona("default")

    assert result is not None
    assert result.profile == "Custom hand-edited profile that must survive reloads."
