"""Tests for connection-driven Rift Vessel action exposure (no DB, no LLM).

Design (see ``docs/rift_vessel.rst`` and AGENTS.md §5c):

* **Disconnected** — the Vessel exposes a *single* ``vessel_connect`` entry
  point whose ``game`` enum lists every enabled world. Gameplay verbs are
  hidden until Synth actually enters a world.
* **Connected to world W** — the world-agnostic *core set*
  (``say``/``move``/``look``/``use``/``attack``/``follow``/``unfollow``/
  ``status``, minus ``connect``) plus W's own ``get_world_actions()`` extras are
  exposed namespaced ``vessel_<W>_<verb>``, together with ``vessel_disconnect``.
  ``vessel_connect`` disappears while embodied.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

from plugins.rift_vessel.vessel_base import (
    PerceptionCallback,
    VesselActionResult,
    VesselConnectorBase,
)
from plugins.rift_vessel.vessel_plugin import VesselPlugin


class _FakeConnector(VesselConnectorBase):
    """Minimal connector exposing one world-specific verb (``craft``)."""

    display_name = "Fake World"

    def __init__(self) -> None:
        self.calls: list[tuple[str, Dict[str, Any]]] = []
        self._connected = False

    async def connect(
        self, settings: Dict[str, Any], on_event: PerceptionCallback
    ) -> bool:
        self._connected = True
        return True

    async def disconnect(self) -> None:
        self._connected = False

    async def act(self, action: str, payload: Dict[str, Any]) -> VesselActionResult:
        self.calls.append((action, payload))
        return VesselActionResult(ok=True, detail=f"did {action}")

    @property
    def is_connected(self) -> bool:
        return self._connected

    def get_world_actions(self) -> Dict[str, Dict[str, Any]]:
        return {
            "craft": {
                "description": "Craft an item in {world}.",
                "required_fields": ["recipe"],
                "optional_fields": [],
                "security_level": "low",
            }
        }


def _make_plugin(monkeypatch: pytest.MonkeyPatch) -> VesselPlugin:
    """Build a VesselPlugin without touching config/DB or importing connectors."""
    monkeypatch.setattr(VesselPlugin, "refresh_config", lambda self: None)
    monkeypatch.setattr(
        VesselPlugin, "_import_builtin_connectors", staticmethod(lambda: None)
    )
    monkeypatch.setattr(
        "plugins.rift_vessel.vessel_plugin.register_plugin",
        lambda name, inst: None,
    )
    p = VesselPlugin()
    p._active_connector_name = "fake"
    return p


@pytest.fixture()
def connected_plugin(monkeypatch: pytest.MonkeyPatch) -> VesselPlugin:
    """A VesselPlugin embodied ("connected") in a fake world named ``fake``."""
    p = _make_plugin(monkeypatch)
    connector = _FakeConnector()
    connector._connected = True
    monkeypatch.setattr(
        "plugins.rift_vessel.vessel_plugin.VESSEL_REGISTRY.load_connector",
        lambda name: connector,
    )
    # Live connection state: report ``fake`` as the connected world.
    monkeypatch.setattr(VesselPlugin, "_connected_world", lambda self: "fake")
    p._test_connector = connector  # type: ignore[attr-defined]
    return p


@pytest.fixture()
def disconnected_plugin(monkeypatch: pytest.MonkeyPatch) -> VesselPlugin:
    """A VesselPlugin with no live connection but ``fake``/``other`` enabled."""
    p = _make_plugin(monkeypatch)
    monkeypatch.setattr(VesselPlugin, "_connected_world", lambda self: None)
    monkeypatch.setattr(VesselPlugin, "_enabled_worlds", lambda self: ["fake", "other"])
    return p


# ---------------------------------------------------------------------------
# Disconnected: only vessel_connect with a game enum
# ---------------------------------------------------------------------------


def test_disconnected_exposes_only_connect(disconnected_plugin: VesselPlugin) -> None:
    actions = disconnected_plugin.get_supported_actions()
    assert set(actions) == {"vessel_connect"}
    schema = actions["vessel_connect"]
    assert schema["required_fields"] == ["game"]
    assert schema["game_choices"] == ["fake", "other"]
    # Enabled worlds are advertised in the description.
    assert "fake" in schema["description"]
    assert "other" in schema["description"]
    # Server-address override is optional.
    assert "host" in schema["optional_fields"]
    assert "port" in schema["optional_fields"]


def test_disconnected_no_enabled_worlds_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = _make_plugin(monkeypatch)
    monkeypatch.setattr(VesselPlugin, "_connected_world", lambda self: None)
    monkeypatch.setattr(VesselPlugin, "_enabled_worlds", lambda self: [])
    assert p.get_supported_actions() == {}


def test_disconnected_is_enabled_tracks_worlds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    p = _make_plugin(monkeypatch)
    monkeypatch.setattr(VesselPlugin, "_connected_world", lambda self: None)
    monkeypatch.setattr(VesselPlugin, "_enabled_worlds", lambda self: ["fake"])
    assert p.is_enabled() is True
    monkeypatch.setattr(VesselPlugin, "_enabled_worlds", lambda self: [])
    assert p.is_enabled() is False


# ---------------------------------------------------------------------------
# Connected: namespaced core set + world extras + disconnect
# ---------------------------------------------------------------------------


def test_connected_exposes_namespaced_core_set(
    connected_plugin: VesselPlugin,
) -> None:
    actions = connected_plugin.get_supported_actions()
    assert "vessel_fake_say" in actions
    assert "vessel_fake_status" in actions
    assert "vessel_fake_attack" in actions
    assert "vessel_fake_follow" in actions
    assert "vessel_fake_unfollow" in actions
    assert "vessel_fake_respawn" in actions
    # Connect disappears while embodied; disconnect is available.
    assert "vessel_connect" not in actions
    assert "vessel_fake_connect" not in actions
    assert "vessel_disconnect" in actions
    # The {world} placeholder is resolved.
    assert "fake" in actions["vessel_fake_say"]["description"]
    assert "{world}" not in actions["vessel_fake_say"]["description"]


def test_connected_say_supports_audio_flag(connected_plugin: VesselPlugin) -> None:
    actions = connected_plugin.get_supported_actions()
    assert "audio" in actions["vessel_fake_say"]["optional_fields"]


def test_connected_world_specific_action_is_exposed(
    connected_plugin: VesselPlugin,
) -> None:
    actions = connected_plugin.get_supported_actions()
    assert "vessel_fake_craft" in actions
    assert actions["vessel_fake_craft"]["required_fields"] == ["recipe"]
    assert "fake" in actions["vessel_fake_craft"]["description"]


def test_all_actions_stay_off_the_agent_lane(
    connected_plugin: VesselPlugin,
    disconnected_plugin: VesselPlugin,
) -> None:
    for actions in (
        connected_plugin.get_supported_actions(),
        disconnected_plugin.get_supported_actions(),
    ):
        for schema in actions.values():
            assert "external_effects" not in schema
            assert schema.get("security_level") == "low"


def test_parse_action_verb_namespaced_and_plain(
    connected_plugin: VesselPlugin,
) -> None:
    assert connected_plugin._parse_action_verb("vessel_fake_say") == "say"
    assert connected_plugin._parse_action_verb("vessel_fake_craft") == "craft"
    # Plain forms still resolve for connect/disconnect.
    assert connected_plugin._parse_action_verb("vessel_connect") == "connect"
    assert connected_plugin._parse_action_verb("vessel_disconnect") == "disconnect"
    # Non-vessel names are ignored.
    assert connected_plugin._parse_action_verb("message_telegram_bot") is None


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_core_combat_and_follow_verbs_dispatch(
    connected_plugin: VesselPlugin,
) -> None:
    connector: _FakeConnector = connected_plugin._test_connector  # type: ignore[attr-defined]
    await connected_plugin.handle_custom_action(
        "vessel_fake_attack", {"target": "creeper"}
    )
    await connected_plugin.handle_custom_action(
        "vessel_fake_follow", {"target": "Steve"}
    )
    await connected_plugin.handle_custom_action("vessel_fake_unfollow", {})
    await connected_plugin.handle_custom_action("vessel_fake_respawn", {})
    assert connector.calls == [
        ("attack", {"target": "creeper"}),
        ("follow", {"target": "Steve"}),
        ("unfollow", {}),
        ("respawn", {}),
    ]


@pytest.mark.asyncio
async def test_world_specific_verb_dispatches_to_connector(
    connected_plugin: VesselPlugin,
) -> None:
    result = await connected_plugin.handle_custom_action(
        "vessel_fake_craft", {"recipe": "torch"}
    )
    assert result["status"] == "ok"
    connector: _FakeConnector = connected_plugin._test_connector  # type: ignore[attr-defined]
    assert connector.calls == [("craft", {"recipe": "torch"})]


@pytest.mark.asyncio
async def test_connect_reads_game_field(monkeypatch: pytest.MonkeyPatch) -> None:
    p = _make_plugin(monkeypatch)
    monkeypatch.setattr(VesselPlugin, "_connected_world", lambda self: None)
    monkeypatch.setattr(VesselPlugin, "_enabled_worlds", lambda self: ["fake", "other"])

    captured: dict[str, Any] = {}

    async def _fake_connect_world(
        self: VesselPlugin,
        connector_name: str | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> VesselActionResult:
        captured["connector_name"] = connector_name
        captured["overrides"] = overrides
        return VesselActionResult(ok=True, detail="connected")

    monkeypatch.setattr(VesselPlugin, "connect_world", _fake_connect_world)
    result = await p.handle_custom_action(
        "vessel_connect", {"game": "fake", "host": "10.0.0.1", "port": 25565}
    )
    assert result["status"] == "ok"
    assert captured["connector_name"] == "fake"
    assert captured["overrides"] == {"host": "10.0.0.1", "port": 25565}


@pytest.mark.asyncio
async def test_connect_rejects_disabled_world(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """connect_world refuses a world whose sub-plugin is not enabled."""
    p = _make_plugin(monkeypatch)
    monkeypatch.setattr(VesselPlugin, "_connected_world", lambda self: None)
    monkeypatch.setattr(VesselPlugin, "_enabled_worlds", lambda self: ["fake"])
    result = await p.connect_world(connector_name="skyrim")
    assert result.ok is False
    assert "world_unavailable" in (result.detail or "")


# ---------------------------------------------------------------------------
# Outbound action activity logging
# ---------------------------------------------------------------------------


class _FakeInterface:
    """Captures outbound-action activity log calls (no DB)."""

    def __init__(self) -> None:
        self.logged: list[dict[str, Any]] = []

    async def log_outbound_action(
        self,
        environment: str,
        action: str,
        summary: str,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        self.logged.append(
            {
                "environment": environment,
                "action": action,
                "summary": summary,
                "metadata": metadata,
            }
        )


@pytest.mark.asyncio
async def test_successful_action_is_logged_to_activity(
    connected_plugin: VesselPlugin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    iface = _FakeInterface()
    monkeypatch.setattr(
        VesselPlugin, "_get_vessel_interface", staticmethod(lambda: iface)
    )
    result = await connected_plugin.act("say", {"text": "Ciao mondo"})
    assert result.ok is True
    assert len(iface.logged) == 1
    entry = iface.logged[0]
    assert entry["environment"] == "fake"
    assert entry["action"] == "say"
    # The summary is built structurally from the action + payload fields.
    assert "say" in entry["summary"]
    assert "Ciao mondo" in entry["summary"]
    assert entry["metadata"] == {"text": "Ciao mondo"}


@pytest.mark.asyncio
async def test_failed_action_is_not_logged(
    connected_plugin: VesselPlugin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connector: _FakeConnector = connected_plugin._test_connector  # type: ignore[attr-defined]

    async def _fail(action: str, payload: dict[str, Any]) -> VesselActionResult:
        return VesselActionResult(ok=False, detail="boom")

    monkeypatch.setattr(connector, "act", _fail)
    iface = _FakeInterface()
    monkeypatch.setattr(
        VesselPlugin, "_get_vessel_interface", staticmethod(lambda: iface)
    )
    result = await connected_plugin.act("move", {"x": 1})
    assert result.ok is False
    assert iface.logged == []


def test_describe_outbound_action_is_structural() -> None:
    # No keyword/content inspection: it just surfaces payload fields.
    assert VesselPlugin._describe_outbound_action("status", {}) == "status"
    desc = VesselPlugin._describe_outbound_action("move", {"x": 1, "y": None, "z": 3})
    assert desc.startswith("move(")
    assert "x=1" in desc
    assert "z=3" in desc
    # None-valued fields are omitted.
    assert "y=" not in desc


# ---------------------------------------------------------------------------
# Verbatim self-repeat suppression
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_verbatim_self_repeat_say_is_suppressed(
    connected_plugin: VesselPlugin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``say`` identical to Synth's last self-line never reaches the world.

    Slow/will-beat turns re-emit the same sentence; the structural identity
    gate against Synth's OWN last line drops the duplicate before dispatch.
    """
    from core import chat_context_manager as ccm

    context = ccm.get_or_create_chat_context("vessel/fake")
    context.clear()
    context.append({"username": "self", "text": "Sto camminando verso l'interno."})

    connector: _FakeConnector = connected_plugin._test_connector  # type: ignore[attr-defined]
    iface = _FakeInterface()
    monkeypatch.setattr(
        VesselPlugin, "_get_vessel_interface", staticmethod(lambda: iface)
    )

    result = await connected_plugin.act(
        "say", {"text": "  Sto camminando verso l'interno.  "}
    )

    assert result.ok is True
    assert result.detail == "suppressed_self_repeat"
    # The connector was never called and nothing was logged to the activity tab.
    assert connector.calls == []
    assert iface.logged == []

    context.clear()


