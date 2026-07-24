# plugins/rift_vessel/minecraft/minecraft.py
"""Minecraft Vessel connector.

Bridges SyntH's Rift Vessel layer to a Minecraft world via the Node.js
Mineflayer bridge (:mod:`interface_dev/minecraft_bridge_minimal.js`, managed by
:mod:`interface.minecraft_provisioner`). This connector speaks plain HTTP to the
local bridge:

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

    async def _apply_skin(self) -> None:
        """Request the uploaded skin from a server-side skin plugin.

        Offline-mode Mineflayer bots cannot set their own texture client-side;
        the skin is applied by the server. The user uploads a skin PNG in the
        WebUI (``MINECRAFT_SKIN_FILE``); it is served over HTTP and its public
        URL is fed to a configurable chat command (default SkinsRestorer syntax)
        so it works across skin plugins and locales without any keyword logic.
        If no skin file is uploaded, nothing happens.
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
        template = str(
            config_registry.get_value(
                "MINECRAFT_SKIN_COMMAND_TEMPLATE", "/skin url {url}"
            )
            or "/skin url {url}"
        )
        command = template.replace("{url}", skin_url).replace("{model}", model)

        command = command.strip()
        if not command:
            return

        res = await self._post(
            "/cmd", {"action": "skin", "payload": {"command": command}}
        )
        if res.get("ok"):
            log_info(f"{LOG_PREFIX} skin command sent: {command}")
        else:
            log_warning(
                f"{LOG_PREFIX} skin command failed (server skin plugin required?): "
                f"{res.get('detail')}"
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

    async def act(
        self,
        action: str,
        payload: Dict[str, Any],
    ) -> VesselActionResult:
        if not self._connected:
            return VesselActionResult(ok=False, detail="not connected to a world")
        res = await self._post("/cmd", {"action": action, "payload": payload or {}})
        return VesselActionResult(
            ok=bool(res.get("ok")),
            detail=res.get("detail"),
            data=res.get("data") or {},
        )

    async def get_world_state(self) -> WorldState | None:
        if not self._connected:
            return None
        res = await self._post("/cmd", {"action": "status", "payload": {}})
        if not res.get("ok"):
            return None
        data = res.get("data") or {}
        return WorldState(
            environment=ENVIRONMENT,
            health=data.get("health"),
            position=data.get("position"),
            possible_actions=[
                "say",
                "move",
                "look",
                "use",
                "attack",
                "follow",
                "unfollow",
                "respawn",
            ],
            flags={"connected": bool(data.get("connected"))},
            extra={"username": data.get("username")},
        )

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
            "MINECRAFT_SKIN_COMMAND_TEMPLATE",
            "/skin url {url}",
            value_type=str,
            label="Minecraft Skin Command Template",
            description=(
                "Chat-command template the bot runs at spawn to apply a "
                "URL-based skin. '{url}' is substituted with the skin URL and "
                "'{model}' with the model variant. Defaults to the SkinsRestorer "
                "syntax; change it if your server uses a different skin plugin."
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
