---
kind: logging_system
name: Structured Logging System with Daily Rotation and LogChat Notifications
category: logging_system
scope:
    - '**'
source_files:
    - core/logging_utils.py
    - core/log_archive.py
    - core/cortex_api_logger.py
    - core/live_api_logger.py
    - core/llm_failure_log.py
    - mcp_servers/synth_logs.py
---

Synthetic Heart implements a layered logging system built on Python's stdlib `logging` module, providing structured file output, daily rotation with gzip compression, per-component log files, and optional chat-based notifications through LogChat.

**Core Framework and Initialization**
The central entry point is `core/logging_utils.py`, which sets up the primary `synth` logger with both stdout and file handlers. It reads configuration from environment variables (`LOGGING_LEVEL`, `LOG_DIR`, `TZ`) or falls back to the config registry for dynamic runtime updates. A custom `TimeZoneFormatter` ensures all timestamps include timezone offsets for cross-system correlation.

**Log File Architecture**
- **Primary log**: `logs/synth.log` captures all levels (DEBUG/INFO/WARNING/ERROR) with full stack traces on errors
- **Error-only companion**: `logs/synth_errors.log` provides a filtered view of only ERROR-level entries for quick diagnosis
- **Component-specific logs**: Separate files like `logs/webui.log`, `logs/cortex_api.log`, `logs/live_api.log` are created via `_write_to_separate_log()` when modules specify a `log_file` parameter
- **Specialized API logs**: `cortex_api_logger.py` and `live_api_logger.py` use dedicated formatters with visual separators for LLM request/response cycles and WebSocket sessions

**Rotation and Retention Strategy**
A custom `TimestampedRotatingFileHandler` extends `RotatingFileHandler` with daily rollover based on calendar days rather than fixed intervals. Files follow the naming scheme: `synth.log` (active), `synth.2026-07-29.log` (daily rotated), `synth.2026-07-29.1.log` (intra-day shards when size/line limits exceeded). The `core/log_archive.py` module manages retention by compressing files older than yesterday into `.gz` format and deleting anything beyond the configured retention window (default 7 days).

**Structured Output Format**
Standard log lines follow the pattern: `[YYYY-MM-DD HH:MM:SS +ZZZZ] [LEVEL] [file.py:lineno] message`. This enables parsing by tools like the MCP server in `mcp_servers/synth_logs.py`, which provides search, tail, and filtering capabilities across all log files including compressed archives.

**Configuration and Dynamic Updates**
Logging levels can be updated at runtime through the config registry without restarting the application. The system supports two level configurations: general logging level and a separate `LOGGING_LOGCHAT_LEVEL` threshold that controls when messages trigger chat notifications.

**LogChat Integration**
When log levels meet the notification threshold, the system asynchronously sends formatted messages to configured chat interfaces (Discord, Matrix, etc.) using the `skip_history=True` flag to prevent polluting conversation context. Messages about interface errors and transport issues are filtered to avoid recursive notification loops.

**External Integrations**
The `llm_failure_log.py` module maintains a separate database table (`llm_failure_log`) for structured failure tracking with automatic code inference, in-memory fallback storage, and recovery plugin integration. Langfuse tracing is optionally enabled for LLM API calls when `CORTEX_LANGFUSE_ENABLED=true`.

**Security Considerations**
Sensitive fields (authorization headers, API keys, tokens) are automatically redacted in cortex API logs. Large payloads are truncated to prevent log flooding, and the system includes safeguards against writing to restricted directories.