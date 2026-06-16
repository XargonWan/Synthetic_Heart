# Mid-Live Tool Call Implementation Plan

## Current State

Tool calling during live sessions is **already partially implemented** for Gemini:

| Layer | File | What it does |
|-------|------|-------------|
| Declaration builder | `interface/discord_interface.py:_build_gemini_tool_declarations()` | Reads SyntH action registry → `genai.types.FunctionDeclaration` list |
| Receive-loop dispatcher | `core/live_session_manager.py:_receive_loop()` | Detects `message.tool_call`, fires `_on_tool_call` callback, calls `session.send_tool_response()` |
| Executor | `interface/discord_interface.py:_handle_live_tool_call()` | Converts `call_dict` → `run_action(action, context, bot, None)` |
| Callback wiring | `discord_interface.py:_start_live_voice()` | `manager.set_tool_call_callback(on_tool_call)` |

**What's missing:** the abstraction that would let a non-Gemini live engine (e.g. OpenAI Realtime, a future local engine) plug into the same executor, and the ability for plugins themselves to declare tool schemas without knowing the target model.

---

## Key Complexity: Gemini 3.1 vs 2.5

| Feature | Gemini 3.1 Flash Live | Gemini 2.5 Flash Live |
|---------|----------------------|----------------------|
| Async tool calls (`NON_BLOCKING`) | **Not supported** — sequential only | Supported |
| Model blocks until tool response | Yes — always | Only for `BLOCKING` tools |
| `scheduling` in `FunctionResponse` | Ignored / not needed | `INTERRUPT` / `WHEN_IDLE` / `SILENT` |

For now all tools run synchronously (Gemini 3.1). The architecture below keeps a slot for async scheduling that gets activated on 2.5 or future engines.

---

## Target Architecture

```
                  ┌──────────────────────────────────────┐
                  │         LiveToolRegistry              │
                  │  (model-agnostic function manifest)   │
                  └───────────────┬──────────────────────┘
                                  │ ToolManifest
                          ┌───────┴────────┐
                          │ FormatAdapter  │  (one per engine family)
                          │  GeminiAdapter │
                          │  OpenAIAdapter │
                          └───────┬────────┘
                                  │ engine-specific declarations
                       ┌──────────┴───────────┐
                       │  LiveEngineBase       │
                       │  open_session(tools=) │
                       └──────────┬────────────┘
                                  │ ToolCallEvent (model-agnostic)
                       ┌──────────┴───────────┐
                       │  LiveToolExecutor    │
                       │  → run_action()      │
                       └─────────────────────┘
```

---

## Component Breakdown

### 1. `ToolManifest` — model-agnostic tool declaration

Location: `plugins/live_base.py` (extend the existing module) or `core/live_tool_registry.py`

```python
@dataclass
class ToolParameter:
    name: str
    type: str           # "string" | "integer" | "boolean" | "object" | "array"
    description: str
    required: bool = True
    enum: list[str] | None = None

@dataclass
class ToolManifest:
    """Model-agnostic description of a callable tool."""
    name: str                          # maps to SyntH action type
    description: str
    parameters: list[ToolParameter]
    async_ok: bool = False             # if True, engine may run NON_BLOCKING
```

### 2. `LiveToolRegistry` — discovers tools from the action registry

Location: `core/live_tool_registry.py`

```python
class LiveToolRegistry:
    @staticmethod
    def build_manifests() -> list[ToolManifest]:
        """Read get_supported_actions() from all plugins and build ToolManifests."""
        # Identical logic to the existing _build_gemini_tool_declarations() in
        # discord_interface.py, but outputs ToolManifest objects instead of
        # genai.types.FunctionDeclaration.
        ...
```

This replaces `_build_gemini_tool_declarations()` as the single source of truth.

### 3. Format adapters

One adapter per engine family converts `list[ToolManifest]` to the engine-specific wire format:

```python
class GeminiToolAdapter:
    @staticmethod
    def to_declarations(manifests: list[ToolManifest]) -> list[Any]:
        # → [genai.types.Tool(function_declarations=[...])]
        ...

class OpenAIRealtimeToolAdapter:
    @staticmethod
    def to_declarations(manifests: list[ToolManifest]) -> list[Any]:
        # → [{"type": "function", "name": ..., "description": ..., "parameters": ...}]
        ...
```

Adapters live in `core/live_tool_adapters/` (one file per family).

### 4. `ToolCallEvent` — model-agnostic event from the receive loop

Extend `LiveEvent` in `plugins/live_base.py`:

```python
class LiveEventType(str, Enum):
    TRANSCRIPT = "transcript"
    AUDIO      = "audio"
    VAD        = "vad"
    ERROR      = "error"
    TOOL_CALL  = "tool_call"    # NEW

@dataclass
class ToolCallPayload:
    call_id: str          # opaque ID to echo back in the response
    name: str             # tool/action name
    args: dict[str, Any]

# LiveEvent gains:
#   tool_call: ToolCallPayload | None = None
```

The engine's receive loop yields `LiveEvent(type=TOOL_CALL, tool_call=...)` instead of calling a Gemini-specific callback directly.

### 5. `LiveToolExecutor` — model-agnostic dispatcher

Location: `core/live_tool_executor.py`

