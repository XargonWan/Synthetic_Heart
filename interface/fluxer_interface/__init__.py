"""Package shim for the ``fluxer_bot`` interface.

The interface code lives in ``interface/fluxer_interface/fluxer_interface.py``
(its own file so it can ship a companion ``icon.png`` and ``guide.md`` in the
same folder, mirroring the multi-file plugin layout). This package re-exports
that module under the historical ``interface.fluxer_interface`` import path. All
names — public and private — are preserved by rebinding this package to the
submodule in ``sys.modules``.
"""

import sys as _sys
import interface.fluxer_interface.fluxer_interface as _mod
from interface.fluxer_interface.fluxer_interface import *  # noqa: E402,F401,F403

_sys.modules[__name__] = _mod