@pytest.mark.asyncio
async def test_new_say_after_self_line_is_dispatched(
    connected_plugin: VesselPlugin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuinely new ``say`` is dispatched even right after a self-line."""
    from core import chat_context_manager as ccm

    context = ccm.get_or_create_chat_context("vessel/fake")
    context.clear()
    context.append({"username": "self", "text": "Prima frase."})

    connector: _FakeConnector = connected_plugin._test_connector  # type: ignore[attr-defined]
    iface = _FakeInterface()
    monkeypatch.setattr(
        VesselPlugin, "_get_vessel_interface", staticmethod(lambda: iface)
    )

    result = await connected_plugin.act("say", {"text": "Una frase diversa."})

    assert result.ok is True
    assert result.detail != "suppressed_self_repeat"
    assert connector.calls == [("say", {"text": "Una frase diversa."})]

    context.clear()


@pytest.mark.asyncio
async def test_self_repeat_gate_ignores_non_self_lines(
    connected_plugin: VesselPlugin,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Repeating a PLAYER's line (not Synth's own) is still dispatched.

    The gate only compares against the most recent ``self`` line, so echoing
    what someone else said is a legitimate action, never suppressed.
    """
    from core import chat_context_manager as ccm

    context = ccm.get_or_create_chat_context("vessel/fake")
    context.clear()
    context.append({"username": "XargonWan", "text": "Ehi Rekku!"})

    connector: _FakeConnector = connected_plugin._test_connector  # type: ignore[attr-defined]
    iface = _FakeInterface()
    monkeypatch.setattr(
        VesselPlugin, "_get_vessel_interface", staticmethod(lambda: iface)
    )

    result = await connected_plugin.act("say", {"text": "Ehi Rekku!"})

    assert result.ok is True
    assert result.detail != "suppressed_self_repeat"
    assert connector.calls == [("say", {"text": "Ehi Rekku!"})]

    context.clear()
