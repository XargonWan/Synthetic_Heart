"""Package shim for the `recent_chats` plugin.

The plugin's code lives in `plugins/recent_chats/recent_chats.py` (its own file so the plugin
loader's `rglob("*.py")` discovery picks it up), while this package re-exports
that module under the historical `plugins.recent_chats` import path. All names — public
and private — are preserved by rebinding this package to the submodule in
`sys.modules`.
"""

from plugins.recent_chats.recent_chats import *  # noqa: F401,F403
import sys as _sys
from plugins.recent_chats import recent_chats as _mod

_sys.modules[__name__] = _mod
