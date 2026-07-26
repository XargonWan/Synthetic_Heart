"""Tests for the Rift Vessel real-time gaming focus.

Covers two enforced behaviours (see AGENTS.md §5c, docs/rift_vessel.rst):

1. **Priority — the game takes top priority while embodied.** When a Vessel
   session is active, ``core.message_queue.enqueue`` raises the Vessel's own
   in-world perceptions to ``HIGH_PRIORITY`` and lowers ordinary chat from
   other interfaces to ``AGENT_PRIORITY`` (trainer and urgent messages exempt).
2. **Context — SyntH is not omniscient while playing.** When a turn originates
   from a Vessel embodiment, ``HistoryEngine.build_context`` forces
   ``unified_mode = False`` and suppresses the global diary/memory injections.

The decision is taken purely from routing metadata (origin interface + the
active-session flag / interface_path) — never from message text.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# VesselSessionManager.has_active_session()
# ---------------------------------------------------------------------------


def test_has_active_session_toggles() -> None:
    """The in-memory active-session flag flips with add/discard."""
    from core.vessel_session_manager import VesselSessionManager

    mgr = VesselSessionManager()
    assert mgr.has_active_session() is False

    mgr._active_session_ids.add("sess-1")
    assert mgr.has_active_session() is True

    mgr._active_session_ids.add("sess-2")
    assert mgr.has_active_session() is True

    mgr._active_session_ids.discard("sess-1")
    assert mgr.has_active_session() is True

    mgr._active_session_ids.discard("sess-2")
    assert mgr.has_active_session() is False


# ---------------------------------------------------------------------------
# message_queue.enqueue priority behaviour
# ---------------------------------------------------------------------------


def _make_message(
    interface_path: str, *, vessel_player_chat: bool = False
) -> SimpleNamespace:
    return SimpleNamespace(
        text="hello",
        chat_id="chat-1",
        chat=SimpleNamespace(type="group", human_count=1),
        from_user=SimpleNamespace(id=42, is_bot=False),
        interface_path=interface_path,
        thread_id=None,
        message_thread_id=None,
        _vessel_player_chat=vessel_player_chat,
    )


def _patch_enqueue_hot_path(
    monkeypatch: pytest.MonkeyPatch, *, session_active: bool, trainer_path: str = ""
) -> list[tuple[Any, Any, Any]]:
    """Neutralise every heavy dependency of ``enqueue`` and capture the queue.

    Returns the list backing the fake priority queue so callers can read the
    ``(priority_val, counter, item)`` tuple that was put on it.
    """
    import core.message_queue as mq

    put_items: list[tuple[Any, Any, Any]] = []

    class _FakeQueue:
        async def put(self, item: tuple[Any, Any, Any]) -> None:
            put_items.append(item)

    monkeypatch.setattr(mq, "_get_queue", lambda: _FakeQueue())

    # ``_broadcast_global_animation_state`` is a nested function inside
    # ``enqueue`` and is already guarded by try/except, so we can't (and needn't)
    # patch it — it degrades to a no-op when the persona manager is absent. We
    # only neutralise the module-level dependencies below.
    monkeypatch.setattr(mq, "get_reaction_emoji", lambda: None)
    monkeypatch.setattr(mq, "get_name_resolver", lambda interface: None)

    async def _not_blocked(user_id: Any) -> bool:
        return False

    monkeypatch.setattr(mq, "is_user_blocked", _not_blocked)

    class _FakePlugin:
        __module__ = "plugins.fake"

        def get_rate_limit(self) -> tuple[int, int, float]:
            return 1000, 1, 1.0

    monkeypatch.setattr(mq.plugin_instance, "get_plugin", lambda: _FakePlugin())
    monkeypatch.setattr(mq.rate_limit, "is_allowed", lambda *a, **k: True)

    class _FakeRegistry:
        def is_trainer(self, interface_id: Any, user_id: Any) -> bool:
            return False

    monkeypatch.setattr(mq, "get_interface_registry", lambda: _FakeRegistry())

    # Vessel session flag + config lookup (lazily imported inside enqueue).
    from core import vessel_session_manager as vsm_mod

    monkeypatch.setattr(
        vsm_mod.vessel_session_manager,
        "has_active_session",
        lambda: session_active,
    )

    from core import config as config_mod

    def _get_value(key: str, default: Any = None) -> Any:
        if key == "TRAINER_CHAT_ID":
            return trainer_path
        return default

    monkeypatch.setattr(config_mod.config_registry, "get_value", _get_value)

    return put_items


@pytest.mark.asyncio
async def test_vessel_player_chat_raised_to_high_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real in-world PLAYER chat during a session gets HIGH_PRIORITY.

    Only a chat tagged ``_vessel_player_chat`` (set structurally by the vessel
    interface from event kind + actor presence) is treated as urgent — so a
    human speaking to Synth jumps ahead of Synth's own autonomous perceptions.
    """
    import core.message_queue as mq

    put_items = _patch_enqueue_hot_path(monkeypatch, session_active=True)
    # World-scoped path (single shared conversation per world, no per-actor
    # suffix) — matches on_world_event's interface_path.
    message = _make_message("vessel/minecraft", vessel_player_chat=True)

    await mq.enqueue(
        bot=None,
        message=message,
        interface_id="vessel",
        skip_mention_check=True,
    )

    assert put_items, "expected the message to be enqueued"
    priority_val = put_items[-1][0]
    assert priority_val == mq.HIGH_PRIORITY


