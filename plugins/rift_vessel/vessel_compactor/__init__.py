"""Package shim for the `vessel_compactor` Rift Vessel plugin.

The plugin's code lives in
`plugins/rift_vessel/vessel_compactor/vessel_compactor.py` (its own file so the
plugin loader's `rglob("*.py")` discovery picks it up), while this package
re-exports that module under the historical
`plugins.rift_vessel.vessel_compactor` import path. All names — public and
private — are preserved by rebinding this package to the submodule in
`sys.modules`.
"""

from plugins.rift_vessel.vessel_compactor.vessel_compactor import *  # noqa: F401,F403
import sys as _sys
from plugins.rift_vessel.vessel_compactor import vessel_compactor as _mod

_sys.modules[__name__] = _mod
