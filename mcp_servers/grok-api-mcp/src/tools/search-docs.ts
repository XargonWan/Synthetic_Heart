import { readFileSync, readdirSync } from "fs";
import { join } from "path";
import { DATA_DIR } from "../utils/data-loader.js";
import { formatContextLength, formatModelValue, parseJSON } from "../utils/format.js";
import { logError, logWarn } from "../utils/logger.js";
import { Endpoint, EndpointsData, Model, ModelsData } from "../types.js";

/**
 * Scoring constants for relevance calculation.
 *
 * Rationale:
 * - EXACT_PHRASE_BONUS (20): Strong signal that document is highly relevant.
 *   Set high to ensure exact matches rank above documents with scattered term hits.
 * - HEADER_MATCH_MULTIPLIER (5): Headers indicate topic relevance. A term in a
 *   header is more significant than in body text.
 * - CODE_BLOCK_MATCH_MULTIPLIER (2): Code examples are valuable for API docs.
 *   Modest bonus to surface implementation examples.
 * - HEADER_PRIORITY (3) vs REGULAR_MATCH_PRIORITY (1): When extracting snippets,
 *   prefer showing header context over body text matches.
 */
const SCORING = {
  EXACT_PHRASE_BONUS: 20,
  HEADER_MATCH_MULTIPLIER: 5,
  CODE_BLOCK_MATCH_MULTIPLIER: 2,
  HEADER_PRIORITY: 3,
  REGULAR_MATCH_PRIORITY: 1,
} as const;

/**
 * Snippet extraction constants.
 *
 * Rationale:
 * - DEFAULT_MAX_LENGTH (400): ~3-4 sentences, enough context without overwhelming.
 *   Balances readability with providing useful preview.
 * - LINE_WRAP_THRESHOLD (100): If match is >100 chars from line start, don't
 *   show the full line prefix. Prevents wasted space on long lines.
 * - FALLBACK_OFFSET (50): When line start is too far, show ~50 chars before
 *   match for context. Half a typical line width.
 */
const SNIPPET = {
  DEFAULT_MAX_LENGTH: 400,
  LINE_WRAP_THRESHOLD: 100,
  FALLBACK_OFFSET: 50,
} as const;

/** Maximum number of search results to return */
const MAX_RESULTS = 10;

/** Minimum term length to include in search */
const MIN_TERM_LENGTH = 2;

/** Maximum query length to prevent performance issues with adversarial inputs */
const MAX_QUERY_LENGTH = 500;

/** Maximum length for a single search term (prevents ReDoS with long patterns) */
const MAX_TERM_LENGTH = 100;

/** Maximum terms to process (defensive limit) */
const MAX_TERM_COUNT = 20;

/** Maximum content size to process for snippets (1MB) */
const MAX_CONTENT_SIZE = 1024 * 1024;

/**
 * Parse a query string into normalized search terms.
 * Filters out terms shorter than MIN_TERM_LENGTH or longer than MAX_TERM_LENGTH.
 * Truncates to MAX_TERM_COUNT terms to prevent performance issues.
 */
function parseSearchTerms(query: string): string[] {
  return query
    .toLowerCase()
    .split(/\s+/)
    .filter((t) => t.length >= MIN_TERM_LENGTH && t.length <= MAX_TERM_LENGTH)
    .slice(0, MAX_TERM_COUNT);
}

/** File extensions for document types */
const FILE_EXTENSIONS = {
  MARKDOWN: ".md",
  JSON: ".json",
} as const;

/** Known JSON data files */
const JSON_FILES = {
  ENDPOINTS: "endpoints.json",
  MODELS: "models.json",
} as const;

interface SearchResult {
  source: string;
  title: string;
  snippet: string;
  relevance: number;
}

interface DocumentContent {
  name: string;
  title: string;
  content: string;
}

/**
 * Cached documents - loaded once on first search, never invalidated.
 * This is intentional: MCP servers are short-lived processes that restart
 * when configuration changes. Runtime data file updates are not expected.
 * If hot-reload is needed in the future, add a cache invalidation mechanism.
 */
let documentsCache: DocumentContent[] | null = null;

