import asyncio
import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error
from typing import Optional
import concurrent.futures

from core.core_initializer import core_initializer, register_plugin
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.time_zone_utils import get_local_location
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


class WeatherPlugin:
    """Plugin to provide weather information using wttr.in."""
    
    display_name = "Weather"

    def __init__(self):
        register_plugin("weather", self)
        log_info("[weather_plugin] Registered WeatherPlugin")
        self._cached_weather: Optional[str] = None
        self._last_fetch: float = 0.0
        
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
        
        # Add listener to update fetch_minutes when config changes
        def _update_fetch_minutes(value):
            try:
                self.fetch_minutes = int(value) if value is not None else 60
                log_info(f"[weather_plugin] Fetch interval updated to {self.fetch_minutes} minutes")
            except (ValueError, TypeError):
                log_warning(f"[weather_plugin] Invalid WEATHER_FETCH_TIME value: {value}, using default 60")
                self.fetch_minutes = 60
        
        config_registry.add_listener("WEATHER_FETCH_TIME", _update_fetch_minutes)
        
        # Use a dedicated executor so we don't depend on the event loop's default executor
        # which may be shut down during interpreter shutdown.
        self._executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        # Background scheduler task (managed as singleton per process)
        self._scheduler_task = None
        self._scheduler_running = False

    # Plugin action registration
    def get_supported_action_types(self):
        return ["static_inject"]

    def get_supported_actions(self):
        return {
            "static_inject": {
                "description": "Inject static contextual data into every prompt",
                "required_fields": [],
                "optional_fields": [],
            }
        }

    async def get_static_injection(self) -> dict:
        await self._ensure_weather()
        return {"weather": self._cached_weather or "Weather data unavailable."}

    async def _ensure_weather(self) -> None:
        now = time.time()
        if (
            not self._cached_weather
            or now - self._last_fetch > self.fetch_minutes * 60
        ):
            await self._update_weather()

    async def _update_weather(self) -> None:
        location = get_local_location()
        encoded = urllib.parse.quote(location)
        url = f"https://wttr.in/{encoded}?format=j1"
        log_info(f"[weather_plugin] Fetching weather for {location}")
        try:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                # Event loop is closed; skip update
                log_warning("[weather_plugin] Event loop closed; aborting weather update")
                self._cached_weather = f"{location}: ⚠️ Weather service temporarily unavailable (system shutting down)"
                return

            try:
                response = await loop.run_in_executor(self._executor, urllib.request.urlopen, url)
                data_bytes = await loop.run_in_executor(self._executor, response.read)
            except urllib.error.HTTPError as e:
                # HTTP errors (404, 500, etc.)
                log_warning(f"[weather_plugin] HTTP error fetching weather: {e.code} {e.reason}")
                self._cached_weather = f"{location}: ⚠️ Cannot reach weather service (HTTP {e.code})"
                return
            except urllib.error.URLError as e:
                # Network/connection errors
                log_warning(f"[weather_plugin] Network error fetching weather: {e.reason}")
                self._cached_weather = f"{location}: ⚠️ Cannot reach weather service (connection failed)"
                return
            except RuntimeError as e:
                # Executor or loop has been shutdown
                log_warning(f"[weather_plugin] Could not schedule weather read: {e}")
                self._cached_weather = f"{location}: ⚠️ Weather service temporarily unavailable"
                return
            if not data_bytes:
                log_warning("[weather_plugin] Empty response from weather service")
                self._cached_weather = f"{location}: ⚠️ Weather service returned empty response"
                return
            try:
                data = json.loads(data_bytes.decode())
            except json.JSONDecodeError as e:
                log_warning(f"[weather_plugin] Invalid JSON weather data: {e}")
                self._cached_weather = f"{location}: ⚠️ Weather service returned invalid data"
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
                "[weather_plugin] Parsed values: desc=%s temp=%s feels=%s humidity=%s wind=%s%s cloud=%s visibility=%s pressure=%s"%
                (desc, temp_c, feels_c, humidity, wind_speed, wind_dir, cloudcover, visibility, pressure)
            )

            # Extract daily forecast for High/Low
            if "weather" in data and len(data["weather"]) > 0:
                today = data["weather"][0]
                max_temp = today.get("maxtempC", "N/A")
                min_temp = today.get("mintempC", "N/A")
            else:
                max_temp, min_temp = "N/A", "N/A"

            emoji = self._choose_emoji(desc)
            weather_string = (
                f"{location}: {emoji} {desc} +{temp_c}°C (High {max_temp}°C/Low {min_temp}°C, "
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
            self._cached_weather = f"{location}: ⚠️ Weather service error (please try again later)"

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
            log_debug("[weather_plugin] No running loop to start scheduler; will start when event loop is available")

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
        """Background loop checks for scheduled notifications and updates weather.
        
        Checks every minute:
        1. If it's time to refresh weather data (based on fetch_minutes)
        2. If it's 6:00 AM local time -> triggers weathergirl announcement
        """
        log_info("[weather_plugin] Weather background loop started")
        
        from core.time_zone_utils import utc_to_local
        from core.auto_response import request_llm_delivery
        from datetime import datetime
        
        last_notification_date = None

        try:
            while self._scheduler_running:
                # 1. Weather Update Check
                now = time.time()
                if not self._cached_weather or (now - self._last_fetch > self.fetch_minutes * 60):
                    try:
                        await self._update_weather()
                    except Exception as e:
                        log_warning(f"[weather_plugin] Error during scheduled update: {e}")

                # 2. Daily Announcement Check (6:00 AM)
                try:
                    local_dt = utc_to_local(datetime.utcnow())
                    today_str = local_dt.strftime("%Y-%m-%d")
                    
                    # Check if it is 06:00 and we haven't notified today
                    if local_dt.hour == 6 and local_dt.minute == 0:
                        if last_notification_date != today_str:
                            log_info(f"[weather_plugin] 🌦️ Initiating daily 6:00 AM weather announcement for {today_str}")
                            
                            # Ensure we have data
                            if not self._cached_weather:
                                await self._update_weather()
                                
                            # Ask LLM to generate the announcement
                            # We use request_llm_delivery to have the LLM "speak" the announcement
                            prompt = (
                                f"It is currently 6:00 AM. Access the weather data provided in the context context. "
                                f"Generate a cheerful, 'weathergirl' style morning announcement for the user. "
                                f"Include current conditions, high/low temperatures for the day, and any warnings (rain/snow). "
                                f"Be concise but energetic. "
                                f"Current weather string: {self._cached_weather}"
                            )
                            
                            # Trigger autonomous delivery
                            # Using 'event_reminder' type ensures it bypasses some filters and gets priority
                            # The recipient will be determined by the auto_response system (LogChat/Trainer)
                            success = await request_llm_delivery(
                                interface=self,
                                context={
                                    "input": {"type": "event_reminder", "text": prompt},
                                    "weather_data": self._cached_weather
                                },
                                reason="daily_weather_forecast"
                            )
                            
                            if success:
                                last_notification_date = today_str
                                log_info("[weather_plugin] Daily weather announcement triggered successfully")
                            else:
                                log_warning("[weather_plugin] Failed to trigger daily weather announcement")
                                
                except Exception as e:
                     log_error(f"[weather_plugin] Error in daily scheduler check: {e}")

                # Sleep for 60 seconds to check again
                await asyncio.sleep(60)
        finally:
            log_info("[weather_plugin] Weather background loop exiting")

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