@pytest.mark.asyncio
async def test_autonomous_vessel_perception_set_to_normal_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synth's OWN autonomous in-world perception gets NORMAL_PRIORITY.

    Will beats / sightings are produced on a fast timer but consumed slowly, so
    they must sit below a real player chat (HIGH) to avoid starving it — while
    staying above deprioritised cross-interface chat (AGENT_PRIORITY).
    """
    import core.message_queue as mq

    put_items = _patch_enqueue_hot_path(monkeypatch, session_active=True)
    # No player-chat flag → an autonomous perception (will beat / sighting).
    message = _make_message("vessel/minecraft")

    await mq.enqueue(
        bot=None,
        message=message,
        interface_id="vessel",
        skip_mention_check=True,
    )

    assert put_items, "expected the message to be enqueued"
    priority_val = put_items[-1][0]
    assert priority_val == mq.NORMAL_PRIORITY
    assert mq.HIGH_PRIORITY < mq.NORMAL_PRIORITY < mq.AGENT_PRIORITY


@pytest.mark.asyncio
async def test_ordinary_chat_deprioritised_during_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary (non-trainer) chat is lowered to AGENT_PRIORITY while embodied."""
    import core.message_queue as mq

    put_items = _patch_enqueue_hot_path(monkeypatch, session_active=True)
    message = _make_message("telegram_bot/999")

    await mq.enqueue(
        bot=None,
        message=message,
        interface_id="telegram_bot",
        skip_mention_check=True,
    )

    assert put_items, "expected the message to be enqueued"
    assert put_items[-1][0] == mq.AGENT_PRIORITY


@pytest.mark.asyncio
async def test_trainer_chat_not_deprioritised_during_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trainer stays at NORMAL_PRIORITY even while a session is active."""
    import core.message_queue as mq

    put_items = _patch_enqueue_hot_path(
        monkeypatch, session_active=True, trainer_path="telegram_bot/999"
    )
    message = _make_message("telegram_bot/999")

    await mq.enqueue(
        bot=None,
        message=message,
        interface_id="telegram_bot",
        skip_mention_check=True,
    )

    assert put_items, "expected the message to be enqueued"
    assert put_items[-1][0] == mq.NORMAL_PRIORITY


@pytest.mark.asyncio
async def test_no_session_leaves_priority_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no active session, ordinary chat keeps NORMAL_PRIORITY."""
    import core.message_queue as mq

    put_items = _patch_enqueue_hot_path(monkeypatch, session_active=False)
    message = _make_message("telegram_bot/999")

    await mq.enqueue(
        bot=None,
        message=message,
        interface_id="telegram_bot",
        skip_mention_check=True,
    )

    assert put_items, "expected the message to be enqueued"
    assert put_items[-1][0] == mq.NORMAL_PRIORITY


@pytest.mark.asyncio
async def test_urgent_message_stays_high_regardless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """priority=True (urgent) always maps to HIGH_PRIORITY, untouched."""
    import core.message_queue as mq

    put_items = _patch_enqueue_hot_path(monkeypatch, session_active=True)
    message = _make_message("telegram_bot/999")

    await mq.enqueue(
        bot=None,
        message=message,
        interface_id="telegram_bot",
        skip_mention_check=True,
        priority=True,
    )

    assert put_items, "expected the message to be enqueued"
    assert put_items[-1][0] == mq.HIGH_PRIORITY


# ---------------------------------------------------------------------------
# En-route element collection (_collect_en_route_sightings)
# ---------------------------------------------------------------------------


def _make_world_state(affordances: list[dict[str, Any]]) -> SimpleNamespace:
    """Minimal WorldState-like stub carrying only the affordance list."""
    return SimpleNamespace(extra={"affordances": affordances})


