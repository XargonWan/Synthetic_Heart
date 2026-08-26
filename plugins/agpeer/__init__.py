from plugins.agpeer.agpeer import *  # noqa: F401,F403
import sys as _sys
from plugins.agpeer import agpeer as _mod

_sys.modules[__name__] = _mod
