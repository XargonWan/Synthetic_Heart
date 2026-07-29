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
from plugins.rift_vessel.minecraft import goals as mc_goals
from plugins.rift_vessel.minecraft import wiki_client
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
        self._connected = False
        self._base_url = ""
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
    # How far (blocks) to run when fleeing a threat.
    _FLEE_DISTANCE = 16.0
    # Consecutive failed defend ticks before escalating DEFEND → FLEE.
    _FIGHT_MAX_FAILS = 3
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
        self._on_event = on_event
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

        # Resolve the self-preservation thresholds for this session (fail-safe;
        # falls back to the class defaults on any read error).
        self._load_self_preservation_config()

        # Ensure the goal/progression table exists (idempotent, fail-safe). This
        # covers non-fresh installs where init-db.sql was not re-run.
        try:
            await mc_goals.init_goal_table()
        except Exception as exc:
            log_debug(f"{LOG_PREFIX} goal table init skipped: {exc}")

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
        if not self._on_event or not isinstance(raw, dict):
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
        if action == "say":
            return await self._act_say(payload or {})
        res = await self._post("/cmd", {"action": action, "payload": payload or {}})
        return VesselActionResult(
            ok=bool(res.get("ok")),
            detail=res.get("detail"),
            data=res.get("data") or {},
        )

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
            res = await self._post("/cmd", {"action": "say", "payload": line_payload})
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
                result = await mc_goals.update_active_goal(
                    note=payload.get("note"),
                    status=payload.get("status"),
                    destination=await self._resolve_travel_destination(payload),
                    steps=payload.get("steps"),
                    current_step=payload.get("current_step"),
                    advance=bool(payload.get("advance")),
                    target_kind=payload.get("target_kind"),
                    target_name=payload.get("target_name"),
                )
                ok = result.get("status") == "ok"
                return VesselActionResult(
                    ok=ok,
                    detail=result.get("message") or "goal updated",
                    data=result,
                )
            # set_goal — free-text objective authored by Synth.
            description = str(payload.get("description") or "").strip()
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
            result = await mc_goals.set_goal(
                description,
                self._session_id,
                note=payload.get("note"),
                destination=await self._resolve_travel_destination(payload),
                target_kind=payload.get("target_kind"),
                target_name=payload.get("target_name"),
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
        affordances = self._build_affordances(entities, blocks)
        current_goal, recent_goals = await self._resolve_goals()
        knowledge = await self._resolve_knowledge(current_goal, affordances)
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
                "oxygen": data.get("oxygen"),
                "is_in_water": data.get("is_in_water"),
                "is_alive": data.get("is_alive"),
                "block_feet": data.get("block_feet"),
                "block_head": data.get("block_head"),
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
            },
        )

    async def _resolve_knowledge(
        self,
        current_goal: Dict[str, Any] | None,
        affordances: list[dict[str, Any]],
    ) -> list[Dict[str, Any]]:
        """Pick knowledge-base facts relevant to the goal and surroundings.

        Builds a **structural** query — the goal's ``target_name`` plus any
        block/entity ids Synth is standing among (from the affordance contract)
        — and looks them up in the connector's knowledge base. Never inspects
        free-text goal descriptions for keywords, so it stays language-agnostic.
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
            if not tokens:
                return []
            query = " ".join(dict.fromkeys(tokens))  # de-dup, preserve order
            # Automatic beat path: cache-only so a WorldState build never blocks
            # on the network or the LLM (AGENTS.md §5c).
            return await self.lookup_knowledge(query, limit=cap, cache_only=True)
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} knowledge resolution failed: {exc}")
            return []

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

        # 4/5. Hostile mob nearby → defend or flee.
        hostile = self._nearest_hostile(state)
        if hostile is not None:
            try:
                raw_dist = hostile.get("distance")
                dist = float(raw_dist) if raw_dist is not None else None
            except (TypeError, ValueError):
                dist = None
            if dist is not None and dist <= self._sp_hostile_dist:
                health = extra.get("health")
                low_health = (
                    isinstance(health, (int, float)) and health <= self._sp_low_health
                )
                target_id = str(hostile.get("name") or "")
                # Track the fail counter against the specific mob being fought;
                # a new/changed threat resets it.
                if self._fight_target != target_id:
                    self._fight_target = target_id
                    self._fight_fail_count = 0
                escalated = self._fight_fail_count >= self._sp_fight_max_fails
                if self._sp_fight_back and not low_health and not escalated:
                    return {
                        "threat": "defend",
                        "verb": "attack",
                        "payload": {"target": target_id} if target_id else {},
                        "reason": {
                            "distance": dist,
                            "health": health,
                            "fails": self._fight_fail_count,
                        },
                    }
                # Escalate to flight.
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
                    },
                }

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
                result = await self._act_flee(state)
            elif verb == "attack":
                result = await self.act("attack", payload)
                # Count this defend tick; escalate on repeated engagement.
                self._fight_fail_count += 1
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
        acted = _result_acted(result)
        log_info(
            f"{LOG_PREFIX} survival reflex: {threat} -> {verb} "
            f"(reason={plan.get('reason')})"
        )
        return {"acted": acted, "reason": f"survival:{threat}"}

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

    async def _act_flee(self, state: "WorldState") -> Any:
        """Run away from the nearest threat (mindcraft moveAway style).

        Picks a destination ``_FLEE_DISTANCE`` blocks in the direction opposite
        the nearest hostile (or, when fleeing fire, simply forward) and gotos
        it. Purely numeric vector math — no keyword logic. Fail-safe.
        """
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
        tx = int(px + (dx / norm) * self._FLEE_DISTANCE)
        tz = int(pz + (dz / norm) * self._FLEE_DISTANCE)
        return await self.act("goto", {"x": tx, "y": int(py), "z": tz})

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
                        await self.act("mine", {"target": name})
                        return {"acted": True, "action": "mine", "target": name}
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
                if name and dest is None:
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
                    "Break and collect a nearby thing by name (target), e.g. a "
                    "block of wood, stone or ore you spotted around you. You "
                    "walk to it first if it is out of reach and use the best "
                    "tool you are carrying. This is how you gather materials."
                ),
                "required_fields": ["target"],
                "optional_fields": ["search_radius", "timeout_ms"],
                "security_level": "low",
            },
            "collect_block": {
                "description": (
                    "Gather several of the same thing by name (name), e.g. "
                    "collect 5 blocks of wood you spotted around you. Give how "
                    "many you want (count). You walk to each one, break it and "
                    "pick up the drop, repeating until you have that many or "
                    "there are none left nearby. This is the reliable way to "
                    "stock up on a material for a goal."
                ),
                "required_fields": ["name"],
                "optional_fields": ["count", "search_radius", "timeout_ms"],
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
            "craft": {
                "description": (
                    "Make a new item out of the materials you are carrying, "
                    "given by its exact name (item), e.g. planks, sticks or a "
                    "tool. Optionally craft several at once (count). If the "
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
                    "target_name='oak_log'; or target_kind='entity', "
                    "target_name='cow'). Pick the name verbatim from your scan — "
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
