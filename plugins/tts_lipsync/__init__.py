"""Package shim for the `tts_lipsync` plugin.

The plugin's code lives in `plugins/tts_lipsync/tts_lipsync.py` (its own file so the plugin
loader's `rglob("*.py")` discovery picks it up), while this package re-exports
that module under the historical `plugins.tts_lipsync` import path. All names — public
and private — are preserved by rebinding this package to the submodule in
`sys.modules`.
"""

from plugins.tts_lipsync.tts_lipsync import *  # noqa: F401,F403
import sys as _sys
from plugins.tts_lipsync import tts_lipsync as _mod

_sys.modules[__name__] = _mod
