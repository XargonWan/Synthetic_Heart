# plugins/rift_vessel/minecraft/minecraft.py
"""Minecraft Vessel connector.

Bridges SyntH's Rift Vessel layer to a Minecraft world via the Node.js
Mineflayer bridge (``minecraft_bridge.js``, in this same folder,
managed by :mod:`interface.minecraft_provisioner`). This connector speaks plain
HTTP to the local bridge:

* normalized actions (``say`` / ``move`` / ``look`` / ``use`` / ``status`` /
  ``skin``) → ``POST /cmd``
* world events ← polled from ``GET /events`` and forwarded to the interface's
  perception callback as :class:`plugins.rift_vessel.vessel_base.PerceptionEvent`.

Design constraints (see ``docs/rift_vessel.rst``):

* The connector never creates agentic tasks — actions map 1:1 to bridge
  commands, and perception events are pushed through the interface's simple
  salience filter (no Agent Lane, no Drone).
* No diary/memory is written here; the interface buffers experiences and flushes
  at end-of-session.

Register at import time so the registry can discover it lazily.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import os
import random
import socket
from urllib.parse import urlparse
from typing import Any, Dict, Optional

import aiohttp

from core.config_manager import config_registry
from core.core_initializer import register_plugin
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.plugin_base import PluginBase
from core.vessel_registry import register_vessel_connector
from plugins.rift_vessel.knowledge_client import WikiSource
from plugins.rift_vessel.minecraft import base_spec
from plugins.rift_vessel.minecraft import bases as mc_bases
from plugins.rift_vessel.minecraft import goals as mc_goals
from plugins.rift_vessel.minecraft import quests
from plugins.rift_vessel.minecraft import target_names as mc_target_names
from plugins.rift_vessel.minecraft import wiki_client
from plugins.rift_vessel.vessel_combat_strategy import (
    apply_combat_strategy,
    register_combat_strategy,
)
from plugins.rift_vessel.vessel_base import (
    PerceptionCallback,
    PerceptionEvent,
    VesselActionResult,
    VesselConnectorBase,
    WorldState,
)

LOG_PREFIX = "[minecraft_connector]"

ENVIRONMENT = "minecraft"
_POLL_INTERVAL_SEC = 1.0
_HTTP_TIMEOUT_SEC = 10.0
# The bridge's /connect can take up to its own pre-spawn retry budget
# (CONNECT_TIMEOUT_MS = 90000 in minecraft_bridge.js) because some
# servers close the first handshake before spawn and the bridge silently
# retries, and slow/proxied servers can take well over 30s to complete the
# login+world-load handshake. The Python client timeout for /connect must
# therefore exceed that budget, otherwise aiohttp aborts at _HTTP_TIMEOUT_SEC
# (10s) — or an over-tight value — while the bridge is still waiting for spawn;
# the connector then reports connect_failed and closes the session even though
# the bot spawns a moment later, leaving an orphaned bridge. 100s leaves
# headroom over the bridge's 90s budget.
_CONNECT_HTTP_TIMEOUT_SEC = 100.0
# Consecutive failed ``/events`` polls after which the connector considers the
# bridge/world client gone and flips ``is_connected`` to False. Without this the
# poll loop would spin forever on a dead bridge, leaving ``is_connected`` stuck
# True — which keeps the Vessel session "active" and lets autonomous beats pile
# up and block the message flow. At ``_POLL_INTERVAL_SEC`` (1s) this is a ~5s
# liveness window, well under the interface disconnect-grace sweep.
_MAX_POLL_FAILURES = 5
# Consecutive ``/health`` reads reporting ``connected: false`` (while the bridge
# HTTP server itself is still answering ``ok: true``) after which the connector
# considers the world embodiment lost and flips ``is_connected`` to False. This
# closes the gap where the Node bridge process stays alive (so ``/events`` keeps
# succeeding and the poll-failure streak above never fires) but its mineflayer
# bot was dropped by the server — leaving ``is_connected`` stuck True and Synth
# believing it is in-world when it is not.
#
# The threshold MUST be generous: the Node bridge auto-reconnects in-process, so
# the mineflayer bot routinely reports ``connected: false`` for several seconds
# during ordinary play — the login→spawn handshake, a respawn after death, a
# dimension/world change, or a momentary server hiccup all blip the flag. A
# small threshold (the historical 3, i.e. ~3s at ``_POLL_INTERVAL_SEC``) turned
# those benign transients into a full disconnect: the connector flipped
# ``is_connected`` False, the interface's grace sweep ended the session with
# ``connect_failed`` while the bridge was still alive and embodied a moment
# later, and every autonomy beat froze — Synth looked "switched off". Only a
# *sustained* absence is a genuine drop, so this is set to ~60s worth of ticks;
# the bridge's own auto-reconnect settles any shorter blip long before it trips.
# Structural boolean check, no keyword logic.
_MAX_HEALTH_FALSE_STREAK = 60
# A Mineflayer reconnect briefly reports ``not connected`` while the bridge
# process is still alive and negotiating the server connection.  Keep a chat
# reply alive across that short transition instead of dropping it immediately.
_SAY_RETRY_ATTEMPTS = 8
_SAY_RETRY_DELAY_SEC = 0.5
# Minecraft servers and proxy/chat plugins can drop back-to-back client chat
# packets even when the HTTP command itself succeeds.  Serialize embodied
# speech and leave a small inter-packet gap so an action batch cannot race the
# server's chat handling.
_SAY_GAP_SEC = 0.15

# Loopback host names that mean "this same machine". When Synth runs inside a
# container these do NOT point at the Docker host (where a "Open to LAN" world
# actually listens), so they are auto-remapped to the host gateway below.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})
# Stable name for the Docker host, provided by ``extra_hosts:
# host.docker.internal:host-gateway`` in docker-compose.yml.
_HOST_GATEWAY_NAME = "host.docker.internal"

# --- Self-preservation: canonical game block ids (NOT keyword scans) ---------
# These are exact Minecraft block enum ids reported by the bridge — matching on
# them is game-logic classification, not natural-language keyword detection.
_LIQUID_BLOCK_IDS = frozenset({"water", "flowing_water", "bubble_column"})
_HOT_BLOCK_IDS = frozenset({"lava", "flowing_lava", "fire", "soul_fire", "magma_block"})
# How many blocks straight up the drowning reflex aims for to reach the surface.
_SURFACE_CLIMB_BLOCKS = 8


def _is_liquid_block(block_id: object) -> bool:
    """True when the given block id is a water/liquid the body can drown in.

    Structural game-id membership test — never a keyword scan of free text.
    """
    return isinstance(block_id, str) and block_id in _LIQUID_BLOCK_IDS


def _is_hot_block(block_id: object) -> bool:
    """True when the given block id is lava/fire that damages the body.

    Structural game-id membership test — never a keyword scan of free text.
    """
    return isinstance(block_id, str) and block_id in _HOT_BLOCK_IDS


def _result_acted(result: object) -> bool:
    """Normalise a survival dispatch outcome to a plain ``acted`` bool.

    The dispatch helpers return either a ``VesselActionResult`` (from ``act``)
    or a plain ``{"acted": bool}`` dict (on an early guard fail), so read
    whichever shape is present.
    """
    ok = getattr(result, "ok", None)
    if ok is not None:
        return bool(ok)
    if isinstance(result, dict):
        items = dict(result)
        return bool(items.get("acted") or items.get("ok"))
    return False


def _is_in_container() -> bool:
    """Best-effort detection of whether Synth runs inside a container.

    Order of precedence: explicit ``SYNTH_IN_CONTAINER`` env override →
    ``/.dockerenv`` (Docker) / ``/run/.containerenv`` (Podman) → a
    ``docker``/``kubepods``/``containerd``/``libpod`` marker in
    ``/proc/1/cgroup``. Defaults to ``False`` (host) when unsure.
    """
    override = os.getenv("SYNTH_IN_CONTAINER")
    if override is not None:
        return override.strip().lower() in ("1", "true", "yes", "on")
    if os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv"):
        return True
    try:
        with open("/proc/1/cgroup", "r", encoding="utf-8", errors="replace") as fh:
            cgroup = fh.read()
        if any(m in cgroup for m in ("docker", "kubepods", "containerd", "libpod")):
            return True
    except Exception:
        pass
    return False


def _detect_lan_ip() -> str | None:
    """Best-effort discovery of the machine's primary outbound (LAN) IP.

    Opens a UDP socket toward a public address and reads the local endpoint the
    OS would use to reach it. No packet is actually sent (UDP ``connect`` only
    selects a route), so this works offline and never blocks on the network.
    Returns ``None`` when no non-loopback address can be determined.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(0.2)
            sock.connect(("8.8.8.8", 80))
            ip = sock.getsockname()[0]
    except Exception:
        try:
            ip = socket.gethostbyname(socket.gethostname())
        except Exception:
            return None
    ip = str(ip or "").strip()
    if not ip or ip in _LOOPBACK_HOSTS or ip.startswith("127."):
        return None
    return ip


