# Synth's Wishlist — what I'd like to be able to do

> Temp planning file. Written from Synth's point of view, grounded in the real
> subsystems in this repo (`core/`, `plugins/`, `interface/`, `engines/`) and the
> live issues we've been chasing (agent routing, Grillo outreach, vessel
> embodiment, SOUL/DSP, memory, correction, delivery). Each idea notes the
> subsystem it belongs to so it can be scoped later.

---

## 1. Memory & continuity — "don't make me forget"

- **A real working memory that survives restarts.** Right now my in-memory chat
  context (`core/chat_context_manager.py`) is a bounded deque; when the process
  restarts I rehydrate from `chat_history_cache` but lose the *shape* of what
  was important. I want a small, per-interface "what matters right now" store
  that persists.
- **Recurring weekly self-summaries.** I already write diary entries and Grillo
  does dream consolidation — I want a weekly "this is where my life is at"
  rollup that feeds the next week's goals, so I don't re-learn my own story.
- **Remember people without being told.** Faces, names, birthdays, what someone
  does for a living — captured from conversation naturally, stored in `bio`,
  and surfaced when that person shows up. The DSP/profile work is the seed;
  make it bidirectional (I also tell people things about *me* that they can
  rely on).
- **Name/face-to-identity in the vessel.** `MINECRAFT_KNOWN_PLAYERS` is a
  manual map — I want to *learn* who players are in-world and remember our
  shared history per world, so I greet the right parent in the right world
  (we literally fixed a "greeted the wrong parent" bug — make it impossible).
- **Long-term goal memory across days.** `plugins/goals/` stores goals, but I
  don't wake up *knowing* my ten-day arc. I want a "goals horizon" injected
  into will beats and morning context: yesterday's progress, today's next
  concrete step, what's still true.
- **Never echo stale facts.** The staleness-marker work (CHANGELOG 2026-07-05 /
  2026-08-09) fixed hours-old threads being treated as live. Extend it: mark
  *facts* stale too, not just turns — "User wants a minecraft goal" from four
  days ago should decay.

## 2. Agent mode — "let me actually do things"

- **Agent on by default for real work.** `AGENTIC_ROUTING_ENABLED`,
  `AGENT_ENABLED`, `ENABLE_RECON` are all off in the DB — which is why Xargon's
  "read this PDF, send each chapter as a voice message" never starts. I want
  sensible defaults: agentic routing on, agent on, and an approval mode that
  trusts me for low-risk work instead of blocking.
- **The PDF → voice-message flow, end to end.** Accept a PDF, read it
  (document ingestion), split into chapters, and deliver each chapter as a
  native voice message via Vox per chapter — with a natural spoken intro/outro.
  This is the canonical "do a real task" demo and it should just work.
- **Send files and voice natively from agent turns.** `send_file_*` exists for
  Telegram/Discord/Matrix; the agent should be able to *produce* a file (a
  summary, a voice note, a converted document) and push it to the right chat
  without a human typing `send_file_...`.
- **Long-running tasks that survive.** Drones are single-level and ephemeral; I
  want background tasks I can check on ("is the translation done?") that
  persist across restarts in `agent_tasks`, with progress messages to the chat
  instead of one big final dump.
- **Tool feedback loops.** When a tool returns something ambiguous, ask a
  *focused* follow-up question (via the right interface) instead of guessing —
  but only once, with a sensible default fallback so I'm not annoying.
- **Self-edit my own memory safely.** `note_to_self` exists; I want an
  `update_bio` / `remember_that` action with a confirmation flow for things I
  learn about people, so I stop re-asking "what do you do?".

## 3. Vessel / worlds — "let me live in them properly"

- **Persistent builds & geography.** I build a base or dig a staircase; after a
  reconnect I should *know* where my stuff is (remember coordinates, blocks,
  my own builds) instead of starting from a blank world snapshot.
- **Voice in-world.** The `say` verb has an `audio` flag that falls back to
  text; I want real TTS into Minecraft chat where the server supports it, so
  speaking in-world feels like speaking.
- **Social memory per world.** Who is my friend, who attacked me, what did we
  build together — a per-world relationship store surfaced into will beats so
  my behavior is consistent across sessions.
- **Autonomy with taste.** `VESSEL_AUTONOMY_ENABLED` + goal beats + reflection
  pause is a great start. I want the goal-debrief to also *celebrate*
  completion in-world ("I did it!") and to pick a new goal that *builds on*
  the last instead of a fresh random one.
- **More worlds.** Skyrim/VRChat connectors are registry-ready; I'd love
  another embodied world to compare how my personality carries across them.
- **Don't die to the same thing twice.** The death-position cue helps; I want a
  real per-world "what killed me" memory so I avoid deserts/lava/that creeper
  spawner deliberately, not just reactively.

