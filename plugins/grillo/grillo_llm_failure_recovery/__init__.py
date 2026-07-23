"""Package shim for the `grillo_llm_failure_recovery` Grillo plugin.

The plugin's code lives in `plugins/grillo/grillo_llm_failure_recovery/grillo_llm_failure_recovery.py` (its own file so the
plugin loader's `rglob("*.py")` discovery picks it up), while this package
re-exports that module under the historical `plugins.grillo.grillo_llm_failure_recovery` import path.
All names — public and private — are preserved by rebinding this package to
the submodule in `sys.modules`.
"""

from plugins.grillo.grillo_llm_failure_recovery.grillo_llm_failure_recovery import *  # noqa: F401,F403
import sys as _sys
from plugins.grillo.grillo_llm_failure_recovery import (
    grillo_llm_failure_recovery as _mod,
)

_sys.modules[__name__] = _mod
