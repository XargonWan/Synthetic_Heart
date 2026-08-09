# Bio Manager

Stores and retrieves **per-user biographical notes** — what Synth knows about
the people she talks to (and about herself). A short bio of the current
participants is injected into the prompt so Synth remembers who she is speaking
with. The full detail can be requested on demand.

Backed by the `bio` table.

## Actions

| Action | Purpose |
|--------|---------|
| `static_inject` | Inject short participant bios into the prompt. |
| `bio_full_request` | Retrieve the full bio for a participant. |
| `bio_update` | Add or update a bio fact. |
