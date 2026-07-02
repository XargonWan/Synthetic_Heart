/** Base URL for xAI documentation */
export declare const DOCS_BASE_URL = "https://docs.x.ai/docs";
/**
 * Fetch documentation page from docs.x.ai and convert to markdown.
 * @param path - Documentation path (e.g., "guides/function-calling")
 * @returns Markdown content
 */
export declare function getDocPage(path: string): Promise<string>;
