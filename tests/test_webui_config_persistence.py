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
