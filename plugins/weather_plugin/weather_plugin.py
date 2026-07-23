import asyncio
import functools
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from typing import Optional
import concurrent.futures

from core.core_initializer import register_plugin
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.time_zone_utils import get_local_location, utc_to_local
from core.config_manager import config_registry, ConfigVar
from core.variables_engine import register_exposed_var

# Injection priority for weather information
INJECTION_PRIORITY = 2  # High priority - weather is contextually important


MAX_WEATHER_FETCH_RETRIES = 2
# Hard cap on how long a single wttr.in request may block. Without this,
# urllib.request.urlopen can hang indefinitely on a stalled connection,
# which freezes the entire scheduler loop (blocking the daily report check).
WEATHER_FETCH_TIMEOUT_SECONDS = 15
DEFAULT_WEATHER_UNAVAILABLE = (
    "Meteo non disponibile al momento, riprova tra qualche minuto."
)
# Owner marker for the persistent daily-report scheduled event. The weather
# plugin owns these events: on config change it clears all of them and creates
# a fresh one, and it dispatches them itself (routing to the configured
# interface) rather than through the generic event dispatcher.
WEATHER_EVENT_CREATED_BY = "weather_plugin"


def register_injection_priority():
    """Register this component's injection priority."""
    log_info(f"[weather_plugin] Registered injection priority: {INJECTION_PRIORITY}")
    return INJECTION_PRIORITY


# Register priority when module is loaded
register_injection_priority()

# Register exposed variable for WebUI
register_exposed_var(
    "WEATHER_FETCH_TIME",
    label="Weather Fetch Interval",
    default=60,
    value_type=int,
    ui_type="number",
    description="Minutes between weather data fetches",
    scope="plugins",
    component="weather",
    tags=["plugin"],
)

register_exposed_var(
    "WEATHER_DAILY_REPORT_ENABLED",
    label="Daily Weather Report",
    default=False,
    value_type=bool,
    ui_type="boolean",
    description="Send a daily weather announcement at the configured local time",
    scope="plugins",
    component="weather",
    tags=["plugin"],
)

register_exposed_var(
    "WEATHER_DAILY_REPORT_TIME",
    label="Daily Weather Report Time",
    default="06:00",
    value_type=str,
    ui_type="string",
    description="Local time (HH:MM) for the daily weather announcement",
    scope="plugins",
    component="weather",
    tags=["plugin"],
)

register_exposed_var(
    "WEATHER_DAILY_REPORT_LANGUAGE",
    label="Daily Weather Report Language",
    default="",
    value_type=str,
    ui_type="string",
    description="Language for the daily weather announcement (e.g., Italian)",
    scope="plugins",
    component="weather",
    tags=["plugin"],
)

register_exposed_var(
    "WEATHER_DAILY_REPORT_INTERFACE",
    label="Daily Weather Report Interface",
    default="synth_webui",
    value_type=str,
    ui_type="interface-path",
    description="Interface id used for the daily weather announcement",
    scope="plugins",
    component="weather",
    tags=["plugin"],
)


