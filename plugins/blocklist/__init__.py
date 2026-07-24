"""Package shim for the `blocklist` plugin.

The plugin's code lives in `plugins/blocklist/blocklist.py` (its own file so the plugin
loader's `rglob("*.py")` discovery picks it up), while this package re-exports
that module under the historical `plugins.blocklist` import path. All names — public
and private — are preserved by rebinding this package to the submodule in
`sys.modules`.
"""

from plugins.blocklist.blocklist import *  # noqa: F401,F403
import sys as _sys
from plugins.blocklist import blocklist as _mod

_sys.modules[__name__] = _mod
