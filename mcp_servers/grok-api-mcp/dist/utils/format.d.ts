/**
 * Format a context length number as a human-readable string.
 * @param contextLength - The context length in tokens (or null/undefined)
 * @returns Formatted string like "128K", "1M", or "N/A"
 */
export declare function formatContextLength(contextLength: number | null | undefined): string;
/**
 * Parse JSON with consistent error handling.
 * @param rawContent - Raw JSON string to parse
 * @param filename - Filename for error messages
 * @returns Parsed data or null if parsing fails
 */
export declare function parseJSON<T>(rawContent: string, filename: string): T | null;
/**
 * Format data as a markdown table.
 * Validates that row lengths match header count to prevent malformed tables.
 * @param headers - Column headers
 * @param rows - Array of row data (each row is an array of cell values)
 * @returns Formatted markdown table string
 */
export declare function formatMarkdownTable(headers: string[], rows: string[][]): string;
/**
 * Format a model value (single string or array of strings) as a comma-separated string.
 * Used for displaying recommended models which can be a single model ID or multiple.
 * @param modelValue - Single model ID or array of model IDs
 * @param options - Formatting options
 * @param options.markdown - If true, wrap each model in backticks for markdown code formatting
 * @returns Formatted string of model ID(s)
 */
export declare function formatModelValue(modelValue: string | string[], options?: {
    markdown?: boolean;
}): string;
