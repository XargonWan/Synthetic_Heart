"""Tests for the Rift Vessel real-time gaming focus.

Covers two enforced behaviours (see AGENTS.md §5c, docs/rift_vessel.rst):

1. **Priority — a pure 0–10 numeric ranking (higher = more urgent), with NO
   de-prioritisation.** ``core.message_queue.enqueue`` ranks each message only
   by its structural origin, unconditionally — never conditional on whether a
   Vessel session is active:

     * a real in-world PLAYER chat → ``PRIORITY_HIGH`` (a human speaking
       directly, above Synth's own autonomous perceptions);
     * Synth's OWN autonomous in-world perception/will-beat → ``PRIORITY_AMBIENT``
       (below every human);
     * the trainer → ``PRIORITY_TRAINER``; ordinary chat → ``PRIORITY_GENERAL``;
     * ``priority=True`` (urgent) → ``PRIORITY_URGENT``.

   Ordinary chat is *never* demoted because Synth happens to be embodied in a
   world, so a person is always answered promptly.
2. **Context — SyntH is not omniscient while playing.** When a turn originates
   from a Vessel embodiment, ``HistoryEngine.build_context`` forces
   ``unified_mode = False`` and suppresses the global diary/memory injections.

The decision is taken purely from routing metadata (origin interface +
interface_path) — never from message text.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest


# ---------------------------------------------------------------------------
# VesselSessionManager.has_active_session()
# ---------------------------------------------------------------------------


def test_has_active_session_toggles() -> None:
    """With no liveness probe the flag falls back to the in-memory set."""
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


def test_has_active_session_reflects_connection_states() -> None:
    """The 3-state model: CONNECTED true, RECONNECTING/ENDED false.

    Once the interface registers a connection-liveness probe,
    ``has_active_session()`` returns True only while the connector is really
    connected (CONNECTED). A session that exists but whose connector has dropped
    (RECONNECTING) reads false so autonomy freezes; an ended session (no ids at
    all) is also false.
    """
    from core.vessel_session_manager import VesselSessionManager

    mgr = VesselSessionManager()
    live = {"connected": False}
    mgr.set_liveness_probe(lambda: live["connected"])

    # ENDED — no tracked session ids: trivially inactive regardless of probe.
    live["connected"] = True
    assert mgr.has_active_session() is False

    # A session now exists.
    mgr._active_session_ids.add("sess-1")

    # CONNECTED — connector really connected.
    live["connected"] = True
    assert mgr.has_active_session() is True

    # RECONNECTING — session still tracked but connector dropped: frozen.
    live["connected"] = False
    assert mgr.has_active_session() is False


def test_has_active_session_probe_failure_is_safe() -> None:
    """A raising probe is treated as not-connected (RECONNECTING), never blows up."""
    from core.vessel_session_manager import VesselSessionManager

    mgr = VesselSessionManager()

    def _boom() -> bool:
        raise RuntimeError("connector lookup failed")

    mgr.set_liveness_probe(_boom)
    mgr._active_session_ids.add("sess-1")
    # Guarded: a failing probe means "not connected" → inactive, no exception.
    assert mgr.has_active_session() is False

    # Clearing the probe reverts to bookkeeping-only behaviour.
    mgr.set_liveness_probe(None)
    assert mgr.has_active_session() is True


# ---------------------------------------------------------------------------
# message_queue.enqueue priority behaviour
# ---------------------------------------------------------------------------


def _make_message(
    interface_path: str,
    *,
    vessel_player_chat: bool = False,
    vessel_reflection: bool = False,
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
        _vessel_reflection=vessel_reflection,
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


def _semantic_priority(put_items: list[tuple[Any, Any, Any]]) -> int:
    """Recover the semantic (higher = more urgent) priority of the last put.

    ``enqueue`` pushes the NEGATED priority as the min-heap key via
    ``_heap_key``, so the tuple's first element is ``-priority``. Un-negate it.
    """
    assert put_items, "expected the message to be enqueued"
    return -int(put_items[-1][0])


@pytest.mark.asyncio
async def test_vessel_player_chat_raised_to_high_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real in-world PLAYER chat gets PRIORITY_HIGH.

    Only a chat tagged ``_vessel_player_chat`` (set structurally by the vessel
    interface from event kind + actor presence) is treated as high — so a
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

    assert _semantic_priority(put_items) == mq.PRIORITY_HIGH


@pytest.mark.asyncio
async def test_autonomous_vessel_perception_set_to_ambient_priority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Synth's OWN autonomous in-world perception gets PRIORITY_AMBIENT.

    Will beats / sightings are produced on a fast timer but consumed slowly, so
    they must sit below a real player chat (HIGH) — and below ordinary chat
    (GENERAL) — so a human is always answered before background play.
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

    assert _semantic_priority(put_items) == mq.PRIORITY_AMBIENT
    # Higher = more urgent: a player chat outranks an ambient perception, which
    # in turn sits below ordinary human chat.
    assert mq.PRIORITY_HIGH > mq.PRIORITY_AMBIENT
    assert mq.PRIORITY_GENERAL > mq.PRIORITY_AMBIENT


@pytest.mark.asyncio
async def test_ordinary_chat_not_demoted_during_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordinary chat stays PRIORITY_GENERAL even while Synth is embodied.

    There is NO de-prioritisation: a Vessel session must never lower ordinary
    cross-interface chat, so a person is always answered promptly.
    """
    import core.message_queue as mq

    put_items = _patch_enqueue_hot_path(monkeypatch, session_active=True)
    message = _make_message("telegram_bot/999")

    await mq.enqueue(
        bot=None,
        message=message,
        interface_id="telegram_bot",
        skip_mention_check=True,
    )

    assert _semantic_priority(put_items) == mq.PRIORITY_GENERAL


