import { getErrorMessage } from "../utils/error-handler.js";
import { logError, logWarn } from "../utils/logger.js";

/** Base URL for xAI documentation */
export const DOCS_BASE_URL = "https://docs.x.ai/docs";

/** Allowed hostname for documentation fetching (SSRF protection) */
const ALLOWED_HOSTNAME = "docs.x.ai";

/** User agent string for fetch requests */
const USER_AGENT = "MCP-Grok-Docs/1.0";

/** Fetch timeout in milliseconds (10 seconds - reduced for security) */
const FETCH_TIMEOUT_MS = 10000;

/** Maximum response size in bytes (5MB) */
const MAX_RESPONSE_SIZE = 5 * 1024 * 1024;

/** Maximum path length to prevent abuse */
const MAX_PATH_LENGTH = 200;

/** HTML entity mappings for decoding (common named entities) */
const HTML_ENTITIES: Record<string, string> = {
  // Basic XML entities
  "&amp;": "&",
  "&lt;": "<",
  "&gt;": ">",
  "&quot;": '"',
  "&#39;": "'",
  "&apos;": "'",
  // Whitespace and typography
  "&nbsp;": " ",
  "&ndash;": "\u2013", // en-dash
  "&mdash;": "\u2014", // em-dash
  "&hellip;": "\u2026", // horizontal ellipsis
  "&lsquo;": "\u2018", // left single quote
  "&rsquo;": "\u2019", // right single quote
  "&ldquo;": "\u201C", // left double quote
  "&rdquo;": "\u201D", // right double quote
  "&bull;": "\u2022", // bullet
  "&middot;": "\u00B7", // middle dot
  // Symbols
  "&copy;": "\u00A9", // copyright
  "&reg;": "\u00AE", // registered
  "&trade;": "\u2122", // trademark
  "&deg;": "\u00B0", // degree
  "&plusmn;": "\u00B1", // plus-minus
  "&times;": "\u00D7", // multiplication
  "&divide;": "\u00F7", // division
  // Currency
  "&cent;": "\u00A2", // cent
  "&pound;": "\u00A3", // pound
  "&euro;": "\u20AC", // euro
  "&yen;": "\u00A5", // yen
  // Arrows
  "&larr;": "\u2190", // left arrow
  "&rarr;": "\u2192", // right arrow
  "&uarr;": "\u2191", // up arrow
  "&darr;": "\u2193", // down arrow
};

/** Pre-compiled regex for named HTML entities (single pass replacement) */
const HTML_ENTITY_PATTERN = new RegExp(
  Object.keys(HTML_ENTITIES).map((e) => e.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|"),
  "g"
);

/** Pattern for numeric HTML entities (decimal and hex) */
const NUMERIC_ENTITY_PATTERN = /&#(\d+);|&#x([0-9a-fA-F]+);/g;

/** Shared type for regex pattern replacement definitions */
type PatternReplacement = Readonly<{ pattern: RegExp; replacement: string }>;

/** Header tag patterns (h1-h6) mapped to markdown equivalents */
const HEADER_PATTERNS: ReadonlyArray<PatternReplacement> = [
  { pattern: /<h1[^>]*>([\s\S]*?)<\/h1>/gi, replacement: "\n# $1\n" },
  { pattern: /<h2[^>]*>([\s\S]*?)<\/h2>/gi, replacement: "\n## $1\n" },
  { pattern: /<h3[^>]*>([\s\S]*?)<\/h3>/gi, replacement: "\n### $1\n" },
  { pattern: /<h4[^>]*>([\s\S]*?)<\/h4>/gi, replacement: "\n#### $1\n" },
  { pattern: /<h5[^>]*>([\s\S]*?)<\/h5>/gi, replacement: "\n##### $1\n" },
  { pattern: /<h6[^>]*>([\s\S]*?)<\/h6>/gi, replacement: "\n###### $1\n" },
];

/** Pre-compiled patterns for script/style removal */
const SCRIPT_PATTERN = /<script[^>]*>[\s\S]*?<\/script>/gi;
const STYLE_PATTERN = /<style[^>]*>[\s\S]*?<\/style>/gi;

/** Pre-compiled patterns for main content extraction */
const MAIN_CONTENT_PATTERNS = [
  /<main[^>]*>([\s\S]*?)<\/main>/i,
  /<article[^>]*>([\s\S]*?)<\/article>/i,
  /<div[^>]*class="[^"]*content[^"]*"[^>]*>([\s\S]*?)<\/div>/i,
];

/** Pre-compiled patterns for code block conversion */
const CODE_BLOCK_PATTERNS: ReadonlyArray<PatternReplacement> = [
  { pattern: /<pre[^>]*><code[^>]*class="[^"]*language-(\w+)[^"]*"[^>]*>([\s\S]*?)<\/code><\/pre>/gi, replacement: "\n```$1\n$2\n```\n" },
  { pattern: /<pre[^>]*><code[^>]*>([\s\S]*?)<\/code><\/pre>/gi, replacement: "\n```\n$1\n```\n" },
  { pattern: /<pre[^>]*>([\s\S]*?)<\/pre>/gi, replacement: "\n```\n$1\n```\n" },
  { pattern: /<code[^>]*>([\s\S]*?)<\/code>/gi, replacement: "`$1`" },
];

/** Pre-compiled pattern for link conversion */
const LINK_PATTERN = /<a[^>]*href="([^"]*)"[^>]*>([\s\S]*?)<\/a>/gi;

