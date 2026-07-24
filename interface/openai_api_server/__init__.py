"""Package shim for the ``ollama_serve`` (OpenAI API Server) interface.

The interface code lives in
``interface/openai_api_server/openai_api_server.py`` (its own file so it can
ship a companion ``icon.png`` and ``guide.md`` in the same folder, mirroring the
multi-file plugin layout). This package re-exports that module under the
historical ``interface.openai_api_server`` import path. All names — public and
private — are preserved by rebinding this package to the submodule in
``sys.modules``.
"""

import sys as _sys
import interface.openai_api_server.openai_api_server as _mod
from interface.openai_api_server.openai_api_server import *  # noqa: E402,F401,F403

_sys.modules[__name__] = _mod
