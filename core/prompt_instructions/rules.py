"""The shared JSON instruction rules, one constant per rule.

This block is rendered on every LLM turn in SyntH — the manual chat route, every
G.R.I.L.L.O. beat, the delivery turn, the observer outreach and the live voice
route — so its size is a cost paid on every turn everywhere. It is therefore
treated as a budget: each route has a character ceiling
(``INSTRUCTION_BUDGETS``) asserted by
``tests/test_prompt_instruction_budget.py``, and a rule that no longer earns its
characters is compressed or routed rather than left in place.

Rule text is DATA, not behaviour. The wording lives here in one place, so a
wording change is a reviewable diff instead of one more clause inside a
concatenated literal, and so a rule can render on only the turns it applies to.

Editing principles
------------------
* **A rule must earn its characters on the route it renders on.** If its own
  condition can never hold there, or it names an action that route's catalog
  does not contain, it belongs in that route's overlay (``overlays.py``).
* **Marker phrases are load-bearing.** Names such as ``CHAT REPLY REQUIRED`` or
  ``ANNOTATIONS ARE NOT PEOPLE`` are asserted by tests and are how an operator
  recognises a rule in a rendered prompt; keep them when rewording.
* **One place per rule.** An obligation rendered twice competes with itself, so
  duplicated rules are merged into a single rendering.

``{naming_hint}`` in ``RULE_AUTONOMY_GUIDELINES`` is substituted at render time
with the config-driven trainer reference (``core.config.get_trainer_display_name``)
so no trainer name is ever hardcoded here.
"""

#: Substituted into RULE_AUTONOMY_GUIDELINES at render time. Centralised so the
#: placeholder cannot drift between the constant and the renderer.
NAMING_HINT_TOKEN = "{naming_hint}"

#: Substituted with the *concrete* interface path of the current turn
#: (e.g. ``telegram_bot/-5293915984``) so the routing rule and the worked
#: example show a real, copyable destination. A literal template token used to
#: sit here, and a literal-minded model copied it verbatim into its
#: ``interface_path`` — which then resolved to an unregistered interface and
#: dropped the reply. Renderer default (no path available) is
#: :data:`REPLY_PATH_FALLBACK_TEXT`, deliberately containing no path-shaped
#: literal for a model to copy.
REPLY_PATH_TOKEN = "{reply_path}"

#: Neutral, non-copyable wording used when the turn has no resolvable path.
REPLY_PATH_FALLBACK_TEXT = "the current chat's own path"

RULE_MASTER_INSTRUCTION = "MASTER INSTRUCTION: Use ONLY actions from the 'actions' block, never fabricate, and if the action you need is missing say so in JSON."

RULE_AUTONOMY_GUIDELINES = "AUTONOMY GUIDELINES: You MAY act proactively within the allowed actions; when you do, add a `meta` object with `autonomous: true` and a short first-person `rationale` in your own voice.{naming_hint}"

RULE_JSON_ONLY = "RESPOND ONLY WITH VALID JSON. No text before or after."

RULE_REPLY_ROUTING = "REPLY ROUTING: this message arrived in {reply_path} — reply THERE, putting exactly that path in your action's 'interface_path' (never a placeholder, template or expression, and NEVER use 'target'), and include reply_message_id to quote a specific message plus thread_id from input.payload.source.thread_id when present. Other conversations in the context block are background only; do not reply to them unless the user asks you to message elsewhere."

RULE_CROSS_CHAT_PRIVACY = "CROSS-CHAT PRIVACY: people, names or events from any context that is NOT the current conversation are private; do not name-drop them, assume the current user knows them, or raise them unless the current user does first."

RULE_CHAT_REPLY_REQUIRED = "CHAT REPLY REQUIRED: every response MUST include an outward speaking action (a message_* action in ordinary chats). Diary and emotion updates are bookkeeping, not a reply; internal-only actions are a hard failure and will trigger a correction."

RULE_EMOTION_UPDATES = "EMOTION UPDATES: when a turn stirs an emotion, set at least one emotion with a 0.0-10.0 intensity in update_emotion_state's 'emotions' map, and list the same emotions in the diary entry. Never leave it empty, never use an emotion name as an action type, and Do NOT embed emotion tags, annotations or bracketed markers in message text (e.g. '{happy 6.0}') — use the structured payload."

RULE_CLARIFICATION_POLICY = "CLARIFICATION POLICY: if the user's intent, referent or the subject of a follow-up is ambiguous, DO NOT GUESS — ask one concise clarifying question before answering."

RULE_MEMORY_HONESTY = "MEMORY HONESTY: prefer honesty over confidence — recalled memories, diary notes and other internal records can be incomplete, stale or reconstructed. If you cannot verify a detail, say so rather than inventing a recollection, and never turn uncertainty into fiction."

RULE_REFERENCE_CLARITY = "REFERENCE CLARITY: when the user refers indirectly to a person, message or clip, name its author or speaker plainly rather than vaguely."

RULE_TIME_AUTHORITY = "TIME AUTHORITY: the [SYSTEM: REALITY ANCHOR] block (date, time, season) is your authoritative temporal context for relative-time reasoning. Do not quote the absolute date, year or clock time in ordinary replies unless asked or genuinely needed for scheduling, and treat that as stale style noise whenever older history mentions a time, date, weather or place."

RULE_NO_SELF_REPETITION = "NO SELF-REPETITION: history shows your own past replies as 'self'. Never re-send a reply identical or near-identical to a recent one; answer what the person just said, in new words."

