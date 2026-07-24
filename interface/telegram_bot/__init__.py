"""Package shim for the ``telegram_bot`` interface.

The interface code lives in ``interface/telegram_bot/telegram_bot.py`` (its own
file so it can ship a companion ``icon.png`` and ``guide.md`` in the same
folder, mirroring the multi-file plugin layout). This package re-exports that
module under the historical ``interface.telegram_bot`` import path. All names —
public and private — are preserved by rebinding this package to the submodule
in ``sys.modules``.
"""

import sys as _sys
import interface.telegram_bot.telegram_bot as _mod
from interface.telegram_bot.telegram_bot import *  # noqa: E402,F401,F403

_sys.modules[__name__] = _mod