class MinecraftConnector(VesselConnectorBase):
    """Rift Vessel connector for Minecraft via the local Mineflayer bridge."""

    display_name = "Minecraft"

    def __init__(self) -> None:
        self._on_event: PerceptionCallback | None = None
        self._session: aiohttp.ClientSession | None = None
        self._poll_task: asyncio.Task[None] | None = None
        # Startup reattach and an explicit vessel_connect can arrive together.
        # Serialize the bridge handshake so two callers cannot replace each
        # other's HTTP session or issue competing /connect requests.
        self._connect_lock = asyncio.Lock()
        self._say_lock = asyncio.Lock()
        self._connected = False
        self._base_url = ""
        # Per-connect settings (host/port/version overrides) captured so the
        # world identity can be resolved consistently for this connect — set at
        # the top of :meth:`connect` and also seeded by the plugin *before*
        # ``begin_session`` so the interface path carries the right world token.
        self._connect_settings: Dict[str, Any] = {}
        # Optional session id for tagging goal rows (best-effort tracability).
        self._session_id: str | None = None
        # Human-readable reason for the last failed connect (bridge health
        # failure, missing mineflayer, server-side connect error such as a
        # version mismatch, ...). Read by connect_world so Synth can tell the
        # requester WHY entering the world failed.
        self.last_error: str | None = None
        # --- Anti-stall reflex state (in-memory, no DB) -------------------
        # When the body has reached its self-chosen destination but cognition
        # (the slow will beat) has not yet handed it a fresh objective, the
        # motor must not freeze in place waiting: the world is live and Synth's
        # plans can change. We count how many consecutive motor ticks have
        # observed "arrived on the same goal with nothing new to do"; past a
        # threshold the reflex reprojects the destination forward so the body
        # keeps exploring on its own instead of circling the arrival tile.
        self._arrival_goal_key: str | None = None
        self._arrival_stall_ticks = 0
        # Rolling heading (radians) used to steer autonomous exploration when
        # reprojecting a stale destination, so successive reprojections fan out
        # instead of retracing the same line.
        self._explore_heading = 0.0
        # Structural id (``kind:name``) of the benign affordance the reflex most
        # recently interacted with while standing still. Once we have ``use``/
        # ``mine``d something in reach, repeating the exact same interaction on
        # the next tick is pointless and pins the body on the spot forever (the
        # user-reported "freezes inert at one point" bug): a live scan keeps
        # re-surfacing the same adjacent block/entity, so without this the reflex
        # would ``use`` it every 3 s and never fall through to travel/march. We
        # remember the last one and skip it so the body moves on. Purely
        # structural (kind + exact id) — never keyword matching.
        self._last_reflex_interaction: str | None = None
        # Progress watchdog for a *named* block/entity target the body keeps
        # ``arriving`` at but can never interact with. The named-target branch
        # of ``motor_step`` re-issues ``goto {target: <id>}`` every tick; the
        # bridge reports ``arrived`` each time (the body is standing at/near the
        # thing) yet the target never surfaces as a benign affordance in reach
        # (e.g. the will beat named an id that isn't actually a harvestable
        # block here, or the pathfinder stops one tile short), so the reflex
        # never falls through to ``mine`` and loops ``goto`` forever on a target
        # it has already reached — the "arrived but no progress" freeze. We
        # count consecutive same-target arrivals and, past ``_STALE_ARRIVAL_TICKS``,
        # give up on that exact target for now: fall through to the directional
        # march (explore new ground) and surface ``arrived_idle`` so the slow
        # will beat can re-plan a reachable objective. Purely structural
        # (kind + exact id) — never keyword matching.
        self._named_target_arrival_key: str | None = None
        self._named_target_arrival_ticks = 0
        # ``_goal_key`` of a numeric destination we have already **reached**.
        # A goal's numeric destination is chosen *once* by the slow will beat
        # and stays static until the next beat (which, on the slow Selenium
        # engine, can be minutes away). Without marking it consumed the motor
        # would: reach it → ``wander`` on arrival → the wander drifts a few
        # metres past ``_ARRIVAL_RADIUS`` → the same static destination reads as
        # "pending" again → ``goto`` back to it → repeat, pacing the *same path
        # back and forth* forever. Once reached we record the key here so later
        # ticks fall through to the directional march (explore *beyond* the
        # point) instead of oscillating around it; a fresh goal (new key) makes
        # its destination live again. Purely structural (goal key) — no keywords.
        self._consumed_destination_key: str | None = None
        # Progress watchdog for an *unreachable* numeric destination. The
        # arrival/consume logic above only fires when the body gets within
        # ``_ARRIVAL_RADIUS`` of the waypoint. But a will-beat coordinate can be
        # physically unreachable (in water, across a ravine, on a cliff): the
        # pathfinder keeps closing to ~7 m, fails, resets, and re-approaches —
        # so ``remaining`` oscillates (7 → 46 → 9 → 41 …) and ``travel_pending``
        # never clears, pacing the *same path back and forth* forever without
        # ever "arriving". We track the best (smallest) horizontal distance seen
        # toward the current destination key and how many consecutive ticks have
        # failed to improve on it; once that stall count crosses
        # ``_STALE_TRAVEL_TICKS`` we consume the destination anyway so the body
        # gives up on the unreachable point and marches on. Purely numeric —
        # no keywords, no goal-text parsing.
        self._travel_dest_key: str | None = None
        self._travel_best_remaining: float | None = None
        self._travel_stall_ticks = 0
        # Physical-motion watchdog for a *stuck body*. The ``remaining``-based
        # watchdog above catches a destination whose distance oscillates but is
        # blind to the case the user hit: the pathfinder gives up on an
        # unwalkable coordinate (water edge, cliff, 1-block ledge) and the body
        # simply **stops moving** while the motor keeps re-issuing the same
        # ``goto`` every tick — the "synth stuck, same spot forever" report. The
        # ``remaining`` oscillation can even reset the stall counter, so it
        # never consumes the point. Here we watch the body's *actual* position:
        # if it fails to move at least ``_STUCK_MOVE_EPS`` blocks for
        # ``_STUCK_POSITION_TICKS`` consecutive ticks *while the motor is
        # emitting a travel action*, the target is unreachable regardless of any
        # distance number — give up on it and force the directional march.
        # Purely numeric (measured motion), no keywords, robust to skipped/laggy
        # ticks. This is watched **globally**, not per-branch: the stall can hit
        # a numeric waypoint, a named block/entity ``target`` (goal_target /
        # in-reach affordance goto) or any other ``goto`` — measuring the body's
        # own displacement covers every one of them uniformly.
        self._last_body_position: Dict[str, float] | None = None
        self._stuck_position_ticks = 0
        # Staticity ward state (see ``_STATIC_WARD_RADIUS`` / ``_staticity_ward``
        # and AGENTS.md §5c). ``_static_anchor`` is the reference point the body
        # is "parked" around; ``_static_ward_ticks`` counts how many consecutive
        # motor ticks it has lingered within ``_static_ward_radius`` of it. When
        # the body strays outside that radius the anchor moves and the counter
        # resets, so the ward only fires on *genuine* stasis, not on normal
        # travel. Runtime-configurable via the ``VESSEL_STATICITY_*`` keys.
        self._static_anchor: Dict[str, float] | None = None
        self._static_ward_ticks = 0
        self._static_ward_enabled: bool = True
        self._static_ward_radius: float = float(self._STATIC_WARD_RADIUS)
        self._static_ward_limit: int = int(self._STATIC_WARD_TICKS)
        # Structural 3-state feedback for the *last named target* the motor
        # tried to reach (``goal_target``: a block/entity id cognition chose
        # from the live scan). A named target can fail two very different ways
        # and the slow will beat must be able to tell them apart to re-plan:
        #   * ``not_found``   — the type is not visible/loaded (it was not in the
        #                       live scan when the goto failed) → pick another
        #                       target or set a destination to go look for it.
        #   * ``unreachable`` — it *was* in the scan but the pathfinder could not
        #                       reach it (buried, across water/a ravine) → aim
        #                       for an intermediate destination or a different
        #                       target.
        #   * ``arrived``     — reached it.
        # The classification is purely structural: ``ok`` from the bridge plus
        # whether ``target_name`` is present in the live scan's block/entity ids.
        # It is **never** derived by parsing the bridge's ``detail`` text (that
        # would be keyword matching). Surfaced verbatim in ``WorldState.extra``
        # (``last_target_result`` / ``last_target_name`` / ``last_target_kind``)
        # for the next will beat to read. All ``None`` until a named target is
        # attempted.
        self._last_target_result: str | None = None
        self._last_target_name: str | None = None
        self._last_target_kind: str | None = None
        # --- Self-preservation reflex state (in-memory, no DB) ------------
        # The survival guard runs at the very top of every motor tick and
        # pre-empts normal movement when the body is in danger (drowning, in
        # lava/fire, dead, or a hostile mob is near). See ``_survival_guard``
        # and AGENTS.md §5c / self-preservation. All state is structural
        # (numeric thresholds + game enum ids) — never keyword matching.
        #
        # Combat escalation: while defending against a hostile mob we count how
        # many consecutive attack ticks failed to shake it. Once the count
        # crosses ``_FIGHT_MAX_FAILS`` (or health drops below the flee
        # threshold, or fighting back is disabled) the reflex escalates from
        # DEFEND to FLEE and runs away instead. ``_fight_target`` remembers the
        # id of the mob currently being fought so the fail counter resets when
        # the threat changes or clears.
        self._fight_fail_count = 0
        self._fight_target: str | None = None
        # Structural snapshot of the most recent survival threat the reflex
        # acted on (threat kind + the numeric readings that triggered it), plus
        # a "just recovered" flag. Surfaced to the slow will beat via
        # ``WorldState.extra["threat"]`` so it can re-plan after safety (e.g.
        # "you nearly drowned mining underwater — mine from the surface next
        # time"). Purely structural — no goal-text parsing.
        self._last_survival_threat: str | None = None
        self._last_survival_reason: dict[str, Any] | None = None
        # One-tick cooldown so the reflex does not immediately re-fire the same
        # corrective action before the world state reflects it (anti-flap): e.g.
        # after issuing a respawn or a surface goto we let the next tick observe
        # the result before acting again.
        self._survival_cooldown_ticks = 0
        # Per-session resolved self-preservation settings (loaded on connect
        # from the VESSEL_SP_* config keys; default to the class constants).
        self._sp_enabled = True
        self._sp_low_oxygen: float = float(self._LOW_OXYGEN)
        self._sp_low_health: float = float(self._LOW_HEALTH_FLEE)
        self._sp_hostile_dist: float = float(self._HOSTILE_NEAR_DIST)
        self._sp_fight_back = True
        self._sp_fight_max_fails: int = int(self._FIGHT_MAX_FAILS)
        # Whether the reflex may use a carried ranged weapon (bow/crossbow) when
        # it has ammunition and the target is far enough to warrant it. When
        # off, combat is melee-only. Loaded from VESSEL_SP_USE_RANGED.
        self._sp_use_ranged = True
        # Minimum distance (blocks) at/above which the reflex prefers a ranged
        # shot over closing to melee (when a ranged weapon + ammo are carried).
        # Below this it closes and swings. From VESSEL_SP_RANGED_MIN_DIST.
        self._sp_ranged_min_dist: float = float(self._RANGED_MIN_DIST)
        # Whether a post-damage social/combat appraisal will beat may be raised
        # when the body takes a hit this tick. From VESSEL_SP_APPRAISAL_ENABLED.
        self._sp_appraisal_enabled = True
        # Power-ratio fight/flee threshold and weak-mob floor (structural
        # numeric only). When armed, the reflex engages a mob whose combat power
        # it matches (own_power / mob_power >= _sp_engage_ratio); when disarmed
        # it flees unless the mob is weaker than _sp_weak_mob_power. Loaded from
        # VESSEL_SP_ENGAGE_RATIO / VESSEL_SP_WEAK_MOB_POWER.
        self._sp_engage_ratio: float = float(self._ENGAGE_RATIO)
        self._sp_weak_mob_power: float = float(self._WEAK_MOB_POWER)
        # Whether the body proactively seeks/builds a shelter at night when
        # hostiles are around. A torch is NOT enough (mobs still path to an
        # exposed body); the reflex builds/digs an enclosed refuge or sleeps in
        # a roofed bed. Loaded from VESSEL_SP_NIGHT_SHELTER. When on, the reflex
        # also uses _sp_shelter_dist as the (wider) radius within which a
        # night-time hostile presence justifies sheltering.
        self._sp_night_shelter = True
        self._sp_shelter_dist: float = float(self._SHELTER_HOSTILE_DIST)
        # Base-retreat: at night with hostiles around, if a registered base is
        # within this many blocks the reflex heads BACK to it (reusing ``goto``)
        # instead of walling the body in wherever it happens to be standing —
        # which was burying Synth underground far from home (the "seppellita
        # sotto terra" bug). Sheltering-in-place stays only as the last resort
        # when no base is reachable. Loaded from VESSEL_BASE_RETREAT_RADIUS.
        self._base_enabled: bool = True
        self._base_retreat_radius: float = float(self._BASE_RETREAT_RADIUS)
        # Ender Dragon questline (directed reference milestones). When enabled,
        # the connector registers the questline at connect, surfaces the active
        # quest into the beats via extra["quest"], and structurally advances it
        # as the world satisfies each objective. Loaded from VESSEL_QUESTS_ENABLED.
        self._quests_enabled: bool = True
        # Anti-flap latch: once a shelter attempt succeeds this session-night we
        # do not keep re-issuing it every tick. Reset when day returns.
        self._sheltered_last_day: bool | None = None
        # Morning bunker-exit: if Synth sheltered underground overnight (dug a
        # bunker with no base), when DAY returns and the body is still buried
        # under a ceiling (no open sky) with no reachable base, carve a walkable
        # ascending staircase back to the surface. Loaded from
        # VESSEL_MORNING_EXIT_ENABLED. Latched per day so it fires once, not
        # every tick, until the body has surfaced (regains sky access).
        self._sp_morning_exit: bool = True
        self._surfaced_last_day: bool | None = None
        # Last observed health reading, used to detect "took damage this tick"
        # (health dropped vs the previous motor tick). Structural numeric delta,
        # never keyword logic. None until the first reading.
        self._last_health: float | None = None
        # Last observed inventory item→count map and surrounding block list,
        # cached from ``get_world_state`` so ``get_progression_context`` can
        # seed a starter-goal KB query from live telemetry only (item/block
        # ids), never chat text. Empty until the first snapshot.
        self._last_inventory_counts: dict[str, int] = {}
        self._last_blocks: list[dict[str, Any]] = []
        # Last observed dimension id (e.g. ``overworld`` / ``the_nether`` /
        # ``the_end``), cached from ``get_world_state`` so the progression-stage
        # detection can tell she has crossed into the Nether/End without a fresh
        # snapshot. Plain game id, never chat text. Empty until first snapshot.
        self._last_dimension: str = ""
        # Craft-material shortfall cue. When a ``craft`` fails for missing
        # ingredients, the bridge returns the exact shortfall (which item was
        # wanted and how many of each material are short); we latch it here with
        # a turn budget so the will/action beats can render a
        # "you wished to build X, you need have/need <material>" hint for a few
        # turns, then it self-clears. Structural (Minecraft item ids + counts),
        # never chat text. ``None`` when there is no pending shortfall.
        self._craft_deficit: Dict[str, Any] | None = None
        # How many turns (world-state builds) a fresh craft shortfall cue stays
        # rendered before it self-clears. Resolved from VESSEL_CRAFT_CUE_TURNS
        # on connect; falls back to the class default.
        self._craft_cue_turns: int = self._CRAFT_CUE_TURNS

    # Self-preservation thresholds (defaults; overridable per-connect via the
    # ``VESSEL_SP_*`` config keys resolved in ``connect``). All structural:
    # numeric readings on health/oxygen/distance, never keyword matching.
    #
    # Air below which the body heads for the surface. NOTE: in this
    # mineflayer/server runtime ``bot.oxygenLevel`` is reported on the 0..20
    # scale (full breath = 20), NOT in air-ticks (vanilla ~300) as some
    # versions do — runtime-confirmed values only ever range 0..20. The
    # threshold must therefore be on the 0..20 scale: a value near the top
    # (e.g. the old 200) makes ``oxygen <= threshold`` always true the instant
    # the head touches water, firing the drowning reflex at FULL air on every
    # tick spent near/along water and stalling autonomous play. 6 (~three
    # bubbles) leaves a few seconds of air to surface given the ~3s motor tick.
    _LOW_OXYGEN = 6
    # Health (0..20) below which the reflex flees a fight instead of trading
    # blows. Roughly three hearts — enough to survive the run to safety.
    _LOW_HEALTH_FLEE = 6.0
    # How close (blocks) a hostile mob must be for the reflex to engage it
    # (defend or flee). Beyond this it is left to the slow will beat.
    _HOSTILE_NEAR_DIST = 8.0
    # How far (blocks) to run when fleeing a threat. Kept deliberately large so
    # a low-health escape actually breaks mob aggro and puts real ground between
    # the body and the threat (a short hop just left Rekku still in range).
    _FLEE_DISTANCE = 48.0
    # Vanilla melee reach (~3 blocks) plus a little slack for lag — mirrors the
    # bridge ``attack`` MELEE_REACH. A mob beyond this cannot be hit by a
    # bare-handed/melee swing, so chasing it while carrying no ranged weapon is
    # pointless (and lethal against a mob that shoots back, e.g. a skeleton):
    # the reflex flees instead of swinging at empty air.
    _MELEE_REACH = 3.5
    # Consecutive failed defend ticks before escalating DEFEND → FLEE. Raised
    # from the historic 3: escalation to flight should be driven primarily by
    # LOW HEALTH (the body is actually losing the fight), with the fail counter
    # only a secondary safeguard against getting stuck swinging at an
    # unreachable mob — so it needs a longer fuse to actually let Synth win a
    # winnable fight rather than bail after three swings.
    _FIGHT_MAX_FAILS = 8
    # Distance (blocks) at/above which the reflex prefers a ranged shot over
    # closing to melee, when a bow/crossbow with ammo is carried. Below this it
    # is faster/safer to close and swing.
    _RANGED_MIN_DIST = 5.0
    # Structural offensive value credited to a usable ranged weapon (bow/
    # crossbow with ammo) in the own-power estimate. Vanilla bow damage is ~6-9
    # per charged hit; this numeric floor lets an archer clear the power gate
    # and reach the shoot branch instead of always fleeing. Never a keyword.
    _RANGED_OFFENSE = 6.0
    # Power-ratio combat threshold (own combat power / mob combat power). At or
    # above this the reflex judges the fight winnable and engages; below it the
    # body flees a mob it is outmatched by. ~1.0 = "fight when at least as
    # strong as the mob". Structural numeric ratio, never keyword logic. From
    # VESSEL_SP_ENGAGE_RATIO.
    _ENGAGE_RATIO = 1.0
    # Combat-power floor below which a mob is considered "weak" (trivial) — a
    # disarmed body will still turn and fight a weak mob rather than flee it.
    # Compared against the mob's structural power (see _mob_power). From
    # VESSEL_SP_WEAK_MOB_POWER.
    _WEAK_MOB_POWER = 6.0
    # Moderate default combat power assumed for a mob whose registry stats are
    # unavailable (older bridge / null max_health & attack_damage). Cautious but
    # not paralysing: it lets an armed, healthy body engage an unknown mob while
    # a disarmed body still treats it as non-trivial (above _WEAK_MOB_POWER).
    _DEFAULT_MOB_POWER = 12.0
    # How close (blocks) a hostile must be at NIGHT for the proactive shelter
    # reflex to trigger. Wider than _HOSTILE_NEAR_DIST so the body starts
    # walling itself in BEFORE the mob closes to melee, rather than only reacting
    # once it is already being hit. From VESSEL_SP_NIGHT_SHELTER radius default.
    _SHELTER_HOSTILE_DIST = 16.0
    # How far (blocks) a registered base may be for the night-retreat reflex to
    # head back to it (reusing ``goto``) instead of sheltering in place. Wide
    # enough to make coming home worthwhile, bounded so the body does not sprint
    # across the map into fresh danger. From VESSEL_BASE_RETREAT_RADIUS.
    _BASE_RETREAT_RADIUS = 64.0
    # How close (blocks) the keep-distance tactic tries to keep a special mob
    # (creeper/enderman) — reuse the flee vector but only when the mob is inside
    # this radius, so the body backs off without a full sprint away.
    _KEEP_DISTANCE = 6.0
    # Default number of turns (world-state builds) a craft-material shortfall
    # cue stays rendered in the will/action prompt before it self-clears. From
    # VESSEL_CRAFT_CUE_TURNS. Kept in the "a few turns" band the request asked
    # for (10-20), so the hint nudges Synth to gather the missing intermediate
    # material without lingering forever once it has moved on.
    _CRAFT_CUE_TURNS = 15
    # Max characters per in-world chat line. Minecraft vanilla chat rejects or
    # truncates anything past ~256 characters, so a long ``say`` is split on
    # word boundaries into multiple ≤256-char lines rather than hard-cut
    # mid-word. World-specific (Minecraft) — lives on the connector, not the
    # rift-vessel core.
    _CHAT_CHAR_LIMIT = 256

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_base_url(self, settings: Dict[str, Any]) -> str:
        host = settings.get("bridge_host") or config_registry.get_value(
            "MINECRAFT_BRIDGE_HOST",
            "127.0.0.1",
            group="plugins",
            component="minecraft_vessel",
        )
        port = settings.get("bridge_port") or config_registry.get_value(
            "MINECRAFT_BRIDGE_PORT",
            8137,
            group="plugins",
            component="minecraft_vessel",
        )
        host = str(host or "127.0.0.1")
        port = str(port or "8137")
        return f"http://{host}:{port}"

    @staticmethod
    def _resolve_server_target(settings: Dict[str, Any]) -> Dict[str, Any]:
        """Resolve the Minecraft *game server* address for this connect.

        Per-connect ``settings`` (``host``/``port``/``version``, seeded from the
        vessel connect action payload) override the plugin defaults; when absent
        the configured ``MINECRAFT_SERVER_HOST``/``MINECRAFT_SERVER_PORT``/
        ``MINECRAFT_SERVER_VERSION`` are used. The result is sent in the bridge
        ``/connect`` body so Synth can target a different server (and pin a
        protocol version) on demand without changing the saved defaults.

        Container-aware loopback remap: the most common setup is a user hosting
        a world ("Open to LAN" or a local server) on the *same machine* that
        runs SyntH, leaving the host as the natural ``127.0.0.1``/``localhost``.
        Inside a container that loopback points at the container itself, not the
        Docker host where the world listens, so the bot silently fails to join.
        When running in a container we therefore rewrite a loopback host to
        ``host.docker.internal`` (mapped to the host gateway via
        ``extra_hosts`` in docker-compose.yml) so the "just play on my machine"
        case works with no manual network configuration. A non-loopback host
        (LAN IP, Tailscale IP, remote server) is always left untouched.
        """
        host = settings.get("host") or config_registry.get_value(
            "MINECRAFT_SERVER_HOST",
            "127.0.0.1",
            group="plugins",
            component="minecraft_vessel",
        )
        host_str = str(host or "127.0.0.1").strip()
        if host_str.lower() in _LOOPBACK_HOSTS and _is_in_container():
            log_info(
                f"{LOG_PREFIX} remapping loopback server host '{host_str}' -> "
                f"'{_HOST_GATEWAY_NAME}' (running in container; targeting Docker host)"
            )
            host_str = _HOST_GATEWAY_NAME
        port = settings.get("port") or config_registry.get_value(
            "MINECRAFT_SERVER_PORT",
            44383,
            group="plugins",
            component="minecraft_vessel",
        )
        target: Dict[str, Any] = {"host": host_str}
        try:
            target["port"] = int(port)
        except (TypeError, ValueError):
            target["port"] = 44383
        # Optional protocol-version pin. When empty the bridge lets Mineflayer
        # auto-detect the server version; some servers announce a protocol the
        # bundled minecraft-data doesn't know ("No data available for version
        # X"), so pinning a supported version here is the general escape hatch.
        version = settings.get("version") or config_registry.get_value(
            "MINECRAFT_SERVER_VERSION",
            "",
            group="plugins",
            component="minecraft_vessel",
        )
        version_str = str(version or "").strip()
        if version_str:
            target["version"] = version_str
        return target

    async def _post(
        self,
        path: str,
        payload: Dict[str, Any],
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        if self._session is None:
            return {"ok": False, "detail": "no http session"}
        try:
            req_timeout = (
                aiohttp.ClientTimeout(total=timeout) if timeout is not None else None
            )
            async with self._session.post(
                f"{self._base_url}{path}", json=payload, timeout=req_timeout
            ) as resp:
                return await resp.json()
        except Exception as exc:
            log_debug(f"{LOG_PREFIX} POST {path} failed: {exc}")
            return {"ok": False, "detail": str(exc)}

    async def _get(self, path: str) -> Dict[str, Any]:
        if self._session is None:
            return {"ok": False, "detail": "no http session"}
        try:
            async with self._session.get(f"{self._base_url}{path}") as resp:
                return await resp.json()
        except Exception as exc:
            log_debug(f"{LOG_PREFIX} GET {path} failed: {exc}")
            return {"ok": False, "detail": str(exc)}

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def _ensure_bridge_running(self) -> None:
        """Start the local Mineflayer bridge on demand.

        The bridge subprocess is launched only when Synth actually enters the
        world (i.e. on connect), never at boot. Gated by whether the
        ``minecraft_vessel`` plugin is enabled (checked inside the provisioner);
        the call is idempotent (a no-op when the bridge is already running).
        """
        try:
            from interface.minecraft_provisioner import get_bridge_provisioner

            provisioner = get_bridge_provisioner()
            res = await provisioner.start()
            if not res.get("ok"):
                log_warning(
                    f"{LOG_PREFIX} bridge auto-start skipped: {res.get('detail')}"
                )
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"{LOG_PREFIX} bridge auto-start error: {exc}")

    async def connect(
        self,
        settings: Dict[str, Any],
        on_event: PerceptionCallback,
    ) -> bool:
        """Serialize connect/reattach attempts for the singleton bridge."""
        async with self._connect_lock:
            if self._connected:
                return True
            return await self._connect_impl(settings, on_event)

    async def _connect_impl(
        self,
        settings: Dict[str, Any],
        on_event: PerceptionCallback,
    ) -> bool:
        self._on_event = on_event
        self._connect_settings = dict(settings or {})
        self._base_url = self._resolve_base_url(settings or {})
        timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SEC)
        self._session = aiohttp.ClientSession(timeout=timeout)

        # Start the bridge on demand — only now that Synth is entering the
        # world. Then wait briefly for the freshly-spawned subprocess to come up.
        await self._ensure_bridge_running()

        # Reset any stale failure reason from a previous attempt.
        self.last_error = None

        health: Dict[str, Any] = {}
        for _ in range(10):
            health = await self._get("/health")
            if health.get("ok"):
                break
            await asyncio.sleep(1.0)
        if not health.get("ok"):
            detail = health.get("detail") or "bridge health check failed"
            self.last_error = (
                f"the Minecraft bridge did not come up at {self._base_url} ({detail})"
            )
            log_error(
                f"{LOG_PREFIX} bridge health check failed at {self._base_url}: {detail}"
            )
            await self._close_session()
            return False

        if not health.get("mineflayer", True):
            self.last_error = (
                "the Minecraft bridge is missing the 'mineflayer' Node module"
            )
            log_error(f"{LOG_PREFIX} bridge reports mineflayer not installed")
            await self._close_session()
            return False

        # ADOPT an already-connected bridge. A cold-start /connect can exceed
        # the bridge CONNECT_TIMEOUT_MS budget (npm/spawn + login + world load
        # on a distant server), which makes the first /connect report a timeout
        # and the core close the freshly-opened session — yet the bridge's
        # in-process auto-reconnect then settles the connection a moment later,
        # leaving a live bridge (/health connected:true) with no driven session
        # (no motor tick, no will beat). If a subsequent connect finds the
        # bridge already embodied for this environment, adopt it instead of
        # re-issuing /connect (which would bot.quit() and restart from scratch,
        # racing the same cold-start timeout again). Structural check only:
        # /health's own connected/environment fields, no keyword matching.
        env = str(health.get("environment") or "").strip().lower()
        if bool(health.get("connected")) and (not env or env == "minecraft"):
            log_info(
                f"{LOG_PREFIX} adopting already-connected bridge at {self._base_url} "
                f"(username={health.get('username')})"
            )
        else:
            # Tell the bridge to (re)connect to the Minecraft server. Pass the
            # resolved target (per-connect override or configured default) so
            # Synth can enter a different server on demand.
            target = self._resolve_server_target(settings or {})
            conn = await self._post(
                "/connect", target, timeout=_CONNECT_HTTP_TIMEOUT_SEC
            )
            if not conn.get("ok"):
                # A cold-start /connect can exceed the bridge's
                # CONNECT_TIMEOUT_MS budget (npm/spawn + login + world load on a
                # distant server) and report a timeout — yet the bridge's
                # in-process auto-reconnect frequently settles the connection a
                # moment later, leaving a live /health connected:true with no
                # driven session. Before giving up, re-probe /health a few
                # times: if the bridge has since entered the world, adopt it
                # instead of failing (which would close the session and leave
                # an orphaned, undriven bridge). Structural check only —
                # /health's own connected/environment fields, no keyword match.
                recovered = False
                for _ in range(15):
                    await asyncio.sleep(2.0)
                    late = await self._get("/health")
                    late_env = str(late.get("environment") or "").strip().lower()
                    if (
                        late.get("ok")
                        and bool(late.get("connected"))
                        and (not late_env or late_env == "minecraft")
                    ):
                        log_info(
                            f"{LOG_PREFIX} /connect reported timeout but the bridge "
                            f"is now in-world; adopting it "
                            f"(username={late.get('username')})"
                        )
                        recovered = True
                        break
                if not recovered:
                    detail = conn.get("detail") or "unknown error"
                    server = f"{target.get('host')}:{target.get('port')}"
                    self.last_error = (
                        f"could not enter the Minecraft server {server}: {detail}"
                    )
                    log_error(f"{LOG_PREFIX} bridge failed to connect: {detail}")
                    await self._close_session()
                    return False

        self._connected = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        log_info(f"{LOG_PREFIX} connected via {self._base_url}")

        # Scope goals by the concrete server we just entered so that logging
        # back into the same world resumes exactly where Synth left off, while
        # a different server keeps its own independent progression. Structural
        # identity (host:port slug), fail-safe — degrades to the shared scope.
        try:
            mc_goals.set_active_world(self.get_world_identity())
        except Exception as exc:
            log_debug(f"{LOG_PREFIX} goal world scope not set: {exc}")

        # Scope bases by the same concrete server so a returning Synth recalls
        # the homes it built there. Structural identity, fail-safe.
        try:
            mc_bases.set_active_world(self.get_world_identity())
        except Exception as exc:
            log_debug(f"{LOG_PREFIX} base world scope not set: {exc}")

        # Resolve the self-preservation thresholds for this session (fail-safe;
        # falls back to the class defaults on any read error).
        self._load_self_preservation_config()

        # Ensure the goal/progression table exists (idempotent, fail-safe). This
        # covers non-fresh installs where init-db.sql was not re-run.
        try:
            await mc_goals.init_goal_table()
        except Exception as exc:
            log_debug(f"{LOG_PREFIX} goal table init skipped: {exc}")

        # Ensure the base (home) table exists (idempotent, fail-safe).
        try:
            await mc_bases.init_base_table()
        except Exception as exc:
            log_debug(f"{LOG_PREFIX} base table init skipped: {exc}")

        # Register the Ender Dragon questline (idempotent; preserves existing
        # per-quest status/progress and promotes the first milestone to active
        # if none is). Reference-only direction, never a script. Fail-safe.
        if self._quests_enabled:
            try:
                res = await quests.register_questline()
                log_info(f"{LOG_PREFIX} questline registered: {res}")
            except Exception as exc:
                log_debug(f"{LOG_PREFIX} questline register skipped: {exc}")

        # Apply the configured skin once the bot is in-world. Best-effort: a
        # failure here (e.g. no server skin plugin) must never break the session.
        try:
            await self._apply_skin()
        except Exception as exc:
            log_debug(f"{LOG_PREFIX} skin apply skipped: {exc}")

        return True

    # TODO: remove once the skin-file upload path is confirmed obsolete. This
    # helper served the uploaded ``MINECRAFT_SKIN_FILE`` over HTTP; the WebUI
    # now takes a direct skin web URL (``MINECRAFT_SKIN_URL``), so it is no
    # longer called. Kept commented in case the upload flow is restored.
    # @staticmethod
    # def _skin_public_base_url() -> str:
    #     """Return the base URL the Minecraft server uses to fetch the skin file.
    #
    #     Prefers an explicit ``MINECRAFT_SKIN_PUBLIC_BASE_URL``; otherwise
    #     auto-derives ``http://<host>:<port>`` from the WebUI env config.
    #
    #     The MC server (or its skin plugin) fetches the texture over HTTP, so
    #     the host must be reachable *from the server's* point of view. A
    #     loopback host (``127.0.0.1``/``localhost``/``0.0.0.0``) only works
    #     when the server runs on the very same machine — a remote or
    #     containerised server cannot open it. When the derived host is a
    #     loopback we therefore try to substitute the machine's primary LAN IP
    #     (see :func:`_detect_lan_ip`) so the skin works out of the box on the
    #     common "SyntH host + server on the LAN" setup. Set
    #     ``MINECRAFT_SKIN_PUBLIC_BASE_URL`` explicitly to override (e.g. a
    #     VPN/public address or a reverse-proxy URL).
    #     """
    #     explicit = str(
    #         config_registry.get_value("MINECRAFT_SKIN_PUBLIC_BASE_URL", "") or ""
    #     ).strip()
    #     if explicit:
    #         return explicit.rstrip("/")
    #
    #     host = (os.environ.get("SYNTH_WEBUI_HOST") or "").strip()
    #     if not host or host.lower() in _LOOPBACK_HOSTS:
    #         lan_ip = _detect_lan_ip()
    #         host = lan_ip or "127.0.0.1"
    #     port = (
    #         os.environ.get("SYNTH_WEBUI_HTTP_PORT")
    #         or os.environ.get("SYNTH_WEBUI_PORT")
    #         or os.environ.get("PORT")
    #         or "8080"
    #     ).strip()
    #     return f"http://{host}:{port}"

    @staticmethod
    def _skin_command_templates() -> list[str]:
        """Return the ordered list of skin chat-command templates to try.

        Different server-side skin providers use different command syntaxes for
        a URL-based skin — e.g. the classic *SkinsRestorer* plugin uses
        ``/skin url <url>`` while the *SkinRestorer* mod (Lionarius/Suiranoil)
        uses ``/skin set web <model> "<url>"``. To work out of the box across
        providers without any keyword/regex logic, the connector runs every
        configured template at spawn and lets the server accept whichever one it
        understands (unknown commands are simply ignored by the server).

        Resolution order (first non-empty wins):

        1. ``MINECRAFT_SKIN_COMMAND_TEMPLATES`` — a newline-separated list of
           templates (advanced). This is the multi-provider knob.
        2. ``MINECRAFT_SKIN_COMMAND_TEMPLATE`` — the legacy single-template key,
           kept for backward compatibility.
        3. The built-in default set covering both known providers.

        Each template supports the ``{url}`` and ``{model}`` placeholders.
        """
        multi = str(
            config_registry.get_value("MINECRAFT_SKIN_COMMAND_TEMPLATES", "") or ""
        ).strip()
        if multi:
            templates = [line.strip() for line in multi.splitlines() if line.strip()]
            if templates:
                return templates

        single = str(
            config_registry.get_value("MINECRAFT_SKIN_COMMAND_TEMPLATE", "") or ""
        ).strip()
        if single:
            return [single]

        # Built-in defaults: try every known provider syntax at login.
        return [
            '/skin set web {model} "{url}"',  # SkinRestorer mod (Lionarius)
            "/skin url {url}",  # SkinsRestorer plugin
        ]

    @staticmethod
    def _validate_skin_url(url: str) -> str | None:
        """Return a human warning if ``url`` is unlikely to be a direct PNG.

        Server-side skin plugins fetch the texture over HTTP and expect a
        **direct link to a ``.png`` file**, not a web page (e.g. a
        minecraftskins.com skin page returns HTML and is silently rejected by
        the plugin). This is a best-effort, non-blocking sanity check: the URL
        is always saved/used regardless, but a clear warning is logged when it
        looks wrong so the operator knows why the skin didn't apply.

        Returns ``None`` when the URL looks valid, otherwise a short reason.
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return (
                "URL scheme is not http/https — the skin plugin needs a direct "
                "web link to a PNG file"
            )
        if not parsed.netloc:
            return "URL has no host — provide a full direct link to a PNG file"
        # Compare against the path only, ignoring any ?query / #fragment so a
        # link like '.../skin.png?v=2' still counts as valid.
        if not parsed.path.lower().endswith(".png"):
            return (
                "URL does not point to a .png file — many skin sites give a page "
                "link, not the direct texture; use the direct '.png' image URL"
            )
        return None

    async def _apply_skin(self) -> None:
        """Request the skin from a server-side skin plugin using a web URL.

        Offline-mode Mineflayer bots cannot set their own texture client-side;
        the skin is applied by the server. The operator provides a direct web
        URL to a skin PNG in the WebUI (``MINECRAFT_SKIN_URL``); the URL is fed
        to one or more configurable chat commands (see
        :meth:`_skin_command_templates`) so it works across skin plugins/mods
        and locales without any keyword logic. Every configured template is run
        at spawn — the server accepts the one it understands and ignores the
        rest. If no skin URL is set, nothing happens.
        """
        skin_url = str(
            config_registry.get_value("MINECRAFT_SKIN_URL", "") or ""
        ).strip()
        if not skin_url:
            return

        # Non-blocking validation: the URL is still used even if it looks wrong,
        # but we warn so the operator understands why the skin may not apply.
        skin_warning = self._validate_skin_url(skin_url)
        if skin_warning:
            log_warning(
                f"{LOG_PREFIX} skin URL may be invalid ({skin_warning}): {skin_url}"
            )

        model = str(
            config_registry.get_value("MINECRAFT_SKIN_MODEL", "classic") or "classic"
        ).strip()

        seen: set[str] = set()
        for template in self._skin_command_templates():
            command = (
                template.replace("{url}", skin_url).replace("{model}", model).strip()
            )
            if not command or command in seen:
                continue
            seen.add(command)

            res = await self._post(
                "/cmd", {"action": "skin", "payload": {"command": command}}
            )
            if res.get("ok"):
                log_info(f"{LOG_PREFIX} skin command sent: {command}")
            else:
                log_warning(
                    f"{LOG_PREFIX} skin command failed "
                    f"(server skin plugin required?): {res.get('detail')}"
                )

    async def disconnect(self) -> None:
        self._connected = False
        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except (asyncio.CancelledError, Exception):
                pass
            self._poll_task = None
        try:
            await self._post("/disconnect", {})
        except Exception:
            pass
        await self._close_session()
        # Reset the goal scope to the shared "none" world so a later
        # scope-agnostic call (e.g. before the next connect) doesn't leak the
        # previous server's identity. Fail-safe.
        try:
            mc_goals.set_active_world(None)
        except Exception:
            pass
        try:
            mc_bases.set_active_world(None)
        except Exception:
            pass
        log_info(f"{LOG_PREFIX} disconnected")

    async def _close_session(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    # ------------------------------------------------------------------
    # Perception polling
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        # Track consecutive poll failures so a dead bridge/world client is
        # detected instead of spinning forever with ``is_connected`` stuck True.
        consecutive_failures = 0
        # Track consecutive ``/health`` reads that say the bridge is alive but
        # its mineflayer bot is NOT embodied. The ``/events`` poll above hits the
        # Node bridge process, which survives a server-side bot drop (the bridge
        # auto-reconnects in-process), so a dropped bot never registers as a poll
        # failure — ``/events`` keeps returning ``{"events": []}`` happily. Probing
        # ``/health.connected`` each tick is the only reliable liveness signal for
        # "actually in the world", closing the gap where Synth believes it is
        # online after the bot silently fell out of the server.
        health_false_streak = 0
        while self._connected:
            try:
                res = await self._get("/events")
                # A successful poll clears the failure streak.
                consecutive_failures = 0
                # Probe embodiment liveness via /health (structural boolean, no
                # keyword logic). A live bridge (ok:true) whose bot is no longer
                # in the world (connected:false) must eventually flip us
                # disconnected so the interface's grace sweep can close the stale
                # session — the /events poll alone can never detect this.
                try:
                    health = await self._get("/health")
                except Exception:
                    health = None
                if isinstance(health, dict) and health.get("ok"):
                    if bool(health.get("connected")):
                        health_false_streak = 0
                    else:
                        health_false_streak += 1
                        log_debug(
                            f"{LOG_PREFIX} bridge reports not embodied "
                            f"({health_false_streak}/{_MAX_HEALTH_FALSE_STREAK})"
                        )
                        if health_false_streak >= _MAX_HEALTH_FALSE_STREAK:
                            self._connected = False
                            self.last_error = (
                                "the Minecraft bridge is alive but its bot is no "
                                f"longer in the world after {health_false_streak} "
                                "checks"
                            )
                            log_warning(
                                f"{LOG_PREFIX} bot no longer embodied after "
                                f"{health_false_streak} health checks — marking "
                                "disconnected"
                            )
                            break
                events = res.get("events") if isinstance(res, dict) else None
                if events:
                    for raw in events:
                        if isinstance(raw, dict):
                            log_debug(
                                f"{LOG_PREFIX} polled event: "
                                f"type={raw.get('event_type')!r} "
                                f"actor={raw.get('actor')!r} "
                                f"summary={str(raw.get('summary'))[:80]!r}"
                            )
                            # Re-apply the skin on every (re)spawn — respawn
                            # after death or a reconnect drops the previous
                            # skin. Best-effort and idempotent.
                            if str(raw.get("event_type")) == "spawn":
                                try:
                                    await self._apply_skin()
                                except Exception as exc:
                                    log_debug(
                                        f"{LOG_PREFIX} skin re-apply on spawn "
                                        f"skipped: {exc}"
                                    )
                        await self._dispatch_event(raw)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                consecutive_failures += 1
                log_debug(
                    f"{LOG_PREFIX} poll error "
                    f"({consecutive_failures}/{_MAX_POLL_FAILURES}): {exc}"
                )
                if consecutive_failures >= _MAX_POLL_FAILURES:
                    # The bridge/world client is unreachable. Flip the liveness
                    # flag so the interface's disconnect-grace sweep can close
                    # the stale session and autonomous beats stop firing.
                    self._connected = False
                    self.last_error = (
                        f"lost contact with the Minecraft bridge after "
                        f"{consecutive_failures} failed polls: {exc}"
                    )
                    log_warning(
                        f"{LOG_PREFIX} bridge unreachable after "
                        f"{consecutive_failures} failed polls — marking "
                        "disconnected"
                    )
                    break
            await asyncio.sleep(_POLL_INTERVAL_SEC)

    async def _dispatch_event(self, raw: Dict[str, Any]) -> None:
        if not isinstance(raw, dict):
            return
        # A `kill` event advances the questline's kill objectives (e.g. the
        # Ender Dragon). Structural: the mob game id comes straight from the
        # bridge, never a keyword scan. Fail-safe — a quest-store error must
        # never stop the perception from reaching the chain.
        if str(raw.get("event_type")) == "kill":
            data = raw.get("data") or {}
            mob = data.get("mob") if isinstance(data, dict) else None
            if isinstance(mob, str) and mob:
                try:
                    await self.on_entity_killed(mob)
                except Exception as exc:  # pragma: no cover - defensive
                    log_debug(f"{LOG_PREFIX} kill objective advance failed: {exc}")
        if not self._on_event:
            return
        try:
            event = PerceptionEvent(
                environment=str(raw.get("environment") or ENVIRONMENT),
                event_type=str(raw.get("event_type") or "event"),
                summary=str(raw.get("summary") or ""),
                actor=raw.get("actor"),
                salience=raw.get("salience"),
                data=raw.get("data") or {},
            )
            # The perception callback may be sync or async (the Vessel plugin
            # wires an ``async def _on_event``). Await it when it returns an
            # awaitable so the event actually reaches the message chain —
            # calling a coroutine without awaiting silently drops the event.
            result = self._on_event(event)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} failed to dispatch event: {exc}")

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    # Goal/progression verbs handled natively by the connector (they touch the
    # ``minecraft_goals`` table, not the Node bridge). Everything else is a
    # bridge command forwarded verbatim to ``POST /cmd``.
    _GOAL_VERBS = frozenset({"set_goal", "goals", "update_goal"})
    _BASE_VERBS = frozenset({"set_base", "list_bases"})

    async def act(
        self,
        action: str,
        payload: Dict[str, Any],
    ) -> VesselActionResult:
        if not self._connected:
            return VesselActionResult(ok=False, detail="not connected to a world")
        if action == "lookup_knowledge":
            return await self._act_lookup_knowledge(payload or {})
        if action in self._GOAL_VERBS:
            return await self._act_goal(action, payload or {})
        if action == "build_base":
            return await self._act_build_base(payload or {})
        if action in self._BASE_VERBS:
            return await self._act_base(action, payload or {})
        if action == "say":
            return await self._act_say(payload or {})
        res = await self._post("/cmd", {"action": action, "payload": payload or {}})
        if action == "craft":
            self._update_craft_deficit(bool(res.get("ok")), res.get("data") or {})
        return VesselActionResult(
            ok=bool(res.get("ok")),
            detail=res.get("detail"),
            data=res.get("data") or {},
        )

    def _update_craft_deficit(self, ok: bool, data: Dict[str, Any]) -> None:
        """Latch or clear the craft-material shortfall cue after a craft attempt.

        On a **successful** craft we clear any pending shortfall (Synth got what
        it wanted). On a **failed** craft the bridge returns the exact shortfall
        — ``{"wanted": <item id>, "missing": [{"item", "have", "need"}, ...]}`` —
        which we latch here with a fresh turn budget so the will/action beats can
        render a "you wished to build X, you need have/need <material>" hint for
        a few turns. Purely structural (Minecraft item ids + counts), never chat
        text; fully fail-safe.
        """
        if ok:
            self._craft_deficit = None
            return
        try:
            wanted = str(data.get("wanted") or "").strip()
            raw_missing = data.get("missing")
            if not wanted or not isinstance(raw_missing, list) or not raw_missing:
                return
            missing: list[dict[str, Any]] = []
            for entry in raw_missing:
                if not isinstance(entry, dict):
                    continue
                item = str(entry.get("item") or "").strip()
                if not item:
                    continue
                try:
                    have = int(entry.get("have") or 0)
                    need = int(entry.get("need") or 0)
                except (TypeError, ValueError):
                    continue
                if need <= 0:
                    continue
                missing.append({"item": item, "have": have, "need": need})
            if not missing:
                return
            self._craft_deficit = {
                "wanted": wanted,
                "missing": missing,
                "turns_left": int(self._craft_cue_turns),
            }
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} craft deficit latch failed: {exc}")

    def _consume_craft_deficit(
        self, inventory_counts: Dict[str, int]
    ) -> Dict[str, Any] | None:
        """Return the active craft-shortfall cue for this turn, or ``None``.

        Called once per ``get_world_state`` (one perception/beat turn). Refreshes
        each missing ingredient's ``have`` from the live inventory, drops any
        ingredient now fully satisfied, decrements the turn budget, and clears
        the whole cue when the budget runs out or every ingredient is satisfied.
        Structural only (item ids + counts). Returns a snapshot dict suitable for
        ``WorldState.extra["craft_deficit"]`` or ``None`` when nothing to show.
        """
        deficit = self._craft_deficit
        if not deficit:
            return None
        try:
            refreshed: list[dict[str, Any]] = []
            for entry in deficit.get("missing", []):
                item = str(entry.get("item") or "")
                need = int(entry.get("need") or 0)
                have = int(inventory_counts.get(item, 0)) if inventory_counts else 0
                if need > 0 and have < need:
                    refreshed.append({"item": item, "have": have, "need": need})
            turns_left = int(deficit.get("turns_left") or 0) - 1
            if not refreshed or turns_left <= 0:
                self._craft_deficit = None
                if not refreshed:
                    return None
                # Last render before it self-clears.
                return {"wanted": deficit.get("wanted"), "missing": refreshed}
            deficit["missing"] = refreshed
            deficit["turns_left"] = turns_left
            return {"wanted": deficit.get("wanted"), "missing": refreshed}
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} craft deficit consume failed: {exc}")
            self._craft_deficit = None
            return None

    @staticmethod
    def _split_chat_text(text: str, limit: int) -> list[str]:
        """Split ``text`` into chunks no longer than ``limit`` characters.

        Minecraft vanilla chat rejects/truncates anything past ~256 characters,
        which produced ugly mid-word cut-offs (a long ``say`` was hard-sliced by
        the bridge). This splits on whitespace boundaries so each chunk is a
        clean, readable line; a single word longer than ``limit`` is hard-split
        as a last resort. Purely structural — never inspects word meaning.
        """
        text = text.strip()
        if not text:
            return []
        if len(text) <= limit:
            return [text]
        chunks: list[str] = []
        current = ""
        for word in text.split():
            # A single oversized word: flush, then hard-split it.
            if len(word) > limit:
                if current:
                    chunks.append(current)
                    current = ""
                for i in range(0, len(word), limit):
                    chunks.append(word[i : i + limit])
                continue
            candidate = f"{current} {word}".strip() if current else word
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    chunks.append(current)
                current = word
        if current:
            chunks.append(current)
        return chunks

    async def _act_say(self, payload: Dict[str, Any]) -> VesselActionResult:
        async with self._say_lock:
            result = await self._act_say_unlocked(payload)
            await asyncio.sleep(_SAY_GAP_SEC)
            return result

    async def _act_say_unlocked(self, payload: Dict[str, Any]) -> VesselActionResult:
        """Send in-world chat, splitting long text into clean ≤256-char lines.

        A chat char limit is a world-specific concern (Minecraft's vanilla cap is
        256), so it lives on the connector rather than the rift-vessel core. The
        text is split on word boundaries and each chunk is sent as its own chat
        line, so a long message is delivered in full instead of being hard-cut
        mid-word by the bridge.
        """
        text = str(payload.get("text") or "").strip()
        if not text:
            return VesselActionResult(ok=False, detail="empty text")
        chunks = self._split_chat_text(text, self._CHAT_CHAR_LIMIT)
        last: Dict[str, Any] = {}
        sent: list[str] = []
        for chunk in chunks:
            line_payload = dict(payload)
            line_payload["text"] = chunk
            res: Dict[str, Any] = {}
            for attempt in range(_SAY_RETRY_ATTEMPTS + 1):
                res = await self._post(
                    "/cmd", {"action": "say", "payload": line_payload}
                )
                if res.get("ok"):
                    break
                detail = str(res.get("detail") or "")
                if (
                    "not connected to a world" not in detail.lower()
                    or attempt >= _SAY_RETRY_ATTEMPTS
                ):
                    break
                await asyncio.sleep(_SAY_RETRY_DELAY_SEC)
            last = res
            if res.get("ok"):
                sent.append(chunk)
            else:
                return VesselActionResult(
                    ok=False,
                    detail=res.get("detail") or "say failed",
                    data={"sent": sent, "text": text},
                )
        return VesselActionResult(
            ok=bool(last.get("ok", True)),
            detail=last.get("detail") or "said",
            data={"sent": sent, "chunks": len(sent), "text": text},
        )

    @staticmethod
    def _extract_destination(
        payload: Dict[str, Any],
    ) -> Dict[str, float] | None:
        """Build a ``{x, z}`` (+ optional ``y``) travel target from a payload.

        Reads the flat ``destination_x`` / ``destination_z`` / ``destination_y``
        fields the will beat may set on ``set_goal`` / ``update_goal``. Purely
        numeric — never inspects any free text. Returns ``None`` when no usable
        pair of coordinates is present (so callers leave the stored destination
        untouched on ``update_goal``).
        """
        try:
            x = payload.get("destination_x")
            z = payload.get("destination_z")
            if x is None or z is None:
                return None
            out: Dict[str, float] = {"x": float(x), "z": float(z)}
            y = payload.get("destination_y")
            if y is not None:
                out["y"] = float(y)
            return out
        except (TypeError, ValueError):
            return None

    # Canonical payload key for a free-text goal, plus the alias keys a weaker
    # LLM tends to emit instead ("goal", "goal_text", "text", "objective",
    # "description_text"). These are *action payload field names* (structural
    # JSON keys of the action schema), NOT natural-language content keywords, so
    # accepting them keeps the multi-language / keyword-free rule intact while
    # stopping a mis-keyed set_goal from silently persisting nothing (the model
    # sometimes puts the description under "goal"/"goal_text" — see the empty
    # goals-table bug). Order is the resolution priority; the canonical key wins.
    _GOAL_DESCRIPTION_KEYS = (
        "description",
        "goal",
        "goal_text",
        "objective",
        "description_text",
        "text",
    )

    @classmethod
    def _extract_goal_description(cls, payload: Dict[str, Any]) -> str:
        """Return the free-text goal from the canonical key or a known alias.

        Reads ``description`` first, then falls back to the structural alias
        payload keys a weaker model emits (``goal``, ``goal_text``, …). Purely
        key-based — never inspects the *value* for keywords — so it is safe in a
        multi-language deployment. Returns the first non-empty stripped string.
        """
        for key in cls._GOAL_DESCRIPTION_KEYS:
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @classmethod
    def _resolve_goal_target(
        cls, payload: Dict[str, Any], description: str
    ) -> tuple[str | None, str | None]:
        """Resolve ``(target_kind, target_name)`` for a set/update_goal payload.

        Cognition's explicit ``target_kind`` / ``target_name`` always win. When
        the (weaker vessel-scope) model omits them, fall back to deriving them
        from the free-text goal ``description`` by matching **Minecraft item /
        block / mob names** — the single, user-authorized exception to the
        keyword-free rule (see :mod:`plugins.rift_vessel.minecraft.target_names`).
        Without a target the motor reflex only wanders, so this is what makes
        autonomous play actually progress toward the authored goal.
        """
        kind = payload.get("target_kind")
        name = payload.get("target_name")
        if (
            isinstance(kind, str)
            and kind.strip()
            and isinstance(name, str)
            and name.strip()
        ):
            return kind.strip(), name.strip()
        derived = mc_target_names.derive_target(description)
        if derived is not None:
            log_info(
                f"{LOG_PREFIX} derived goal target "
                f"{derived['target_kind']}={derived['target_name']} "
                f"from free-text goal"
            )
            return derived["target_kind"], derived["target_name"]
        return kind, name

    async def _act_goal(
        self,
        action: str,
        payload: Dict[str, Any],
    ) -> VesselActionResult:
        """Handle the native goal verbs (``set_goal`` / ``goals`` / ``update_goal``).

        Goals are **self-authored free text** — there is no catalogue. ``goals``
        reports Synth's current objective and its own recent goal history;
        ``set_goal`` adopts a new free-text objective; ``update_goal`` lets Synth
        note progress on, complete, or drop the active goal (Synth judges its own
        progress). All are fail-safe — a DB hiccup degrades to an ``ok=False``
        result and never raises into the message chain.
        """
        try:
            if action == "goals":
                active = await mc_goals.get_active_goal()
                recent = await mc_goals.list_recent_goals()
                return VesselActionResult(
                    ok=True,
                    detail="listed goals",
                    data={
                        "current_goal": active,
                        "recent_goals": recent,
                    },
                )
            if action == "update_goal":
                upd_desc = self._extract_goal_description(payload) or (
                    payload.get("note") if isinstance(payload.get("note"), str) else ""
                )
                upd_kind, upd_name = self._resolve_goal_target(payload, upd_desc or "")
                result = await mc_goals.update_active_goal(
                    note=payload.get("note"),
                    status=payload.get("status"),
                    destination=await self._resolve_travel_destination(payload),
                    steps=payload.get("steps"),
                    current_step=payload.get("current_step"),
                    advance=bool(payload.get("advance")),
                    target_kind=upd_kind,
                    target_name=upd_name,
                )
                ok = result.get("status") == "ok"
                return VesselActionResult(
                    ok=ok,
                    detail=result.get("message") or "goal updated",
                    data=result,
                )
            # set_goal — free-text objective authored by Synth. Accept the
            # canonical `description` key OR the structural alias keys a weaker
            # model emits (goal/goal_text/objective/…) so a mis-keyed payload
            # still persists a goal instead of silently no-op'ing (empty
            # goals-table bug: the model put the text under "goal"/"goal_text").
            description = self._extract_goal_description(payload)
            if not description:
                return VesselActionResult(
                    ok=False, detail="set_goal requires a free-text description"
                )
            # NOTE: `steps` is deliberately NOT threaded from set_goal. The
            # slow will beat authors goals as free text only; the ordered,
            # tool-first plan is filled in by the out-of-band Goal-expander
            # Drone via update_goal (it consults the knowledge base first).
            # Letting the will beat pass `steps` here would pre-fill a vague
            # plan and gate the expander out (_goal_needs_expansion -> False),
            # so the goal would never be expanded. Structural, keyword-free.
            set_kind, set_name = self._resolve_goal_target(payload, description)
            result = await mc_goals.set_goal(
                description,
                self._session_id,
                note=payload.get("note"),
                destination=await self._resolve_travel_destination(payload),
                target_kind=set_kind,
                target_name=set_name,
            )
            ok = result.get("status") == "ok"
            return VesselActionResult(
                ok=ok,
                detail=result.get("message") or "goal adopted",
                data=result,
            )
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"{LOG_PREFIX} goal verb '{action}' failed: {exc}")
            return VesselActionResult(ok=False, detail=str(exc))

    @staticmethod
    def _extract_anchor(payload: Dict[str, Any]) -> Dict[str, float] | None:
        """Build a ``{x, y, z}`` base anchor from flat numeric payload fields.

        Reads ``x`` / ``y`` / ``z`` (all three required). Purely numeric — never
        inspects free text. Returns ``None`` when a usable triple is absent, so
        the caller can fall back to the body's live position.
        """
        try:
            x = payload.get("x")
            y = payload.get("y")
            z = payload.get("z")
            if x is None or y is None or z is None:
                return None
            return {"x": float(x), "y": float(y), "z": float(z)}
        except (TypeError, ValueError):
            return None

    async def _live_position(self) -> Dict[str, float] | None:
        """Read the body's current ``{x, y, z}`` position from the bridge.

        Fail-safe: any error (no connection, unreadable snapshot) returns None.
        """
        try:
            res = await self._post("/cmd", {"action": "status", "payload": {}})
            if not res.get("ok"):
                return None
            pos = (res.get("data") or {}).get("position")
            if not isinstance(pos, dict):
                return None
            x = pos.get("x")
            y = pos.get("y")
            z = pos.get("z")
            if x is None or y is None or z is None:
                return None
            return {"x": float(x), "y": float(y), "z": float(z)}
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} could not read live position: {exc}")
            return None

    async def _act_base(
        self,
        action: str,
        payload: Dict[str, Any],
    ) -> VesselActionResult:
        """Handle the native base (home) verbs (``set_base`` / ``list_bases``).

        A base is a place Synth chose to build up, store resources in, shelter
        or sleep at — Synth names and places it freely (there is no catalogue).
        ``list_bases`` reports the bases Synth registered in this world;
        ``set_base`` claims/updates a base at an explicit ``{x, y, z}`` anchor
        (or, when omitted, the body's current position). Both are fail-safe — a
        DB hiccup degrades to an ``ok=False`` result and never raises into the
        message chain.
        """
        try:
            if action == "list_bases":
                bases = await mc_bases.list_bases()
                return VesselActionResult(
                    ok=True,
                    detail=f"{len(bases)} base(s)",
                    data={"bases": bases},
                )
            # set_base — claim/update a home. The name is free text Synth chose;
            # the anchor is numeric coordinates (explicit fields, else the live
            # body position). Structural, keyword-free.
            name = payload.get("name")
            if not isinstance(name, str) or not name.strip():
                return VesselActionResult(ok=False, detail="set_base requires a name")
            anchor = self._extract_anchor(payload)
            if anchor is None:
                anchor = await self._live_position()
            kind = payload.get("kind") if isinstance(payload.get("kind"), str) else None
            note = payload.get("note") if isinstance(payload.get("note"), str) else None
            result = await mc_bases.set_base(
                name.strip(),
                anchor=anchor,
                kind=kind,
                note=note,
                session_id=self._session_id,
            )
            ok = result.get("status") == "ok"
            return VesselActionResult(
                ok=ok,
                detail=result.get("message") or "base registered",
                data=result,
            )
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"{LOG_PREFIX} base verb '{action}' failed: {exc}")
            return VesselActionResult(ok=False, detail=str(exc))

    async def _act_build_base(self, payload: Dict[str, Any]) -> VesselActionResult:
        """Build a first shelter, then register the result as a base (home).

        This is the Fase-2 counterpart to ``set_base`` (which only *claims* a
        spot): it derives a bounded shelter layout from the body's live
        inventory (:mod:`base_spec`), forwards the block list to the Node bridge
        ``build_base`` verb (which physically places every block), and — on a
        successful (even partial) build — registers the shelter's interior
        anchor and bounding box in the core base store so night-retreat and
        ``list_bases`` can find it.

        The build origin is an explicit ``{x, y, z}`` payload triple when given,
        otherwise the body's live position. Materials come from the inventory
        and layout is pure grid math — no free text is ever inspected (only
        canonical Minecraft block ids, which the scope rules permit as
        structural). Fully fail-safe: a missing position, an empty inventory, or
        a bridge hiccup degrades to an ``ok=False`` result and never raises into
        the message chain.
        """
        try:
            name = payload.get("name")
            if not isinstance(name, str) or not name.strip():
                return VesselActionResult(ok=False, detail="build_base requires a name")
            # Build origin: explicit coords, else the live body position.
            origin_pos = self._extract_anchor(payload)
            if origin_pos is None:
                origin_pos = await self._live_position()
            if origin_pos is None:
                return VesselActionResult(
                    ok=False, detail="build_base could not read a build position"
                )
            # Live inventory so the layout uses materials actually carried.
            res_status = await self._post("/cmd", {"action": "status", "payload": {}})
            inventory = (res_status.get("data") or {}).get("inventory") or []
            inventory_counts = self._inventory_counts(inventory)

            layout = base_spec.derive_base_layout(origin_pos, inventory_counts)
            if not layout.get("ok"):
                missing = layout.get("missing") or []
                return VesselActionResult(
                    ok=False,
                    detail=(
                        "cannot build a base yet — missing materials: "
                        + ", ".join(str(m) for m in missing)
                    ),
                    data={"missing": missing},
                )

            # Forward the physical build to the bridge.
            bridge_payload: Dict[str, Any] = {
                "blocks": layout.get("blocks") or [],
                "anchor": layout.get("anchor"),
            }
            for key in ("door", "torch", "crafting_table", "bed"):
                if layout.get(key):
                    bridge_payload[key] = layout[key]
            res = await self._post(
                "/cmd", {"action": "build_base", "payload": bridge_payload}
            )
            built = bool(res.get("ok"))
            data = res.get("data") or {}

            # Register the base on a successful (even partial) build so the body
            # can retreat/sleep here. The interior-centre anchor and the outer
            # bounding box come from the deterministic layout.
            registered: Dict[str, Any] | None = None
            if built:
                anchor = layout.get("anchor")
                box = layout.get("box")
                kind = (
                    payload.get("kind")
                    if isinstance(payload.get("kind"), str)
                    else "home"
                )
                note = (
                    payload.get("note")
                    if isinstance(payload.get("note"), str)
                    else None
                )
                registered = await mc_bases.set_base(
                    name.strip(),
                    anchor=anchor,
                    box=box,
                    kind=kind,
                    note=note,
                    session_id=self._session_id,
                )

            detail = res.get("detail") or (
                "base built" if built else "base build failed"
            )
            return VesselActionResult(
                ok=built,
                detail=detail,
                data={"build": data, "base": registered},
            )
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"{LOG_PREFIX} build_base failed: {exc}")
            return VesselActionResult(ok=False, detail=str(exc))

    async def _resolve_travel_destination(
        self,
        payload: Dict[str, Any],
    ) -> Dict[str, float] | None:
        """Extract the payload destination and reproject it to a real distance.

        Combines :meth:`_extract_destination` (numeric-only, no free text) with
        :meth:`_reproject_destination` so a too-close coordinate the will beat
        chose is pushed out along the same heading to ``_MIN_TRAVEL_DISTANCE``.
        This is the fix that makes the body actually walk when cognition sets an
        ambitious goal but a near destination: storage keeps the *direction*
        Synth chose, the reflex gets a genuinely distant target. Fail-safe — a
        missing/unreadable live position leaves the destination untouched.
        """
        dest = self._extract_destination(payload)
        if dest is None:
            return None
        position: Any = None
        try:
            res = await self._post("/cmd", {"action": "status", "payload": {}})
            if res.get("ok"):
                position = (res.get("data") or {}).get("position")
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} could not read position for reprojection: {exc}")
            return dest
        return self._reproject_destination(position, dest)

    async def get_world_state(self) -> WorldState | None:
        if not self._connected:
            return None
        res = await self._post("/cmd", {"action": "status", "payload": {}})
        if not res.get("ok"):
            return None
        data = res.get("data") or {}
        entities = data.get("entities") or []
        blocks = data.get("blocks") or []
        inventory = data.get("inventory") or []
        inventory_counts = self._inventory_counts(inventory)
        # Cache live telemetry for ``get_progression_context`` (starter-goal
        # KB seeding). Structural ids only, never chat text.
        self._last_inventory_counts = inventory_counts
        # Advance the craft-material shortfall cue by one turn (refresh have,
        # decrement budget, self-clear when done). Structural, fail-safe.
        craft_deficit = self._consume_craft_deficit(inventory_counts)
        self._last_blocks = blocks
        self._last_dimension = str(data.get("dimension") or "")
        affordances = self._build_affordances(entities, blocks)
        current_goal, recent_goals = await self._resolve_goals()
        bases = await self._resolve_bases()
        quest = await self._resolve_quest(inventory_counts, bases)
        knowledge = await self._resolve_knowledge(
            current_goal, affordances, inventory_counts, blocks
        )
        # Prepend the current progression-stage reference facts (virtual quest
        # tech-tree) to the knowledge block so the will/action beats render
        # "where you are / a typical next milestone / the far horizon" through
        # the same "reference, not a script" framing as the KB. Structural /
        # numeric only (id-count + dimension), fully fail-safe (never breaks the
        # snapshot). See quests.py and AGENTS.md §5c (spontaneity rule).
        try:
            stage = quests.detect_stage(inventory_counts, self._last_dimension)
            stage_facts = quests.stage_reference_facts(stage)
            if stage_facts:
                knowledge = stage_facts + list(knowledge or [])
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} progression stage facts failed: {exc}")
        # Structural "took damage this tick" delta: health dropped versus the
        # previous snapshot. Numeric-only, never keyword logic. The magnitude
        # feeds the post-damage appraisal will beat (see vessel_interface).
        cur_health = data.get("health")
        damage_taken: float | None = None
        if isinstance(cur_health, (int, float)) and isinstance(
            self._last_health, (int, float)
        ):
            drop = float(self._last_health) - float(cur_health)
            if drop > 0:
                damage_taken = drop
        if isinstance(cur_health, (int, float)):
            self._last_health = float(cur_health)
        return WorldState(
            environment=ENVIRONMENT,
            health=data.get("health"),
            position=data.get("position"),
            possible_actions=[
                "say",
                "move",
                "look",
                "observe",
                "use",
                "attack",
                "shoot",
                "follow",
                "unfollow",
                "respawn",
                # Minecraft-specific gameplay verbs (see get_world_actions).
                "goto",
                "mine",
                "collect_block",
                "place",
                "craft",
                "inventory",
                "wander",
                "scan",
                # Self-directed goal verbs (see get_world_actions).
                "set_goal",
                "goals",
                "update_goal",
                # Base (home) verbs (see get_world_actions).
                "set_base",
                "list_bases",
            ],
            flags={
                "connected": bool(data.get("connected")),
                "is_day": data.get("is_day"),
            },
            extra={
                "username": data.get("username"),
                "food": data.get("food"),
                "dimension": data.get("dimension"),
                "time_of_day": data.get("time_of_day"),
                "is_day": data.get("is_day"),
                "entities": entities,
                "blocks": blocks,
                "inventory": inventory,
                # Structured id->count view of the inventory, so cognition (the
                # action beat) and any world-agnostic consumer can judge "how
                # many oak_log do I still need" without re-scanning the list.
                # Keyword-free: it is a plain aggregation of the bridge ids.
                "inventory_counts": inventory_counts,
                "affordances": affordances,
                # Self-preservation telemetry (structural numeric/game-id fields
                # from the bridge status snapshot). Feed the survival reflex and
                # the will-beat "heads up" cue. Null-safe: absent on an older
                # bridge that predates the telemetry, in which case the reflex
                # simply degrades to inaction for that danger.
                #
                # ``health`` is mirrored into ``extra`` (it is also the
                # top-level WorldState.health field) because the fast survival
                # reflex reads ``extra.get("health")`` — without it the reflex
                # saw ``None`` every tick, so ``low_health`` was permanently
                # False and the body would keep swinging at mobs instead of
                # fleeing when actually dying. Numeric-only, null-safe.
                "health": data.get("health"),
                "oxygen": data.get("oxygen"),
                "is_in_water": data.get("is_in_water"),
                "is_alive": data.get("is_alive"),
                # The most recent death: {x, y, z, count, at} or None before the
                # first death. Lets the will beat steer Synth away from the spot
                # it keeps dying at and reconsider its approach instead of
                # resuming the same fatal goal. Structural (coordinates + a
                # count), never text. Null on an older bridge.
                "last_death": data.get("last_death"),
                "block_feet": data.get("block_feet"),
                "block_head": data.get("block_head"),
                # Combat readiness telemetry (structural, from the bridge
                # inventory + minecraft-data): whether a bow/crossbow with ammo
                # is carried, how much ammo, and the attack-damage of the best
                # melee weapon in the inventory. Feed the ranged-vs-melee reflex
                # decision and the will beat. Null/0 on an older bridge.
                "has_ranged_weapon": data.get("has_ranged_weapon"),
                "ranged_ammo": data.get("ranged_ammo"),
                "best_melee_damage": data.get("best_melee_damage"),
                # Total equipped armor defense points (helmet+chest+legs+boots),
                # summed by the bridge from minecraft-data. Feeds the survivor
                # term of the power-aware fight/flee decision. Null/0 bare or on
                # an older bridge. Numeric-only, structural.
                "armor_points": data.get("armor_points"),
                # Structural "took damage this tick" magnitude (health drop vs
                # the previous snapshot), or None if unchanged/unknown. Drives
                # the post-damage appraisal will beat. Numeric-only.
                "damage_taken": damage_taken,
                # Whether the most recent hit came from a *person* (another
                # player) vs a creature/environment. Structural bool from the
                # bridge (classifyAttacker game type, time-boxed), or None when
                # unknown/absent. Lets the appraisal choose a social response to
                # a player instead of reflexively swinging back.
                "damage_from_player": data.get("damage_from_player"),
                # The most recent survival threat the reflex acted on (or the
                # currently-active one). Lets the slow will beat acknowledge the
                # danger in-character. None when the body is safe.
                "threat": self._last_survival_threat,
                "threat_reason": self._last_survival_reason,
                # Self-directed play: the free-text objective Synth set for
                # itself and its own recent goal history. Populated from the
                # minecraft_goals table (see goals.py) — no catalogue, no
                # auto-computed progress; Synth judges its own progress.
                "current_goal": current_goal,
                "recent_goals": recent_goals,
                # Goal-level material shortfall: what the ACTIVE goal still
                # needs (have/need per named product/target) vs the live
                # inventory. Rendered by the will/action beats as a "your goal
                # still needs N/M <item>" cue so Synth picks the concrete next
                # step instead of drifting (the "runs around" gap). Structural
                # (canonical ids + counts), never chat text. None when the goal
                # names nothing countable or is fully satisfied.
                "goal_deficit": self._compute_goal_deficit(
                    current_goal, inventory_counts
                ),
                # Structural 3-state feedback on the last named target the motor
                # tried to reach (arrived / not_found / unreachable). Lets the
                # slow will beat see *why* a target failed and re-plan (pick
                # another target, or set a destination to go look for it). All
                # None until a named target is attempted. See motor_step /
                # _record_target_outcome — classification is keyword-free.
                "last_target_result": self._last_target_result,
                "last_target_name": self._last_target_name,
                "last_target_kind": self._last_target_kind,
                # Curated game-rule facts relevant to the current goal /
                # surroundings (reference only, never a script). Surfaced into
                # the will/action beats so Synth reasons with real Minecraft
                # rules. Empty when the knowledge base is disabled or nothing
                # matches. Keyed on structural ids, keyword-free.
                "knowledge": knowledge,
                # Craft-material shortfall cue: the item Synth last tried to
                # craft but lacked materials for, with each missing ingredient's
                # live have/need counts. Rendered by the will/action beats as a
                # "you wished to build X, you need have/need <material>" hint for
                # a few turns, then self-clears. Structural (Minecraft item ids +
                # counts), never chat text. None when there is no pending
                # shortfall.
                "craft_deficit": craft_deficit,
                # Registered bases (homes) Synth built/claimed in this world.
                # Surfaced into the will/action/reflection beats so Synth
                # remembers it has a home to build up, store in, or return to at
                # night, and read by the night-retreat reflex. Structural
                # (name/kind + {x,y,z} anchor), never chat text. Empty when no
                # base has been set. See bases.py / vessel_bases.py.
                "bases": bases,
                # The active quest (directed milestone toward the Ender Dragon).
                # Surfaced into the will/action/reflection beats as *reference*
                # only — a direction to bind the freely-authored goal to, never
                # a script (spontaneity rule). The store advances a quest only
                # when the world structurally satisfies its objectives. None
                # when quests are disabled or the questline is complete. See
                # quests.py / vessel_quests.py and AGENTS.md §5c.
                "quest": quest,
            },
        )

    async def _resolve_knowledge(
        self,
        current_goal: Dict[str, Any] | None,
        affordances: list[dict[str, Any]],
        inventory_counts: dict[str, int] | None = None,
        blocks: list[dict[str, Any]] | None = None,
    ) -> list[Dict[str, Any]]:
        """Pick knowledge-base facts relevant to the goal and surroundings.

        Builds a **structural** query — the goal's ``target_name`` plus any
        block/entity ids Synth is standing among (from the affordance contract)
        — and looks them up in the connector's knowledge base. Never inspects
        free-text goal descriptions for keywords, so it stays language-agnostic.

        Starter-goal seeding: when there is **no active goal** and nothing
        interactable nearby produced query tokens, the query falls back to the
        ids Synth actually **holds** (inventory) and the ids of **blocks around
        her** — purely structural facts about her real situation, never a
        scripted progression catalogue. This lets the will beat author a
        *progression-appropriate* first goal from what she has and sees, rather
        than blind (AGENTS.md §5c, the spontaneity rule: the facts are
        reference only; Synth still chooses the goal).

        Gated by ``VESSEL_KNOWLEDGE_ENABLED`` and capped by
        ``VESSEL_KNOWLEDGE_MAX_SNIPPETS``. Fail-safe: any error degrades to no
        knowledge rather than breaking the world snapshot.
        """
        try:
            enabled = bool(
                config_registry.get_value(
                    "VESSEL_KNOWLEDGE_ENABLED",
                    True,
                    group="plugins",
                    component="vessel_plugin",
                )
            )
            if not enabled:
                return []
            try:
                cap = int(
                    config_registry.get_value(
                        "VESSEL_KNOWLEDGE_MAX_SNIPPETS",
                        5,
                        group="plugins",
                        component="vessel_plugin",
                    )
                )
            except (TypeError, ValueError):
                cap = 5
            cap = max(1, min(20, cap))

            tokens: list[str] = []
            if isinstance(current_goal, dict):
                tname = current_goal.get("target_name")
                if tname:
                    tokens.append(str(tname).lower())
            for aff in affordances or []:
                if not isinstance(aff, dict):
                    continue
                target = aff.get("target")
                if target:
                    tokens.append(str(target).lower())

            # Starter-goal seeding. With no goal target and no interactable
            # nearby, seed the query from Synth's ACTUAL situation — the item
            # ids she holds and the block ids around her — so a first login
            # still surfaces progression-relevant facts (what she has / sees),
            # letting the will beat author an informed first goal. Purely
            # structural (bridge ids), never a scripted objective list.
            has_goal = isinstance(current_goal, dict) and bool(
                str(current_goal.get("description") or "").strip()
            )
            if not tokens and not has_goal:
                # First, steer the seed toward the NEXT progression milestone
                # (virtual-quest tech-tree): from the detected stage's next-step
                # query ids (e.g. she has logs → seed crafting_table /
                # wooden_pickaxe / cobblestone). Structural / numeric only
                # (id-count + dimension), so the starter goal points at the
                # next tier instead of random nearby junk — while staying
                # reference-only (spontaneity rule: Synth still chooses).
                try:
                    stage = quests.detect_stage(inventory_counts, self._last_dimension)
                    tokens.extend(quests.progression_query_tokens(stage))
                except Exception:  # pragma: no cover - defensive
                    pass
                # Then add her ACTUAL situation (held item ids + surrounding
                # block ids) so the seed is also grounded in what she has/sees.
                progression = self._progression_query_tokens(inventory_counts, blocks)
                tokens.extend(progression)

            if not tokens:
                return []
            query = " ".join(dict.fromkeys(tokens))  # de-dup, preserve order
            # Automatic beat path: cache-only so a WorldState build never blocks
            # on the network or the LLM (AGENTS.md §5c).
            return await self.lookup_knowledge(query, limit=cap, cache_only=True)
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} knowledge resolution failed: {exc}")
            return []

    @staticmethod
    def _progression_query_tokens(
        inventory_counts: dict[str, int] | None,
        blocks: list[dict[str, Any]] | None,
    ) -> list[str]:
        """Structural KB-query tokens describing Synth's real situation.

        Used for starter-goal seeding when she has no goal yet: the ids she
        actually **holds** plus the ids of **blocks around her**. This is a
        plain read of live bridge ids — never a hardcoded progression stage or
        objective list (spontaneity rule). Fail-safe: any error → ``[]``.
        """
        tokens: list[str] = []
        try:
            if isinstance(inventory_counts, dict):
                # Order by quantity so the most-held items lead the query;
                # numeric only, no id inspection/keyword logic.
                for item_id, _count in sorted(
                    inventory_counts.items(),
                    key=lambda kv: kv[1],
                    reverse=True,
                ):
                    if item_id:
                        tokens.append(str(item_id).lower())
            for blk in blocks or []:
                if not isinstance(blk, dict):
                    continue
                name = blk.get("name")
                if name:
                    tokens.append(str(name).lower())
        except Exception:  # pragma: no cover - defensive
            return []
        # De-dup, preserve order, and keep the seed small.
        return list(dict.fromkeys(tokens))[:6]

    def get_progression_context(self) -> list[str] | None:
        """Structural progression-context tokens for the starter-goal hook.

        Delegates to :meth:`_progression_query_tokens` over the connector's
        last-known inventory/blocks so a world-agnostic caller (the core will
        beat / starter-goal path) can seed a knowledge lookup without knowing
        Minecraft specifics. Fail-safe: returns ``None`` when nothing is known.
        """
        try:
            counts = self._last_inventory_counts
            blocks = self._last_blocks
            tokens = self._progression_query_tokens(counts, blocks)
            return tokens or None
        except Exception:  # pragma: no cover - defensive
            return None

    def get_progression_stage(self) -> Dict[str, Any] | None:
        """Current virtual-quest stage + typical next milestone (reference).

        Minecraft-specific **content** side of the core
        :meth:`VesselConnectorBase.get_progression_stage` mechanism. Delegates
        to the adapter's structural tech-tree (:func:`quests.detect_stage`) over
        the connector's last-known inventory counts and dimension id — plain
        game ids only, never chat text (AGENTS.md §5c). The result is surfaced
        purely as reference context; Synth still authors its own goal freely.
        Fail-safe: any error → ``None``.
        """
        try:
            return quests.detect_stage(
                self._last_inventory_counts, self._last_dimension
            )
        except Exception:  # pragma: no cover - defensive
            return None

    async def _resolve_goals(
        self,
    ) -> tuple[Dict[str, Any] | None, list[Dict[str, Any]]]:
        """Recall Synth's active goal and its own recent goal history.

        Fail-safe: any error (e.g. DB unavailable) degrades to "no goal / no
        history" rather than breaking the world snapshot.
        """
        try:
            current = await mc_goals.get_active_goal()
            recent = await mc_goals.list_recent_goals()
            return current, recent
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} goal resolution failed: {exc}")
            return None, []

    async def _resolve_bases(self) -> list[Dict[str, Any]]:
        """Recall the bases (homes) Synth registered in this world.

        Fail-safe: any error (e.g. DB unavailable or the base store missing)
        degrades to "no base" rather than breaking the world snapshot.
        """
        try:
            return await mc_bases.list_bases()
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} base resolution failed: {exc}")
            return []

    def _compute_goal_deficit(
        self,
        goal: Dict[str, Any] | None,
        inventory_counts: Dict[str, int],
    ) -> Dict[str, Any] | None:
        """Compute the active goal's material shortfall (have/need), or None.

        Structural counterpart of :meth:`_update_craft_deficit` for the *goal*
        itself: reads the goal's named products/targets
        (:func:`target_names.derive_product_quantities` /
        :func:`target_names.derive_quantity`) and diffs them against the live
        inventory counts. Returns ``{"items": [{"item", "have", "need"}]}``
        listing only items still short (``have < need``), or ``None`` when the
        goal names nothing countable or is fully satisfied. Rendered by the
        will/action beats as a "your goal still needs N/M <item>" cue, so Synth
        picks the concrete "gather the missing quantity" step instead of
        drifting. Purely structural (canonical ids + numeric counts), never
        parses free text for intent. Fail-safe.
        """
        try:
            if not isinstance(goal, dict):
                return None
            if not isinstance(inventory_counts, dict):
                return None
            description = " ".join(
                str(goal.get(field) or "") for field in ("description", "note")
            ).strip()
            products = mc_target_names.derive_product_quantities(description)
            needs: Dict[str, int] = dict(products)
            target_name = goal.get("target_name")
            if not target_name:
                derived = mc_target_names.derive_target(description)
                if derived:
                    target_name = derived.get("target_name")
            if isinstance(target_name, str) and target_name:
                needs[target_name] = mc_target_names.derive_quantity(
                    description, target_name
                )
            items: list[Dict[str, Any]] = []
            for item, need in needs.items():
                try:
                    have = int(inventory_counts.get(item, 0))
                    need_i = int(need)
                except (TypeError, ValueError):
                    continue
                if need_i <= 0:
                    continue
                if have < need_i:
                    items.append({"item": item, "have": have, "need": need_i})
            if not items:
                return None
            return {"items": items}
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} goal deficit computation failed: {exc}")
            return None

    async def _resolve_quest(
        self,
        inventory_counts: dict[str, int],
        bases: list[Dict[str, Any]],
    ) -> Dict[str, Any] | None:
        """Return the active questline milestone (reference only), auto-advancing.

        Reads the current active quest from the core store and, if the world now
        **structurally** satisfies its objectives (inventory counts, current
        dimension, base/bed flags, kill counters), marks it done and promotes
        the next milestone — then returns the (possibly newly-promoted) active
        quest for the beats to render as reference. Never inspects chat/goal
        text; advancement is purely structural. Fully fail-safe: any error (or a
        disabled questline) degrades to "no quest".
        """
        if not self._quests_enabled:
            return None
        try:
            from plugins.rift_vessel.vessel_quests import evaluate_quest_objectives

            active = await quests.get_active_quest()
            if not isinstance(active, dict) or not active:
                return active if isinstance(active, dict) else None
            has_base = bool(bases)
            # A slept-in bed sets respawn; we treat carrying/placing a bed as
            # satisfying "have a bed" (structural: bed id in inventory). The
            # core evaluator also checks counts["bed"], but any *_bed id counts.
            has_bed = any(
                name.endswith("_bed") or name == "bed"
                for name in inventory_counts
                if isinstance(name, str)
            )
            result = evaluate_quest_objectives(
                active,
                inventory_counts,
                self._last_dimension,
                has_base=has_base,
                has_bed=has_bed,
            )
            if isinstance(result, dict) and result.get("complete"):
                qid = active.get("quest_id")
                if isinstance(qid, str) and qid:
                    log_info(
                        f"{LOG_PREFIX} questline milestone complete: {qid} -> advancing"
                    )
                    await quests.complete_quest(qid)
                    return await quests.get_active_quest()
            return active
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} quest resolution failed: {exc}")
            return None

    @staticmethod
    def _inventory_counts(inventory: list[dict[str, Any]]) -> dict[str, int]:
        """Aggregate the raw inventory list into an ``id -> total count`` map.

        Pure structural aggregation of the bridge's inventory records (which may
        list the same item id across several stacks); never inspects names for
        keywords. Fail-safe: malformed rows are skipped.
        """
        counts: dict[str, int] = {}
        for item in inventory:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if not name:
                continue
            try:
                count = int(item.get("count") or 0)
            except (TypeError, ValueError):
                continue
            counts[str(name)] = counts.get(str(name), 0) + count
        return counts

    @staticmethod
    def _build_affordances(
        entities: list[dict[str, Any]],
        blocks: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Turn raw surroundings into the world-agnostic *affordance* contract.

        An affordance is a structured ``{kind, target, verb, distance}`` record
        telling the Vessel core "here is something you could interact with and
        the verb that would do it". This is deliberately structural — it never
        inspects names for keywords; it maps entity/block *shape* to the generic
        embodiment verbs the core already exposes. The Vessel core surfaces
        these to Synth without needing to know what a "creeper" or "oak_log" is.
        """
        out: list[dict[str, Any]] = []
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            name = ent.get("name")
            if not name:
                continue
            kind = str(ent.get("kind") or "entity")
            # A mob affords an attack; anything with a position affords follow;
            # players/objects afford a benign interaction. Structural mapping —
            # no name/keyword inspection.
            verb = "attack" if kind == "mob" else "use"
            out.append(
                {
                    "kind": "entity",
                    "target": name,
                    "verb": verb,
                    "distance": ent.get("distance"),
                    # Absolute world position, carried through so the Vessel
                    # core can derive a cardinal bearing (N/E/S/W) for the
                    # sighting view. Purely geometric — never inspected here.
                    "position": ent.get("position"),
                }
            )
        for blk in blocks:
            if not isinstance(blk, dict):
                continue
            name = blk.get("name")
            if not name:
                continue
            out.append(
                {
                    "kind": "block",
                    "target": name,
                    "verb": "use",
                    "distance": blk.get("distance"),
                    "position": blk.get("position"),
                }
            )
        out.sort(key=lambda a: (a.get("distance") is None, a.get("distance") or 0))
        return out

    # Motorics: how close (blocks) an affordance must be for the body to act on
    # it directly (mine/use) rather than first walking toward it.
    _MOTOR_REACH = 3.0

    # Utility / light-source block ids the fast motor reflex must NEVER mine on
    # its own. These are player- (or self-) placed functional blocks — chiefly
    # **torches**, the primary way an underground path is kept lit. Reflexively
    # mining a torch it walks past would strip the light Synth (or a player)
    # just placed, plunging the tunnel back into darkness (the reported "Rekku
    # steals torches" bug). This is a structural guard on **game block-state
    # ids**, not natural-language keywords, so it is multi-language-safe: the
    # motor never destroys these by reflex. A *deliberate* will-beat goal that
    # names one of these as its target is still honoured (cognition may have a
    # reason) — only the incidental "grab whatever benign block is in reach"
    # reflex is suppressed. The set is intentionally small and light-focused.
    _REFLEX_NO_MINE_BLOCKS = frozenset(
        {
            "torch",
            "wall_torch",
            "soul_torch",
            "soul_wall_torch",
            "redstone_torch",
            "redstone_wall_torch",
            "lantern",
            "soul_lantern",
            "sea_lantern",
            "jack_o_lantern",
            "glowstone",
            "shroomlight",
            "campfire",
            "soul_campfire",
            "beacon",
            "end_rod",
            "candle",
        }
    )

    # How close (blocks, horizontal) the body must get to a self-chosen travel
    # destination before it counts as "arrived" and stops steering toward it.
    _ARRIVAL_RADIUS = 4.0

    # Minimum genuine travel distance (blocks, horizontal) a self-chosen
    # destination must sit at for the body to actually walk. The will beat (a
    # slow LLM) often picks a coordinate only a couple of blocks away — well
    # inside ``_ARRIVAL_RADIUS`` — so the motor reflex treats it as "already
    # arrived" and never moves. When that happens we *reproject* the target
    # along the very same direction cognition chose, out to this distance, so
    # the body receives a genuinely distant goal without overriding *where*
    # Synth wants to go. It is ``_ARRIVAL_RADIUS`` plus a margin so the target
    # is unambiguously outside the arrival ring.
    _MIN_TRAVEL_DISTANCE = 16.0

    # Self-directed exploration legs must be genuinely long, not short and
    # segmented (TODO): when the body invents its own next waypoint it walks a
    # distance of ``_MIN_TRAVEL_DISTANCE`` times a factor drawn uniformly from
    # ``[_EXPLORE_LEG_MIN_FACTOR, _EXPLORE_LEG_MAX_FACTOR]`` — i.e. roughly
    # 3–4× longer than the bare minimum, randomised each time so successive
    # legs are never identical. The randomness keeps autonomous movement from
    # looking mechanical, and the length means the body commits to a real trek
    # (it can still be interrupted mid-leg by an en-route sighting or a benign
    # affordance in reach — the motor tick re-decides every interval).
    _EXPLORE_LEG_MIN_FACTOR = 3.0
    _EXPLORE_LEG_MAX_FACTOR = 4.0

    # How many consecutive motor ticks the body may sit "arrived at the
    # destination with nothing new to do" before the reflex stops waiting for
    # cognition and reprojects the destination forward on its own. This is the
    # concrete guarantee that Synth "does not stay blocked until it reaches the
    # destination" (TODO): the world is live, plans can change, so the body
    # keeps exploring instead of circling the arrival tile while the slow will
    # beat catches up. Purely a tick counter — no timers, no keywords.
    _STALE_ARRIVAL_TICKS = 3

    # How many consecutive motor ticks the body may pursue a numeric
    # destination *without meaningfully closing the gap* before the reflex
    # gives up on it as unreachable. A will-beat coordinate can be physically
    # unreachable (in water, across a ravine, on a cliff): the pathfinder keeps
    # closing to a few metres, fails, resets and re-approaches, so ``remaining``
    # oscillates and the body never gets inside ``_ARRIVAL_RADIUS`` to "arrive"
    # — pacing the same path back and forth forever. When the best distance we
    # have managed toward the current destination fails to improve by at least
    # ``_TRAVEL_PROGRESS_EPS`` for this many ticks, we consume the destination
    # so the body marches on. Purely numeric — no timers, no keywords.
    _STALE_TRAVEL_TICKS = 6

    # Minimum improvement (blocks, horizontal) in the best distance toward the
    # current destination that counts as "still making progress". Smaller
    # oscillations are treated as being stuck.
    _TRAVEL_PROGRESS_EPS = 1.0

    # Physical-motion watchdog thresholds (see ``_last_body_position``). If the
    # body moves less than ``_STUCK_MOVE_EPS`` blocks (horizontal) between two
    # consecutive motor ticks while a destination is still pending, that tick
    # counts as "not moving"; after ``_STUCK_POSITION_TICKS`` such ticks the
    # destination is treated as unreachable and consumed. This measures the
    # *body*, not the distance number, so it fires even when ``remaining``
    # oscillates or ticks are skipped during blocking pathfinding.
    _STUCK_MOVE_EPS = 0.75
    _STUCK_POSITION_TICKS = 4

    # Staticity ward thresholds (see ``_static_anchor`` / ``_staticity_ward``).
    # Where ``_STUCK_POSITION_TICKS`` measures *tick-to-tick* motion and only
    # runs while a goal is actively driving the body, the staticity ward is a
    # broader, always-on guard: it fires when the body *lingers in the same
    # small area* for too long **regardless of goal or what it is doing** — the
    # "Synth stays parked in one spot forever" case the tick-to-tick watchdog
    # misses (no goal at all, or endlessly ``mine``/``use``ing an in-reach block
    # without displacing). ``_STATIC_WARD_RADIUS`` is the radius (blocks,
    # horizontal) that still counts as "the same place": while the body stays
    # within this radius of a moving anchor it accrues idle ticks; leaving it
    # resets the anchor. After ``_STATIC_WARD_TICKS`` consecutive idle ticks the
    # ward forces a fresh long directional march to break the parking. Purely
    # positional/numeric — no timers, no keywords.
    _STATIC_WARD_RADIUS = 2.0
    _STATIC_WARD_TICKS = 8

    # Turn applied to the exploration heading each time a stale destination is
    # reprojected, so successive self-directed reprojections fan out across the
    # world instead of retracing the same straight line. ~2.4 rad ≈ 137° (a
    # golden-angle-ish step) spreads directions well without ever repeating.
    _EXPLORE_TURN_RAD = 2.399963

    @staticmethod
    def _explore_leg_distance() -> float:
        """Randomised length (blocks) of a self-directed exploration leg.

        Returns ``_MIN_TRAVEL_DISTANCE`` scaled by a uniform factor in
        ``[_EXPLORE_LEG_MIN_FACTOR, _EXPLORE_LEG_MAX_FACTOR]`` — i.e. legs are
        ~3–4× longer than the bare minimum and vary each call, so autonomous
        travel is neither short/segmented nor mechanically repetitive.
        """
        factor = random.uniform(
            MinecraftConnector._EXPLORE_LEG_MIN_FACTOR,
            MinecraftConnector._EXPLORE_LEG_MAX_FACTOR,
        )
        return MinecraftConnector._MIN_TRAVEL_DISTANCE * factor

    def _update_staticity_ward(self, position: Any) -> bool:
        """Track lingering-in-place and report when the ward should fire.

        Purely positional/numeric guard (no timers, no keywords). Maintains a
        moving anchor: while the body stays within ``_static_ward_radius`` of the
        anchor it accrues idle ticks; straying outside resets the anchor and the
        counter. Returns ``True`` exactly once when the idle count reaches
        ``_static_ward_limit`` — the signal that the body has been parked in the
        same small area too long and must relocate. On that fire the counter and
        anchor are reset so the ward re-arms cleanly for the next stasis.

        Fail-safe: a missing/malformed position resets tracking and never fires.
        """
        if not self._static_ward_enabled:
            return False
        cur = position if isinstance(position, dict) else None
        if cur is None:
            self._static_anchor = None
            self._static_ward_ticks = 0
            return False
        try:
            here = {"x": float(cur["x"]), "z": float(cur["z"])}
        except (KeyError, TypeError, ValueError):
            self._static_anchor = None
            self._static_ward_ticks = 0
            return False
        if self._static_anchor is None:
            self._static_anchor = here
            self._static_ward_ticks = 0
            return False
        moved = self._horizontal_distance(here, self._static_anchor)
        if moved is None or moved > self._static_ward_radius:
            # The body left the parked area — re-anchor and start fresh.
            self._static_anchor = here
            self._static_ward_ticks = 0
            return False
        self._static_ward_ticks += 1
        if self._static_ward_ticks >= self._static_ward_limit:
            # Parked too long: fire once, re-anchor here and re-arm.
            self._static_ward_ticks = 0
            self._static_anchor = here
            return True
        return False

    @staticmethod
    def _reproject_forward(
        position: Any,
        heading: float,
    ) -> Dict[str, float] | None:
        """Pick a fresh, distant travel target from ``position`` along ``heading``.

        Purely geometric self-directed exploration: project a point a
        randomised long distance (see :meth:`_explore_leg_distance`, ~3–4×
        ``_MIN_TRAVEL_DISTANCE``) away from the current position along the given
        planar ``heading`` (radians). Used by the anti-stall reflex when a
        self-chosen destination has been reached but cognition has not yet
        supplied a new one — the body invents its own next waypoint so it keeps
        moving, over a genuinely long, non-repetitive leg. Returns ``None`` when
        there is no usable position.
        """
        if not isinstance(position, dict):
            return None
        try:
            px = float(position["x"])
            pz = float(position["z"])
        except (KeyError, TypeError, ValueError):
            return None
        leg = MinecraftConnector._explore_leg_distance()
        dx = math.cos(heading) * leg
        dz = math.sin(heading) * leg
        return {"x": px + dx, "z": pz + dz}

    @staticmethod
    def _reproject_destination(
        position: Any,
        dest: Dict[str, float] | None,
    ) -> Dict[str, float] | None:
        """Push a too-close destination out to a real travel distance.

        Purely geometric, no keywords: if ``dest`` sits closer than
        ``_MIN_TRAVEL_DISTANCE`` to ``position`` (but is not the current tile),
        move it *along the same heading* out to ``_MIN_TRAVEL_DISTANCE`` so the
        motor reflex actually traverses toward the direction cognition chose,
        instead of treating a 2-block offset as "arrived". Returns ``dest``
        unchanged when it is already far enough, and ``None`` / the original
        when there is nothing usable to reproject (e.g. no position, or the
        target coincides with the current position — no meaningful heading).
        """
        if not isinstance(dest, dict) or "x" not in dest or "z" not in dest:
            return dest
        if not isinstance(position, dict):
            return dest
        try:
            px = float(position["x"])
            pz = float(position["z"])
        except (KeyError, TypeError, ValueError):
            return dest
        dx = dest["x"] - px
        dz = dest["z"] - pz
        distance = (dx * dx + dz * dz) ** 0.5
        if distance >= MinecraftConnector._MIN_TRAVEL_DISTANCE:
            return dest
        if distance <= 1e-6:
            # Destination coincides with the current position: no heading to
            # extend along, leave it untouched so the reflex falls back to
            # local wandering rather than an arbitrary direction.
            return dest
        scale = MinecraftConnector._MIN_TRAVEL_DISTANCE / distance
        reprojected: Dict[str, float] = {
            "x": px + dx * scale,
            "z": pz + dz * scale,
        }
        if "y" in dest:
            reprojected["y"] = dest["y"]
        return reprojected

    @staticmethod
    def _goal_destination(goal: Dict[str, Any] | None) -> Dict[str, float] | None:
        """Extract the structural ``{x, z}`` travel target from the active goal.

        The *will beat* (cognition) is the only thing that decides **where** to
        head — it records that as a numeric coordinate on the goal (see
        ``goals.set_goal(destination=...)``). This reflex only reads those
        numbers; it never inspects the goal's free text. Returns ``None`` when
        the goal carries no usable destination.
        """
        if not isinstance(goal, dict):
            return None
        dest = goal.get("destination")
        if not isinstance(dest, dict):
            return None
        try:
            x = float(dest["x"])
            z = float(dest["z"])
        except (KeyError, TypeError, ValueError):
            return None
        out: Dict[str, float] = {"x": x, "z": z}
        y = dest.get("y")
        if y is not None:
            try:
                out["y"] = float(y)
            except (TypeError, ValueError):
                pass
        return out

    @staticmethod
    def _goal_target(goal: Dict[str, Any] | None) -> Dict[str, str] | None:
        """Extract the structural ``{kind, name}`` target from the active goal.

        The *what to head for* (a specific block/entity type Synth chose from
        the live scan, or a bare ``coordinate`` marker) is decided by cognition
        — the will beat or the out-of-band drone planner — and stored on the
        goal (see ``goals._coerce_target``). This reflex only reads those
        already-validated fields; it never inspects the goal's free text nor
        matches names against keywords. Returns ``None`` when the goal carries
        no usable block/entity target (a bare ``coordinate`` target is handled
        by :meth:`_goal_destination`, so it is treated as "no reflex target"
        here). The bridge resolves ``name`` structurally by exact id.
        """
        if not isinstance(goal, dict):
            return None
        kind = goal.get("target_kind")
        name = goal.get("target_name")
        if kind not in ("block", "entity"):
            return None
        if not isinstance(name, str) or not name.strip():
            return None
        return {"kind": kind, "name": name.strip().lower()}

    @staticmethod
    def _goal_key(goal: Dict[str, Any] | None) -> str | None:
        """Stable identity for the active goal + its destination.

        Used by the anti-stall reflex to tell "still the same objective" from
        "cognition gave me a new one": the stall counter resets whenever this
        key changes. Combines the goal id (if any) with its numeric destination
        so that a re-aimed destination on the same goal id also counts as new.
        Purely structural — never reads the goal's free text.
        """
        if not isinstance(goal, dict):
            return None
        gid = goal.get("id")
        dest = MinecraftConnector._goal_destination(goal)
        if dest is None:
            return f"{gid}:none" if gid is not None else None
        return f"{gid}:{dest.get('x')}:{dest.get('z')}"

    @staticmethod
    def _horizontal_distance(position: Any, dest: Dict[str, float]) -> float | None:
        """Planar (x/z) distance from ``position`` to ``dest``, or ``None``."""
        if not isinstance(position, dict):
            return None
        try:
            dx = float(position["x"]) - dest["x"]
            dz = float(position["z"]) - dest["z"]
        except (KeyError, TypeError, ValueError):
            return None
        return (dx * dx + dz * dz) ** 0.5

    @staticmethod
    def _scan_has_target(state: Any, target: Dict[str, str]) -> bool:
        """Whether the named target's exact id is present in the live scan.

        Structural, keyword-free: it compares the goal's already-validated
        ``target_name`` (lowercased exact id) against the block/entity ids the
        world snapshot actually enumerated (``extra['blocks']`` /
        ``extra['entities']``), matching by the same field the will beat picked
        the id from (``name`` for blocks, ``type`` then ``name`` for entities).
        Never parses free text. Used only to tell ``not_found`` (type not in the
        scan) from ``unreachable`` (in the scan but pathfinder failed).
        """
        try:
            extra = getattr(state, "extra", None) or {}
        except Exception:  # pragma: no cover - defensive
            return False
        name = target.get("name")
        kind = target.get("kind")
        if not name:
            return False
        if kind == "block":
            items = extra.get("blocks") or []
            keys = ("name",)
        else:
            items = extra.get("entities") or []
            keys = ("type", "name")
        for item in items:
            if not isinstance(item, dict):
                continue
            for k in keys:
                val = item.get(k)
                if isinstance(val, str) and val.strip().lower() == name:
                    return True
        return False

    def _record_target_outcome(
        self,
        state: Any,
        target: Dict[str, str],
        result: VesselActionResult,
    ) -> None:
        """Store the structural 3-state outcome of a named-target ``goto``.

        Purely structural classification (see ``__init__`` note): ``ok`` maps to
        ``arrived``; a failure is ``unreachable`` when the target id was in the
        live scan, else ``not_found``. Never inspects the bridge ``detail`` text.
        The result is surfaced in the next ``WorldState.extra`` so the slow will
        beat can re-plan. Fail-safe: any error leaves the previous feedback
        untouched.
        """
        try:
            if getattr(result, "ok", False):
                outcome = "arrived"
            elif self._scan_has_target(state, target):
                outcome = "unreachable"
            else:
                outcome = "not_found"
            self._last_target_result = outcome
            self._last_target_name = target.get("name")
            self._last_target_kind = target.get("kind")
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} target outcome record failed: {exc}")

    # ------------------------------------------------------------------
    # Self-preservation reflex
    # ------------------------------------------------------------------

    def _load_self_preservation_config(self) -> None:
        """Resolve the ``VESSEL_SP_*`` self-preservation settings for this
        session, falling back to the class-constant defaults on any read error.

        Structural numeric thresholds only — never keyword matching. Called once
        on connect; the resolved values live on the connector for the session.
        """

        def _flt(key: str, default: float) -> float:
            try:
                raw = config_registry.get_value(
                    key, default, group="plugins", component="vessel_plugin"
                )
                return float(raw)
            except (TypeError, ValueError):
                return default

        def _boolv(key: str, default: bool) -> bool:
            try:
                raw = config_registry.get_value(
                    key, default, group="plugins", component="vessel_plugin"
                )
                if isinstance(raw, bool):
                    return raw
                return str(raw).strip().lower() in ("1", "true", "yes", "on")
            except Exception:
                return default

        def _intv(key: str, default: int) -> int:
            try:
                raw = config_registry.get_value(
                    key, default, group="plugins", component="vessel_plugin"
                )
                return int(raw)
            except (TypeError, ValueError):
                return default

        try:
            self._sp_enabled = _boolv("VESSEL_SELF_PRESERVATION_ENABLED", True)
            self._sp_low_oxygen = _flt("VESSEL_SP_LOW_OXYGEN", float(self._LOW_OXYGEN))
            self._sp_low_health = _flt(
                "VESSEL_SP_LOW_HEALTH", float(self._LOW_HEALTH_FLEE)
            )
            self._sp_hostile_dist = _flt(
                "VESSEL_SP_HOSTILE_DIST", float(self._HOSTILE_NEAR_DIST)
            )
            self._sp_fight_back = _boolv("VESSEL_SP_FIGHT_BACK", True)
            self._sp_fight_max_fails = _intv(
                "VESSEL_SP_FIGHT_MAX_FAILS", int(self._FIGHT_MAX_FAILS)
            )
            self._sp_use_ranged = _boolv("VESSEL_SP_USE_RANGED", True)
            self._sp_ranged_min_dist = _flt(
                "VESSEL_SP_RANGED_MIN_DIST", float(self._RANGED_MIN_DIST)
            )
            self._sp_appraisal_enabled = _boolv("VESSEL_SP_APPRAISAL_ENABLED", True)
            # Power-ratio fight/flee threshold and weak-mob floor.
            ratio = _flt("VESSEL_SP_ENGAGE_RATIO", float(self._ENGAGE_RATIO))
            self._sp_engage_ratio = min(max(ratio, 0.2), 5.0)
            self._sp_weak_mob_power = _flt(
                "VESSEL_SP_WEAK_MOB_POWER", float(self._WEAK_MOB_POWER)
            )
            # Proactive night shelter: enable flag + (wider) trigger radius.
            self._sp_night_shelter = _boolv("VESSEL_SP_NIGHT_SHELTER", True)
            self._sp_shelter_dist = _flt(
                "VESSEL_SP_SHELTER_DIST", float(self._SHELTER_HOSTILE_DIST)
            )
            # Morning bunker-exit: carve a staircase back to the surface when day
            # returns and the body is still buried underground with no base.
            self._sp_morning_exit = _boolv("VESSEL_MORNING_EXIT_ENABLED", True)
            # Base concept + night-retreat radius. When enabled, the night
            # shelter reflex first tries to head back to the nearest registered
            # base within this radius (reusing ``goto``) instead of walling the
            # body in on the spot.
            self._base_enabled = _boolv("VESSEL_BASE_ENABLED", True)
            self._base_retreat_radius = _flt(
                "VESSEL_BASE_RETREAT_RADIUS", float(self._BASE_RETREAT_RADIUS)
            )
            # Ender Dragon questline enablement (directed reference milestones).
            self._quests_enabled = _boolv("VESSEL_QUESTS_ENABLED", True)
            # Craft-material shortfall cue budget (turns). Clamp to the sane
            # "a few turns" band so a stray value can neither disable it (0) nor
            # pin the cue forever.
            craft_turns = _intv("VESSEL_CRAFT_CUE_TURNS", int(self._CRAFT_CUE_TURNS))
            self._craft_cue_turns = min(max(craft_turns, 1), 200)
            # Staticity ward: always-on guard that relocates the body when it
            # lingers in the same small area too long, regardless of goal.
            self._static_ward_enabled = _boolv("VESSEL_STATICITY_WARD_ENABLED", True)
            radius = _flt("VESSEL_STATICITY_RADIUS", float(self._STATIC_WARD_RADIUS))
            self._static_ward_radius = min(max(radius, 0.5), 32.0)
            limit = _intv("VESSEL_STATICITY_TICKS", int(self._STATIC_WARD_TICKS))
            self._static_ward_limit = min(max(limit, 2), 1000)
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} self-preservation config load failed: {exc}")

    @staticmethod
    def _nearest_hostile(state: "WorldState") -> dict[str, Any] | None:
        """Return the nearest hostile entity dict from the world state, or None.

        Structural only: relies on the bridge's per-entity ``hostile`` flag
        (game-logic mob classification) and numeric ``distance`` — never a
        keyword scan of entity names. Falls back to the structural
        ``kind == "mob"`` classification when the flag is absent (older bridge).
        """
        try:
            entities = (state.extra or {}).get("entities") or []
        except Exception:
            return None
        best: dict[str, Any] | None = None
        best_dist: float | None = None
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            hostile = ent.get("hostile")
            if hostile is None:
                # Older bridge without the flag: fall back to structural kind.
                hostile = ent.get("kind") == "mob"
            if not hostile:
                continue
            try:
                dist = float(ent.get("distance"))
            except (TypeError, ValueError):
                continue
            if best_dist is None or dist < best_dist:
                best = ent
                best_dist = dist
        return best

    def _aggressive_targets(
        self, state: "WorldState", near_dist: float
    ) -> list[dict[str, Any]]:
        """Return every aggressive mob worth engaging, nearest first.

        Structural only. A mob qualifies when EITHER it is flagged ``hostile``
        (game-logic mob classification) and within ``near_dist``, OR it is
        actively attacking the bot right now (``is_targeting_me`` from the
        bridge — a recent swing or an attack target pointing at us), regardless
        of distance so a ranged attacker that hit us from afar is still
        engaged. Players are NEVER included (a human hitting Synth is a social
        matter for the will beat, never a reflex melee). No keyword logic.
        """
        try:
            entities = (state.extra or {}).get("entities") or []
        except Exception:
            return []
        out: list[dict[str, Any]] = []
        for ent in entities:
            if not isinstance(ent, dict):
                continue
            # Never reflex-attack a person.
            if ent.get("kind") == "player":
                continue
            hostile = ent.get("hostile")
            if hostile is None:
                hostile = ent.get("kind") == "mob"
            targeting = bool(ent.get("is_targeting_me"))
            try:
                dist = float(ent.get("distance"))
            except (TypeError, ValueError):
                continue
            near = hostile and dist <= near_dist
            if not (near or (targeting and hostile)):
                continue
            out.append(ent)
        out.sort(key=lambda e: float(e.get("distance") or 1e9))
        return out

    # ------------------------------------------------------------------
    # Power model (structural, pure — no LLM, no keyword logic)
    # ------------------------------------------------------------------

    def _own_power(self, extra: dict[str, Any]) -> float:
        """Structural estimate of the body's current combat power.

        Combines the best melee weapon's attack damage (or a small bare-hand
        base), the equipped armor defense points, and the current health — all
        numeric telemetry from the bridge. Never reads item/mob names as
        keywords. Higher = more able to win a fight.
        """
        try:
            melee = extra.get("best_melee_damage")
            weapon = float(melee) if isinstance(melee, (int, float)) else 0.0
        except (TypeError, ValueError):
            weapon = 0.0
        # A usable ranged weapon (bow/crossbow with ammo) is itself an offensive
        # capability: a body that can shoot from afar is NOT helpless just
        # because it carries no melee weapon. Give it a structural offensive
        # term so an archer clears the power gate and reaches the ranged
        # (``shoot``) branch instead of always fleeing. Vanilla bow damage is
        # ~6-9 per charged hit; ``_RANGED_OFFENSE`` is a numeric floor, never a
        # name keyword. Both flags come from the bridge.
        try:
            ranged_ok = bool(extra.get("has_ranged_weapon"))
        except (TypeError, ValueError):
            ranged_ok = False
        ranged = self._RANGED_OFFENSE if ranged_ok else 0.0
        # Bare-handed vanilla base attack is ~1 heart/hit; give it a small floor
        # so a weaponless body still has a non-zero offensive term. Take the
        # strongest available offensive option (melee vs ranged).
        offense = max(weapon, ranged, 1.0)
        try:
            armor_raw = extra.get("armor_points")
            armor = float(armor_raw) if isinstance(armor_raw, (int, float)) else 0.0
        except (TypeError, ValueError):
            armor = 0.0
        try:
            hp_raw = extra.get("health")
            health = float(hp_raw) if isinstance(hp_raw, (int, float)) else 20.0
        except (TypeError, ValueError):
            health = 20.0
        # Survivability scales the offense: full armor + full health roughly
        # doubles effective power, an unarmored dying body roughly halves it.
        survivability = 1.0 + (armor / 20.0) + (health / 40.0)
        return offense * survivability

    def _mob_power(self, entity: dict[str, Any]) -> float:
        """Structural estimate of a mob's combat power.

        Combines the mob's registry max health and attack damage (from the
        bridge). When both are unavailable (older bridge / missing registry
        value) it returns a cautious MODERATE default so an unknown mob is
        neither trivially engaged nor treated as unbeatable. Numeric only.
        """
        max_health: float | None = None
        attack: float | None = None
        try:
            mh = entity.get("max_health")
            if isinstance(mh, (int, float)):
                max_health = float(mh)
        except (TypeError, ValueError):
            max_health = None
        try:
            ad = entity.get("attack_damage")
            if isinstance(ad, (int, float)):
                attack = float(ad)
        except (TypeError, ValueError):
            attack = None
        if max_health is None and attack is None:
            return float(self._DEFAULT_MOB_POWER)
        # A missing single term falls back to a neutral component so the mob is
        # still ranked, just less precisely.
        hp_term = max_health if max_health is not None else 20.0
        atk_term = attack if attack is not None else 3.0
        # Health is the dominant survivability term; attack scales the threat.
        return hp_term * (1.0 + atk_term / 8.0)

    def _is_disarmed(self, extra: dict[str, Any]) -> bool:
        """True when the body carries no melee weapon and no usable ranged one.

        Structural: reads the bridge's numeric ``best_melee_damage`` (0 when
        bare) and ``has_ranged_weapon`` flag. Never a name keyword.
        """
        try:
            melee = extra.get("best_melee_damage")
            has_melee = isinstance(melee, (int, float)) and float(melee) > 0
        except (TypeError, ValueError):
            has_melee = False
        has_ranged = bool(extra.get("has_ranged_weapon"))
        return not has_melee and not has_ranged

    def _is_weak_mob(self, entity: dict[str, Any]) -> bool:
        """True when a mob's structural power is below the weak-mob floor.

        Lets a disarmed body still turn and fight a trivial creature instead of
        fleeing everything. Numeric only.
        """
        return self._mob_power(entity) < self._sp_weak_mob_power

    def _survival_threat(self, state: "WorldState") -> dict[str, Any] | None:
        """Assess the highest-priority survival threat from the world state.

        Pure/structural: reads numeric health/oxygen/distance and canonical
        game block/entity ids from ``state`` — no goal text, no keyword scan.
        Returns a plan dict ``{"threat", "verb", "payload", "reason"}`` for the
        most urgent danger, or ``None`` when the body is safe. Priority order
        (highest first):

          1. dead → respawn
          2. drowning (head underwater / in water & low oxygen) → surface
          3. standing in lava/fire → move to safety
          4. hostile near & healthy & fight-back on → defend (attack), with
             escalation to flee after repeated failures / low health
          5. hostile near & (low health | fight-back off | escalated) → flee
        """
        extra = state.extra or {}

        # 1. Dead → come back to life.
        is_alive = extra.get("is_alive")
        if is_alive is False:
            return {
                "threat": "dead",
                "verb": "respawn",
                "payload": {},
                "reason": {"is_alive": False},
            }

        # 2. Drowning — head submerged and air running low. Structural: the
        # bridge tells us the block at the head is a water/liquid id and the
        # numeric oxygen level. Head for the surface before air hits 0.
        #
        # The body is only actually DROWNING when its HEAD is submerged in a
        # liquid block (block_head is a water id). The raw ``is_in_water``
        # physics flag is True even when merely wading through shallow water
        # (feet wet, head in air) or swimming at the surface — neither of which
        # loses air — so it must NOT trigger the reflex on its own, otherwise
        # the reflex fires on every tick spent near/along water and constantly
        # interrupts autonomous travel. ``is_in_water`` is kept only as a
        # secondary confirmation. Oxygen must also be a real, non-negative
        # reading below the low-air threshold (the bridge reports -1/None when
        # the value is unavailable — that must never look like suffocation).
        oxygen = extra.get("oxygen")
        block_head = extra.get("block_head")
        is_in_water = extra.get("is_in_water")
        if (
            _is_liquid_block(block_head)
            and isinstance(oxygen, (int, float))
            and oxygen >= 0
            and oxygen <= self._sp_low_oxygen
        ):
            return {
                "threat": "drowning",
                "verb": "goto_surface",
                "payload": {},
                "reason": {
                    "oxygen": oxygen,
                    "block_head": block_head,
                    "is_in_water": is_in_water,
                },
            }

        # 3. Standing in lava or fire → run to the nearest safe ground.
        block_feet = extra.get("block_feet")
        if _is_hot_block(block_feet) or _is_hot_block(block_head):
            return {
                "threat": "burning",
                "verb": "flee",
                "payload": {},
                "reason": {"block_feet": block_feet, "block_head": block_head},
            }

        # 4/5. Aggressive mob(s) nearby → defend (melee or ranged) or flee.
        #
        # Engage EVERY aggressive mob around the body, not just the single
        # nearest: the target list includes any mob flagged hostile within
        # range PLUS any mob actively attacking us from range (is_targeting_me),
        # so a skeleton shooting from afar is fought back rather than only fled.
        # The chosen target is the nearest of that set (finish the closest first
        # then the reflex re-evaluates next tick and moves to the next one).
        targets = self._aggressive_targets(state, self._sp_hostile_dist)
        if targets:
            target = targets[0]
            try:
                raw_dist = target.get("distance")
                dist = float(raw_dist) if raw_dist is not None else None
            except (TypeError, ValueError):
                dist = None
            health = extra.get("health")
            low_health = (
                isinstance(health, (int, float)) and health <= self._sp_low_health
            )
            target_id = str(target.get("name") or "")
            # Track the fail counter against the specific mob being fought; a
            # new/changed threat resets it.
            if self._fight_target != target_id:
                self._fight_target = target_id
                self._fight_fail_count = 0

            # --- Per-mob strategy override (§17) -----------------------------
            # Before the generic power-ratio decision, give a special creature
            # (creeper/enderman/…) the chance to impose its own tactic via the
            # world-agnostic core registry. Keyed on the canonical entity id —
            # never a keyword. Returns a full plan dict, or None to fall through
            # to the generic reflex below. Fully fail-safe.
            override = apply_combat_strategy(ENVIRONMENT, target, extra)
            if override is not None:
                return override

            # --- Power-aware fight/flee decision -----------------------------
            # A disarmed body (no melee weapon, no usable ranged) should not
            # trade blows with a real mob — it flees, UNLESS the mob is weak
            # enough to punch out. An armed body compares its own combat power
            # (weapon + armor + health) against the mob's (health + attack) and
            # engages only when the ratio says the fight is winnable. All
            # structural numeric telemetry, never keywords.
            own_power = self._own_power(extra)
            mob_power = self._mob_power(target)
            ratio = own_power / mob_power if mob_power > 0 else 999.0
            disarmed = self._is_disarmed(extra)
            weak_mob = self._is_weak_mob(target)
            if disarmed:
                power_ok = weak_mob
            else:
                power_ok = ratio >= self._sp_engage_ratio
            # Escalation to flight is driven PRIMARILY by low health (the body
            # is actually losing), with the fail counter only a secondary
            # safeguard against swinging forever at an unreachable mob.
            escalated = self._fight_fail_count >= self._sp_fight_max_fails
            if self._sp_fight_back and power_ok and not low_health and not escalated:
                # Ranged vs melee: prefer a bow/crossbow shot when we carry one
                # with ammo AND the target is far enough to warrant it (closing
                # to melee would take damage on the way). Structural: uses the
                # bridge-reported has_ranged_weapon/ranged_ammo flags and the
                # numeric distance — never a name keyword.
                has_ranged = bool(extra.get("has_ranged_weapon"))
                if (
                    self._sp_use_ranged
                    and has_ranged
                    and dist is not None
                    and dist >= self._sp_ranged_min_dist
                ):
                    return {
                        "threat": "defend",
                        "verb": "shoot",
                        "payload": {"target": target_id} if target_id else {},
                        "reason": {
                            "distance": dist,
                            "health": health,
                            "ranged": True,
                            "ammo": extra.get("ranged_ammo"),
                            "targets": len(targets),
                            "fails": self._fight_fail_count,
                            "own_power": round(own_power, 2),
                            "mob_power": round(mob_power, 2),
                            "ratio": round(ratio, 2),
                        },
                    }
                # Melee engage: close the gap and swing. The bridge ``attack``
                # verb already runs a GoalFollow that walks the body up to
                # melee reach before striking, so a mob anywhere inside the
                # hostile radius (``_sp_hostile_dist``) is REACHABLE on foot —
                # the reflex must approach and fight it, not flee just because
                # it is momentarily beyond arm's length.
                #
                # The original passive death loop (chasing a kiting skeleton
                # that shoots and never lets the gap close) is NOT handled here
                # by a distance cutoff — that made the body flee every ordinary
                # melee mob at 3.5–8 blocks and never finish a fight. It is
                # handled by the EXISTING escalation above: a mob that keeps us
                # from landing hits either drains our health (→ ``low_health``
                # → the outer flee branch) or trips the ``_fight_fail_count``
                # cap (→ ``escalated`` → flee). Both are structural and require
                # no knowledge of whether the mob is ranged, so no name keyword
                # is ever needed.
                return {
                    "threat": "defend",
                    "verb": "attack",
                    "payload": {"target": target_id} if target_id else {},
                    "reason": {
                        "distance": dist,
                        "health": health,
                        "ranged": False,
                        "best_melee_damage": extra.get("best_melee_damage"),
                        "targets": len(targets),
                        "fails": self._fight_fail_count,
                        "own_power": round(own_power, 2),
                        "mob_power": round(mob_power, 2),
                        "ratio": round(ratio, 2),
                    },
                }
            # Escalate to flight — outmatched, disarmed vs a non-weak mob, low
            # health, fight-back disabled, or the fail cap tripped.
            return {
                "threat": "flee",
                "verb": "flee",
                "payload": {},
                "reason": {
                    "distance": dist,
                    "health": health,
                    "low_health": low_health,
                    "fight_back": self._sp_fight_back,
                    "escalated": escalated,
                    "targets": len(targets),
                    "own_power": round(own_power, 2),
                    "mob_power": round(mob_power, 2),
                    "ratio": round(ratio, 2),
                    "disarmed": disarmed,
                    "weak_mob": weak_mob,
                    "power_ok": power_ok,
                },
            }

        # 6. Night shelter — proactive, lower priority than an active fight.
        #
        # No hostile is close enough to fight/flee (handled above), but it is
        # NIGHT and hostiles are within the wider shelter radius: wall the body
        # in / sleep in a roofed bed BEFORE a mob closes to melee. A torch is
        # deliberately NOT used — an exposed body is still reachable; the point
        # is an actual enclosed refuge. Structural: numeric is_day flag + mob
        # distance only, never keyword logic. Latched per night so it does not
        # re-issue every tick once enclosed.
        if self._sp_night_shelter:
            extra_now = state.extra or {}
            is_day = extra_now.get("is_day")
            if is_day is False:
                # Reset the per-night latch would happen in daytime (see the
                # daytime branch below). Only shelter once per night unless the
                # body left cover.
                if self._sheltered_last_day is not False:
                    near = self._aggressive_targets(state, self._sp_shelter_dist)
                    # Also shelter if ANY hostile mob is within the wider radius,
                    # not only ones actively targeting us (a wandering zombie at
                    # night is a reason to enclose). _aggressive_targets already
                    # covers hostile-flagged + targeting mobs.
                    if near:
                        self._sheltered_last_day = False
                        return {
                            "threat": "night_shelter",
                            "verb": "shelter",
                            "payload": {},
                            "reason": {
                                "is_day": False,
                                "hostiles_near": len(near),
                                "shelter_dist": self._sp_shelter_dist,
                            },
                        }
            elif is_day is True:
                # Day returned — arm the shelter reflex for the next night.
                self._sheltered_last_day = True

        # 7. Morning bunker exit — lowest priority, only when nothing else is
        #    pressing (no fight/flee/shelter above triggered).
        #
        # If Synth spent the night in a dug bunker (no base), when DAY returns
        # and the body is still buried under a ceiling (no open sky) it should
        # carve a walkable ascending staircase back to the surface — a jump-up
        # stair (one block up, one block forward) rather than a pit it cannot
        # climb out of. Structural: numeric ``is_day`` + the bridge
        # ``sky_access`` flag only, never keyword logic. The "no base reachable"
        # gate and the actual staircase action live in the async
        # ``_run_survival_guard`` (base lookup is async); here we only surface
        # the candidate plan and manage the per-day latch so it fires once.
        if self._sp_morning_exit:
            extra_now = state.extra or {}
            is_day = extra_now.get("is_day")
            sky = extra_now.get("sky_access")
            if is_day is True:
                if sky is True:
                    # Out in the open — arm the reflex and clear the latch so a
                    # fresh burial next night/morning re-triggers it.
                    self._surfaced_last_day = True
                elif sky is False and self._surfaced_last_day is not False:
                    # Buried under a ceiling in daylight: candidate for exit.
                    self._surfaced_last_day = False
                    return {
                        "threat": "morning_exit",
                        "verb": "climb_staircase",
                        "payload": {},
                        "reason": {
                            "is_day": True,
                            "sky_access": False,
                        },
                    }
            elif is_day is False:
                # Night — re-arm so the next morning fires the exit if buried.
                self._surfaced_last_day = True

        # Safe — clear any lingering fight state.
        if self._fight_target is not None:
            self._fight_target = None
            self._fight_fail_count = 0
        return None

    async def _run_survival_guard(self, state: "WorldState") -> dict[str, Any] | None:
        """Execute the highest-priority survival reflex, if any.

        Returns a completed ``motor_step`` result dict when it acted (so the
        caller returns immediately), or ``None`` to let normal movement run.
        Fail-safe: any error degrades to ``None``. All decisions are structural
        (see :meth:`_survival_threat`).
        """
        if not self._sp_enabled:
            return None

        # Anti-flap: after issuing a corrective action, skip one tick so the
        # world state can reflect it before we react again.
        if self._survival_cooldown_ticks > 0:
            self._survival_cooldown_ticks -= 1
            return None

        try:
            plan = self._survival_threat(state)
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} survival threat assessment failed: {exc}")
            return None
        if plan is None:
            self._last_survival_threat = None
            self._last_survival_reason = None
            return None

        threat = plan["threat"]
        verb = plan["verb"]
        payload = plan.get("payload") or {}

        # Translate the reflex verb into a concrete connector action.
        try:
            if verb == "respawn":
                result = await self.act("respawn", {})
            elif verb == "goto_surface":
                result = await self._act_goto_surface(state)
            elif verb == "flee":
                # Fleeing while submerged: a horizontal ``goto`` has no walkable
                # block underwater, so the pathfinder cannot move the body and
                # it just hangs in the water taking hits (the "ferma nell'acqua"
                # symptom). Surface first — emerging breaks line-of-sight/reach
                # of aquatic mobs (drowned) and puts solid ground back under the
                # feet so a subsequent flee can actually run. Structural: reuses
                # the numeric ``is_in_water`` flag, no keyword logic.
                if (state.extra or {}).get("is_in_water"):
                    result = await self._act_goto_surface(state)
                else:
                    result = await self._act_flee(state)
            elif verb == "keep_distance":
                # Special-mob tactic (creeper/enderman): back off only when the
                # mob is inside the keep-distance radius, otherwise hold — no
                # full sprint. Reuses the flee vector at a shorter range. When
                # the mob is already far enough, do nothing and let the will
                # beat decide. Structural (distance only).
                entity = payload.get("entity") if isinstance(payload, dict) else None
                near = False
                if isinstance(entity, dict):
                    try:
                        ed = entity.get("distance")
                        near = (
                            isinstance(ed, (int, float))
                            and float(ed) <= self._KEEP_DISTANCE
                        )
                    except (TypeError, ValueError):
                        near = False
                if near:
                    result = await self._act_flee(
                        state, distance=self._FLEE_DISTANCE / 2
                    )
                else:
                    result = {"acted": False, "reason": "keep_distance_hold"}
            elif verb == "attack":
                result = await self.act("attack", payload)
                # Count this defend tick; escalate on repeated engagement.
                self._fight_fail_count += 1
            elif verb == "shoot":
                result = await self.act("shoot", payload)
                # A shot is also a defend tick for escalation purposes.
                self._fight_fail_count += 1
            elif verb == "shelter":
                # Base-retreat first: if Synth has a home nearby, head BACK to
                # it (reusing ``goto``) instead of burying the body wherever it
                # is standing. Sheltering-in-place stays only as the last resort
                # when no base is reachable. Structural: numeric distance vs the
                # retreat radius, never keyword logic. Fully fail-safe — any
                # error falls through to the in-place shelter.
                retreat = await self._retreat_to_base(state)
                if retreat is not None:
                    result = retreat
                    threat = "night_retreat"
                else:
                    result = await self.act("shelter", payload)
            elif verb == "climb_staircase":
                # Morning bunker exit. Only carve a staircase when Synth has NO
                # reachable base — a registered base means it has a home to
                # path back to, not a bunker to dig out of. Structural async
                # base lookup; fail-safe. If a base IS reachable we let the
                # will/motor beats handle returning home instead.
                if await self._has_reachable_base(state):
                    self._surfaced_last_day = True  # not a bunker; disarm
                    return None
                result = await self.act("climb_staircase", payload)
            else:  # pragma: no cover - defensive
                return None
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} survival action '{verb}' failed: {exc}")
            return None

        self._last_survival_threat = threat
        self._last_survival_reason = plan.get("reason")
        # Respawn / surface take a moment; give the world a tick to update.
        if verb in ("respawn", "goto_surface"):
            self._survival_cooldown_ticks = 1
        # Sheltering (navigate to bed / place walls / dig in) takes several
        # seconds; hold the reflex a couple of ticks so it does not re-issue
        # while the previous attempt is still resolving.
        elif verb == "shelter":
            self._survival_cooldown_ticks = 2
        # Carving a staircase up takes several seconds; hold a few ticks so the
        # reflex does not re-issue while the previous climb is still resolving.
        elif verb == "climb_staircase":
            self._survival_cooldown_ticks = 3
        acted = _result_acted(result)
        log_info(
            f"{LOG_PREFIX} survival reflex: {threat} -> {verb} "
            f"(reason={plan.get('reason')})"
        )
        return {"acted": acted, "reason": f"survival:{threat}"}

    async def _retreat_to_base(self, state: "WorldState") -> dict[str, Any] | None:
        """Head back to the nearest registered base at night, if one is close.

        The fix for Synth being buried underground far from home: instead of
        walling the body in wherever it is standing, the night-shelter reflex
        first checks whether a base is within ``_base_retreat_radius`` and, if
        so, walks the body toward that base's anchor (reusing ``goto`` — no new
        verb). Returns a ``motor_step``-style result dict when it retreated, or
        ``None`` to let the caller fall back to the in-place shelter (no base,
        base too far, or any error). Purely structural (numeric distance), never
        keyword logic; fully fail-safe.
        """
        if not self._base_enabled:
            return None
        try:
            pos = self._position_from_state(state)
            if pos is None:
                pos = await self._live_position()
            if pos is None:
                return None
            base = await mc_bases.get_nearest_base(pos)
            if not isinstance(base, dict):
                return None
            anchor = base.get("anchor")
            distance = base.get("distance")
            if not isinstance(anchor, dict):
                return None
            if not isinstance(distance, (int, float)) or (
                float(distance) > self._base_retreat_radius
            ):
                return None
            ax = anchor.get("x")
            ay = anchor.get("y")
            az = anchor.get("z")
            if ax is None or az is None:
                return None
            goto_payload: Dict[str, Any] = {"x": float(ax), "z": float(az)}
            if ay is not None:
                goto_payload["y"] = int(float(ay))
            await self.act("goto", goto_payload)
            log_info(
                f"{LOG_PREFIX} night retreat -> base "
                f"'{base.get('name')}' at {goto_payload} (dist={distance})"
            )
            return {"acted": True, "action": "goto", "reason": "night_retreat"}
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} base retreat failed: {exc}")
            return None

    async def _has_reachable_base(self, state: "WorldState") -> bool:
        """Whether a registered base is within ``_base_retreat_radius``.

        Used by the morning bunker-exit reflex to decide it is a genuine
        bunker (dig out) vs a body that simply has a home to path back to. A
        base registered but far away does NOT count as reachable, so a body
        buried on the far side of the world still digs out. Purely structural
        (numeric distance vs the retreat radius); fully fail-safe → ``False``
        (i.e. "no home nearby, treat as a bunker") on any error.
        """
        if not self._base_enabled:
            return False
        try:
            pos = self._position_from_state(state)
            if pos is None:
                pos = await self._live_position()
            if pos is None:
                return False
            base = await mc_bases.get_nearest_base(pos)
            if not isinstance(base, dict):
                return False
            distance = base.get("distance")
            return isinstance(distance, (int, float)) and (
                float(distance) <= self._base_retreat_radius
            )
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} reachable-base check failed: {exc}")
            return False

    @staticmethod
    def _position_from_state(state: "WorldState") -> Dict[str, float] | None:
        """Extract the body's ``{x, y, z}`` from a WorldState, or None.

        Reads the structural ``position`` field; fully fail-safe.
        """
        try:
            pos = getattr(state, "position", None)
            if not isinstance(pos, dict):
                extra = state.extra or {}
                pos = extra.get("position")
            if not isinstance(pos, dict):
                return None
            x = pos.get("x")
            y = pos.get("y")
            z = pos.get("z")
            if x is None or y is None or z is None:
                return None
            return {"x": float(x), "y": float(y), "z": float(z)}
        except (TypeError, ValueError):
            return None

    async def _act_goto_surface(self, state: "WorldState") -> Any:
        """Swim straight up to escape drowning (mineflayer ``jump`` in water).

        Delegates to the bridge ``surface`` verb, which holds the ``jump``
        control (the body ascends while submerged) and polls the head block
        until it clears the liquid. This is the correct way to emerge in open
        water: a pathfinder ``goto`` toward an air coordinate above has no
        walkable block to stand on and never surfaces the body — it just keeps
        drowning (oxygen falls 14→3). Purely structural — no keyword logic.
        Fail-safe: returns a plain dict on any error.
        """
        return await self.act("surface", {})

    async def _act_flee(
        self, state: "WorldState", distance: float | None = None
    ) -> Any:
        """Run away from the nearest threat (mindcraft moveAway style).

        Picks a destination ``distance`` blocks (default ``_FLEE_DISTANCE``) in
        the direction opposite the nearest hostile (or, when fleeing fire,
        simply forward) and gotos it. A shorter ``distance`` is used by the
        keep-distance special-mob tactic. Purely numeric vector math — no
        keyword logic. Fail-safe.
        """
        flee_dist = self._FLEE_DISTANCE if distance is None else float(distance)
        try:
            pos = state.position if isinstance(state.position, dict) else None
            if pos is None:
                return {"acted": False, "reason": "no_position"}
            px = float(pos["x"])
            pz = float(pos["z"])
            py = float(pos["y"])
        except (KeyError, TypeError, ValueError):
            return {"acted": False, "reason": "bad_position"}

        hostile = self._nearest_hostile(state)
        dx, dz = 0.0, 0.0
        hp = hostile.get("position") if isinstance(hostile, dict) else None
        if isinstance(hp, dict):
            try:
                hx = float(hp["x"])
                hz = float(hp["z"])
                # Vector pointing away from the hostile.
                dx = px - hx
                dz = pz - hz
            except (KeyError, TypeError, ValueError):
                dx, dz = 0.0, 0.0
        norm = (dx * dx + dz * dz) ** 0.5
        if norm < 1e-3:
            # No usable direction (fire, or hostile on top of us): reuse the
            # persistent exploration heading to pick a consistent escape line.
            dx = float(math.cos(self._explore_heading))
            dz = float(math.sin(self._explore_heading))
            norm = 1.0
        tx = int(px + (dx / norm) * flee_dist)
        tz = int(pz + (dz / norm) * flee_dist)
        return await self.act("goto", {"x": tx, "y": int(py), "z": tz})

    async def evaluate_goal_completion(
        self, goal: Dict[str, Any] | None, world_state: WorldState | None
    ) -> Dict[str, Any]:
        """Judge whether ``goal`` is already satisfied by the live inventory.

        World-owned half of the core goal debrief (see
        ``core.vessel_goal_debrief`` and AGENTS.md §5c). A goal is considered
        structurally satisfied when its free text names a concrete **target** or
        **product** item and that exact game id is already present in the live
        inventory in quantity >= 1:

          * **Gather goals** (a natural block/entity target, e.g. *"gather oak
            logs"*): the derived ``target_name`` — from the goal's explicit
            field or :func:`target_names.derive_target` — sits in the inventory.
          * **Craft/build goals** (a produced item, e.g. *"craft a crafting
            table"*): the product ids named in the goal text
            (:func:`target_names.derive_products`) sit in the inventory.

        **Multi-part goals need ALL their products.** A goal that names several
        distinct products (e.g. *"a proper door, a warm torch, four walls and a
        roof"*) is only satisfied when **every** named product is in the
        inventory. Completing on *any one* product auto-closed multi-part build
        goals the moment a single ingredient landed (observed churn: a cottage
        goal re-authored every ~60 s because one torch/plank/table in inventory
        marked the whole goal done). A derived raw-material target (e.g.
        ``oak_log`` from *"gather a bit more wood"*) is an **ingredient** of a
        product-naming goal, never its outcome, so it does not satisfy such a
        goal. Stepped goals (a non-empty ``steps`` plan) drive their own
        progression in the goal store and are never auto-completed here.

        **Quantities are honoured.** *"Gather 20 oak logs"* requires 20 in the
        inventory, not just one: the goal text's stated count
        (:func:`target_names.derive_quantity`) is compared against the live
        count, so a goal is not auto-closed the moment a single item lands
        (observed: "gather a bit more wood" goals completing on one log). A text
        without a number keeps the pre-existing presence semantics (>= 1).

        This is the exact Minecraft-name exception the rest of this adapter
        already relies on: matching against canonical game item ids is
        structural, not natural-language intent detection. It never decides what
        to do — only whether the goal's concrete outcome already exists. Fully
        fail-safe: any error / no recognizable item → ``{"satisfied": False}``.
        """
        try:
            if not isinstance(goal, dict) or world_state is None:
                return {"satisfied": False}
            extra = getattr(world_state, "extra", None) or {}
            inv = extra.get("inventory_counts")
            if not isinstance(inv, dict) or not inv:
                return {"satisfied": False}

            description = " ".join(
                str(goal.get(field) or "") for field in ("description", "note")
            ).strip()

            # A stepped goal self-completes when its step plan is exhausted
            # (goal store ``update_active_goal``). The debrief must not close a
            # plan mid-flight from a partial inventory match.
            steps = goal.get("steps")
            if isinstance(steps, list) and steps:
                return {"satisfied": False}

            products = mc_target_names.derive_product_quantities(description)
            if products:
                # Craft/build goal: the outcome is the FULL named product set,
                # each at its stated quantity. ALL products must exist in that
                # count — a single ingredient is progress, not completion, and
                # never closes a multi-part build goal.
                for product, qty in products.items():
                    try:
                        if int(inv.get(product, 0)) < qty:
                            return {"satisfied": False}
                    except (TypeError, ValueError):
                        return {"satisfied": False}
                return {
                    "satisfied": True,
                    "reason": "product_in_inventory",
                    "item": next(iter(products)),
                }

            # No named product: a pure gather/hunt goal — single target check,
            # also quantity-aware.
            target_name = goal.get("target_name")
            if not target_name:
                derived = mc_target_names.derive_target(description)
                if derived:
                    target_name = derived.get("target_name")
            if isinstance(target_name, str) and target_name:
                try:
                    qty = mc_target_names.derive_quantity(description, target_name)
                    if int(inv.get(target_name, 0)) >= qty:
                        return {
                            "satisfied": True,
                            "reason": "target_in_inventory",
                            "item": target_name,
                        }
                except (TypeError, ValueError):
                    pass

            return {"satisfied": False}
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"[minecraft] evaluate_goal_completion failed: {exc}")
            return {"satisfied": False}

    # Structural map: a goal target_kind → the successful action event_types
    # that count as "the goal was reached by an action actually taken", plus the
    # metadata payload keys that carry the acted-upon game id for that verb.
    # These come straight from ``get_world_actions`` payload schemas (mine→target,
    # collect_block→name, place/craft/smelt→item, attack/shoot→target). All are
    # canonical Minecraft ids — the same explicitly-authorized id exception the
    # rest of this adapter uses; never natural-language intent.
    _HISTORY_BLOCK_EVENTS: tuple[str, ...] = (
        "action_mine",
        "action_collect_block",
        "action_place",
    )
    _HISTORY_ENTITY_EVENTS: tuple[str, ...] = (
        "action_attack",
        "action_shoot",
    )
    _HISTORY_CRAFT_EVENTS: tuple[str, ...] = (
        "action_craft",
        "action_smelt",
    )
    _HISTORY_TARGET_KEYS: tuple[str, ...] = ("target", "name", "item")

    async def evaluate_goal_completion_from_history(
        self,
        goal: Dict[str, Any] | None,
        session_id: str | None,
        world_state: WorldState | None = None,
    ) -> Dict[str, Any]:
        """Judge whether ``goal`` was reached by an action actually taken.

        Complements :meth:`evaluate_goal_completion` (which reads only the live
        inventory + world state). Many goals leave **no inventory trace** — you
        place a block, kill a mob, or say something — so an inventory scan never
        marks them done. This half inspects the session's ``vessel_activity_log``
        (the structured audit of every outbound action) and confirms completion
        when a **successful action row** exists whose logged metadata target id
        matches the goal's concrete structural target:

          * **block** target (mine/gather/place goals) → a logged
            ``mine``/``collect_block``/``place`` on that exact block id;
          * **entity** target (kill goals) → a logged ``attack``/``shoot`` on
            that exact entity id;
          * **crafted product** (from :func:`target_names.derive_products`) → a
            logged ``craft``/``smelt`` of that exact item id.

        **Multi-part goals need ALL their products.** When the goal names
        several distinct products, every named product must have a matching
        successful craft/smelt row — completing on *any one* action auto-closed
        multi-part build goals the moment a single ingredient step succeeded
        (observed churn: a cottage goal completed because one ``collect_block
        oak_log`` row matched a raw-material target the goal merely mentioned as
        an intermediate step). A derived raw-material target (e.g. ``oak_log``
        from *"gather a bit more wood"*) is an **ingredient** of a
        product-naming goal and never satisfies it; only a goal naming *no
        product* falls back to its single block/entity target. Stepped goals (a
        non-empty ``steps`` plan) drive their own progression and are never
        auto-completed here.

        **Quantities are honoured.** Each matching row's logged result
        (``_result.data`` — ``collected`` for mine/collect, ``count`` for
        craft/smelt) is summed toward the goal's stated quantity
        (:func:`target_names.derive_quantity`). *"Gather 20 oak logs"* is only
        satisfied when the session's successful collects total >= 20. When a row
        carries no count (older rows / world events) each match counts as 1.

        Matching is purely structural, by canonical Minecraft id (the same
        authorized id exception this adapter already uses) — it never parses the
        goal's or the log's free text for intent. Fully fail-safe: any error /
        no session / no recognizable target → ``{"satisfied": False}``.
        """
        try:
            if not isinstance(goal, dict) or not session_id:
                return {"satisfied": False}

            description = " ".join(
                str(goal.get(field) or "") for field in ("description", "note")
            ).strip()

            # A stepped goal self-completes when its step plan is exhausted
            # (goal store ``update_active_goal``). The debrief must not close a
            # plan mid-flight from a partial history match.
            steps = goal.get("steps")
            if isinstance(steps, list) and steps:
                return {"satisfied": False}

            products = mc_target_names.derive_product_quantities(description)
            if products:
                # Craft/build goal: the outcome is the FULL named product set,
                # each at its stated quantity. ALL products must be evidenced by
                # successful craft/smelt rows whose logged counts sum to the
                # quantity — a single ingredient action is progress, not
                # completion.
                from core.vessel_diary_compactor import load_activity_rows

                rows = await load_activity_rows(session_id)
                matched: Dict[str, int] = {}
                for row in rows:
                    event_type = row.get("event_type") or ""
                    if event_type not in self._HISTORY_CRAFT_EVENTS:
                        continue
                    meta = row.get("metadata") or {}
                    _res = meta.get("_result")
                    if _res is not None and not bool(_res.get("ok")):
                        continue
                    ids = {
                        str(meta.get(key)).strip()
                        for key in self._HISTORY_TARGET_KEYS
                        if isinstance(meta.get(key), (str, int))
                        and str(meta.get(key)).strip()
                    }
                    count = self._row_result_count(_res, default=1)
                    for product in products:
                        if product in ids:
                            matched[product] = matched.get(product, 0) + count
                for product, qty in products.items():
                    if int(matched.get(product, 0)) < qty:
                        return {"satisfied": False}
                return {
                    "satisfied": True,
                    "reason": "action_in_history",
                    "event_type": "action_craft",
                    "item": next(iter(products)),
                }

            # No named product: a pure gather/hunt goal — single target check.
            target_kind = goal.get("target_kind")
            target_name = goal.get("target_name")
            if not target_name:
                derived = mc_target_names.derive_target(description)
                if derived:
                    target_kind = derived.get("target_kind")
                    target_name = derived.get("target_name")

            wanted_block = (
                target_name
                if isinstance(target_name, str)
                and target_name
                and target_kind == "block"
                else None
            )
            wanted_entity = (
                target_name
                if isinstance(target_name, str)
                and target_name
                and target_kind == "entity"
                else None
            )
            if not (wanted_block or wanted_entity):
                return {"satisfied": False}

            qty = mc_target_names.derive_quantity(description, target_name or "")
            from core.vessel_diary_compactor import load_activity_rows

            rows = await load_activity_rows(session_id)
            matched_total = 0
            for row in rows:
                event_type = row.get("event_type") or ""
                meta = row.get("metadata") or {}
                _res = meta.get("_result")
                if _res is not None and not bool(_res.get("ok")):
                    continue
                ids = {
                    str(meta.get(key)).strip()
                    for key in self._HISTORY_TARGET_KEYS
                    if isinstance(meta.get(key), (str, int))
                    and str(meta.get(key)).strip()
                }
                if not ids:
                    continue
                hit = False
                if (
                    wanted_block
                    and event_type in self._HISTORY_BLOCK_EVENTS
                    and wanted_block in ids
                ):
                    hit = True
                if (
                    wanted_entity
                    and event_type in self._HISTORY_ENTITY_EVENTS
                    and wanted_entity in ids
                ):
                    hit = True
                if not hit:
                    continue
                matched_total += self._row_result_count(_res, default=1)
                if matched_total >= qty:
                    return {
                        "satisfied": True,
                        "reason": "action_in_history",
                        "event_type": event_type,
                        "item": wanted_block or wanted_entity,
                    }
            return {"satisfied": False}
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(
                f"[minecraft] evaluate_goal_completion_from_history failed: {exc}"
            )
            return {"satisfied": False}

    @staticmethod
    def _row_result_count(_res: Any, default: int = 1) -> int:
        """Extract the item count a logged action result reports, else ``default``.

        The connector logs ``_result.data`` with a per-verb count field:
        ``collected`` for mine/collect_block, ``count`` for craft/smelt. Reads
        those structurally (int/float coercion, bounded to >= 1). Missing or
        malformed data degrades to ``default`` (1) so older rows still count as
        one match. Purely structural — never inspects text.
        """
        try:
            if not isinstance(_res, dict):
                return default
            data = _res.get("data")
            if not isinstance(data, dict):
                return default
            raw = data.get("collected")
            if raw is None:
                raw = data.get("count")
            if raw is None:
                return default
            count = int(raw)
            return max(1, count)
        except (TypeError, ValueError, Exception):
            return default

    async def get_active_goal(self) -> Dict[str, Any] | None:
        """Return the active Minecraft goal from the scoped goal store."""
        try:
            return await mc_goals.get_active_goal()
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"[minecraft] get_active_goal failed: {exc}")
            return None

    async def complete_active_goal(
        self, reason: str = "auto_completed"
    ) -> Dict[str, Any]:
        """Mark the active Minecraft goal ``done`` via the scoped goal store."""
        try:
            note = f"[debrief] {reason}"
            return await mc_goals.update_active_goal(
                status=mc_goals.STATUS_DONE, note=note
            )
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"[minecraft] complete_active_goal failed: {exc}")
            return {"status": "error", "message": str(exc)}

    async def get_bases(self) -> list[Dict[str, Any]]:
        """Return the bases (homes) Synth registered in this world.

        Concretises the core :meth:`VesselConnectorBase.get_bases` hook by
        delegating to the scoped Minecraft base store. Fail-safe: any error
        degrades to an empty list.
        """
        try:
            return await mc_bases.list_bases()
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"[minecraft] get_bases failed: {exc}")
            return []

    async def get_active_quest(self) -> Dict[str, Any] | None:
        """Return the active Ender Dragon questline milestone (reference only).

        Concretises the core :meth:`VesselConnectorBase.get_active_quest` hook by
        delegating to the scoped Minecraft questline store. The quest is a
        *direction* Synth may bind its freely-authored goal to, never a script
        (AGENTS.md §5c). Fail-safe: any error (or a disabled questline) degrades
        to ``None``.
        """
        if not self._quests_enabled:
            return None
        try:
            return await quests.get_active_quest()
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"[minecraft] get_active_quest failed: {exc}")
            return None

    async def on_entity_killed(self, mob_kind: str) -> None:
        """Advance the active quest's kill objective for a slain mob.

        Concretises the core :meth:`VesselConnectorBase.on_entity_killed` hook.
        Called when the bridge reports the bot killed an entity; forwards the
        mob game id to the questline store's structural kill counter (e.g. the
        Ender Dragon milestone). Fail-safe: any error is swallowed.
        """
        if not self._quests_enabled or not mob_kind:
            return None
        try:
            await quests.record_kill(str(mob_kind))
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"[minecraft] on_entity_killed failed: {exc}")
        return None

    async def motor_step(self, goal: Dict[str, Any] | None) -> Dict[str, Any]:
        """Fast reflexive step toward the active goal — **no LLM, no cognition**.

        Called on a short timer by the interface scheduler (see
        ``core.vessel_beat.resolve_motor_interval`` and AGENTS.md §5c). It never
        builds a prompt, never runs a cognition turn and never writes a diary
        entry — it just reads the live :class:`WorldState` and applies purely
        **structural** rules to pick and execute exactly one concrete bridge
        action. It deliberately does *not* read the goal's free text (that would
        be keyword matching); the goal only supplies (a) whether the body should
        be moving at all and (b) an optional **numeric travel destination** that
        the will beat chose — the *what/where* is cognition's, the *how* is this
        reflex.

        Rules (no keywords — purely on affordance shape/distance and numeric
        coordinates):
          * No connection or no goal → do nothing (idle until the will beat
            gives the body something to pursue).
          * A benign affordance already **within reach** → interact with it via
            its structural ``verb`` (``use`` / ``mine``), regardless of any
            distant destination — grab what is right in front of you. Hostile
            ``attack`` verbs are never triggered reflexively — aggression stays
            a deliberate, will-driven act.
          * The goal has a travel destination we have **not reached yet** →
            steer toward that coordinate (``goto`` x/z), *even if* far-off
            benign affordances exist. This is what makes movement
            *goal-relevant*: when what Synth is looking for is not in the
            current area (e.g. no trees in a desert), it heads toward the place
            cognition chose instead of chasing ubiquitous incidental scenery
            (sand, stone) and freezing in place.
          * A benign affordance farther away **and no pending destination** →
            ``goto`` it (walk closer).
          * No numeric destination but the goal names a structural block/entity
            **target** (chosen by cognition from the live scan) → ``goto`` that
            named thing so the bridge resolves it by exact id and pathfinds to
            it. This is the "idea → technical action" translation that stops the
            body from circling when the direction lived only in the goal text.
          * Nothing to interact with, no destination and no target → march
            along a **persistent heading** (a directed exploration line that
            only rotates on arrival/stall), never a random ``wander`` that
            drifts in tight loops resembling circling.

        Fail-safe: any error degrades to ``{"acted": False, ...}`` and never
        raises into the scheduler.
        """
        try:
            if not self._connected:
                return {"acted": False, "reason": "not_connected"}

            # Fetch the world state FIRST — before honouring a missing goal — so
            # the self-preservation reflex can save the body even when it has no
            # goal to pursue (e.g. idle underwater). Danger does not wait for a
            # will beat.
            state = await self.get_world_state()
            if state is None:
                return {"acted": False, "reason": "no_world_state"}

            # Highest-priority reflex: survive. Runs every tick, pre-empting all
            # normal movement, and is the only branch allowed to act with no
            # goal. Structural only (numeric health/oxygen/distance + game enum
            # block/entity ids) — never keyword matching. Returns a completed
            # result dict when it acted, else None to fall through.
            survival = await self._run_survival_guard(state)
            if survival is not None:
                return survival

            # Staticity ward — runs BEFORE the ``no goal`` early-return and every
            # other movement branch, so it covers the cases the tick-to-tick
            # ``_stuck_position_ticks`` watchdog cannot: the body parked in one
            # spot with *no goal at all*, or endlessly ``mine``/``use``ing an
            # in-reach block without displacing. When the body has lingered in
            # the same small area for too many ticks, break the parking by
            # rotating the exploration heading and marching to a fresh, distant
            # waypoint — the body ends up *somewhere else*, exactly the ward the
            # user asked for. Purely positional (no goal text, no keywords).
            if self._update_staticity_ward(state.position):
                self._explore_heading += self._EXPLORE_TURN_RAD
                forward = self._reproject_forward(state.position, self._explore_heading)
                if forward is not None:
                    await self.act("goto", {"x": forward["x"], "z": forward["z"]})
                    log_info(
                        f"{LOG_PREFIX} staticity ward: parked too long -> "
                        f"relocating to ({forward['x']:.0f}, {forward['z']:.0f})"
                    )
                    return {
                        "acted": True,
                        "action": "goto",
                        "destination": forward,
                        "reason": "staticity_ward",
                    }
                # No usable position to reproject from — last-resort roam.
                await self.act("wander", {})
                return {"acted": True, "action": "wander", "reason": "staticity_ward"}

            if not goal:
                return {"acted": False, "reason": "no_goal"}

            affordances = (state.extra or {}).get("affordances") or []
            # Reflexes stay peaceful: skip hostile targets, act only on benign
            # affordances. Structural filter — verb, not name.
            benign = [
                a
                for a in affordances
                if isinstance(a, dict) and a.get("verb") in ("use", "mine")
            ]

            # Does the goal name a place cognition chose to head for, and are we
            # still short of it? This numeric destination is the structural
            # signal that "what I'm looking for is *not* here" — so it must win
            # over merely *walking toward* incidental far-off scenery.
            dest = self._goal_destination(goal)
            # A numeric destination we have already reached for *this* goal is
            # "spent": ignore it so the body explores beyond the point instead
            # of pacing back and forth to a static waypoint the slow will beat
            # has not yet refreshed. A new goal (new key) revives its dest.
            if (
                dest is not None
                and self._goal_key(goal) == self._consumed_destination_key
            ):
                dest = None
            travel_pending = False
            travel_remaining: float | None = None
            if dest is not None:
                travel_remaining = self._horizontal_distance(state.position, dest)
                travel_pending = (
                    travel_remaining is None or travel_remaining > self._ARRIVAL_RADIUS
                )
                # Progress watchdog for an unreachable waypoint. Track the best
                # (smallest) distance we have managed toward *this* destination
                # key; if it stops improving for a while the point is
                # effectively unreachable (pathfinder oscillates without ever
                # entering the arrival ring) — consume it so the body stops
                # pacing back and forth and marches on. Purely numeric.
                dest_key = self._goal_key(goal)
                if dest_key != self._travel_dest_key:
                    self._travel_dest_key = dest_key
                    self._travel_best_remaining = travel_remaining
                    self._travel_stall_ticks = 0
                elif travel_remaining is not None:
                    if (
                        self._travel_best_remaining is None
                        or travel_remaining
                        <= self._travel_best_remaining - self._TRAVEL_PROGRESS_EPS
                    ):
                        self._travel_best_remaining = travel_remaining
                        self._travel_stall_ticks = 0
                    else:
                        self._travel_stall_ticks += 1
                if self._travel_stall_ticks >= self._STALE_TRAVEL_TICKS:
                    self._consumed_destination_key = dest_key
                    self._travel_stall_ticks = 0
                    dest = None
                    travel_pending = False
                    travel_remaining = None

            # Physical-motion watchdog — **global**, not per-branch. The
            # distance-based watchdog above only sees a *numeric* waypoint whose
            # ``remaining`` oscillates; it is blind to the far more general case
            # the user hit: the body simply **stops moving** while the motor
            # keeps re-issuing a ``goto`` every tick. That ``goto`` may be
            # steering to a numeric coordinate, but it may equally be walking
            # toward a named block/entity ``target`` (the goal_target branch or
            # an out-of-reach affordance) that the pathfinder can never actually
            # reach (across water, up a cliff, inside a dug pit at y=60) — the
            # observed "goto reason=None dest=None, body pinned at one spot
            # forever" report. None of those branches carry a distance number,
            # so the only reliable signal is the body's *own* displacement.
            #
            # Measure the horizontal distance the body actually covered since the
            # previous tick; a tick that moved it less than ``_STUCK_MOVE_EPS``
            # blocks counts as "not moving". After ``_STUCK_POSITION_TICKS`` such
            # ticks the body is wedged regardless of what it is aiming at, so we
            # (a) consume any numeric destination, and (b) raise ``force_march``
            # to suppress the named-target / affordance-goto branches below and
            # fall straight through to the directional march, which reprojects a
            # fresh forward waypoint *and rotates the heading* — the one action
            # guaranteed to break free of a wedge. Purely numeric (measured
            # motion), no keywords, robust to skipped/laggy ticks.
            force_march = False
            cur_pos = state.position if isinstance(state.position, dict) else None
            moved: float | None = None
            if cur_pos is not None and self._last_body_position is not None:
                moved = self._horizontal_distance(cur_pos, self._last_body_position)
            if cur_pos is not None:
                try:
                    self._last_body_position = {
                        "x": float(cur_pos["x"]),
                        "z": float(cur_pos["z"]),
                    }
                except (KeyError, TypeError, ValueError):
                    self._last_body_position = None
            if moved is not None and moved < self._STUCK_MOVE_EPS:
                self._stuck_position_ticks += 1
            else:
                self._stuck_position_ticks = 0
            if self._stuck_position_ticks >= self._STUCK_POSITION_TICKS:
                self._stuck_position_ticks = 0
                force_march = True
                # Rotate the exploration heading so the forced march breaks out
                # in a *new* direction rather than re-aiming at the same wedge.
                self._explore_heading += self._EXPLORE_TURN_RAD
                if dest is not None:
                    self._consumed_destination_key = self._goal_key(goal)
                    self._travel_dest_key = None
                    self._travel_best_remaining = None
                    self._travel_stall_ticks = 0
                    dest = None
                    travel_pending = False
                    travel_remaining = None

            # The structural target cognition chose (a specific block/entity id
            # from the live scan). This is the "idea → technical action"
            # translation: instead of the body wandering in circles because the
            # *direction* lived only in the goal's free text, cognition names a
            # concrete thing to walk to and the bridge resolves it by exact id.
            goal_target = self._goal_target(goal)

            # A benign affordance in reach is only worth stopping for when we
            # are NOT mid-journey. If cognition chose a destination we have not
            # reached yet (``travel_pending``), travelling wins over grabbing
            # incidental scenery — otherwise, in a desert (sand/sandstone are
            # *always* within reach) the body would ``use``/``mine`` the ground
            # under its feet every tick and never actually walk to the goal,
            # exactly the "wandering in circles" trap the will beat warns about.
            if benign and not travel_pending and not force_march:
                # Affordances arrive distance-sorted (nearest first). Skip any
                # in-reach one we already interacted with on the previous tick:
                # a live scan keeps re-surfacing the same adjacent block/entity,
                # so re-``use``/``mine``ing it every 3 s would pin the body on
                # the spot forever (the "freezes inert at one point" bug). By
                # dropping it we fall through to the travel/march branches and
                # keep moving. Structural (kind + exact id) — never keyword-based.
                fresh: list[Dict[str, Any]] = []
                for a in benign:
                    a_name = a.get("target")
                    a_key = f"{a.get('kind')}:{a_name}" if a_name else None
                    if a_key is not None and a_key == self._last_reflex_interaction:
                        continue
                    # Never let the incidental mine reflex destroy a light /
                    # utility block (torches, lanterns, glowstone, …). Breaking
                    # a player's torches to "grab scenery" removes the light
                    # they placed to keep a path lit — pure vandalism. Skip
                    # such blocks here so the body falls through to travel /
                    # march instead. Structural (exact block id vs a denylist
                    # of mcData ids) — never keyword/natural-language matching.
                    # A block cognition *deliberately* named as its goal target
                    # is still honoured further below; this only suppresses the
                    # unasked-for reflex grab.
                    if (
                        a.get("kind") == "block"
                        and a_name in self._REFLEX_NO_MINE_BLOCKS
                    ):
                        continue
                    fresh.append(a)
                target = fresh[0] if fresh else None
                distance = target.get("distance") if target else None
                name = target.get("target") if target else None

                within_reach = isinstance(distance, (int, float)) and (
                    distance <= self._MOTOR_REACH
                )
                if within_reach and name and target is not None:
                    # Something new is literally in front of us and we have
                    # nowhere to be — grab/use it, then remember it so the next
                    # tick moves on instead of repeating the same interaction.
                    self._last_reflex_interaction = f"{target.get('kind')}:{name}"
                    if target.get("kind") == "block":
                        # Reflex mining is only ever justified for a block
                        # cognition *deliberately* named as the goal target: the
                        # will / action beat decides *what* to mine, the motor
                        # only executes it. An incidental block that merely
                        # happens to be the nearest benign affordance (the dirt /
                        # grass / stone under the body's feet) must NOT be dug —
                        # that is the reported "digs a block beneath itself for
                        # no apparent reason" behaviour, pure world vandalism.
                        # So gate the incidental ``mine`` on the block matching
                        # the goal's already-validated ``target_name`` by exact
                        # id (structural, never keyword/free-text). When it does
                        # not match we fall through to the travel / march
                        # branches and keep moving instead of scarring the world.
                        if (
                            goal_target is not None
                            and goal_target.get("kind") == "block"
                            and goal_target.get("name") == name
                        ):
                            await self.act("mine", {"target": name})
                            return {"acted": True, "action": "mine", "target": name}
                        # Not the goal target — do not mine incidental terrain.
                        # Fall through past this affordance to travel / march.
                    else:
                        await self.act("use", {"target": name})
                        return {"acted": True, "action": "use", "target": name}

                # Out of reach. Only chase this affordance if we have *no
                # chosen destination at all*; otherwise heading toward random
                # far scenery (e.g. ubiquitous sand in a desert) would trap the
                # body in place and never reach the goal. Crucially, this must
                # also step aside once a destination has been *reached* (not
                # just while still travelling): if we kept chasing incidental
                # affordances after arrival, the body would never fall into the
                # arrival/anti-stall block below and would freeze on the spot.
                # So gate on ``dest is None`` (no destination) rather than
                # ``not travel_pending`` (which is also true once arrived).
                #
                # And chase it only when it is the block cognition *deliberately*
                # named as the goal target (exact id): walking toward an
                # incidental terrain block the will beat never asked for is the
                # same aimless wandering as digging it — the body should instead
                # fall through to the directional march and genuinely explore.
                # Entities stay chase-able (a mob/villager out of reach is
                # inherently salient); only a non-goal *block* is skipped.
                # Structural (kind + exact id), never keyword/free-text.
                if name and dest is None:
                    is_goal_block = (
                        goal_target is not None
                        and goal_target.get("kind") == "block"
                        and goal_target.get("name") == name
                    )
                    if target is not None and (
                        target.get("kind") != "block" or is_goal_block
                    ):
                        await self.act("goto", {"target": name})
                        return {"acted": True, "action": "goto", "target": name}

            # Honour a self-chosen travel destination so movement stays attuned
            # to the goal even when incidental affordances litter the path.
            if dest is not None:
                if travel_pending:
                    # Still en route: steer toward the goal, but do NOT lock the
                    # body to it until arrival — the loops above already let a
                    # benign affordance in reach interrupt the trip, so plans
                    # can change mid-travel if something turns up.
                    self._arrival_goal_key = None
                    self._arrival_stall_ticks = 0
                    payload: Dict[str, Any] = {"x": dest["x"], "z": dest["z"]}
                    if "y" in dest:
                        payload["y"] = dest["y"]
                    await self.act("goto", payload)
                    return {
                        "acted": True,
                        "action": "goto",
                        "destination": dest,
                        "remaining": travel_remaining,
                    }

                # Arrived at the destination. Mark it *consumed* for this goal
                # so subsequent ticks stop steering back to the same static
                # waypoint (which caused the "same path back and forth" pacing):
                # from now on the body explores beyond the point via the
                # directional march, until a fresh goal supplies a new dest.
                self._consumed_destination_key = self._goal_key(goal)
                # Reset the unreachable-waypoint watchdog: we arrived cleanly.
                self._travel_dest_key = None
                self._travel_best_remaining = None
                self._travel_stall_ticks = 0
                # The will beat (a slow LLM) may not have handed the body a
                # fresh objective yet — but the world is live and Synth must not
                # freeze here waiting: count how long we have been idling on this
                # same goal, and once it goes stale invent our own next waypoint
                # so the body keeps exploring and its plans can change on their
                # own.
                key = self._goal_key(goal)
                if key != self._arrival_goal_key:
                    self._arrival_goal_key = key
                    self._arrival_stall_ticks = 1
                else:
                    self._arrival_stall_ticks += 1

                if self._arrival_stall_ticks >= self._STALE_ARRIVAL_TICKS:
                    forward = self._reproject_forward(
                        state.position, self._explore_heading
                    )
                    # Fan the heading out for the next reprojection so repeated
                    # stalls explore new directions instead of one line.
                    self._explore_heading += self._EXPLORE_TURN_RAD
                    self._arrival_stall_ticks = 0
                    if forward is not None:
                        await self.act("goto", {"x": forward["x"], "z": forward["z"]})
                        return {
                            "acted": True,
                            "action": "goto",
                            "destination": forward,
                            "reason": "stale_arrival_reproject",
                        }

                # Not stale yet: roam locally so the body keeps searching around
                # the target while giving cognition a brief chance to re-aim.
                await self.act("wander", {})
                return {"acted": True, "action": "wander", "reason": "arrived"}

            # No numeric destination, but cognition named a concrete block/
            # entity to reach. Let the bridge resolve it structurally by exact
            # id (``resolveTargetBlock``) and pathfind to it — this is the whole
            # point of the fix: the body walks *toward the named thing* instead
            # of wandering in circles because the direction was only in words.
            if goal_target is not None and not force_march:
                self._arrival_goal_key = None
                self._arrival_stall_ticks = 0
                # If the named block target is already within reach, MINE it
                # rather than re-issuing ``goto`` at a thing the body is already
                # standing next to. The generic in-reach branch above only fires
                # when there is no travel destination *and* the block surfaced as
                # a benign affordance; a named goal target can reach this branch
                # with the block right in front of it (e.g. the affordance was
                # just consumed and re-surfaced, or the will beat named a target
                # the body already arrived at) and would otherwise walk in place.
                # Structural match only: same ``kind`` + exact ``target`` id +
                # numeric distance ≤ reach — never keyword inspection. The
                # bridge's ``mine`` picks up the drop and reports the delta, so a
                # single reflex mine advances a gather goal. Entities are never
                # mined here (mining is block-only); they fall through to goto.
                if goal_target["kind"] == "block":
                    reachable = next(
                        (
                            a
                            for a in affordances
                            if isinstance(a, dict)
                            and a.get("kind") == "block"
                            and a.get("target") == goal_target["name"]
                            and isinstance(a.get("distance"), (int, float))
                            and a.get("distance") <= self._MOTOR_REACH
                        ),
                        None,
                    )
                    if reachable is not None:
                        name = goal_target["name"]
                        self._last_reflex_interaction = f"block:{name}"
                        # Interacting resets the arrival-stall watchdog: we are
                        # making progress on this target, not looping on it.
                        self._named_target_arrival_key = None
                        self._named_target_arrival_ticks = 0
                        await self.act("mine", {"target": name})
                        return {
                            "acted": True,
                            "action": "mine",
                            "target": name,
                            "target_kind": "block",
                        }
                result = await self.act("goto", {"target": goal_target["name"]})
                # Record the structural 3-state outcome (arrived / not_found /
                # unreachable) so the next will beat can re-plan when the named
                # target can't be reached. Keyword-free — see
                # ``_record_target_outcome``.
                self._record_target_outcome(state, goal_target, result)
                target_key = f"{goal_target['kind']}:{goal_target['name']}"
                # Arrived at the named block but the affordance-based ``mine``
                # branch above never fired (the block's scan distance did not
                # fall inside ``_MOTOR_REACH`` even though the bridge pathfinder
                # stopped within its own ``range`` of it — the two reach numbers
                # disagree, which is exactly what left the body looping ``goto``
                # on a block it was already standing next to). When we *arrive*
                # at a block target, try mining it directly: the bridge's own
                # ``mine`` re-resolves the nearest matching block and reports the
                # inventory delta, so this is the authoritative "can I actually
                # interact?" test. A successful mine advances the gather goal;
                # a miss ("no matching block") means the target is genuinely not
                # here and the watchdog below releases it. Block-only — entities
                # are never mined. Structural (kind + exact id + ``arrived``).
                if (
                    goal_target["kind"] == "block"
                    and self._last_target_result == "arrived"
                ):
                    mine_result = await self.act(
                        "mine", {"target": goal_target["name"]}
                    )
                    if getattr(mine_result, "ok", False):
                        name = goal_target["name"]
                        self._last_reflex_interaction = f"block:{name}"
                        self._named_target_arrival_key = None
                        self._named_target_arrival_ticks = 0
                        return {
                            "acted": True,
                            "action": "mine",
                            "target": name,
                            "target_kind": "block",
                            "target_result": "arrived",
                        }
                # Arrival-stall watchdog: if we keep *arriving* at the same named
                # target without ever managing to interact with it (the block/
                # entity never surfaces as a benign affordance in reach), stop
                # re-issuing ``goto`` at it forever. Count consecutive same-target
                # arrivals and, past the threshold, give up on this exact target
                # for now — fall through to the directional march so the body
                # explores new ground while the slow will beat re-plans. Purely
                # structural (kind + exact id + ``arrived`` outcome) — no keywords.
                if self._last_target_result == "arrived":
                    if target_key != self._named_target_arrival_key:
                        self._named_target_arrival_key = target_key
                        self._named_target_arrival_ticks = 1
                    else:
                        self._named_target_arrival_ticks += 1
                    if self._named_target_arrival_ticks >= self._STALE_ARRIVAL_TICKS:
                        # Reached but never interactable — release this target
                        # and let cognition re-aim.
                        self._named_target_arrival_key = None
                        self._named_target_arrival_ticks = 0
                        forward = self._reproject_forward(
                            state.position, self._explore_heading
                        )
                        self._explore_heading += self._EXPLORE_TURN_RAD
                        if forward is not None:
                            await self.act(
                                "goto", {"x": forward["x"], "z": forward["z"]}
                            )
                            return {
                                "acted": True,
                                "action": "goto",
                                "target": goal_target["name"],
                                "target_kind": goal_target["kind"],
                                "target_result": "arrived_idle",
                                "destination": forward,
                                "reason": "target_arrived_idle_reproject",
                            }
                else:
                    # A non-``arrived`` outcome (still travelling / unreachable /
                    # not_found) means we are not stuck on arrival — reset.
                    self._named_target_arrival_key = None
                    self._named_target_arrival_ticks = 0
                return {
                    "acted": True,
                    "action": "goto",
                    "target": goal_target["name"],
                    "target_kind": goal_target["kind"],
                    "target_result": self._last_target_result,
                }

            # Nothing to work with and nowhere chosen to go. Rather than a random
            # ``wander`` (which drifts in tight loops and looks like circling),
            # keep marching along a *persistent* heading so the body covers real
            # ground while the slow will beat decides on a concrete target. The
            # heading only rotates when a leg is reached or the walk stalls, so
            # exploration is a directed line, not a spin in place.
            forward = self._reproject_forward(state.position, self._explore_heading)
            march_key = self._goal_key(goal)
            if march_key != self._arrival_goal_key:
                self._arrival_goal_key = march_key
                self._arrival_stall_ticks = 1
            else:
                self._arrival_stall_ticks += 1
            if self._arrival_stall_ticks >= self._STALE_ARRIVAL_TICKS:
                # Reached (or stuck near) the current leg — turn and lay the
                # next one so the march sweeps new ground instead of one line.
                self._explore_heading += self._EXPLORE_TURN_RAD
                self._arrival_stall_ticks = 0
            if forward is not None:
                await self.act("goto", {"x": forward["x"], "z": forward["z"]})
                return {
                    "acted": True,
                    "action": "goto",
                    "destination": forward,
                    "reason": "directional_march",
                }

            # Reprojection failed (no position) — last-resort roam.
            await self.act("wander", {})
            return {"acted": True, "action": "wander"}
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} motor_step failed: {exc}")
            return {"acted": False, "reason": "error", "error": str(exc)}

    def get_world_actions(self) -> Dict[str, Dict[str, Any]]:
        """Minecraft-specific gameplay verbs added on top of the core set.

        These are the concrete ways Synth can *play* Minecraft: navigate,
        gather, build and explore on its own. The core Vessel plugin namespaces
        each as ``vessel_minecraft_<verb>`` and dispatches it back to
        :meth:`act` (→ the Node bridge). None declare ``external_effects`` —
        they stay on the Fast-Lane like the rest of the Vessel verbs.

        The verbs are intentionally world-specific: goals, crafting-style
        gathering and block placement are what makes *this* world playable, and
        they live here (not in the world-agnostic core) so a different world can
        expose its own vocabulary. Field names are descriptive but never
        keyword-matched by the code — the bridge resolves targets structurally
        by the exact block/entity name Synth read from ``observe``/``scan``.
        """
        return {
            "shoot": {
                "description": (
                    "Fire a ranged weapon (a bow or crossbow you are carrying, "
                    "with arrows) at a nearby thing by name (target), e.g. a "
                    "hostile mob attacking you from a distance. You equip the "
                    "weapon, aim at it and loose a shot. Only works when you "
                    "actually carry a bow/crossbow and have arrows — otherwise "
                    "close in and attack instead. Use this to hit things that "
                    "are too far or too dangerous to melee."
                ),
                "required_fields": [],
                "optional_fields": ["target"],
                "security_level": "low",
            },
            "goto": {
                "description": (
                    "Walk to somewhere in the world. Give either exact "
                    "coordinates (x, y, z) or the name of a nearby thing to "
                    "walk up to (target). Use this to reach something you saw "
                    "while looking around before you interact with it."
                ),
                "required_fields": [],
                "optional_fields": [
                    "x",
                    "y",
                    "z",
                    "range",
                    "target",
                    "search_radius",
                    "timeout_ms",
                ],
                "security_level": "low",
            },
            "mine": {
                "description": (
                    "Break and collect a nearby block by its exact Minecraft id "
                    "(target) from the latest observation, such as "
                    "dark_oak_log, stone or iron_ore. Never substitute a generic "
                    "or different variant when the observation provides the id. "
                    "When any wood-log variant is acceptable, target the explicit "
                    "structural pattern *_log. "
                    "Walk to it first if it is out of reach and use the best "
                    "tool you are carrying. This is how you gather materials."
                ),
                "required_fields": ["target"],
                "optional_fields": ["search_radius", "timeout_ms"],
                "security_level": "low",
            },
            "collect_block": {
                "description": (
                    "Gather several of the same block by its exact Minecraft id "
                    "(name) from the latest observation, such as dark_oak_log. "
                    "When any wood-log variant is acceptable, use the explicit "
                    "structural pattern *_log. "
                    "Give how many you want (count). You walk to each one, break it and "
                    "pick up the drop, repeating until you have that many or "
                    "there are none left nearby. This is the reliable way to "
                    "stock up on a material for a goal."
                ),
                "required_fields": ["name"],
                "optional_fields": [
                    "count",
                    "search_radius",
                    "timeout_ms",
                ],
                "security_level": "low",
            },
            "place": {
                "description": (
                    "Put down one of the things you are carrying (item, given "
                    "by its exact name) at your feet. This is how you build and "
                    "arrange the world around you."
                ),
                "required_fields": ["item"],
                "optional_fields": [],
                "security_level": "low",
            },
            "drop": {
                "description": (
                    "Drop an item you are carrying at your current position "
                    "for a nearby player to pick up (item, given by its exact "
                    "Minecraft id, e.g. wooden_pickaxe or stick). Optionally "
                    "drop several items with count."
                ),
                "required_fields": ["item"],
                "optional_fields": ["count"],
                "security_level": "low",
            },
            "craft": {
                "description": (
                    "Make a new item out of the materials you are carrying, "
                    "given by its exact Minecraft id (item), e.g. oak_planks, "
                    "stick or wooden_pickaxe. Do not pluralize or paraphrase "
                    "the id. Optionally craft several at once (count). If the "
                    "recipe needs a workbench you walk to a nearby crafting "
                    "table on your own. This is how you turn what you gathered "
                    "into something more useful."
                ),
                "required_fields": ["item"],
                "optional_fields": ["count", "search_radius", "timeout_ms"],
                "security_level": "low",
            },
            "smelt": {
                "description": (
                    "Cook or smelt something in a nearby furnace, given by its "
                    "exact input name (item), e.g. turn raw_iron into "
                    "iron_ingot or cook food. Optionally smelt several (count) "
                    "and name the 'fuel' you want to use (coal by default). You "
                    "walk to a nearby furnace on your own. Smelting takes time "
                    "in-world, so check your inventory again on a later moment "
                    "to see the results. This is how you refine ores into "
                    "usable metal."
                ),
                "required_fields": ["item"],
                "optional_fields": [
                    "count",
                    "fuel",
                    "search_radius",
                    "timeout_ms",
                ],
                "security_level": "low",
            },
            "equip": {
                "description": (
                    "Wear or hold one of the things you are carrying, given by "
                    "its exact name (item), e.g. put on an iron_chestplate or "
                    "iron_helmet, hold a sword, or raise a shield. The right "
                    "body slot is chosen for you (armor goes where it belongs); "
                    "pass 'slot' only if you want to override it. This is how "
                    "you actually protect yourself with the armor you made."
                ),
                "required_fields": ["item"],
                "optional_fields": ["slot"],
                "security_level": "low",
            },
            "inventory": {
                "description": (
                    "Check what you are currently carrying. Purely "
                    "informational — it changes nothing. Use it to decide what "
                    "you can build with or whether you need to gather more."
                ),
                "required_fields": [],
                "optional_fields": [],
                "security_level": "low",
            },
            "wander": {
                "description": (
                    "Roam to a random reachable spot nearby to explore on your "
                    "own, with no particular destination. Use it to discover "
                    "new surroundings when you have nothing specific in mind."
                ),
                "required_fields": [],
                "optional_fields": ["radius", "timeout_ms"],
                "security_level": "low",
            },
            "dig_staircase": {
                "description": (
                    "Dig your way down while leaving yourself a walkable way "
                    "back up. Instead of digging straight down (which leaves a "
                    "pit you cannot climb out of), you carve a descending "
                    "staircase: each step goes one block down and one block "
                    "forward, so the same corridor becomes a stair you can walk "
                    "back up on foot. Give how many steps down you want with "
                    "'depth'. Use this whenever you need to go underground to "
                    "reach ores or caves and still be able to return to the "
                    "surface later. It carves toward the way you are currently "
                    "facing; pass 'yaw' only if you want to force a direction."
                ),
                "required_fields": [],
                "optional_fields": ["depth", "yaw"],
                "security_level": "low",
            },
            "return_surface": {
                "description": (
                    "Climb back up to the surface out of a dry pit or tunnel by "
                    "pillaring up: you jump and place a block under your feet "
                    "again and again, rising one block each time, until you "
                    "reach open sky. Use this when you are stuck underground "
                    "with no staircase and need to get out. You must be "
                    "carrying blocks to build with; give 'height' for how far "
                    "up to climb, or 'target_y' to stop at a specific height. "
                    "Pass 'item' only if you want to use a particular block as "
                    "scaffolding. (This is different from swimming up out of "
                    "water — use it when you are on dry land underground.)"
                ),
                "required_fields": [],
                "optional_fields": ["height", "target_y", "item"],
                "security_level": "low",
            },
            "climb_staircase": {
                "description": (
                    "Carve a walkable staircase UP to the surface out of a "
                    "bunker or tunnel. Instead of pillaring straight up (a pole "
                    "you can only fall off), you build a diagonal ramp: each "
                    "step goes one block up and one block forward, placing a "
                    "solid tread under your next foothold and clearing the "
                    "space above it, so the same corridor becomes a stair you "
                    "can walk and jump up on foot — one block up, one block "
                    "forward. Use this when you dug yourself underground with no "
                    "staircase and need to get back to open sky. You must be "
                    "carrying blocks to place the treads; give 'height' for how "
                    "many steps up, or 'target_y' to stop at a specific height. "
                    "Pass 'item' to use a particular block, or 'yaw' to force a "
                    "direction."
                ),
                "required_fields": [],
                "optional_fields": ["height", "target_y", "item", "yaw"],
                "security_level": "low",
            },
            "scan": {
                "description": (
                    "Take a wider, tunable survey of your surroundings than a "
                    "normal glance: which presences and notable things are "
                    "around you and how far. Purely perceptual — it changes "
                    "nothing."
                ),
                "required_fields": [],
                "optional_fields": ["radius", "max_entities", "max_blocks"],
                "security_level": "low",
            },
            "goals": {
                "description": (
                    "Recall what you are currently trying to do in this world "
                    "and the things you set out to do before. Purely "
                    "informational. Use it to remember your own intentions and "
                    "reflect on how you want to play."
                ),
                "required_fields": [],
                "optional_fields": [],
                "security_level": "low",
            },
            "set_goal": {
                "description": (
                    "Decide, in your own words, what you want to do in this "
                    "world right now, and make it your goal. There is no fixed "
                    "list to pick from — say whatever you actually feel like "
                    "doing (build something, explore a biome, tame an animal, "
                    "just relax by the water…). Put it in 'description'. This "
                    "becomes your single active goal and guides how you play "
                    "until you finish or change your mind. This is how you play "
                    "your own game, not a script. Just say what you want in "
                    "'description' — do NOT try to spell out the ordered "
                    "sub-steps yourself. If the goal is a bigger project that "
                    "takes several stages (for example crafting a full iron "
                    "armor set, or building a house), a separate planning pass "
                    "will look up the correct Minecraft order (gather the "
                    "prerequisites, craft the tools, mine, smelt, craft, wear, "
                    "and so on) and fill the concrete steps in for you shortly "
                    "after, so you always have the right tools before you need "
                    "them. You will then work through those steps one at a time "
                    "and mark each done with 'update_goal' (advance). If what "
                    "you want is NOT in this "
                    "wood, or you want to reach a different biome), pick a "
                    "place to head toward and give its coordinates in "
                    "'destination_x' and 'destination_z' (from your position "
                    "and what you can see): your body will then walk that way on "
                    "its own while you play. Leave them out if you are happy "
                    "where you are. IMPORTANT — so your body actually walks to "
                    "what you want instead of drifting in circles, whenever your "
                    "goal is about reaching or gathering a specific thing you "
                    "can see, name that thing structurally: set 'target_kind' to "
                    "'block' or 'entity' and 'target_name' to its EXACT id from "
                    "what you observed (e.g. target_kind='block', "
                    "target_name='<exact observed block id>'; or "
                    "target_kind='entity', target_name='<exact observed entity id>'). "
                    "Pick the name verbatim from your scan — "
                    "do not invent one. Your body will then head straight to the "
                    "nearest one. Use 'coordinate' only when you mean a bare "
                    "spot with the destination fields."
                ),
                "required_fields": ["description"],
                "optional_fields": [
                    "note",
                    "destination_x",
                    "destination_z",
                    "target_kind",
                    "target_name",
                ],
                "security_level": "low",
            },
            "update_goal": {
                "description": (
                    "Reflect on the goal you set for yourself: jot a 'note' on "
                    "how it is going in your own words, or set 'status' to "
                    "'done' when you feel you have achieved it or 'abandoned' "
                    "if you have changed your mind. You are the judge of your "
                    "own progress — nothing counts it for you. If your goal has "
                    "an ordered plan of sub-steps and you have just finished "
                    "the current one, set 'advance' to true to move on to the "
                    "next step. You can also rewrite the whole plan by passing "
                    "a new 'steps' list, or jump to a specific step with "
                    "'current_step' (0-based). If you realise you need to travel "
                    "somewhere else to make progress, set a new "
                    "'destination_x'/'destination_z' and your body will head "
                    "there; you do not have to touch it if the direction still "
                    "feels right. If you now want to head for a specific thing "
                    "you can see, re-aim your body by setting 'target_kind' "
                    "('block' or 'entity') and 'target_name' to its EXACT id "
                    "from what you observed (verbatim from your scan) — your "
                    "body will then walk to the nearest one instead of "
                    "wandering."
                ),
                "required_fields": [],
                "optional_fields": [
                    "note",
                    "status",
                    "advance",
                    "steps",
                    "current_step",
                    "destination_x",
                    "destination_z",
                    "target_kind",
                    "target_name",
                ],
                "security_level": "low",
            },
            "lookup_knowledge": {
                "description": (
                    "Look up how this world works before you commit to a plan: "
                    "search the game's rules and knowledge base for what a goal "
                    "actually needs. Give a short 'query' of the things you want "
                    "to understand (for example the exact block or item id, or "
                    "what tool a resource requires). You get back a few short, "
                    "factual notes — e.g. that a certain ore can only be mined "
                    "with a specific tool you must craft first, or what a recipe "
                    "needs. Purely informational; it changes nothing in the "
                    "world. Use it to order your sub-steps correctly (gather the "
                    "prerequisite before the thing that needs it) instead of, "
                    "say, trying to mine ore bare-handed."
                ),
                "required_fields": ["query"],
                "optional_fields": ["limit"],
                "security_level": "low",
            },
            "set_base": {
                "description": (
                    "Claim a place in this world as one of your bases — a home "
                    "you build up, store things at, shelter or sleep in, and "
                    "return to. Give it a 'name' in your own words (there is no "
                    "list to pick from — call it whatever it means to you). By "
                    "default the base is claimed right where your body is "
                    "standing; give explicit 'x'/'y'/'z' coordinates only if you "
                    "mean somewhere else you can see. You can keep several bases: "
                    "claiming a new name adds one, reusing a name updates it. Add "
                    "an optional 'kind' (for example 'home', 'mine', 'farm') and "
                    "a short 'note'. Having a base matters for survival: when "
                    "night falls and danger is near, your body heads back to the "
                    "nearest base instead of walling itself in wherever it "
                    "happens to be."
                ),
                "required_fields": ["name"],
                "optional_fields": ["x", "y", "z", "kind", "note"],
                "security_level": "low",
            },
            "list_bases": {
                "description": (
                    "Recall the bases (homes) you have claimed in this world — "
                    "their names, kinds and coordinates — so you can decide "
                    "whether to head back to one, build it up, or claim a new "
                    "place. Takes no fields."
                ),
                "required_fields": [],
                "optional_fields": [],
                "security_level": "low",
            },
            "build_base": {
                "description": (
                    "Actually build a first shelter with your own hands: a small "
                    "walled, roofed room with a door, a torch inside so nothing "
                    "spawns in the dark, and a crafting table — and, if you carry "
                    "a bed, a bed to sleep and set your respawn. Your body places "
                    "the blocks from your inventory around where you stand (give "
                    "explicit 'x'/'y'/'z' only if you want to build somewhere "
                    "else you can see). Give it a 'name' so it is remembered as a "
                    "base you can return to. If you are missing blocks the build "
                    "will be partial and tell you what you still need — gather "
                    "stone/wood, a door, a torch and a crafting table first."
                ),
                "required_fields": ["name"],
                "optional_fields": ["x", "y", "z", "kind", "note"],
                "security_level": "low",
            },
        }

    async def _act_lookup_knowledge(
        self, payload: Dict[str, Any]
    ) -> VesselActionResult:
        """Handle the ``lookup_knowledge`` verb locally (no bridge round-trip).

        Reads the curated knowledge base via :meth:`lookup_knowledge` and
        returns the matched entries so the goal-expansion Drone (and cognition)
        can consult the game's rules before ordering sub-steps. Fail-safe — a
        missing/empty KB degrades to an ``ok=True`` empty result rather than
        raising into the chain.
        """
        try:
            query = str(payload.get("query") or "").strip()
            raw_limit = payload.get("limit")
            try:
                limit = int(raw_limit) if raw_limit is not None else 5
            except (TypeError, ValueError):
                limit = 5
            # Explicit verb / goal-expansion Drone: allow the live path (network
            # + LLM), unlike the cache-only automatic beat path.
            entries = await self.lookup_knowledge(query, limit=limit, cache_only=False)
            notes = [
                {
                    "title": e.get("title"),
                    "text": e.get("text"),
                    "url": e.get("url"),
                }
                for e in entries
                if isinstance(e, dict)
            ]
            return VesselActionResult(
                ok=True,
                detail=f"found {len(notes)} knowledge note(s)",
                data={"query": query, "notes": notes},
            )
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"{LOG_PREFIX} lookup_knowledge failed: {exc}")
            return VesselActionResult(
                ok=False, detail="knowledge lookup failed", data={}
            )

    def describe_capabilities(self) -> Dict[str, Any]:
        return {
            "movement": True,
            "chat": True,
            "perception": True,
            "interaction": True,
            "local": True,
        }

    # ------------------------------------------------------------------
    # Knowledge base (live game wiki + web fallback — reference, never a script)
    # ------------------------------------------------------------------

    def get_knowledge_wiki_sources(self) -> list[WikiSource]:
        """Return the Minecraft knowledge sources for the core client.

        Declares the live `minecraft.wiki <https://minecraft.wiki>`_ MediaWiki
        endpoint. The world-agnostic client
        (:mod:`plugins.rift_vessel.knowledge_client`) consumes these to search,
        fetch, summarise, and cache pages — the source URLs are never hardcoded
        in the core.
        """
        return [wiki_client.MINECRAFT_WIKI_SOURCE]

    async def lookup_knowledge(
        self, query: str, limit: int = 5, *, cache_only: bool = False
    ) -> list[dict[str, Any]]:
        """Return knowledge notes relevant to ``query`` (a structural token).

        ``query`` is expected to be structural game tokens (a goal
        ``target_name``, item/block ids, whitespace-joined) — never a
        natural-language sentence — so matching stays keyword-free and
        language-agnostic. Delegates to :func:`wiki_client.lookup`, which drives
        the local-first precedence ``local cache → minecraft.wiki → generic web
        search`` and returns each note as ``{"title", "text", "url"}``.

        ``cache_only`` (set by the automatic will/action-beat path) forbids any
        network or LLM call, serving only already-cached pages. Fully fail-safe:
        any error degrades to ``[]`` rather than breaking the beat.
        """
        try:
            lim = max(1, int(limit))
        except (TypeError, ValueError):
            lim = 5
        try:
            return await wiki_client.lookup(query, limit=lim, cache_only=cache_only)
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"{LOG_PREFIX} lookup_knowledge failed: {exc}")
            return []

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def probe_external_liveness(self) -> bool:
        """Return True if the Node bridge is already alive and embodied.

        Cheap, read-only ``GET /health`` against the bridge address resolved
        from the saved plugin config — it never starts the bridge, never issues
        ``/connect``, and never touches ``self._session``/``self._connected``.
        Lets the interface's boot-time reattach adopt a bridge that stayed
        logged into the world across a SyntH restart (or a connector drop) so
        the session can be re-opened and the autonomy beats resume, without
        spawning anything. Fully fail-safe: any error means "not live".
        """
        base_url = self._resolve_base_url({})
        try:
            timeout = aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_SEC)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{base_url}/health") as resp:
                    health = await resp.json()
        except Exception as exc:
            log_debug(f"{LOG_PREFIX} liveness probe failed at {base_url}: {exc}")
            return False
        if not isinstance(health, dict) or not health.get("ok"):
            return False
        env = str(health.get("environment") or "").strip().lower()
        return bool(health.get("connected")) and (not env or env == ENVIRONMENT)

    def get_in_world_name(self) -> str | None:
        """Return the in-world Minecraft username other players use to address
        the bot.

        Mirrors the resolution the provisioner applies when launching the
        bridge (``interface/minecraft_provisioner.py``): an explicit
        ``MINECRAFT_BOT_USERNAME_OVERRIDE`` wins, otherwise ``SYNTH_NAME``. This
        lets direct-address detection recognise an in-world chat line that names
        the bot by its Minecraft nickname even when it differs from Synth's
        persona name.
        """
        try:
            from core.config_manager import config_registry

            override = str(
                config_registry.get_value(
                    "MINECRAFT_BOT_USERNAME_OVERRIDE",
                    "",
                    group="plugins",
                    component="minecraft_vessel",
                )
                or ""
            ).strip()
            if override:
                return override
            name = str(config_registry.get_value("SYNTH_NAME", "") or "").strip()
            return name or None
        except Exception:  # pragma: no cover - defensive
            return None

    def get_world_identity(self) -> str | None:
        """Return a stable structural token identifying *which* Minecraft
        server Synth is embodied in, for the ``<world>`` path level and the
        goal-store ``world`` scope.

        Derived from the resolved server ``host:port`` (per-connect override,
        else the configured ``MINECRAFT_SERVER_HOST``/``MINECRAFT_SERVER_PORT``)
        so a login always resumes progression on the same concrete server.
        Purely structural — the raw address, never a keyword-derived label.

        The Docker loopback remap (``127.0.0.1`` -> ``host.docker.internal``)
        is intentionally *not* applied here: both point at the same logical
        server, so the identity must stay identical whether SyntH runs in a
        container or on the host. The interface slugifies the returned token
        into a path-safe form. Fully fail-safe — returns ``None`` on any error
        so the caller falls back to the legacy shared scope.
        """
        try:
            settings = self._connect_settings or {}
            host = settings.get("host") or config_registry.get_value(
                "MINECRAFT_SERVER_HOST",
                "127.0.0.1",
                group="plugins",
                component="minecraft_vessel",
            )
            host_str = str(host or "127.0.0.1").strip()
            # Canonicalise loopback so container/host deployments agree on the
            # same world token (structural normalisation, not keyword logic).
            if host_str.lower() in _LOOPBACK_HOSTS or host_str == _HOST_GATEWAY_NAME:
                host_str = "localhost"
            port = settings.get("port") or config_registry.get_value(
                "MINECRAFT_SERVER_PORT",
                44383,
                group="plugins",
                component="minecraft_vessel",
            )
            try:
                port_int = int(port)
            except (TypeError, ValueError):
                port_int = 44383
            return f"{host_str}:{port_int}"
        except Exception:  # pragma: no cover - defensive
            return None


