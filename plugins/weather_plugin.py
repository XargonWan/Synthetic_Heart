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
        return ["static_inject", "trigger_weather_report"]

    def get_supported_actions(self):
        return {
            "static_inject": {
                "description": "Inject static contextual data into every prompt",
                "required_fields": [],
                "optional_fields": [],
            },
            "trigger_weather_report": {
                "description": "Manually trigger the 'Weathergirl' 6 AM style announcement immediately. EXECUTE SILENTLY: Do NOT output any conversational text or acknowledgment. Just trigger the event.",
                "required_fields": [],
                "optional_fields": ["interface_path"],
                "brief": "Trigger the weather announcement immediately for testing.",
                "source": "weather"
            }
        }

    async def execute_action(self, action: dict, context: dict, bot, original_message):
        action_type = action.get("type")
        payload = action.get("payload", {})
        
        if action_type == "trigger_weather_report":
            log_info("[weather_plugin] Manual trigger received for weather report")
            # Extract interface_path from payload if provided
            target = payload.get("interface_path")
            success = await self.trigger_announcement(manual=True, target=target)
            return {"status": "success" if success else "failed"}
        return {"error": "Unknown action"}

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

    async def trigger_announcement(self, manual: bool = False, target: str = None) -> bool:
        """Trigger the 'weathergirl' style announcement.
        
        Args:
            manual: If True, bypass time checks and force delivery.
            target: Optional interface_path to send the report to (required for manual triggers).
        """
        try:
            log_info(f"[weather_plugin] 🌦️ Initiating {'MANUAL ' if manual else ''}daily weather announcement")
            
            # Ensure we have data
            if not self._cached_weather:
                await self._update_weather()
                
            from core.auto_response import request_llm_delivery
            

            
            # Resolve interface to use for delivery
            delivery_interface = self
            context_message = None
            
            if target:
                # target format: "interface_name/chat_id"
                if "/" in target:
                    parts = target.split("/")
                    interface_name = parts[0]
                    
                    # 1. Resolve Interface Instance
                    try:
                        from core.core_initializer import INTERFACE_REGISTRY
                        if interface_name in INTERFACE_REGISTRY:
                            delivery_interface = INTERFACE_REGISTRY[interface_name]
                            log_info(f"[weather_plugin] Using resolved interface: {interface_name}")
                        else:
                             log_warning(f"[weather_plugin] Could not find interface '{interface_name}' in registry")
                    except Exception as e:
                        log_warning(f"[weather_plugin] Error resolving interface: {e}")

                    # 2. Create Context Message for Routing
                    try:
                        chat_id_str = parts[1]
                        # Handle potential thread_id
                        thread_id = parts[2] if len(parts) > 2 else None
                        
                        from types import SimpleNamespace
                        context_message = SimpleNamespace()
                        # Best effort conversion to int for chat_id, though string is often acceptable
                        try:
                            context_message.chat_id = int(chat_id_str)
                        except ValueError:
                            context_message.chat_id = chat_id_str
                            
                        context_message.message_id = 0
                        context_message.message_thread_id = int(thread_id) if thread_id else None
                        context_message.from_user = SimpleNamespace(id=0, username="weather_system", full_name="Weather System")
                        context_message.chat = SimpleNamespace(id=context_message.chat_id, type="private")
                        context_message.text = "System Weather Trigger"
                        
                        log_info(f"[weather_plugin] Created context message bound to chat {context_message.chat_id}")
                    except Exception as e:
                        log_warning(f"[weather_plugin] Failed to create context message: {e}")

            # Build target instruction JSON snippet
            target_json_field = f', "interface_path": "{target}"' if target else ''

            # Ask LLM to generate the announcement
            # NOTE: We only request message_telegram_bot because TelegramInterface.send_message
            # has built-in TTS that automatically generates combined voice+text messages.
            # Requesting tts_speak separately would cause duplicate TTS generation.
            context_note = "It is currently 6:00 AM." if not manual else "This is a MANUAL test trigger of the 6:00 AM segment."
            prompt = (
                f"{context_note} Access the weather data provided in the context. "
                f"Generate a cheerful, 'weathergirl' style morning announcement for the user. "
                f"Include current conditions, high/low temperatures for the day, and any warnings (rain/snow). "
                f"Be concise but energetic. Keep the message under 1024 characters so it fits in a voice message caption. "
                f"IMPORTANT: Do NOT use emojis - the message will be converted to spoken audio. "
                f"Current weather string: {self._cached_weather}\n\n"
                f"=== CRITICAL INSTRUCTIONS ===\n"
                f"1. You are acting as the Weathergirl system. Output ONLY valid JSON.\n"
                f"2. Do NOT output any conversational text, pleasantries, or acknowledgments.\n"
                f"3. Do NOT use emojis or special unicode symbols in the announcement text.\n"
                f"4. Respond ONLY with a JSON object: {{ \"actions\": [ ... ] }}.\n"
                f"5. Include ONLY this action in the 'actions' array:\n"
                f"   - {{ \"type\": \"message_telegram_bot\", \"payload\": {{ \"text\": \"...announcement...\"{target_json_field} }} }}\n"
                f"6. The Telegram interface will automatically generate TTS audio for your message.\n"
                f"7. NEVER include 'trigger_weather_report' or 'tts_speak' to avoid recursion/duplicates."
            )

            log_info(f"[weather_plugin] Requesting LLM delivery to interface: {delivery_interface.__class__.__name__}")
            
            # Trigger autonomous delivery
            success = await request_llm_delivery(
                interface=delivery_interface,
                message=context_message,
                context={
                    "input": {"type": "event_reminder", "text": prompt},
                    "weather_data": self._cached_weather
                },
                reason="daily_weather_forecast" if not manual else "manual_weather_trigger"
            )
            
            log_info(f"[weather_plugin] request_llm_delivery success: {success}")
            return success
            
            if success:
                log_info("[weather_plugin] Weather announcement triggered successfully")
                return True
            else:
                log_warning("[weather_plugin] Failed to trigger weather announcement")
                return False
                
        except Exception as e:
             log_error(f"[weather_plugin] Error in trigger_announcement: {e}")
             return False

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
            
            # ... rest of update logic unchanged ...
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
                            
                            success = await self.trigger_announcement()
                            
                            if success:
                                last_notification_date = today_str
                                
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
