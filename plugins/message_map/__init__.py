"""Package shim for the `message_map` plugin.

The plugin's code lives in `plugins/message_map/message_map.py` (its own file so the plugin
loader's `rglob("*.py")` discovery picks it up), while this package re-exports
that module under the historical `plugins.message_map` import path. All names — public
and private — are preserved by rebinding this package to the submodule in
`sys.modules`.
"""

from plugins.message_map.message_map import *  # noqa: F401,F403
import sys as _sys
from plugins.message_map import message_map as _mod

_sys.modules[__name__] = _mod
