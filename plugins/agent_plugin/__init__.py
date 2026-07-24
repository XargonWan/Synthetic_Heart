"""Package shim for the `agent_plugin` plugin.

The plugin's code lives in `plugins/agent_plugin/agent_plugin.py` (its own file so the plugin
loader's `rglob("*.py")` discovery picks it up), while this package re-exports
that module under the historical `plugins.agent_plugin` import path. All names — public
and private — are preserved by rebinding this package to the submodule in
`sys.modules`.
"""

from plugins.agent_plugin.agent_plugin import *  # noqa: F401,F403
import sys as _sys
from plugins.agent_plugin import agent_plugin as _mod

_sys.modules[__name__] = _mod
