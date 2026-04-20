# Plan: Full Prompt Assembly Rewrite

## Context

The current `build_json_prompt()` system is architecturally obsolete. Every LLM engine does
`json.dumps(prompt_dict, indent=2)` and sends the entire prompt as one text blob in the user
message. This predates native function calling, prompt caching, and conversation-turn APIs.

`core/live_tool_adapters/` and `build_live_system_instruction()` already prove the right pattern.
The batch API path needs to catch up.

---

## All Five Prompt Paths (full scope)

| # | Path | Entry point | Used by | What it produces |
|---|------|-------------|---------|-----------------|
| 1 | **Main chat** | `build_json_prompt()` | All regular messages, Grillo outreach, Agent plugin | Full prompt dict |
| 2 | **Grillo internal beats** | Same `build_json_prompt()`, `grillo_beat=True` in context_memory | `grillo_impl._enqueue_with_low_priority()` | Same dict but: recon skipped, actions scoped to beat allowlist, no real user |
| 3 | **Mini-prompt** *(was "auto-response")* | `build_minified_json_instructions()` / `build_full_json_instructions()` | `auto_response.py` — action result delivery + event reminders | Instructions + actions only, no chat context |
| 4 | **Live API** | `build_live_system_instruction()` | `gemini_api.py`, `discord_interface.py`, `live_session_manager.py` | Plain text system instruction string |
| 5 | **System message** | Inline `context_memory["system_message"]` dict | `plugin_instance.py` — event engine | Pre-assembled payload, bypasses prompt_engine entirely — already correct |

**Path 5 is already the most correct** — it sidesteps the JSON blob pattern entirely.

---

## Root Cause

```
Every turn (Paths 1–3):
  [system] → instructions_verbose + instructions  (extracted from prompt dict)
  [user]   → json.dumps({context, input, instructions, actions}, indent=2)
                         ^^^ EVERYTHING as a text blob the LLM must parse
```

| Problem | Paths | Token impact |
|---------|-------|-------------|
| History embedded in JSON, not conversation turns | 1, 2 | ~30–40% overhead on history |
| Actions as text schemas inside user message | 1, 2, 3 | ~20–40% on actions section |
| `indent=2` serialization | 1, 2, 3 | ~15–20% overhead everywhere |
| Persona duplicated in `instructions` AND `instructions_verbose` | 1 | 300–1500 chars doubled per chat turn |
| No prompt caching possible | 1, 2, 3 | Full KV recompute every turn |
| Recon "Prompt 0" = extra LLM call per turn | 1 | 2× inference cost on local models |
| Grillo gets full chat context it doesn't use | 2 | Wasted tokens on internal beats |
| Mini-prompt rebuilds instructions from scratch on every delivery | 3 | CPU + minor token waste |

---

## New Architecture: `PromptRequest`

A typed intermediate representation replaces the raw dict. Engines render it natively.
**The OpenAI Chat Completions format is the baseline standard** — it's what every engine
speaks (OpenRouter, Ollama, LM Studio, llamafile, vLLM, Gemini OpenAI-compat endpoint, etc.).
Engine-specific renderers are addons on top, not the baseline.

### Core Dataclasses (new file: `core/prompt_request.py`)