def _element_signature_helper() -> Any:
    from interface.vessel_interface import VesselInterface

    return VesselInterface._element_signature


def test_element_signature_is_structural() -> None:
    """Signature keys on ``kind:target`` only — never language-specific text."""
    sig = _element_signature_helper()
    assert sig({"kind": "block", "target": "diamond_ore", "distance": 5}) == (
        "block:diamond_ore"
    )
    # Missing kind falls back to a neutral token; missing/blank target is None.
    assert sig({"target": "villager"}) == "thing:villager"
    assert sig({"kind": "mob"}) is None
    assert sig("not-a-dict") is None


@pytest.mark.asyncio
async def test_en_route_new_elements_surface_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A brand-new element is surfaced once; re-seeing it stays silent."""
    from interface.vessel_interface import VesselInterface

    iface = VesselInterface.__new__(VesselInterface)
    iface._seen_elements = {}

    events: list[dict[str, Any]] = []

    async def _fake_event(**kwargs: Any) -> bool:
        events.append(kwargs)
        return True

    monkeypatch.setattr(iface, "on_world_event", _fake_event)

    state = _make_world_state(
        [{"kind": "block", "target": "diamond_ore", "distance": 6}]
    )

    # First pass: the new element is announced.
    await iface._collect_en_route_sightings("minecraft", state)
    assert len(events) == 1
    assert events[0]["event_type"] == "sighting"
    assert events[0]["environment"] == "minecraft"
    # The batched contract carries a structured ``sightings`` list.
    sightings = events[0]["data"]["sightings"]
    assert [s["target"] for s in sightings] == ["diamond_ore"]
    assert "diamond_ore" in events[0]["summary"]

    # Second pass with the same element: nothing new to surface.
    await iface._collect_en_route_sightings("minecraft", state)
    assert len(events) == 1, "an already-seen element must not resurface"


@pytest.mark.asyncio
async def test_en_route_only_new_elements_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When a new element joins known ones, only the new one is surfaced."""
    from interface.vessel_interface import VesselInterface

    iface = VesselInterface.__new__(VesselInterface)
    iface._seen_elements = {}

    events: list[dict[str, Any]] = []

    async def _fake_event(**kwargs: Any) -> bool:
        events.append(kwargs)
        return True

    monkeypatch.setattr(iface, "on_world_event", _fake_event)

    first = _make_world_state([{"kind": "mob", "target": "cow", "distance": 3}])
    await iface._collect_en_route_sightings("minecraft", first)
    assert len(events) == 1

    # Same cow plus a new chest — only the chest is newly noticed.
    second = _make_world_state(
        [
            {"kind": "mob", "target": "cow", "distance": 2},
            {"kind": "block", "target": "chest", "distance": 8},
        ]
    )
    await iface._collect_en_route_sightings("minecraft", second)
    assert len(events) == 2
    new_targets = [s["target"] for s in events[-1]["data"]["sightings"]]
    assert new_targets == ["chest"]
    assert "chest" in events[-1]["summary"]


def test_cardinal_bearing_is_geometric() -> None:
    """Bearing is a pure 8-point compass label from a planar offset."""
    from interface.vessel_interface import VesselInterface

    origin = {"x": 0.0, "y": 64.0, "z": 0.0}
    bearing = VesselInterface._cardinal_bearing
    # Minecraft axes: +x East, -x West, +z South, -z North.
    assert bearing(origin, {"x": 0.0, "y": 64.0, "z": -10.0}) == "N"
    assert bearing(origin, {"x": 10.0, "y": 64.0, "z": 0.0}) == "E"
    assert bearing(origin, {"x": 0.0, "y": 64.0, "z": 10.0}) == "S"
    assert bearing(origin, {"x": -10.0, "y": 64.0, "z": 0.0}) == "W"
    assert bearing(origin, {"x": 10.0, "y": 64.0, "z": -10.0}) == "NE"
    assert bearing(origin, {"x": -10.0, "y": 64.0, "z": 10.0}) == "SW"
    # Coincident / missing positions yield no heading.
    assert bearing(origin, {"x": 0.0, "y": 64.0, "z": 0.0}) is None
    assert bearing(origin, None) is None
    assert bearing(None, {"x": 1.0}) is None