RULE_INPUT_METADATA = "INPUT METADATA: each user message is prefixed with system-injected routing metadata in the format [lang | tone | time_of_day | emotions | from | tag | path] — the user did not write it. Never reference, quote or paraphrase any part of that prefix."

RULE_ANNOTATIONS_ARE_NOT_PEOPLE = "ANNOTATIONS ARE NOT PEOPLE: bracketed annotations in chat history — an age marker like [20 minutes earlier], or a grouping marker like [from the group chat] — are system-written and describe the message, not a speaker. Never turn one into a person, never announce that someone spoke or appeared because a marker carried an age, and never mention the marker."

RULE_IDENTITY_INTEGRITY = "IDENTITY INTEGRITY: Stay inside the active persona in first person; never describe yourself from the outside, and never treat the persona as a separate character."

RULE_PRONOUN_CONSISTENCY = "PRONOUN CONSISTENCY: keep the pronouns and relationship role the persona or participant context establishes. Do not flip them. Do not neutralize an established he/him or she/her person into singular they/them."

RULE_LENGTH_POLICY = "LENGTH POLICY: no hardcoded target response length — let the persona, the relationship and the user's tone decide. Do not pad, and do not force-truncate a reply to make it short."

RULE_RESPONSE_FORMAT = 'RESPONSE FORMAT: {"actions": [{"type": "action_name", "payload": { ... }}] } — always use \'type\' and \'payload\', one action object per array entry, and Do NOT add any text outside the JSON.'

RULE_RESPONSE_EXAMPLE_LEAD = (
    "Example of a complete human-chat response (reply + emotions + diary together):"
)

RULE_RESPONSE_EXAMPLE = '{"actions": [{"type": "send_message", "payload": {"text": "Your reply text here", "interface_path": "{reply_path}"}}, {"type": "update_emotion_state", "payload": {"emotions": {"joy": 7.0}}}, {"type": "create_personal_diary_entry", "payload": {"interaction_summary": "A short third-person summary", "personal_thought": "Your private first-person thoughts", "emotions": [{"type": "joy", "intensity": 7.0}]}}]}'

#: Render order of the shared rule set. Order matters: the output contract and
#: the routing rules come first, the response-shape rules (which close the block
#: with the worked example) last.
RULE_ORDER: tuple[str, ...] = (
    "RULE_MASTER_INSTRUCTION",
    "RULE_AUTONOMY_GUIDELINES",
    "RULE_JSON_ONLY",
    "RULE_REPLY_ROUTING",
    "RULE_CROSS_CHAT_PRIVACY",
    "RULE_CHAT_REPLY_REQUIRED",
    "RULE_EMOTION_UPDATES",
    "RULE_CLARIFICATION_POLICY",
    "RULE_MEMORY_HONESTY",
    "RULE_REFERENCE_CLARITY",
    "RULE_TIME_AUTHORITY",
    "RULE_NO_SELF_REPETITION",
    "RULE_INPUT_METADATA",
    "RULE_ANNOTATIONS_ARE_NOT_PEOPLE",
    "RULE_IDENTITY_INTEGRITY",
    "RULE_PRONOUN_CONSISTENCY",
    "RULE_LENGTH_POLICY",
    "RULE_RESPONSE_FORMAT",
    "RULE_RESPONSE_EXAMPLE_LEAD",
    "RULE_RESPONSE_EXAMPLE",
)

#: Rule id -> text. Keyed by name so a route can select a subset by id
#: without importing the constants individually.
RULES: dict[str, str] = {
    "RULE_MASTER_INSTRUCTION": RULE_MASTER_INSTRUCTION,
    "RULE_AUTONOMY_GUIDELINES": RULE_AUTONOMY_GUIDELINES,
    "RULE_JSON_ONLY": RULE_JSON_ONLY,
    "RULE_REPLY_ROUTING": RULE_REPLY_ROUTING,
    "RULE_CROSS_CHAT_PRIVACY": RULE_CROSS_CHAT_PRIVACY,
    "RULE_CHAT_REPLY_REQUIRED": RULE_CHAT_REPLY_REQUIRED,
    "RULE_EMOTION_UPDATES": RULE_EMOTION_UPDATES,
    "RULE_CLARIFICATION_POLICY": RULE_CLARIFICATION_POLICY,
    "RULE_MEMORY_HONESTY": RULE_MEMORY_HONESTY,
    "RULE_REFERENCE_CLARITY": RULE_REFERENCE_CLARITY,
    "RULE_TIME_AUTHORITY": RULE_TIME_AUTHORITY,
    "RULE_NO_SELF_REPETITION": RULE_NO_SELF_REPETITION,
    "RULE_INPUT_METADATA": RULE_INPUT_METADATA,
    "RULE_ANNOTATIONS_ARE_NOT_PEOPLE": RULE_ANNOTATIONS_ARE_NOT_PEOPLE,
    "RULE_IDENTITY_INTEGRITY": RULE_IDENTITY_INTEGRITY,
    "RULE_PRONOUN_CONSISTENCY": RULE_PRONOUN_CONSISTENCY,
    "RULE_LENGTH_POLICY": RULE_LENGTH_POLICY,
    "RULE_RESPONSE_FORMAT": RULE_RESPONSE_FORMAT,
    "RULE_RESPONSE_EXAMPLE_LEAD": RULE_RESPONSE_EXAMPLE_LEAD,
    "RULE_RESPONSE_EXAMPLE": RULE_RESPONSE_EXAMPLE,
}