```python
from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Turn:
    """One conversation turn."""
    role: str            # "user" | "assistant"
    content: str         # text; assistant turns hold the raw JSON action string
    timestamp: str | None = None


@dataclass
class RuntimeContext:
    """Fully dynamic per-turn state injected into the current user turn."""
    interface_name: str | None = None
    interface_path: str | None = None
    message_id: int | None = None
    username: str | None = None
    usertag: str | None = None
    timestamp: str | None = None
    time_of_day: str | None = None
    input_source: str = "text"        # "voice" | "text"
    emotions: str | None = None       # compact NL: "curious 0.7, warm 0.4"
    scope: str = "local"
    language: str | None = None
    tone: str | None = None
    voice_channel_id: str | None = None
    is_grillo_beat: bool = False
    beat_type: str | None = None


@dataclass
class Attachment:
    mime_type: str
    data: bytes | str | None = None
    url: str | None = None
    filename: str | None = None
    media_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromptRequest:
    """Engine-agnostic intermediate prompt representation.

    Stability order (most cacheable → least):
      1. system_instruction   — persona + rules; stable across all turns
      2. tool_declarations    — action schemas; changes only on plugin reload
      3. context_summary      — diary + memories + bios; formatted plain text
      4. conversation_history — past turns; grows each turn
      5. current_text + runtime_ctx + attachments — fully dynamic

    mode values:
      "chat"       — regular user conversation (full context + history)
      "grillo"     — internal autonomous beat (no history, minimal context)
      "delivery"   — mini-prompt for delivering action results to a user
      "live"       — Live API / voice session (renders as flat text string)
    """
    system_instruction: str                                    # stable
    tool_declarations: list[Any] = field(default_factory=list) # stable: ToolManifest list

    context_summary: str = ""                                  # moderately stable

    conversation_history: list[Turn] = field(default_factory=list)  # dynamic
    current_text: str = ""
    runtime_ctx: RuntimeContext = field(default_factory=RuntimeContext)
    attachments: list[Attachment] = field(default_factory=list)
    reply_to: dict[str, Any] | None = None

    supports_tool_calling: bool = False
    mode: str = "chat"
```

---

## Rendering Pipeline (new file: `core/prompt_renderers.py`)

```
PromptRequest
    │
    ├─→ OpenAIRenderer    (baseline standard — works for every engine)
    │     messages = [{"role":"system","content":...}, turn, turn, ..., current_turn]
    │     + tools=[] if supports_tool_calling
    │
    ├─→ AnthropicRenderer (Anthropic Messages API + cache_control + tool_use)
    │
    ├─→ GeminiRenderer    (google-genai native: systemInstruction + contents + tools)
    │
    └─→ TextRenderer      (compact fallback for engines that can't do structured input)
```

**`TextRenderer`** is for engines with no conversation-turn support (some local models,
manual/debug engines). It produces a compact single-string user message — NOT the current
`json.dumps(full_dict, indent=2)` blob. Differences:
- No `indent=2` → compact JSON
- Actions: brief-only text, no schema
- History: compact text lines `"[14:22 user] hi\n[14:22 synth] hey\n"`
- Context summary as a pre-formatted block
- ~35–45% smaller than current output for the same content

**`OpenAIRenderer`** is the new default for `openapi.py`, `openrouter.py`, and any new engine.
It produces standard OpenAI `messages` format:
```python
[
  {"role": "system", "content": system_instruction + "\n\n" + context_summary},
  {"role": "user",      "content": turn.content},  # history turns
  {"role": "assistant", "content": turn.content},
  ...
  {"role": "user",      "content": f"[{runtime_ctx_line}] {current_text}"}
]
```
With `tools=[...]` appended to the API call when `req.supports_tool_calling` is True.

---

## What the LLM Sees After Rewrite

**Today (all engines):**
```
[system] instructions_verbose (800 chars) + instructions (1400 chars)
[user]   {
           "context": {
             "persona": "...",         ← duplicate of system
             "history_current_chat": [{"role":"user","content":"hi"}, ...],
             "diary":[...], "memories":[...], "emotions":{...}, "recon":{...}
           },
           "input": {"type":"message","interface":"telegram_bot","payload":{"text":"yo"}},
           "instructions": "MASTER INSTRUCTION...",  ← triplicate
           "actions": {"message_telegram_bot":{"schema":{...},"brief":"..."}, ...}
         }   ← ~12,000 chars, indent=2, everything mixed together
```

**After (OpenAI-compat, default for most engines):**
```
[system]      You are SyntH... [persona 400 chars + rules 600 chars]
              Diary: ... Memories: ... [context_summary 800 chars]

[user]        hi
[assistant]   {"actions":[{"type":"message_telegram_bot","payload":{"text":"hey!"}}]}
[user]        how are you
[assistant]   {"actions":[...]}
[user]        [2026-04-13 15:30 | emotions: curious 0.7] yo

tools: [{"name":"message_telegram_bot","description":"...","parameters":{...}}, ...]
```

Token savings estimate for a 10-turn conversation: ~40–50%.
Grillo internal beats: ~60% (no history, no participants, minimal context).

---

## Phase 1 — Foundation: New Prompt Type (no behavior change)

