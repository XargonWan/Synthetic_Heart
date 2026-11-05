from plugins.home_assistant.home_assistant import *  # noqa: F401,F403
import sys as _sys
from plugins.home_assistant import home_assistant as _mod

_sys.modules[__name__] = _mod
