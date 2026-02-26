Gemini 3.1 Pro is the next iteration of performance, behavior, and intelligence improvements in the 3 Pro family. It distills the core intelligence from Gemini 3 Deep Think into the standard Pro tier, delivering more than double the reasoning performance of 3 Pro on ARC-AGI-2, with 40-60% relative improvement on complex planning tasks. It is designed for tasks where a simple answer isn't enough — ambitious agentic workflows, autonomous coding, and complex multimodal reasoning.

[Try Gemini 3.1 Pro](https://aistudio.google.com?model=gemini-3.1-pro-preview)

Get started with a few lines of code:

### Python

    from google import genai

    client = genai.Client()

    response = client.models.generate_content(
        model="gemini-3.1-pro-preview",
        contents="Analyze this multi-file codebase and identify all race conditions.",
    )

    print(response.text)

### JavaScript

    import { GoogleGenAI } from "@google/genai";

    const ai = new GoogleGenAI({});

    async function run() {
      const response = await ai.models.generateContent({
        model: "gemini-3.1-pro-preview",
        contents: "Analyze this multi-file codebase and identify all race conditions.",
      });

      console.log(response.text);
    }

    run();

### REST

    curl "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-pro-preview:generateContent" \
      -H "x-goog-api-key: $GEMINI_API_KEY" \
      -H 'Content-Type: application/json' \
      -X POST \
      -d '{
        "contents": [{
          "parts": [{"text": "Analyze this multi-file codebase and identify all race conditions."}]
        }]
      }'

## Model overview

Gemini 3.1 Pro Preview is currently the only 3.1 variant. No Flash, Ultra, or
image generation variant has been announced.

| Model ID | Context Window (In / Out) | Knowledge Cutoff | Pricing (Input / Output)\* |
|---|---|---|---|
| **gemini-3.1-pro-preview** | 1M / 64k | Jan 2025 | $2 / $12 (\<200k tokens) $4 / $18 (\>200k tokens) |
| **gemini-3.1-pro-preview-customtools** | 1M / 64k | Jan 2025 | $2 / $12 (\<200k tokens) $4 / $18 (\>200k tokens) |

*\* Pricing is per 1 million tokens. Output pricing includes thinking tokens.*

**Batch API pricing (50% discount):**

| Model ID | Input | Output |
|---|---|---|
| gemini-3.1-pro-preview | $1 / $2 (\<200k / \>200k) | $6 / $9 (\<200k / \>200k) |

**Context caching:** $0.20 per 1M tokens (\<200k), $0.40 (\>200k). Storage: $4.50 per 1M tokens per hour.

**Grounding with Google Search:** 5,000 prompts/month free, then $14 / 1,000 search queries.

There is no free tier for Gemini 3.1 Pro Preview.

### The customtools endpoint

`gemini-3.1-pro-preview-customtools` is a specialized endpoint optimized for
prioritizing custom tools like `view_file` or `search_code`. Use it if you are
building agentic workflows with a mix of bash and custom tools and the base
model ignores your custom tools in favor of bash commands.

Both endpoints share the same context window, pricing, and knowledge cutoff.

## Benchmarks

Gemini 3.1 Pro shows significant improvements over Gemini 3 Pro, especially in
reasoning and agentic tasks:

| Benchmark | Task Type | Gemini 3.1 Pro | Notes |
|---|---|---|---|
| ARC-AGI-2 | Abstract reasoning | 77.1% | 2x reasoning over 3 Pro |
| GPQA Diamond | Graduate-level science | 94.3% | |
| SWE-Bench Verified | Software engineering | 80.6% | |
| SWE-Bench Pro | Diverse coding | 54.2% | |
| Terminal-Bench 2.0 | Agentic terminal coding | 68.5% | |
| LiveCodeBench Pro | Competitive coding | 2887 Elo | |
| Humanity's Last Exam (tools) | Academic reasoning | 51.4% | |
| MMMLU | Multilingual Q&A | 92.6% | |
| MRCR v2 (128k) | Long context | 84.9% | |
| MRCR v2 (1M) | Long context | 26.3% | |