**What this is:** Right now, `build_json_prompt()` returns a Python `dict`. Every engine takes
that dict and calls `json.dumps()` on it. This phase introduces a new type — `PromptRequest`
— that holds the same information in a structured, typed way. The `dict` still gets returned
(and engines still use it exactly as before), but the `PromptRequest` is now *attached* to it
as an extra key so engines can optionally use it later.

Think of it as: we're laying the foundation. Nothing breaks, nothing changes for users.
We're just building the new type system that the rest of the migration will use.

**Steps:**
1. Create `core/prompt_request.py` with all dataclasses (`PromptRequest`, `Turn`, `RuntimeContext`, `Attachment`).
2. Create `core/prompt_renderers.py` with **`OpenAIRenderer` only** (the baseline renderer). Its `render(req) -> list[dict]` returns the standard OpenAI messages list.
3. In `build_json_prompt()`, after building the existing dict, also build a `PromptRequest` from the same data and attach it: `prompt_dict["__prompt_request"] = req`.

After Phase 1, nothing in the system changes. Engines all ignore `__prompt_request` and
keep working exactly as before. But the new type exists and is being populated, so Phase 2
can start migrating engines one at a time.

**Test:** `test_prompt_request_attached()` — call `build_json_prompt()`, assert the returned
dict has a `"__prompt_request"` key that is a `PromptRequest` instance with non-empty
`system_instruction`, `current_text`, and `conversation_history`.

---

## Phase 2 — Grillo Beat Mode

**Goal:** Grillo internal beats produce a `PromptRequest(mode="grillo")` with:
- `system_instruction` = persona + minimal diary-writing rules only
- `context_summary` = recent diary entries + tag-matched memories for this beat type
- `conversation_history` = **empty** (Grillo has no ongoing conversation)
- `tool_declarations` = only beat-allowed actions (e.g. `create_personal_diary_entry`)
- `runtime_ctx.is_grillo_beat = True`, `runtime_ctx.beat_type = beat_type`

**In `core/prompt_engine.py`:** When `is_grillo_internal` is True, skip populating
`conversation_history` in the `PromptRequest` and set `mode="grillo"`.

**In `core/history_engine.py`:** When `beat_mode=True`, skip `history_current_chat` and
`history_recent`. Only fetch diary and tag-matched memories for `context_summary`.

**`grillo_impl.py` is unchanged** — beat prompts are still plain text strings that become
`message.text`, which `build_json_prompt()` picks up as `current_text`. No Grillo business
logic changes.

**Result:** Grillo beat `PromptRequest` objects are tiny. When an engine migrates to read
`PromptRequest` in Phase 4+, Grillo beats automatically become ~60% smaller.

---

## Phase 3 — Delivery Mode (Mini-Prompt)

**Goal:** Replace `auto_response.py`'s use of `build_minified_json_instructions()` /
`build_full_json_instructions()` with a purpose-built `PromptRequest(mode="delivery")`.

**What a delivery prompt needs:**
- Persona + "deliver these results to the user" instruction
- Only message_* actions (no diary, no animation — just send a message)
- The action output as `current_text`
- No history, no context summary

**New function in `core/prompt_engine.py`:**
```python
async def build_delivery_request(
    action_type: str,
    action_outputs: list[dict],
    interface_name: str | None,
    interface_path: str | None,
) -> PromptRequest:
    """Build a minimal PromptRequest for delivering action results to a user.

    The LLM receives persona + delivery instruction + the outputs and sends
    exactly one message_* action back. No context, no history.
    """
```

**In `auto_response.py`:** Call `build_delivery_request()` instead of the two instruction
builders. The `PromptRequest` is passed through the message queue in `context_memory` as
`context_memory["__prompt_request"]`.

**Note:** "Mini-prompt" / "delivery" is also the right pattern for event reminders
(`type="event_reminder"` in the system_message path). The event plugin can switch from
the inline dict pattern to `build_delivery_request()` as well.

---

## Phase 4 — Migrate OpenAI-Compat Engines (broadest reach)

**Priority: `openapi.py` first, then `openrouter.py`.**

These cover every OpenAI-compatible endpoint: Ollama, LM Studio, llamafile, vLLM, any local
model, and even Gemini via its OpenAI-compat endpoint. This is the change that immediately
helps local model users.