```python
class LiveToolExecutor:
    """Receives ToolCallEvents and routes them to SyntH's run_action pipeline."""

    def __init__(self, engine: LiveEngineBase) -> None:
        self._engine = engine

    async def handle(
        self,
        session_id: str,
        event: LiveEvent,
        context: dict[str, Any],
        bot: Any,
    ) -> None:
        """Execute the tool and send the response back through the engine."""
        assert event.type == LiveEventType.TOOL_CALL
        tc = event.tool_call
        action = {"type": tc.name, "payload": tc.args}
        try:
            result = await run_action(action, context, bot, None)
        except Exception as exc:
            result = {"status": "error", "message": str(exc)}
        await self._engine.send_tool_response(session_id, tc.call_id, tc.name, result)
```

### 6. `LiveEngineBase` extension — `send_tool_response`

Add to `plugins/live_base.py`:

```python
class LiveEngineBase(ABC):
    ...
    async def send_tool_response(
        self,
        session_id: str,
        call_id: str,
        name: str,
        result: dict[str, Any],
    ) -> None:
        """Send a tool result back to the model.

        Default no-op; override in engines that support function calling.
        Engines that support async scheduling should honour the
        ``scheduling`` key in ``result`` if present.
        """
```

### 7. `GeminiLiveEngine` — full implementation

The stub in `plugins/live_engines/gemini.py` becomes a real implementation:

- `open_session()` accepts `tools: list[Any] | None` (already-adapted Gemini declarations)
- `_pump_events()` parses `message.tool_call` → yields `LiveEvent(type=TOOL_CALL, ...)`
- `send_tool_response()` calls `session.send_tool_response(function_responses=...)`

The `LiveSessionManager` then uses `LiveToolExecutor` instead of calling `_on_tool_call` directly — or keeps the callback pattern as a thin shim over `LiveToolExecutor` for backwards compatibility.

---

## Migration Path (no breaking changes)

1. **Phase 1 (current):** `LiveSessionManager` + `discord_interface._handle_live_tool_call` — Gemini-locked, works.
2. **Phase 2:** Introduce `ToolManifest` + `LiveToolRegistry`; replace `_build_gemini_tool_declarations()` with `GeminiToolAdapter.to_declarations(LiveToolRegistry.build_manifests())`. No behaviour change, just plumbing.
3. **Phase 3:** Extend `LiveEvent` with `TOOL_CALL`, extend `LiveEngineBase` with `send_tool_response`. `GeminiLiveEngine` stub becomes a full implementation.
4. **Phase 4:** `LiveSessionManager._receive_loop` stops calling `_on_tool_call` directly; instead yields `TOOL_CALL` events into `LiveToolExecutor`. Discord interface callback becomes a thin wrapper.
5. **Phase 5:** `OpenAI Realtime` (or any future engine) implements `send_tool_response` with its own wire format — executor is unchanged.

---

## Async Tool Calls (Gemini 2.5 / future engines)

Gemini 3.1 **does not support** async tool calls — the model blocks until the response is sent. This means `LiveToolExecutor.handle()` must `await` and return before the next audio frame is processed.

On Gemini 2.5 (and engines that support `NON_BLOCKING`):
- Set `behavior: NON_BLOCKING` in the function declaration if `ToolManifest.async_ok = True`.
- After calling `run_action`, include `scheduling` in the response:
  ```python
  result["scheduling"] = "INTERRUPT"   # or WHEN_IDLE / SILENT
  ```
- The executor reads `async_ok` from the manifest to decide the scheduling hint.

This can be gated per-engine:

```python
# in GeminiToolAdapter.to_declarations():
if manifest.async_ok and engine_supports_nonblocking:
    fd_kwargs["behavior"] = "NON_BLOCKING"
```

---

## Files to Create / Modify

| Action | File |
|--------|------|
| Create | `core/live_tool_registry.py` — `ToolManifest`, `LiveToolRegistry` |
| Create | `core/live_tool_adapters/gemini.py` — `GeminiToolAdapter` |
| Create | `core/live_tool_adapters/openai_realtime.py` — `OpenAIRealtimeToolAdapter` (stub) |
| Create | `core/live_tool_executor.py` — `LiveToolExecutor` |
| Modify | `plugins/live_base.py` — add `TOOL_CALL` to `LiveEventType`, `ToolCallPayload` to `LiveEvent`, `send_tool_response` to `LiveEngineBase` |
| Modify | `plugins/live_engines/gemini.py` — full implementation replacing stub |
| Modify | `core/live_session_manager.py` — wire `LiveToolExecutor`; keep `set_tool_call_callback` as backwards-compat shim |
| Modify | `interface/discord_interface.py` — swap `_build_gemini_tool_declarations` for `LiveToolRegistry` + `GeminiToolAdapter` |
| Deprecate | `interface/discord_interface.py:_build_gemini_tool_declarations` — remove once Phase 2 is merged |

---

## Open Questions

1. **Streaming tool results:** Some tools (e.g. a web search) may want to stream partial results. The current sync `run_action` pipeline doesn't support this. Punting for now.
2. **Tool timeouts:** If `run_action` hangs, the live session stalls (especially on 3.1 which is always blocking). Add a `asyncio.wait_for` timeout in `LiveToolExecutor`.
3. **Multi-call batching:** Gemini can emit multiple function calls in a single `tool_call` message. The executor should run them sequentially (3.1) or concurrently with `asyncio.gather` (2.5 async).
4. **Schema drift:** `ToolManifest` maps to SyntH `get_supported_actions()` field types. OpenAI Realtime uses a slightly different JSON Schema dialect. The adapter layer handles translation, but complex nested schemas need extra care.
