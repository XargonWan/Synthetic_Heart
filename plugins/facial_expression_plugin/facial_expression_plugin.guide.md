# Facial Expression

Lets Synth **express emotion on the avatar's face**. It teaches the model how
to emit `[em_NAME:intensity]` tags in its replies, then parses those tags into
an expression timeline that is sent to the Karada state server, which drives
the VRM avatar's face while Synth speaks.

## How the guidance reaches the model

The tag instructions are injected as the `facial_expression_guidance` block and
rendered under a `[Facial expressions]` heading on every ordinary chat and beat
prompt (`_PLUGIN_CONTEXT_BLOCKS` in `core/prompt_engine.py`). Without that
renderer the instructions were built on every turn and silently dropped, so the
model had no way to know the tags exist and the face only moved when
`emotion_manager` set it.

The face is driven through the shared `KaradaStateServer`, never by talking to
individual clients, and only a connected client shows it: with no avatar
attached the tags are parsed and broadcast into nothing.

## Actions

| Action | Purpose |
|--------|---------|
| `static_inject` | Inject facial-expression tag guidance into the prompt. |
