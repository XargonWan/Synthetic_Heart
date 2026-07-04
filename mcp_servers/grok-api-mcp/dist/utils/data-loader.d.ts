import { z } from "zod";
/** Path to the data directory */
export declare const DATA_DIR: string;
/** Zod schema for endpoint entries - exported for external validation */
export declare const EndpointSchema: z.ZodObject<{
    method: z.ZodString;
    path: z.ZodString;
    description: z.ZodString;
    category: z.ZodString;
    api: z.ZodEnum<{
        inference: "inference";
        management: "management";
    }>;
}, z.core.$strip>;
/** Zod schema for endpoints.json - exported for external validation */
export declare const EndpointsDataSchema: z.ZodObject<{
    endpoints: z.ZodArray<z.ZodObject<{
        method: z.ZodString;
        path: z.ZodString;
        description: z.ZodString;
        category: z.ZodString;
        api: z.ZodEnum<{
            inference: "inference";
            management: "management";
        }>;
    }, z.core.$strip>>;
    apiBaseUrls: z.ZodObject<{
        inference: z.ZodString;
        management: z.ZodString;
    }, z.core.$strip>;
    categories: z.ZodRecord<z.ZodString, z.ZodString>;
}, z.core.$strip>;
/** Zod schema for model entries - exported for external validation */
export declare const ModelSchema: z.ZodObject<{
    id: z.ZodString;
    name: z.ZodString;
    description: z.ZodString;
    contextLength: z.ZodNullable<z.ZodNumber>;
    capabilities: z.ZodArray<z.ZodString>;
    knowledgeCutoff: z.ZodOptional<z.ZodString>;
}, z.core.$strip>;
/** Zod schema for model aliases - exported for external validation */
export declare const ModelAliasesSchema: z.ZodObject<{
    description: z.ZodString;
    formats: z.ZodArray<z.ZodObject<{
        pattern: z.ZodString;
        description: z.ZodString;
    }, z.core.$strip>>;
}, z.core.$strip>;
/** Zod schema for models.json - exported for external validation */
export declare const ModelsDataSchema: z.ZodObject<{
    models: z.ZodArray<z.ZodObject<{
        id: z.ZodString;
        name: z.ZodString;
        description: z.ZodString;
        contextLength: z.ZodNullable<z.ZodNumber>;
        capabilities: z.ZodArray<z.ZodString>;
        knowledgeCutoff: z.ZodOptional<z.ZodString>;
    }, z.core.$strip>>;
    aliases: z.ZodOptional<z.ZodObject<{
        description: z.ZodString;
        formats: z.ZodArray<z.ZodObject<{
            pattern: z.ZodString;
            description: z.ZodString;
        }, z.core.$strip>>;
    }, z.core.$strip>>;
    recommendedModels: z.ZodOptional<z.ZodRecord<z.ZodString, z.ZodUnion<readonly [z.ZodString, z.ZodArray<z.ZodString>]>>>;
}, z.core.$strip>;
/**
 * Load and parse a JSON file from the data directory.
 * Validates against Zod schema if one exists for the file.
 * @param filename - The filename to load (e.g., "models.json")
 * @returns The parsed and validated JSON data
 * @throws Error if file cannot be read, parsed, or fails validation
 */
export declare function loadDataFile<T>(filename: string): T;
