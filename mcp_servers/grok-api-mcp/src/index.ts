#!/usr/bin/env node

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

import { searchDocs } from "./tools/search-docs.js";
import { getDocPage, DOCS_BASE_URL } from "./tools/get-doc-page.js";
import { listEndpoints, formatEndpointsTable } from "./tools/list-endpoints.js";
import { getModelsData, formatModelsTable } from "./tools/list-models.js";
import { formatMcpResponse } from "./utils/mcp-response.js";
import { getErrorMessage } from "./utils/error-handler.js";
import { logInfo, logError } from "./utils/logger.js";

/**
 * Wrap a tool handler with consistent error handling.
 * Returns error response instead of throwing, ensuring MCP protocol compliance.
 */
function withErrorHandler<T>(
  toolName: string,
  handler: () => T | Promise<T>
): Promise<ReturnType<typeof formatMcpResponse>> {
  return Promise.resolve()
    .then(() => handler())
    .then((result) => result as ReturnType<typeof formatMcpResponse>)
    .catch((error) => {
      logError(toolName, "Tool handler failed", error);
      return formatMcpResponse(`Error: ${getErrorMessage(error)}`, true);
    });
}

/** Server configuration */
const SERVER_CONFIG = {
  name: "grok-api-docs",
  version: "1.0.0",
} as const;

/** Tool definitions with names and descriptions */
const TOOLS = {
  SEARCH_DOCS: {
    name: "search_docs",
    description:
      "Search bundled Grok API documentation for relevant information. Use this for quick lookups of API features, models, function calling, and built-in tools.",
  },
  GET_DOC_PAGE: {
    name: "get_doc_page",
    description:
      "Fetch fresh documentation from docs.x.ai. Use this when you need the latest information or content not available in bundled docs.",
  },
  LIST_ENDPOINTS: {
    name: "list_api_endpoints",
    description:
      "List available xAI API endpoints. Filter by category: chat, images, videos, voice, models, files, batch, collections, api-keys, billing, team, audit.",
  },
  LIST_MODELS: {
    name: "list_models",
    description: "List all available Grok models with their IDs, context lengths, and capabilities.",
  },
} as const;

/** API base URLs */
const API_BASE_URLS = {
  inference: "https://api.x.ai",
  management: "https://management-api.x.ai",
} as const;

const server = new McpServer(SERVER_CONFIG);

// Tool: search_docs
server.tool(
  TOOLS.SEARCH_DOCS.name,
  TOOLS.SEARCH_DOCS.description,
  {
    query: z.string().describe("Search term or phrase to find in documentation"),
  },
  async ({ query }) => {
    return withErrorHandler(TOOLS.SEARCH_DOCS.name, () => {
      const results = searchDocs(query);

      if (results.length === 0) {
        return formatMcpResponse(
          `No results found for "${query}". Try different keywords or use ${TOOLS.GET_DOC_PAGE.name} to fetch specific documentation pages.`
        );
      }

      const output = results
        .map((r) => `## ${r.title} (${r.source})\n\n${r.snippet}`)
        .join("\n\n---\n\n");

      return formatMcpResponse(`Found ${results.length} result(s) for "${query}":\n\n${output}`);
    });
  }
);

// Tool: get_doc_page
server.tool(
  TOOLS.GET_DOC_PAGE.name,
  TOOLS.GET_DOC_PAGE.description,
  {
    path: z
      .string()
      .describe("Documentation path, e.g., 'guides/function-calling', 'api-reference', 'models'"),
  },
  async ({ path }) => {
    try {
      const content = await getDocPage(path);
      return formatMcpResponse(
        `# Documentation: ${path}\n\nSource: ${DOCS_BASE_URL}/${path}\n\n${content}`
      );
    } catch (error) {
      return formatMcpResponse(
        `Error fetching documentation: ${getErrorMessage(error)}\n\nTry using ${TOOLS.SEARCH_DOCS.name} to find bundled documentation instead.`,
        true
      );
    }
  }
);

// Tool: list_api_endpoints
server.tool(
  TOOLS.LIST_ENDPOINTS.name,
  TOOLS.LIST_ENDPOINTS.description,
  {
    category: z
      .string()
      .optional()
      .describe("Filter by category: chat, images, videos, voice, models, files, batch, collections, api-keys, billing, team, audit"),
  },
  async ({ category }) => {
    return withErrorHandler(TOOLS.LIST_ENDPOINTS.name, () => {
      const result = listEndpoints(category);
      const table = formatEndpointsTable(result);
      const header = category ? `# xAI API Endpoints (${category})` : "# xAI API Endpoints";
      const baseUrls = `**Inference API:** ${API_BASE_URLS.inference}\n**Management API:** ${API_BASE_URLS.management}`;

      return formatMcpResponse(`${header}\n\n${baseUrls}\n\n${table}`);
    });
  }
);

// Tool: list_models
server.tool(
  TOOLS.LIST_MODELS.name,
  TOOLS.LIST_MODELS.description,
  {},
  async () => {
    return withErrorHandler(TOOLS.LIST_MODELS.name, () => {
      const data = getModelsData();
      const table = formatModelsTable(data);

      return formatMcpResponse(`# Grok Models\n\n${table}`);
    });
  }
);

// Start the server
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
  logInfo("server", `${SERVER_CONFIG.name} v${SERVER_CONFIG.version} running on stdio`);
}

main().catch((error) => {
  logError("server", "Fatal error during startup", error);
  process.exit(1);
});
