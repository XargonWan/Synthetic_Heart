# Plan: Conditional Tool Call Registration Based on Module Enablement

## Context

Currently, **all** tool calls/actions are registered into the LLM prompt regardless of whether their underlying module is enabled. This wastes tokens and can confuse the model with unavailable actions.

**Example:**
- Even when `ACTIVE_VOX_ENGINE="disabled"`, the `tts_speak` action is still added to the prompt
- Even when `ACTIVE_IRIS_ENGINE="disabled"`, the `vision_describe` action is still added
- Even when `SOUL_PLUGIN_ENABLED=false`, SOUL-related actions remain in the prompt

The actions are collected in `core/core_initializer.py` via `_build_actions_block()` which calls `get_supported_actions()` on every plugin without checking if the plugin's functionality is actually available.

## Key Files

| File | Purpose |
|------|---------|
| `core/core_initializer.py` | `_build_actions_block()` method (lines 1305-1536) - collects all actions |
| `plugins/vox_plugin.py` | `VoxPlugin` - TTS subsystem, `ACTIVE_VOX_ENGINE="disabled"` should hide `tts_speak` |
| `plugins/auris_plugin.py` | `AurisPlugin` - STT subsystem, `ACTIVE_AURIS_ENGINE="disabled"` should hide `stt_transcribe` |
| `plugins/iris_plugin.py` | `IrisPlugin` - Vision subsystem, `ACTIVE_IRIS_ENGINE="disabled"` should hide `vision_describe` |
| `plugins/soul_plugin.py` | `SOUL_PLUGIN_ENABLED=false` should hide SOUL actions |
| `plugins/agent_plugin.py` | `AGENT_ENABLED=false` already has enablement logic |
| `plugins/memory_search.py` | `ENABLE_MEMORY_SEARCH=false` should hide memory search actions |

## Current Behavior

In `_build_actions_block()` (line 1412-1440), the only enablement check is:
```python
# Skip plugins that expose an `enabled` attribute and are currently disabled
if hasattr(plugin, "enabled") and not getattr(plugin, "enabled"):
    log_debug(f"Plugin {name} has `enabled=False`, skipping action registration")
    continue
```

Media subsystem plugins (Vox, Auris, Iris) don't use an `enabled` attribute - they check config values at runtime but still register their actions.

## Proposed Solution

### 1. Standardize Enablement Check Pattern

Add a new method `is_enabled()` that plugins can implement to declare whether they should register actions:

```python
def is_enabled(self) -> bool:
    """Return True if this plugin's actions should be registered in the LLM prompt."""
    return True  # Default: always enabled
```

### 2. Modify `_build_actions_block()` in `core/core_initializer.py`

Update the plugin loop to check `is_enabled()` before calling `get_supported_actions()`:

```python
# Around line 1410, modify the plugin loop:
for name, plugin in PLUGIN_REGISTRY.items():
    log_debug(f"[core_initializer] Processing plugin: {name}")

    # Check if plugin is enabled (new standardized check)
    plugin_enabled = True
    if hasattr(plugin, "is_enabled"):
        try:
            plugin_enabled = plugin.is_enabled()
            log_debug(f"[core_initializer] Plugin {name} is_enabled={plugin_enabled}")
        except Exception as e:
            log_warning(f"[core_initializer] Error checking is_enabled for {name}: {e}")
    elif hasattr(plugin, "enabled") and not getattr(plugin, "enabled"):
        # Legacy check for backward compatibility
        log_debug(f"[core_initializer] Plugin {name} has `enabled=False`, skipping")
        continue

    if not plugin_enabled:
        log_debug(f"[core_initializer] Plugin {name} is disabled, skipping action registration")
        continue

    # ... rest of existing action registration logic
```

### 3. Implement `is_enabled()` in Affected Plugins

#### 3.1 VoxPlugin (`plugins/vox_plugin.py`)

```python
class VoxPlugin(AIPluginBase):
    # ... existing code ...

    def is_enabled(self) -> bool:
        """Return True if TTS is enabled (engine is not 'disabled')."""
        return self._active_engine_name != "disabled"
```

#### 3.2 AurisPlugin (`plugins/auris_plugin.py`)

```python
class AurisPlugin(AIPluginBase):
    # ... existing code ...

    def is_enabled(self) -> bool:
        """Return True if STT is enabled (engine is not 'disabled')."""
        return self._active_engine_name != "disabled"
```

