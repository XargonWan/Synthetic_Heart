import { logError } from "./logger.js";

/** Threshold for formatting as millions (1M tokens) */
const MILLION_THRESHOLD = 1_000_000;

/** Threshold for formatting as thousands (1K tokens) */
const THOUSAND_THRESHOLD = 1_000;

/**
 * Format a context length number as a human-readable string.
 * @param contextLength - The context length in tokens (or null/undefined)
 * @returns Formatted string like "128K", "1M", or "N/A"
 */
export function formatContextLength(contextLength: number | null | undefined): string {
  // Handle null, undefined, zero, and negative values
  if (contextLength === null || contextLength === undefined || contextLength <= 0) {
    return "N/A";
  }
  if (contextLength >= MILLION_THRESHOLD) {
    return `${(contextLength / MILLION_THRESHOLD).toFixed(0)}M`;
  }
  if (contextLength >= THOUSAND_THRESHOLD) {
    return `${(contextLength / THOUSAND_THRESHOLD).toFixed(0)}K`;
  }
  // Values under 1000 shown as-is
  return String(contextLength);
}

/**
 * Parse JSON with consistent error handling.
 * @param rawContent - Raw JSON string to parse
 * @param filename - Filename for error messages
 * @returns Parsed data or null if parsing fails
 */
export function parseJSON<T>(rawContent: string, filename: string): T | null {
  try {
    return JSON.parse(rawContent) as T;
  } catch (error) {
    logError("parseJSON", `Invalid JSON in ${filename}`, error);
    return null;
  }
}

/**
 * Format data as a markdown table.
 * Validates that row lengths match header count to prevent malformed tables.
 * @param headers - Column headers
 * @param rows - Array of row data (each row is an array of cell values)
 * @returns Formatted markdown table string
 */
export function formatMarkdownTable(headers: string[], rows: string[][]): string {
  if (headers.length === 0) {
    return "";
  }

  const headerCount = headers.length;
  const headerRow = `| ${headers.join(" | ")} |`;
  const separatorRow = `|${headers.map(() => "---").join("|")}|`;

  // Normalize rows to match header count (pad with empty or truncate)
  const dataRows = rows.map((row) => {
    const normalizedRow =
      row.length === headerCount
        ? row
        : row.length < headerCount
          ? [...row, ...Array(headerCount - row.length).fill("")]
          : row.slice(0, headerCount);
    return `| ${normalizedRow.join(" | ")} |`;
  });

  return [headerRow, separatorRow, ...dataRows].join("\n");
}

/**
 * Format a model value (single string or array of strings) as a comma-separated string.
 * Used for displaying recommended models which can be a single model ID or multiple.
 * @param modelValue - Single model ID or array of model IDs
 * @param options - Formatting options
 * @param options.markdown - If true, wrap each model in backticks for markdown code formatting
 * @returns Formatted string of model ID(s)
 */
export function formatModelValue(
  modelValue: string | string[],
  options?: { markdown?: boolean }
): string {
  const wrapModel = options?.markdown ? (m: string) => `\`${m}\`` : (m: string) => m;
  return Array.isArray(modelValue)
    ? modelValue.map(wrapModel).join(", ")
    : wrapModel(modelValue);
}
