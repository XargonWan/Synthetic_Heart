import asyncio
import json
import time
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime
from typing import Optional
import concurrent.futures

from core.core_initializer import register_plugin
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.time_zone_utils import get_local_location, utc_to_local
from core.config_manager import config_registry
from core.variables_engine import register_exposed_var

# Injection priority for weather information
INJECTION_PRIORITY = 2  # High priority - weather is contextually important


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
    component="weather_plugin",
    tags=["plugin"],
)

register_exposed_var(
    "WEATHER_DAILY_REPORT_ENABLED",
    label="Daily Weather Report",
    default=False,
    value_type=bool,
    ui_type="boolean",
    description="Send a daily weather announcement at 06:00 local time",
    scope="plugins",
    component="weather_plugin",
    tags=["plugin"],
)

register_exposed_var(
    "WEATHER_DAILY_REPORT_INTERFACE",
    label="Daily Weather Report Interface",
    default="synth_webui",
    value_type=str,
    ui_type="text",
    description="Interface id used for the daily weather announcement",
    scope="plugins",
    component="weather_plugin",
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

        # Register configuration with config_registry
        self.fetch_minutes = config_registry.get_value(
            "WEATHER_FETCH_TIME",
            60,  # Default 60 minutes as requested
            label="Weather Fetch Interval",
            description="Minutes between weather data fetches",
            value_type=int,
            group="plugins",
            component="weather_plugin",
            advanced=False,
        )
        self.daily_report_enabled = config_registry.get_value(
            "WEATHER_DAILY_REPORT_ENABLED",
            False,
            label="Daily Weather Report",
            description="Send a daily weather announcement at 06:00 local time",
            value_type=bool,
            group="plugins",
            component="weather_plugin",
            advanced=False,
        )
        self.daily_report_interface = config_registry.get_value(
            "WEATHER_DAILY_REPORT_INTERFACE",
            "synth_webui",
            label="Daily Weather Report Interface",
            description="Interface id used for the daily weather announcement",
            value_type=str,
            group="plugins",
            component="weather_plugin",
            advanced=True,
        )

        # Add listener to update fetch_minutes when config changes
        def _update_fetch_minutes(value):
            try:
                self.fetch_minutes = int(value) if value is not None else 60
                log_info(
                    f"[weather_plugin] Fetch interval updated to {self.fetch_minutes} minutes"
                )
            except (ValueError, TypeError):
                log_warning(
                    f"[weather_plugin] Invalid WEATHER_FETCH_TIME value: {value}, using default 60"
                )
                self.fetch_minutes = 60

        config_registry.add_listener("WEATHER_FETCH_TIME", _update_fetch_minutes)

        def _update_daily_report_enabled(value):
            try:
                self.daily_report_enabled = bool(value)
                log_info(
                    f"[weather_plugin] Daily report enabled: {self.daily_report_enabled}"
                )
            except Exception:
                self.daily_report_enabled = False

        def _update_daily_report_interface(value):
            try:
                self.daily_report_interface = str(value) if value else ""
                log_info(
                    f"[weather_plugin] Daily report interface set to: {self.daily_report_interface or 'none'}"
                )
            except Exception:
                self.daily_report_interface = ""

        config_registry.add_listener(
            "WEATHER_DAILY_REPORT_ENABLED", _update_daily_report_enabled
        )
        config_registry.add_listener(
            "WEATHER_DAILY_REPORT_INTERFACE", _update_daily_report_interface
        )

        # Use a dedicated executor so we don't depend on the event loop's default executor
        # which may be shut down during interpreter shutdown.
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        # Background scheduler task (managed as singleton per process)
        self._scheduler_task = None
        self._scheduler_running = False
        self._last_daily_report_date = None

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
            if not target_iface and isinstance(target_path, str) and "/" in target_path:
                target_iface = target_path.split("/")[0]
            if not target_iface:
                log_warning(
                    "[weather_plugin] trigger_weather_report missing interface_id/interface_path"
                )
                return {
                    "status": "error",
                    "message": "interface_id or interface_path required",
                }

            success = await self._trigger_manual_report(target_iface)
            return {"status": "success" if success else "failed"}

        return {"error": "Unknown action"}

    async def get_static_injection(self) -> dict:
        """Get current weather for static injection. Returns cached value immediately."""
        # Check if update is needed based on fetch_minutes config
        now = time.time()
        timeout_sec = self.fetch_minutes * 60

        is_stale = not self._cached_weather or (now - self._last_fetch > timeout_sec)

        if is_stale:
            if self._update_task and not self._update_task.done():
                # Update already in progress, just return cached message
                pass
            else:
                log_debug(
                    "[weather_plugin] Weather data is stale or missing, triggering background update"
                )
                # Trigger background update without awaiting it
                asyncio.create_task(self._update_weather())

        return {
            "weather": self._cached_weather or "Weather data gathering in progress..."
        }

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

    async def _fetch_weather_data(self) -> None:
        """Actual worker method to fetch weather from wttr.in."""
        location = get_local_location()
        encoded = urllib.parse.quote(location)
        url = f"https://wttr.in/{encoded}?format=j1"
        log_info(f"[weather_plugin] Fetching weather for {location}")
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
                    self._executor, urllib.request.urlopen, url
                )
                data_bytes = await loop.run_in_executor(self._executor, response.read)
            except urllib.error.HTTPError as e:
                # HTTP errors (404, 500, etc.)
                log_warning(
                    f"[weather_plugin] HTTP error fetching weather: {e.code} {e.reason}"
                )
                self._cached_weather = (
                    f"{location}: ⚠️ Cannot reach weather service (HTTP {e.code})"
                )
                return
            except urllib.error.URLError as e:
                # Network/connection errors
                log_warning(
                    f"[weather_plugin] Network error fetching weather: {e.reason}"
                )
                self._cached_weather = (
                    f"{location}: ⚠️ Cannot reach weather service (connection failed)"
                )
                return
            except RuntimeError as e:
                # Executor or loop has been shutdown
                log_warning(f"[weather_plugin] Could not schedule weather read: {e}")
                self._cached_weather = (
                    f"{location}: ⚠️ Weather service temporarily unavailable"
                )
                return
            if not data_bytes:
                log_warning("[weather_plugin] Empty response from weather service")
                self._cached_weather = (
                    f"{location}: ⚠️ Weather service returned empty response"
                )
                return
            try:
                data = json.loads(data_bytes.decode())
            except json.JSONDecodeError as e:
                log_warning(f"[weather_plugin] Invalid JSON weather data: {e}")
                self._cached_weather = (
                    f"{location}: ⚠️ Weather service returned invalid data"
                )
                return
            cc = data.get("current_condition", [{}])[0]
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
                f"Pressure {pressure}hPa, Cloud cover {cloudcover}%)"
            )
            log_debug(f"[weather_plugin] Final weather string: {weather_string}")
            self._cached_weather = weather_string
            self._last_fetch = time.time()
            log_info(f"[weather_plugin] Weather updated: {self._cached_weather}")
        except Exception as e:
            log_warning(f"[weather_plugin] Failed to fetch weather: {e}")
            log_error("[weather_plugin] Weather update error", e)
            self._cached_weather = (
                f"{location}: ⚠️ Weather service error (please try again later)"
            )

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

    async def _weather_loop(self):
        """Background loop that updates weather periodically based on fetch_minutes."""
        log_info("[weather_plugin] Weather background loop started")
        try:
            while self._scheduler_running:
                try:
                    await self._ensure_weather()
                    if self.daily_report_enabled:
                        now_local = utc_to_local(datetime.utcnow())
                        today_key = now_local.date().isoformat()
                        if now_local.hour == 6 and now_local.minute == 0:
                            if self._last_daily_report_date != today_key:
                                if await self._trigger_daily_report():
                                    self._last_daily_report_date = today_key
                except Exception as e:
                    log_warning(f"[weather_plugin] Error during scheduled update: {e}")
                await asyncio.sleep(60)
        finally:
            log_info("[weather_plugin] Weather background loop exiting")

    async def _trigger_daily_report(self) -> bool:
        """Trigger a daily weather announcement via the LLM."""
        try:
            interface_id = (self.daily_report_interface or "").strip()
            if not interface_id:
                log_warning(
                    "[weather_plugin] Daily report interface not configured; skipping announcement"
                )
                return False

            try:
                from core.core_initializer import INTERFACE_REGISTRY

                interface = INTERFACE_REGISTRY.get(interface_id)
            except Exception as e:
                log_warning(
                    f"[weather_plugin] Failed to resolve interface registry: {e}"
                )
                interface = None

            if interface is None:
                log_warning(
                    f"[weather_plugin] Interface '{interface_id}' not available; skipping announcement"
                )
                return False

            if not self._cached_weather:
                await self._update_weather()

            from core.auto_response import request_llm_delivery

            prompt = (
                "It is 6:00 AM local time. Use the weather data provided in context to create "
                "a short, friendly daily weather update. Keep it concise and professional. "
                f"Weather data: {self._cached_weather}"
            )

            success = await request_llm_delivery(
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
            from core.core_initializer import INTERFACE_REGISTRY

            interface = INTERFACE_REGISTRY.get(interface_id)
            if interface is None:
                log_warning(
                    f"[weather_plugin] Interface '{interface_id}' not available for manual report"
                )
                return False

            if not self._cached_weather:
                await self._update_weather()

            from core.auto_response import request_llm_delivery

            prompt = (
                "Manual weather report request. Use the weather data in context to create a short, "
                "friendly update. Keep it concise. Weather data: "
                f"{self._cached_weather}"
            )

            success = await request_llm_delivery(
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
