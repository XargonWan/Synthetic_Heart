from datetime import datetime, timezone

from core.time_zone_utils import get_local_timezone, format_dual_time, get_local_location
from core.core_initializer import core_initializer, register_plugin


class TimePlugin:
    """Plugin that injects current date, time, and location."""
    
    display_name = "Time & Location"

    def __init__(self):
        register_plugin("time", self)

    def get_supported_action_types(self):
        return ["static_inject"]

    def get_supported_actions(self):
        return {
            "static_inject": {
                "description": "Inject current date, time, and location into the prompt context",
                "required_fields": [],
                "optional_fields": [],
            }
        }

    def get_static_injection(self) -> dict:
        tz = get_local_timezone()
        now_local = datetime.now(tz)
        now_utc = now_local.astimezone(timezone.utc)
        return {
            "location": get_local_location(),
            "date": now_local.strftime("%Y-%m-%d"),
            "time": format_dual_time(now_utc),
        }


PLUGIN_CLASS = TimePlugin
