# SOUL Wiring — Fix Proposal

> Analysis + proposal. **No code changed.** Builds on `SOUL-REWRITE-COMPARISON.md`.
> Date: 2026-08-05

---

## 0. Plain-English explainer (for the monkey in all of us)

### What is DSP?

**DSP = Dynamic System Prompt** — but that name is confusing. Think of it as
**"who is the person I'm talking to, compressed to a cheat-sheet."**

Right now, when Synth replies, she only has the *persona* (who SHE is). She has
almost no standing reminder of *who YOU are* — your name, that you're the trainer
Scarlet, that you're fixing the Minecraft vessel, how you like her to talk to you.
She has to re-learn/guess it from the last few chat messages every single turn.

**DSP is that standing cheat-sheet.** It's a short block of facts, like:

> User is Scarlet (trainer). Fixing a Minecraft "rift vessel" for Synth. Prefers
> simple, patient explanations. Works on the SynthHeart project.

It's built in the background (from past conversations) and injected so Synth
*always knows who she's talking to*, even at the start of a new session.

**Why it's separate from memory:** memory is *"remember this event"*. DSP is
*"this is a stable fact about the person"*. It changes rarely (a few times a
month), so it's the perfect thing to bake into the start of the prompt.

### What is "role placement"?

Every chat message has a **role**: `system`, `user`, or `assistant`. The model
treats them very differently:

- **`system`** = *"you ARE this"* → the model obeys it hardest. Treated as
  ground truth about how to behave. Highest obedience, but also highest risk:
  if it's wrong or weird, it **corrupts the whole character** and it's hard to
  detect why.
- **`user`** = *"you are talking to this"* → the model treats it as the *other
  side of the conversation*. It reads it like context/information, not as an
  order. Much safer: if it's slightly wrong, Synth just responds a bit off, but
  her personality is never hijacked.

The plan's key decision: **put the DSP in the `user` role, not `system`.** The
reason is exactly the small-LLM fear you have: a bad DSP in `system` can quietly
rewrite who Synth thinks she is ("corrupt character in ways slow to diagnose").
A bad DSP in `user` just makes one reply slightly off — recoverable.

> **Monke summary:** `system` = "you ARE this" (obey, dangerous if wrong).
> `user` = "you're talking to this" (safe to feed info). DSP goes in `user` so a
> mistake in it can't break Synth's personality.

---

## 1. Where caching actually stands (your "it worked once" question)

I checked the code. Here's the truth:

- **Prompt caching IS implemented** — but only for two engines:
  - **Anthropic**: `cache_control: {"type": "ephemeral"}` on the stable
    `system_instruction` block (`core/prompt_renderers.py:472`,
    `engines/external_engines/anthropic.py:360`, gated by `ENABLE_PROMPT_CACHING`).
  - **Gemini**: `cachedContent` (`core/prompt_renderers.py` + `gemini` engines).
- **Your active engine is `openai_compat:Venice2`.** That path uses
  `OpenAIRenderer`, which has **no caching at all** — and Venice's
  OpenAI-compatible endpoint **does not support LCP/prefix caching** anyway.

So you almost certainly **did not break it** — you **switched engines.** Caching
"worked" when your active cortex was Anthropic or Gemini. On Venice it never
applies and never will, because the endpoint doesn't offer it. There's nothing to
fix there; it's an engine capability, not a bug.

> **Monke summary:** caching isn't broken, it's just not possible on Venice.
> It only exists for Claude/Gemini. Don't chase this on the current engine.

### What that means for the SOUL cost math

Because the active path (Venice) has **no caching**, every token you add to the
static layers is paid **on every single turn**. So the earlier "only ~160–180
tokens" estimate is honest but *unamortized* — it's ~160–180 tokens added to
*every* reply, forever. That's exactly why I recommended holding off until the
value is high enough to justify it.

---

## 2. Proposed fixes (in priority order)

### Fix A — Fix the DSP quality first (do this before any wiring)

You can't safely inject a cheat-sheet that ISN'T one. The current DSP is raw
rule-extracted status telemetry:

> `<user_profile>User says they are guessing your res; User says they are
> figuring out how to use it bit by bit, connect to the minecraft vessel now;
> User says they are gonna go fix it, good job baby; ...`

That's not "who the user is" — that's a dump of last week's chat. It's exactly
the kind of thing that corrupts a small model.

**Fix:** change `RuleBasedDspBuilder` (`core/soul/strategies.py:532`) to
**aggregate + dedupe + keep only *stable* facts** — filter out one-off status
lines ("user says they are fixing it now"), keep recurring/biographical facts
(name, role, standing preference), and cap it tightly (e.g. 3–6 facts).
Budget-wise this keeps the block small (~100 tokens) AND makes it trustworthy.

### Fix B — Wire the two *safe* blocks at per-turn, NOT system, position

Wire **only** `soul_session_state` (foresight + emotion snapshot) and the
`{"e":{}}` mood delta — **not** the DSP yet. Put them in the **active user
context** (per-turn), which is where they belong.

