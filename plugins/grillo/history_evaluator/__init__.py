"""Package shim for the `history_evaluator` Grillo plugin.

The plugin's code lives in `plugins/grillo/history_evaluator/history_evaluator.py` (its own file so the
plugin loader's `rglob("*.py")` discovery picks it up), while this package
re-exports that module under the historical `plugins.grillo.history_evaluator` import path.
All names — public and private — are preserved by rebinding this package to
the submodule in `sys.modules`.
"""

from plugins.grillo.history_evaluator.history_evaluator import *  # noqa: F401,F403
import sys as _sys
from plugins.grillo.history_evaluator import history_evaluator as _mod

_sys.modules[__name__] = _mod