/** Pre-compiled patterns for list conversion */
const LIST_PATTERNS: ReadonlyArray<PatternReplacement> = [
  { pattern: /<ul[^>]*>/gi, replacement: "\n" },
  { pattern: /<\/ul>/gi, replacement: "\n" },
  { pattern: /<ol[^>]*>/gi, replacement: "\n" },
  { pattern: /<\/ol>/gi, replacement: "\n" },
  { pattern: /<li[^>]*>([\s\S]*?)<\/li>/gi, replacement: "- $1\n" },
];

/** Pre-compiled patterns for text formatting conversion */
const TEXT_FORMAT_PATTERNS: ReadonlyArray<PatternReplacement> = [
  { pattern: /<p[^>]*>([\s\S]*?)<\/p>/gi, replacement: "\n$1\n" },
  { pattern: /<strong[^>]*>([\s\S]*?)<\/strong>/gi, replacement: "**$1**" },
  { pattern: /<b[^>]*>([\s\S]*?)<\/b>/gi, replacement: "**$1**" },
  { pattern: /<em[^>]*>([\s\S]*?)<\/em>/gi, replacement: "*$1*" },
  { pattern: /<i[^>]*>([\s\S]*?)<\/i>/gi, replacement: "*$1*" },
  { pattern: /<br\s*\/?>/gi, replacement: "\n" },
  { pattern: /<blockquote[^>]*>([\s\S]*?)<\/blockquote>/gi, replacement: "\n> $1\n" },
];

/** Pre-compiled patterns for markdown cleanup */
const REMAINING_HTML_PATTERN = /<[^>]+>/g;
const MULTIPLE_NEWLINES_PATTERN = /\n{3,}/g;

/** Pattern for validating path segments (alphanumeric, hyphens, underscores) */
const VALID_SEGMENT_PATTERN = /^[a-zA-Z0-9\-_]+$/;

/**
 * Validate and sanitize documentation path to prevent path traversal and injection attacks.
 * @throws Error if path is invalid
 * @returns Sanitized path segments joined with /
 */
function validatePath(path: string): string {
  // Length check first to prevent processing huge inputs
  if (path.length > MAX_PATH_LENGTH) {
    throw new Error("Invalid path: too long");
  }

  // Early check for empty or whitespace-only paths
  if (!path || !path.trim()) {
    throw new Error("Invalid path: empty path");
  }

  // Decode any URL encoding to catch bypass attempts like %252e (double-encoded)
  let decoded = path;
  try {
    // Keep decoding until stable to catch multi-level encoding
    let prev = "";
    let iterations = 0;
    while (decoded !== prev && iterations < 3) {
      prev = decoded;
      decoded = decodeURIComponent(decoded);
      iterations++;
    }
  } catch {
    // decodeURIComponent throws on malformed sequences - reject them
    throw new Error("Invalid path: malformed encoding");
  }

  // Block path traversal attempts (after decoding)
  if (decoded.includes("..")) {
    throw new Error("Invalid path: path traversal not allowed");
  }

  // Block null bytes and other control characters
  if (/[\x00-\x1f\x7f]/.test(decoded)) {
    throw new Error("Invalid path: control characters not allowed");
  }

  // Split into segments and validate each
  const segments = decoded.split("/").filter((s) => s.length > 0);
  if (segments.length === 0) {
    throw new Error("Invalid path: empty path");
  }

  // Reject segments that are . or .. (extra safety)
  if (segments.some((s) => s === "." || s === "..")) {
    throw new Error("Invalid path: relative path not allowed");
  }

  // Validate allowed characters on each segment
  if (!segments.every((s) => VALID_SEGMENT_PATTERN.test(s))) {
    throw new Error("Invalid path: contains disallowed characters");
  }

  return segments.join("/");
}

