/**
 * MCP response formatting utilities
 */

/**
 * Format a text response for MCP protocol.
 * Returns an object compatible with MCP tool handler return type.
 */
export function formatMcpResponse(text: string, isError = false) {
  return {
    content: [{ type: "text" as const, text }],
    ...(isError && { isError: true }),
  };
}
