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

-- Grant privileges to synth user from any host
GRANT ALL PRIVILEGES ON synth.* TO 'synth'@'%' IDENTIFIED BY 'synth';
FLUSH PRIVILEGES;