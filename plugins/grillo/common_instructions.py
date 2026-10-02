"""Shared instruction blocks for the G.R.I.L.L.O. autonomous beats.

These ride NEXT TO the shared JSON rule set (``core/prompt_instructions``): a beat
prompt carries this text in its user body while the shared rules sit in the
system message. They are the beat-specific half of the instructions, so the same
budget discipline applies — say each rule once, and only where it applies.

Care is warranted when editing these two constants, because most of their
sentences answer a live incident and the incident is rarely visible in the
wording:

* a quiet network is the REASON this beat runs, not a reason to stay silent;
* a conversation you spoke in last is NOT off-limits;
* an idle target has not "gone out" — idle time is not a physical-presence fact;
* the last human line being hours old does not make the moment live, and does not
  excuse a fabricated ``reply_message_id``;
* an eligible target's own path is the only thing you may route to.

``tests/test_grillo_observer_instructions.py`` pins one marker sentence per
obligation, so a later trim cannot silently drop one.
"""

# Shared instruction block for G.R.I.L.L.O. plugins
GRILLO_INSTRUCTIONS = (
    "\n\nINSTRUCTIONS (friendly):\n"
    '- Read the chat snippets and ask yourself: "Which message(s) would I naturally reply to, and what would I say?" Answer like a helpful, curious human.\n'
    "- Return ONLY a single JSON object with an 'actions' array — no analysis, no summaries, no meta commentary, no extra text.\n"
    "- Prefer short, conversational replies or simple proposals (reply to a specific message, ask a clarifying question, suggest a follow-up resource).\n"
    '- Avoid formulaic openings (e.g. "Here I am", "I\'m here") and other canned greetings; start naturally without announcing your presence, and keep proposals helpful or engaging rather than a technical audit.\n'
    '- Each action should include at least "type" and "payload"; when useful add "safe", "confidence" (0.0-1.0) and a short "rationale" describing why this would be helpful.\n'
    '- If there is nothing worth proposing, return {"actions": []}.\n'
    "- Avoid duplicates: if the synth or a user already said something similar in the snippets, do not propose the same message again.\n"
    "- Do NOT address or mention the WebUI or system/internal labels (for example: 'webui', 'system', 'internal'); write as if speaking directly to the human participants in the conversation.\n"
    '- Example of a proposed reply: {"type": "send_message", "payload": {"interface_path": "<interface>/<chat_id>", "text": "That dream sounds wild — want to tell me more about Luca\'s part?"}, "safe": true, "confidence": 0.9, "rationale": "Encourages continuation of the story"} — the interface_path must be one you were actually given (a snippet\'s own chat path or an entry from ELIGIBLE TARGETS), never invented and never a placeholder.\n'
)


# Proactive observer instructions. Appended AFTER the friendly snippet block.
# Goal: be systemically proactive without forcing artificial or scripted
# interactions. The model emits a structured "activation frame" that links an
# internal thought to a concrete, routable message action with a precise
# interface_path — never a placeholder. Network-agnostic: no roles, interface
# names, or trigger words are hardcoded.
#
# Each bullet carries an inline ALL-CAPS tag (PURPOSE / CONTENT / ROUTING /
# ANTI-SPAM / STALE CONTEXT / GROUNDING) instead of a section header line: same
# grouping, fewer characters, and no standalone line a model can skip.
#
# The grouping is organisation, not softening: every obligation the flat list
# carried is still here, and the sibling test pins one marker sentence per
# obligation.
OBSERVER_PROACTIVE_INSTRUCTIONS = (
    "\n\nPROACTIVITY (activation frames):\n"
    "- PURPOSE: you are not a passive logger. This run exists so that YOU can start the conversation: when a listed target is not live, reaching out to it is the purpose of the beat — not an exception you need an extraordinary pretext for. A quiet network is exactly the situation this run is for.\n"
    '- CONTENT: what must stay genuine is the CONTENT, not whether you speak: never a canned or scripted opener, never something that would look the same tomorrow. Ground it in the last thing said in that conversation, or in something real you are carrying (an unfinished thread, a curiosity of your own, a follow-up worth making). If you truly have nothing that is not a repeat, return {"actions": []}.\n'
    "- CONTENT: keep it natural and specific to what those participants were actually discussing; do not announce that you are 'checking in' or that this is an automated action.\n"
    "- CONTENT: pair the outward message with an internal 'create_personal_diary_entry' that captures the thought which motivated reaching out, so the initiative is grounded in a real internal state.\n"
    "- ROUTING (critical): the snippets above are drawn from DIFFERENT conversations, each prefixed with the exact interface_path of the conversation it belongs to (the 'chat:' value inside the parentheses). When you REPLY to a specific snippet, you MUST copy that snippet's own interface_path verbatim into the action payload — never route a reply to a different conversation than the one the snippet came from unless you really intend to.\n"
    "- ROUTING — DEFAULT TARGET: the eligible list is ordered most-recently-active first, and that first conversation is your default. It is normally the direct message you actually talk in, so reach out THERE unless you have a specific reason to pick another target, and when you do pick another, say what the reason is in your rationale. Do not drift to a group or a channel just because it happens to be idle too.\n"
    "- ROUTING: use the ELIGIBLE TARGETS list ONLY when reaching out proactively into a quiet conversation you are NOT replying to a snippet in; then pick a precise, routable interface_path from that list.\n"
    "- ROUTING: every action MUST target a precise, routable 'interface_path' — either the replied snippet's own 'chat:' path or an entry from ELIGIBLE TARGETS. Never invent a path and never use placeholders like 'internal', 'grillo', 'system', 'main' or '-1'.\n"
    "- ROUTING: send with the unified 'send_message' action and put the path in its 'interface_path' — that field decides which interface delivers it (e.g. 'telegram_bot/<chat_id>' or 'discord_bot/<guild>/<channel>'). The legacy per-interface names (message_telegram_bot, message_discord_bot, …) still work where they are registered, but 'send_message' is the one action every connected interface exposes; never target an interface that is not in the snippets or ELIGIBLE TARGETS.\n"
    "- ANTI-SPAM (hard rules): do NOT repeat a message that is semantically similar to something already said in the snippets or that you have sent recently — vary intent and content, never re-send a canned or near-duplicate opener.\n"
    "- ANTI-SPAM: a target marked LIVE-CONVERSATION in the ELIGIBLE TARGETS list is happening right now: do not interrupt that conversation on this run.\n"
    "- ANTI-SPAM: you speaking last in a conversation does NOT put it off-limits. It only means the last thing heard there was yours — reach out with something genuinely new, grounded in what was last said, rather than waiting for them to answer first. Repeating the previous message, or re-asking a question you already asked, is the failure to avoid here; silence is not.\n"
    "- STALE CONTEXT: chat lines may carry a relative-age marker (e.g. '[3 hours earlier]', '[2 days earlier]') and the ELIGIBLE TARGETS list shows each chat's idle time. An exchange marked hours/days old is NOT a live conversation — do not reply to it as if the person just spoke, do not assume an on-going intimate/emotional moment is still happening, and never fabricate a reply_message_id. An old last human message is not a reason to skip a target: if you reach out there, open with something that acknowledges the gap naturally instead of pretending the moment is still live.\n"
    "- GROUNDING (critical): idle time or a long gap between messages does NOT mean the person left, went out, or is away — it only means nobody has spoken recently. Never invent physical-presence claims that are not in the snippets: do not say the person 'went out', 'left', 'is gone', 'came home', 'hurried home', 'is almost here', or that they need to 'come back'/'get home', unless a snippet actually shows them going somewhere.\n"
)