@pytest.mark.asyncio
async def test_trainer_chat_ranked_above_general(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trainer is ranked at PRIORITY_TRAINER (above ordinary chat)."""
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

    assert _semantic_priority(put_items) == mq.PRIORITY_TRAINER
    assert mq.PRIORITY_TRAINER > mq.PRIORITY_GENERAL


@pytest.mark.asyncio
async def test_no_session_leaves_priority_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no active session, ordinary chat keeps PRIORITY_GENERAL."""
    import core.message_queue as mq

    put_items = _patch_enqueue_hot_path(monkeypatch, session_active=False)
    message = _make_message("telegram_bot/999")

    await mq.enqueue(
        bot=None,
        message=message,
        interface_id="telegram_bot",
        skip_mention_check=True,
    )

    assert _semantic_priority(put_items) == mq.PRIORITY_GENERAL


@pytest.mark.asyncio
async def test_urgent_message_stays_high_regardless(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """priority=True (urgent) always maps to PRIORITY_URGENT, untouched."""
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

    assert _semantic_priority(put_items) == mq.PRIORITY_URGENT


@pytest.mark.asyncio
async def test_vessel_reflection_ranked_above_player_chat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reflection-pause turn gets PRIORITY_REFLECTION.

    The deliberate stop-and-think turn is ranked above a real player chat
    (HIGH) so it is consumed before ordinary in-world traffic, yet stays below
    urgent/emergency so it never pre-empts a true escalation.
    """
    import core.message_queue as mq

    put_items = _patch_enqueue_hot_path(monkeypatch, session_active=True)
    message = _make_message("vessel/minecraft", vessel_reflection=True)

    await mq.enqueue(
        bot=None,
        message=message,
        interface_id="vessel",
        skip_mention_check=True,
    )

    assert _semantic_priority(put_items) == mq.PRIORITY_REFLECTION
    # Above player chat, below urgent/emergency.
    assert mq.PRIORITY_REFLECTION > mq.PRIORITY_HIGH
    assert mq.PRIORITY_URGENT > mq.PRIORITY_REFLECTION
    assert mq.PRIORITY_EMERGENCY > mq.PRIORITY_URGENT


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


# ---------------------------------------------------------------------------
# Reconnect dispatch (background, deduped per world)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_goal_authoring_allowlist() -> None:
    """An active goal blocks set_goal on autonomous beats (anti-churn)."""
    from interface.vessel_interface import VesselInterface

    # With a live goal the beat can only update — it cannot replace mid-task.
    assert VesselInterface._goal_authoring_allowlist("minecraft", has_goal=True) == {
        "vessel_minecraft_update_goal"
    }
    # With no goal the beat may set one.
    assert VesselInterface._goal_authoring_allowlist("minecraft", has_goal=False) == {
        "vessel_minecraft_set_goal",
        "vessel_minecraft_update_goal",
    }
    # Namespace is world-scoped.
    assert VesselInterface._goal_authoring_allowlist("skyrim", has_goal=True) == {
        "vessel_skyrim_update_goal"
    }


@pytest.mark.asyncio
async def test_spawn_reattach_retry_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed reattach is retried once in the background per world, and the
    task registry is cleaned up when the retry finishes."""
    from interface.vessel_interface import VesselInterface

    iface = VesselInterface.__new__(VesselInterface)
    iface._reattach_retry_tasks = {}
    spawns: list[str] = []

    async def _fake_retry(environment: str) -> None:
        spawns.append(environment)
        await asyncio.sleep(0.05)

    monkeypatch.setattr(iface, "_retry_reattach", _fake_retry)

    iface._spawn_reattach_retry("minecraft")
    iface._spawn_reattach_retry("minecraft")  # deduped while running
    await asyncio.sleep(0.01)
    iface._spawn_reattach_retry("other")
    await asyncio.sleep(0.1)  # let both finish and their callbacks fire

    assert spawns == ["minecraft", "other"]
    assert iface._reattach_retry_tasks == {}


@pytest.mark.asyncio
async def test_spawn_reconnect_dedupes_per_world(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dropped world gets ONE background reconnect while one is in flight,
    and the task registry is cleaned up once each reconnect finishes."""
    from interface.vessel_interface import VesselInterface

    iface = VesselInterface.__new__(VesselInterface)
    iface._reconnect_tasks = {}
    spawns: list[str] = []

    async def _fake_reconnect(environment: str) -> None:
        spawns.append(environment)
        await asyncio.sleep(0.05)

    monkeypatch.setattr(iface, "_attempt_reconnect", _fake_reconnect)

    iface._spawn_reconnect("minecraft")
    iface._spawn_reconnect("minecraft")  # deduped: first still running
    await asyncio.sleep(0.01)
    iface._spawn_reconnect("other")  # a different world spawns its own
    await asyncio.sleep(0.1)  # let both finish and their callbacks fire

    assert spawns == ["minecraft", "other"]
    assert iface._reconnect_tasks == {}


@pytest.mark.asyncio
async def test_describe_sighting_shows_known_player_identity() -> None:
    """A known player sighting shows WHO is there, not just the raw username."""
    from interface.vessel_interface import VesselInterface

    origin: dict[str, Any] = {"x": 0.0, "y": 64.0, "z": 0.0}
    line = VesselInterface._describe_sighting(
        {
            "target": "remuraine",
            "distance": 9,
            "position": {"x": 0.0, "y": 64.0, "z": -9.0},
            "known_as": "Scar - your papa",
        },
        origin,
    )
    assert line is not None
    assert "remuraine (Scar - your papa)" in line
    assert "blocks" in line
    # Without an identity label the rendering is unchanged.
    plain = VesselInterface._describe_sighting(
        {"target": "sheep", "distance": 3, "position": {"x": 3.0, "y": 64.0, "z": 0.0}},
        origin,
    )
    assert plain is not None
    assert plain == "sheep (~3 blocks E)"
