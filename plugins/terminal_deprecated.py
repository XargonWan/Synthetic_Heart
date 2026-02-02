# plugins/terminal_deprecated.py
# Deprecated wrapper for the previous Terminal plugin. Kept here for reference during the Agent PoC branch.
# The Agent plugin provides an internal executor and policy controls; remove this file in a follow-up cleanup if desired.

from core.logging_utils import log_warning

log_warning("[terminal_deprecated] The terminal plugin has been deprecated in favor of the Agent plugin's internal executor. Remove in cleanup.")
