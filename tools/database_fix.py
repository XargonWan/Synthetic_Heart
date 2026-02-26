#!/usr/bin/env python3
"""
create_missing_tables.py — Create all missing tables AND columns in the `synth` database.

Usage:
    python tools/create_missing_tables.py

Behaviour:
    1. Reads DB credentials from the project `.env` file.
    2. Connects to the `synth` database.
    3. Lists existing tables via SHOW TABLES.
    4. For every expected table that is NOT present, executes the
       corresponding CREATE TABLE IF NOT EXISTS statement.
    5. For every expected table that IS present, checks for missing
       columns via SHOW COLUMNS and adds them with ALTER TABLE ADD COLUMN.
    6. Reports what was created/added and what was already present.

Safety:
    - Uses CREATE TABLE IF NOT EXISTS — will never overwrite or alter
      existing tables.
    - Uses ALTER TABLE ADD COLUMN — will never modify existing columns,
      only add missing ones.
    - Table schemas are sourced from the codebase's Python definitions
      (authoritative), with synth-db.sql as fallback for tables only
      defined there (grillo_beats, settings).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Locate the .env file (one level up from tools/)
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _SCRIPT_DIR.parent
_ENV_FILE = _PROJECT_ROOT / ".env"


def _load_dotenv(env_path: Path) -> dict[str, str]:
    """Minimal .env loader — no external dependencies required."""
    env_vars: dict[str, str] = {}
    if not env_path.is_file():
        return env_vars
    with open(env_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            # Strip optional surrounding quotes
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            env_vars[key] = value
    return env_vars


# ---------------------------------------------------------------------------
# Table definitions — CREATE TABLE IF NOT EXISTS
# ---------------------------------------------------------------------------
# Each entry is (table_name, sql_statement).
# The order matters only for readability; all statements are idempotent.
TABLE_DEFINITIONS: list[tuple[str, str]] = [
    # ── core tables ──────────────────────────────────────────────
    (
        "config",
        """
        CREATE TABLE IF NOT EXISTS config (
            `config_key` VARCHAR(255) PRIMARY KEY,
            `value` TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    (
        "chat_history_cache",
        """
        CREATE TABLE IF NOT EXISTS chat_history_cache (
            id INT AUTO_INCREMENT PRIMARY KEY,
            interface_path VARCHAR(512) NOT NULL,
            sender_name VARCHAR(255),
            sender_id VARCHAR(255),
            message_text LONGTEXT NOT NULL,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_interface_path (interface_path),
            INDEX idx_timestamp (timestamp),
            UNIQUE KEY uniq_message (interface_path, timestamp)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    (
        "chat_session_meta",
        """
        CREATE TABLE IF NOT EXISTS chat_session_meta (
            interface_path VARCHAR(512) PRIMARY KEY,
            meta LONGTEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_updated_at (updated_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    (
        "chat_archives",
        """
        CREATE TABLE IF NOT EXISTS chat_archives (
            id VARCHAR(64) PRIMARY KEY,
            session_id VARCHAR(255) DEFAULT NULL,
            name VARCHAR(255) DEFAULT NULL,
            messages LONGTEXT NOT NULL,
            metadata LONGTEXT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_session (session_id),
            INDEX idx_created_at (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    # ── plugin tables ────────────────────────────────────────────
    (
        "ai_diary",
        """
        CREATE TABLE IF NOT EXISTS ai_diary (
            id INT AUTO_INCREMENT PRIMARY KEY,
            content TEXT NOT NULL COMMENT 'What synth said/did in the interaction',
            personal_thought TEXT COMMENT 'synth personal reflection about the interaction',
            emotions TEXT DEFAULT '[]' COMMENT 'synth emotions about this interaction',
            interaction_summary TEXT COMMENT 'Brief summary of what happened',
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            interface VARCHAR(50),
            chat_id VARCHAR(255),
            thread_id VARCHAR(255),
            user_message TEXT COMMENT 'What the user said that triggered this response',
            context_tags TEXT DEFAULT '[]' COMMENT 'Tags about the context/topic',
            involved_users TEXT DEFAULT '[]' COMMENT 'JSON list of users involved in the interaction',
            INDEX idx_timestamp (timestamp),
            INDEX idx_interface_chat (interface, chat_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    (
        "ai_diary_archive",
        """
        CREATE TABLE IF NOT EXISTS ai_diary_archive (
            id INT AUTO_INCREMENT PRIMARY KEY,
            content TEXT NOT NULL COMMENT 'What synth said/did in the interaction',
            personal_thought TEXT COMMENT 'synth personal reflection about the interaction',
            emotions TEXT DEFAULT '[]' COMMENT 'synth emotions about this interaction',
            interaction_summary TEXT COMMENT 'Brief summary of what happened',
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            interface VARCHAR(50),
            chat_id VARCHAR(255),
            thread_id VARCHAR(255),
            user_message TEXT COMMENT 'What the user said that triggered this response',
            context_tags TEXT DEFAULT '[]' COMMENT 'Tags about the context/topic',
            involved_users TEXT DEFAULT '[]' COMMENT 'JSON list of users involved in the interaction',
            INDEX idx_timestamp (timestamp)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    (
        "emotion_state",
        """
        CREATE TABLE IF NOT EXISTS emotion_state (
            id INT AUTO_INCREMENT PRIMARY KEY,
            emotion_name VARCHAR(100) NOT NULL,
            intensity FLOAT NOT NULL DEFAULT 5.0,
            timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_emotion_name (emotion_name),
            INDEX idx_timestamp (timestamp)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    (
        "emotion_diary",
        """
        CREATE TABLE IF NOT EXISTS emotion_diary (
            id INT AUTO_INCREMENT PRIMARY KEY,
            source VARCHAR(100),
            event VARCHAR(100),
            emotion VARCHAR(100),
            intensity FLOAT,
            state VARCHAR(100),
            trigger_condition VARCHAR(255),
            decision_logic TEXT,
            next_check DATETIME,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_timestamp (timestamp)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    (
        "memories",
        """
        CREATE TABLE IF NOT EXISTS memories (
            id INT AUTO_INCREMENT PRIMARY KEY,
            timestamp DATETIME NOT NULL,
            content TEXT NOT NULL,
            author VARCHAR(100),
            source VARCHAR(100),
            tags TEXT,
            scope VARCHAR(50),
            emotion VARCHAR(50),
            intensity INT,
            emotion_state VARCHAR(50)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    (
        "bio",
        """
        CREATE TABLE IF NOT EXISTS bio (
            id VARCHAR(255) PRIMARY KEY,
            known_as TEXT DEFAULT '[]',
            likes TEXT DEFAULT '[]',
            not_likes TEXT DEFAULT '[]',
            information TEXT DEFAULT '',
            past_events TEXT DEFAULT '[]',
            feelings TEXT DEFAULT '[]',
            contacts TEXT DEFAULT '{}',
            social_accounts TEXT DEFAULT '[]',
            privacy TEXT DEFAULT 'default',
            created_at VARCHAR(50),
            last_accessed VARCHAR(50),
            last_update TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            update_count INT DEFAULT 0,
            user_name VARCHAR(255)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    (
        "scheduled_events",
        """
        CREATE TABLE IF NOT EXISTS scheduled_events (
            id INT AUTO_INCREMENT PRIMARY KEY,
            `date` DATE NOT NULL,
            `time` TIME DEFAULT '00:00',
            recurrence_type VARCHAR(20) DEFAULT 'none',
            next_run DATETIME NOT NULL,
            description TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            delivered BOOLEAN DEFAULT 0,
            created_by VARCHAR(100) DEFAULT 'synth'
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    (
        "chatlink",
        """
        CREATE TABLE IF NOT EXISTS chatlink (
            int_id INT AUTO_INCREMENT PRIMARY KEY,
            interface VARCHAR(32) NOT NULL,
            chat_id TEXT NOT NULL,
            thread_id TEXT DEFAULT NULL,
            chat_name TEXT DEFAULT NULL,
            message_thread_name TEXT DEFAULT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            UNIQUE KEY unique_chat (interface, chat_id(255))
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    (
        "message_map",
        """
        CREATE TABLE IF NOT EXISTS message_map (
            trainer_message_id INTEGER PRIMARY KEY,
            chat_id BIGINT NOT NULL,
            message_id INTEGER NOT NULL,
            timestamp DOUBLE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    (
        "blocklist",
        """
        CREATE TABLE IF NOT EXISTS blocklist (
            user_id BIGINT PRIMARY KEY,
            reason TEXT,
            blocked_at DATETIME DEFAULT CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    (
        "recent_chats",
        """
        CREATE TABLE IF NOT EXISTS recent_chats (
            chat_id VARCHAR(255) PRIMARY KEY,
            last_active DOUBLE NOT NULL,
            metadata TEXT,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_last_active (last_active)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    # ── grillo tables ────────────────────────────────────────────
    (
        "grillo_activity_log",
        """
        CREATE TABLE IF NOT EXISTS grillo_activity_log (
            id INT AUTO_INCREMENT PRIMARY KEY,
            beat_type VARCHAR(50) NOT NULL,
            prompt_text TEXT NOT NULL,
            response_text LONGTEXT,
            diary_entry_id INT,
            executed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            metadata JSON,
            suppressed_count INT DEFAULT 0,
            INDEX idx_executed_at (executed_at),
            INDEX idx_beat_type (beat_type),
            INDEX idx_diary_entry (diary_entry_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    (
        "grillo_action_execs",
        """
        CREATE TABLE IF NOT EXISTS grillo_action_execs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            activity_log_id INT NOT NULL,
            action_index INT NOT NULL,
            action_type VARCHAR(150) NOT NULL,
            payload JSON,
            status ENUM('pending','processed','failed') NOT NULL DEFAULT 'pending',
            error_text TEXT,
            result JSON,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_activity_log_id (activity_log_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    (
        "grillo_beats",
        # Only defined in synth-db.sql — no Python code references this table.
        """
        CREATE TABLE IF NOT EXISTS grillo_beats (
            id INT AUTO_INCREMENT PRIMARY KEY,
            beat_type VARCHAR(50) NOT NULL,
            next_beat DATETIME NOT NULL,
            metadata JSON,
            enabled TINYINT(1) DEFAULT 1,
            plugin_enabled TINYINT(1) DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    # ── agent tables ─────────────────────────────────────────────
    # NOTE: agent_activity_log and agent_action_execs schemas are the
    # union of columns used by BOTH agent_plugin.py and agent_core.py,
    # which reference slightly different column sets.
    (
        "agent_activity_log",
        """
        CREATE TABLE IF NOT EXISTS agent_activity_log (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            command JSON,
            proposer VARCHAR(255),
            status ENUM('proposed','approved','rejected','executed','cancelled') NOT NULL DEFAULT 'proposed',
            trainer_id VARCHAR(100),
            request_ts DATETIME DEFAULT CURRENT_TIMESTAMP,
            response_ts DATETIME,
            response_text LONGTEXT,
            result LONGTEXT,
            metadata JSON,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    (
        "agent_action_execs",
        """
        CREATE TABLE IF NOT EXISTS agent_action_execs (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            activity_log_id BIGINT NOT NULL,
            action_index INT NOT NULL DEFAULT 0,
            action_type VARCHAR(150) DEFAULT NULL,
            command TEXT,
            payload JSON,
            status ENUM('pending','processed','executed','failed') NOT NULL DEFAULT 'pending',
            error_text TEXT,
            result JSON,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
            INDEX idx_activity_log_id (activity_log_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    (
        "agent_tasks",
        """
        CREATE TABLE IF NOT EXISTS agent_tasks (
            id BIGINT AUTO_INCREMENT PRIMARY KEY,
            engine VARCHAR(64),
            status ENUM('pending','running','waiting_for_approval','paused','completed','failed','cancelled') NOT NULL DEFAULT 'pending',
            input JSON,
            iterations_meta JSON,
            output JSON,
            trainer_id VARCHAR(64),
            metadata JSON,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    # ── message_logs (telegram_bot.py — multimodal memory injection) ──
    (
        "message_logs",
        """
        CREATE TABLE IF NOT EXISTS message_logs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            chat_id VARCHAR(255) NOT NULL,
            interface VARCHAR(100),
            sender_id VARCHAR(255),
            sender_name VARCHAR(255),
            content LONGTEXT,
            role VARCHAR(50) DEFAULT 'user',
            metadata JSON,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_chat_id (chat_id),
            INDEX idx_created_at (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    # ── archived_memories (grillo_compactor.py — compacted memory storage) ──
    (
        "archived_memories",
        """
        CREATE TABLE IF NOT EXISTS archived_memories (
            id INT AUTO_INCREMENT PRIMARY KEY,
            tag TEXT COMMENT 'JSON array of tags for the compacted cluster',
            summary TEXT NOT NULL COMMENT 'LLM-generated summary of the compacted memories',
            source_ids TEXT COMMENT 'JSON array of source ai_diary entry IDs',
            source_count INT DEFAULT 0,
            llm_model VARCHAR(255) COMMENT 'The LLM cortex used for summarisation',
            confidence FLOAT COMMENT 'LLM confidence score for the summary',
            notes TEXT COMMENT 'JSON object with justification and detail fields',
            compaction_level INT DEFAULT 1,
            total_source_chars INT DEFAULT 0,
            summary_chars INT DEFAULT 0,
            created_by VARCHAR(100) DEFAULT 'grillo_compactor',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            INDEX idx_created_at (created_at)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
    # ── settings (only in synth-db.sql, no Python references) ───
    (
        "settings",
        """
        CREATE TABLE IF NOT EXISTS settings (
            setting_key VARCHAR(255) PRIMARY KEY,
            `value` TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;
        """,
    ),
]


# ---------------------------------------------------------------------------
# Expected columns per table — used for ALTER TABLE ADD COLUMN
# ---------------------------------------------------------------------------
# Format: table_name -> list of (column_name, ADD COLUMN sql_fragment)
#
# These are the FULL column definitions that go after ALTER TABLE <t> ADD COLUMN.
# Primary-key / auto-increment columns are included so the check is complete;
# they will simply already exist and be skipped.
#
# Sources: same Python files as the CREATE TABLE statements above.
EXPECTED_COLUMNS: dict[str, list[tuple[str, str]]] = {
    # ── core ─────────────────────────────────────────────────────
    "config": [
        ("config_key", "`config_key` VARCHAR(255) NOT NULL"),
        ("value", "`value` TEXT NOT NULL"),
        ("created_at", "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"),
        (
            "updated_at",
            "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
        ),
    ],
    "chat_history_cache": [
        ("id", "id INT AUTO_INCREMENT PRIMARY KEY"),
        ("interface_path", "interface_path VARCHAR(512) NOT NULL"),
        ("sender_name", "sender_name VARCHAR(255)"),
        ("sender_id", "sender_id VARCHAR(255)"),
        ("message_text", "message_text LONGTEXT NOT NULL"),
        ("timestamp", "`timestamp` DATETIME DEFAULT CURRENT_TIMESTAMP"),
    ],
    "chat_session_meta": [
        ("interface_path", "interface_path VARCHAR(512) NOT NULL"),
        ("meta", "meta LONGTEXT NOT NULL"),
        (
            "updated_at",
            "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
        ),
    ],
    "chat_archives": [
        ("id", "id VARCHAR(64) NOT NULL"),
        ("session_id", "session_id VARCHAR(255) DEFAULT NULL"),
        ("name", "`name` VARCHAR(255) DEFAULT NULL"),
        ("messages", "messages LONGTEXT NOT NULL"),
        ("metadata", "metadata LONGTEXT NULL"),
        ("created_at", "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"),
    ],
    # ── plugin ───────────────────────────────────────────────────
    "ai_diary": [
        ("id", "id INT AUTO_INCREMENT PRIMARY KEY"),
        (
            "content",
            "content TEXT NOT NULL COMMENT 'What synth said/did in the interaction'",
        ),
        (
            "personal_thought",
            "personal_thought TEXT COMMENT 'synth personal reflection about the interaction'",
        ),
        (
            "emotions",
            "emotions TEXT DEFAULT '[]' COMMENT 'synth emotions about this interaction'",
        ),
        (
            "interaction_summary",
            "interaction_summary TEXT COMMENT 'Brief summary of what happened'",
        ),
        ("timestamp", "`timestamp` TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        ("interface", "interface VARCHAR(50)"),
        ("chat_id", "chat_id VARCHAR(255)"),
        ("thread_id", "thread_id VARCHAR(255)"),
        (
            "user_message",
            "user_message TEXT COMMENT 'What the user said that triggered this response'",
        ),
        (
            "context_tags",
            "context_tags TEXT DEFAULT '[]' COMMENT 'Tags about the context/topic'",
        ),
        (
            "involved_users",
            "involved_users TEXT DEFAULT '[]' COMMENT 'JSON list of users involved in the interaction'",
        ),
    ],
    "ai_diary_archive": [
        ("id", "id INT AUTO_INCREMENT PRIMARY KEY"),
        (
            "content",
            "content TEXT NOT NULL COMMENT 'What synth said/did in the interaction'",
        ),
        (
            "personal_thought",
            "personal_thought TEXT COMMENT 'synth personal reflection about the interaction'",
        ),
        (
            "emotions",
            "emotions TEXT DEFAULT '[]' COMMENT 'synth emotions about this interaction'",
        ),
        (
            "interaction_summary",
            "interaction_summary TEXT COMMENT 'Brief summary of what happened'",
        ),
        ("timestamp", "`timestamp` TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        ("interface", "interface VARCHAR(50)"),
        ("chat_id", "chat_id VARCHAR(255)"),
        ("thread_id", "thread_id VARCHAR(255)"),
        (
            "user_message",
            "user_message TEXT COMMENT 'What the user said that triggered this response'",
        ),
        (
            "context_tags",
            "context_tags TEXT DEFAULT '[]' COMMENT 'Tags about the context/topic'",
        ),
        (
            "involved_users",
            "involved_users TEXT DEFAULT '[]' COMMENT 'JSON list of users involved in the interaction'",
        ),
    ],
    "emotion_state": [
        ("id", "id INT AUTO_INCREMENT PRIMARY KEY"),
        ("emotion_name", "emotion_name VARCHAR(100) NOT NULL"),
        ("intensity", "intensity FLOAT NOT NULL DEFAULT 5.0"),
        ("timestamp", "`timestamp` DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP"),
        (
            "updated_at",
            "updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
        ),
    ],
    "emotion_diary": [
        ("id", "id INT AUTO_INCREMENT PRIMARY KEY"),
        ("source", "source VARCHAR(100)"),
        ("event", "event VARCHAR(100)"),
        ("emotion", "emotion VARCHAR(100)"),
        ("intensity", "intensity FLOAT"),
        ("state", "state VARCHAR(100)"),
        ("trigger_condition", "trigger_condition VARCHAR(255)"),
        ("decision_logic", "decision_logic TEXT"),
        ("next_check", "next_check DATETIME"),
        ("timestamp", "`timestamp` DATETIME DEFAULT CURRENT_TIMESTAMP"),
    ],
    "memories": [
        ("id", "id INT AUTO_INCREMENT PRIMARY KEY"),
        ("timestamp", "`timestamp` DATETIME NOT NULL"),
        ("content", "content TEXT NOT NULL"),
        ("author", "author VARCHAR(100)"),
        ("source", "source VARCHAR(100)"),
        ("tags", "tags TEXT"),
        ("scope", "scope VARCHAR(50)"),
        ("emotion", "emotion VARCHAR(50)"),
        ("intensity", "intensity INT"),
        ("emotion_state", "emotion_state VARCHAR(50)"),
    ],
    "bio": [
        ("id", "id VARCHAR(255) NOT NULL"),
        ("known_as", "known_as TEXT DEFAULT '[]'"),
        ("likes", "likes TEXT DEFAULT '[]'"),
        ("not_likes", "not_likes TEXT DEFAULT '[]'"),
        ("information", "information TEXT DEFAULT ''"),
        ("past_events", "past_events TEXT DEFAULT '[]'"),
        ("feelings", "feelings TEXT DEFAULT '[]'"),
        ("contacts", "contacts TEXT DEFAULT '{}'"),
        ("social_accounts", "social_accounts TEXT DEFAULT '[]'"),
        ("privacy", "privacy TEXT DEFAULT 'default'"),
        ("created_at", "created_at VARCHAR(50)"),
        ("last_accessed", "last_accessed VARCHAR(50)"),
        # Columns added by bio_manager.py migrations:
        ("last_update", "last_update TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        ("update_count", "update_count INT DEFAULT 0"),
        ("user_name", "user_name VARCHAR(255)"),
    ],
    "scheduled_events": [
        ("id", "id INT AUTO_INCREMENT PRIMARY KEY"),
        ("date", "`date` DATE NOT NULL"),
        ("time", "`time` TIME DEFAULT '00:00'"),
        ("recurrence_type", "recurrence_type VARCHAR(20) DEFAULT 'none'"),
        ("next_run", "next_run DATETIME NOT NULL"),
        ("description", "description TEXT NOT NULL"),
        ("created_at", "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"),
        ("delivered", "delivered BOOLEAN DEFAULT 0"),
        ("created_by", "created_by VARCHAR(100) DEFAULT 'synth'"),
    ],
    "chatlink": [
        ("int_id", "int_id INT AUTO_INCREMENT PRIMARY KEY"),
        ("interface", "interface VARCHAR(32) NOT NULL"),
        ("chat_id", "chat_id TEXT NOT NULL"),
        ("thread_id", "thread_id TEXT DEFAULT NULL"),
        ("chat_name", "chat_name TEXT DEFAULT NULL"),
        ("message_thread_name", "message_thread_name TEXT DEFAULT NULL"),
        ("created_at", "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        (
            "last_updated",
            "last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
        ),
    ],
    "message_map": [
        ("trainer_message_id", "trainer_message_id INTEGER NOT NULL"),
        ("chat_id", "chat_id BIGINT NOT NULL"),
        ("message_id", "message_id INTEGER NOT NULL"),
        ("timestamp", "`timestamp` DOUBLE"),
    ],
    "blocklist": [
        ("user_id", "user_id BIGINT NOT NULL"),
        ("reason", "reason TEXT"),
        ("blocked_at", "blocked_at DATETIME DEFAULT CURRENT_TIMESTAMP"),
    ],
    "recent_chats": [
        ("chat_id", "chat_id VARCHAR(255) NOT NULL"),
        ("last_active", "last_active DOUBLE NOT NULL"),
        ("metadata", "metadata TEXT"),
        ("created_at", "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"),
    ],
    # ── grillo ───────────────────────────────────────────────────
    "grillo_activity_log": [
        ("id", "id INT AUTO_INCREMENT PRIMARY KEY"),
        ("beat_type", "beat_type VARCHAR(50) NOT NULL"),
        ("prompt_text", "prompt_text TEXT NOT NULL"),
        ("response_text", "response_text LONGTEXT"),
        ("diary_entry_id", "diary_entry_id INT"),
        ("executed_at", "executed_at DATETIME DEFAULT CURRENT_TIMESTAMP"),
        ("metadata", "metadata JSON"),
        ("suppressed_count", "suppressed_count INT DEFAULT 0"),
    ],
    "grillo_action_execs": [
        ("id", "id INT AUTO_INCREMENT PRIMARY KEY"),
        ("activity_log_id", "activity_log_id INT NOT NULL"),
        ("action_index", "action_index INT NOT NULL"),
        ("action_type", "action_type VARCHAR(150) NOT NULL"),
        ("payload", "payload JSON"),
        (
            "status",
            "status ENUM('pending','processed','failed') NOT NULL DEFAULT 'pending'",
        ),
        ("error_text", "error_text TEXT"),
        ("result", "`result` JSON"),
        ("created_at", "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"),
        (
            "updated_at",
            "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
        ),
    ],
    "grillo_beats": [
        ("id", "id INT AUTO_INCREMENT PRIMARY KEY"),
        ("beat_type", "beat_type VARCHAR(50) NOT NULL"),
        ("next_beat", "next_beat DATETIME NOT NULL"),
        ("metadata", "metadata JSON"),
        ("enabled", "enabled TINYINT(1) DEFAULT 1"),
        ("plugin_enabled", "plugin_enabled TINYINT(1) DEFAULT 1"),
        ("created_at", "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"),
        (
            "updated_at",
            "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
        ),
    ],
    # ── agent ────────────────────────────────────────────────────
    # Union of columns from agent_plugin.py and agent_core.py
    "agent_activity_log": [
        ("id", "id BIGINT AUTO_INCREMENT PRIMARY KEY"),
        ("command", "command JSON"),
        ("proposer", "proposer VARCHAR(255)"),
        (
            "status",
            "status ENUM('proposed','approved','rejected','executed','cancelled') NOT NULL DEFAULT 'proposed'",
        ),
        ("trainer_id", "trainer_id VARCHAR(100)"),
        ("request_ts", "request_ts DATETIME DEFAULT CURRENT_TIMESTAMP"),
        ("response_ts", "response_ts DATETIME"),
        ("response_text", "response_text LONGTEXT"),
        ("result", "`result` LONGTEXT"),
        ("metadata", "metadata JSON"),
        ("created_at", "created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"),
        (
            "updated_at",
            "updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
        ),
    ],
    "agent_action_execs": [
        ("id", "id BIGINT AUTO_INCREMENT PRIMARY KEY"),
        ("activity_log_id", "activity_log_id BIGINT NOT NULL"),
        ("action_index", "action_index INT NOT NULL DEFAULT 0"),
        ("action_type", "action_type VARCHAR(150) DEFAULT NULL"),
        ("command", "command TEXT"),
        ("payload", "payload JSON"),
        (
            "status",
            "status ENUM('pending','processed','executed','failed') NOT NULL DEFAULT 'pending'",
        ),
        ("error_text", "error_text TEXT"),
        ("result", "`result` JSON"),
        ("created_at", "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"),
        (
            "updated_at",
            "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
        ),
    ],
    "agent_tasks": [
        ("id", "id BIGINT AUTO_INCREMENT PRIMARY KEY"),
        ("engine", "engine VARCHAR(64)"),
        (
            "status",
            "status ENUM('pending','running','waiting_for_approval','paused','completed','failed','cancelled') NOT NULL DEFAULT 'pending'",
        ),
        ("input", "`input` JSON"),
        ("iterations_meta", "iterations_meta JSON"),
        ("output", "`output` JSON"),
        ("trainer_id", "trainer_id VARCHAR(64)"),
        ("metadata", "metadata JSON"),
        ("created_at", "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"),
        (
            "updated_at",
            "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
        ),
    ],
    # ── message_logs ─────────────────────────────────────────────
    "message_logs": [
        ("id", "id INT AUTO_INCREMENT PRIMARY KEY"),
        ("chat_id", "chat_id VARCHAR(255) NOT NULL"),
        ("interface", "interface VARCHAR(100)"),
        ("sender_id", "sender_id VARCHAR(255)"),
        ("sender_name", "sender_name VARCHAR(255)"),
        ("content", "content LONGTEXT"),
        ("role", "role VARCHAR(50) DEFAULT 'user'"),
        ("metadata", "metadata JSON"),
        ("created_at", "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"),
    ],
    # ── archived_memories ────────────────────────────────────────
    "archived_memories": [
        ("id", "id INT AUTO_INCREMENT PRIMARY KEY"),
        ("tag", "tag TEXT"),
        ("summary", "summary TEXT NOT NULL"),
        ("source_ids", "source_ids TEXT"),
        ("source_count", "source_count INT DEFAULT 0"),
        ("llm_model", "llm_model VARCHAR(255)"),
        ("confidence", "confidence FLOAT"),
        ("notes", "notes TEXT"),
        ("compaction_level", "compaction_level INT DEFAULT 1"),
        ("total_source_chars", "total_source_chars INT DEFAULT 0"),
        ("summary_chars", "summary_chars INT DEFAULT 0"),
        ("created_by", "created_by VARCHAR(100) DEFAULT 'grillo_compactor'"),
        ("created_at", "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"),
    ],
    # ── settings ─────────────────────────────────────────────────
    "settings": [
        ("setting_key", "setting_key VARCHAR(255) NOT NULL"),
        ("value", "`value` TEXT NOT NULL"),
        ("created_at", "created_at DATETIME DEFAULT CURRENT_TIMESTAMP"),
        (
            "updated_at",
            "updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP",
        ),
    ],
}


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------
def main() -> None:
    # 1. Load .env
    if not _ENV_FILE.is_file():
        print(f"[ERROR] .env file not found at {_ENV_FILE}")
        sys.exit(1)

    env = _load_dotenv(_ENV_FILE)

    db_host = env.get("DB_HOST", os.getenv("DB_HOST", "localhost"))
    db_port = int(env.get("DB_PORT", os.getenv("DB_PORT", "3306")))
    db_user = env.get("DB_USER", os.getenv("DB_USER", "root"))
    db_pass = env.get("DB_PASS", os.getenv("DB_PASS", ""))
    db_name = env.get("DB_NAME", os.getenv("DB_NAME", "synth"))

    print(f"[INFO] Connecting to {db_user}@{db_host}:{db_port}/{db_name}")

    # 2. Connect (synchronous — pymysql)
    try:
        import pymysql
    except ImportError:
        print("[ERROR] pymysql is not installed. Install it with: pip install pymysql")
        sys.exit(1)

    try:
        conn = pymysql.connect(
            host=db_host,
            port=db_port,
            user=db_user,
            password=db_pass,
            database=db_name,
            charset="utf8mb4",
            connect_timeout=10,
        )
    except Exception as exc:
        print(f"[ERROR] Failed to connect to MySQL/MariaDB: {exc}")
        sys.exit(1)

    print("[INFO] Connected successfully.\n")

    # 3. List existing tables
    with conn.cursor() as cur:
        cur.execute("SHOW TABLES")
        existing_tables: set[str] = {row[0] for row in cur.fetchall()}

    print(
        f"[INFO] Existing tables ({len(existing_tables)}): {', '.join(sorted(existing_tables)) or '(none)'}\n"
    )

    # ── Phase 1: Create missing tables ───────────────────────────
    print("─── Phase 1: Creating missing tables ───")
    tables_created: list[str] = []
    tables_skipped: list[str] = []
    table_errors: list[tuple[str, str]] = []

    for table_name, ddl in TABLE_DEFINITIONS:
        if table_name in existing_tables:
            tables_skipped.append(table_name)
            continue

        try:
            with conn.cursor() as cur:
                cur.execute(ddl)
            conn.commit()
            tables_created.append(table_name)
            existing_tables.add(table_name)  # so Phase 2 can inspect it
            print(f"  [CREATED] {table_name}")
        except Exception as exc:
            table_errors.append((table_name, str(exc)))
            print(f"  [ERROR]   {table_name}: {exc}")

    if not tables_created and not table_errors:
        print("  (all tables already present)")

    # ── Phase 2: Add missing columns to existing tables ──────────
    print("\n─── Phase 2: Adding missing columns ───")
    columns_added: list[tuple[str, str]] = []  # (table, column)
    columns_skipped = 0
    column_errors: list[tuple[str, str, str]] = []  # (table, column, error)

    for table_name, expected_cols in EXPECTED_COLUMNS.items():
        if table_name not in existing_tables:
            # Table doesn't exist and wasn't created (error in Phase 1) — skip
            continue

        # Fetch existing column names for this table
        with conn.cursor() as cur:
            cur.execute(f"SHOW COLUMNS FROM `{table_name}`")
            current_columns: set[str] = {row[0] for row in cur.fetchall()}

        for col_name, col_def in expected_cols:
            if col_name in current_columns:
                columns_skipped += 1
                continue

            # Column is missing — add it
            alter_sql = f"ALTER TABLE `{table_name}` ADD COLUMN {col_def}"
            try:
                with conn.cursor() as cur:
                    cur.execute(alter_sql)
                conn.commit()
                columns_added.append((table_name, col_name))
                print(f"  [ADDED]  {table_name}.{col_name}")
            except Exception as exc:
                err_msg = str(exc)
                # Duplicate column is not a real error (race / already present)
                if "Duplicate column" in err_msg:
                    columns_skipped += 1
                else:
                    column_errors.append((table_name, col_name, err_msg))
                    print(f"  [ERROR]  {table_name}.{col_name}: {err_msg}")

    if not columns_added and not column_errors:
        print("  (all columns already present)")

    # ── Summary ──────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("SUMMARY")
    print("=" * 60)
    print(f"  Tables already present : {len(tables_skipped)}")
    print(f"  Tables created         : {len(tables_created)}")
    print(f"  Table errors           : {len(table_errors)}")
    print(f"  Columns already present: {columns_skipped}")
    print(f"  Columns added          : {len(columns_added)}")
    print(f"  Column errors          : {len(column_errors)}")

    if tables_created:
        print(f"\n  Created tables: {', '.join(sorted(tables_created))}")
    if columns_added:
        print("\n  Added columns:")
        for tbl, col in columns_added:
            print(f"    • {tbl}.{col}")
    if table_errors:
        print("\n  Table errors:")
        for tbl, err in table_errors:
            print(f"    • {tbl}: {err}")
    if column_errors:
        print("\n  Column errors:")
        for tbl, col, err in column_errors:
            print(f"    • {tbl}.{col}: {err}")

    conn.close()
    print("\n[INFO] Done.")


if __name__ == "__main__":
    main()
