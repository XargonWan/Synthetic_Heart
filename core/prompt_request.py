# core/prompt_request.py
"""Engine-agnostic intermediate prompt representation.

``PromptRequest`` replaces the raw ``dict`` that ``build_prompt_request()`` currently
returns as its sole output.  Every LLM engine renders it natively using a
dedicated renderer (see ``core/prompt_renderers.py``).

Stability order (most cacheable → least):
  1. system_instruction   — persona + rules; stable across all turns
  2. tool_declarations    — action schemas; changes only on plugin reload
  3. context_summary      — diary + memories + bios; formatted plain text
  4. conversation_history — past turns; grows each turn
  5. current_text + runtime_ctx + attachments — fully dynamic

mode values:
  "chat"       — regular user conversation (full context + history)
  "grillo"     — internal autonomous beat (no history, minimal context)
  "delivery"   — mini-prompt for delivering action results to a user
  "live"       — Live API / voice session (renders as flat text string)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Turn:
    """One conversation turn in the history."""

    role: str  # "user" | "assistant"
    content: str  # text; assistant turns hold the raw JSON action string
    timestamp: str | None = None


@dataclass
class RuntimeContext:
    """Fully dynamic per-turn state injected into the current user turn."""

    interface_name: str | None = None
    interface_path: str | None = None
    chat_type: str | None = None  # "group" | "dm", when derivable from interface_path
    message_id: int | None = None
    username: str | None = None
    usertag: str | None = None
    timestamp: str | None = None
    time_of_day: str | None = None
    input_source: str = "text"  # "voice" | "text"
    emotions: str | None = None  # compact NL: "curious 0.7, warm 0.4"
    scope: str = "local"
    language: str | None = None
    tone: str | None = None
    voice_channel_id: str | None = None
    is_grillo_beat: bool = False
    beat_type: str | None = None


@dataclass
class Attachment:
    """A multimodal attachment (image, audio, video, document)."""

    mime_type: str
    data: bytes | str | None = None  # raw bytes or base64 string
    url: str | None = None
    filename: str | None = None
    media_metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class PromptRequest:
    """Engine-agnostic intermediate prompt representation.

    Engines render this into their native wire format.  The legacy dict
    (returned by ``build_prompt_request()``) is kept as-is; this object is
    attached to it under the ``"__prompt_request"`` key so engines can
    opt-in incrementally.

    Attributes:
        system_instruction: Persona + rules block. Stable across turns — ideal
            for prompt caching in Anthropic / Gemini.
        tool_declarations: List of ``ToolManifest`` objects.  Updated only when
            the plugin registry changes.
        context_summary: Diary entries, memories, recent cross-chat context —
            pre-formatted as plain text.  Moderately stable (changes at best
            once per turn when Grillo writes a new diary entry).
        conversation_history: Ordered list of past ``Turn`` objects from the
            active conversation window.
        current_text: The current user message text.
        runtime_ctx: All fully-dynamic per-turn metadata (timestamp, emotions,
            language, etc.).
        attachments: Multimodal attachments for the current turn.
        reply_to: Optional dict describing the message being replied to.
        supports_tool_calling: True when the rendering engine supports native
            function/tool calling (set by the engine, not by build_json_prompt).
        mode: One of "chat", "grillo", "delivery", "live".
    """

    # --- Stable (most cacheable) ---
    system_instruction: str = ""
    tool_declarations: list[Any] = field(default_factory=list)  # list[ToolManifest]

    # --- Moderately stable ---
    context_summary: str = ""

    # --- Dynamic ---
    conversation_history: list[Turn] = field(default_factory=list)
    current_text: str = ""
    runtime_ctx: RuntimeContext = field(default_factory=RuntimeContext)
    attachments: list[Attachment] = field(default_factory=list)
    reply_to: dict[str, Any] | None = None

    # --- Engine-set flags ---
    supports_tool_calling: bool = False

    # --- Mode ---
    mode: str = "chat"  # "chat" | "grillo" | "delivery" | "live"
