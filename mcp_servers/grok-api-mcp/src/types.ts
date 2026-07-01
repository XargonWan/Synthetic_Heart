/**
 * Shared type definitions for xAI/Grok API data structures.
 * Canonical source - all modules should import from here.
 */

/** API endpoint definition from endpoints.json */
export interface Endpoint {
  method: string;
  path: string;
  description: string;
  category: string;
  api: "inference" | "management";
}

/** Endpoints data file structure */
export interface EndpointsData {
  endpoints: Endpoint[];
  apiBaseUrls: {
    inference: string;
    management: string;
  };
  categories: Record<string, string>;
}

/** Model definition from models.json */
export interface Model {
  id: string;
  name: string;
  description: string;
  contextLength: number | null;
  capabilities: string[];
  knowledgeCutoff?: string;
}

/** Model alias format definition */
export interface AliasFormat {
  pattern: string;
  description: string;
}

/** Model aliases section */
export interface ModelAliases {
  description: string;
  formats: AliasFormat[];
}

/** Models data file structure */
export interface ModelsData {
  models: Model[];
  aliases?: ModelAliases;
  recommendedModels?: Record<string, string | string[]>;
}
