"""Package shim for the ``discord_bot`` interface.

The interface code lives in ``interface/discord_interface/discord_interface.py``
(its own file so it can ship a companion ``icon.png`` and ``guide.md`` in the
same folder, mirroring the multi-file plugin layout). This package re-exports
that module under the historical ``interface.discord_interface`` import path.
All names — public and private — are preserved by rebinding this package to the
submodule in ``sys.modules``.
"""

import sys as _sys
import interface.discord_interface.discord_interface as _mod
from interface.discord_interface.discord_interface import *  # noqa: E402,F401,F403

_sys.modules[__name__] = _mod
