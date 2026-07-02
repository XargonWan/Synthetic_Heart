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
/**
 * Log an error message.
 * @param context - Module or function name for context
 * @param message - Error description
 * @param error - Optional error object for details
 */
export declare function logError(context: string, message: string, error?: unknown): void;
/**
 * Log a warning message.
 * @param context - Module or function name for context
 * @param message - Warning description
 */
export declare function logWarn(context: string, message: string): void;
/**
 * Log an info message (to stderr for MCP compatibility).
 * @param context - Module or function name for context
 * @param message - Info message
 */
export declare function logInfo(context: string, message: string): void;
