"""Package shim for the `grillo_temporal_reflection` Grillo plugin.

The plugin's code lives in `plugins/grillo/grillo_temporal_reflection/grillo_temporal_reflection.py` (its own file so the
plugin loader's `rglob("*.py")` discovery picks it up), while this package
re-exports that module under the historical `plugins.grillo.grillo_temporal_reflection` import path.
All names — public and private — are preserved by rebinding this package to
the submodule in `sys.modules`.
"""

from plugins.grillo.grillo_temporal_reflection.grillo_temporal_reflection import *  # noqa: F401,F403
import sys as _sys
from plugins.grillo.grillo_temporal_reflection import grillo_temporal_reflection as _mod

_sys.modules[__name__] = _mod
