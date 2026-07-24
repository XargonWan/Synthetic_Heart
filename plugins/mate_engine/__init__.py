"""Package shim for the `mate_engine` plugin.

The plugin's code lives in `plugins/mate_engine/mate_engine.py` (its own file so the plugin
loader's `rglob("*.py")` discovery picks it up), while this package re-exports
that module under the historical `plugins.mate_engine` import path. All names — public
and private — are preserved by rebinding this package to the submodule in
`sys.modules`.
"""

from plugins.mate_engine.mate_engine import *  # noqa: F401,F403
import sys as _sys
from plugins.mate_engine import mate_engine as _mod

_sys.modules[__name__] = _mod
