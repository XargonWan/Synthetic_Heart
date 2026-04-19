# core/prompt_renderers.py
"""Engine-specific renderers for ``PromptRequest``.

Each renderer converts a ``PromptRequest`` into the wire format expected by a
particular LLM API family.  Renderers are pure functions / stateless classes:
they receive a ``PromptRequest``, they produce a data structure.  No I/O.

Renderers added so far:
  - ``OpenAIRenderer``   — baseline (works for every OpenAI-compatible engine)
  - ``AnthropicRenderer`` — Anthropic Messages API with ``cache_control``
  - ``GeminiRenderer``   — Google Gemini REST ``contents`` format

Invariant: ``OpenAIRenderer`` output is valid for every engine that speaks the
OpenAI Chat Completions protocol (Ollama, LM Studio, vLLM, OpenRouter, …).
Engine-specific renderers are enhancements, not replacements.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from core.prompt_request import PromptRequest, RuntimeContext

if TYPE_CHECKING:
    from core.live_tool_registry import ToolManifest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_runtime_prefix(ctx: RuntimeContext) -> str:
    """Compact runtime context prefix injected at the start of the current turn.

    Example output: ``[2026-04-13 15:30 | scope:local | lang:en | from:Scarlet]``
    """
    parts: list[str] = []
    if ctx.timestamp:
        parts.append(ctx.timestamp)
    if ctx.scope and ctx.scope != "local":
        parts.append(f"scope:{ctx.scope}")
    if ctx.language:
        parts.append(f"lang:{ctx.language}")
    if ctx.tone:
        parts.append(f"tone:{ctx.tone}")
    if ctx.emotions:
        parts.append(f"emotions:{ctx.emotions}")
    if ctx.input_source == "voice":
        parts.append("input:voice")
    if ctx.voice_channel_id:
        parts.append(f"voice_chan:{ctx.voice_channel_id}")
    if ctx.username:
        parts.append(f"from:{ctx.username}")
    if ctx.usertag:
        parts.append(f"tag:{ctx.usertag}")
    if ctx.interface_path:
        parts.append(f"path:{ctx.interface_path}")
    if ctx.is_grillo_beat:
        parts.append("grillo:true")
    if ctx.beat_type:
        parts.append(f"beat:{ctx.beat_type}")
    if not parts:
        return ""
    return "[" + " | ".join(parts) + "] "


def _build_multimodal_turn_text(
    ctx: RuntimeContext,
    current_text: str,
    multimodal_parts: list[dict[str, Any]],
) -> str:
    """Build the text companion for a multimodal current turn.

    Image-only turns are especially prone to hallucinated details when they carry
    no user caption. Add a short grounding instruction so OpenAI-compatible
    models describe only visible content instead of filling gaps from prior chat
    context.
    """

    segments: list[str] = []

    prefix = _build_runtime_prefix(ctx).strip()
    if prefix:
        segments.append(prefix)

    user_text = current_text.strip()
    image_count = sum(1 for part in multimodal_parts if part.get("type") == "image_url")
    if image_count:
        if user_text:
            segments.append(
                f"The user attached {image_count} image(s). Ground your reply only "
                "in clearly visible details from the attached image(s). Do not "
                "infer hidden, obscured, or non-visible features. If something is "
                "ambiguous, say that you are unsure."
            )
        else:
            segments.append(
                f"The user attached {image_count} image(s) with no accompanying "
                "text. Describe only clearly visible details from the attached "
                "image(s). Do not infer hidden, obscured, or non-visible features. "
                "If something is ambiguous, say that you are unsure."
            )

    if user_text:
        segments.append(user_text)

    return "\n\n".join(segment for segment in segments if segment)


def _manifest_to_openai_schema(manifest: "ToolManifest") -> dict[str, Any]:
    """Convert a ``ToolManifest`` to the OpenAI function-calling JSON schema."""
    params: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    for p in manifest.parameters:
        prop: dict[str, Any] = {"type": p.type}
        if p.description:
            prop["description"] = p.description
        if p.enum:
            prop["enum"] = p.enum
        params["properties"][p.name] = prop
        if p.required:
            params["required"].append(p.name)

    return {
        "type": "function",
        "function": {
            "name": manifest.name,
            "description": manifest.description,
            "parameters": params,
        },
    }


def _manifest_to_anthropic_tool(manifest: "ToolManifest") -> dict[str, Any]:
    """Convert a ``ToolManifest`` to Anthropic tool format."""
    input_schema: dict[str, Any] = {
        "type": "object",
        "properties": {},
        "required": [],
    }
    for p in manifest.parameters:
        prop: dict[str, Any] = {"type": p.type}
        if p.description:
            prop["description"] = p.description
        if p.enum:
            prop["enum"] = p.enum
        input_schema["properties"][p.name] = prop
        if p.required:
            input_schema["required"].append(p.name)

    return {
        "name": manifest.name,
        "description": manifest.description,
        "input_schema": input_schema,
    }


def _manifest_to_gemini_tool(manifest: "ToolManifest") -> dict[str, Any]:
    """Convert a ``ToolManifest`` to Gemini function declaration format."""
    parameters: dict[str, Any] = {
        "type": "OBJECT",
        "properties": {},
        "required": [],
    }
    _TYPE_MAP = {
        "string": "STRING",
        "integer": "INTEGER",
        "number": "NUMBER",
        "boolean": "BOOLEAN",
        "object": "OBJECT",
        "array": "ARRAY",
    }
    for p in manifest.parameters:
        prop: dict[str, Any] = {"type": _TYPE_MAP.get(p.type, "STRING")}
        if p.description:
            prop["description"] = p.description
        if p.enum:
            prop["enum"] = p.enum
        parameters["properties"][p.name] = prop
        if p.required:
            parameters["required"].append(p.name)

    return {
        "name": manifest.name,
        "description": manifest.description,
        "parameters": parameters,
    }


def _is_tool_manifest(obj: Any) -> bool:
    """Duck-type check: does the object look like a ToolManifest?"""
    return (
        hasattr(obj, "name")
        and hasattr(obj, "description")
        and hasattr(obj, "parameters")
    )


# ---------------------------------------------------------------------------
# OpenAI renderer (baseline — works for every OpenAI-compat engine)
# ---------------------------------------------------------------------------


class OpenAIRenderer:
    """Render a ``PromptRequest`` as an OpenAI Chat Completions ``messages`` list.

    This is the baseline standard format.  Every engine that speaks
    OpenAI-compatible Chat Completions can use this renderer.

    Usage::

        renderer = OpenAIRenderer(req)
        messages = renderer.render()
        tools = renderer.tool_schemas()  # empty list if not tool-calling

        payload = {"model": model, "messages": messages, "max_tokens": ...}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
    """

    def __init__(self, req: PromptRequest) -> None:
        self.req = req

    def render(self) -> list[dict[str, Any]]:
        """Build the full messages list in OpenAI format.

        Returns:
            List of ``{"role": ..., "content": ...}`` dicts.  The first message
            is always a ``system`` message containing the stable instruction +
            context summary.  Subsequent messages alternate user/assistant turns
            from ``conversation_history``, and the final message is the current
            user turn with runtime context injected.
        """
        req = self.req
        messages: list[dict[str, Any]] = []

        # System message = stable persona/rules + moderately-stable context
        system_content = req.system_instruction
        if req.context_summary:
            system_content = system_content + "\n\n" + req.context_summary
        messages.append({"role": "system", "content": system_content})

        # Past turns — preserve them as proper user/assistant pairs
        for turn in req.conversation_history:
            messages.append({"role": turn.role, "content": turn.content})

        # Current user turn with compact runtime context prefix
        prefix = _build_runtime_prefix(req.runtime_ctx)
        current_content = prefix + (req.current_text or "")
        messages.append({"role": "user", "content": current_content})

        return messages

    def render_with_multimodal(
        self,
        multimodal_parts: list[dict[str, Any]],
        supports_vision: bool = True,
    ) -> list[dict[str, Any]]:
        """Render messages and inject multimodal parts into the last user turn.

        Args:
            multimodal_parts: List of OpenAI content-part dicts (image_url /
                input_audio / etc.) to prepend to the current user message.
            supports_vision: When False, image parts are silently dropped
                (audio parts still pass through if present).

        Returns:
            Messages list with the last user turn upgraded to a multipart list.
        """
        messages = self.render()
        if not multimodal_parts:
            return messages

        text = _build_multimodal_turn_text(
            self.req.runtime_ctx,
            self.req.current_text or "",
            multimodal_parts,
        )

        content_parts: list[dict[str, Any]] = []
        for part in multimodal_parts:
            ptype = part.get("type", "")
            if ptype == "image_url" and not supports_vision:
                continue  # silently drop unsupported vision parts
            content_parts.append(part)
        content_parts.append({"type": "text", "text": text})

        # Replace the last user message (the current turn)
        messages[-1] = {"role": "user", "content": content_parts}
        return messages

    def tool_schemas(self) -> list[dict[str, Any]]:
        """Convert ``ToolManifest`` objects to OpenAI function-calling JSON schemas.

        Returns an empty list when ``req.tool_declarations`` is empty or when
        ``req.supports_tool_calling`` is False.
        """
        if not self.req.supports_tool_calling:
            return []
        schemas: list[dict[str, Any]] = []
        for manifest in self.req.tool_declarations:
            if _is_tool_manifest(manifest):
                schemas.append(_manifest_to_openai_schema(manifest))
        return schemas

    @staticmethod
    def parse_tool_call_response(data: dict[str, Any]) -> str:
        """Convert an OpenAI tool_calls response to SyntH JSON action format.

        When the model responds with ``tool_calls`` instead of plain text, this
        method normalises the output to the same
        ``{"actions": [{"type": ..., "payload": ...}]}`` structure the rest of
        the system expects.

        Args:
            data: Parsed JSON response body from the OpenAI Chat Completions API.

        Returns:
            JSON string in SyntH action format, or empty string on failure.
        """
        import json

        choices = data.get("choices") or []
        if not choices:
            return ""

        message = choices[0].get("message") or {}
        tool_calls = message.get("tool_calls") or []

        if not tool_calls:
            # Regular text payload — return as-is
            return str(message.get("content") or "").strip()

        actions: list[dict[str, Any]] = []
        for tc in tool_calls:
            func = tc.get("function") or {}
            name: str = func.get("name") or ""
            try:
                args: dict[str, Any] = json.loads(func.get("arguments") or "{}")
            except Exception:
                args = {}
            if name:
                actions.append({"type": name, "payload": args})

        return json.dumps({"actions": actions}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Anthropic renderer (Phase 5)
# ---------------------------------------------------------------------------


class AnthropicRenderer:
    """Render a ``PromptRequest`` for the Anthropic Messages API.

    Anthropic uses a different format from OpenAI:
    - ``system`` is a *list* of content blocks (allows ``cache_control``)
    - ``messages`` alternate user/assistant turns
    - ``tools`` is separate from ``messages``

    Prompt caching:
    - The stable prefix (``system_instruction``) gets ``cache_control: ephemeral``
      so repeated calls within 5 minutes benefit from cached KV computation.
    - The dynamic ``context_summary`` is a second block *without* cache_control.
    - Set ``enable_caching=False`` (via ``ENABLE_PROMPT_CACHING`` config) to
      disable all ``cache_control`` blocks for predictable billing.

    Usage::

        renderer = AnthropicRenderer(req, enable_caching=True)
        payload = renderer.render()
        # payload keys: "system", "messages" [, "tools"]
    """

    def __init__(self, req: PromptRequest, enable_caching: bool = True) -> None:
        self.req = req
        self.enable_caching = enable_caching

    def render(self) -> dict[str, Any]:
        """Build the Anthropic Messages API payload dict (without model/max_tokens).

        Returns:
            Dict with keys ``"system"``, ``"messages"``, and optionally ``"tools"``.
        """
        req = self.req

        # ── System blocks ─────────────────────────────────────────────
        system_blocks: list[dict[str, Any]] = []

        stable_block: dict[str, Any] = {
            "type": "text",
            "text": req.system_instruction,
        }
        if self.enable_caching:
            stable_block["cache_control"] = {"type": "ephemeral"}
        system_blocks.append(stable_block)

        if req.context_summary:
            # Context summary is moderately dynamic — no cache_control
            system_blocks.append({"type": "text", "text": req.context_summary})

        # ── Messages ──────────────────────────────────────────────────
        messages: list[dict[str, Any]] = []
        for turn in req.conversation_history:
            anthr_role = "assistant" if turn.role == "assistant" else "user"
            messages.append({"role": anthr_role, "content": turn.content})

        # Current user turn
        prefix = _build_runtime_prefix(req.runtime_ctx)
        current_text = prefix + (req.current_text or "")
        messages.append({"role": "user", "content": current_text})

        # ── Result ────────────────────────────────────────────────────
        result: dict[str, Any] = {
            "system": system_blocks,
            "messages": messages,
        }

        tools = self._tool_schemas()
        if tools:
            result["tools"] = tools
            result["tool_choice"] = {"type": "auto"}

        return result

    def render_with_image_parts(
        self, image_parts: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Render and inject Anthropic image content blocks into the last user turn."""
        result = self.render()
        if not image_parts or not result.get("messages"):
            return result

        prefix = _build_runtime_prefix(self.req.runtime_ctx)
        text = prefix + (self.req.current_text or "")

        content: list[dict[str, Any]] = []
        content.extend(image_parts)
        content.append({"type": "text", "text": text})

        result["messages"][-1] = {"role": "user", "content": content}
        return result

    def _tool_schemas(self) -> list[dict[str, Any]]:
        if not self.req.supports_tool_calling:
            return []
        schemas: list[dict[str, Any]] = []
        for manifest in self.req.tool_declarations:
            if _is_tool_manifest(manifest):
                schemas.append(_manifest_to_anthropic_tool(manifest))
        return schemas

    @staticmethod
    def parse_tool_use_response(data: dict[str, Any]) -> str:
        """Convert an Anthropic ``tool_use`` response to SyntH JSON action format."""
        import json

        content_blocks = data.get("content") or []
        if not content_blocks:
            return ""

        text_blocks = [b for b in content_blocks if b.get("type") == "text"]
        tool_use_blocks = [b for b in content_blocks if b.get("type") == "tool_use"]

        if not tool_use_blocks:
            return (
                str((text_blocks[0] or {}).get("text", "")).strip()
                if text_blocks
                else ""
            )

        actions: list[dict[str, Any]] = []
        for block in tool_use_blocks:
            name: str = block.get("name") or ""
            args: dict[str, Any] = block.get("input") or {}
            if name:
                actions.append({"type": name, "payload": args})

        return json.dumps({"actions": actions}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Gemini renderer (Phase 6)
# ---------------------------------------------------------------------------


class GeminiRenderer:
    """Render a ``PromptRequest`` for the Google Gemini REST API.

    Produces a dict with ``contents`` (conversation turns) and
    ``system_instruction_text`` (combined stable + context).
    The engine converts these into native ``google.genai`` types or builds
    the REST payload dict directly.

    Gemini role mapping:
      - "user"      → "user"
      - "assistant" → "model"

    Usage::

        renderer = GeminiRenderer(req)
        rendered = renderer.render()
        # rendered keys: "system_instruction_text", "contents" [, "tools"]
        # Engine merges rendered["contents"] into the REST payload.
    """

    def __init__(self, req: PromptRequest) -> None:
        self.req = req

    def render(self) -> dict[str, Any]:
        """Build a Gemini-compatible prompt dict.

        Returns:
            Dict with:
            - ``"system_instruction_text"`` — stable instruction + context summary
              as a plain string (maps to ``systemInstruction.parts[0].text``).
            - ``"contents"`` — list of ``{role, parts}`` dicts for all turns
              (history + current), matching the Gemini REST ``contents`` schema.
            - ``"tools"`` (optional) — Gemini function declarations list.
        """
        req = self.req

        # System instruction text
        sys_text = req.system_instruction
        if req.context_summary:
            sys_text = sys_text + "\n\n" + req.context_summary

        # Conversation history
        contents: list[dict[str, Any]] = []
        for turn in req.conversation_history:
            gemini_role = "model" if turn.role == "assistant" else "user"
            contents.append({"role": gemini_role, "parts": [{"text": turn.content}]})

        # Current user turn
        prefix = _build_runtime_prefix(req.runtime_ctx)
        current_text = prefix + (req.current_text or "")
        contents.append({"role": "user", "parts": [{"text": current_text}]})

        result: dict[str, Any] = {
            "system_instruction_text": sys_text,
            "contents": contents,
        }

        tools = self._tool_schemas()
        if tools:
            result["tools"] = [{"function_declarations": tools}]

        return result

    def render_with_multimodal(
        self, multimodal_parts: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """Render and inject multimodal parts into the last user turn."""
        result = self.render()
        if not multimodal_parts or not result.get("contents"):
            return result

        prefix = _build_runtime_prefix(self.req.runtime_ctx)
        text = prefix + (self.req.current_text or "")

        parts: list[dict[str, Any]] = []
        parts.extend(multimodal_parts)
        parts.append({"text": text})

        result["contents"][-1] = {"role": "user", "parts": parts}
        return result

    def _tool_schemas(self) -> list[dict[str, Any]]:
        if not self.req.supports_tool_calling:
            return []
        schemas: list[dict[str, Any]] = []
        for manifest in self.req.tool_declarations:
            if _is_tool_manifest(manifest):
                schemas.append(_manifest_to_gemini_tool(manifest))
        return schemas

    @staticmethod
    def parse_function_call_response(data: dict[str, Any]) -> str:
        """Convert a Gemini ``functionCall`` response to SyntH JSON action format."""
        import json

        candidates = data.get("candidates") or []
        if not candidates:
            return ""

        content = (candidates[0] or {}).get("content") or {}
        parts = content.get("parts") or []

        text_parts = [p.get("text", "") for p in parts if "text" in p]
        fc_parts = [p.get("functionCall") for p in parts if "functionCall" in p]

        if not fc_parts:
            return str(text_parts[0]).strip() if text_parts else ""

        actions: list[dict[str, Any]] = []
        for fc in fc_parts:
            if not fc:
                continue
            name: str = fc.get("name") or ""
            args: dict[str, Any] = fc.get("args") or {}
            if name:
                actions.append({"type": name, "payload": args})

        return json.dumps({"actions": actions}, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Text renderer (compact fallback for engines with no conversation-turn support)
# ---------------------------------------------------------------------------


class TextRenderer:
    """Compact single-string renderer for engines without structured input support.

    Produces a single user-message string roughly 35–45% smaller than the
    current ``json.dumps(full_dict, indent=2)`` blob for the same content.

    Differences from the legacy blob:
    - No ``indent=2`` → compact JSON
    - Actions: brief-only text, no full schema
    - History: compact text lines ``"[14:22 user] hi\\n[14:22 synth] hey\\n"``
    - Context summary as a pre-formatted block

    This renderer is the fallback *only*; prefer ``OpenAIRenderer`` for any
    engine that supports the Chat Completions messages format.
    """

    def __init__(self, req: PromptRequest) -> None:
        self.req = req

    def render(self) -> str:
        """Build a compact single-string prompt."""

        req = self.req
        lines: list[str] = []

        lines.append(req.system_instruction)

        if req.context_summary:
            lines.append("\n--- Context ---")
            lines.append(req.context_summary)

        if req.conversation_history:
            lines.append("\n--- History ---")
            for turn in req.conversation_history:
                tag = "assistant" if turn.role == "assistant" else "user"
                lines.append(f"[{tag}] {turn.content}")

        prefix = _build_runtime_prefix(req.runtime_ctx)
        lines.append(f"\n[current] {prefix}{req.current_text or ''}")

        # Compact tool listing (brief only)
        if req.tool_declarations:
            lines.append("\n--- Actions ---")
            for m in req.tool_declarations:
                if _is_tool_manifest(m):
                    lines.append(f"{m.name}: {m.description[:80]}")

        return "\n".join(lines)


class LiveRenderer:
    """Renderer for ``PromptRequest(mode='live')`` plain-text instructions.

    The Live API callers expect one flat instruction string, so this renderer
    intentionally does not emit message arrays or JSON scaffolding.
    """

    def __init__(self, req: PromptRequest) -> None:
        self.req = req

    def render_as_text(self) -> str:
        chunks: list[str] = []
        if self.req.system_instruction:
            chunks.append(self.req.system_instruction)
        if self.req.context_summary:
            chunks.append(self.req.context_summary)
        return "\n\n".join(chunks).strip()
