# Shared instruction block for G.R.I.L.L.O. plugins

GRILLO_INSTRUCTIONS = (
    "\n\nINSTRUCTIONS:\n"
    "- Be concise: return only the requested JSON object and avoid extra commentary.\n"
    "- Ensure each action includes at least \"type\" and \"payload\"; when relevant include \"safe\", \"confidence\" (0.0-1.0) and a brief \"rationale\".\n"
    "- If you have no useful suggestions, return {\"actions\": []}.\n"
    "- Do not attempt to auto-execute any action; this prompt is for proposal only.\n"
    "- Before deciding to write a message or propose a communication, check the chat snippets above for similar messages or concepts. If you (the assistant) or the synth already authored a similar message, do NOT repeat it—avoid producing duplicate messages or proposals.\n"
    "- Treat messages authored by the synth (e.g., 'SyntH', 'Rekku' or other system agents) as existing proposals to consider when checking for duplicates.\n"
    "- Examples: return JSON like {\"actions\": []} when there are no suggestions; for a proposed message use e.g. {\"type\": \"message_telegram_bot\", \"payload\": {\"text\": \"Hi\"}, \"safe\": true, \"confidence\": 0.85}.\n"
)
