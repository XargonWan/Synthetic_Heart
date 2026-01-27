-- Set max_connections for proper connection pool management
-- This ensures 150 connections for Python pool + 50 for buffer/overhead
SET GLOBAL max_connections=200;

-- Emotion State Table for Centralized Emotion Management
-- Stores emotional state with timestamps for decay calculation
CREATE TABLE IF NOT EXISTS emotion_state (
    id INT AUTO_INCREMENT PRIMARY KEY,
    emotion_name VARCHAR(100) NOT NULL,
    intensity FLOAT NOT NULL DEFAULT 5.0,
    timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_emotion_name (emotion_name),
    INDEX idx_timestamp (timestamp)
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

-- Grant privileges to synth user from any host
GRANT ALL PRIVILEGES ON synth.* TO 'synth'@'%' IDENTIFIED BY 'synth';
FLUSH PRIVILEGES;