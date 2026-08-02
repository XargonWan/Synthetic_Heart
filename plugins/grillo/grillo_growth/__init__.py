"""Package shim for the `grillo_growth` Grillo plugin.

The plugin's code lives in `plugins/grillo/grillo_growth/grillo_growth.py` (its own file so the
plugin loader's `rglob("*.py")` discovery picks it up), while this package
re-exports that module under the historical `plugins.grillo.grillo_growth` import path.
All names — public and private — are preserved by rebinding this package to
the submodule in `sys.modules`.
"""

from plugins.grillo.grillo_growth.grillo_growth import *  # noqa: F401,F403
import sys as _sys
from plugins.grillo.grillo_growth import grillo_growth as _mod

_sys.modules[__name__] = _mod