/**
 * Regex pattern types for match counting and location finding.
 * - count:* patterns use global flag for counting all matches
 * - find:* patterns are non-global for finding first match location
 */
type CountPatternType = "count:term" | "count:header" | "count:codeBlock";
type FindPatternType = "find:header";
type PatternType = CountPatternType | FindPatternType;

/** Maximum entries in regex cache to prevent unbounded growth */
const MAX_REGEX_CACHE_SIZE = 1000;

/** Extra entries to remove during eviction to avoid frequent cache cleanups */
const EVICTION_BUFFER = 100;

/** Cache for compiled regex patterns to avoid repeated compilation (LRU eviction) */
const regexCache = new Map<string, RegExp>();

/**
 * Evict least recently used entries from regex cache when at or exceeding max size.
 * Uses Map's insertion order property - oldest entries are at the start.
 * Accessing an entry via touchCacheEntry() moves it to the end (most recently used).
 * Proactive eviction: triggers at threshold, not after exceeding it.
 */
function evictRegexCache(): void {
  if (regexCache.size < MAX_REGEX_CACHE_SIZE) return;

  const entriesToRemove = Math.min(EVICTION_BUFFER, regexCache.size);
  let removed = 0;
  for (const key of regexCache.keys()) {
    if (removed >= entriesToRemove) break;
    regexCache.delete(key);
    removed++;
  }
}

/**
 * Move a cache entry to the end of the Map (mark as most recently used).
 * This enables LRU eviction since Map iteration order is insertion order.
 */
function touchCacheEntry(key: string, value: RegExp): void {
  regexCache.delete(key);
  regexCache.set(key, value);
}

