-- Set max_connections for proper connection pool management
-- This ensures 150 connections for Python pool + 50 for buffer/overhead
SET GLOBAL max_connections=200;

-- Emotion State Table for Centralized Emotion Management
-- Stores emotional state with timestamps for decay calculation
CREATE TABLE IF NOT EXISTS emotion_state (
    id INT AUTO_INCREMENT PRIMARY KEY,
    emotion_name VARCHAR(100) NOT NULL,
    intensity FLOAT NOT NULL DEFAULT 5.0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_emotion_name (emotion_name),
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- G.R.I.L.L.O. Beat Tracking Table
-- Generator for Reflective Inner Loop & Logical Observation
-- Stores autonomous "beat" events for SyntH's internal conscience system
CREATE TABLE IF NOT EXISTS grillo_beats (
    id INT AUTO_INCREMENT PRIMARY KEY,
    beat_type VARCHAR(50) NOT NULL,
    next_beat DATETIME NOT NULL,
    metadata JSON,
    enabled BOOLEAN DEFAULT 1,
    plugin_enabled BOOLEAN DEFAULT 1,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_next_beat (next_beat, enabled, plugin_enabled),
    INDEX idx_beat_type (beat_type)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- G.R.I.L.L.O. Activity Log Table
-- Tracks execution history of all grillo beats for WebUI display
CREATE TABLE IF NOT EXISTS grillo_activity_log (
    id INT AUTO_INCREMENT PRIMARY KEY,
    beat_type VARCHAR(50) NOT NULL,
    prompt_text TEXT NOT NULL,
    response_text LONGTEXT,
    diary_entry_id INT,
    executed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    metadata JSON,
    -- Persistent counter for how many times an outbound beat was suppressed
    suppressed_count INT DEFAULT 0,
    INDEX idx_executed_at (executed_at DESC),
    INDEX idx_beat_type (beat_type),
    INDEX idx_diary_entry (diary_entry_id),
    FOREIGN KEY (diary_entry_id) REFERENCES ai_diary(id) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Tracks action-level executions proposed/executed by Grillo (linked to grillo_activity_log)
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
    INDEX idx_activity_log_id (activity_log_id),
    FOREIGN KEY (activity_log_id) REFERENCES grillo_activity_log(id) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- G.R.I.L.L.O. Self-Growth States Table
-- Rolling history (max 10 rows) of SyntH's evolving self-growth reflection.
-- Exactly one row has is_current = 1. Older rows beyond the newest 10 are pruned.
CREATE TABLE IF NOT EXISTS growth_states (
    id INT AUTO_INCREMENT PRIMARY KEY,
    content LONGTEXT NOT NULL,
    created_by VARCHAR(64) NOT NULL DEFAULT 'grillo_growth',
    source VARCHAR(64) NOT NULL DEFAULT 'weekly',
    is_current BOOLEAN NOT NULL DEFAULT 0,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_growth_current (is_current),
    INDEX idx_growth_created_at (created_at DESC)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Karada Touch Events Table
-- Records WebUI 3D-interaction events (environment/window taps and touches on
-- the Synth avatar) so Synth is aware of physical interaction with her Karada.
-- Environment/window rows are transient (~10 min TTL); synth_touch rows are
-- retained for a sliding 24h window for later reflection.
CREATE TABLE IF NOT EXISTS karada_touch_events (
    id INT AUTO_INCREMENT PRIMARY KEY,
    interface_path VARCHAR(255) NOT NULL,
    session_id VARCHAR(255),
    event_type VARCHAR(50) NOT NULL,
    body_part VARCHAR(100),
    raw_part VARCHAR(100),
    username VARCHAR(255),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME NOT NULL,
    flushed BOOLEAN NOT NULL DEFAULT 0,
    attached BOOLEAN NOT NULL DEFAULT 0,
    INDEX idx_kte_expires (expires_at),
    INDEX idx_kte_flushed (flushed, event_type),
    INDEX idx_kte_iface (interface_path)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Agent tasks table: persistent record of Agentic Runtime turns and their iterations
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
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_agent_status (status),
    INDEX idx_agent_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Rift Vessel Sessions: one row per embodiment session. The buffered lived
-- experience (experience_buffer) is flushed to a single diary entry at
-- end-of-session (explicit logout or inactivity cooldown). No diary/memory is
-- written mid-session. NOTE: never use a bare `timestamp` column (reserved).
CREATE TABLE IF NOT EXISTS vessel_sessions (
    session_id VARCHAR(128) PRIMARY KEY,
    environment VARCHAR(64) NOT NULL,
    interface_path VARCHAR(512),
    status ENUM('active','ended') NOT NULL DEFAULT 'active',
    experience_buffer LONGTEXT,
    diary_entry_id INT,
    started_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    last_event_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    ended_at DATETIME,
    INDEX idx_vessel_sessions_status (status),
    INDEX idx_vessel_sessions_environment (environment),
    INDEX idx_vessel_sessions_last_event (last_event_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Rift Vessel Activity Log: audit trail of embodiment events/actions, shown in
-- the WebUI History > Vessel sub-tab (mirrors grillo_activity_log / radio).
CREATE TABLE IF NOT EXISTS vessel_activity_log (
    id INT AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(128),
    interface_path VARCHAR(512),
    environment VARCHAR(64) NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    summary TEXT NOT NULL,
    metadata JSON,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_vessel_activity_created_at (created_at DESC),
    INDEX idx_vessel_activity_environment (environment),
    INDEX idx_vessel_activity_session (session_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Rift Vessel Diary: the compacted autobiographical entry produced by chunked
-- LLM summarisation of a session's lived experience at end-of-session. This is
-- SEPARATE from the real ai_diary — the vessel no longer writes to ai_diary
-- (that polluted the Fast Lane prompt). Whether/how to import these entries into
-- ai_diary is a later, unimplemented decision. Never a bare `timestamp` column.
CREATE TABLE IF NOT EXISTS vessel_diary (
    id INT AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(128),
    interface_path VARCHAR(512),
    environment VARCHAR(64) NOT NULL,
    summary LONGTEXT NOT NULL,
    moments_count INT DEFAULT 0,
    reason VARCHAR(32),
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_vessel_diary_created_at (created_at DESC),
    INDEX idx_vessel_diary_environment (environment),
    INDEX idx_vessel_diary_session (session_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Rift Vessel Bases: places Synth chose in a world to build, store resources,
-- shelter, sleep, or set its respawn. A world can have several bases, so this
-- is a list per scope tuple. Owned by the Rift Vessel CORE base store
-- (plugins/rift_vessel/vessel_bases.py) — having a home is common to most game
-- worlds. Coordinates are structural (never text): `anchor` is the base's
-- {x,y,z} point used by the night-retreat reflex; `box` is the optional built
-- structure bounding box {x1..z2}. There is NO catalogue of predefined bases.
-- Scope-aware like `goals`; Minecraft bases pin scope='vessel'/game='minecraft'.
-- Never a bare `timestamp` column.
CREATE TABLE IF NOT EXISTS vessel_bases (
    id INT AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(128),
    scope VARCHAR(64) DEFAULT 'none',
    game VARCHAR(64) DEFAULT 'none',
    world VARCHAR(64) DEFAULT 'none',
    name VARCHAR(120) NOT NULL,
    kind VARCHAR(32) DEFAULT 'home',
    anchor TEXT,
    box TEXT,
    note TEXT,
    status VARCHAR(32) DEFAULT 'active',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    INDEX idx_vessel_bases_status (status),
    INDEX idx_vessel_bases_scope (scope, game, world)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Rift Vessel Quests: ordered, directed milestones Synth works toward in a
-- world — one step of a questline (e.g. build first base -> craft a bed -> ...
-- -> defeat the Ender Dragon). The quest STORE + MECHANISM are owned by the
-- Rift Vessel CORE (plugins/rift_vessel/vessel_quests.py) — having a sense of
-- direction is common to most game worlds — while the questline CONTENT (the
-- ordered Minecraft milestones and their structural objectives) lives in the
-- adapter. A quest is surfaced to cognition ONLY as reference; Synth still
-- authors its own goal freely (spontaneity rule). Exactly one row is `active`
-- per scope tuple; `objectives`/`progress` are JSON TEXT. Objectives are
-- matched STRUCTURALLY (inventory ids, dimension id, base/bed flags, per-mob
-- kill counter), never against free text. Scope-aware like `goals`/`vessel_bases`;
-- Minecraft quests pin scope='vessel'/game='minecraft'. Never a bare `timestamp`.
CREATE TABLE IF NOT EXISTS vessel_quests (
    id INT AUTO_INCREMENT PRIMARY KEY,
    scope VARCHAR(64) DEFAULT 'none',
    game VARCHAR(64) DEFAULT 'none',
    world VARCHAR(64) DEFAULT 'none',
    quest_id VARCHAR(64) NOT NULL,
    title VARCHAR(200) NOT NULL,
    description TEXT,
    order_index INT DEFAULT 0,
    status VARCHAR(32) DEFAULT 'locked',
    objectives TEXT,
    progress TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE KEY uq_vessel_quests_identity (scope, game, world, quest_id),
    INDEX idx_vessel_quests_scope (scope, game, world),
    INDEX idx_vessel_quests_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Minecraft Goals: Synth's self-authored, free-text in-world objectives for the
-- Minecraft Vessel (world-specific; owned by the Minecraft goal store,
-- plugins/rift_vessel/minecraft/goals.py). There is NO catalogue of predefined
-- objectives: `description` is whatever Synth decided to do, in its own words,
-- and `note` is its own progress reflection. One active goal at a time; Synth
-- judges its own progress. Never a bare `timestamp` column.
-- Goals: Synth's self-directed goal store (extracted from the Minecraft
-- adapter into the generic `goals` plugin). Scope-aware: a goal is filed under
-- a three-level scope tuple (`scope`/`game`/`world`, all default 'none').
-- 'none'/'none'/'none' is a personal life goal; a Minecraft embodiment goal is
-- filed under 'vessel'/'minecraft'/'none'. At most one row is `active` per
-- scope tuple. The legacy `minecraft_goals` table is renamed + backfilled by
-- core/migrations.py::_migrate_goals_table on upgrade.
CREATE TABLE IF NOT EXISTS goals (
    id INT AUTO_INCREMENT PRIMARY KEY,
    session_id VARCHAR(128),
    scope VARCHAR(64) DEFAULT 'none',
    game VARCHAR(64) DEFAULT 'none',
    world VARCHAR(64) DEFAULT 'none',
    description TEXT NOT NULL,
    note TEXT,
    destination TEXT,
    steps TEXT,
    current_step INT DEFAULT 0,
    target_kind VARCHAR(16),
    target_name VARCHAR(64),
    status VARCHAR(32) DEFAULT 'active',
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_goals_status (status),
    INDEX idx_goals_session (session_id),
    INDEX idx_goals_scope (scope, game, world)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Web Search Tasks: decoupled background web-search jobs triggered by the
-- recon web-search plugin. Recon fires a search and returns immediately; the
-- orchestrator runs the searches off the message pipeline, synthesises an
-- aseptic result via the cortex, and delivers it back as a second turn.
CREATE TABLE IF NOT EXISTS web_search_tasks (
    id VARCHAR(64) PRIMARY KEY,
    interface_path TEXT,
    queries TEXT,
    search_context TEXT,
    status VARCHAR(16) NOT NULL DEFAULT 'pending',
    result_text LONGTEXT,
    error TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_web_search_status (status),
    INDEX idx_web_search_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- External Endpoints: user-defined external AI service endpoints
-- (OpenAI-compatible, Gemini, Anthropic, custom)
CREATE TABLE IF NOT EXISTS external_endpoints (
    id INT AUTO_INCREMENT PRIMARY KEY,
    name VARCHAR(255) NOT NULL UNIQUE,
    display_label VARCHAR(255) NOT NULL DEFAULT '',
    protocol VARCHAR(50) NOT NULL DEFAULT 'openai',
    base_url VARCHAR(1024) NOT NULL DEFAULT '',
    api_key_enc TEXT,
    enabled BOOLEAN NOT NULL DEFAULT 1,
    capabilities JSON,
    subsystem_map JSON,
    available_models JSON,
    models_metadata JSON,
    default_model VARCHAR(255),
    probe_status VARCHAR(50) NOT NULL DEFAULT 'never',
    last_probe_at DATETIME,
    extra_config JSON,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_enabled (enabled),
    INDEX idx_protocol (protocol)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Default external endpoint: Selenium LLM Engine
INSERT IGNORE INTO external_endpoints
    (name, display_label, protocol, base_url, enabled, capabilities, subsystem_map, default_model, probe_status, extra_config)
VALUES
    (
        'selenium-llm-engine',
        'Selenium LLM Engine',
        'openai',
        'http://synth-selenium-llm-engine:8000',
        1,
        '{"llm": true, "tts": false, "stt": false}',
        '{"cortex": true, "vox": false, "auris": false, "live": false}',
        'gemini',
        'never',
        '{"timeout": 300}'
    );

-- Core config table (authoritative for config_registry)
CREATE TABLE IF NOT EXISTS config (
    `config_key` VARCHAR(255) PRIMARY KEY,
    `value` TEXT NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

INSERT IGNORE INTO config (`config_key`, `value`) VALUES ('BASE_CORTEX', 'selenium-llm-engine');
INSERT IGNORE INTO config (`config_key`, `value`) VALUES ('GRILLO_CORTEX', 'Default');
INSERT IGNORE INTO config (`config_key`, `value`) VALUES ('TRAINER_CORTEX', 'Default');
INSERT IGNORE INTO config (`config_key`, `value`) VALUES ('ACTIVE_IRIS_ENGINE', 'selenium-llm-engine');
-- Enable vision support for selenium-llm-engine which supports image uploads
INSERT IGNORE INTO config (`config_key`, `value`) VALUES ('OPENAPI_SUPPORTS_VISION', 'true');
-- Allow enough time for selenium-llm-engine queue processing
INSERT IGNORE INTO config (`config_key`, `value`) VALUES ('RESPONSE_TIMEOUT', '600');
INSERT IGNORE INTO config (`config_key`, `value`) VALUES ('SOUL_PLUGIN_ENABLED', '1');
INSERT IGNORE INTO config (`config_key`, `value`) VALUES ('SOUL_COMPILE_IDLE_SECONDS', '300');
INSERT IGNORE INTO config (`config_key`, `value`) VALUES ('SOUL_SCHEDULER_INTERVAL_SECONDS', '60');
INSERT IGNORE INTO config (`config_key`, `value`) VALUES ('SOUL_REPOSITORY_BACKEND', 'memory');
INSERT IGNORE INTO config (`config_key`, `value`) VALUES ('SOUL_POSTGRES_DSN', '');

-- Grant privileges to synth user from any host
GRANT ALL PRIVILEGES ON synth.* TO 'synth'@'%' IDENTIFIED BY 'synth';
FLUSH PRIVILEGES;