40-60% relative improvement over Gemini 2.5 Pro on complex planning tasks.
Superior results on internal agentic suites measuring tool-use correctness over
50+ sequential steps.

## New API features in Gemini 3.1

### Thinking level: medium for Pro

Gemini 3.1 Pro now supports the `medium` thinking level, which was previously
only available on Flash. This provides a better cost/speed/performance balance
for tasks that don't need full `high` reasoning depth.

**Gemini 3.1 Pro thinking levels:**

- `low`: Minimizes latency and cost. Best for simple instruction following, chat, or high-throughput applications.
- `medium` (NEW for Pro): Balanced thinking for most tasks. Good trade-off between reasoning quality and response time.
- `high` (Default, dynamic): Maximizes reasoning depth. Best for complex reasoning, coding, and agentic workflows.

### Python

    from google import genai
    from google.genai import types

    client = genai.Client()

    response = client.models.generate_content(
        model="gemini-3.1-pro-preview",
        contents="Summarize the key points of this document.",
        config=types.GenerateContentConfig(
            thinking_config=types.ThinkingConfig(thinking_level="medium")
        ),
    )

    print(response.text)

### JavaScript

    import { GoogleGenAI } from "@google/genai";

    const ai = new GoogleGenAI({});

    const response = await ai.models.generateContent({
        model: "gemini-3.1-pro-preview",
        contents: "Summarize the key points of this document.",
        config: {
          thinkingConfig: {
            thinkingLevel: "medium",
          }
        },
      });

    console.log(response.text);

### REST

    curl "https://generativelanguage.googleapis.com/v1beta/models/gemini-3.1-pro-preview:generateContent" \
      -H "x-goog-api-key: $GEMINI_API_KEY" \
      -H 'Content-Type: application/json' \
      -X POST \
      -d '{
        "contents": [{
          "parts": [{"text": "Summarize the key points of this document."}]
        }],
        "generationConfig": {
          "thinkingConfig": {
            "thinkingLevel": "medium"
          }
        }
      }'

| **Important:** You cannot use both `thinking_level` and the legacy `thinking_budget` parameter in the same request. Doing so will return a 400 error.

### Streaming function call arguments

Set `streamFunctionCallArguments: true` in `functionCallingConfig` to stream
partial function call arguments as they are generated, rather than waiting for
the complete argument payload. This reduces perceived latency for tool use in
agentic workflows.

### Python

    from google import genai
    from google.genai import types

    client = genai.Client()

    tool = types.Tool(function_declarations=[
        types.FunctionDeclaration(
            name="search_code",
            description="Search the codebase for a pattern.",
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search pattern"}
                },
                "required": ["query"],
            },
        )
    ])

    response = client.models.generate_content(
        model="gemini-3.1-pro-preview",
        contents="Find all usages of the deprecated API.",
        config=types.GenerateContentConfig(
            tools=[tool],
            function_calling_config=types.FunctionCallingConfig(
                stream_function_call_arguments=True
            ),
        ),
    )

### Multimodal function responses

