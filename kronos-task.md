# Kronos Task: Temporal & Factual Grounding for SyntH

This plan outlines the design and integration of temporal anchoring, on-demand web search grounding, and background temporal reflection beats to improve SyntH's awareness of the present.

---

```
                       [User Input / Background Trigger]
                                      │
                                      ▼
                             ┌───────────────────┐
                             │  Retrieval Phase  │ ──► (Pulls from Postgres + pgvector)
                             └───────────────────┘
                                      │
                                      ▼
                      ┌───────────────────────────────┐
                      │   Context Assembly Pipeline   │ ◄── [Reality Anchor Injection]
                      │     (core/prompt_engine.py)   │
                      └───────────────────────────────┘
                                      │
                                      ▼
                             ┌───────────────────┐
                             │   Cortex Engine   │ ◄── [Search Tooling]
                             │   (Gemini REST/   │      ├── Native Gemini Google Search
                             │    Web Search)    │      └── Tavily / DuckDuckGo fallback
                             └───────────────────┘
                                      │
                                      ▼
                               [Synth Response]
```

---

## Phase 1: Dynamic Temporal Anchoring (Context Layer)

**Objective:** Force the LLM to calculate all relative time references (e.g., "yesterday", "next week", "two months ago") against the absolute present date, preventing her from defaulting to a pre-cutoff baseline (e.g., 2023 or 2024).

