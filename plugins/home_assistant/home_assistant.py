# plugins/home_assistant/home_assistant.py
"""Home Assistant plugin - the house as a capability bus for Synth.

Home Assistant runs the smart home; this plugin lets Synth see it and act on
it, and it delegates whole features to HA instead of growing engines inside
SyntH. The first delegated feature is image generation: Synth asks for a
picture, one HA script runs ``ai_task.generate_image`` on whichever engine is
configured there (ComfyUI on a GPU host), and the finished file is pulled back
into SyntH's own media tree and sent through the ordinary ``send_message``
path.

Design notes
------------
* **WebSocket first.** The WS API is the transport here: ``call_service`` with
  ``return_response`` brings a script's result back in one round trip, and
  ``subscribe_events`` keeps the house-state snapshot fresh without polling.
  The REST API is used only for camera proxies and media upload.
* **Agent Lane only, structurally.** Every action declares
  ``external_effects``, so any reply carrying a ``hass_*`` call is classified
  to the Agent Lane by the router, and the Fast-Lane catalogue stays free of
  house verbs. No keyword or regex reasoning anywhere.
* **Deny beats allow.** Locks, alarm panels, shell commands and the HA service
  domains are denied by default and the allow list bounds the rest. A HA
  long-lived token is admin-scoped, so these lists are the real boundary.
* **Media stays inside the sandbox.** Downloads land under
  ``HASS_MEDIA_CACHE_DIR`` (default ``res/hass_cache``) after being validated
  against ``core.outbound_file_utils.allowed_file_roots()``, so anything Synth
  pulls out of HA can also be sent out again.
* **Fail closed, never fatal.** HA down, a wrong token, a missing entity or an
  unknown style produce a clean per-action error; importing or removing this
  plugin never breaks the rest of Synth.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import time
from datetime import datetime, timedelta, timezone
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from core.config_manager import config_registry
from core.logging_utils import log_debug, log_info, log_warning
from core.plugin_base import PluginBase

LOG_PREFIX = "[home_assistant]"

_DEFAULT_BASE_URL = "http://192.168.1.25:8123"
_DEFAULT_MEDIA_CACHE_DIR = "res/hass_cache"
_DEFAULT_ALLOWED_DOMAINS = (
    "light,switch,climate,media_player,cover,scene,script,automation,"
    "input_boolean,input_number,input_select,button,fan,vacuum,notify,"
    "counter,timer,weather"
)
_DEFAULT_DENIED_DOMAINS = (
    "lock,alarm_control_panel,camera,shell_command,homeassistant,hassio,"
    "tts,recorder,frontend,persistent_notification"
)
_DEFAULT_AWARENESS_ENTITIES = "sensor.*_temperature,sensor.*_humidity,light.*,switch.*,person.*,cover.*,climate.*,binary_sensor.*"
_DEFAULT_IMAGE_SCRIPT = "script.synth_image"

_RECONNECT_MIN_INTERVAL_SEC = 5.0
_STATE_PIECE_LIMIT = 40
_APP_ROOT = Path(__file__).resolve().parents[2]

try:  # pragma: no cover - optional UI plumbing
    from core.variables_engine import register_exposed_var
except Exception:  # pragma: no cover
    register_exposed_var = None  # type: ignore[assignment]


def _expose(
    key: str,
    label: str,
    default: Any,
    value_type: type,
    ui_type: str,
    description: str,
) -> None:
    """Register a WebUI-exposed config key (best effort, never fatal)."""
    if register_exposed_var is None:
        return
    try:
        register_exposed_var(
            key,
            label=label,
            default=default,
            value_type=value_type,
            ui_type=ui_type,
            description=description,
            scope="home_assistant",
            component="home_assistant",
        )
    except Exception as exc:  # pragma: no cover - defensive
        log_warning(f"{LOG_PREFIX} could not expose {key}: {exc}")


_expose(
    "HASS_BASE_URL",
    "Home Assistant URL",
    _DEFAULT_BASE_URL,
    str,
    "string",
    "Base URL of the Home Assistant instance (http://host:8123).",
)
_expose(
    "HASS_TOKEN",
    "Home Assistant Token",
    "",
    str,
    "password",
    (
        "Long-lived access token from the HA profile page. Stored masked and "
        "never logged. The plugin stays disabled while this is empty."
    ),
)
_expose(
    "HASS_VERIFY_TLS",
    "Verify TLS Certificate",
    False,
    bool,
    "bool",
    "Enable only for a https Home Assistant with a valid certificate.",
)
_expose(
    "HASS_AWARENESS_ENABLED",
    "Inject House State",
    True,
    bool,
    "bool",
    "Add a short house-state block (the configured entities) to every prompt.",
)
_expose(
    "HASS_AWARENESS_ENTITIES",
    "House State Entities",
    _DEFAULT_AWARENESS_ENTITIES,
    str,
    "string",
    (
        "Comma-separated entity ids or wildcard patterns included in the "
        "house-state block."
    ),
)
_expose(
    "HASS_AWARENESS_MAX_CHARS",
    "House State Max Characters",
    1200,
    int,
    "number",
    "Hard cap on the injected house-state block.",
)
_expose(
    "HASS_AWARENESS_MAX_AGE_SEC",
    "House State Max Age (s)",
    900,
    int,
    "number",
    "Omit the block entirely when the cached state is older than this.",
)
_expose(
    "HASS_ALLOWED_DOMAINS",
    "Allowed Service Domains",
    _DEFAULT_ALLOWED_DOMAINS,
    str,
    "string",
    ("Comma-separated domains hass_call_service and hass_run_script may address."),
)
_expose(
    "HASS_DENIED_DOMAINS",
    "Denied Service Domains",
    _DEFAULT_DENIED_DOMAINS,
    str,
    "string",
    "Comma-separated domains that are always refused, even if allowed above.",
)
_expose(
    "HASS_CALL_TIMEOUT_SEC",
    "Service Call Timeout (s)",
    30,
    int,
    "number",
    "Timeout for ordinary service calls.",
)
_expose(
    "HASS_JOB_TIMEOUT_SEC",
    "Job Timeout (s)",
    300,
    int,
    "number",
    "Timeout for long jobs: scripts with a response, image generation.",
)
_expose(
    "HASS_IMAGE_ENABLED",
    "Enable Image Generation",
    True,
    bool,
    "bool",
    "Allow Synth to ask Home Assistant to generate images.",
)
_expose(
    "HASS_IMAGE_SCRIPT",
    "Image Script",
    _DEFAULT_IMAGE_SCRIPT,
    str,
    "string",
    "The one HA script Synth calls for images (it picks the engine).",
)
_expose(
    "HASS_IMAGE_ENTITIES",
    "Image Styles",
    "",
    str,
    "string",
    (
        "Style name to ai_task entity map, e.g. "
        "'quick=ai_task.comfyui_quick,hires=ai_task.comfyui_hires'. Used by "
        "hass_image_styles and passed to the script as the style name."
    ),
)
_expose(
    "HASS_IMAGE_DEFAULT_STYLE",
    "Default Image Style",
    "quick",
    str,
    "string",
    "Style used when Synth does not ask for one.",
)
_expose(
    "HASS_IMAGE_EDIT_ENABLED",
    "Enable Image Editing",
    False,
    bool,
    "bool",
    (
        "Off until the Home Assistant image provider can take a reference "
        "image (the ComfyUI component cannot yet). While off, hass_image_edit "
        "refuses with an explanation."
    ),
)
_expose(
    "HASS_IMAGE_DAILY_CAP",
    "Image Generation Daily Cap",
    10,
    int,
    "number",
    "Maximum images per day (0 = unlimited). Counted across restarts.",
)
_expose(
    "HASS_MEDIA_CACHE_DIR",
    "Media Cache Directory",
    _DEFAULT_MEDIA_CACHE_DIR,
    str,
    "string",
    (
        "Where generated images and snapshots are stored. Must stay inside "
        "the outbound media roots or sends will be refused."
    ),
)
_expose(
    "HASS_MEDIA_KEEP",
    "Keep Newest Files",
    50,
    int,
    "number",
    "How many files to keep per cache subfolder.",
)
_expose(
    "HASS_MAX_MEDIA_MB",
    "Max Download Size (MB)",
    25,
    int,
    "number",
    "Refuse to download a file larger than this.",
)
_expose(
    "HASS_ENABLED",
    "Plugin Enabled",
    True,
    bool,
    "bool",
    "Master switch for the whole plugin.",
)


_expose(
    "HASS_BEAT_AWARENESS_ENABLED",
    "Inject House State On Beats",
    False,
    bool,
    "bool",
    (
        "Give Synth the house-state line while she is on one of her own "
        "autonomous beats (Grillo). Off by default: beats run without the "
        "house unless you switch this on."
    ),
)
_expose(
    "HASS_BEAT_AWARENESS_BEATS",
    "Beats That Get The House State",
    "",
    str,
    "string",
    (
        "Comma-separated beat types that receive the house-state line "
        "(observer, dream, weekly_review...). Empty means every beat family. "
        "Wildcards allowed."
    ),
)
_expose(
    "HASS_BEAT_AWARENESS_MAX_CHARS",
    "Beat House State Max Characters",
    600,
    int,
    "number",
    "Hard cap for the beat house-state line (0 = use the conversation cap).",
)
_expose(
    "HASS_BEAT_ACTIONS_ALLOWED",
    "Allow House Actions On Beats",
    False,
    bool,
    "bool",
    (
        "Let Synth change the house (services, scripts, automations, "
        "notifications, image generation) while she is on a beat. Off means "
        "beats can only read the house."
    ),
)


_expose(
    "HASS_WEATHER_ENABLED",
    "Inject Weather From HA",
    True,
    bool,
    "bool",
    (
        "Build the weather line from Home Assistant's own weather entity "
        "(plus a short hourly forecast) instead of the local wttr.in block."
    ),
)
_expose(
    "HASS_WEATHER_ENTITY",
    "Home Assistant Weather Entity",
    "",
    str,
    "string",
    "weather.* entity to read. Empty means the first weather entity HA reports.",
)
_expose(
    "HASS_WEATHER_FORECAST_HOURS",
    "Weather Forecast Hours",
    6,
    int,
    "number",
    "How many hours of hourly forecast to append (0 = none).",
)
_expose(
    "HASS_LOCATION_ENABLED",
    "Inject House Location From HA",
    True,
    bool,
    "bool",
    (
        "Inject the house's real coordinates, timezone and today's sun times "
        "from Home Assistant."
    ),
)
_expose(
    "HASS_LOCATION_LABEL",
    "House Location (rough)",
    "",
    str,
    "text",
    (
        "A rough place name for the house (a valley, a village, a district). "
        "While set, the [House] block names it instead of the exact "
        "coordinates, so the synth knows where the household is without "
        "carrying the pinpoint. Blank keeps the coordinates."
    ),
)
_expose(
    "HASS_TIMEZONE_ENABLED",
    "House Timezone Drives The Clock",
    True,
    bool,
    "bool",
    (
        "While HA is connected, read the clock in the timezone from Home "
        "Assistant's own configuration. It outranks the core TZ setting, which "
        "stays the fallback whenever HA has not been read yet or is down."
    ),
)
_expose(
    "HASS_CORE_CONFIG_TTL_SEC",
    "House Facts Refresh (seconds)",
    900,
    int,
    "number",
    (
        "How long HA's own configuration (house coordinates, elevation, "
        "timezone) may be reused before it is re-read in the background, so a "
        "change made in Home Assistant lands without a restart. 0 = only read "
        "on connect."
    ),
)


# ---------------------------------------------------------------------------
# Config readers (faithful to the agpeer plugin's helpers)
# ---------------------------------------------------------------------------


def _cfg_str(key: str, default: str) -> str:
    try:
        val = config_registry.get_value(key, default)
        return str(val).strip() if val is not None else default
    except Exception:
        return default


def _cfg_int(key: str, default: int) -> int:
    try:
        return int(config_registry.get_value(key, default))
    except Exception:
        return default


def _cfg_bool(key: str, default: bool) -> bool:
    try:
        val = config_registry.get_value(key, default)
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("1", "true", "yes", "on")
    except Exception:
        return default


def _cfg_list(key: str, default: str) -> List[str]:
    raw = _cfg_str(key, default)
    return [item.strip().lower() for item in raw.split(",") if item.strip()]


def _now() -> datetime:
    return datetime.now(timezone.utc)


# Actions that change the house (or spend a GPU rendering). They are refused
# while Synth is on one of her own beats unless the operator allows them: a beat
# is her own initiative, and the house is not hers to move unprompted.
_BEAT_BLOCKED_ACTIONS = frozenset(
    {
        "hass_call_service",
        "hass_run_script",
        "hass_automation",
        "hass_notify",
        "hass_image_generate",
        "hass_image_edit",
    }
)


def _beat_turn_info(message: Any, context_memory: Any) -> Tuple[bool, str]:
    """Return ``(is_beat_turn, beat_type)`` from the chain's own markers.

    Beats enqueue a synthetic message carrying ``grillo_beat``/``beat_type`` in
    its context (``plugins/grillo/grillo_impl.py::_enqueue_with_low_priority``),
    so this reads routing metadata only - never message text.
    """
    is_beat = False
    beat_type = ""
    if isinstance(context_memory, dict):
        is_beat = bool(context_memory.get("grillo_beat"))
        beat_type = str(context_memory.get("beat_type") or "")
    if not is_beat:
        is_beat = bool(getattr(message, "grillo_beat", False))
        beat_type = beat_type or str(getattr(message, "beat_type", "") or "")
    return is_beat, beat_type


_COMPASS_POINTS = (
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
)


def _compass(bearing: Any) -> str:
    """Convert a wind bearing in degrees to a compass point."""
    try:
        value = float(bearing) % 360.0
    except (TypeError, ValueError):
        return ""
    return _COMPASS_POINTS[int((value + 11.25) % 360.0 // 22.5)]


def _num(value: Any, digits: int = 1) -> Optional[str]:
    """Format a numeric attribute, dropping a trailing ``.0``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    text = f"{float(value):.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or None


