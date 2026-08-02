import { loadDataFile } from "../utils/data-loader.js";
import { formatContextLength, formatMarkdownTable, formatModelValue } from "../utils/format.js";
/** Display labels for use case keys in recommendedModels */
const USE_CASE_LABELS = {
    complexReasoning: "Complex Reasoning",
    balanced: "Balanced Speed/Quality",
    highThroughput: "High Throughput",
    coding: "Coding Tasks",
    agentic: "Agentic/Tool Calling",
    imageGeneration: "Image Generation",
    documentUnderstanding: "Document Understanding",
};
/** Returns full models data including aliases and recommendations */
export function getModelsData() {
    return loadDataFile("models.json");
}
function formatModelSummaryTable(models) {
    const headers = ["Model ID", "Name", "Context", "Capabilities"];
    const rows = models.map((model) => [
        `\`${model.id}\``,
        model.name,
        formatContextLength(model.contextLength),
        model.capabilities.join(", "),
    ]);
    return formatMarkdownTable(headers, rows);
}
function formatModelDetails(models) {
    return [
        "",
        "## Model Details",
        ...models.flatMap((model) => [
            "",
            `### ${model.name}`,
            `**ID**: \`${model.id}\``,
            "",
            model.description,
            ...(model.knowledgeCutoff ? ["", `**Knowledge Cutoff**: ${model.knowledgeCutoff}`] : []),
        ]),
    ];
}
function formatAliasesSection(aliases) {
    if (!aliases)
        return [];
    return [
        "",
        "## Model Aliases",
        "",
        aliases.description,
        "",
        ...aliases.formats.map((format) => `- \`${format.pattern}\`: ${format.description}`),
    ];
}
function formatRecommendationsSection(recommendedModels) {
    if (!recommendedModels)
        return [];
    return [
        "",
        "## Recommended Models by Use Case",
        "",
        ...Object.entries(recommendedModels).map(([useCase, modelValue]) => {
            const label = USE_CASE_LABELS[useCase] || useCase;
            return `- **${label}**: ${formatModelValue(modelValue, { markdown: true })}`;
        }),
    ];
}
export function formatModelsTable(data) {
    const { models, aliases, recommendedModels } = data;
    if (models.length === 0) {
        return "No models found.";
    }
    const sections = [
        formatModelSummaryTable(models),
        ...formatModelDetails(models),
        ...formatAliasesSection(aliases),
        ...formatRecommendationsSection(recommendedModels),
    ];
    return sections.join("\n");
}
