"""Package shim for the `weather_plugin` plugin.

The plugin's code lives in `plugins/weather_plugin/weather_plugin.py` (its own file so the plugin
loader's `rglob("*.py")` discovery picks it up), while this package re-exports
that module under the historical `plugins.weather_plugin` import path. All names — public
and private — are preserved by rebinding this package to the submodule in
`sys.modules`.
"""

from plugins.weather_plugin.weather_plugin import *  # noqa: F401,F403
import sys as _sys
from plugins.weather_plugin import weather_plugin as _mod

_sys.modules[__name__] = _mod
