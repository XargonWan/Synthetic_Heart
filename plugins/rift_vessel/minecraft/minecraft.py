# plugins/rift_vessel/minecraft/minecraft.py
"""Minecraft Vessel connector.

Bridges SyntH's Rift Vessel layer to a Minecraft world via the Node.js
Mineflayer bridge (``minecraft_bridge_minimal.js``, in this same folder,
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
import os
import socket
from typing import Any, Dict

import aiohttp

from core.config_manager import config_registry
from core.core_initializer import register_plugin
from core.logging_utils import log_debug, log_error, log_info, log_warning
from core.plugin_base import PluginBase
from core.vessel_registry import register_vessel_connector
from plugins.rift_vessel.minecraft import goals as mc_goals
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

# Loopback host names that mean "this same machine". When Synth runs inside a
# container these do NOT point at the Docker host (where a "Open to LAN" world
# actually listens), so they are auto-remapped to the host gateway below.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0.0.0.0"})
# Stable name for the Docker host, provided by ``extra_hosts:
# host.docker.internal:host-gateway`` in docker-compose.yml.
_HOST_GATEWAY_NAME = "host.docker.internal"


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

    async def _post(self, path: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        if self._session is None:
            return {"ok": False, "detail": "no http session"}
        try:
            async with self._session.post(
                f"{self._base_url}{path}", json=payload
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

        # Tell the bridge to (re)connect to the Minecraft server. Pass the
        # resolved target (per-connect override or configured default) so Synth
        # can enter a different server on demand.
        target = self._resolve_server_target(settings or {})
        conn = await self._post("/connect", target)
        if not conn.get("ok"):
            detail = conn.get("detail") or "unknown error"
            server = f"{target.get('host')}:{target.get('port')}"
            self.last_error = f"could not enter the Minecraft server {server}: {detail}"
            log_error(f"{LOG_PREFIX} bridge failed to connect: {detail}")
            await self._close_session()
            return False

        self._connected = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        log_info(f"{LOG_PREFIX} connected via {self._base_url}")

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

    @staticmethod
    def _skin_public_base_url() -> str:
        """Return the base URL the Minecraft server uses to fetch the skin file.

        Prefers an explicit ``MINECRAFT_SKIN_PUBLIC_BASE_URL``; otherwise
        auto-derives ``http://<host>:<port>`` from the WebUI env config.

        The MC server (or its skin plugin) fetches the texture over HTTP, so the
        host must be reachable *from the server's* point of view. A loopback
        host (``127.0.0.1``/``localhost``/``0.0.0.0``) only works when the
        server runs on the very same machine — a remote or containerised server
        cannot open it. When the derived host is a loopback we therefore try to
        substitute the machine's primary LAN IP (see :func:`_detect_lan_ip`) so
        the skin works out of the box on the common "SyntH host + server on the
        LAN" setup. Set ``MINECRAFT_SKIN_PUBLIC_BASE_URL`` explicitly to override
        (e.g. a VPN/public address or a reverse-proxy URL).
        """
        explicit = str(
            config_registry.get_value("MINECRAFT_SKIN_PUBLIC_BASE_URL", "") or ""
        ).strip()
        if explicit:
            return explicit.rstrip("/")

        host = (os.environ.get("SYNTH_WEBUI_HOST") or "").strip()
        if not host or host.lower() in _LOOPBACK_HOSTS:
            lan_ip = _detect_lan_ip()
            host = lan_ip or "127.0.0.1"
        port = (
            os.environ.get("SYNTH_WEBUI_HTTP_PORT")
            or os.environ.get("SYNTH_WEBUI_PORT")
            or os.environ.get("PORT")
            or "8080"
        ).strip()
        return f"http://{host}:{port}"

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

    async def _apply_skin(self) -> None:
        """Request the uploaded skin from a server-side skin plugin.

        Offline-mode Mineflayer bots cannot set their own texture client-side;
        the skin is applied by the server. The user uploads a skin PNG in the
        WebUI (``MINECRAFT_SKIN_FILE``); it is served over HTTP and its public
        URL is fed to one or more configurable chat commands (see
        :meth:`_skin_command_templates`) so it works across skin plugins/mods
        and locales without any keyword logic. Every configured template is run
        at spawn — the server accepts the one it understands and ignores the
        rest. If no skin file is uploaded, nothing happens.
        """
        skin_file = str(
            config_registry.get_value("MINECRAFT_SKIN_FILE", "") or ""
        ).strip()
        if not skin_file:
            return

        skin_url = f"{self._skin_public_base_url()}/api/config/MINECRAFT_SKIN_FILE/file"
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
        while self._connected:
            try:
                res = await self._get("/events")
                events = res.get("events") if isinstance(res, dict) else None
                if events:
                    for raw in events:
                        await self._dispatch_event(raw)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                log_debug(f"{LOG_PREFIX} poll error: {exc}")
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
        if action in self._GOAL_VERBS:
            return await self._act_goal(action, payload or {})
        res = await self._post("/cmd", {"action": action, "payload": payload or {}})
        return VesselActionResult(
            ok=bool(res.get("ok")),
            detail=res.get("detail"),
            data=res.get("data") or {},
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
                    destination=self._extract_destination(payload),
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
            result = await mc_goals.set_goal(
                description,
                self._session_id,
                note=payload.get("note"),
                destination=self._extract_destination(payload),
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
        affordances = self._build_affordances(entities, blocks)
        current_goal, recent_goals = await self._resolve_goals()
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
                "affordances": affordances,
                # Self-directed play: the free-text objective Synth set for
                # itself and its own recent goal history. Populated from the
                # minecraft_goals table (see goals.py) — no catalogue, no
                # auto-computed progress; Synth judges its own progress.
                "current_goal": current_goal,
                "recent_goals": recent_goals,
            },
        )

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
                }
            )
        out.sort(key=lambda a: (a.get("distance") is None, a.get("distance") or 0))
        return out

    # Motorics: how close (blocks) an affordance must be for the body to act on
    # it directly (mine/use) rather than first walking toward it.
    _MOTOR_REACH = 3.0

    # How close (blocks, horizontal) the body must get to a self-chosen travel
    # destination before it counts as "arrived" and stops steering toward it.
    _ARRIVAL_RADIUS = 4.0

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
          * Nothing around and no destination → ``wander`` as a last resort so
            the body still explores instead of freezing.

        Fail-safe: any error degrades to ``{"acted": False, ...}`` and never
        raises into the scheduler.
        """
        try:
            if not self._connected:
                return {"acted": False, "reason": "not_connected"}
            if not goal:
                return {"acted": False, "reason": "no_goal"}

            state = await self.get_world_state()
            if state is None:
                return {"acted": False, "reason": "no_world_state"}

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
            travel_pending = False
            travel_remaining: float | None = None
            if dest is not None:
                travel_remaining = self._horizontal_distance(state.position, dest)
                travel_pending = (
                    travel_remaining is None or travel_remaining > self._ARRIVAL_RADIUS
                )

            if benign:
                # Affordances arrive distance-sorted (nearest first); take head.
                target = benign[0]
                distance = target.get("distance")
                name = target.get("target")

                within_reach = isinstance(distance, (int, float)) and (
                    distance <= self._MOTOR_REACH
                )
                if within_reach and name:
                    # Something is literally in front of us — grab/use it first,
                    # regardless of any distant destination.
                    if target.get("kind") == "block":
                        await self.act("mine", {"target": name})
                        return {"acted": True, "action": "mine", "target": name}
                    await self.act("use", {"target": name})
                    return {"acted": True, "action": "use", "target": name}

                # Out of reach. Only chase this affordance if we have *nowhere
                # chosen to go*; otherwise heading toward random far scenery
                # (e.g. ubiquitous sand in a desert) would trap the body in
                # place and never reach the goal. When a travel destination is
                # still pending, fall through to it below.
                if name and not travel_pending:
                    await self.act("goto", {"target": name})
                    return {"acted": True, "action": "goto", "target": name}

            # Honour a self-chosen travel destination so movement stays attuned
            # to the goal even when incidental affordances litter the path.
            if dest is not None:
                if travel_pending:
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
                # Arrived at the destination but nothing useful is here yet;
                # roam locally so the body keeps searching around the target.
                await self.act("wander", {})
                return {"acted": True, "action": "wander", "reason": "arrived"}

            # Nothing to work with and nowhere chosen to go — roam to explore.
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
                    "your own game, not a script. If what you want is NOT in "
                    "this area (for example there are no trees here and you "
                    "want wood, or you want to reach a different biome), pick a "
                    "place to head toward and give its coordinates in "
                    "'destination_x' and 'destination_z' (from your position "
                    "and what you can see): your body will then walk that way on "
                    "its own while you play. Leave them out if you are happy "
                    "where you are."
                ),
                "required_fields": ["description"],
                "optional_fields": ["note", "destination_x", "destination_z"],
                "security_level": "low",
            },
            "update_goal": {
                "description": (
                    "Reflect on the goal you set for yourself: jot a 'note' on "
                    "how it is going in your own words, or set 'status' to "
                    "'done' when you feel you have achieved it or 'abandoned' "
                    "if you have changed your mind. You are the judge of your "
                    "own progress — nothing counts it for you. If you realise "
                    "you need to travel somewhere else to make progress, set a "
                    "new 'destination_x'/'destination_z' and your body will head "
                    "there; you do not have to touch it if the direction still "
                    "feels right."
                ),
                "required_fields": [],
                "optional_fields": [
                    "note",
                    "status",
                    "destination_x",
                    "destination_z",
                ],
                "security_level": "low",
            },
        }

    def describe_capabilities(self) -> Dict[str, Any]:
        return {
            "movement": True,
            "chat": True,
            "perception": True,
            "interaction": True,
            "local": True,
        }

    @property
    def is_connected(self) -> bool:
        return self._connected


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
        log_info("[minecraft_vessel] Registered MinecraftVesselPlugin")

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
        # (e.g. SkinsRestorer: ``/skin url <url>``). Upload a PNG skin file in
        # the WebUI: it is served back over HTTP and its URL is fed to the skin
        # command template at spawn. Registered as an exposed ``file`` variable
        # so the WebUI plugin card renders a native file-upload control.
        from core.variables_engine import register_exposed_var

        register_exposed_var(
            "MINECRAFT_SKIN_FILE",
            label="Minecraft Skin File",
            default="",
            value_type=str,
            ui_type="file",
            description=(
                "Upload a Minecraft skin texture PNG. It is served over HTTP "
                "and applied at spawn via the server-side skin command. "
                "Requires a server skin plugin such as SkinsRestorer."
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
        config_registry.get_value(
            "MINECRAFT_SKIN_PUBLIC_BASE_URL",
            "",
            value_type=str,
            label="Minecraft Skin Public Base URL",
            description=(
                "Base URL the Minecraft server can reach to fetch the uploaded "
                "skin file (e.g. 'http://192.168.1.42:9009'). Leave empty to "
                "auto-derive from the WebUI host/port. The final texture URL is "
                "'<base>/api/config/MINECRAFT_SKIN_FILE/file'."
            ),
            group="plugins",
            component="minecraft_vessel",
            advanced=True,
        )
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


PLUGIN_CLASS = MinecraftVesselPlugin
