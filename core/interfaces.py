# This module has been removed due to deprecation.
# The original logic was migrated to `core.action_parser` and related registries.
# Keeping this import-time stub helps catch any accidental imports during CI/tests so
# callers can migrate to the new APIs rather than silently continue using the old file.
raise ImportError("core.interfaces has been removed - use core.action_parser and core.interfaces_registry instead")
