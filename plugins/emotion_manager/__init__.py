"""Package shim for the `emotion_manager` plugin.

The plugin's code lives in `plugins/emotion_manager/emotion_manager.py` (its own file so the plugin
loader's `rglob("*.py")` discovery picks it up), while this package re-exports
that module under the historical `plugins.emotion_manager` import path. All names — public
and private — are preserved by rebinding this package to the submodule in
`sys.modules`.
"""

from plugins.emotion_manager.emotion_manager import *  # noqa: F401,F403
import sys as _sys
from plugins.emotion_manager import emotion_manager as _mod

_sys.modules[__name__] = _mod
