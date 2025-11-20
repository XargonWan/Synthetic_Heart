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

-- Grant privileges to synth user from any host
GRANT ALL PRIVILEGES ON synth.* TO 'synth'@'%' IDENTIFIED BY 'synth';
FLUSH PRIVILEGES;