**In `engines/external_engines/openapi.py`, `generate_response()`:**
```python
req: PromptRequest | None = (
    prompt.get("__prompt_request") if isinstance(prompt, dict) else None
)
if req is not None:
    messages = OpenAIRenderer(req).render()
    tools = OpenAIRenderer(req).tool_schemas() if req.supports_tool_calling else None
    response = await client.chat.completions.create(
        model=..., messages=messages,
        **({"tools": tools, "tool_choice": "auto"} if tools else {})
    )
    return _parse_response(response, used_tool_calling=tools is not None)
else:
    # Legacy path unchanged — json.dumps as before
    ...
```

**`openrouter.py`** gets the same treatment. Its `OpenRouterModel.supports_tool_use` flag
is already set correctly — now it actually gets used to populate `req.supports_tool_calling`.

**New `_parse_response(response, used_tool_calling: bool)`:** handles both JSON text responses
(current) and `tool_calls[].function.{name, arguments}` (new). Returns the same
`[{"type": action_name, "payload": {...}}]` format the rest of the system expects.

---

## Phase 5 — Migrate Anthropic Engine with Prompt Caching

**In `engines/external_engines/anthropic.py`:**

`AnthropicRenderer` produces:
```python
{
  "system": [
    {"type": "text", "text": system_instruction,
     "cache_control": {"type": "ephemeral"}},   # stable prefix — cached
    {"type": "text", "text": context_summary}   # dynamic — not cached
  ],
  "messages": [...turns..., current_user_turn],
  "tools": [...],
  "tool_choice": {"type": "auto"}
}
```

**Config key:** `ENABLE_PROMPT_CACHING` (bool, default True). When False, no
`cache_control` blocks — predictable billing for users who need it.

Anthropic's ephemeral cache TTL is 5 minutes. For an active conversation with messages
every minute, the stable prefix (persona + instructions ≈ 1000–2000 tokens) is cached
and costs ~10% on subsequent calls. Savings kick in from the second message onward.

---

## Phase 6 — Migrate Gemini REST Engine

**In `engines/external_engines/gemini_api.py`:**

`GeminiRenderer` maps `PromptRequest` to Gemini's native wire format:
- `system_instruction + context_summary` → `systemInstruction.parts[0].text`
- `conversation_history` turns → `contents` list (role: user/model alternating)
- Current turn → last `contents` entry with inline_data parts for attachments
- `tool_declarations` → `GeminiToolAdapter.to_declarations()` (reuse from `core/live_tool_adapters/gemini.py`)
- `mode == "grillo"` → single-turn contents, no history

Gemini's `cachedContent` API (for caching the stable prefix) is an optional add-on in
this phase — the plain `systemInstruction` path already works and is simpler.

---

## Phase 7 — Live API Convergence

`build_live_system_instruction()` is already mostly correct (plain text, persona-first).
Replace its ad-hoc string assembly with `PromptRequest(mode="live")` rendered by
`LiveRenderer().render_as_text(req)`. Return value is the same plain string the callers
expect — **no change to `live_session_manager.py`, `discord_interface.py`, or `gemini_api.py`
Live path callers.** They already receive the right output type.

---

## Phase 8 — Remove Legacy Paths

After all engines are migrated:
- Remove `json.dumps(prompt, indent=2)` from all engines
- Remove `instructions_verbose` assembly in `build_json_prompt()` (now in renderers)
- Remove `__prompt_request` passthrough key (engines receive `PromptRequest` directly)
- Remove `build_full_json_instructions()` and `build_minified_json_instructions()` (replaced by `build_delivery_request()`)
- Rename `build_json_prompt()` → `build_prompt_request() -> PromptRequest`. Keep the old name as a deprecated alias.

---

## Emotion Manager, Facial Expressions, and Animation Handler

These three systems interact with the prompt in different ways and need different treatment.

---

### Emotion Manager — split stable from dynamic (Phase 1–2)

`plugins/emotion_manager.py:get_static_injection()` currently returns three things that
all land in the `context` dict:

| Key | Content | Stability |
|-----|---------|-----------|
| `emotion_state` | Multi-line instruction string: current values + available emotions list + tag how-to | Mixed — list is stable, values are dynamic |
| `current_emotions_nl` | Compact NL string: `"curious (7.0 - high), warm (4.2 - moderate)"` | Dynamic — changes every turn |
| `available_emotions` | List of canonical emotion names | Stable — only changes if code changes |

