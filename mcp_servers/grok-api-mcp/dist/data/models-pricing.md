# Models and Pricing

Comprehensive overview of Grok models and their pricing.

## Knowledge Cutoff

The knowledge cutoff date for Grok 3 and Grok 4 is **November 2024**.

To get real-time information, enable server-side search tools (Web Search / X Search).

## Model Pricing

### Grok 4

| Token Type | Price per Million |
|------------|-------------------|
| Input tokens | $2.00 |
| Output tokens | $10.00 |
| Cached input tokens | $0.50 |
| Reasoning tokens | $10.00 |

**Context**: 2,000,000 tokens

### Grok 4 Fast

| Token Type | Price per Million |
|------------|-------------------|
| Input tokens | $0.20 |
| Output tokens | $0.50 |
| Cached input tokens | $0.05 |

**Context**: 2,000,000 tokens

### Grok 4.1 Fast (Reasoning)

| Token Type | Price per Million |
|------------|-------------------|
| Input tokens | $0.20 |
| Output tokens | $0.50 |
| Reasoning tokens | $0.50 |

**Context**: 131,072 tokens

### Grok 4.1 Fast (Non-Reasoning)

| Token Type | Price per Million |
|------------|-------------------|
| Input tokens | $0.10 |
| Output tokens | $0.25 |

**Context**: 131,072 tokens

### Grok Code Fast 1

| Token Type | Price per Million |
|------------|-------------------|
| Input tokens | $0.10 |
| Output tokens | $0.25 |

**Context**: 256,000 tokens

### Grok 3

| Token Type | Price per Million |
|------------|-------------------|
| Input tokens | $0.30 |
| Output tokens | $1.00 |

**Context**: 131,072 tokens

### Grok 3 Mini

| Token Type | Price per Million |
|------------|-------------------|
| Input tokens | $0.10 |
| Output tokens | $0.25 |

**Context**: 131,072 tokens

## Image Generation Pricing

### Grok Imagine Image

| Quality | Price per Image |
|---------|-----------------|
| Standard | $0.02 |
| HD | $0.04 |

## Video Generation Pricing

Pricing varies by duration and quality. Contact xAI for details.

## Tool Pricing

### Live Search

| Resource | Price |
|----------|-------|
| Per source used | $0.025 |
| Per 1,000 sources | $25.00 |

### Code Execution

Included with model usage, no additional charge.

## Token Types Explained

### Prompt Tokens
Input text sent to the model.

### Completion Tokens
Generated output text.

### Reasoning Tokens
Tokens used for model reasoning (Grok 4 reasoning models only).

### Cached Tokens
Previously processed tokens served from cache at reduced rate.

### Audio Tokens
Tokens for audio processing (Voice API).

### Image Tokens
Tokens for image processing (image understanding).

## Model Aliases

| Alias Format | Description |
|--------------|-------------|
| `<model>` | Latest stable version |
| `<model>-latest` | Latest version with newest features |
| `<model>-<date>` | Specific release (e.g., grok-4-0709) |

## Beta Models

Some models are available in beta:
- `grok-3-mini-fast-beta`
- `grok-3-fast-beta`

Beta models may have different pricing or availability.

## Cost Optimization Tips

### 1. Use Cached Tokens
Repeated prompts benefit from caching at reduced rates.

### 2. Choose Appropriate Models
- Simple tasks → grok-4-1-fast-non-reasoning (cheapest)
- Complex reasoning → grok-4 (most capable)
- Coding → grok-code-fast-1 (optimized)

### 3. Optimize Prompts
- Be concise
- Avoid redundant context
- Use system prompts efficiently

### 4. Monitor Usage
Use Usage Explorer to track costs and optimize.

## Billing

### Prepaid Credits
- Pre-purchase credits
- No overage until exhausted
- Credits don't expire

### Monthly Invoicing
- Billed at end of month
- Based on actual usage

## Rate Limits

Rate limits vary by tier:
- Requests per minute (RPM)
- Tokens per minute (TPM)

Contact support@x.ai for higher limits.
