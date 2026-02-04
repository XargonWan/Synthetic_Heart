"""
Compatibility wrapper for chat_archives module.

This module delegates to `core.chat_archives_db` to provide a DB-backed
implementation while keeping the same import path for legacy code.
"""

from core.chat_archives_db import (
    init_chat_archives_table,
    create_archive,
    list_archives,
    load_archive,
    delete_archive,
    rename_archive,
)

__all__ = [
    "init_chat_archives_table",
    "create_archive",
    "list_archives",
    "load_archive",
    "delete_archive",
    "rename_archive",
]