#### 3.3 IrisPlugin (`plugins/iris_plugin.py`)

```python
class IrisPlugin(AIPluginBase):
    # ... existing code ...

    def is_enabled(self) -> bool:
        """Return True if vision is enabled (engine is not 'disabled')."""
        return self._active_engine_name != "disabled"
```

#### 3.4 SoulPlugin (`plugins/soul_plugin.py`)

```python
class SoulPlugin(AIPluginBase):
    # ... existing code ...

    def is_enabled(self) -> bool:
        """Return True if SOUL plugin is enabled via config."""
        return bool(config_registry.get_value("SOUL_PLUGIN_ENABLED", True))
```

#### 3.5 MemorySearchPlugin (`plugins/memory_search.py`)

```python
class MemorySearchPlugin(AIPluginBase):
    # ... existing code ...

    def is_enabled(self) -> bool:
        """Return True if memory search is enabled via config."""
        return bool(config_registry.get_value("ENABLE_MEMORY_SEARCH", False))
```

**Note:** `AgentPlugin` already has enablement logic checking `AGENT_ENABLED` and sets an `enabled` attribute, so the legacy check will handle it. No changes needed.

### 4. Ensure Runtime Config Refresh

When users change configuration (e.g., `ACTIVE_VOX_ENGINE`), the actions block needs to be rebuilt. The existing `_refresh_actions_block()` call path (lines 1818-2034 in `core_initializer.py`) should handle this:

```python
# In config listener or when engine is changed:
await core_initializer._build_actions_block()
```

### 5. Add Base Class Default (Optional Enhancement)

For forward compatibility, add the default `is_enabled()` method to `AIPluginBase` in `core/ai_plugin_base.py`:

```python
class AIPluginBase(PluginBase):
    # ... existing code ...

    def is_enabled(self) -> bool:
        """Return True if this plugin's actions should be registered in the LLM prompt.
        Subclasses can override to implement conditional registration based on config.
        """
        return True
```

## Verification

1. **Test disabled Vox:** Set `ACTIVE_VOX_ENGINE="disabled"`, rebuild actions block, verify `tts_speak` is NOT in `core_initializer.actions_block["available_actions"]`

2. **Test enabled Vox:** Set `ACTIVE_VOX_ENGINE="kitten"`, rebuild, verify `tts_speak` IS registered

3. **Test disabled Iris:** Set `ACTIVE_IRIS_ENGINE="disabled"`, verify `vision_describe` is NOT registered

4. **Test disabled SOUL:** Set `SOUL_PLUGIN_ENABLED=false`, verify SOUL actions are NOT registered

5. **Test config change:** Change `ACTIVE_VOX_ENGINE` from "disabled" to "kitten" via WebUI/API, verify `tts_speak` appears after rebuild

6. **Check logs:** Verify debug logs show `Plugin X is_enabled=False/Y, skipping action registration` appropriately

7. **Run existing tests:** Ensure no regressions in `tests/test_core_initializer.py`, `tests/test_vox_plugin.py`, `tests/test_auris_plugin.py`, `tests/test_iris.py`

## Benefits

1. **Reduced token usage:** Disabled actions don't consume prompt space
2. **Clearer model behavior:** LLM only sees actions that can actually execute
3. **Consistent enablement pattern:** Single `is_enabled()` method works for all plugins
4. **Backward compatible:** Existing `enabled` attribute check is preserved
5. **Runtime reconfigurable:** Changing config and rebuilding actions block immediately affects available actions

## Risk Assessment

- **Low risk:** Changes are additive and defensive (additional checks)
- **Existing plugins without `is_enabled()`** will default to enabled via the `else` path
- **Legacy `enabled` attribute** still works via the `elif` branch
- **No breaking changes** to plugin interfaces - `is_enabled()` is optional

## Order of Implementation

1. Add `is_enabled()` default to `AIPluginBase` (optional but recommended)
2. Modify `_build_actions_block()` to check `is_enabled()`
3. Implement `is_enabled()` in VoxPlugin, AurisPlugin, IrisPlugin
4. Implement `is_enabled()` in SoulPlugin, MemorySearchPlugin
5. Test with various enable/disable combinations
6. Verify runtime config changes trigger rebuild correctly
