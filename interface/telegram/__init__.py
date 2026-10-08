"""Package shim for the ``telegram`` (user account) interface.

The implementation lives in ``interface/telegram/telegram.py`` next to its
``guide.md``/``icon.svg`` companions, mirroring ``interface/telegram_bot``. This
package re-exports that module under the ``interface.telegram`` import path by
rebinding itself to the submodule in ``sys.modules`` (so every name, public or
private, and ``monkeypatch.setattr`` targets behave as on the real module).

Unlike ``telegram_bot`` this interface has helper submodules (``_compat``,
``_login``, ``webui_login``), so the rebound module must keep ``__path__`` for
``interface.telegram.<submodule>`` imports to resolve.
"""

import importlib as _importlib
import sys as _sys
import interface.telegram.telegram as _mod
from interface.telegram.telegram import *  # noqa: E402,F401,F403

_mod.__path__ = globals()["__path__"]  # type: ignore[attr-defined]
# ``from interface.telegram import _compat`` resolves names against the rebound
# module and would otherwise re-import the file under the name
# ``interface.telegram.telegram._compat`` (a second, distinct module object).
for _sub in ("_compat", "_login", "webui_login"):
    setattr(_mod, _sub, _importlib.import_module(f"interface.telegram.{_sub}"))
_sys.modules[__name__] = _mod
