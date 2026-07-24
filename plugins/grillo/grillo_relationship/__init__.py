"""Package shim for the `grillo_relationship` Grillo plugin.

The plugin's code lives in `plugins/grillo/grillo_relationship/grillo_relationship.py` (its own file so the
plugin loader's `rglob("*.py")` discovery picks it up), while this package
re-exports that module under the historical `plugins.grillo.grillo_relationship` import path.
All names — public and private — are preserved by rebinding this package to
the submodule in `sys.modules`.
"""

from plugins.grillo.grillo_relationship.grillo_relationship import *  # noqa: F401,F403
import sys as _sys
from plugins.grillo.grillo_relationship import grillo_relationship as _mod

_sys.modules[__name__] = _mod