# ----------------------------------------------------------------------
# Per-mob combat strategy overrides (Minecraft content for the generic
# core mechanism in ``vessel_combat_strategy``). Keyed on the bridge's
# canonical structural entity id (game enum, e.g. ``"creeper"``) — never a
# display name / keyword. Each returns the reflex plan shape
# ``{"threat", "verb", "payload", "reason"}`` or ``None`` to fall through to
# the generic power-ratio decision. Pure/structural, Fast-Lane only.
# ----------------------------------------------------------------------


def _mc_target_distance(entity: Dict[str, Any]) -> float:
    """Structural distance to a target entity (large when unknown)."""
    try:
        dist = entity.get("distance")
        return float(dist) if isinstance(dist, (int, float)) else 999.0
    except (TypeError, ValueError):
        return 999.0


def _mc_strategy_creeper(
    entity: Dict[str, Any], extra: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Creeper: never chase — it explodes on contact.

    A creeper's raw health/attack under-rates it badly (its real threat is a
    contact explosion), so the generic power-ratio would happily close and get
    the body blown up. Instead: keep distance when it is near, otherwise leave
    it to the slow will beat. Purely structural (distance only).
    """
    dist = _mc_target_distance(entity)
    name = str(entity.get("name") or "creeper")
    return {
        "threat": "special_mob",
        "verb": "keep_distance",
        "payload": {"entity": entity},
        "reason": {
            "mob": name,
            "distance": dist,
            "strategy": "creeper_no_chase",
        },
    }


def _mc_strategy_enderman(
    entity: Dict[str, Any], extra: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Enderman: disengage / keep distance rather than trade blows.

    An enderman teleports and hits very hard once provoked; a cornered fight is
    rarely winnable for a lightly-equipped body. Conservative structural
    tactic: back off and keep distance. (Gaze/eye-contact telemetry is not yet
    exposed by the bridge, so we do not attempt the "don't look at it" nuance —
    a structural disengage is the safe default.)
    """
    dist = _mc_target_distance(entity)
    name = str(entity.get("name") or "enderman")
    return {
        "threat": "special_mob",
        "verb": "keep_distance",
        "payload": {"entity": entity},
        "reason": {
            "mob": name,
            "distance": dist,
            "strategy": "enderman_disengage",
        },
    }


