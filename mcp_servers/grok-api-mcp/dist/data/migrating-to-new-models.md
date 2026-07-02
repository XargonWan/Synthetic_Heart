# Migrating to New Models

Guide for migrating from deprecated models to newer versions.

## Model Lifecycle

### Deprecation Process

1. **Active**: Model is fully supported and recommended for use
2. **Deprecated**: Model still works but should be migrated away from
3. **Obsolete**: Model is discontinued and no longer serving requests

You will see a `deprecated` tag by deprecated model names on the xAI Console models page.

## When to Migrate

You should consider moving to a newer model when:
- The model of your choice is being deprecated
- A newer model offers better performance for your use case
- You want access to new features not available in older models

xAI may transition a deprecated model to obsolete and discontinue serving at any time after the deprecation period.

## Migration Steps

### 1. Identify Deprecated Models

Check the xAI Console models page for deprecation tags, or monitor release notes for deprecation announcements.

### 2. Test with New Model

Before switching production traffic:

```python
# Test with new model
response = client.chat.completions.create(
    model="grok-4",  # New model
    messages=[{"role": "user", "content": "Test prompt"}]
)
```

### 3. Compare Outputs

Ensure the new model produces acceptable results for your use case:
- Quality of responses
- Latency characteristics
- Token usage and costs

### 4. Update Model References

Update your code to use the new model:

```python
# Before
MODEL = "grok-3"  # Deprecated

# After
MODEL = "grok-4"  # Current
```

### 5. Monitor After Migration

Watch for:
- Changes in response quality
- Error rates
- Latency differences
- Cost changes

## Model Aliases

Use model aliases to simplify migrations:

| Alias Pattern | Description |
|---------------|-------------|
| `grok-4` | Latest stable version |
| `grok-4-latest` | Latest version (may include beta features) |
| `grok-4-MMDD` | Specific dated release (never changes) |

### Recommendation

- **Development**: Use `<model>-latest` for access to newest features
- **Production**: Use dated versions (`<model>-MMDD`) for consistency
- **General use**: Use base alias (`grok-4`) for automatic updates to stable releases

## Breaking Changes

When migrating between major versions (e.g., grok-3 to grok-4), be aware of:

### Parameter Changes

Some parameters may not be supported:
- `presencePenalty`, `frequencyPenalty`, `stop` not supported by reasoning models
- `reasoning_effort` only supported by specific models (grok-3-mini)

### Response Format Changes

- Reasoning tokens may be included in responses
- Token counts may differ between models

### Pricing Changes

- Different input/output token prices
- Different rates for cached tokens
- Different tool invocation costs

## Migration Checklist

1. [ ] Identify all code using deprecated models
2. [ ] Review release notes for the new model
3. [ ] Test new model in development environment
4. [ ] Compare response quality and latency
5. [ ] Update cost projections based on new pricing
6. [ ] Update model references in code
7. [ ] Deploy to staging and validate
8. [ ] Monitor production after migration
9. [ ] Remove any deprecated model fallbacks

## Getting Help

If you encounter issues during migration:
- Documentation: docs.x.ai
- Support: support@x.ai
- Status: status.x.ai