def _local_hhmm(value: Any, tz_name: str) -> str:
    """Render an ISO timestamp as local HH:MM in HA's own timezone."""
    if not value:
        return ""
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return text[:16]
    if tz_name:
        try:
            from zoneinfo import ZoneInfo

            parsed = parsed.astimezone(ZoneInfo(tz_name))
        except Exception:
            pass
    return parsed.strftime("%H:%M")


def _iapp(dt: datetime) -> str:
    """ISO timestamp HA accepts (seconds precision, UTC)."""
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


# ---------------------------------------------------------------------------
# Home Assistant WebSocket client
# ---------------------------------------------------------------------------


class _HAClient:
    """One long-lived HA WebSocket connection with id-keyed request/response.

    The connection is created lazily on first use, kept open by a background
    reader task, and re-established with a floor between attempts so a dead HA
    cannot turn every action into a connect storm.
    """

    def __init__(self, plugin: "HomeAssistantPlugin") -> None:
        self._plugin = plugin
        self._session: Any = None
        self._ws: Any = None
        self._reader: Optional[asyncio.Task] = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._next_id = 1
        self._version = ""
        self._connected = False
        self._last_attempt = 0.0
        self._lock = asyncio.Lock()
        self._states: Dict[str, Dict[str, Any]] = {}
        self._states_updated_at = 0.0
        # Streaming commands (render_template) answer with event frames on the
        # same message id instead of a result payload.
        self._streaming: set[int] = set()
        self._event_waiters: Dict[int, asyncio.Future] = {}

    # -- introspection -------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def version(self) -> str:
        return self._version

    @property
    def states(self) -> Dict[str, Dict[str, Any]]:
        return self._states

    @property
    def states_age_sec(self) -> float:
        if not self._states_updated_at:
            return float("inf")
        return time.monotonic() - self._states_updated_at

    def _ws_url(self) -> str:
        base = self._plugin.base_url()
        if base.startswith("https://"):
            return "wss://" + base[len("https://") :].rstrip("/") + "/api/websocket"
        if base.startswith("http://"):
            return "ws://" + base[len("http://") :].rstrip("/") + "/api/websocket"
        return base.rstrip("/") + "/api/websocket"

    # -- lifecycle -----------------------------------------------------------

    async def ensure_connected(self) -> Optional[str]:
        """Return None when a usable connection exists, else an error string."""
        if self._connected and self._ws is not None and not self._ws.closed:
            return None
        async with self._lock:
            if self._connected and self._ws is not None and not self._ws.closed:
                return None
            token = self._plugin.token()
            if not token:
                return "HASS_TOKEN is not configured"
            now = time.monotonic()
            if now - self._last_attempt < _RECONNECT_MIN_INTERVAL_SEC:
                return "Home Assistant is reconnecting, try again in a moment"
            self._last_attempt = now
            await self._teardown()
            try:
                import aiohttp
            except Exception as exc:  # pragma: no cover - dependency missing
                return f"aiohttp is unavailable: {exc}"
            try:
                self._session = aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=None, connect=10)
                )
                ws = await self._session.ws_connect(
                    self._ws_url(), timeout=10, heartbeat=30
                )
                first = await asyncio.wait_for(ws.receive_json(), timeout=10)
                if str(first.get("type")) != "auth_required":
                    await ws.close()
                    return f"unexpected greeting from Home Assistant: {first}"
                await ws.send_json({"type": "auth", "access_token": token})
                reply = await asyncio.wait_for(ws.receive_json(), timeout=10)
                rtype = str(reply.get("type"))
                if rtype == "auth_invalid":
                    await ws.close()
                    return (
                        "Home Assistant rejected the token: "
                        f"{reply.get('message') or 'auth_invalid'}"
                    )
                if rtype != "auth_ok":
                    await ws.close()
                    return f"unexpected auth reply: {reply}"
                self._version = str(reply.get("ha_version") or "")
                self._ws = ws
                self._connected = True
                self._reader = asyncio.create_task(self._read_loop())
            except Exception as exc:
                await self._teardown()
                return (
                    f"cannot reach Home Assistant at {self._plugin.base_url()}: "
                    f"{type(exc).__name__} {exc}"
                )
            error = await self._prime()
            if error:
                await self._teardown()
                return error
            log_info(
                f"{LOG_PREFIX} connected to Home Assistant {self._version} "
                f"({len(self._states)} entities)"
            )
            return None

    async def _prime(self) -> Optional[str]:
        result, error = await self.send(
            {"type": "get_states"}, timeout=20, ensure=False
        )
        if error:
            return error
        if isinstance(result, list):
            self._states = {
                str(item.get("entity_id")): item
                for item in result
                if isinstance(item, dict) and item.get("entity_id")
            }
            self._states_updated_at = time.monotonic()
        if self._plugin.watch_enabled():
            await self.send(
                {"type": "subscribe_events", "event_type": "state_changed"},
                timeout=15,
                ensure=False,
            )
        return None

    async def _teardown(self) -> None:
        self._connected = False
        reader, self._reader = self._reader, None
        if reader is not None and not reader.done():
            reader.cancel()
        for future in list(self._pending.values()):
            if not future.done():
                future.cancel()
        self._pending.clear()
        self._streaming.clear()
        self._event_waiters.clear()
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
        session, self._session = self._session, None
        if session is not None:
            try:
                await session.close()
            except Exception:
                pass

    async def stop(self) -> None:
        await self._teardown()

    # -- framing -------------------------------------------------------------

    async def _read_loop(self) -> None:
        import aiohttp

        ws = self._ws
        try:
            while ws is not None:
                message = await ws.receive()
                if message.type == aiohttp.WSMsgType.TEXT:
                    try:
                        data = json.loads(message.data)
                    except Exception:
                        continue
                elif message.type == aiohttp.WSMsgType.BINARY:
                    try:
                        data = json.loads(message.data.decode("utf-8", "replace"))
                    except Exception:
                        continue
                elif message.type in (
                    aiohttp.WSMsgType.CLOSE,
                    aiohttp.WSMsgType.CLOSING,
                    aiohttp.WSMsgType.CLOSED,
                    aiohttp.WSMsgType.ERROR,
                ):
                    break
                else:
                    continue
                mtype = data.get("type")
                if mtype == "result":
                    future = self._pending.pop(data.get("id"), None)
                    if future is not None and not future.done():
                        future.set_result(data)
                elif mtype == "event":
                    self._handle_event_frame(data)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_debug(f"{LOG_PREFIX} websocket reader stopped: {exc}")
        finally:
            self._connected = False

    def _handle_event_frame(self, frame: Dict[str, Any]) -> None:
        """Route an event frame: streamed command output vs state_changed."""
        message_id = frame.get("id")
        if isinstance(message_id, int) and message_id in self._streaming:
            payload = frame.get("event")
            waiter = self._event_waiters.pop(message_id, None)
            if waiter is not None and not waiter.done():
                waiter.set_result(payload)
            return
        self._apply_event(frame.get("event") or {})

    def _apply_event(self, event: Dict[str, Any]) -> None:
        if str(event.get("event_type")) != "state_changed":
            return
        data = event.get("data") or {}
        entity_id = str(data.get("entity_id") or "")
        new_state = data.get("new_state")
        if not entity_id:
            return
        if isinstance(new_state, dict):
            self._states[entity_id] = new_state
        else:
            self._states.pop(entity_id, None)
        self._states_updated_at = time.monotonic()

    async def send(
        self,
        payload: Dict[str, Any],
        timeout: float = 30.0,
        ensure: bool = True,
    ) -> Tuple[Optional[Any], Optional[str]]:
        """Send one WS command and wait for its result."""
        if ensure:
            error = await self.ensure_connected()
            if error:
                return None, error
        ws = self._ws
        if ws is None or ws.closed:
            return None, "Home Assistant connection is not open"
        message_id = self._next_id
        self._next_id += 1
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[message_id] = future
        body = dict(payload)
        body["id"] = message_id
        try:
            await ws.send_json(body)
            reply = await asyncio.wait_for(future, timeout=timeout)
        except asyncio.TimeoutError:
            self._pending.pop(message_id, None)
            return None, f"Home Assistant did not answer within {int(timeout)}s"
        except asyncio.CancelledError:
            self._pending.pop(message_id, None)
            raise
        except Exception as exc:
            self._pending.pop(message_id, None)
            return None, f"Home Assistant request failed: {exc}"
        if not reply.get("success"):
            error_obj = reply.get("error") or {}
            reason = (
                error_obj.get("message")
                if isinstance(error_obj, dict)
                else str(error_obj)
            )
            return (
                None,
                f"Home Assistant refused the command: {reason or 'unknown error'}",
            )
        return reply.get("result"), None

    async def send_streaming(
        self, payload: Dict[str, Any], timeout: float = 30.0, ensure: bool = True
    ) -> Tuple[Optional[Any], Optional[str]]:
        """Send a command whose answer arrives as an event frame.

        ``render_template`` streams its rendered value on the same message id
        and then completes with an empty result, so the event is the payload
        (and template errors arrive as an event carrying ``error``).
        """
        if ensure:
            error = await self.ensure_connected()
            if error:
                return None, error
        ws = self._ws
        if ws is None or ws.closed:
            return None, "Home Assistant connection is not open"
        message_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_running_loop()
        result_future: asyncio.Future = loop.create_future()
        event_future: asyncio.Future = loop.create_future()
        self._pending[message_id] = result_future
        self._streaming.add(message_id)
        self._event_waiters[message_id] = event_future
        body = dict(payload)
        body["id"] = message_id
        try:
            await ws.send_json(body)
            await asyncio.wait(
                {result_future, event_future},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if not event_future.done():
                # The completion frame often lands first; give the streamed
                # payload a short extra window before giving up on it.
                await asyncio.wait({event_future}, timeout=5)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return None, f"Home Assistant request failed: {exc}"
        finally:
            self._pending.pop(message_id, None)
            self._streaming.discard(message_id)
            self._event_waiters.pop(message_id, None)
        if event_future.done() and not event_future.cancelled():
            streamed = event_future.result()
            if isinstance(streamed, dict) and streamed.get("error"):
                return None, f"Home Assistant template error: {streamed['error']}"
            return streamed, None
        if result_future.done() and not result_future.cancelled():
            reply = result_future.result()
            if isinstance(reply, dict) and not reply.get("success"):
                error_obj = reply.get("error") or {}
                reason = (
                    error_obj.get("message")
                    if isinstance(error_obj, dict)
                    else str(error_obj)
                )
                return (
                    None,
                    f"Home Assistant refused the command: {reason or 'unknown error'}",
                )
            return reply.get("result") if isinstance(reply, dict) else reply, None
        return None, f"Home Assistant did not answer within {int(timeout)}s"

    async def call_service(
        self,
        domain: str,
        service: str,
        service_data: Optional[Dict[str, Any]] = None,
        target: Optional[Dict[str, Any]] = None,
        return_response: bool = False,
        timeout: float = 30.0,
    ) -> Tuple[Optional[Any], Optional[str]]:
        payload: Dict[str, Any] = {
            "type": "call_service",
            "domain": domain,
            "service": service,
        }
        if service_data:
            payload["service_data"] = service_data
        if target:
            payload["target"] = target
        if return_response:
            payload["return_response"] = True
        return await self.send(payload, timeout=timeout)


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------


class HomeAssistantPlugin(PluginBase):
    """Home Assistant bridge plugin (state awareness plus house actions)."""

    display_name = "Home Assistant"
    description = (
        "Lets Synth see the house and act on it through Home Assistant, and "
        "delegates features to HA (image generation first). WebSocket based, "
        "deny-by-default, media cached inside the outbound sandbox."
    )
    # Static-injection plumbing (see core.action_parser.gather_static_injections)
    allow_static_injection_stale_fallback = False
    static_injection_cache_ttl_seconds = 0.0

    # One listener per process for the timezone switch (start() may re-run).
    _timezone_listener_registered: bool = False

    def __init__(self) -> None:
        super().__init__()
        self._client = _HAClient(self)
        self._watched: set[str] = set()
        self._last_injection: Dict[str, Any] = {}
        self._connect_task: Optional[asyncio.Task] = None
        self._forecast_task: Optional[asyncio.Task] = None
        self._forecast: List[Dict[str, Any]] = []
        self._forecast_at = 0.0
        self._core_config: Dict[str, Any] = {}
        self._core_config_at: float = 0.0
        self._core_config_task: Optional[asyncio.Task] = None
        try:
            from core.core_initializer import register_plugin

            register_plugin("home_assistant", self)
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"{LOG_PREFIX} register_plugin failed: {exc}")
        log_info(f"{LOG_PREFIX} HomeAssistantPlugin registered")

    # -- configuration -------------------------------------------------------

    def base_url(self) -> str:
        return _cfg_str("HASS_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")

    def token(self) -> str:
        return _cfg_str("HASS_TOKEN", "")

    def is_enabled(self) -> bool:
        return bool(self.token()) and _cfg_bool("HASS_ENABLED", True)

    def image_enabled(self) -> bool:
        return self.is_enabled() and _cfg_bool("HASS_IMAGE_ENABLED", True)

    def watch_enabled(self) -> bool:
        return _cfg_bool("HASS_AWARENESS_ENABLED", True) or bool(self._watched)

    def _allowed_domains(self) -> List[str]:
        return _cfg_list("HASS_ALLOWED_DOMAINS", _DEFAULT_ALLOWED_DOMAINS)

    def _denied_domains(self) -> List[str]:
        return _cfg_list("HASS_DENIED_DOMAINS", _DEFAULT_DENIED_DOMAINS)

    def _call_timeout(self) -> float:
        return float(max(5, min(_cfg_int("HASS_CALL_TIMEOUT_SEC", 30), 600)))

    def _job_timeout(self) -> float:
        return float(max(10, min(_cfg_int("HASS_JOB_TIMEOUT_SEC", 300), 3600)))

    def _image_styles(self) -> Dict[str, str]:
        styles: Dict[str, str] = {}
        for item in _cfg_str("HASS_IMAGE_ENTITIES", "").split(","):
            chunk = item.strip()
            if not chunk or "=" not in chunk:
                continue
            name, _, entity = chunk.partition("=")
            name = name.strip().lower()
            entity = entity.strip()
            if name and entity:
                styles[name] = entity
        return styles

    def _image_script(self) -> str:
        raw = _cfg_str("HASS_IMAGE_SCRIPT", _DEFAULT_IMAGE_SCRIPT)
        return raw.split(".")[-1] if raw else "synth_image"

    def _domain_error(self, domain: str) -> Optional[str]:
        """Deny beats allow; unknown domains are refused."""
        clean = (domain or "").strip().lower()
        if not clean:
            return "a domain is required"
        if clean in self._denied_domains():
            return f"domain '{clean}' is denied by configuration"
        allowed = self._allowed_domains()
        if allowed and clean not in allowed:
            return (
                f"domain '{clean}' is not in the allowed domain list "
                "(the operator controls HASS_ALLOWED_DOMAINS)"
            )
        return None

    def _target_error(self, target: Optional[Dict[str, Any]]) -> Optional[str]:
        """Refuse a call whose entities live in a denied domain."""
        if not isinstance(target, dict):
            return None
        raw = target.get("entity_id")
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, list):
            return None
        denied = self._denied_domains()
        for entity in raw:
            entity_domain = str(entity).split(".")[0].strip().lower()
            if entity_domain in denied:
                return (
                    f"entity '{entity}' belongs to a denied domain ('{entity_domain}')"
                )
        return None

    # -- media sandbox -------------------------------------------------------

    def _cache_root(self) -> Path:
        raw = _cfg_str("HASS_MEDIA_CACHE_DIR", _DEFAULT_MEDIA_CACHE_DIR)
        path = Path(raw)
        if not path.is_absolute():
            path = _APP_ROOT / path
        return path

    def _ensure_cache_dir(
        self, subdir: str = ""
    ) -> Tuple[Optional[Path], Optional[str]]:
        """Create and validate a cache subfolder inside the outbound roots."""
        target = self._cache_root() / subdir if subdir else self._cache_root()
        try:
            target = target.resolve()
        except Exception:
            pass
        try:
            from core.outbound_file_utils import allowed_file_roots

            roots = [Path(str(root)).resolve() for root in allowed_file_roots()]
        except Exception as exc:  # pragma: no cover - defensive
            return None, f"cannot resolve the media sandbox: {exc}"
        if not any(target == root or root in target.parents for root in roots):
            return None, (
                f"media cache directory '{target}' is outside the allowed "
                "outbound roots, so nothing saved there could be sent"
            )
        try:
            target.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            return None, f"cannot create '{target}': {exc}"
        return target, None

    def _prune_cache(self, folder: Path) -> None:
        keep = max(1, _cfg_int("HASS_MEDIA_KEEP", 50))
        try:
            files = sorted(
                (p for p in folder.iterdir() if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except Exception:
            return
        for stale in files[keep:]:
            try:
                stale.unlink()
                log_debug(f"{LOG_PREFIX} pruned {stale}")
            except Exception as exc:
                log_debug(f"{LOG_PREFIX} could not prune {stale}: {exc}")

    # -- image quota ---------------------------------------------------------

    def _quota_file(self) -> Optional[Path]:
        folder, error = self._ensure_cache_dir()
        if error or folder is None:
            return None
        return folder / "state.json"

    def _quota_used(self) -> int:
        path = self._quota_file()
        if path is None or not path.exists():
            return 0
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return 0
        if str(data.get("date")) != _now().date().isoformat():
            return 0
        try:
            return int(data.get("images") or 0)
        except Exception:
            return 0

    def _quota_bump(self) -> int:
        used = self._quota_used() + 1
        path = self._quota_file()
        if path is not None:
            try:
                path.write_text(
                    json.dumps({"date": _now().date().isoformat(), "images": used}),
                    encoding="utf-8",
                )
            except Exception as exc:
                log_debug(f"{LOG_PREFIX} could not persist the image quota: {exc}")
        return used

    def _quota_left(self) -> Optional[int]:
        cap = _cfg_int("HASS_IMAGE_DAILY_CAP", 10)
        if cap <= 0:
            return None
        return max(0, cap - self._quota_used())

    # -- downloads -----------------------------------------------------------

    async def _download(
        self,
        url_path: str,
        subdir: str,
        filename_hint: str = "",
        not_found_hint: str = "",
    ) -> Tuple[Optional[str], Optional[str], Optional[str]]:
        """Fetch a HA-served file into the cache. Returns (path, mime, error)."""
        folder, error = self._ensure_cache_dir(subdir)
        if error or folder is None:
            return None, None, error
        if not url_path:
            return None, None, "no media URL was returned by Home Assistant"
        url = url_path
        if url.startswith("/"):
            url = self.base_url() + url
        try:
            import aiohttp
        except Exception as exc:  # pragma: no cover
            return None, None, f"aiohttp is unavailable: {exc}"
        max_bytes = max(1, _cfg_int("HASS_MAX_MEDIA_MB", 25)) * 1024 * 1024
        headers = {"Authorization": f"Bearer {self.token()}"}
        try:
            timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    url, headers=headers, allow_redirects=True
                ) as response:
                    if response.status in (401, 403):
                        return (
                            None,
                            None,
                            (
                                "Home Assistant refused the media fetch "
                                f"(HTTP {response.status}); the token may be "
                                "invalid or the signed URL expired"
                            ),
                        )
                    if response.status >= 400:
                        if response.status == 404 and not_found_hint:
                            return None, None, not_found_hint
                        return (
                            None,
                            None,
                            f"media fetch failed with HTTP {response.status}",
                        )
                    length = response.headers.get("Content-Length")
                    if length and length.isdigit() and int(length) > max_bytes:
                        return (
                            None,
                            None,
                            f"file is larger than HASS_MAX_MEDIA_MB ({length} bytes)",
                        )
                    data = await response.read()
                    mime = (response.headers.get("Content-Type") or "").split(";")[0]
        except Exception as exc:
            return None, None, f"media fetch failed: {type(exc).__name__} {exc}"
        if len(data) > max_bytes:
            return None, None, "file is larger than HASS_MAX_MEDIA_MB"
        extension = {
            "image/png": ".png",
            "image/jpeg": ".jpg",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(mime.lower())
        if not extension:
            suffix = Path(url.split("?")[0]).suffix.lower()
            extension = suffix if suffix else ".bin"
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = (
            "".join(
                ch for ch in (filename_hint or "hass") if ch.isalnum() or ch in "-_"
            )[:40]
            or "hass"
        )
        target = folder / f"{base}_{stamp}{extension}"
        try:
            target.write_bytes(data)
        except Exception as exc:
            return None, None, f"cannot write '{target}': {exc}"
        self._prune_cache(folder)
        return str(target), mime, None

    async def _upload_media(
        self, path: str, folder: str = "synth"
    ) -> Tuple[Optional[str], Optional[str]]:
        """Upload a local file into HA's media library; returns its media id."""
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = (_APP_ROOT / candidate).resolve()
        if not candidate.exists():
            return None, f"file not found: {candidate}"
        try:
            from core.outbound_file_utils import resolve_safe_outbound_path

            _resolved, error = resolve_safe_outbound_path(str(candidate))
            if error:
                return None, f"refusing to upload '{candidate}': {error}"
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} sandbox check skipped: {exc}")
        try:
            import aiohttp
        except Exception as exc:  # pragma: no cover
            return None, f"aiohttp is unavailable: {exc}"
        content_type = mimetypes.guess_type(candidate.name)[0] or ""
        if not content_type.startswith(("image/", "video/", "audio/")):
            return None, (
                f"Home Assistant only accepts image, video or audio files in "
                f"its media library ('{candidate.name}' looks like "
                f"'{content_type or 'unknown'}')"
            )
        url = f"{self.base_url()}/api/media_source/local_source/upload"
        headers = {"Authorization": f"Bearer {self.token()}"}
        try:
            timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=120)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                data = aiohttp.FormData()
                data.add_field(
                    "media_content_id", f"media-source://media_source/local/{folder}"
                )
                data.add_field(
                    "file",
                    candidate.read_bytes(),
                    filename=candidate.name,
                    content_type=content_type,
                )
                async with session.post(url, data=data, headers=headers) as response:
                    body = await response.text()
                    if response.status >= 400:
                        return (
                            None,
                            f"media upload failed (HTTP {response.status}): {body[:200]}",
                        )
                    try:
                        payload = json.loads(body)
                    except Exception:
                        return None, f"unexpected upload reply: {body[:200]}"
        except Exception as exc:
            return None, f"media upload failed: {type(exc).__name__} {exc}"
        media_id = str(payload.get("media_content_id") or "")
        if not media_id:
            return None, f"media upload returned no id: {payload}"
        return media_id, None

    # -- house-state injection ----------------------------------------------

    def _awareness_patterns(self) -> List[str]:
        patterns = _cfg_list("HASS_AWARENESS_ENTITIES", _DEFAULT_AWARENESS_ENTITIES)
        if not patterns and self._watched:
            patterns = sorted(self._watched)
        return patterns

    def _render_house_state(self, max_chars: int = 0) -> str:
        states = self._client.states
        patterns = self._awareness_patterns()
        if not states or not patterns:
            return ""
        pieces: List[str] = []
        for entity_id in sorted(states):
            if not any(fnmatch(entity_id, pattern) for pattern in patterns):
                continue
            item = states.get(entity_id) or {}
            value = item.get("state")
            if value in (None, "", "unknown", "unavailable"):
                continue
            attributes = item.get("attributes") or {}
            name = str(attributes.get("friendly_name") or entity_id)
            unit = str(attributes.get("unit_of_measurement") or "")
            pieces.append(f"{name} {value}{unit}")
            if len(pieces) >= _STATE_PIECE_LIMIT:
                break
        if not pieces:
            return ""
        text = ", ".join(pieces)
        limit = max_chars or max(120, _cfg_int("HASS_AWARENESS_MAX_CHARS", 1200))
        if len(text) > limit:
            text = text[: max(60, limit - 3)].rstrip() + "..."
        return text

    def _ensure_connect_task(self) -> None:
        """Open the HA link in the background without ever blocking a caller.

        The prompt builder must not wait on the network, and the house-state
        block is empty until the link exists, so this is what makes the block
        appear on the first turn after boot instead of only after the first
        ``hass_*`` action.
        """
        if not self.is_enabled():
            return
        if self._client.connected:
            return
        task = self._connect_task
        if task is not None and not task.done():
            return
        try:
            self._connect_task = asyncio.get_running_loop().create_task(
                self._connect_and_prime()
            )
        except RuntimeError:  # pragma: no cover - no running loop
            self._connect_task = None

    def beat_awareness_enabled(self) -> bool:
        return self.is_enabled() and _cfg_bool("HASS_BEAT_AWARENESS_ENABLED", False)

    def _beat_allowed(self, beat_type: str) -> bool:
        """True when this beat family may carry the house-state line."""
        wanted = _cfg_list("HASS_BEAT_AWARENESS_BEATS", "")
        if not wanted:
            return True
        name = str(beat_type or "").strip().lower()
        return any(fnmatch(name, pattern) for pattern in wanted)

    # -- weather, location and the bootstrap that feeds them -----------------

    def weather_enabled(self) -> bool:
        return self.is_enabled() and _cfg_bool("HASS_WEATHER_ENABLED", True)

    def location_enabled(self) -> bool:
        return self.is_enabled() and _cfg_bool("HASS_LOCATION_ENABLED", True)

    def timezone_enabled(self) -> bool:
        return self.is_enabled() and _cfg_bool("HASS_TIMEZONE_ENABLED", True)

    def publish_house_timezone(self) -> None:
        """Let the house's own timezone drive the clock.

        Called once HA's core config is known — it carries ``time_zone``, the
        zone the household actually lives in. The core keeps the ``TZ`` config
        as the fallback, so this replaces a value that is wrong for the house
        rather than removing the clock, and a turn built before HA was ever read
        behaves exactly as before.
        """
        if not self.timezone_enabled():
            return
        try:
            from core.time_zone_utils import set_house_timezone

            set_house_timezone(self._time_zone())
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} house timezone publish skipped: {exc}")

    def clear_house_timezone(self) -> None:
        """Give the clock back to the core ``TZ`` config (plugin stopped)."""
        try:
            from core.time_zone_utils import set_house_timezone

            set_house_timezone("")
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} house timezone clear skipped: {exc}")

    def _weather_entity(self) -> str:
        configured = _cfg_str("HASS_WEATHER_ENTITY", "")
        if configured:
            return configured
        for entity_id in sorted(self._client.states):
            if entity_id.startswith("weather."):
                return entity_id
        return ""

    def _time_zone(self) -> str:
        return str(self._core_config.get("time_zone") or "")

    def _render_forecast(self) -> str:
        hours = max(0, _cfg_int("HASS_WEATHER_FORECAST_HOURS", 6))
        if hours <= 0 or not self._forecast:
            return ""
        tz_name = self._time_zone()
        chunks: List[str] = []
        for entry in self._forecast[:hours]:
            condition = str(entry.get("condition") or "").replace("_", " ")
            temperature = _num(entry.get("temperature"))
            when = _local_hhmm(entry.get("datetime"), tz_name)
            bits = " ".join(
                part
                for part in (when, condition, f"{temperature}°" if temperature else "")
                if part
            )
            if bits:
                chunks.append(bits)
        return ", ".join(chunks)

    def _render_weather(self) -> str:
        """Weather line from Home Assistant's own weather entity."""
        if not self.weather_enabled():
            return ""
        entity_id = self._weather_entity()
        if not entity_id:
            return ""
        state = self._client.states.get(entity_id) or {}
        attributes = state.get("attributes") or {}
        condition = str(state.get("state") or "").replace("_", " ").strip()
        temperature = _num(attributes.get("temperature"))
        unit = str(attributes.get("temperature_unit") or "").strip()
        bits: List[str] = []
        if condition or temperature:
            head = condition or "unknown"
            if temperature:
                head += f", {temperature}{unit}"
            bits.append(head)
        for key, label, suffix in (
            ("humidity", "humidity", "%"),
            ("cloud_coverage", "cloud", "%"),
            ("uv_index", "UV", ""),
            ("pressure", "pressure", str(attributes.get("pressure_unit") or "")),
        ):
            value = _num(attributes.get(key))
            if value:
                bits.append(f"{label} {value}{suffix}".strip())
        wind = _num(attributes.get("wind_speed"))
        if wind:
            wind_unit = str(attributes.get("wind_speed_unit") or "")
            bearing = _compass(attributes.get("wind_bearing"))
            bits.append(
                f"wind {wind}{wind_unit}" + (f" from {bearing}" if bearing else "")
            )
        line = ", ".join(bit for bit in bits if bit)

        sun_state = self._client.states.get("sun.sun") or {}
        sun = sun_state.get("attributes") or {}
        sun_bits: List[str] = []
        horizon = str(sun_state.get("state") or "").replace("_", " ")
        if horizon:
            sun_bits.append(f"sun {horizon}")
        for key, label in (("next_rising", "sunrise"), ("next_setting", "sunset")):
            when = _local_hhmm(sun.get(key), self._time_zone())
            if when:
                sun_bits.append(f"{label} {when}")
        if sun_bits:
            line += ("\n" if line else "") + ", ".join(sun_bits)

        forecast = self._render_forecast()
        if forecast:
            line += f"\nNext hours: {forecast}"
        attribution = str(attributes.get("attribution") or "")
        if "met.no" in attribution.lower():
            line += "\n(source: met.no)"
        return line.strip()

    def _render_location(self) -> str:
        """Where the house is and the clock it keeps, from HA's own config."""
        if not self.location_enabled():
            return ""
        core = self._core_config or {}
        zone_attributes = (self._client.states.get("zone.home") or {}).get(
            "attributes"
        ) or {}
        place = str(core.get("location_name") or "").strip() or "home"
        latitude = core.get("latitude", zone_attributes.get("latitude"))
        longitude = core.get("longitude", zone_attributes.get("longitude"))
        bits: List[str] = []
        label = _cfg_str("HASS_LOCATION_LABEL", "")
        if label:
            # A rough place name instead of the pinpoint: the household wants the
            # synth to know WHERE it is without the model holding coordinates it
            # could quote back or that a transcript could carry away.
            bits.append(f"{place} in {label}")
        elif isinstance(latitude, (int, float)) and isinstance(longitude, (int, float)):
            bits.append(f"{place} at {float(latitude):.4f}, {float(longitude):.4f}")
        else:
            bits.append(place)
        country = str(core.get("country") or "").strip()
        if country:
            bits.append(country)
        tz_name = self._time_zone()
        if tz_name:
            bits.append(f"timezone {tz_name}")
        elevation = core.get("elevation")
        if isinstance(elevation, (int, float)) and float(elevation):
            bits.append(f"elevation {_num(elevation, 0)} m")
        return ", ".join(bits)

    async def _refresh_forecast(self) -> None:
        if not self.weather_enabled():
            return
        if max(0, _cfg_int("HASS_WEATHER_FORECAST_HOURS", 6)) <= 0:
            return
        entity_id = self._weather_entity()
        if not entity_id:
            return
        result, error = await self._client.call_service(
            "weather",
            "get_forecasts",
            service_data={"entity_id": entity_id, "type": "hourly"},
            return_response=True,
            timeout=self._call_timeout(),
        )
        if error:
            log_debug(f"{LOG_PREFIX} forecast unavailable: {error}")
            return
        payload: Any = result.get("response") if isinstance(result, dict) else result
        entry = payload.get(entity_id) if isinstance(payload, dict) else None
        forecast = entry.get("forecast") if isinstance(entry, dict) else None
        if isinstance(forecast, list):
            self._forecast = [item for item in forecast if isinstance(item, dict)]
            self._forecast_at = time.monotonic()
            log_debug(f"{LOG_PREFIX} forecast cached: {len(self._forecast)} entries")

    def _schedule_forecast_refresh(self) -> None:
        if not self.weather_enabled():
            return
        if max(0, _cfg_int("HASS_WEATHER_FORECAST_HOURS", 6)) <= 0:
            return
        if self._forecast and time.monotonic() - self._forecast_at < 1800:
            return
        if self._forecast_task is not None and not self._forecast_task.done():
            return
        try:
            self._forecast_task = asyncio.get_running_loop().create_task(
                self._refresh_forecast()
            )
        except RuntimeError:  # pragma: no cover - no running loop
            self._forecast_task = None

    def _schedule_core_config_refresh(self, force: bool = False) -> None:
        """Re-read HA's own configuration once the cached copy has aged.

        That config carries the house's coordinates, elevation and timezone, and
        all three can change on the HA side (a corrected zone, a moved instance).
        Nothing else re-reads them until the next reconnect, so a change made in
        HA would otherwise only land after a restart. The read runs in the
        background: the turn in flight keeps using the last value that landed,
        and a failed read leaves it untouched. ``force`` skips the age check,
        which is what the timezone switch uses.
        """
        if not self.is_enabled():
            return
        ttl = max(0, _cfg_int("HASS_CORE_CONFIG_TTL_SEC", 900))
        if not force:
            if ttl <= 0:
                return
            if self._core_config and time.monotonic() - self._core_config_at < ttl:
                return
        if self._core_config_task is not None and not self._core_config_task.done():
            return
        try:
            self._core_config_task = asyncio.get_running_loop().create_task(
                self._fetch_core_config()
            )
        except RuntimeError:  # pragma: no cover - no running loop
            self._core_config_task = None

    async def _fetch_core_config(self) -> None:
        """One-shot read of HA's own configuration (house location, timezone)."""
        if not self.is_enabled():
            return
        try:
            import aiohttp
        except Exception:  # pragma: no cover - dependency missing
            return
        headers = {"Authorization": f"Bearer {self.token()}"}
        try:
            timeout = aiohttp.ClientTimeout(total=None, connect=10, sock_read=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    f"{self.base_url()}/api/config", headers=headers
                ) as response:
                    if response.status >= 400:
                        log_debug(
                            f"{LOG_PREFIX} core config fetch failed: HTTP {response.status}"
                        )
                        return
                    self._core_config = await response.json()
                    self._core_config_at = time.monotonic()
            self.publish_house_timezone()
        except Exception as exc:
            log_debug(
                f"{LOG_PREFIX} core config fetch failed: {type(exc).__name__} {exc}"
            )

    async def _connect_and_prime(self) -> None:
        """Connect, then cache the pieces the environment blocks need."""
        error = await self._client.ensure_connected()
        if error:
            log_debug(f"{LOG_PREFIX} prime skipped: {error}")
            return
        await self._fetch_core_config()
        await self._refresh_forecast()

    async def get_static_injection(
        self, message: Any = None, context_memory: Any = None
    ) -> Dict[str, Any]:
        """Inject the house state, the weather and the house location.

        Three independent blocks, each with its own switch:

        * ``home`` — the entity snapshot, gated by ``HASS_AWARENESS_ENABLED``
        * ``home_weather`` — HA's weather entity plus a short forecast,
          gated by ``HASS_WEATHER_ENABLED``
        * ``home_location`` — the house's real coordinates and timezone,
          gated by ``HASS_LOCATION_ENABLED``

        When the weather or location block is present, core drops the legacy
        ``weather``/``location`` keys they supersede, so the old wttr.in text and
        the configured location string cannot be told alongside them. With this
        plugin disabled or the switches off, nothing is injected and the old
        providers behave exactly as before.

        Autonomous beat turns additionally require
        ``HASS_BEAT_AWARENESS_ENABLED`` (off by default) plus the optional
        per-beat allowlist.
        """
        if not self.is_enabled():
            return {}
        is_beat, beat_type = _beat_turn_info(message, context_memory)
        # The entity snapshot is house-state awareness, so on beats it stays
        # behind the opt-in switch (and the optional per-beat allowlist).
        # The weather and location blocks are NOT new information: they replace
        # blocks Synth already receives on every route, so they must not be
        # gated here - a beat that kept the old wttr.in line and the configured
        # location string while the replacement sat behind a switch nobody
        # turned on is exactly the bug this fixes.
        if is_beat:
            house_allowed = _cfg_bool(
                "HASS_BEAT_AWARENESS_ENABLED", False
            ) and self._beat_allowed(beat_type)
            beat_cap = max(0, _cfg_int("HASS_BEAT_AWARENESS_MAX_CHARS", 600))
        else:
            house_allowed = _cfg_bool("HASS_AWARENESS_ENABLED", True)
            beat_cap = 0
        max_age = max(30, _cfg_int("HASS_AWARENESS_MAX_AGE_SEC", 900))
        if not self._client.connected:
            # Warm the link for the next turn; this prompt goes out without it.
            self._ensure_connect_task()
            return {}
        if self._client.states_age_sec > max_age:
            return {}
        # The house's own facts (coordinates, elevation, timezone) age on the HA
        # side, so keep them fresh on every live turn, independent of which
        # blocks render below.
        self._schedule_core_config_refresh()

        blocks: Dict[str, Any] = {}
        if house_allowed:
            house = self._render_house_state(max_chars=beat_cap)
            if house:
                blocks["home"] = house
        weather = self._render_weather()
        if weather:
            blocks["home_weather"] = weather
            self._schedule_forecast_refresh()
        location = self._render_location()
        if location:
            blocks["home_location"] = location
        if not blocks:
            return {}
        self._last_injection = dict(blocks)
        return blocks

    # -- actions -------------------------------------------------------------

    def get_supported_action_types(self) -> List[str]:
        return list(self.get_supported_actions().keys()) + ["static_inject"]

    def get_metadata(self) -> Dict[str, Any]:
        return {
            "name": "home_assistant",
            "display_name": "Home Assistant",
            "description": (
                "See the house and act on it through Home Assistant: entity "
                "state and history, service calls, scripts and automations, "
                "Jinja templates, notifications, camera snapshots. Image "
                "generation is delegated to an HA script (ai_task -> ComfyUI) "
                "and the result is cached inside Synth's media sandbox."
            ),
            "category": "Various",
            "icon": "icon.svg",
            "guide": "guide.md",
            "disable_allowed": True,
        }

    def get_supported_actions(self) -> Dict[str, Any]:
        return {
            "hass_status": {
                "repeatable": True,
                "description": (
                    "Check the Home Assistant link: connection state, HA "
                    "version, how many entities are visible, the image styles "
                    "configured, and how many images are left today. Call this "
                    "first when any other hass_ action errors."
                ),
                "required_fields": [],
                "optional_fields": [],
                "security_level": "low",
                "external_effects": ["network"],
            },
            "hass_states": {
                "repeatable": True,
                "description": (
                    "List home entities and their current state. Optional "
                    "'domain' narrows to one domain (light, sensor, switch, "
                    "climate, cover, media_player...), 'search' matches a "
                    "substring of the entity id or friendly name, 'limit' caps "
                    "the rows returned. Use it to find the exact entity id "
                    "before acting on something."
                ),
                "required_fields": [],
                "optional_fields": ["domain", "search", "limit"],
                "security_level": "low",
                "external_effects": ["network"],
            },
            "hass_state": {
                "repeatable": True,
                "description": (
                    "Read one entity in full: state, attributes (battery, "
                    "brightness, temperature, media title...), and when it last "
                    "changed."
                ),
                "required_fields": ["entity_id"],
                "optional_fields": [],
                "security_level": "low",
                "external_effects": ["network"],
            },
            "hass_history": {
                "repeatable": True,
                "description": (
                    "Recent state changes of one entity over the last 'hours' "
                    "(default 6). Use it for 'what happened' questions (when did "
                    "the door open, how did the temperature move) instead of "
                    "guessing from the current value."
                ),
                "required_fields": ["entity_id"],
                "optional_fields": ["hours", "limit"],
                "security_level": "low",
                "external_effects": ["network"],
            },
            "hass_call_service": {
                "description": (
                    "Call a Home Assistant service - this is how the house is "
                    "actually controlled. 'domain' and 'service' name the "
                    "action (light.turn_on, climate.set_temperature, "
                    "media_player.play_media, cover.set_cover_position...), "
                    "'entity_id' or 'area_id' the target, and 'data' carries the "
                    "service fields (brightness_pct, temperature, position...). "
                    "Set 'wait' true when you need the result. Domains outside "
                    "the operator's allow list, and locks/alarm panels/cameras, "
                    "are refused by configuration - say so instead of insisting."
                ),
                "required_fields": ["domain", "service"],
                "optional_fields": ["entity_id", "area_id", "data", "wait"],
                "security_level": "medium",
                "external_effects": ["network", "actuation"],
            },
            "hass_run_script": {
                "description": (
                    "Run a Home Assistant script by name ('goodnight', "
                    "'script.goodnight' or the entity id) with optional "
                    "'variables', and return its response when it has one. "
                    "Prefer a script over a burst of service calls when the "
                    "house has one for the job - the operator keeps the logic "
                    "in HA, where it belongs."
                ),
                "required_fields": ["script"],
                "optional_fields": ["variables", "wait"],
                "security_level": "medium",
                "external_effects": ["network", "actuation"],
            },
            "hass_automation": {
                "description": (
                    "'mode' trigger runs an automation now (target "
                    "'entity_id'); 'list' shows the automations and scripts with "
                    "their on/off state; 'on', 'off' and 'toggle' enable, "
                    "disable or flip one."
                ),
                "required_fields": ["mode"],
                "optional_fields": ["entity_id"],
                "security_level": "medium",
                "external_effects": ["network", "actuation"],
            },
            "hass_template": {
                "description": (
                    "Render a Jinja template inside Home Assistant and return "
                    "the text. This is the precise way to ask a question about "
                    "the house ('how many lights are on', 'is anyone home', "
                    "'when does the washing machine finish') because HA evaluates "
                    "it with full access to its own state."
                ),
                "required_fields": ["template"],
                "optional_fields": [],
                "security_level": "medium",
                "external_effects": ["network"],
            },
            "hass_notify": {
                "description": (
                    "Send a notification through Home Assistant: to a phone via "
                    "its notify service ('target', e.g. 'mobile_app_pixel'), or "
                    "as a persistent notification in the HA UI when no target is "
                    "given. Optional 'image_path' attaches a local image (uploaded "
                    "into the HA media library first) on services that accept "
                    "one."
                ),
                "required_fields": ["message"],
                "optional_fields": ["title", "target", "image_path"],
                "security_level": "medium",
                "external_effects": ["network"],
            },
            "hass_snapshot": {
                "description": (
                    "Grab a still picture from a camera entity and save it "
                    "locally, returning the file path. Use it to look at "
                    "something yourself or to forward the picture in a message. "
                    "Camera streaming and control stay denied by configuration."
                ),
                "required_fields": ["entity_id"],
                "optional_fields": [],
                "security_level": "medium",
                "external_effects": ["network", "filesystem"],
            },
            "hass_image_styles": {
                "repeatable": True,
                "description": (
                    "List the image styles Home Assistant can render (each maps "
                    "to a configured image model/workflow there). Ask for one of "
                    "these names in hass_image_generate."
                ),
                "required_fields": [],
                "optional_fields": [],
                "security_level": "low",
                "external_effects": ["network"],
            },
            "hass_image_generate": {
                "description": (
                    "Draw a picture through the house's image engine (Home "
                    "Assistant runs it on its own GPU). Describe what you want in "
                    "'prompt'; optionally pick a 'style' from "
                    "hass_image_styles. It returns a local file path - send that "
                    "path in your message with send_message (caption and image in "
                    "one message). Rendering can take up to a minute, so do not "
                    "treat a wait as a failure."
                ),
                "required_fields": ["prompt"],
                "optional_fields": ["style"],
                "security_level": "medium",
                "external_effects": ["network", "filesystem"],
            },
            "hass_image_edit": {
                "description": (
                    "Edit an existing picture (change the style, add or remove "
                    "something) by handing Home Assistant a reference image. "
                    "Currently refused by configuration because the configured "
                    "image provider cannot take a reference image yet - if this "
                    "returns an error, tell the user plainly that editing is not "
                    "available yet and offer a fresh generation instead."
                ),
                "required_fields": ["prompt"],
                "optional_fields": ["reference_path", "style"],
                "security_level": "medium",
                "external_effects": ["network", "filesystem"],
            },
            "hass_watch": {
                "description": (
                    "'list' shows which entities are being watched, 'add' and "
                    "'remove' change that set (comma-separated entity ids or "
                    "wildcards). Watched entities are kept fresh in your "
                    "house-state block even when it is configured by hand."
                ),
                "required_fields": ["mode"],
                "optional_fields": ["entity_ids"],
                "security_level": "medium",
                "external_effects": ["network"],
            },
        }

    async def execute_action(
        self,
        action: Dict[str, Any],
        context: Dict[str, Any] | None = None,
        bot: Any = None,
        original_message: Any = None,
    ) -> Dict[str, Any]:
        action = action or {}
        action_name = str(action.get("type") or "")
        payload = action.get("payload") or {}
        try:
            if action_name == "static_inject":
                injection = await self.get_static_injection()
                return {"status": "ok", "home": injection.get("home", "")}

            if not self.is_enabled():
                return {
                    "status": "error",
                    "message": (
                        "Home Assistant is not configured (HASS_TOKEN is empty) "
                        "or the plugin is disabled"
                    ),
                }

            is_beat, beat_type = _beat_turn_info(original_message, context)
            if (
                is_beat
                and action_name in _BEAT_BLOCKED_ACTIONS
                and not _cfg_bool("HASS_BEAT_ACTIONS_ALLOWED", False)
            ):
                # Read-only on her own beats: the house is not hers to move
                # unprompted, whatever the prompt offered her.
                log_info(
                    f"{LOG_PREFIX} refused {action_name} on beat "
                    f"'{beat_type or 'unknown'}' (HASS_BEAT_ACTIONS_ALLOWED is off)"
                )
                return {
                    "status": "error",
                    "message": (
                        "house-changing actions are not available while you are "
                        "on one of your own beats - you can look at the house "
                        "but not change it. Say what you noticed instead, or "
                        "ask the user to do it."
                    ),
                }

            if action_name == "hass_status":
                return await self._act_status()

            if action_name == "hass_states":
                return await self._act_states(payload)

            if action_name == "hass_state":
                return await self._act_state(payload)

            if action_name == "hass_history":
                return await self._act_history(payload)

            if action_name == "hass_call_service":
                return await self._act_call_service(payload)

            if action_name == "hass_run_script":
                return await self._act_run_script(payload)

            if action_name == "hass_automation":
                return await self._act_automation(payload)

            if action_name == "hass_template":
                return await self._act_template(payload)

            if action_name == "hass_notify":
                return await self._act_notify(payload)

            if action_name == "hass_snapshot":
                return await self._act_snapshot(payload)

            if action_name == "hass_image_styles":
                return self._act_image_styles()

            if action_name == "hass_image_generate":
                return await self._act_image_generate(payload)

            if action_name == "hass_image_edit":
                return await self._act_image_edit(payload)

            if action_name == "hass_watch":
                return await self._act_watch(payload)

            return {"status": "error", "message": f"unknown_action:{action_name}"}
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"{LOG_PREFIX} action '{action_name}' failed: {exc}")
            return {"status": "error", "message": str(exc)}

    # -- action implementations ---------------------------------------------

    async def _act_status(self) -> Dict[str, Any]:
        error = await self._client.ensure_connected()
        styles = self._image_styles()
        result: Dict[str, Any] = {
            "status": "ok" if not error else "error",
            "connected": self._client.connected,
            "base_url": self.base_url(),
            "ha_version": self._client.version,
            "entities": len(self._client.states),
            "image_script": self._image_script(),
            "image_styles": sorted(styles.keys()),
            "image_edit_enabled": _cfg_bool("HASS_IMAGE_EDIT_ENABLED", False),
            "images_left_today": self._quota_left(),
            "awareness_enabled": _cfg_bool("HASS_AWARENESS_ENABLED", True),
        }
        if error:
            result["message"] = error
        return result

    async def _act_states(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        error = await self._client.ensure_connected()
        if error:
            return {"status": "error", "message": error}
        domain = str(payload.get("domain") or "").strip().lower()
        search = str(payload.get("search") or "").strip().lower()
        try:
            limit = int(payload.get("limit") or 60)
        except (TypeError, ValueError):
            limit = 60
        limit = max(1, min(limit, 300))
        rows: List[Dict[str, Any]] = []
        for entity_id in sorted(self._client.states):
            item = self._client.states.get(entity_id) or {}
            if domain and not entity_id.startswith(f"{domain}."):
                continue
            attributes = item.get("attributes") or {}
            name = str(attributes.get("friendly_name") or "")
            if (
                search
                and search not in entity_id.lower()
                and search not in name.lower()
            ):
                continue
            unit = str(attributes.get("unit_of_measurement") or "")
            rows.append(
                {
                    "entity_id": entity_id,
                    "name": name,
                    "state": item.get("state"),
                    "unit": unit,
                    "last_changed": item.get("last_changed"),
                }
            )
            if len(rows) >= limit:
                break
        return {"status": "ok", "count": len(rows), "entities": rows}

    async def _act_state(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        entity_id = str(payload.get("entity_id") or "").strip()
        if not entity_id:
            return {"status": "error", "message": "hass_state requires 'entity_id'"}
        error = await self._client.ensure_connected()
        if error:
            return {"status": "error", "message": error}
        item = self._client.states.get(entity_id)
        if item is None:
            result, ws_error = await self._client.send(
                {"type": "get_state", "entity_id": entity_id},
                timeout=self._call_timeout(),
            )
            if ws_error:
                return {"status": "error", "message": ws_error}
            item = result
        if not isinstance(item, dict):
            return {"status": "error", "message": f"unknown entity '{entity_id}'"}
        return {
            "status": "ok",
            "entity_id": item.get("entity_id", entity_id),
            "state": item.get("state"),
            "attributes": item.get("attributes") or {},
            "last_changed": item.get("last_changed"),
            "last_updated": item.get("last_updated"),
        }

    async def _act_history(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        entity_id = str(payload.get("entity_id") or "").strip()
        if not entity_id:
            return {"status": "error", "message": "hass_history requires 'entity_id'"}
        try:
            hours = float(payload.get("hours") or 6)
        except (TypeError, ValueError):
            hours = 6.0
        hours = max(0.1, min(hours, 24 * 30))
        try:
            limit = int(payload.get("limit") or 40)
        except (TypeError, ValueError):
            limit = 40
        limit = max(1, min(limit, 200))
        end = _now()
        start = end - timedelta(hours=hours)
        result, error = await self._client.send(
            {
                "type": "history/history_during_period",
                "start_time": _iapp(start),
                "end_time": _iapp(end),
                "entity_ids": [entity_id],
                "minimal_response": True,
                "no_attributes": True,
            },
            timeout=self._call_timeout(),
        )
        if error:
            return {"status": "error", "message": error}
        series = []
        if isinstance(result, dict):
            series = result.get(entity_id) or []
        elif isinstance(result, list) and result:
            series = result[0] if isinstance(result[0], list) else result
        changes: List[Dict[str, Any]] = []
        for entry in series:
            if not isinstance(entry, dict):
                continue
            # minimal_response uses short keys: s = state, lu/lc = timestamps.
            value = entry.get("s") if "s" in entry else entry.get("state")
            when = (
                entry.get("lc")
                or entry.get("lu")
                or entry.get("last_changed")
                or entry.get("last_updated")
            )
            if isinstance(when, (int, float)):
                try:
                    when = datetime.fromtimestamp(
                        float(when), tz=timezone.utc
                    ).isoformat()
                except Exception:
                    when = str(when)
            changes.append({"at": when, "state": value})
        return {
            "status": "ok",
            "entity_id": entity_id,
            "hours": hours,
            "count": len(changes),
            "changes": changes[-limit:],
        }

    async def _act_call_service(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        domain = str(payload.get("domain") or "").strip().lower()
        service = str(payload.get("service") or "").strip().lower()
        if not service:
            return {
                "status": "error",
                "message": "hass_call_service requires 'service'",
            }
        domain_error = self._domain_error(domain)
        if domain_error:
            return {"status": "error", "message": domain_error}
        target: Dict[str, Any] = {}
        entity_id = payload.get("entity_id")
        if entity_id:
            target["entity_id"] = entity_id
        area_id = str(payload.get("area_id") or "").strip()
        if area_id:
            target["area_id"] = area_id
        target_error = self._target_error(target)
        if target_error:
            return {"status": "error", "message": target_error}
        service_data = payload.get("data")
        if not isinstance(service_data, dict):
            service_data = {}
        wait = bool(payload.get("wait"))
        timeout = self._job_timeout() if wait else self._call_timeout()
        result, error = await self._client.call_service(
            domain,
            service,
            service_data=service_data or None,
            target=target or None,
            return_response=wait,
            timeout=timeout,
        )
        if error:
            return {"status": "error", "message": error}
        response: Dict[str, Any] = {
            "status": "ok",
            "domain": domain,
            "service": service,
            "target": target,
        }
        if isinstance(result, dict) and "response" in result:
            response["response"] = result.get("response")
        return response

    async def _act_run_script(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        raw = str(payload.get("script") or "").strip()
        if not raw:
            return {"status": "error", "message": "hass_run_script requires 'script'"}
        key = raw.split(".")[-1].split("/")[-1].strip().lower()
        if not key or not all(ch.isalnum() or ch in "-_" for ch in key):
            return {"status": "error", "message": f"invalid script name '{raw}'"}
        domain_error = self._domain_error("script")
        if domain_error:
            return {"status": "error", "message": domain_error}
        variables = payload.get("variables")
        if not isinstance(variables, dict):
            variables = {}
        wait = payload.get("wait")
        wait = True if wait is None else bool(wait)
        result, error = await self._client.call_service(
            "script",
            key,
            service_data=variables or None,
            return_response=wait,
            timeout=self._job_timeout() if wait else self._call_timeout(),
        )
        if error:
            return {"status": "error", "message": error}
        response: Dict[str, Any] = {"status": "ok", "script": key}
        if isinstance(result, dict) and "response" in result:
            response["response"] = result.get("response")
        return response

    async def _act_automation(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        mode = str(payload.get("mode") or "").strip().lower()
        entity_id = str(payload.get("entity_id") or "").strip()
        if mode == "list":
            rows = []
            for target_domain in ("automation", "script", "scene"):
                for eid in sorted(self._client.states):
                    if eid.startswith(f"{target_domain}."):
                        item = self._client.states.get(eid) or {}
                        attributes = item.get("attributes") or {}
                        rows.append(
                            {
                                "entity_id": eid,
                                "name": str(attributes.get("friendly_name") or ""),
                                "state": item.get("state"),
                            }
                        )
            return {"status": "ok", "count": len(rows), "automations": rows}
        if mode not in ("trigger", "on", "off", "toggle"):
            return {
                "status": "error",
                "message": "hass_automation requires 'mode' = trigger|list|on|off|toggle",
            }
        if not entity_id:
            return {
                "status": "error",
                "message": f"hass_automation {mode} requires 'entity_id'",
            }
        domain = entity_id.split(".")[0].strip().lower()
        if domain not in ("automation", "script"):
            return {
                "status": "error",
                "message": "automation/script entities only",
            }
        domain_error = self._domain_error(domain)
        if domain_error:
            return {"status": "error", "message": domain_error}
        service = "trigger" if mode == "trigger" else mode
        result, error = await self._client.call_service(
            domain,
            service,
            target={"entity_id": entity_id},
            timeout=self._call_timeout(),
        )
        if error:
            return {"status": "error", "message": error}
        return {"status": "ok", "entity_id": entity_id, "mode": mode}

    async def _act_template(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        template = str(payload.get("template") or "").strip()
        if not template:
            return {"status": "error", "message": "hass_template requires 'template'"}
        if len(template) > 4000:
            return {"status": "error", "message": "template is too long"}
        result, error = await self._client.send_streaming(
            {
                "type": "render_template",
                "template": template,
                "report_errors": True,
                "timeout": 5,
            },
            timeout=self._call_timeout(),
        )
        if error:
            return {"status": "error", "message": error}
        text = result.get("result") if isinstance(result, dict) else result
        if text is None:
            return {
                "status": "ok",
                "result": "",
                "note": "the template rendered an empty value",
            }
        rendered = text if isinstance(text, str) else json.dumps(text)
        if len(rendered) > 4000:
            rendered = rendered[:4000] + "..."
        return {"status": "ok", "result": rendered}

    async def _act_notify(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        message = str(payload.get("message") or "").strip()
        if not message:
            return {"status": "error", "message": "hass_notify requires 'message'"}
        title = str(payload.get("title") or "").strip()
        target = str(payload.get("target") or "").strip()
        image_path = str(payload.get("image_path") or "").strip()
        service_data: Dict[str, Any] = {"message": message}
        if title:
            service_data["title"] = title
        if image_path:
            media_id, upload_error = await self._upload_media(image_path)
            if upload_error:
                return {"status": "error", "message": upload_error}
            if media_id:
                # Local media source URLs are served under /media/local/.
                suffix = media_id.split("local/", 1)[-1]
                service_data["data"] = {"image": f"/media/local/{suffix}"}
        if target:
            domain = "notify"
            service = target.split(".")[-1].strip().lower()
            domain_error = self._domain_error(domain)
            if domain_error:
                return {"status": "error", "message": domain_error}
            result, error = await self._client.call_service(
                domain, service, service_data=service_data, timeout=self._call_timeout()
            )
            if error:
                return {"status": "error", "message": error}
            return {
                "status": "ok",
                "service": f"notify.{service}",
                "image": bool(image_path),
            }
        result, error = await self._client.call_service(
            "persistent_notification",
            "create",
            service_data=service_data,
            timeout=self._call_timeout(),
        )
        if error:
            return {"status": "error", "message": error}
        return {"status": "ok", "service": "persistent_notification.create"}

    async def _act_snapshot(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        entity_id = str(payload.get("entity_id") or "").strip()
        if not entity_id.startswith("camera."):
            return {
                "status": "error",
                "message": "hass_snapshot needs a camera.* entity",
            }
        error = await self._client.ensure_connected()
        if error:
            return {"status": "error", "message": error}
        object_id = entity_id.split(".", 1)[1]
        path, mime, download_error = await self._download(
            f"/api/camera_proxy/{object_id}",
            "camera",
            filename_hint=object_id,
            not_found_hint=(
                f"camera '{entity_id}' returned no image (the entity is "
                "probably unavailable or busy streaming)"
            ),
        )
        if download_error:
            return {"status": "error", "message": download_error}
        return {"status": "ok", "entity_id": entity_id, "path": path, "mime_type": mime}

    def _act_image_styles(self) -> Dict[str, Any]:
        styles = self._image_styles()
        default = _cfg_str("HASS_IMAGE_DEFAULT_STYLE", "quick").lower()
        return {
            "status": "ok",
            "default": default,
            "styles": [
                {"style": name, "target": entity}
                for name, entity in sorted(styles.items())
            ],
            "image_script": self._image_script(),
            "edit_enabled": _cfg_bool("HASS_IMAGE_EDIT_ENABLED", False),
        }

    async def _act_image_generate(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.image_enabled():
            return {
                "status": "error",
                "message": "image generation is disabled by configuration",
            }
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return {
                "status": "error",
                "message": "hass_image_generate requires 'prompt'",
            }
        styles = self._image_styles()
        style = str(payload.get("style") or "").strip().lower()
        if not style:
            style = _cfg_str("HASS_IMAGE_DEFAULT_STYLE", "quick").lower()
        if styles and style not in styles:
            return {
                "status": "error",
                "message": (
                    f"unknown style '{style}'; available: {', '.join(sorted(styles))}"
                ),
            }
        left = self._quota_left()
        if left is not None and left <= 0:
            return {
                "status": "error",
                "message": (
                    "the daily image limit is reached "
                    "(HASS_IMAGE_DAILY_CAP); tell the user and offer to try "
                    "again tomorrow"
                ),
            }
        error = await self._client.ensure_connected()
        if error:
            return {"status": "error", "message": error}
        script = self._image_script()
        service_data: Dict[str, Any] = {"prompt": prompt}
        if style:
            service_data["style"] = style
        result, call_error = await self._client.call_service(
            "script",
            script,
            service_data=service_data,
            return_response=True,
            timeout=self._job_timeout(),
        )
        if call_error:
            return {"status": "error", "message": call_error}
        response = result.get("response") if isinstance(result, dict) else None
        if response is None and isinstance(result, dict):
            response = result
        if not isinstance(response, dict):
            return {
                "status": "error",
                "message": f"the image script '{script}' returned no result",
            }
        url = str(response.get("url") or response.get("media_source_url") or "")
        media_id = str(response.get("media_source_id") or "")
        path, mime, download_error = await self._download(
            url, "images", filename_hint=style or "image"
        )
        if download_error:
            return {
                "status": "error",
                "message": (
                    f"Home Assistant produced the image but it could not be "
                    f"fetched: {download_error}"
                ),
                "media_source_id": media_id,
            }
        used = self._quota_bump()
        log_info(
            f"{LOG_PREFIX} image generated (style={style or 'default'}) -> {path} "
            f"[{used} today]"
        )
        return {
            "status": "ok",
            "path": path,
            "mime_type": mime,
            "style": style or "default",
            "media_source_id": media_id,
            "model": response.get("model"),
            "width": response.get("width"),
            "height": response.get("height"),
            "revised_prompt": response.get("revised_prompt"),
            "images_used_today": used,
            "note": (
                "send this path in a message with send_message, caption and "
                "image together, and tell the user the style you used"
            ),
        }

    async def _act_image_edit(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not self.image_enabled():
            return {
                "status": "error",
                "message": "image editing is disabled by configuration",
            }
        if not _cfg_bool("HASS_IMAGE_EDIT_ENABLED", False):
            return {
                "status": "error",
                "message": (
                    "image editing is not available yet: the Home Assistant "
                    "image engine cannot take a reference image at the moment. "
                    "Tell the user plainly and offer a fresh generation instead."
                ),
            }
        reference = str(payload.get("reference_path") or "").strip()
        if not reference:
            return {
                "status": "error",
                "message": "hass_image_edit requires 'reference_path'",
            }
        media_id, upload_error = await self._upload_media(reference, folder="synth")
        if upload_error:
            return {"status": "error", "message": upload_error}
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return {"status": "error", "message": "hass_image_edit requires 'prompt'"}
        style = str(payload.get("style") or "").strip().lower()
        left = self._quota_left()
        if left is not None and left <= 0:
            return {
                "status": "error",
                "message": "the daily image limit is reached (HASS_IMAGE_DAILY_CAP)",
            }
        service_data: Dict[str, Any] = {"prompt": prompt, "reference": media_id}
        if style:
            service_data["style"] = style
        result, call_error = await self._client.call_service(
            "script",
            self._image_script(),
            service_data=service_data,
            return_response=True,
            timeout=self._job_timeout(),
        )
        if call_error:
            return {"status": "error", "message": call_error}
        response = result.get("response") if isinstance(result, dict) else None
        if not isinstance(response, dict):
            return {"status": "error", "message": "the image script returned no result"}
        path, mime, download_error = await self._download(
            str(response.get("url") or ""), "images", filename_hint="edit"
        )
        if download_error:
            return {"status": "error", "message": download_error}
        self._quota_bump()
        return {
            "status": "ok",
            "path": path,
            "mime_type": mime,
            "style": style or "default",
        }

    async def _act_watch(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        mode = str(payload.get("mode") or "").strip().lower()
        raw = str(payload.get("entity_ids") or "")
        items = [item.strip().lower() for item in raw.split(",") if item.strip()]
        if mode == "list":
            return {"status": "ok", "watching": sorted(self._watched)}
        if mode not in ("add", "remove"):
            return {
                "status": "error",
                "message": "hass_watch requires 'mode' = list|add|remove",
            }
        if not items:
            return {
                "status": "error",
                "message": f"hass_watch {mode} requires 'entity_ids'",
            }
        if mode == "add":
            self._watched.update(items)
        else:
            for item in items:
                self._watched.discard(item)
        return {"status": "ok", "mode": mode, "watching": sorted(self._watched)}

    # -- lifecycle -----------------------------------------------------------

    async def start(self) -> None:
        """Connect to Home Assistant in the background (queued by the core).

        The core queues an async plugin ``start()`` during boot, so the link and
        the first state snapshot exist before the first prompt is built.
        """
        self._ensure_connect_task()
        self._register_timezone_listener()

    def _register_timezone_listener(self) -> None:
        """Let *House Timezone Drives The Clock* take effect without a restart."""
        if HomeAssistantPlugin._timezone_listener_registered:
            return
        try:
            from core.config_manager import config_registry

            config_registry.add_listener(
                "HASS_TIMEZONE_ENABLED", self._on_timezone_switch
            )
            HomeAssistantPlugin._timezone_listener_registered = True
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} timezone switch listener skipped: {exc}")

    def _on_timezone_switch(self, _value: object) -> None:
        """Off hands the clock back to the TZ config; on re-reads the house's."""
        if self.timezone_enabled():
            # Re-read rather than re-publish the cached copy, so flipping the
            # switch is also how a timezone changed in HA is picked up at once.
            self._schedule_core_config_refresh(force=True)
        else:
            self.clear_house_timezone()

    def stop(self) -> None:
        """Best-effort teardown; the connection is rebuilt on demand."""
        # The clock goes back to the core TZ config when the plugin is stopped:
        # the house timezone is only authoritative while this plugin runs.
        self.clear_house_timezone()
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(self._client.stop())
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(f"{LOG_PREFIX} stop() skipped: {exc}")


PLUGIN_CLASS = HomeAssistantPlugin
