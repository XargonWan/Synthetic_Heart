"""Package shim for the `ai_diary` plugin.

The plugin's code lives in `plugins/ai_diary/ai_diary.py` (its own file so the plugin
loader's `rglob("*.py")` discovery picks it up), while this package re-exports
that module under the historical `plugins.ai_diary` import path. All names — public
and private — are preserved by rebinding this package to the submodule in
`sys.modules`.
"""

from plugins.ai_diary.ai_diary import *  # noqa: F401,F403
import sys as _sys
from plugins.ai_diary import ai_diary as _mod

_sys.modules[__name__] = _mod
