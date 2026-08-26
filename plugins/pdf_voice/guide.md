# PDF Voice

Reads a **PDF document aloud**, one chapter at a time.

Synth takes a PDF file (from the shared agent filesystem sandbox), splits it
into chapters/sections, and synthesises one TTS clip per chapter through the
public **Vox** API (`VoxPlugin.speak(generate_only=True)`). Each clip is then
delivered to a chat interface as a playable voice message, or broadcast to the
shared avatar on the WebUI.

This plugin has **no LLM of its own** — it drives Vox, so it subclasses
`PluginBase`.

## Action

| Action | Purpose |
|--------|---------|
| `pdf_to_voice` | Split a PDF into chapters and speak each one. |

**Required field:** `path` — the PDF path inside the agent filesystem sandbox.

**Optional fields:**

| Field | Meaning |
|-------|---------|
| `interface_path` | Chat destination for the audio clips (otherwise they play on the shared avatar). |
| `max_chapters` | Cap how many chapters are spoken (default `PDFVOICE_MAX_CHAPTERS`). |
| `language` | Route TTS to that language's configured engine/voice (`VOX_LANGUAGE_OVERRIDES`). |
| `voice` | Explicit per-call voice override. |

The action declares `security_level: "medium"` and `external_effects:
["filesystem"]` — it reads a file and produces output, so it routes to the
**Agent Lane**.

## Chapter splitting (structural, not keyword)

* **Outline mode** (`PDFVOICE_SPLIT_MODE=outline`, default): boundaries come
  from the PDF's outline/bookmark tree (`PdfReader.outline`). Pages before the
  first entry become an "Introduction" chapter; each entry owns the pages up to
  the next entry. Falls back to size mode when the document has no usable
  outline.
* **Size mode** (`size`): pages are accumulated so every chunk stays within
  `PDFVOICE_MAX_CHUNK_CHARS`; a single oversized page is hard-split. The result
  is capped at `PDFVOICE_MAX_CHAPTERS`.

## Config

| Key | Default | Purpose |
|-----|---------|---------|
| `PDFVOICE_MAX_CHUNK_CHARS` | `8000` | Size-mode chunk budget (characters). |
| `PDFVOICE_MAX_CHAPTERS` | `30` | Hard cap on chapters produced. |
| `PDFVOICE_SPLIT_MODE` | `outline` | `outline` or `size`. |

## Failure behaviour

Everything is fail-safe: a missing/invalid file, a non-PDF, a broken PDF, a
disabled Vox engine (`skipped` / `vox_disabled`), or a per-chapter synthesis
error all degrade to a structured status dict and never raise into the message
chain.
