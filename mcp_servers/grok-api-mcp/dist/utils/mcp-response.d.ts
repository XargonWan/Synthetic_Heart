/**
 * MCP response formatting utilities
 */
/**
 * Format a text response for MCP protocol.
 * Returns an object compatible with MCP tool handler return type.
 */
export declare function formatMcpResponse(text: string, isError?: boolean): {
    isError?: boolean | undefined;
    content: {
        type: "text";
        text: string;
    }[];
};
