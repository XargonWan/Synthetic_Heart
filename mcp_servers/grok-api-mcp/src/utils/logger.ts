/**
 * Centralized logging utility for consistent error and warning messages.
 * All console output should go through this module.
 *
 * IMPORTANT: All log levels (error, warn, info) write to stderr.
 * This is intentional for MCP (Model Context Protocol) compatibility:
 * - MCP uses stdio transport where stdout is reserved for JSON-RPC messages
 * - Any stdout output would corrupt the protocol communication
 * - stderr is the correct destination for all diagnostic output
 *
 * This means log level filtering must happen externally (e.g., via shell redirection)
 * rather than by output stream separation.
 */

/** Log levels for filtering */
export type LogLevel = "error" | "warn" | "info";

/** Structured log entry */
interface LogEntry {
  level: LogLevel;
  context: string;
  message: string;
  error?: unknown;
}

/**
 * Sanitize a string for safe logging.
 * Escapes newlines and other control characters to prevent log injection.
 */
function sanitizeForLog(str: string): string {
  return str
    .replace(/\\/g, "\\\\") // Escape backslashes first
    .replace(/\n/g, "\\n")
    .replace(/\r/g, "\\r")
    .replace(/\t/g, "\\t")
    .replace(/[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/g, ""); // Remove other control chars
}

/**
 * Format a log entry consistently.
 * Sanitizes user-controlled content to prevent log injection attacks.
 */
function formatLogEntry(entry: LogEntry): string {
  const prefix = `[${sanitizeForLog(entry.context)}]`;
  const safeMessage = sanitizeForLog(entry.message);
  const errorDetail = entry.error ? `: ${sanitizeForLog(String(entry.error))}` : "";
  return `${prefix} ${safeMessage}${errorDetail}`;
}

/**
 * Log an error message.
 * @param context - Module or function name for context
 * @param message - Error description
 * @param error - Optional error object for details
 */
export function logError(context: string, message: string, error?: unknown): void {
  console.error(formatLogEntry({ level: "error", context, message, error }));
}

/**
 * Log a warning message.
 * @param context - Module or function name for context
 * @param message - Warning description
 */
export function logWarn(context: string, message: string): void {
  console.error(formatLogEntry({ level: "warn", context, message }));
}

/**
 * Log an info message (to stderr for MCP compatibility).
 * @param context - Module or function name for context
 * @param message - Info message
 */
export function logInfo(context: string, message: string): void {
  console.error(formatLogEntry({ level: "info", context, message }));
}
