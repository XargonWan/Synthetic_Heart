"""Package shim for the `gasmask` plugin.

The plugin's code lives in `plugins/gasmask/gasmask.py` (its own file so the plugin
loader's `rglob("*.py")` discovery picks it up), while this package re-exports
that module under the historical `plugins.gasmask` import path. All names — public
and private — are preserved by rebinding this package to the submodule in
`sys.modules`.
"""

from plugins.gasmask.gasmask import *  # noqa: F401,F403
import sys as _sys
from plugins.gasmask import gasmask as _mod

_sys.modules[__name__] = _mod