@pytest.mark.asyncio
async def test_en_route_batches_into_one_directional_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Multiple new elements surface as ONE compact, directional perception."""
    from interface.vessel_interface import VesselInterface

    iface = VesselInterface.__new__(VesselInterface)
    iface._seen_elements = {}

    events: list[dict[str, Any]] = []

    async def _fake_event(**kwargs: Any) -> bool:
        events.append(kwargs)
        return True

    monkeypatch.setattr(iface, "on_world_event", _fake_event)

    state = SimpleNamespace(
        position={"x": 0.0, "y": 64.0, "z": 0.0},
        extra={
            "affordances": [
                {
                    "kind": "block",
                    "target": "tall_seagrass",
                    "distance": 6,
                    "position": {"x": 4.0, "y": 64.0, "z": -4.0},
                },
                {
                    "kind": "block",
                    "target": "dirt",
                    "distance": 5,
                    "position": {"x": -4.0, "y": 64.0, "z": 4.0},
                },
            ]
        },
    )

    await iface._collect_en_route_sightings("minecraft", state)

    # A single grouped event, not one per element.
    assert len(events) == 1
    summary = events[0]["summary"]
    assert "You notice the following blocks:" in summary
    assert "tall_seagrass (~6 blocks NE)" in summary
    assert "dirt (~5 blocks SW)" in summary
    targets = [s["target"] for s in events[0]["data"]["sightings"]]
    assert set(targets) == {"tall_seagrass", "dirt"}


@pytest.mark.asyncio
async def test_en_route_no_state_is_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing world/state surfaces nothing and never raises."""
    from interface.vessel_interface import VesselInterface

    iface = VesselInterface.__new__(VesselInterface)
    iface._seen_elements = {}

    events: list[dict[str, Any]] = []

    async def _fake_event(**kwargs: Any) -> bool:
        events.append(kwargs)
        return True

    monkeypatch.setattr(iface, "on_world_event", _fake_event)

    await iface._collect_en_route_sightings(None, _make_world_state([]))
    await iface._collect_en_route_sightings("minecraft", None)
    await iface._collect_en_route_sightings("minecraft", _make_world_state([]))
    assert events == []


# ---------------------------------------------------------------------------
# Damage perception — source-aware, player-count-aware (never hardcoded)
# ---------------------------------------------------------------------------


def _make_damage_iface() -> Any:
    """A bare VesselInterface with only the attack-count registry wired."""
    from interface.vessel_interface import VesselInterface

    iface = VesselInterface.__new__(VesselInterface)
    iface._attack_counts = {}
    return iface


def test_damage_player_increments_and_reports_count() -> None:
    """Repeated player hits carry an escalating count into the summary.

    The count is surfaced (accident on the first, a pattern by the third) but
    the reaction is left entirely to cognition — nothing here decides it.
    """
    iface = _make_damage_iface()
    scope = "vessel/minecraft"
    data = {"attacker": {"name": "Griefer", "source": "player", "distance": 2.0}}

    s1 = iface._enrich_damage_summary(scope, "Took damage from Griefer", data)
    s2 = iface._enrich_damage_summary(scope, "Took damage from Griefer", data)
    s3 = iface._enrich_damage_summary(scope, "Took damage from Griefer", data)

    assert "Griefer" in s1 and "player" in s1 and "1 time" in s1
    assert "2 time" in s2
    assert "3 time" in s3
    assert iface._attack_counts[scope]["Griefer"] == 3


def test_damage_mob_not_counted_but_flagged_hostile() -> None:
    """Mob damage is flagged hostile (natural counter) and never tallied."""
    iface = _make_damage_iface()
    scope = "vessel/minecraft"
    data = {"attacker": {"name": "Zombie", "source": "mob", "distance": 1.5}}

    out = iface._enrich_damage_summary(scope, "Took damage from Zombie", data)

    assert "Zombie" in out and "hostile" in out and "not a player" in out
    assert scope not in iface._attack_counts


def test_damage_counts_are_per_player() -> None:
    """Each player gets an independent tally within the same world scope."""
    iface = _make_damage_iface()
    scope = "vessel/minecraft"
    a = {"attacker": {"name": "Alice", "source": "player"}}
    b = {"attacker": {"name": "Bob", "source": "player"}}

    iface._enrich_damage_summary(scope, "Took damage from Alice", a)
    iface._enrich_damage_summary(scope, "Took damage from Alice", a)
    iface._enrich_damage_summary(scope, "Took damage from Bob", b)

    assert iface._attack_counts[scope]["Alice"] == 2
    assert iface._attack_counts[scope]["Bob"] == 1


def test_damage_no_attacker_returns_summary_unchanged() -> None:
    """Environmental/unattributed damage degrades to the original summary."""
    iface = _make_damage_iface()
    scope = "vessel/minecraft"

    assert (
        iface._enrich_damage_summary(scope, "Took fall damage", None)
        == "Took fall damage"
    )
    assert (
        iface._enrich_damage_summary(scope, "Took damage", {"attacker": None})
        == "Took damage"
    )
    assert iface._attack_counts == {}
