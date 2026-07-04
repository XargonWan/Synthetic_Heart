import { loadDataFile } from "../utils/data-loader.js";
import { formatMarkdownTable } from "../utils/format.js";
import { logWarn } from "../utils/logger.js";
/** Valid endpoint categories */
const VALID_CATEGORIES = [
    "chat",
    "images",
    "videos",
    "voice",
    "models",
    "files",
    "batch",
    "collections",
    "api-keys",
    "billing",
    "team",
    "audit",
];
/** Maximum length for category parameter to prevent abuse */
const MAX_CATEGORY_LENGTH = 50;
export function listEndpoints(category) {
    const data = loadDataFile("endpoints.json");
    if (!category) {
        return { endpoints: data.endpoints };
    }
    // Validate category length to prevent abuse
    if (category.length > MAX_CATEGORY_LENGTH) {
        return { endpoints: [], warning: "Category name too long" };
    }
    const normalizedCategory = category.toLowerCase();
    const isValidCategory = VALID_CATEGORIES.includes(normalizedCategory);
    // Validate category BEFORE filtering to avoid unnecessary work on invalid input
    if (!isValidCategory) {
        // Find similar categories for helpful suggestions
        const suggestions = VALID_CATEGORIES.filter((valid) => valid.includes(normalizedCategory) || normalizedCategory.includes(valid));
        const suggestionText = suggestions.length > 0
            ? ` Did you mean: ${suggestions.join(", ")}?`
            : "";
        const warning = `Unknown category "${category}". Valid categories: ${VALID_CATEGORIES.join(", ")}.${suggestionText}`;
        logWarn("listEndpoints", warning);
        // Return empty array with clear message - no results for invalid category
        return { endpoints: [], warning };
    }
    const endpoints = data.endpoints.filter((e) => e.category === normalizedCategory);
    return { endpoints };
}
export function formatEndpointsTable(result) {
    const parts = [];
    if (result.warning) {
        parts.push(`> **Warning**: ${result.warning}\n`);
    }
    if (result.endpoints.length === 0) {
        parts.push("No endpoints found.");
        return parts.join("\n");
    }
    const headers = ["Method", "Path", "Description", "Category", "API"];
    const rows = result.endpoints.map((e) => [e.method, e.path, e.description, e.category, e.api]);
    parts.push(formatMarkdownTable(headers, rows));
    return parts.join("\n");
}
