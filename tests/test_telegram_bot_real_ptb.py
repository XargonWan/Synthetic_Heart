"""A stub ``telegram`` already in ``sys.modules`` must not reach ``start_bot()``.

``tests/__init__.py`` installs empty stand-ins for ``telegram``, ``telegram.error``
and ``telegram.ext`` so that guarded imports work without python-telegram-bot.
If anything pulls that package in before this interface is imported at runtime,
the module binds the stand-in ``ApplicationBuilder`` and ``start_bot()`` dies
much later with ``AttributeError: 'ApplicationBuilder' object has no attribute
'token'``, which says nothing about the cause.

The interface drops the stand-ins on import instead. Under pytest the stand-ins
are the point (the suite relies on them), so the purge is skipped there and this
check has to run in a subprocess.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

_PROBE = """
import importlib
import sys

import tests  # installs the stand-ins for telegram / telegram.ext

assert getattr(sys.modules.get("telegram.ext"), "__file__", None) is None, (
    "expected the stand-in module to be in place before the interface imports"
)

tb = importlib.import_module("interface.telegram_bot")

print("BUILDER_HAS_TOKEN", hasattr(tb.ApplicationBuilder, "token"))
print("BUILDER_MODULE", getattr(tb.ApplicationBuilder, "__module__", None))
print(
    "EXT_IS_REAL",
    getattr(sys.modules.get("telegram.ext"), "__file__", None) is not None,
)
"""


def test_a_stubbed_telegram_is_replaced_before_start_bot_uses_it() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert result.returncode == 0, result.stderr[-2000:]
    assert "BUILDER_HAS_TOKEN True" in result.stdout, result.stdout[-2000:]
    assert "BUILDER_MODULE telegram.ext" in result.stdout, result.stdout[-2000:]
    assert "EXT_IS_REAL True" in result.stdout, result.stdout[-2000:]