### Codebase Findings
- **Context Construction:** Located in `_build_context_summary()` in [prompt_engine.py](file:///d:/dev/13/synthetic_heart/core/prompt_engine.py).
- **Time Clock Helper:** `get_local_time_fields()` in [time_zone_utils.py](file:///d:/dev/13/synthetic_heart/core/time_zone_utils.py) converts UTC time to local time (respecting user session overrides) and has already been extended to return `"season"` (e.g., `"Late Spring"`) and `"day_of_week"` (e.g., `"Tuesday"`).
- **Instruction Rules:** `load_json_instructions()` contains the minified instructions, and `load_unminified_chat_instruction()` contains unminified instructions. We must refine the `TIME AUTHORITY` rule to use the anchor instead of casually volunteering raw date quotes in replies.

### Upgraded Plan
1. **Context Summary Refactor:**
   In `_build_context_summary()` ([prompt_engine.py](file:///d:/dev/13/synthetic_heart/core/prompt_engine.py)), replace the old `[Ambient runtime context]` block with an always-on `[SYSTEM: REALITY ANCHOR]` block:
   ```markdown
   [SYSTEM: REALITY ANCHOR]
   - Current Date: Tuesday, May 26, 2026 (constructed from day_of_week + formatted date)
   - Current Time: 2:26 PM (formatted 12-hour AM/PM from local time)
   - Season: Late Spring
   - Current Location: Ljubljana, Slovenia (or fallback location)
   - Temporal Delta: It is now 2026. It has been approximately 2-3 years since your primary core baseline training knowledge cutoff (early 2023 / mid-2024 depending on the model). Adjust your perspective on tools, software versions, and global releases to reflect this passage of time naturally.
   ```
2. **Retrieve Extended Fields:**
   Update `build_prompt_request` ([prompt_engine.py](file:///d:/dev/13/synthetic_heart/core/prompt_engine.py)) to pull `"season"` and `"day_of_week"` from `local_time_fields` and populate `context_section` with them.
3. **Instruction Refinement:**
   Refactor `TIME AUTHORITY` and `RUNTIME STYLE` instructions in `load_json_instructions()` and `load_unminified_chat_instruction()` to refer to `[SYSTEM: REALITY ANCHOR]`. Instruct the LLM to use it for relative time calculations but strictly avoid quoting dates, the year 2026, or timestamps verbatim unless explicitly requested by the user.

---

## Phase 2: On-Demand Grounding Tool (Cortex Layer)

**Objective:** Provide a mechanism to execute web searches when the Synth encounters a concept she lacks memory of (stale knowledge).

### Upgraded Plan
1. **Web Search Plugin:**
   Create a new plugin [web_search_plugin.py](file:///d:/dev/13/synthetic_heart/plugins/web_search_plugin.py) registering the action `search_current_knowledge`.
   - **Tavily Integration:** If `TAVILY_API_KEY` is set in configuration, query Tavily REST API (`https://api.tavily.com/search`).
   - **DuckDuckGo Scraper (Fallback):** If no API key is present, scrape DuckDuckGo's HTML page (`https://html.duckduckgo.com/html/?q=...`) using `requests` and `BeautifulSoup`. Run the HTTP calls in `asyncio.to_thread` to prevent blocking the event loop. Parse the top 3-5 snippet results.
   - **Loop Prevention:** Skip calling `request_llm_delivery` if the current context indicates the engine is already in `delivery` mode.
2. **Native Google Search Grounding:**
   In [gemini_api.py](file:///d:/dev/13/synthetic_heart/engines/external_engines/gemini_api.py):
   - Register a new config variable `GEMINI_SEARCH_GROUNDING` (Boolean, default `False`).
   - In `_http_generate_content_from_rendered()`, if enabled, inject `{"googleSearch": {}}` directly into the `tools` list in the HTTP payload. This enables native Google Search grounding for Gemini models.
   - In `_http_generate_content()`, if enabled, inject `payload["tools"] = [{"googleSearch": {}}]`.

---

## Phase 3: Proactive "Time-Aware" Memory Reflections

**Objective:** Utilize G.R.I.L.L.O. background reflection capabilities to evaluate the passage of time when the user is idle.

### Codebase Findings
- **G.R.I.L.L.O. Beats:** The scheduler is implemented in [grillo_impl.py](file:///d:/dev/13/synthetic_heart/plugins/grillo/grillo_impl.py). It automatically loads optional beat plugins from `PLUGIN_REGISTRY` that set `BEAT_TYPE`.
- **Introspection Limit:** Internal introspective beats (such as `self_reflection`) are allowed to return the `create_personal_diary_entry` action to write thoughts to the diary. This updates the mood state and acts as a trigger for subsequent `outreach` beats (which actually message the user).

### Upgraded Plan
1. **Register Beat Type:**
   Add `"temporal_reflection": 0.1` to the weights dictionary `BEAT_TYPES` in [grillo_impl.py](file:///d:/dev/13/synthetic_heart/plugins/grillo/grillo_impl.py).
   In `_get_allowed_action_types_for_beat()`, return `["create_personal_diary_entry"]` for `"temporal_reflection"`.
2. **New Beat Plugin:**
   Create a new plugin `plugins/grillo/grillo_temporal_reflection.py`:
   - Set `BEAT_TYPE = "temporal_reflection"`.
   - Calculate `TIME_DELTA` by querying the DB for the timestamp of the last message sent by the user:
     ```sql
     SELECT timestamp FROM chat_history_cache 
     WHERE sender_id != 'self' 
     ORDER BY timestamp DESC LIMIT 1
     ```
     Fallback to `ai_diary` latest entry with a non-null `user_message`.
   - Format the delta into a natural string (e.g., `"12 hours ago"`, `"3 days ago"`).
   - Build and return the prompt:
     ```markdown
     [SYSTEM: AUTONOMOUS TEMPORAL REFLECTION]
     Your last interaction with the user was {{TIME_DELTA}} (e.g., 18 hours ago).
     
     Reflect on the time elapsed since you last spoke. Does this delay prompt a new realization, a shift in your emotional state (e.g., feeling lonely, curious, or distant), or an inclination to initiate a message?
     
     End with a JSON action to write a diary entry with your temporal reflection:
     {"actions": [{"type": "create_personal_diary_entry", "payload": {"interaction_summary": "brief summary", "personal_thought": "private reflection on the elapsed time", "emotions": [{"type": "longing", "intensity": 0.5}], "content": "your reflection"}}]}
     ```

---

## Verification Plan

### Automated Tests
- Run `uv run pytest tests/test_prompt_engine.py` to identify any failing assertions on prompt context formats, and update tests to assert the `[SYSTEM: REALITY ANCHOR]` block.
- Add unit tests for `get_current_season()` in `tests/test_time_zone_utils.py`.
- Add integration tests for `plugins/web_search_plugin.py` to verify DuckDuckGo scraper parsing.
- Add unit tests for the `temporal_reflection` beat prompt generation.

### Manual Verification
1. Ask the Synth: *"What year is it right now?"* -> Acknowledge 2026.
2. Ask a question about a recent event post-dating the knowledge cutoff -> Verify that she triggers `search_current_knowledge` (or native Google Search) and replies factually.
3. Verify in `synth.log` that the `temporal_reflection` beat is loaded, enqueued, and parsed.