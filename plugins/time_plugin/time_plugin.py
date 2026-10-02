from datetime import datetime

from core.time_zone_utils import (
    get_local_timezone,
    get_local_location,
)
from core.core_initializer import register_plugin


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
        # The prompt carries the household's own clock and nothing else. The
        # dual local+UTC rendering (``format_dual_time``) is right where an
        # operator compares two clocks, but in a prompt it hands the model a
        # second clock and a zone name it can quote back at the user, and until
        # an environment plugin publishes the house timezone it reads
        # "21:05 UTC (21:05 UTC)". Bare local ``HH:MM`` is what the Reality
        # Anchor renders as "10:52 PM".
        tz = get_local_timezone()
        now_local = datetime.now(tz)
        return {
            "location": get_local_location(),
            "date": now_local.strftime("%Y-%m-%d"),
            "time": now_local.strftime("%H:%M"),
        }


PLUGIN_CLASS = TimePlugin
