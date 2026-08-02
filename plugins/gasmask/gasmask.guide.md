# Gasmask

A lightweight **persona-protection** layer. It injects guidance that helps
Synth stay in character and resist basic jailbreak / persona-override attempts,
reinforcing her identity (`SYNTH_NAME`) in the prompt.

## Actions

| Action | Purpose |
|--------|---------|
| `static_inject` | Inject persona-protection guidance into the prompt. |

## Configuration

| Key | Purpose |
|-----|---------|
| `SYNTH_NAME` | The persona name reinforced by the guidance. |
