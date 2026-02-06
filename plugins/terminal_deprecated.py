# Terminal plugin removed (deprecated)
# This file previously implemented a legacy terminal wrapper. It has been
# removed in favor of the Agent plugin's internal executor and audit logging.
# If any code imports this module, fail fast so callers can be migrated.
raise ImportError("plugins.terminal_deprecated removed: use Agent plugin execution APIs instead")