/**
 * Fetch documentation page from docs.x.ai and convert to markdown.
 * @param path - Documentation path (e.g., "guides/function-calling")
 * @returns Markdown content
 */
export async function getDocPage(path: string): Promise<string> {
  const normalizedPath = path.replace(/^\/+/, "");

  // Validate and sanitize path before constructing URL
  const sanitizedPath = validatePath(normalizedPath);

  // Construct and validate URL
  const url = new URL(`${DOCS_BASE_URL}/${sanitizedPath}`);

  // SSRF protection: verify hostname matches expected domain
  if (url.hostname !== ALLOWED_HOSTNAME) {
    throw new Error("Invalid documentation URL");
  }

  // Verify protocol is HTTPS
  if (url.protocol !== "https:") {
    throw new Error("Only HTTPS is allowed");
  }

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);

  try {
    const response = await fetch(url.toString(), {
      headers: {
        "User-Agent": USER_AGENT,
        Accept: "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
      },
      signal: controller.signal,
      redirect: "error", // Don't follow redirects to prevent SSRF via redirect
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    // Check Content-Length header if present
    const contentLength = response.headers.get("content-length");
    if (contentLength && parseInt(contentLength, 10) > MAX_RESPONSE_SIZE) {
      throw new Error("Response too large");
    }

    // Read body with size limit
    const reader = response.body?.getReader();
    if (!reader) {
      throw new Error("No response body");
    }

    const chunks: Uint8Array[] = [];
    let totalSize = 0;

    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        totalSize += value.length;
        if (totalSize > MAX_RESPONSE_SIZE) {
          reader.cancel();
          throw new Error("Response too large");
        }
        chunks.push(value);
      }
    } finally {
      reader.releaseLock();
    }

    const html = new TextDecoder().decode(
      chunks.reduce((acc, chunk) => {
        const merged = new Uint8Array(acc.length + chunk.length);
        merged.set(acc);
        merged.set(chunk, acc.length);
        return merged;
      }, new Uint8Array())
    );

    return htmlToMarkdown(html);
  } catch (error) {
    if (error instanceof Error && error.name === "AbortError") {
      throw new Error("Request timed out");
    }
    // Don't leak internal error details
    const message = error instanceof Error ? error.message : "Unknown error";
    // Only pass through safe error messages
    if (message.startsWith("HTTP ") || message === "Response too large" || message === "Request timed out") {
      throw new Error(message);
    }
    throw new Error("Failed to fetch documentation");
  } finally {
    clearTimeout(timeoutId);
  }
}

/**
 * Check if a Unicode codepoint is safe to decode.
 * Rejects surrogates, control characters (except common whitespace), and invalid ranges.
 */
function isSafeCodePoint(codePoint: number): boolean {
  // Invalid or out of range
  if (codePoint < 0 || codePoint > 0x10ffff) return false;
  // Surrogate pairs (invalid as standalone)
  if (codePoint >= 0xd800 && codePoint <= 0xdfff) return false;
  // NULL byte
  if (codePoint === 0) return false;
  // Control characters except tab, newline, carriage return
  if (codePoint < 32 && codePoint !== 9 && codePoint !== 10 && codePoint !== 13) return false;
  // DEL and C1 control characters
  if (codePoint >= 0x7f && codePoint <= 0x9f) return false;
  return true;
}

function decodeHtmlEntities(text: string): string {
  // Decode named entities
  let result = text.replace(HTML_ENTITY_PATTERN, (match) => HTML_ENTITIES[match] || match);
  // Decode numeric entities (decimal &#123; and hex &#x7B;) with validation
  result = result.replace(NUMERIC_ENTITY_PATTERN, (match, decimal, hex) => {
    const codePoint = decimal ? parseInt(decimal, 10) : parseInt(hex, 16);
    // Validate codepoint before converting
    if (!isSafeCodePoint(codePoint)) {
      return ""; // Remove invalid entities
    }
    try {
      return String.fromCodePoint(codePoint);
    } catch {
      return ""; // Remove if conversion fails
    }
  });
  return result;
}

