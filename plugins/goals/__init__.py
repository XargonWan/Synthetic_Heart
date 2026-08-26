from plugins.goals.goals import *  # noqa: F401,F403
import sys as _sys
from plugins.goals import goals as _mod

_sys.modules[__name__] = _mod