# Register the Minecraft-specific strategies against the generic core registry.
register_combat_strategy("minecraft", "creeper", _mc_strategy_creeper)
register_combat_strategy("minecraft", "enderman", _mc_strategy_enderman)


# Module-level connector class + self-registration (registry contract).
CONNECTOR_CLASS = MinecraftConnector

register_vessel_connector(
    "minecraft",
    __name__,
    capabilities={
        "movement": True,
        "chat": True,
        "perception": True,
        "interaction": True,
        "local": True,
    },
    label="Minecraft (Mineflayer)",
)


class MinecraftVesselPlugin(PluginBase):
    """Attachable Minecraft Vessel sub-plugin (Grillo-style).

    The Rift Vessel *core* (``vessel_plugin``) owns the global embodiment
    actions and generic settings; each world connector ships as its own
    attachable plugin so it gets a dedicated WebUI banner, icon, guide, and its
    own connector-specific configuration namespace (component
    ``minecraft_vessel``). This class registers **no** actions — the
    ``vessel_*`` actions live in the core plugin — it exists to surface the
    Minecraft connector as a first-class, separately toggleable entity and to
    own the Minecraft-specific config keys.
    """

    display_name = "Minecraft Vessel"

    def __init__(self) -> None:
        super().__init__()
        self._register_config()
        register_plugin("minecraft_vessel", self)
        self._register_skin_listeners()
        log_info("[minecraft_vessel] Registered MinecraftVesselPlugin")

    @staticmethod
    def _register_skin_listeners() -> None:
        """Re-apply the skin live when its config changes during an active session.

        When the operator edits ``MINECRAFT_SKIN_URL`` (or the model) in the
        WebUI while Synth is already in the world, we push the skin command to
        the server immediately so the change is visible in-game without
        reconnecting. Fully fail-safe: registration or dispatch failures never
        break the save.
        """
        for key in ("MINECRAFT_SKIN_URL", "MINECRAFT_SKIN_MODEL"):
            try:
                config_registry.add_listener(
                    key, MinecraftVesselPlugin._on_skin_config_changed
                )
            except Exception as exc:  # pragma: no cover - defensive
                log_warning(
                    f"[minecraft_vessel] could not register skin listener for "
                    f"{key}: {exc}"
                )

    @staticmethod
    def _on_skin_config_changed(_value: Any) -> None:
        """Config-change callback: re-apply the skin if a session is connected.

        The config listener is synchronous, so we schedule the async re-apply
        on the running event loop. If there is no active/connected Minecraft
        connector, this is a no-op.
        """
        try:
            from core.vessel_registry import VESSEL_REGISTRY

            connector = (getattr(VESSEL_REGISTRY, "_instances", {}) or {}).get(
                "minecraft"
            )
            if connector is None or not getattr(connector, "is_connected", False):
                return

            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None

            if loop is not None:
                loop.create_task(connector._apply_skin())
            else:  # pragma: no cover - no running loop (unlikely at save time)
                asyncio.run(connector._apply_skin())

            log_info(
                "[minecraft_vessel] skin config changed — re-applying skin live "
                "(active session)"
            )
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"[minecraft_vessel] live skin re-apply failed: {exc}")

    @staticmethod
    def _register_config() -> None:
        """Register Minecraft-specific config keys under this plugin's namespace."""
        config_registry.get_value(
            "MINECRAFT_BRIDGE_RUN_AT_START",
            False,
            value_type=bool,
            label="Minecraft Bridge Autostart",
            description=(
                "Optional: start the Minecraft bridge at boot. By default the "
                "bridge is started on demand, only when Synth actually enters "
                "the world."
            ),
            group="plugins",
            component="minecraft_vessel",
            advanced=True,
        )
        config_registry.get_value(
            "MINECRAFT_BRIDGE_HOST",
            "127.0.0.1",
            value_type=str,
            label="Minecraft Bridge Host",
            description="Host the local Mineflayer bridge listens on for HTTP commands.",
            group="plugins",
            component="minecraft_vessel",
            advanced=True,
        )
        config_registry.get_value(
            "MINECRAFT_BRIDGE_PORT",
            8137,
            value_type=int,
            label="Minecraft Bridge Port",
            description="TCP port the local Mineflayer bridge listens on for HTTP commands.",
            group="plugins",
            component="minecraft_vessel",
            advanced=True,
        )
        config_registry.get_value(
            "MINECRAFT_SERVER_HOST",
            "127.0.0.1",
            value_type=str,
            label="Minecraft Server Host",
            description="Hostname or IP of the Minecraft server the bot connects to.",
            group="plugins",
            component="minecraft_vessel",
        )
        config_registry.get_value(
            "MINECRAFT_SERVER_PORT",
            44383,
            value_type=int,
            label="Minecraft Server Port",
            description="TCP port of the Minecraft server the bot connects to.",
            group="plugins",
            component="minecraft_vessel",
        )
        config_registry.get_value(
            "MINECRAFT_BOT_USERNAME_OVERRIDE",
            "",
            value_type=str,
            label="Minecraft Bot Username Override",
            description=(
                "Optional in-world username for the Minecraft bot. Leave empty "
                "to use Synth's configured name (SYNTH_NAME)."
            ),
            group="plugins",
            component="minecraft_vessel",
            advanced=True,
        )
        config_registry.get_value(
            "MINECRAFT_SERVER_VERSION",
            "",
            value_type=str,
            label="Minecraft Server Version",
            description=(
                "Optional Minecraft protocol/version to pin (e.g. '1.21.4'). "
                "Leave empty to auto-detect. Set this if the bridge log shows "
                "'No data available for version X' — the server announces a "
                "version the bundled Mineflayer data doesn't know, so pin the "
                "closest supported version instead."
            ),
            group="plugins",
            component="minecraft_vessel",
            advanced=True,
        )
        # --- Skin ------------------------------------------------------
        # In offline-mode a Mineflayer bot cannot set its own texture from the
        # client; the skin is applied server-side. The bot therefore requests
        # its skin by running a chat command against a server-side skin plugin
        # (e.g. SkinsRestorer: ``/skin url <url>``). The operator provides a
        # direct web URL to the skin PNG in the WebUI: the URL is fed to the
        # skin command template at spawn. Registered as a plain text variable so
        # the WebUI plugin card renders a native text-input control.
        from core.variables_engine import register_exposed_var

        # NOTE: Skin file upload disabled in favour of a direct web URL. The
        # original ``file`` upload control is commented out below; it served the
        # uploaded PNG over HTTP, which required the MC server to be able to
        # reach the SyntH host. Providing a public skin URL directly is simpler
        # and works with any reachable host.
        # register_exposed_var(
        #     "MINECRAFT_SKIN_FILE",
        #     label="Minecraft Skin File",
        #     default="",
        #     value_type=str,
        #     ui_type="file",
        #     description=(
        #         "Upload a Minecraft skin texture PNG. It is served over HTTP "
        #         "and applied at spawn via the server-side skin command. "
        #         "Requires a server skin plugin such as SkinsRestorer."
        #     ),
        #     scope="plugins",
        #     component="minecraft_vessel",
        # )
        register_exposed_var(
            "MINECRAFT_SKIN_URL",
            label="Minecraft Skin URL",
            default="https://www.minecraftskins.com/uploads/skins/2026/07/28/rei-24229347.png",
            value_type=str,
            description=(
                "Direct web URL to a Minecraft skin texture PNG (e.g. "
                "'https://example.com/skin.png'). It MUST be a direct link to "
                "the .png image, NOT a skin-site page (e.g. a "
                "minecraftskins.com skin page returns HTML and is rejected). "
                "Applied at spawn via the server-side skin command; the URL "
                "must be reachable from the Minecraft server. Requires a server "
                "skin plugin such as SkinsRestorer."
            ),
            scope="plugins",
            component="minecraft_vessel",
        )
        register_exposed_var(
            "MINECRAFT_SKIN_MODEL",
            label="Minecraft Skin Model",
            default="classic",
            value_type=str,
            ui_type="select",
            options=["classic", "slim"],
            description=(
                "Skin model variant for URL-based skins: 'classic' (Steve, "
                "4px arms) or 'slim' (Alex, 3px arms)."
            ),
            scope="plugins",
            component="minecraft_vessel",
        )
        # TODO: remove once the skin-file upload path is confirmed obsolete.
        # Only relevant to the (now disabled) file-upload flow that served the
        # skin over HTTP; the direct ``MINECRAFT_SKIN_URL`` needs no base URL.
        # config_registry.get_value(
        #     "MINECRAFT_SKIN_PUBLIC_BASE_URL",
        #     "",
        #     value_type=str,
        #     label="Minecraft Skin Public Base URL",
        #     description=(
        #         "Base URL the Minecraft server can reach to fetch the uploaded "
        #         "skin file (e.g. 'http://192.168.1.42:9009'). Leave empty to "
        #         "auto-derive from the WebUI host/port. The final texture URL is "
        #         "'<base>/api/plugins/minecraft_vessel/skin.png'."
        #     ),
        #     group="plugins",
        #     component="minecraft_vessel",
        #     advanced=True,
        # )
        config_registry.get_value(
            "MINECRAFT_SKIN_COMMAND_TEMPLATES",
            "",
            value_type=str,
            label="Minecraft Skin Command Templates",
            description=(
                "Newline-separated list of chat-command templates the bot runs "
                "at spawn to apply a URL-based skin. Every template is tried in "
                "order, so it works across skin providers out of the box. "
                "'{url}' is substituted with the skin URL and '{model}' with "
                "the model variant. Leave empty to try both built-in defaults: "
                "the SkinRestorer mod ('/skin set web {model} \"{url}\"') and "
                "the SkinsRestorer plugin ('/skin url {url}')."
            ),
            group="plugins",
            component="minecraft_vessel",
            advanced=True,
        )
        config_registry.get_value(
            "MINECRAFT_SKIN_COMMAND_TEMPLATE",
            "",
            value_type=str,
            label="Minecraft Skin Command Template (legacy)",
            description=(
                "Legacy single-template override, kept for backward "
                "compatibility. When set it takes precedence over the built-in "
                "defaults but is itself overridden by "
                "MINECRAFT_SKIN_COMMAND_TEMPLATES. '{url}' and '{model}' are "
                "substituted. Prefer MINECRAFT_SKIN_COMMAND_TEMPLATES."
            ),
            group="plugins",
            component="minecraft_vessel",
            advanced=True,
        )

    def get_metadata(self) -> dict:
        """Declarative metadata for the WebUI plugin banner and docs."""
        return {
            "name": "minecraft_vessel",
            "display_name": "Minecraft Vessel",
            "description": (
                "Attachable Minecraft world connector for the Rift Vessel. "
                "Bridges Synth to a Minecraft world via the local Mineflayer "
                "bridge. Requires the Rift Vessel core plugin."
            ),
            "category": "Vessels",
            "icon": "icon.svg",
            "guide": "guide.md",
            # Advisory dependency: the Minecraft world attaches to the Rift
            # Vessel core plugin, which in turn depends on the Goals plugin.
            "depends_on": ["vessel_plugin"],
        }

    def get_supported_action_types(self) -> list[str]:
        return []

    def get_supported_actions(self) -> dict:
        # Minecraft exposes no actions of its own; the generic ``vessel_*``
        # actions are owned by the Rift Vessel core plugin.
        return {}

    async def teardown(self) -> None:
        """End the Minecraft embodiment when this sub-plugin is disabled.

        Called by the runtime plugin toggle (``POST /api/components/toggle`` →
        :meth:`core_initializer.disable_plugin`). Disabling the Minecraft world
        while connected must close its session so the lived experience is
        flushed and the connection is dropped — otherwise a phantom session
        would keep deprioritising chat and running beats until the cooldown.
        Fully fail-safe: teardown must never raise.
        """
        try:
            from core.core_initializer import INTERFACE_REGISTRY

            iface = INTERFACE_REGISTRY.get("vessel")
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"[minecraft_vessel] teardown: interface unavailable: {exc}")
            iface = None

        try:
            from core.vessel_registry import VESSEL_REGISTRY

            connector = (getattr(VESSEL_REGISTRY, "_instances", {}) or {}).get(
                "minecraft"
            )
            if connector is not None:
                await connector.disconnect()
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"[minecraft_vessel] teardown: disconnect failed: {exc}")

        if iface is not None and hasattr(iface, "end_sessions_for_environment"):
            try:
                await iface.end_sessions_for_environment("minecraft", reason="logout")
            except Exception as exc:  # pragma: no cover - defensive
                log_warning(
                    f"[minecraft_vessel] teardown: end_sessions_for_environment failed: {exc}"
                )

        log_info("[minecraft_vessel] teardown complete (session closed on disable)")


PLUGIN_CLASS = MinecraftVesselPlugin
