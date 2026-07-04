# Migrating to New Models

Guide for migrating to the latest Grok models and endpoints.

## Model Aliases

xAI provides model aliases to help with automatic migration:

| Alias Format | Description |
|--------------|-------------|
| `<modelname>` | Latest stable version |
| `<modelname>-latest` | Latest version (may include new features) |
| `<modelname>-<date>` | Specific model release |

### Examples

```python
# Uses latest stable grok-4
model = "grok-4"

# Uses latest version with newest features
model = "grok-4-latest"

# Uses specific release
model = "grok-4-0709"
```

## Migrating from Grok 3 to Grok 4

### Key Differences

| Feature | Grok 3 | Grok 4 |
|---------|--------|--------|
| Context length | 131K | 2M |
| Reasoning | Basic | Advanced (always on) |
| Knowledge cutoff | Nov 2024 | Nov 2024 |
| `reasoning_effort` | Supported | Not supported |
| `presencePenalty` | Supported | Not supported |
| `frequencyPenalty` | Supported | Not supported |

### Code Changes

**Before (Grok 3)**:
```python
response = client.chat.completions.create(
    model="grok-3",
    messages=[{"role": "user", "content": "Hello"}],
    presence_penalty=0.5,
    frequency_penalty=0.5
)
```

**After (Grok 4)**:
```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[{"role": "user", "content": "Hello"}]
    # Remove presence_penalty and frequency_penalty
)
```

## Migrating to Responses API

The Responses API is the preferred method for new integrations.

### From Chat Completions

**Before**:
```python
response = client.chat.completions.create(
    model="grok-4",
    messages=[
        {"role": "system", "content": "You are helpful."},
        {"role": "user", "content": "Hello"}
    ]
)
result = response.choices[0].message.content
```

**After**:
```python
response = client.responses.create(
    model="grok-4",
    instructions="You are helpful.",
    input="Hello"
)
result = response.output_text
```

## Fast Model Variants

### Choosing the Right Variant

| Model | Use Case |
|-------|----------|
| `grok-4` | Complex reasoning, highest quality |
| `grok-4-fast` | Balanced speed and quality |
| `grok-4-1-fast-reasoning` | Agentic tasks with reasoning |
| `grok-4-1-fast-non-reasoning` | High throughput, simple tasks |

### Migration Example

```python
# For simple tasks, switch to fast variant
response = client.chat.completions.create(
    model="grok-4-1-fast-non-reasoning",  # Was: grok-4
    messages=[{"role": "user", "content": "Summarize this text..."}]
)
```

## Handling Deprecations

### Check for Deprecation Warnings

```python
import warnings

def check_model_status(model_name):
    models = client.models.list()
    for model in models.data:
        if model.id == model_name:
            if hasattr(model, 'deprecated') and model.deprecated:
                warnings.warn(f"Model {model_name} is deprecated")
            return model
    return None
```

### Gradual Migration

```python
def get_model(task_type):
    """Returns appropriate model based on task and migration status."""
    models = {
        "complex": "grok-4",
        "standard": "grok-4-fast",
        "simple": "grok-4-1-fast-non-reasoning",
        "code": "grok-code-fast-1"
    }
    return models.get(task_type, "grok-4")
```

## Testing Migration

### A/B Testing

```python
import random

def compare_models(prompt, models=["grok-3", "grok-4"]):
    results = {}
    for model in models:
        response = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}]
        )
        results[model] = {
            "response": response.choices[0].message.content,
            "tokens": response.usage.total_tokens
        }
    return results
```

### Validation

```python
def validate_migration(test_cases, old_model, new_model):
    """Compare outputs between old and new model."""
    results = []
    for test in test_cases:
        old_response = get_completion(old_model, test["prompt"])
        new_response = get_completion(new_model, test["prompt"])

        results.append({
            "prompt": test["prompt"],
            "old": old_response,
            "new": new_response,
            "expected": test.get("expected")
        })
    return results
```

## Best Practices

1. **Test thoroughly**: Validate output quality before switching
2. **Use aliases**: Let xAI handle version updates for stable features
3. **Pin versions**: Use dated versions for production stability
4. **Monitor performance**: Track latency and quality metrics
5. **Gradual rollout**: Migrate incrementally, not all at once
6. **Read changelogs**: Stay informed about model updates
