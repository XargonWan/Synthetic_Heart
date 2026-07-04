/**
 * MCP response formatting utilities
 */
/**
 * Format a text response for MCP protocol.
 * Returns an object compatible with MCP tool handler return type.
 */
export function formatMcpResponse(text, isError = false) {
    return {
        content: [{ type: "text", text }],
        ...(isError && { isError: true }),
    };
}
