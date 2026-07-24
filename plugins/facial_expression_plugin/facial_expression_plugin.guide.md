# Facial Expression

Lets Synth **express emotion on the avatar's face**. It teaches the model how
to emit `[em_NAME:intensity]` tags in its replies, then parses those tags into
an expression timeline that is sent to the Karada state server, which drives
the VRM avatar's face while Synth speaks.

## Actions

| Action | Purpose |
|--------|---------|
| `static_inject` | Inject facial-expression tag guidance into the prompt. |