- Per-turn mood delta: prepend `{"e": {"joy":..,"fear":..,"sad":..,"anger":..}}`
  to the current user message — this is cheap (~18 tokens) and *correct*, since
  it's by definition per-turn.
- Session state (foresight): inject only when there are **active** foresight
  signals (currently 0 — so today it's `- None`, ~empty). When it's empty, emit
  nothing. This keeps the normal-case cost near zero.

### Fix C — Dedupe the overlapping soul keys before wiring

The plugin returns 4 keys with overlap. `soul_session_state` already embeds the
emotion snapshot AND lists foresight; `soul_active_foresight` duplicates that
foresight; `soul_turn_emotion_delta` re-sends the same emotion. If wired naively
you'd double-inject (~50+ wasted tokens).

**Fix:** wire exactly one rendering path — either `soul_session_state` (contains
foresight + emotion together) *or* the individual keys, never both. I recommend:
render `soul_session_state` (one block) + the per-turn `{"e":{}}` on the user
role, and drop `soul_active_foresight` as a separate line.

### Fix D — Wire DSP only into the `user` role (correct placement)

When DSP quality is fixed (Fix A), place it as a **User-role** context line,
not system. Concretely in `core/prompt_engine.py`, emit the DSP as part of the
conversation/context that reads like "here's who you're talking to" rather than
"you ARE this." Because `_build_context_summary` currently builds into the system
message, getting true user-role placement means emitting it alongside the chat
turns or as a labeled context block that is not an identity directive.

> If you can't get clean user-role placement in this codebase without a big
> refactor, then **keep DSP out** for now — a system-role DSP on a small model is
> riskier than not having a DSP at all.

### Fix E — Reconcile the two emotion systems (bigger, optional)

Currently legacy `emotion_manager` drives the prompt and SOUL's `EmotionalEngine`
only affects recall — split. Long-term, pick ONE. But this is a large change
(you'd rip out `emotion_manager`'s injection and switch to SOUL's 4-dim delta).
Not required for Fixes A–D. Defer it.

### Fix F — Caching (only relevant if you switch engines)

If you ever run the main chat on Anthropic or Gemini, the cache markers already
exist — you just need Fix D (correct layer placement) so the **stable** DSP sits
in the cached `system_instruction` block and the **dynamic** session-state/emotion
sits in the non-cached `context_summary` block. That's the plan's exact
"cache at end of DSP layer" strategy. On Venice, ignore this entirely.

---

## 3. Suggested order of work

1. **Fix A** — improve DSP builder quality & stability (small, pure, unit-testable).
2. **Fix C** — decide the deduped wiring layout (a short design note).
3. **Fix B + D** — wire session-state + emotion delta at per-turn/user position;
   wire DSP into user role only if placement is clean; otherwise leave DSP out.
4. Re-measure a real trace prompt size before/after (target: keep the normal-case
   increase under ~100 tokens).
5. **Fix E/F** — defer as above.

---

## 4. Size expectation after these fixes (Venice, no caching)

| Block | Before (naive) | After (deduped, gated) |
|-------|----------------|------------------------|
| DSP (user role) | ~100t (bad content) | ~80–100t (good content, only if Fix A done) |
| Session state (foresight) | ~40t always | ~0–30t (empty today → ~0) |
| `{"e":{}}` delta | ~18t | ~18t (per-turn, correct) |
| **Normal-case total** | **~160–180t** | **~18–120t** (0 foresight → ~18t + DSP if enabled) |

Net: after deduping and gating empty blocks, the normal-case hit drops to the
per-turn emotion delta (~18 tokens) plus optionally a *stable* user-role DSP
(~80–100 tokens). That's a defensible cost **only after** Fix A makes the DSP
trustworthy; otherwise wire just the delta and session-state.

---

## 5. Files involved

| File | Change |
|------|--------|
| `core/soul/strategies.py` | Fix A: `RuleBasedDspBuilder` — aggregate/dedupe/stable-only facts |
| `core/prompt_engine.py` | Fix B/D: wire `soul_session_state` + `{"e":{}}`; DSP into user role; Fix C dedupe |
| `plugins/soul_plugin/soul_plugin.py` | Guidance: prefer a single rendered path; gate empty foresight/session-state |
| `core/prompt_renderers.py` | Fix F (only if switching engines): ensure DSP in cached system block, dynamic below |
| `tests/soul/*` | Unit-test DSP stability + render wiring |

---

## 6. Bottom line

- **It didn't break; you swapped engines** — caching only exists for
  Anthropic/Gemini and can't apply to Venice.
- **Don't wire the DSP as-is** — the current content is untrustworthy telemetry
  and, placed in `system`, risks corrupting the small model. That's the "role
  placement" concern.
- **The cheap, safe wins**: wire the per-turn `{"e":{}}` delta and gated
  session-state (near-zero today), dedupe the overlapping keys, and quote the
  DSP cost **per turn** (no cache to amortize it).
- **Do DSP only after quality is fixed AND you can place it in the `user` role.**
