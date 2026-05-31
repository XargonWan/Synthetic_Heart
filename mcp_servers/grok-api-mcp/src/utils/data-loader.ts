import { readFileSync } from "fs";
import { fileURLToPath } from "url";
import { dirname, join } from "path";
import { z } from "zod";
import { getErrorMessage } from "./error-handler.js";
import { parseJSON } from "./format.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

/** Path to the data directory */
export const DATA_DIR = join(__dirname, "..", "data");

/** Zod schema for endpoint entries - exported for external validation */
export const EndpointSchema = z.object({
  method: z.string(),
  path: z.string(),
  description: z.string(),
  category: z.string(),
  api: z.enum(["inference", "management"]),
});

/** Zod schema for endpoints.json - exported for external validation */
export const EndpointsDataSchema = z.object({
  endpoints: z.array(EndpointSchema),
  apiBaseUrls: z.object({
    inference: z.string(),
    management: z.string(),
  }),
  categories: z.record(z.string(), z.string()),
});

/** Zod schema for model entries - exported for external validation */
export const ModelSchema = z.object({
  id: z.string(),
  name: z.string(),
  description: z.string(),
  contextLength: z.number().nullable(),
  capabilities: z.array(z.string()),
  knowledgeCutoff: z.string().optional(),
});

/** Zod schema for model aliases - exported for external validation */
export const ModelAliasesSchema = z.object({
  description: z.string(),
  formats: z.array(
    z.object({
      pattern: z.string(),
      description: z.string(),
    })
  ),
});

/** Zod schema for models.json - exported for external validation */
export const ModelsDataSchema = z.object({
  models: z.array(ModelSchema),
  aliases: ModelAliasesSchema.optional(),
  recommendedModels: z.record(z.string(), z.union([z.string(), z.array(z.string())])).optional(),
});

/** Schema registry for known data files */
const DATA_SCHEMAS: Record<string, z.ZodType> = {
  "endpoints.json": EndpointsDataSchema,
  "models.json": ModelsDataSchema,
};

/** Whitelist of allowed data files for security */
const ALLOWED_DATA_FILES = new Set(["endpoints.json", "models.json"]);

/** Pattern for valid filenames (alphanumeric, hyphens, underscores, single dot for extension) */
const VALID_FILENAME_PATTERN = /^[a-zA-Z0-9_-]+\.[a-zA-Z0-9]+$/;

/**
 * Load and parse a JSON file from the data directory.
 * Validates against Zod schema if one exists for the file.
 * @param filename - The filename to load (e.g., "models.json")
 * @returns The parsed and validated JSON data
 * @throws Error if file cannot be read, parsed, or fails validation
 */
export function loadDataFile<T>(filename: string): T {
  // Security: validate filename to prevent path traversal
  if (!VALID_FILENAME_PATTERN.test(filename)) {
    throw new Error("Invalid filename");
  }

  // Security: only allow whitelisted files
  if (!ALLOWED_DATA_FILES.has(filename)) {
    throw new Error("File not allowed");
  }

  const dataPath = join(DATA_DIR, filename);
  try {
    const content = readFileSync(dataPath, "utf-8");
    const parsed = parseJSON<unknown>(content, filename);
    if (parsed === null) {
      throw new Error("JSON parsing failed");
    }

    // Validate against schema if one exists
    const schema = DATA_SCHEMAS[filename];
    if (schema) {
      const result = schema.safeParse(parsed);
      if (!result.success) {
        // Don't leak detailed schema errors to callers
        throw new Error("Schema validation failed");
      }
      return result.data as T;
    }

    // No schema registered - this shouldn't happen with whitelist
    return parsed as T;
  } catch (error) {
    // Don't leak internal paths in error messages
    if (error instanceof Error && error.message.includes("ENOENT")) {
      throw new Error("Data file not found");
    }
    throw new Error("Failed to load data file");
  }
}