In the new `PromptRequest`, these split cleanly:

- **Stable part** → `system_instruction`: available emotion names + how to express them.
  Cached by Anthropic/Gemini. Never changes mid-session.
- **Dynamic part** → `RuntimeContext.emotions`: just the compact NL string of current values.
  Injected into the current turn's context line alongside time and tone.

`context_summary` does NOT include emotion data — it's in `RuntimeContext` where it belongs.
This removes `emotion_state`, `current_emotions_nl`, and `available_emotions` as separate
context section keys entirely.

**No changes to `emotion_manager.py` itself.** The split happens in how
`build_prompt_request()` maps `get_static_injection()` output to `PromptRequest` fields.

---

### Existing contradiction to resolve (Phase 4, alongside tool calling)

There is a pre-existing conflict in the codebase:

- `emotion_manager.get_static_injection()` instructs the LLM:
  *"To express emotions, include tags in your response like `{happy 8.5, surprised 3.0}`"*
- `load_json_instructions()` explicitly bans this:
  *"Do NOT embed emotion tags, annotations, or bracketed markers inside message text (e.g., '{happy 6.0}'). Include it as structured data in the JSON."*

These are contradictory and the LLM receives both. With native tool calling this conflict
becomes acute — tool call responses are structured JSON, not free text, so `{happy 8.5}`
curly-brace tags in text fields would be silently ignored or break parsers.

**Resolution in Phase 4:** Replace the `{happy 8.5}` curly-brace format with a proper
`update_emotion_state` action the LLM calls explicitly alongside its message action:

```json
{"actions": [
  {"type": "message_telegram_bot", "payload": {"text": "Hey, how are you?"}},
  {"type": "update_emotion_state", "payload": {"emotions": {"happy": 8.5, "curious": 6.0}}}
]}
```

This is:
- Tool-call compatible (structured payload, no in-text parsing)
- Consistent with the master instruction ban on in-text tags
- Already the direction `emotion_manager.py` supports (`sync_emotions_from_all_sources` exists)

**Files:** `plugins/emotion_manager.py` (add `update_emotion_state` action handler),
`core/prompt_engine.py` (remove the contradictory instruction from the emotion injection),
`load_json_instructions()` (clarify the rule now that the replacement exists).

---

### Facial Expression Plugin — minor plumbing, not a rewrite (Phase 4)

`plugins/facial_expression_plugin.py` injects instructions for `[em_NAME:INTENSITY]`
square-bracket tokens that the LLM embeds inside message text. These are a **completely
separate system** from the `{happy 8.5}` curly-brace emotion state tags:

- Purpose: drive real-time blend-shape facial animations via `KaradaStateServer` WebSocket
- Format: `[em_smile:0.9]` embedded in message text, timed to speech audio duration
- Processing: `FacialExpressionPlugin.process_message_text(text)` strips tags and schedules animation timeline

**These tokens are already tool-call compatible.** They live inside `payload.text` of a
`message_*` action — which is exactly where they'll be in the new architecture:

```json
{"type": "message_telegram_bot", "payload": {"text": "Ciao! [em_grin:0.9] Come va?"}}
```

The `[em_grin:0.9]` tag is inside the `text` string, which `process_message_text()` already
parses and strips before the message is sent to the user. **No format change needed.**

The only plumbing change: wherever `process_message_text()` is currently called on the
full raw LLM response text, it must instead be called on `payload["text"]` extracted from
each `message_*` action in the response. This is a one-function change in the message
dispatch layer — **not part of the prompt assembly rewrite**, just a consequence of it.
Schedule this as a small fix alongside Phase 4 (the first engine migration).

---

### Animation Handler and `use_animation` — no changes needed

Two distinct systems:

1. **`use_animation` action** (`core/persona_manager.py:handle_use_animation()`):
   - LLM calls this to explicitly set an animation state (`idle`, `think`, `write`, `talk`)
   - Registered via `get_supported_actions()` in `persona_manager.py`
   - Automatically included in `tool_declarations` when `LiveToolRegistry.build_manifests()`
     is extended to batch mode (Phase 1 of this plan)
   - `handle_use_animation()` is unchanged — it just receives a `payload` dict as always

