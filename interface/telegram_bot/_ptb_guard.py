"""Refuse to bind the telegram_bot interface to a stubbed ``telegram`` package.

``tests/__init__.py`` registers empty stand-ins for ``telegram``,
``telegram.error`` and ``telegram.ext`` so guarded imports work without
python-telegram-bot installed. Those stand-ins live in ``sys.modules`` for the
whole process once anything imports that package.

Importing this repository's ``tests`` package at runtime therefore leaves
``telegram.ext.ApplicationBuilder`` as a bare class, and the interface only
notices much later, inside ``start_bot()``, where the first chained call fails
with ``AttributeError: 'ApplicationBuilder' object has no attribute 'token'``.
That message names neither the stand-in nor the import that installed it.

The interface package (``interface/telegram_bot/__init__.py``) calls
:func:`drop_stub_telegram` before it imports the implementation module, so the
real ``python-telegram-bot`` is what the module binds. Under pytest the stand-ins
are the point (the suite relies on them), so nothing is dropped there.
"""

from __future__ import annotations

import sys

#: Modules that must come from the real python-telegram-bot package.
_TELEGRAM_MODULES = ("telegram.ext", "telegram.error", "telegram")


def drop_stub_telegram() -> str | None:
    """Drop stubbed ``telegram`` modules that are already cached.

    Returns the origin of the dropped ``telegram.ext`` when a stand-in was
    removed, or ``None`` when the real package was in place, absent (so the
    normal import below loads the real one), or when running under pytest.
    """
    ext = sys.modules.get("telegram.ext")
    builder = getattr(ext, "ApplicationBuilder", None)
    if builder is None or hasattr(builder, "token"):
        return None

    if "pytest" in sys.modules:
        # The stand-ins are deliberate here; the suite imports them on purpose.
        return None

    origin = getattr(ext, "__file__", None) or "no __file__ (stub module)"
    for name in _TELEGRAM_MODULES:
        sys.modules.pop(name, None)
    return origin