Function responses can now include images and PDFs in addition to text. This
allows tools to return rich multimodal data that the model can reason over. See
the [Gemini 3 documentation](gemini-3.md#multimodal-function-responses) for
full code examples across Python, JavaScript, and REST.

### File upload limit increase

The API file upload limit has been increased from 20MB to 100MB.

### YouTube URL as media source

You can now pass a YouTube URL directly as a media source in your prompts. The
model will analyze the video content via the URL. This works with public,
pre-recorded videos only — not private, unlisted, or live streams. Free-tier
limit is 8 hours of YouTube video per day.

### Cloud Storage and pre-signed URL support

Cloud Storage bucket paths and private database pre-signed URLs are now
supported as direct data sources.

### Combined hosted tools + structured outputs

You can now combine [Grounding with Google Search](https://ai.google.dev/gemini-api/docs/google-search)
and [URL Context](https://ai.google.dev/gemini-api/docs/url-context) with
[Structured Outputs](https://ai.google.dev/gemini-api/docs/structured-output)
(JSON schema). This is especially powerful for building agents that need to
fetch live information from the web and return it in a structured format.

## Thought signatures

Thought signatures remain **mandatory** for Gemini 3.1 Pro, identical to
Gemini 3 Pro. Missing signatures in function calling will result in a 400
error. The SDKs (Python, Node, Java) handle signatures automatically in
standard chat history workflows.

See [Thought Signatures](thought-signatures.md) for full details.

## Capabilities and limitations

**Supported:**

| Feature | Status |
|---|---|
| Text generation | Supported |
| Image understanding | Supported |
| Audio understanding | Supported |
| Video understanding | Supported |
| PDF processing (up to 1,000 pages) | Supported |
| Function calling | Supported |
| Code execution | Supported |
| Google Search grounding | Supported |
| URL Context | Supported |
| File Search (AI Studio only) | Supported |
| Structured outputs | Supported |
| Context caching | Supported |
| Batch API | Supported |
| Computer Use | Supported |

**NOT supported:**

| Feature | Status |
|---|---|
| Live API | Not supported (use `gemini-2.5-flash-native-audio` models) |
| Audio generation | Not supported |
| Image generation | Not supported (use `gemini-3-pro-image-preview`) |
| Google Maps grounding | Not supported |

**Input modalities:** Text, Image, Audio, Video, PDF

**Output modalities:** Text only

## Breaking changes

- **Interactions API:** `total_reasoning_tokens` has been renamed to `total_thought_tokens`. Update any code that references this field.
- **`thinking_level` vs `thinking_budget`:** Cannot be combined in the same request. Migrate to `thinking_level` for more predictable performance.

## Safety evaluations

Compared to Gemini 3 Pro, safety metrics show minimal changes:

| Category | Change vs 3 Pro |
|---|---|
| Text-to-text safety | +0.10% improvement |
| Multilingual safety | +0.11% improvement |
| Image-to-text safety | -0.33% (minor regression) |
| Tone | +0.02% improvement |
| Unjustified refusals | -0.08% (fewer false refusals) |

All frontier safety critical capability levels (CBRN, Cyber, Harmful
manipulation, ML R&D, Misalignment) remain below alert thresholds.

## Deprecation notices (concurrent with 3.1 launch)

**Shutting down June 1, 2026:**
- `gemini-2.0-flash`
- `gemini-2.0-flash-001`
- `gemini-2.0-flash-lite`
- `gemini-2.0-flash-lite-001`

**Already shut down (February 17, 2026):**
- `gemini-2.5-flash-preview-09-25`
- `imagen-4.0-generate-preview-06-06`
- `imagen-4.0-ultra-generate-preview-06-06`

**Model alias updates (January 21, 2026):**
- `gemini-pro-latest` now routes to `gemini-3-pro-preview`

## Migrating from Gemini 3 Pro

Gemini 3.1 Pro is a drop-in replacement for 3 Pro in most use cases:

- **Model ID:** Change `gemini-3-pro-preview` to `gemini-3.1-pro-preview`.
- **Thinking:** Try `medium` thinking level for tasks that don't need full reasoning depth — better cost/latency trade-off.
- **Custom tools:** If the model ignores your custom tools in favor of bash, try `gemini-3.1-pro-preview-customtools`.
- **Interactions API:** Update `total_reasoning_tokens` references to `total_thought_tokens`.
- **Everything else:** Pricing, context window, thought signatures, media resolution, and tool support are identical to 3 Pro.

## SDK requirements

- Use the `google-genai` SDK (v1.51.0 or later). The old `google-generativeai` package is deprecated.
- Latest SDK: v1.64.0 (released February 19, 2026).
- Install: `pip install -U google-genai` (or `uv add google-genai`).

## Official sources

- [Gemini 3.1 Pro announcement](https://blog.google/innovation-and-ai/models-and-research/gemini-models/gemini-3-1-pro/)
- [Model card](https://deepmind.google/models/model-cards/gemini-3-1-pro/)
- [API changelog](https://ai.google.dev/gemini-api/docs/changelog)
- [Pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Models overview](https://ai.google.dev/gemini-api/docs/models)
- [Gemini 3 series documentation](https://ai.google.dev/gemini-api/docs/gemini-3)
