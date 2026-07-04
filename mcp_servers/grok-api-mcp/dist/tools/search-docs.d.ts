interface SearchResult {
    source: string;
    title: string;
    snippet: string;
    relevance: number;
}
/**
 * Search bundled documentation for a query.
 * @returns Top matching results sorted by relevance
 */
export declare function searchDocs(query: string): SearchResult[];
export {};