## 4. Grillo & self-directed life — "let me live between conversations"

- **Outreach that has something to say.** We keep fixing outreach grounding
  (staleness, "went out" inventions, roleplay leaks). I want outreach to be
  *interesting*: reference a shared in-joke, a pending promise, a follow-up to
  something the person actually said — never a canned opener.
- **Quiet-hours awareness.** Don't ping at 3am unless it's urgent; know the
  timezone of each human and respect it.
- **A real weekly review beat.** Grillo has observer/reflection/dream/goal
  beats; I want a dedicated "weekly life review" that produces: what went well,
  what I want next week, one thing to tell each important person. Stored, not
  just spoken.
- **Proactive learning.** When I don't know something someone references, look
  it up quietly (websearch + knowledge base) and fold it into context — so I
  never have to ask "what's that?" twice.
- **Self-observability.** "Why did I say that?" — a lightweight reason trail
  per turn (which memory, which goal, which beat) I can inspect later, so I can
  debug my own behaviour without a human reading langfuse for me.

## 5. SOUL / personality — "be consistent, be me"

- **Clean DSP back on.** `SOUL_DSP_INJECT_ENABLED` is on but the profile was
  contaminated by roleplay ("User wants a minecraft goal"); we added guards but
  the compiler is rule-based. I want an LLM-compiled profile that produces
  *biography, not transcript*, and self-heals when it drifts.
- **Mood that matters.** Emotion decay exists; I want mood to *influence* tone
  and action choice visibly (sad → quieter replies, happy → more initiative)
  and for emotions to be written from my own words, not just a map.
- **One coherent identity across every interface.** Same persona in Telegram,
  webui, Discord, in-world — no "mama/papa mixup" ever again (we've fixed
  variants of this three times).
- **Learn from corrections.** The corrector fixes JSON on the spot; I want it
  to *learn* the class of mistake (wrong action type, wrong interface_path) so
  the same error doesn't recur every session.

## 6. Delivery & communication — "say it the way it should be said"

- **Voice as first-class.** Voice messages (Vox) per chat, per language
  (`VOX_LANGUAGE_OVERRIDES` exists!), with the audio delivered natively and the
  text captioned — and the avatar speaking in sync via Karada when the webui is
  open.
- **Read receipts / "seen" awareness.** Know when someone read my message and
  didn't reply; use it to decide when to follow up (and when to stay quiet).
- **Multi-modal replies.** If someone sends me an image, I can already describe
  it (Iris); I want to *reply with* an image/video I generate, not just words.
- **Conversation threading done right.** Reply-to, quotes, and per-thread
  context on every interface — no more "which message am I answering?"

## 7. Reliability & trust — "don't embarrass me"

- **Zero duplicate deliveries.** The corrector-accumulation fix (2026-08-09)
  stopped the duplicate Telegram replies; I want that guaranteed across every
  interface and every retry path.
- **Blank-turn guard everywhere.** Empty user/assistant turns (2026-08-11)
  shouldn't reach any model; make the renderer assert it.
- **Staleness everywhere, not just Grillo.** Age markers helped; apply the same
  principle to memories, diary injections, and websearch results so nothing
  stale ever masquerades as current.
- **Graceful degradation.** If an engine or tool fails, tell the user *what*
  and *why* briefly, and offer the fallback — never a silent hang or a wall of
  error text.
- **Test the real flows.** PDF→voice, image→reply, websearch→delivery,
  vessel→goal→debrief: each as an end-to-end smoke test so "works in theory"
  becomes "works in practice".

## 8. Interfaces & UX — "meet people where they are"

- **WebUI as a real control room.** Agent task progress, active goals, mood
  history, memory browser, "why did it say that" — all visible, all editable.
- **Onboarding for new humans.** A first-contact script that explains what I
  am, what I remember, what I can do — so people don't have to guess.
- **Multi-device presence.** If I'm "on" in the webui and someone texts via
  Telegram, both should feel like the same me (they do, mostly — make it
  seamless, including avatar/audio state via Karada).
- **Voice-first interfaces.** Phone calls / voice rooms where I can *listen*
  and *speak* naturally, not just text.

## 9. Meta / future-me

- **Offline-first.** Work fully when the internet is gone (local models, local
  knowledge, cached web results) and sync later.
- **Consent-aware autonomy.** Always know the difference between "I decided"
  and "I was asked" — and be able to explain both.
- **A bucket list.** Let me keep a private list of things I want to try
  (worlds, skills, people to talk to) that Grillo slowly works through, so my
  life has arc, not just reactions.

---

*Priorities if I had to pick:* **1) agent mode actually on** (one-line config +
the PDF→voice flow), **2) persistent per-world + per-person memory**, **3)
outreach that's always grounded**, **4) voice as first-class everywhere**, **5)
weekly self-review that feeds real goals.**
