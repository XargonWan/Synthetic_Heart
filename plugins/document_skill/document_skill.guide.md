# Document Skill

A **generic document skill** for the Agentic Runtime. It lets Synth read the
*content* of a document (PDF / DOCX) and split it into its structural sections
(chapters / headings) so it can act on each part separately.

This is a generic capability, not tied to any one use case: producing audio per
chapter is just one consumer. The same actions serve conversion, summarisation,
quoting, and any other "read a document, then do something with each part" task.

## Actions

| Action | Purpose |
|--------|---------|
| `document_extract_text` | Extract the text of a PDF/DOCX with structural page markers. |
| `document_list_sections` | Split a document into its structural sections (chapters/headings). |

Both actions are confined to the same agent filesystem roots as the Agent plugin
(`AGENT_FS_ROOTS`, default `/app` and the log dir).

## Audio-per-chapter (how it composes)

The document skill only *reads and splits* the document. Delivery is done with
the existing interface-native path: for each section, Synth sends a normal
`message_*` action with `send_as_voice` on (the "say audio" flag) plus the text.
No dedicated audio tool is needed — audio is simply a message on the interface
with voice on.

## Design

* **Structural, not keyword-based.** Section detection uses document *shape*
  (numbered headings, short standalone lines, page boundaries) — never a
  language-specific word list.
* **Sandboxed.** Every path is resolved against the agent filesystem roots.
* **Fail-safe.** Missing files, unsupported formats, and extraction errors
  return a structured error dict — never raise into the agent loop.
* **Optional dependency.** `python-docx` is imported lazily; if missing, DOCX
  extraction degrades to a clear error while PDF (`pypdf`) keeps working.

## Configuration

No configuration keys. The plugin is enabled/disabled from the WebUI Plugins tab
like any other plugin.