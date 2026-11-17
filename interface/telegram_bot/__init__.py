"""Package shim for the ``telegram_bot`` interface.

The interface code lives in ``interface/telegram_bot/telegram_bot.py`` (its own
file so it can ship a companion ``icon.png`` and ``guide.md`` in the same
folder, mirroring the multi-file plugin layout). This package re-exports that
module under the historical ``interface.telegram_bot`` import path. All names —
public and private — are preserved by rebinding this package to the submodule
in ``sys.modules``.

The stand-in ``telegram`` package that ``tests/__init__.py`` installs is dropped
first: if anything imported it earlier in this process, the implementation
module would bind its empty ``ApplicationBuilder`` and ``start_bot()`` would die
with ``AttributeError: 'ApplicationBuilder' object has no attribute 'token'``.
"""

import sys as _sys

from interface.telegram_bot._ptb_guard import drop_stub_telegram as _drop_stub

_STUB_ORIGIN = _drop_stub()

import interface.telegram_bot.telegram_bot as _mod  # noqa: E402
from interface.telegram_bot.telegram_bot import *  # noqa: E402,F401,F403

if _STUB_ORIGIN is not None:
    _mod.log_warning(
        "[telegram_bot] Dropped a stub 'telegram' package already in sys.modules "
        f"({_STUB_ORIGIN}) and loaded the real python-telegram-bot instead. "
        "Something imported this repo's 'tests' package before this interface."
    )

_sys.modules[__name__] = _mod