class WeatherPlugin:
    """Plugin to provide weather information using wttr.in."""

    display_name = "Weather"

    def __init__(self):
        register_plugin("weather", self)
        log_info("[weather_plugin] Registered WeatherPlugin")
        self._cached_weather: Optional[str] = None
        self._last_fetch: float = 0.0
        self._update_task: Optional[asyncio.Task] = None

        # Use ConfigVar for always-fresh reads from config_registry.
        # ConfigVar fetches the latest value on every access, so it never
        # goes out of sync — even if the value is updated in the DB after
        # the plugin starts. This replaces the fragile listener-based pattern
        # that could miss updates due to startup timing.
        self._fetch_minutes_var = ConfigVar(
            "WEATHER_FETCH_TIME", registry=config_registry
        )
        self._daily_report_enabled_var = ConfigVar(
            "WEATHER_DAILY_REPORT_ENABLED", registry=config_registry
        )
        self._daily_report_time_var = ConfigVar(
            "WEATHER_DAILY_REPORT_TIME", registry=config_registry
        )
        self._daily_report_language_var = ConfigVar(
            "WEATHER_DAILY_REPORT_LANGUAGE", registry=config_registry
        )
        self._daily_report_interface_var = ConfigVar(
            "WEATHER_DAILY_REPORT_INTERFACE", registry=config_registry
        )

        # Ensure these vars are registered in config_registry with proper defaults
        # (ConfigVar only reads, doesn't register — so we do a get_value call to
        # ensure each key is defined before any ConfigVar tries to read it).
        config_registry.get_value(
            "WEATHER_FETCH_TIME",
            60,
            label="Weather Fetch Interval",
            description="Minutes between weather data fetches",
            value_type=int,
            group="plugins",
            component="weather",
            advanced=False,
        )
        config_registry.get_value(
            "WEATHER_DAILY_REPORT_ENABLED",
            False,
            label="Daily Weather Report",
            description="Send a daily weather announcement at the configured local time",
            value_type=bool,
            group="plugins",
            component="weather",
            advanced=False,
        )
        config_registry.get_value(
            "WEATHER_DAILY_REPORT_TIME",
            "06:00",
            label="Daily Weather Report Time",
            description="Local time (HH:MM) for the daily weather announcement",
            value_type=str,
            group="plugins",
            component="weather",
            advanced=False,
        )
        config_registry.get_value(
            "WEATHER_DAILY_REPORT_LANGUAGE",
            "",
            label="Daily Weather Report Language",
            description="Language for the daily weather announcement (e.g., Italian)",
            value_type=str,
            group="plugins",
            component="weather",
            advanced=False,
        )
        config_registry.get_value(
            "WEATHER_DAILY_REPORT_INTERFACE",
            "synth_webui",
            label="Daily Weather Report Interface",
            description="Interface id used for the daily weather announcement",
            value_type=str,
            group="plugins",
            component="weather",
            advanced=False,
        )

        # Use a dedicated executor so we don't depend on the event loop's default executor
        # which may be shut down during interpreter shutdown.
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        # Background scheduler task (managed as singleton per process)
        self._scheduler_task = None
        self._scheduler_running = False
        # Last applied daily-report configuration snapshot. Used to detect config
        # changes so the persistent scheduled event can be cleared and recreated.
        # None means "not yet reconciled since process start".
        self._last_report_config: Optional[tuple[bool, str, str, str]] = None

    @property
    def fetch_minutes(self) -> int:
        """Return fetch interval in minutes, always fresh from registry."""
        raw = str(self._fetch_minutes_var.value or "60")
        try:
            return int(raw)
        except (ValueError, TypeError):
            return 60

    @property
    def daily_report_enabled(self) -> bool:
        """Return whether daily report is enabled, always fresh from registry."""
        raw = str(self._daily_report_enabled_var.value or "false")
        return raw.strip().lower() in {"1", "true", "yes", "on"}

    @property
    def daily_report_time(self) -> str:
        """Return daily report time, always fresh from registry."""
        return str(self._daily_report_time_var.value or "06:00")

    @property
    def daily_report_language(self) -> str:
        """Return daily report language hint, always fresh from registry."""
        return str(self._daily_report_language_var.value or "")

    @property
    def daily_report_interface(self) -> str:
        """Return daily report interface ID, always fresh from registry."""
        return str(self._daily_report_interface_var.value or "")

    # Plugin action registration
    def get_supported_action_types(self):
        return ["static_inject", "trigger_weather_report"]

    def get_supported_actions(self):
        return {
            "static_inject": {
                "description": "Inject static contextual data into every prompt",
                "required_fields": [],
                "optional_fields": [],
            },
            "trigger_weather_report": {
                "description": "Manually trigger a weather announcement via LLM.",
                "required_fields": [],
                "optional_fields": ["interface_path", "interface_id"],
            },
        }

    async def execute_action(self, action: dict, context: dict, bot, original_message):
        action_type = action.get("type")
        payload = action.get("payload", {}) or {}

        if action_type == "trigger_weather_report":
            target_path = payload.get("interface_path")
            target_iface = payload.get("interface_id")
            # Prefer the full interface_path when provided: it pins the exact
            # recipient. _trigger_manual_report splits it into the registry name
            # and the explicit delivery target internally.
            target = None
            if isinstance(target_path, str) and target_path.strip():
                target = target_path.strip()
            elif isinstance(target_iface, str) and target_iface.strip():
                target = target_iface.strip()
            if not target:
                log_warning(
                    "[weather_plugin] trigger_weather_report missing interface_id/interface_path"
                )
                return {
                    "status": "error",
                    "message": "interface_id or interface_path required",
                }

            success = await self._trigger_manual_report(target)
            return {"status": "success" if success else "failed"}

        return {"error": "Unknown action"}

    async def get_static_injection(self) -> dict:
        """Get current weather for static injection. Returns cached value immediately."""
        now = time.time()
        timeout_sec = self.fetch_minutes * 60

        is_stale = not self._cached_weather or (now - self._last_fetch > timeout_sec)

        if is_stale:
            if self._update_task and not self._update_task.done():
                pass
            else:
                log_debug(
                    "[weather_plugin] Weather data is stale or missing, triggering background update"
                )
                asyncio.create_task(self._update_weather())

        weather_text = self._cached_weather
        if not weather_text:
            weather_text = DEFAULT_WEATHER_UNAVAILABLE

        return {"weather": weather_text}

    async def _ensure_weather(self) -> None:
        now = time.time()
        if not self._cached_weather or now - self._last_fetch > self.fetch_minutes * 60:
            await self._update_weather()

    async def _update_weather(self) -> None:
        """Coordinating method to ensure only one fetch runs at a time."""
        if self._update_task and not self._update_task.done():
            # Join existing task
            try:
                await self._update_task
            except asyncio.CancelledError:
                pass
            return

        # Start new task
        try:
            loop = asyncio.get_running_loop()
            self._update_task = loop.create_task(self._fetch_weather_data())
            await self._update_task
        except RuntimeError:
            # If no running loop, just return (likely shutdown)
            pass
        except Exception as e:
            log_error(f"[weather_plugin] Error in update coordination: {e}")

    async def _is_weather_data_valid(self, cc: dict) -> bool:
        if not isinstance(cc, dict):
            return False
        desc = cc.get("weatherDesc", [{}])[0].get("value", "")
        temp_c = cc.get("temp_C")
        feels_c = cc.get("FeelsLikeC")

        if not desc or desc == "N/A":
            return False
        if temp_c in (None, "", "N/A"):
            return False
        if feels_c in (None, "", "N/A"):
            return False
        return True

    async def get_current_weather(self) -> dict:
        """Return the latest weather data, triggering refresh if needed."""
        await self._ensure_weather()
        if self._cached_weather and "⚠️" not in self._cached_weather:
            status = "ok"
            weather_text = self._cached_weather
        else:
            status = "unavailable"
            weather_text = self._cached_weather or DEFAULT_WEATHER_UNAVAILABLE

        return {
            "weather": weather_text,
            "status": status,
            "last_fetch": datetime.utcfromtimestamp(self._last_fetch).isoformat()
            if self._last_fetch
            else None,
        }

    async def _fetch_weather_data(self) -> None:
        """Actual worker method to fetch weather from wttr.in."""
        location = get_local_location()
        encoded = urllib.parse.quote(location)
        url = f"https://wttr.in/{encoded}?format=j1"

        attempt = 0
        while attempt < MAX_WEATHER_FETCH_RETRIES:
            attempt += 1
            log_info(
                f"[weather_plugin] Fetching weather for {location} (attempt {attempt})"
            )

            try:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    # Event loop is closed; skip update
                    log_warning(
                        "[weather_plugin] Event loop closed; aborting weather update"
                    )
                    self._cached_weather = f"{location}: ⚠️ Weather service temporarily unavailable (system shutting down)"
                    return

                try:
                    response = await loop.run_in_executor(
                        self._executor,
                        functools.partial(
                            urllib.request.urlopen,
                            url,
                            timeout=WEATHER_FETCH_TIMEOUT_SECONDS,
                        ),
                    )
                    data_bytes = await loop.run_in_executor(
                        self._executor, response.read
                    )
                except urllib.error.HTTPError as e:
                    log_warning(
                        f"[weather_plugin] HTTP error fetching weather: {e.code} {e.reason}"
                    )
                    if attempt < MAX_WEATHER_FETCH_RETRIES:
                        await asyncio.sleep(1)
                        continue
                    self._cached_weather = (
                        f"{location}: ⚠️ Cannot reach weather service (HTTP {e.code})"
                    )
                    return
                except urllib.error.URLError as e:
                    log_warning(
                        f"[weather_plugin] Network error fetching weather: {e.reason}"
                    )
                    if attempt < MAX_WEATHER_FETCH_RETRIES:
                        await asyncio.sleep(1)
                        continue
                    self._cached_weather = f"{location}: ⚠️ Cannot reach weather service (connection failed)"
                    return
                except RuntimeError as e:
                    log_warning(
                        f"[weather_plugin] Could not schedule weather read: {e}"
                    )
                    self._cached_weather = (
                        f"{location}: ⚠️ Weather service temporarily unavailable"
                    )
                    return

                if not data_bytes:
                    log_warning("[weather_plugin] Empty response from weather service")
                    if attempt < MAX_WEATHER_FETCH_RETRIES:
                        await asyncio.sleep(1)
                        continue
                    self._cached_weather = (
                        f"{location}: ⚠️ Weather service returned empty response"
                    )
                    return

                try:
                    data = json.loads(data_bytes.decode())
                except json.JSONDecodeError as e:
                    log_warning(f"[weather_plugin] Invalid JSON weather data: {e}")
                    if attempt < MAX_WEATHER_FETCH_RETRIES:
                        await asyncio.sleep(1)
                        continue
                    self._cached_weather = (
                        f"{location}: ⚠️ Weather service returned invalid data"
                    )
                    return

                cc = data.get("current_condition", [{}])[0]
                if not await self._is_weather_data_valid(cc):
                    log_warning(
                        "[weather_plugin] Received invalid or incomplete weather payload"
                    )
                    if attempt < MAX_WEATHER_FETCH_RETRIES:
                        await asyncio.sleep(1)
                        continue
                    self._cached_weather = (
                        f"{location}: ⚠️ Meteo non disponibile (dati non completi)"
                    )
                    self._last_fetch = time.time()
                    return

                desc = cc.get("weatherDesc", [{}])[0].get("value", "N/A")
                temp_c = cc.get("temp_C", "N/A")
                feels_c = cc.get("FeelsLikeC", "N/A")
                humidity = cc.get("humidity", "N/A")
                wind_speed = cc.get("windspeedKmph", "N/A")
                wind_dir = cc.get("winddir16Point", "N/A")
                cloudcover = cc.get("cloudcover", "N/A")
                visibility = cc.get("visibility", "N/A")
                pressure = cc.get("pressure", "N/A")

                log_debug(
                    "[weather_plugin] Parsed values: desc=%s temp=%s feels=%s humidity=%s wind=%s%s cloud=%s visibility=%s pressure=%s"
                    % (
                        desc,
                        temp_c,
                        feels_c,
                        humidity,
                        wind_speed,
                        wind_dir,
                        cloudcover,
                        visibility,
                        pressure,
                    )
                )

                emoji = self._choose_emoji(desc)
                weather_string = (
                    f"{location}: {emoji} {desc} +{temp_c}°C ("
                    f"Feels like {feels_c}°C, Humidity {humidity}%, "
                    f"Wind {wind_speed}km/h {wind_dir}, Visibility {visibility}km, "
                    f"Pressure {pressure}hPa, Cloud cover {cloudcover}% )"
                )
                log_debug(f"[weather_plugin] Final weather string: {weather_string}")

                self._cached_weather = weather_string
                self._last_fetch = time.time()
                log_info(f"[weather_plugin] Weather updated: {self._cached_weather}")
                return
            except Exception as e:
                log_warning(f"[weather_plugin] Failed to fetch weather: {e}")
                log_error("[weather_plugin] Weather update error", e)
                if attempt < MAX_WEATHER_FETCH_RETRIES:
                    await asyncio.sleep(1)
                    continue
                self._cached_weather = (
                    f"{location}: ⚠️ Weather service error (please try again later)"
                )
                return

    async def start(self):
        """Start background scheduler to periodically refresh weather cache."""
        if self._scheduler_task and not self._scheduler_task.done():
            log_info("[weather_plugin] Scheduler already running, skipping start")
            return

        self._scheduler_running = True
        # Ensure immediate fetch on start
        loop = None
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            self._scheduler_task = loop.create_task(self._weather_loop())
            log_info("[weather_plugin] Scheduler task started")
        else:
            # If no running loop, schedule task later when start() is invoked in that context
            log_debug(
                "[weather_plugin] No running loop to start scheduler; will start when event loop is available"
            )

    async def stop(self):
        """Stop background scheduler and cancel task."""
        self._scheduler_running = False
        if self._scheduler_task and not self._scheduler_task.done():
            self._scheduler_task.cancel()
            try:
                await self._scheduler_task
            except asyncio.CancelledError:
                pass
        self._scheduler_task = None
        log_info("[weather_plugin] Scheduler stopped")

    @staticmethod
    def _parse_report_time(report_time_str: Optional[str]) -> tuple[int, int]:
        """Parse an ``HH:MM`` daily-report time into a clamped ``(hour, minute)``.

        Defaults to ``(6, 0)`` and clamps to valid ranges on malformed input.
        """
        report_hour, report_minute = 6, 0
        if report_time_str:
            try:
                parts = report_time_str.split(":")
                if len(parts) >= 2:
                    report_hour = int(parts[0])
                    report_minute = int(parts[1])
                elif len(parts) == 1:
                    report_hour = int(parts[0])
            except Exception:
                pass
        report_hour = max(0, min(23, report_hour))
        report_minute = max(0, min(59, report_minute))
        return report_hour, report_minute

    async def _reconcile_weather_event(self) -> None:
        """Ensure the persistent daily-report event matches current config.

        Implements the clear+recreate / clear semantics:

        * When the daily report is enabled (or its time/interface/language
          changes), all pending weather events are deleted and a single new
          ``daily`` event is created at the next occurrence of the configured
          local time.
        * When the daily report is disabled, all pending weather events are
          deleted and none are recreated.

        No-ops when the configuration is unchanged since the last reconcile.
        """
        current = (
            bool(self.daily_report_enabled),
            self.daily_report_time or "06:00",
            (self.daily_report_interface or "").strip(),
            (self.daily_report_language or "").strip(),
        )
        if current == self._last_report_config:
            return

        from core.db import (
            delete_scheduled_events_by_created_by,
            insert_scheduled_event,
        )

        enabled, report_time, _interface, _language = current

        # Always clear existing weather events first (clear-then-recreate).
        deleted = await delete_scheduled_events_by_created_by(WEATHER_EVENT_CREATED_BY)
        log_info(
            f"[weather_plugin] Reconcile: cleared {deleted} pending weather event(s) "
            f"(enabled={enabled}, time={report_time})"
        )

        if enabled:
            report_hour, report_minute = self._parse_report_time(report_time)
            now_local = utc_to_local(datetime.utcnow())
            occurrence = now_local.replace(
                hour=report_hour, minute=report_minute, second=0, microsecond=0
            )
            # If today's time has already passed, schedule for tomorrow.
            if occurrence <= now_local:
                occurrence = occurrence + timedelta(days=1)

            date_str = occurrence.date().isoformat()
            time_str = f"{report_hour:02d}:{report_minute:02d}"
            await insert_scheduled_event(
                date=date_str,
                time=time_str,
                recurrence_type="daily",
                description="[weather_report] Daily weather update",
                created_by=WEATHER_EVENT_CREATED_BY,
            )
            log_info(
                f"[weather_plugin] Reconcile: created daily weather event at "
                f"{date_str} {time_str} local"
            )

        self._last_report_config = current

    async def _dispatch_due_weather_events(self) -> None:
        """Self-deliver any due weather events owned by this plugin.

        The generic event dispatcher hard-codes the telegram_bot interface, so
        the weather plugin dispatches its own events to honour the configured
        interface. After a successful delivery the event is marked delivered,
        which auto-reschedules the ``daily`` event to the next day.
        """
        from core.db import get_due_events_by_created_by, mark_event_delivered

        try:
            due = await get_due_events_by_created_by(WEATHER_EVENT_CREATED_BY)
        except Exception as e:
            log_warning(f"[weather_plugin] Failed to fetch due weather events: {e}")
            return

        for event in due:
            event_id = event.get("id")
            if await self._trigger_daily_report():
                if event_id is not None:
                    await mark_event_delivered(event_id)
                    log_info(
                        f"[weather_plugin] Delivered weather event {event_id}; "
                        "rescheduled for next day"
                    )
            else:
                log_warning(
                    f"[weather_plugin] Daily report delivery failed for event "
                    f"{event_id}; will retry next loop"
                )

    async def _weather_loop(self):
        """Background loop: refresh weather, reconcile config, dispatch due events."""
        log_info("[weather_plugin] Weather background loop started")
        # Reconcile once at startup so config changes made while the process was
        # down are applied immediately.
        try:
            await self._reconcile_weather_event()
        except Exception as e:
            log_warning(f"[weather_plugin] Initial reconcile failed: {e}")
        try:
            while self._scheduler_running:
                try:
                    # Defensive timeout: even if a fetch hangs for any reason,
                    # the loop must keep running so reconciliation and due-event
                    # dispatch below are always reached.
                    try:
                        await asyncio.wait_for(
                            self._ensure_weather(),
                            timeout=(WEATHER_FETCH_TIMEOUT_SECONDS + 5)
                            * MAX_WEATHER_FETCH_RETRIES,
                        )
                    except asyncio.TimeoutError:
                        log_warning(
                            "[weather_plugin] Weather fetch timed out; continuing loop"
                        )

                    # Apply any config changes (clear+recreate / clear).
                    await self._reconcile_weather_event()
                    # Deliver any due weather events (self-routed).
                    await self._dispatch_due_weather_events()
                except Exception as e:
                    log_warning(f"[weather_plugin] Error during scheduled update: {e}")
                await asyncio.sleep(60)
        finally:
            log_info("[weather_plugin] Weather background loop exiting")

    @staticmethod
    def _split_interface_target(raw: str) -> tuple[str, str | None]:
        """Split a configured interface value into (interface_name, explicit_path).

        The configured value may be either a bare interface name
        (e.g. ``telegram_bot``) or a full interface_path that already pins the
        recipient (e.g. ``telegram_bot/31321637``). The registry is keyed by the
        bare interface name, so the name (before the first ``/``) is used for the
        registry lookup, while the full value — when it contains a target — is
        returned as the explicit delivery path.
        """
        value = (raw or "").strip()
        if "/" in value:
            interface_name = value.split("/", 1)[0]
            return interface_name, value
        return value, None

    def _resolve_delivery_path(
        self, interface_name: str, explicit_path: str | None = None
    ) -> str | None:
        """Resolve the interface_path to deliver the weather report to.

        Ordered, data-driven, with no per-interface hardcoding:
        1. an explicit path already configured on the interface value
        2. the trainer configured for this interface (TRAINER_IDS)

        Returns ``None`` when neither resolves.
        """
        if explicit_path:
            return explicit_path
        from core.config import get_trainer_id

        trainer_id = get_trainer_id(interface_name)
        if trainer_id is not None:
            return f"{interface_name}/{trainer_id}"
        return None

    @staticmethod
    def _build_synthetic_message(interface_path: str, text: str) -> Optional[object]:
        """Build a synthetic message anchored to a real recipient.

        ``request_llm_delivery`` falls back to a mock message with
        ``chat_id = -1`` when no ``message`` is supplied, which routes the
        autonomous turn to ``<interface>/-1`` instead of the configured
        recipient. By constructing a message that already carries the resolved
        ``interface_path`` and the real ``chat_id`` (the trailing segment of the
        path), the downstream chain anchors the turn to the intended chat so the
        weather bulletin actually reaches the trainer.

        Returns ``None`` when the ``chat_id`` cannot be derived (in that case the
        caller falls back to the previous synthetic-message behaviour).
        """
        from types import SimpleNamespace

        chat_segment = interface_path.rsplit("/", 1)[-1].strip()
        if not chat_segment:
            return None
        # Telegram chat ids are integers; keep the raw string when it is not
        # numeric so non-Telegram interfaces still get a usable target.
        chat_id: object
        try:
            chat_id = int(chat_segment)
        except (TypeError, ValueError):
            chat_id = chat_segment

        mock_message = SimpleNamespace()
        mock_message.chat_id = chat_id
        mock_message.message_id = 0
        mock_message.text = text
        mock_message.interface_path = interface_path
        mock_message.date = datetime.utcnow()
        mock_message.from_user = SimpleNamespace(
            id=0, username="weather_plugin", full_name="WeatherReporter"
        )
        mock_message.chat = SimpleNamespace(id=chat_id, type="private")
        return mock_message

    async def _trigger_daily_report(self) -> bool:
        """Trigger a daily weather announcement via the LLM."""
        try:
            raw_interface = (self.daily_report_interface or "").strip()
            if not raw_interface:
                log_warning(
                    "[weather_plugin] Daily report interface not configured; skipping announcement"
                )
                return False

            interface_name, explicit_path = self._split_interface_target(raw_interface)

            try:
                from core.core_initializer import INTERFACE_REGISTRY

                interface = INTERFACE_REGISTRY.get(interface_name)
            except Exception as e:
                log_warning(
                    f"[weather_plugin] Failed to resolve interface registry: {e}"
                )
                interface = None

            if interface is None:
                log_warning(
                    f"[weather_plugin] Interface '{interface_name}' not available; skipping announcement"
                )
                return False

            if not self._cached_weather:
                await self._update_weather()

            from core.auto_response import request_llm_delivery

            language_hint = ""
            if self.daily_report_language:
                language_hint = f"Write the update in {self.daily_report_language}. "

            # Resolve the delivery target so the LLM is told exactly where to
            # send the message. Without an explicit delivery target and a message
            # action instruction, local models may store the bulletin (e.g. as a
            # diary entry) instead of sending it.
            interface_path = self._resolve_delivery_path(interface_name, explicit_path)
            delivery_hint = ""
            if interface_path is not None:
                delivery_hint = (
                    "The trainer asks you to send them this weather update as a "
                    f"message on interface_path '{interface_path}'. Emit a message "
                    "action addressed to that interface_path. "
                )
            else:
                log_warning(
                    f"[weather_plugin] No delivery target could be resolved for "
                    f"interface '{interface_name}'; delivery target cannot be pinned"
                )

            prompt = (
                f"It is {self.daily_report_time or '06:00'} local time. "
                f"{language_hint}{delivery_hint}Use the weather data provided in context "
                "to create a short, friendly daily weather update. Keep it concise and "
                f"professional. Weather data: {self._cached_weather}"
            )

            # Anchor the autonomous turn to the resolved recipient. Without an
            # explicit message, request_llm_delivery synthesises a mock message
            # with chat_id = -1, so the turn is routed to '<interface>/-1' and the
            # bulletin never reaches the trainer. Supplying a synthetic message
            # that carries the real interface_path/chat_id fixes the routing.
            synthetic_message = None
            if interface_path is not None:
                synthetic_message = self._build_synthetic_message(
                    interface_path, prompt
                )
                if synthetic_message is None:
                    log_warning(
                        f"[weather_plugin] Could not derive chat_id from "
                        f"interface_path '{interface_path}'; delivery may be misrouted"
                    )

            success = await request_llm_delivery(
                message=synthetic_message,
                interface=interface,
                context={
                    "input": {"type": "event_reminder", "text": prompt},
                    "weather_data": self._cached_weather,
                },
                reason="daily_weather_report",
            )

            if success:
                log_info("[weather_plugin] Daily weather announcement triggered")
                return True

            log_warning("[weather_plugin] Daily weather announcement failed")
            return False
        except Exception as e:
            log_error(f"[weather_plugin] Daily weather announcement error: {e}")
            return False

    async def _trigger_manual_report(self, interface_id: str) -> bool:
        """Trigger a weather report on demand via LLM."""
        try:
            interface_name, explicit_path = self._split_interface_target(interface_id)

            from core.core_initializer import INTERFACE_REGISTRY

            interface = INTERFACE_REGISTRY.get(interface_name)
            if interface is None:
                log_warning(
                    f"[weather_plugin] Interface '{interface_name}' not available for manual report"
                )
                return False

            if not self._cached_weather:
                await self._update_weather()

            from core.auto_response import request_llm_delivery

            # Pin the delivery target so the LLM emits a message action to the
            # right interface_path instead of storing the bulletin.
            interface_path = self._resolve_delivery_path(interface_name, explicit_path)
            delivery_hint = ""
            if interface_path is not None:
                delivery_hint = (
                    "The trainer asks you to send them this weather update as a "
                    f"message on interface_path '{interface_path}'. Emit a message "
                    "action addressed to that interface_path. "
                )
            else:
                log_warning(
                    f"[weather_plugin] No delivery target could be resolved for "
                    f"interface '{interface_name}'; delivery target cannot be pinned"
                )

            prompt = (
                "Manual weather report request. "
                f"{delivery_hint}Use the weather data in context to create a short, "
                "friendly update. Keep it concise. Weather data: "
                f"{self._cached_weather}"
            )

            # Anchor the turn to the resolved recipient (see _trigger_daily_report).
            synthetic_message = None
            if interface_path is not None:
                synthetic_message = self._build_synthetic_message(
                    interface_path, prompt
                )
                if synthetic_message is None:
                    log_warning(
                        f"[weather_plugin] Could not derive chat_id from "
                        f"interface_path '{interface_path}'; delivery may be misrouted"
                    )

            success = await request_llm_delivery(
                message=synthetic_message,
                interface=interface,
                context={
                    "input": {"type": "event_reminder", "text": prompt},
                    "weather_data": self._cached_weather,
                },
                reason="manual_weather_report",
            )

            if success:
                log_info("[weather_plugin] Manual weather announcement triggered")
                return True
            log_warning("[weather_plugin] Manual weather announcement failed")
            return False
        except Exception as e:
            log_error(f"[weather_plugin] Manual weather announcement error: {e}")
            return False

    @staticmethod
    def _choose_emoji(description: str) -> str:
        if not description:
            return "🌡️"
        desc = description.lower()
        if "thunder" in desc:
            return "⛈️"
        if "snow" in desc:
            return "❄️"
        if "rain" in desc:
            return "🌧️"
        if "fog" in desc or "mist" in desc:
            return "🌫️"
        if "cloud" in desc:
            return "☁️"
        if "sun" in desc or "clear" in desc:
            return "☀️"
        return "🌡️"

    def shutdown(self):
        """Shutdown the plugin's executor to avoid scheduling new futures after interpreter shutdown."""
        try:
            self._executor.shutdown(wait=False)
            log_debug("[weather_plugin] Executor shutdown invoked")
        except Exception:
            # Best-effort cleanup
            pass


PLUGIN_CLASS = WeatherPlugin
