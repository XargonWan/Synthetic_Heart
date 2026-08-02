# Message

Core message-delivery plugin. It intercepts the `message_*` actions produced by
the model and passes the right context through to each interface when Synth
sends a reply (for example attaching TTS audio to an outbound message). The set
of handled actions is derived dynamically from the interface registry, so any
enabled interface is supported automatically.

This plugin is **message-chain critical** and cannot be disabled.

## Actions

Dynamically one `message_<interface>` per enabled interface, e.g.:
`message_telegram_bot`, `message_discord_bot`, `message_synth_webui`,
`message_matrix_chat`, `message_reddit`, `message_x`, `message_ollama_serve`.
