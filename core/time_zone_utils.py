from zoneinfo import ZoneInfo, available_timezones
import os
from datetime import datetime

from core.logging_utils import log_warning
from core.config_manager import config_registry

# Get list of available timezones for dropdown
_AVAILABLE_TIMEZONES = sorted(available_timezones())

# Register timezone configuration
_TZ = config_registry.get_var(
    "TZ",
    "UTC",
    label="Timezone",
    description="Timezone for scheduled events and time display (e.g., 'Asia/Tokyo', 'Europe/Rome', 'America/New_York')",
    group="core",
    component="core",
    constraints={"choices": _AVAILABLE_TIMEZONES},
)

# Register location configuration
_PROMPT_LOCATION = config_registry.get_var(
    "PROMPT_LOCATION",
    "",
    label="Default Location",
    description="Default location for prompts and plugins (e.g., 'Kyoto,Japan', 'Rome,Italy')",
    group="core",
    component="core",
)


def get_local_timezone() -> ZoneInfo:
    """Return the local timezone defined by the TZ config variable or UTC.

    Logs a warning and falls back to UTC if the variable is missing or
    points to an invalid timezone.
    """
    tz_name = str(_TZ)
    env_tz = os.getenv("TZ")
    if (not tz_name or tz_name == "UTC") and env_tz:
        tz_name = env_tz
    tz_name = tz_name or "UTC"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        log_warning(f"Invalid TZ '{tz_name}', falling back to UTC")
        return ZoneInfo("UTC")


def utc_to_local(dt: datetime) -> datetime:
    """Convert a UTC datetime to local time using the local TZ."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(get_local_timezone())


def parse_local_to_utc(date_str: str, time_str: str) -> datetime:
    """Parse local date and time strings and return a UTC datetime."""
    local_tz = get_local_timezone()
    dt_local = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
    return dt_local.replace(tzinfo=local_tz).astimezone(ZoneInfo("UTC"))


def format_dual_time(dt_utc: datetime) -> str:
    """Return formatted time in local timezone with UTC in parentheses."""
    dt_local = utc_to_local(dt_utc)
    return f"{dt_local.strftime('%H:%M %Z')} ({dt_utc.strftime('%H:%M UTC')})"


def get_local_location() -> str:
    """Return a human-readable location using a dedicated configuration variable.

    The location is primarily sourced from the PROMPT_LOCATION configuration
    variable. If it is not set, a best-effort location name is derived from the
    TZ configuration variable. This keeps timezone and location as separate
    configuration options while providing a sensible fallback.
    """

    location = str(_PROMPT_LOCATION)
    if location:
        return location

    tz_name = str(_TZ) or "UTC"
    # Typically in the form Region/City; use the last part as location
    if "/" in tz_name:
        location = tz_name.split("/")[-1]
    else:
        location = tz_name
    return location.replace("_", " ")


def get_suggested_locations() -> list:
    """Return a list of suggested locations derived from timezone names.
    
    Extracts city names from timezone identifiers (e.g., 'Asia/Tokyo' -> 'Tokyo')
    and formats them as 'City,Country' pairs. Filters out special timezone identifiers
    like 'Etc/GMT' that don't represent real locations.
    """
    locations = set()
    
    # List of prefixes to skip (these are not real locations)
    skip_prefixes = ('Etc', 'GMT', 'SystemV', 'US', 'MST', 'HST', 'EST', 'CST', 'PST')
    
    for tz_name in _AVAILABLE_TIMEZONES:
        # Skip special timezone groups
        if any(tz_name.startswith(prefix) for prefix in skip_prefixes):
            continue
            
        if "/" in tz_name:
            # Extract city/area from timezone (last part)
            city_part = tz_name.split("/")[-1].replace("_", " ")
            # Extract country/region from timezone (first part)
            country_part = tz_name.split("/")[0].replace("_", " ")
            
            # Skip if city or country contains numbers (like GMT+0, GMT+1, etc.)
            if any(c.isdigit() for c in city_part) or any(c.isdigit() for c in country_part):
                continue
            
            # Format as "City,Country"
            location = f"{city_part},{country_part}"
            locations.add(location)
    
    return sorted(list(locations))