function removeScriptsAndStyles(html: string): string {
  return html.replace(SCRIPT_PATTERN, "").replace(STYLE_PATTERN, "");
}

function extractMainContent(html: string): string {
  for (const pattern of MAIN_CONTENT_PATTERNS) {
    const match = html.match(pattern);
    if (match) {
      return match[1];
    }
  }
  return html;
}

/** Apply a list of pattern replacements to content */
function applyPatterns(
  content: string,
  patterns: ReadonlyArray<{ pattern: RegExp; replacement: string }>
): string {
  return patterns.reduce(
    (result, { pattern, replacement }) => result.replace(pattern, replacement),
    content
  );
}

function cleanupMarkdown(content: string): string {
  return content
    .replace(REMAINING_HTML_PATTERN, "")
    .replace(MULTIPLE_NEWLINES_PATTERN, "\n\n")
    .trim();
}

/** Tracks failed transform steps for degradation reporting */
interface TransformContext {
  failedSteps: string[];
}

/**
 * Safely apply a transform function, returning original content on error.
 * Provides error isolation in the HTML-to-Markdown pipeline.
 * Tracks failed steps in context for degradation reporting.
 */
function safeTransform(
  content: string,
  transform: (s: string) => string,
  stepName: string,
  context: TransformContext
): string {
  try {
    return transform(content);
  } catch (error) {
    logError("safeTransform", `Transform "${stepName}" failed`, error);
    context.failedSteps.push(stepName);
    return content;
  }
}

/** Pipeline step definition for HTML-to-Markdown conversion */
type TransformStep = Readonly<{
  name: string;
  transform: (content: string) => string;
}>;

/**
 * HTML-to-Markdown conversion pipeline steps.
 * Order matters: cleanup steps come first, formatting steps follow, final cleanup last.
 * Each step is isolated via safeTransform - a failure in one step won't prevent others.
 */
const TRANSFORM_PIPELINE: ReadonlyArray<TransformStep> = [
  { name: "removeScriptsAndStyles", transform: removeScriptsAndStyles },
  { name: "extractMainContent", transform: extractMainContent },
  { name: "convertHeaders", transform: (c) => applyPatterns(c, HEADER_PATTERNS) },
  { name: "convertCodeBlocks", transform: (c) => applyPatterns(c, CODE_BLOCK_PATTERNS) },
  { name: "convertLinks", transform: (c) => c.replace(LINK_PATTERN, "[$2]($1)") },
  { name: "convertLists", transform: (c) => applyPatterns(c, LIST_PATTERNS) },
  { name: "convertTextFormatting", transform: (c) => applyPatterns(c, TEXT_FORMAT_PATTERNS) },
  { name: "decodeHtmlEntities", transform: decodeHtmlEntities },
  { name: "cleanupMarkdown", transform: cleanupMarkdown },
];

/** Critical transform steps that warrant extra warning if they fail */
const CRITICAL_TRANSFORMS = new Set(["removeScriptsAndStyles", "cleanupMarkdown"]);

/**
 * HTML-to-Markdown conversion pipeline with error boundaries.
 * Each step is isolated - a failure in one step won't prevent other transforms.
 * Returns content with optional degradation warning if any transforms failed.
 */
function htmlToMarkdown(html: string): string {
  const context: TransformContext = { failedSteps: [] };

  let content = html;
  for (const step of TRANSFORM_PIPELINE) {
    content = safeTransform(content, step.transform, step.name, context);
  }

  // Log and warn about failed transforms
  if (context.failedSteps.length > 0) {
    const criticalFailed = context.failedSteps.filter((s) => CRITICAL_TRANSFORMS.has(s));
    if (criticalFailed.length > 0) {
      logWarn(
        "htmlToMarkdown",
        `Critical transforms failed: ${criticalFailed.join(", ")}. Output may contain unprocessed HTML.`
      );
    }
    const warning = `> **Warning**: Some formatting may be incomplete (failed: ${context.failedSteps.join(", ")})\n\n`;
    content = warning + content;
  }

  return content;
}