function escapeRegex(str: string): string {
  return str.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Get or create a cached regex pattern for the given type and term.
 * Pattern types prefixed with "count:" are global for counting matches.
 * Pattern types prefixed with "find:" are non-global for location finding.
 * Cache uses LRU eviction - accessed patterns are moved to end of Map.
 */
function getPattern(type: PatternType, term: string): RegExp {
  const cacheKey = `${type}:${term}`;
  const cached = regexCache.get(cacheKey);
  if (cached) {
    // Move to end of Map (mark as most recently used)
    touchCacheEntry(cacheKey, cached);
    return cached;
  }

  let regex: RegExp;
  const escaped = escapeRegex(term);
  switch (type) {
    case "count:term":
      regex = new RegExp(escaped, "gi");
      break;
    case "count:header":
      // Non-greedy .*? to prevent backtracking on long lines
      regex = new RegExp(`^#+.*?${escaped}`, "gim");
      break;
    case "count:codeBlock":
      regex = new RegExp(`\`[^\`]*${escaped}[^\`]*\``, "gi");
      break;
    case "find:header":
      // Captures header prefix for location finding (non-global)
      // Non-greedy .*? to prevent backtracking on long lines
      regex = new RegExp(`^(#+.*?)${escaped}`, "im");
      break;
  }
  regexCache.set(cacheKey, regex);
  evictRegexCache();
  return regex;
}

/**
 * Count matches of a pattern in content.
 */
function countMatches(content: string, type: "term" | "header" | "codeBlock", term: string): number {
  const countType = `count:${type}` as CountPatternType;
  const regex = getPattern(countType, term);
  regex.lastIndex = 0; // Reset for reuse with global patterns
  const matches = content.match(regex);
  return matches ? matches.length : 0;
}

interface MatchLocation {
  index: number;
  priority: number;
}

/**
 * Find a match of the specified type in content.
 * @param type - "header" for header matches, "regular" for plain text matches
 */
function findMatch(
  content: string,
  term: string,
  type: "header" | "regular"
): MatchLocation | null {
  if (type === "header") {
    const regex = getPattern("find:header", term);
    regex.lastIndex = 0;
    const match = content.match(regex);
    if (match && match.index !== undefined) {
      return { index: match.index, priority: SCORING.HEADER_PRIORITY };
    }
    return null;
  }

  // Regular match - no regex needed, simple string search
  const index = content.toLowerCase().indexOf(term.toLowerCase());
  if (index !== -1) {
    return { index, priority: SCORING.REGULAR_MATCH_PRIORITY };
  }
  return null;
}

/** Maximum length for extracted titles to prevent oversized values */
const MAX_TITLE_LENGTH = 200;

function extractTitle(content: string, filename: string): string {
  const match = content.match(/^#\s+(.+)$/m);
  if (match && match[1].trim().length > 0) {
    const title = match[1].trim();
    // Truncate overly long titles
    return title.length > MAX_TITLE_LENGTH ? title.slice(0, MAX_TITLE_LENGTH) + "..." : title;
  }
  // Fallback to filename-derived title
  const fallback = filename.replace(/-/g, " ").replace(/\.md$/, "");
  return fallback.length > MAX_TITLE_LENGTH ? fallback.slice(0, MAX_TITLE_LENGTH) + "..." : fallback;
}

function formatEndpointForSearch(endpoint: Endpoint): string {
  return `${endpoint.method} ${endpoint.path}: ${endpoint.description} (${endpoint.category}, ${endpoint.api} API)`;
}

function formatModelForSearch(model: Model): string {
  const context = formatContextLength(model.contextLength);
  return `${model.name} (${model.id}): ${model.description}. Context: ${context}. Capabilities: ${model.capabilities.join(", ")}`;
}

/** Result of document loading - distinguishes read errors from parse errors */
interface LoadDocumentResult {
  doc: DocumentContent | null;
  error?: "read" | "parse";
}

/**
 * Generic document loader with error handling.
 * Returns a result object that distinguishes between read errors and parse errors.
 * @param filePath - Path to the file
 * @param parser - Function to parse content into DocumentContent
 * @param errorContext - Description for error messages
 */
function loadDocument<T>(
  filePath: string,
  parser: (raw: string) => DocumentContent | null,
  errorContext: string
): LoadDocumentResult {
  let rawContent: string;
  try {
    rawContent = readFileSync(filePath, "utf-8");
  } catch (error) {
    // Don't log full file path to avoid information disclosure
    logError("loadDocument", `Failed to read ${errorContext}`, error);
    return { doc: null, error: "read" };
  }

  try {
    const doc = parser(rawContent);
    if (doc === null) {
      logWarn("loadDocument", `Failed to parse ${errorContext}: parser returned null`);
      return { doc: null, error: "parse" };
    }
    return { doc };
  } catch (error) {
    logError("loadDocument", `Failed to parse ${errorContext}`, error);
    return { doc: null, error: "parse" };
  }
}

function parseMarkdown(content: string, filename: string): DocumentContent | null {
  const name = filename.replace(FILE_EXTENSIONS.MARKDOWN, "");
  return {
    name,
    title: extractTitle(content, filename),
    content,
  };
}

/**
 * Configuration for parsing a JSON data file into searchable content.
 * Uses parseJSON + manual validation instead of loadDataFile with Zod schemas
 * because search-docs needs graceful degradation (return null) rather than throwing.
 * This allows partial functionality if a single data file is corrupt.
 */
interface JsonDataParserConfig<T> {
  filename: string;
  name: string;
  title: string;
  /** Validates parsed data has required fields */
  validate: (data: T) => boolean;
  /** Formats validated data into searchable text content */
  formatContent: (data: T) => string;
}

/**
 * Generic parser for JSON data files.
 * Returns null on parse failure or validation failure for graceful degradation.
 */
function parseJsonDataFile<T>(
  rawContent: string,
  config: JsonDataParserConfig<T>
): DocumentContent | null {
  const data = parseJSON<T>(rawContent, config.filename);
  if (data === null) return null;
  if (!config.validate(data)) return null;

  return {
    name: config.name,
    title: config.title,
    content: config.formatContent(data),
  };
}

/** Parser config for endpoints.json */
const ENDPOINTS_PARSER_CONFIG: JsonDataParserConfig<EndpointsData> = {
  filename: "endpoints.json",
  name: "endpoints",
  title: "API Endpoints Reference",
  validate: (data) => Array.isArray(data.endpoints),
  formatContent: (data) => data.endpoints.map(formatEndpointForSearch).join("\n"),
};

/** Parser config for models.json */
const MODELS_PARSER_CONFIG: JsonDataParserConfig<ModelsData> = {
  filename: "models.json",
  name: "models",
  title: "Models Reference",
  validate: (data) => Array.isArray(data.models),
  formatContent: (data) => {
    let content = data.models.map(formatModelForSearch).join("\n");
    if (data.recommendedModels) {
      content += "\n\nRecommended Models:\n";
      for (const [useCase, model] of Object.entries(data.recommendedModels)) {
        content += `${useCase}: ${formatModelValue(model)}\n`;
      }
    }
    return content;
  },
};

function parseEndpointsData(rawContent: string): DocumentContent | null {
  return parseJsonDataFile(rawContent, ENDPOINTS_PARSER_CONFIG);
}

function parseModelsData(rawContent: string): DocumentContent | null {
  return parseJsonDataFile(rawContent, MODELS_PARSER_CONFIG);
}

/** Critical data files that must be present for full functionality */
const CRITICAL_FILES = new Set([JSON_FILES.ENDPOINTS, JSON_FILES.MODELS]);

/**
 * Detect file type and return parser info.
 */
function getFileParser(
  file: string
): { parser: (raw: string) => DocumentContent | null; context: string } | null {
  if (file.endsWith(FILE_EXTENSIONS.MARKDOWN)) {
    return { parser: (raw) => parseMarkdown(raw, file), context: `markdown file "${file}"` };
  }
  if (file === JSON_FILES.ENDPOINTS) {
    return { parser: parseEndpointsData, context: "endpoints.json" };
  }
  if (file === JSON_FILES.MODELS) {
    return { parser: parseModelsData, context: "models.json" };
  }
  return null;
}

/**
 * Check if file should be tracked for failure reporting.
 */
function isTrackableFile(file: string): boolean {
  return file.endsWith(FILE_EXTENSIONS.MARKDOWN) || file.endsWith(FILE_EXTENSIONS.JSON);
}

/**
 * Load documents from disk, with caching.
 * Documents are loaded once and cached for subsequent searches.
 * Warns about missing critical files to aid debugging.
 */
function loadDocuments(): DocumentContent[] {
  if (documentsCache !== null) {
    return documentsCache;
  }

  const docs: DocumentContent[] = [];
  const failedFiles: string[] = [];
  const loadedFiles = new Set<string>();

  try {
    const files = readdirSync(DATA_DIR);

    for (const file of files) {
      const parserInfo = getFileParser(file);
      if (!parserInfo) continue;

      const filePath = join(DATA_DIR, file);
      const result = loadDocument(filePath, parserInfo.parser, parserInfo.context);

      if (result.doc) {
        docs.push(result.doc);
        loadedFiles.add(file);
      } else if (isTrackableFile(file)) {
        failedFiles.push(file);
      }
    }

    // Warn about critical missing files
    for (const critical of CRITICAL_FILES) {
      if (!loadedFiles.has(critical)) {
        logWarn("loadDocuments", `Critical data file "${critical}" failed to load or is missing`);
      }
    }

    if (failedFiles.length > 0) {
      logWarn("loadDocuments", `${failedFiles.length} file(s) failed to load: ${failedFiles.join(", ")}`);
    }
  } catch (error) {
    // This catch only handles directory enumeration failures (e.g., DATA_DIR doesn't exist)
    // Individual file load/parse errors are handled by loadDocument() and logged separately
    logError("loadDocuments", `Failed to enumerate data directory "${DATA_DIR}"`, error);
  }

  documentsCache = docs;
  return docs;
}

/**
 * Calculate relevance score for content against a query.
 * Higher scores indicate better matches.
 */
function calculateRelevance(content: string, query: string): number {
  const lowerContent = content.toLowerCase();
  const lowerQuery = query.toLowerCase();
  const terms = parseSearchTerms(query);

  let score = 0;

  // Exact phrase match bonus
  if (lowerContent.includes(lowerQuery)) {
    score += SCORING.EXACT_PHRASE_BONUS;
  }

  for (const term of terms) {
    const occurrences = countMatches(content, "term", term);
    if (occurrences > 0) {
      score += occurrences;
      score += countMatches(content, "header", term) * SCORING.HEADER_MATCH_MULTIPLIER;
      score += countMatches(content, "codeBlock", term) * SCORING.CODE_BLOCK_MATCH_MULTIPLIER;
    }
  }

  return score;
}

/**
 * Update best match if the new match has higher priority.
 * Returns the updated best match state.
 */
function updateBestMatch(
  current: { index: number; priority: number },
  candidate: MatchLocation | null
): { index: number; priority: number } {
  if (candidate && candidate.priority > current.priority) {
    return { index: candidate.index, priority: candidate.priority };
  }
  return current;
}

/**
 * Find the best match location across all terms.
 * Prefers header matches over regular text matches.
 */
function findBestMatchLocation(content: string, terms: string[]): number {
  let best = { index: -1, priority: 0 };

  for (const term of terms) {
    // Check headers first (higher priority)
    best = updateBestMatch(best, findMatch(content, term, "header"));

    // Check regular matches only if no better match found yet
    if (best.priority < SCORING.REGULAR_MATCH_PRIORITY) {
      best = updateBestMatch(best, findMatch(content, term, "regular"));
    }
  }

  return best.index;
}

function findLineStart(content: string, index: number): number {
  let start = index;
  while (start > 0 && content[start - 1] !== "\n") {
    start--;
  }
  return start;
}

/**
 * Calculate the optimal start position for a snippet around a match.
 * Prefers starting at line boundaries, falls back to offset from match.
 */
function calculateSnippetStart(content: string, matchIndex: number): number {
  const lineStart = findLineStart(content, matchIndex);

  // If line start is too far back, use a closer offset
  if (matchIndex - lineStart > SNIPPET.LINE_WRAP_THRESHOLD) {
    return Math.max(0, matchIndex - SNIPPET.FALLBACK_OFFSET);
  }
  return lineStart;
}

/**
 * Add ellipsis markers to indicate truncation.
 */
function addEllipsis(snippet: string, hasLeading: boolean, hasTrailing: boolean): string {
  let result = snippet;
  if (hasLeading) result = "..." + result;
  if (hasTrailing) result = result + "...";
  return result;
}

/**
 * Extract a relevant snippet from content around the best match.
 * Limits content size to prevent DoS from large documents.
 */
function extractSnippet(
  content: string,
  query: string,
  maxLength: number = SNIPPET.DEFAULT_MAX_LENGTH
): string {
  // Limit content size to prevent processing huge documents
  const safeContent = content.length > MAX_CONTENT_SIZE ? content.slice(0, MAX_CONTENT_SIZE) : content;

  const terms = parseSearchTerms(query);
  const bestIndex = findBestMatchLocation(safeContent, terms);

  // No match found - return beginning of content
  if (bestIndex === -1) {
    const truncated = safeContent.slice(0, maxLength).trim();
    return addEllipsis(truncated, false, safeContent.length > maxLength);
  }

  const start = calculateSnippetStart(safeContent, bestIndex);
  const end = Math.min(safeContent.length, start + maxLength);
  const snippet = safeContent.slice(start, end).trim();

  return addEllipsis(snippet, start > 0, end < safeContent.length);
}

/**
 * Search bundled documentation for a query.
 * @returns Top matching results sorted by relevance
 */
export function searchDocs(query: string): SearchResult[] {
  // Check length BEFORE trimming to prevent memory exhaustion from whitespace-padded queries
  if (query.length > MAX_QUERY_LENGTH * 2) {
    logWarn("searchDocs", "Query exceeds max length");
    return [];
  }

  const trimmedQuery = query.trim();
  if (!trimmedQuery) {
    logWarn("searchDocs", "Empty query provided");
    return [];
  }
  if (trimmedQuery.length > MAX_QUERY_LENGTH) {
    logWarn("searchDocs", "Query exceeds max length");
    return [];
  }

  const documents = loadDocuments();
  const results: SearchResult[] = [];

  for (const doc of documents) {
    const relevance = calculateRelevance(doc.content, query);
    if (relevance > 0) {
      results.push({
        source: doc.name,
        title: doc.title,
        snippet: extractSnippet(doc.content, query),
        relevance,
      });
    }
  }

  results.sort((a, b) => b.relevance - a.relevance);
  return results.slice(0, MAX_RESULTS);
}