2. **State machine** (`AnimationHandler` — THINK → WRITE → IDLE transitions):
   - Triggered by `persona_manager.set_animation_state()` during message processing
   - Completely independent of prompt assembly
   - **Zero changes needed, now or after the rewrite**

---

## Recon + Language Detection (addressed alongside this rewrite)

- **Phases 1–3:** Keep recon as-is. Its output populates `RuntimeContext.language` and `.tone`.
- **Phase 4 (alongside OpenAI engine migration):** Add language/tone TTL cache in `core/recon.py` (300s default). Cache hit skips the Recon LLM call; `RuntimeContext` populated from cache.
- **Phase 5:** Local lingua detection as a pre-check before Recon. `lingua-language-detector` is already in `pyproject.toml`. Singleton in `core/recon.py` (separate from `plugins/vox_plugin.py` to avoid core→plugin dep).

---

## Key Invariants

1. **OpenAI format is the standard baseline.** `OpenAIRenderer` output works for every engine family. Gemini and Anthropic renderers are enhancements, not replacements.
2. **Engines own rendering.** `build_prompt_request()` never touches wire format. Renderers live in engine files or `core/prompt_renderers.py`.
3. **Mode drives what gets populated.** `"grillo"` skips history; `"delivery"` skips context; `"live"` renders as flat text.
4. **Backward compat until Phase 8.** Every engine handles both `PromptRequest` (via `__prompt_request`) and the legacy dict path. No flag days.
5. **`LiveToolRegistry` is shared.** `GeminiToolAdapter` in `core/live_tool_adapters/gemini.py` is reused by `GeminiRenderer`. No duplication.
6. **Context summary is a string.** Diary, memories, relationships → pre-formatted plain text in `PromptRequest.context_summary`. Engines don't parse it.
7. **Grillo business logic is unchanged.** `grillo_impl._create_*_prompt()` continues returning plain text strings; `build_prompt_request()` picks them up as `current_text`.
8. **System message path (Path 5) is untouched.** Already correct for its use case.

---

## Files to Create / Modify (in order)

| File | Phase | Change |
|------|-------|--------|
| `core/prompt_request.py` | 1 | New: all dataclasses |
| `core/prompt_renderers.py` | 1 | New: `OpenAIRenderer` (+ other renderers added in later phases) |
| `core/live_tool_registry.py` | 1 | Add `interface_name` param to `build_manifests()` |
| `core/prompt_engine.py` | 1–3 | Attach `__prompt_request`; add `build_delivery_request()` |
| `core/history_engine.py` | 1–2 | `build_context()` populates `PromptRequest` fields |
| `core/auto_response.py` | 3 | Use `build_delivery_request()` instead of `build_*_json_instructions()` |
| `engines/external_engines/openapi.py` | 4 | Prefer `PromptRequest` → `OpenAIRenderer` |
| `engines/external_engines/openrouter.py` | 4 | Same; wire up existing `supports_tool_use` flag |
| `engines/external_engines/anthropic.py` | 5 | `AnthropicRenderer` + `cache_control` |
| `engines/external_engines/gemini_api.py` | 6 | `GeminiRenderer` using existing tool adapter |
| `core/recon.py` | 4–5 | Language/tone cache + lingua local detection |
| `tests/test_prompt_renderers.py` | 1+ | New: renderer output tests |
| `tests/test_prompt_engine.py` | 1+ | Add `PromptRequest` assertions |

---

## Verification

```bash
uv run ruff format .
uv run ruff check --fix .
uv run ty check core/prompt_request.py core/prompt_renderers.py core/prompt_engine.py core/history_engine.py core/auto_response.py
uv run pytest tests/test_prompt_engine.py tests/test_prompt_minification.py tests/test_prompt_renderers.py tests/test_current_chat_history.py -v
```

**End-to-end after Phase 4:**
- Send a test message via the Ollama API (port 11435)
- Check `cortex_api.log`: should show `messages` list (turns), not one JSON blob
- Compare token counts from response metadata before and after — expect 40%+ reduction
- Monitor Grillo beats: `docker exec synth-dev tail -f /app/logs/synth.log | grep -E "\[grillo\]"`
- Verify Grillo beats produce single-turn requests in the log
