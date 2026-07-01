# core/external_endpoints/bridges/cortex_bridge.py
"""Cortex (LLM) bridge for external endpoints.

Wraps a :class:`BaseProtocolAdapter` in the ``AIPluginBase`` interface so
that any external endpoint registered as a Cortex engine behaves identically
to a built-in engine from the perspective of the SyntH core.
"""

from __future__ import annotations

import asyncio
import base64
import copy
import json
from typing import TYPE_CHECKING, Any, ClassVar

from core.ai_plugin_base import AIPluginBase
from core.external_endpoints.models import EndpointProtocol
from core.logging_utils import log_debug, log_warning

if TYPE_CHECKING:
    from core.external_endpoints.adapters.base import BaseProtocolAdapter
    from core.external_endpoints.models import ExternalEndpoint

# Default completion cap applied ONLY to local-model endpoints (those opting into
# disable_tools / force_action_grammar). Stops a repetition loop from filling the
# whole context window. Cloud openai endpoints (xai, openrouter) are left uncapped
# unless they set max_tokens explicitly.
_LOCAL_MAX_TOKENS_DEFAULT = 4096


# ---------------------------------------------------------------------------
# Multimodal extraction helper
# ---------------------------------------------------------------------------

# Keys whose values can contain lists of multimodal attachment dicts.
_MM_KEYS: frozenset[str] = frozenset(
    {"attachments", "images", "audio", "documents", "videos"}
)
# Subtree-root keys that describe action *schemas*, not actual media data.
_SCHEMA_KEYS: frozenset[str] = frozenset({"actions", "available_actions", "schema"})


def _extract_attachments_and_redact(
    prompt: dict[str, Any],
) -> tuple[dict[str, Any], list[dict[str, str]]]:
    """Extract multimodal attachments from *prompt* and return a redacted copy.

    Recursively walks the prompt dict looking for attachment items that contain
    base64-encoded data (``data`` or ``base64`` field) alongside a ``mime_type``.
    Those are collected as ``{"mime_type": …, "data": …}`` dicts and their
    base64 payload is replaced with a short placeholder in the returned copy so
    the JSON text sent to the model stays compact.

    Returns:
        A ``(redacted_prompt_copy, multimodal_parts)`` tuple.
    """
    redacted = copy.deepcopy(prompt)
    parts: list[dict[str, str]] = []

    def _looks_like_base64_payload(value: str) -> bool:
        candidate = value.strip()
        if not candidate:
            return False
        try:
            base64.b64decode(candidate, altchars=b"-_", validate=True)
            return True
        except Exception:
            padding = (-len(candidate)) % 4
            if padding:
                try:
                    base64.b64decode(
                        candidate + ("=" * padding),
                        altchars=b"-_",
                        validate=True,
                    )
                    return True
                except Exception:
                    return False
            return False

    def _try_extract(item: Any) -> None:
        """If *item* looks like an attachment with base64, collect + redact it."""
        if not isinstance(item, dict):
            return
        mime = item.get("mime_type") or item.get("mimeType")
        if not mime:
            return
        for field in ("data", "base64"):
            b64 = item.get(field)
            if (
                b64
                and isinstance(b64, str)
                and (len(b64) > 256 or _looks_like_base64_payload(b64))
            ):
                parts.append({"mime_type": str(mime), "data": b64})
                item[field] = f"<redacted: {len(b64)} chars>"
                return  # one data field per attachment

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            for key in _MM_KEYS:
                items = node.get(key)
                if isinstance(items, list):
                    for item in items:
                        _try_extract(item)
                elif isinstance(items, dict):
                    _try_extract(items)
            for key, val in node.items():
                if key not in _SCHEMA_KEYS and key not in _MM_KEYS:
                    _walk(val)
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(redacted)
    return redacted, parts


class ExternalCortexEngine(AIPluginBase):
    """AIPluginBase implementation backed by an external endpoint adapter."""

    supports_prompt_request = True

    def __init__(
        self,
        endpoint: "ExternalEndpoint",
        adapter: "BaseProtocolAdapter",
        notify_fn: Any = None,
    ) -> None:
        self._endpoint = endpoint
        self._adapter = adapter
        self._adapter._engine_label = endpoint.name or "cortex_bridge"
        self.notify_fn = notify_fn
        self.display_name = endpoint.display_label or endpoint.name
        self._last_response_metadata: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Multimodal format helpers
    # ------------------------------------------------------------------

    _OPENAI_AUDIO_FORMATS: ClassVar[dict[str, str]] = {
        "audio/wav": "wav",
        "audio/x-wav": "wav",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
    }

    def _format_mm_part(self, part: dict[str, Any]) -> dict[str, Any]:
        """Format a multimodal attachment dict for the endpoint's wire protocol.

        Gemini expects ``{"type": "inline_data", "inline_data": {…}}``,
        while OpenAI-compat endpoints (OpenRouter, Grok, GPT, etc.) only get
        raw binary parts for media types the wire format can express directly.
        Documents are downgraded to metadata-only placeholders so PDF bytes are
        not mislabeled as images.

        The Gemini adapter already converts ``image_url`` → ``inline_data``
        internally, so emitting ``image_url`` is safe for *all* protocols,
        but we default to ``inline_data`` for Gemini to skip the conversion.
        """
        from core.external_endpoints.models import EndpointProtocol

        mime = part["mime_type"]
        data = part.get("data", "")
        filename = part.get("filename")
        extracted_text = part.get("extracted_text")
        extracted_text_truncated = bool(part.get("extracted_text_truncated"))
        page_image_count = int(part.get("page_image_count") or 0)
        page_images_truncated = bool(part.get("page_images_truncated"))

        if self._endpoint.protocol == EndpointProtocol.GEMINI:
            return {
                "type": "inline_data",
                "inline_data": {"mime_type": mime, "data": data},
            }

        if mime.startswith("image/"):
            return {
                "type": "image_url",
                "image_url": {"url": f"data:{mime};base64,{data}"},
            }

        audio_format = self._OPENAI_AUDIO_FORMATS.get(mime.lower())
        if audio_format:
            return {
                "type": "input_audio",
                "input_audio": {"data": data, "format": audio_format},
            }

        document_part: dict[str, Any] = {
            "type": "document",
            "document": {"mime_type": mime},
        }
        if isinstance(filename, str) and filename:
            document_part["document"]["filename"] = filename
        if isinstance(extracted_text, str) and extracted_text.strip():
            document_part["document"]["extracted_text"] = extracted_text
            if extracted_text_truncated:
                document_part["document"]["extracted_text_truncated"] = True
        if page_image_count > 0:
            document_part["document"]["page_image_count"] = page_image_count
            if page_images_truncated:
                document_part["document"]["page_images_truncated"] = True
        return {
            **document_part,
        }

    def _build_mm_parts_from_prompt_request(self, req: Any) -> list[dict[str, Any]]:
        """Convert ``PromptRequest.attachments`` into OpenAI-style content parts."""
        parts: list[dict[str, Any]] = []
        attachments = getattr(req, "attachments", [])
        for attachment in attachments:
            mime = getattr(attachment, "mime_type", None)
            if not isinstance(mime, str) or not mime:
                continue

            filename = getattr(attachment, "filename", None)
            part_meta: dict[str, Any] = {"mime_type": mime}
            if isinstance(filename, str) and filename:
                part_meta["filename"] = filename
            media_metadata = getattr(attachment, "media_metadata", None)
            page_images: list[dict[str, Any]] = []
            if isinstance(media_metadata, dict):
                extracted_text = media_metadata.get("extracted_text")
                if isinstance(extracted_text, str) and extracted_text.strip():
                    part_meta["extracted_text"] = extracted_text
                    if bool(media_metadata.get("extracted_text_truncated")):
                        part_meta["extracted_text_truncated"] = True
                raw_page_images = media_metadata.get("page_images")
                if isinstance(raw_page_images, list):
                    for raw_page_image in raw_page_images:
                        if not isinstance(raw_page_image, dict):
                            continue
                        page_mime = raw_page_image.get("mime_type")
                        page_data = raw_page_image.get("data")
                        if not isinstance(page_mime, str) or not isinstance(
                            page_data, str
                        ):
                            continue
                        page_part: dict[str, Any] = {
                            "mime_type": page_mime,
                            "data": page_data,
                        }
                        page_filename = raw_page_image.get("filename")
                        if isinstance(page_filename, str) and page_filename:
                            page_part["filename"] = page_filename
                        page_images.append(page_part)
                if page_images:
                    part_meta["page_image_count"] = len(page_images)
                    if bool(media_metadata.get("page_images_truncated")):
                        part_meta["page_images_truncated"] = True

            built_part: dict[str, Any] | None = None

            data = getattr(attachment, "data", None)
            if isinstance(data, bytes):
                b64_data = base64.b64encode(data).decode("ascii")
                part_meta["data"] = b64_data
                built_part = self._format_mm_part(part_meta)
            elif isinstance(data, str) and data:
                part_meta["data"] = data
                built_part = self._format_mm_part(part_meta)
            else:
                url = getattr(attachment, "url", None)
                if isinstance(url, str) and url:
                    if mime.startswith("image/"):
                        built_part = {"type": "image_url", "image_url": {"url": url}}
                    else:
                        built_part = self._format_mm_part(part_meta)
                elif part_meta.get("extracted_text") or page_images:
                    built_part = self._format_mm_part(part_meta)

            if built_part is not None:
                parts.append(built_part)

            for page_image in page_images:
                parts.append(self._format_mm_part(page_image))

        return parts

    def _supports_vision_for_mm_parts(self, mm_parts: list[dict[str, Any]]) -> bool:
        """Decide whether image parts should be forwarded for this request."""
        has_image_parts = any(part.get("type") == "image_url" for part in mm_parts)
        if not has_image_parts:
            return True

        if bool((self._endpoint.capabilities or {}).get("vision")):
            return True

        try:
            if bool(self._endpoint.effective_subsystem_map().get("vision")):
                return True
        except Exception:
            pass

        if self._endpoint.default_model:
            log_debug(
                f"[cortex_bridge:{self._endpoint.name}] forwarding image parts "
                f"despite endpoint vision flag being false because default_model="
                f"{self._endpoint.default_model!r} is set"
            )
            return True

        log_warning(
            f"[cortex_bridge:{self._endpoint.name}] dropping image parts because "
            "the endpoint is not marked vision-capable and no explicit model is set"
        )
        return False

    # ------------------------------------------------------------------
    # Core LLM interface
    # ------------------------------------------------------------------

    def _extra_api_kwargs(self) -> dict[str, Any]:
        """Build extra API kwargs derived from ``endpoint.extra_config``.

        Supported keys (set inside the endpoint's *Extra Config* JSON field):

        * ``disable_thinking`` (bool) — pass ``enable_thinking=False`` to the
          API.  Supported by Qwen3 / LM Studio: prevents the model from spending
          the entire context window on chain-of-thought tokens before generating
          a response.  Drastically reduces latency on models that default to
          extended thinking mode.
        * ``force_json_object`` (bool) — request ``response_format={"type":
          "json_object"}`` so the server constrains decoding to syntactically
          valid JSON.  Recommended for small local quants (llama.cpp / LM Studio)
          that otherwise emit malformed action JSON (unescaped quotes, missing
          delimiters) on long replies and trigger corrector retries.
        * ``response_format`` (dict) — forwarded verbatim; use for an explicit
          ``{"type": "json_schema", ...}`` constraint. Takes precedence over
          ``force_json_object``.
        * ``grammar`` (str) — llama.cpp GBNF grammar string, sent via
          ``extra_body`` for the strictest, schema-level constraint.
        * ``force_action_grammar`` (bool) — auto-build a GBNF grammar for the
          action-JSON shape (type enum from the request's actions) and send it
          via ``extra_body``. Implies the in-prompt protocol (no native tools).
          The hardest constraint: the model must emit exactly one well-formed
          ``{"actions":[...]}`` object — no thinking preamble, malformed JSON,
          invented types, or repeated objects. A manual ``grammar`` wins over it.
        * ``max_tokens`` (int) — cap on completion length. An explicit value
          always applies; otherwise a safe default is applied only when this is a
          local-model endpoint (``disable_tools`` / ``force_action_grammar``).
          Cloud openai endpoints stay uncapped unless they set this. Prevents
          repetition loops from filling the whole context window.

        Note: ``response_format`` / ``grammar`` are dropped when native
        tool-calling is active (see ``generate_response``) because tool-calling
        already constrains output and most servers reject the combination.
        """
        extra = self._endpoint.extra_config or {}
        kwargs: dict[str, Any] = {}
        if extra.get("disable_thinking"):
            kwargs["enable_thinking"] = False

        # Cap completion length. An explicit value always wins; otherwise apply a
        # safe default ONLY for local-model endpoints (disable_tools /
        # force_action_grammar) so cloud openai endpoints stay uncapped.
        max_tokens = extra.get("max_tokens")
        if max_tokens is not None:
            try:
                kwargs["max_tokens"] = int(max_tokens)
            except (TypeError, ValueError):
                pass
        elif self._disable_tools():
            kwargs["max_tokens"] = _LOCAL_MAX_TOKENS_DEFAULT

        response_format = extra.get("response_format")
        if response_format is None and extra.get("force_json_object"):
            response_format = {"type": "json_object"}
        if response_format is not None:
            kwargs["response_format"] = response_format

        grammar = extra.get("grammar")
        if grammar:
            extra_body = kwargs.setdefault("extra_body", {})
            extra_body["grammar"] = grammar

        return kwargs

    def _get_retry_settings(self) -> tuple[int, float]:
        extra = self._endpoint.extra_config or {}
        max_retries = int(extra.get("retry_attempts", 3))
        backoff = float(extra.get("retry_backoff", 0.5))
        return max_retries, backoff

    def _retry_on_timeout(self) -> bool:
        extra = self._endpoint.extra_config or {}
        return bool(extra.get("retry_on_timeout", False))

    def _disable_tools(self) -> bool:
        """True when this endpoint opts out of native tool-calling.

        Small local models often ignore native function-calling and emit the
        action JSON in plain content; advertising 49 tools then just confuses
        them. Setting ``disable_tools`` in extra_config forces the legacy
        in-prompt JSON-action protocol instead (the action catalog is folded
        into the system prompt by ``_inject_actions_into_prompt``).

        ``force_action_grammar`` also implies this: a GBNF grammar constrains the
        *content* output, which only makes sense without native tool-calling.
        """
        extra = self._endpoint.extra_config or {}
        return bool(extra.get("disable_tools") or extra.get("force_action_grammar"))

    def _build_action_grammar(self, prompt_request: Any) -> str | None:
        """Build a GBNF grammar for the action JSON, or ``None``.

        Opt-in via ``extra_config.force_action_grammar``. A manual ``grammar`` in
        extra_config takes precedence (handled in ``_extra_api_kwargs``), so this
        returns ``None`` when one is set. The grammar's ``type`` enum is the exact
        set of action names offered for this request, so the model cannot invent
        or duplicate types and must emit a single well-formed object.
        """
        extra = self._endpoint.extra_config or {}
        if not extra.get("force_action_grammar") or extra.get("grammar"):
            return None
        try:
            from core.external_endpoints.action_grammar import build_actions_gbnf

            names = sorted(
                {
                    getattr(m, "name", None)
                    for m in (getattr(prompt_request, "tool_declarations", None) or [])
                }
                - {None}
            )
            return build_actions_gbnf(names)
        except Exception as exc:
            log_warning(
                f"[cortex_bridge:{self._endpoint.name}] action grammar build failed: {exc}"
            )
            return None

    def _build_fallback_action_grammar(self) -> str | None:
        """Build a catalog-wide action grammar for prompts without a PromptRequest.

        Opt-in via ``extra_config.force_action_grammar`` (a manual ``grammar``
        in extra_config still wins, handled in ``_extra_api_kwargs``). Used when
        a caller sends a raw string/dict prompt — notably the corrector's
        JSON-correction retries — so the strict action-JSON shape is still
        enforced even though no scoped ``tool_declarations`` are available. The
        ``type`` enum is the full set of action types from the core actions
        block. Returns ``None`` when the endpoint hasn't opted in or no action
        names are available, so callers simply skip attaching a grammar.
        """
        extra = self._endpoint.extra_config or {}
        if not extra.get("force_action_grammar") or extra.get("grammar"):
            return None
        try:
            from core.core_initializer import core_initializer
            from core.external_endpoints.action_grammar import build_actions_gbnf

            available = (
                core_initializer.actions_block.get("available_actions", {}) or {}
            )
            names = sorted(n for n in available.keys() if isinstance(n, str))
            return build_actions_gbnf(names)
        except Exception as exc:
            log_debug(
                f"[cortex_bridge:{self._endpoint.name}] fallback action grammar build failed: {exc}"
            )
            return None

    def _inject_actions_into_prompt(self, prompt_request: Any) -> None:
        """Fold the scoped action catalog into the system prompt.

        Needed when native tools are disabled: the PromptRequest path otherwise
        delivers the available actions *only* as native tool declarations, so
        without this the model would lose its action catalog entirely. The
        catalog is built from the same actions that would have been offered as
        tools, so nothing is lost — it is just delivered as text.
        """
        try:
            from core.config_manager import config_registry
            from core.core_initializer import core_initializer
            from core.prompt_engine import minify_actions_block

            names: set[str] = {
                str(n)
                for m in (getattr(prompt_request, "tool_declarations", None) or [])
                if (n := getattr(m, "name", None)) is not None
            }
            if not names:
                return

            # Drop message_* actions that belong to other interfaces — the
            # model only needs the one matching its current interface.
            _rtx = getattr(prompt_request, "runtime_ctx", None)
            _iface: str = str(getattr(_rtx, "interface_name", "") or "").strip()
            if _iface:
                names = {
                    n
                    for n in names
                    if not n.startswith("message_") or n == f"message_{_iface}"
                }

            # Drop animation/visual actions when no animation client is connected.
            # Emitting use_animation with no WebUI open triggers a corrector pass.
            _animation_names = {"use_animation", "tts_speak"}
            if names & _animation_names:
                try:
                    from core.animation_handler import get_karada_state_server

                    _srv = get_karada_state_server()
                    if _srv is None or not _srv.has_connected_clients():
                        names -= _animation_names
                except Exception:
                    pass

            raw = core_initializer.actions_block.get("available_actions", {}) or {}
            scoped = {k: v for k, v in raw.items() if k in names}
            if not scoped:
                return

            is_lite: bool = bool(config_registry.get_value("PROMPT_LITE_MODE", False))
            catalog = minify_actions_block(scoped, lite=is_lite)
            # Render as a flat, unambiguous list — NOT a nested JSON dict. The
            # nested ``{name: {brief, schema}}`` shape made small models emit
            # sub-keys like "brief" as action types.
            lines: list[str] = []
            for name, spec in catalog.items():
                if not isinstance(spec, dict):
                    lines.append(f"- {name}")
                    continue
                brief = str(spec.get("brief", "") or "").strip()
                schema = spec.get("schema")
                props = (
                    list((schema.get("properties") or {}).keys())
                    if isinstance(schema, dict)
                    else []
                )
                line = f"- {name}"
                if brief:
                    line += f": {brief}"
                if props:
                    line += f" (payload keys: {', '.join(str(p) for p in props)})"
                lines.append(line)

            block = (
                "\n\n=== AVAILABLE ACTIONS ===\n"
                'Reply ONLY as {"actions":[{"type":"<action_name>","payload":{...}}]}. '
                'Each "type" MUST be exactly one of the action names below — do not '
                "invent, combine, or abbreviate names, and never use a payload key as "
                "a type:\n" + "\n".join(lines)
            )
            prompt_request.system_instruction = (
                getattr(prompt_request, "system_instruction", "") or ""
            ) + block
        except Exception as exc:
            log_warning(
                f"[cortex_bridge:{self._endpoint.name}] action-catalog injection failed: {exc}"
            )

    @staticmethod
    def _is_retryable_exception(exc: Exception) -> bool:
        if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
            return True
        msg = str(exc).lower()
        transient_api_markers = (
            "503",
            "502",
            "504",
            "429",
            "unavailable",
            "service unavailable",
            "bad gateway",
            "gateway timeout",
            "too many requests",
            "rate limit",
            "resource exhausted",
            "overloaded",
            "high demand",
            "try again later",
        )
        return any(
            token in msg
            for token in (
                "connection",
                "timeout",
                "refused",
                "reset",
                "temporarily unavailable",
                "dns",
                "unreachable",
                *transient_api_markers,
            )
        )

    def _get_request_timeout(self) -> float:
        """Get the request timeout from endpoint extra_config or the configured default.

        Precedence: per-endpoint ``extra_config["timeout"]`` → the global
        ``LLM_GENERATION_TIMEOUT_SEC`` config var (env/.env/WebUI tunable) →
        a generous hard fallback. The default is intentionally large so slow
        hardware is not silently cut off mid-generation (which closes the socket
        and makes llama.cpp cancel the task).
        """
        extra = self._endpoint.extra_config or {}
        timeout = extra.get("timeout")
        if timeout is not None:
            try:
                return float(timeout)
            except (ValueError, TypeError):
                pass
        try:
            from core.config_manager import config_registry

            return float(
                config_registry.get_value(
                    "LLM_GENERATION_TIMEOUT_SEC", 1800, value_type=int
                )
            )
        except Exception:
            return 1800.0

    def _tool_api_kwargs(self, prompt: Any) -> dict[str, Any]:
        """Build adapter kwargs derived from a typed PromptRequest.

        Preserve native tool declarations for external protocols that support
        function/tool calling so adapters can parse structured tool responses.
        """
        try:
            from core.prompt_renderers import (
                AnthropicRenderer,
                GeminiRenderer,
                OpenAIRenderer,
            )
            from core.prompt_request import PromptRequest

            prompt_request: PromptRequest | None = None
            if isinstance(prompt, PromptRequest):
                prompt_request = prompt
            elif isinstance(prompt, dict):
                candidate = prompt.get("__prompt_request")
                if isinstance(candidate, PromptRequest):
                    prompt_request = candidate

            if prompt_request is None or not prompt_request.tool_declarations:
                # No typed PromptRequest — e.g. the corrector's JSON-correction
                # retries pass a raw string prompt (and recon may pass a plain
                # dict). On force_action_grammar endpoints, still constrain the
                # output by falling back to a grammar built from the full
                # registered action catalog, so these retries don't regress to
                # unconstrained JSON that a small local model can't recover from
                # (which otherwise exhausts the corrector). Gated on the
                # endpoint's extra_config, so other engines are never affected.
                fallback = self._build_fallback_action_grammar()
                return {"extra_body": {"grammar": fallback}} if fallback else {}

            if self._disable_tools():
                # Force the legacy in-prompt JSON-action protocol: keep native
                # tools off, fold the action catalog into the system prompt, and
                # leave supports_tool_calling False so the renderer expects a
                # JSON-in-content reply rather than tool_calls.
                prompt_request.supports_tool_calling = False
                self._inject_actions_into_prompt(prompt_request)
                grammar = self._build_action_grammar(prompt_request)
                if grammar:
                    return {"extra_body": {"grammar": grammar}}
                return {}

            prompt_request.supports_tool_calling = True

            if self._endpoint.protocol is EndpointProtocol.GEMINI:
                rendered = GeminiRenderer(prompt_request).render()
                tools = rendered.get("tools") or []
                return {"tools": tools} if tools else {}

            if self._endpoint.protocol is EndpointProtocol.OPENAI:
                tools = OpenAIRenderer(prompt_request).tool_schemas()
                return {"tools": tools, "tool_choice": "auto"} if tools else {}

            if self._endpoint.protocol is EndpointProtocol.ANTHROPIC:
                rendered = AnthropicRenderer(prompt_request).render()
                tools = rendered.get("tools") or []
                if not tools:
                    return {}
                payload: dict[str, Any] = {"tools": tools}
                tool_choice = rendered.get("tool_choice")
                if tool_choice:
                    payload["tool_choice"] = tool_choice
                return payload

            return {}
        except Exception as exc:
            log_debug(
                f"[cortex_bridge:{self._endpoint.name}] tool extraction skipped: {exc}"
            )
            return {}

    async def generate_response(self, messages: list[dict[str, Any]] | Any) -> str:
        """Forward ``messages`` to the external endpoint and return the response text.

        Accepts either a list of OpenAI-style message dicts (e.g. from recon) or a
        SyntH JSON-prompt dict/str — same flexible contract as the built-in engines.
        """
        prompt_extra_kwargs: dict[str, Any] = {}
        if isinstance(messages, list):
            msg_list = messages
            # Ensure sufficient output tokens for structured responses (e.g. Recon JSON).
            # Most adapters default to 1024 which can truncate multi-field JSON output.
            prompt_extra_kwargs.setdefault("max_tokens", 4096)
        else:
            prompt_extra_kwargs = self._tool_api_kwargs(messages)
            msg_list = self._build_messages(messages)

        model = self._endpoint.default_model
        if not model and self._endpoint.available_models:
            model = self._endpoint.available_models[0]
        self._last_response_metadata = {}
        max_retries, backoff = self._get_retry_settings()
        request_timeout = self._get_request_timeout()
        retry_on_timeout = self._retry_on_timeout()
        attempt = 0
        while True:
            attempt += 1
            try:
                extra_kwargs = self._extra_api_kwargs()
                extra_kwargs.update(prompt_extra_kwargs)
                # Native tool-calling already constrains output, and most
                # OpenAI-compatible servers reject response_format alongside
                # tools — so let tool-calling win when both are present.
                if "tools" in extra_kwargs:
                    extra_kwargs.pop("response_format", None)
                    grammar_body = extra_kwargs.get("extra_body")
                    if isinstance(grammar_body, dict):
                        grammar_body.pop("grammar", None)
                        if not grammar_body:
                            extra_kwargs.pop("extra_body", None)
                # A grammar is the strongest constraint; response_format is then
                # redundant and can conflict on some servers, so drop it.
                _eb = extra_kwargs.get("extra_body")
                if isinstance(_eb, dict) and _eb.get("grammar"):
                    extra_kwargs.pop("response_format", None)
                extra_kwargs.setdefault("timeout", request_timeout)
                chat_resp = await asyncio.wait_for(
                    self._adapter.chat_completion(
                        msg_list, model=model, **extra_kwargs
                    ),
                    timeout=request_timeout,
                )
                response_metadata: dict[str, Any] = {
                    "model": getattr(chat_resp, "model", None) or model,
                    "finish_reason": getattr(chat_resp, "finish_reason", None)
                    or "stop",
                    "empty_response": not bool(getattr(chat_resp, "content", "")),
                }
                adapter_response_metadata = getattr(
                    self._adapter,
                    "_last_completion_metadata",
                    None,
                )
                if isinstance(adapter_response_metadata, dict):
                    for key, value in adapter_response_metadata.items():
                        if value is None or value == "":
                            continue
                        response_metadata[key] = value
                self._last_response_metadata = response_metadata
                return chat_resp.content
            except asyncio.TimeoutError:
                log_warning(
                    f"[cortex_bridge:{self._endpoint.name}] generate_response timed out "
                    f"after {request_timeout}s (attempt {attempt}/{max_retries})"
                )
                should_retry = retry_on_timeout and attempt < max_retries
                if should_retry:
                    delay = backoff * (2 ** (attempt - 1))
                    log_warning(
                        f"[cortex_bridge:{self._endpoint.name}] timed out, retrying in {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                raise TimeoutError(
                    f"LLM request timed out after {request_timeout}s "
                    f"and {max_retries} retry attempts"
                )
            except Exception as exc:
                should_retry = attempt < max_retries and self._is_retryable_exception(
                    exc
                )
                if should_retry:
                    delay = backoff * (2 ** (attempt - 1))
                    log_warning(
                        f"[cortex_bridge:{self._endpoint.name}] generate_response failed "
                        f"(attempt {attempt}/{max_retries}): {exc}; retrying in {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                log_warning(
                    f"[cortex_bridge:{self._endpoint.name}] generate_response failed: {exc}"
                )
                raise

    def _build_messages(self, prompt: Any) -> list[dict[str, Any]]:
        """Convert a SyntH prompt into an OpenAI-style messages list.

        Extracts ``instructions_verbose`` (or ``instructions``) from a SyntH prompt
        dict and places it as a ``system`` role message so the LLM receives explicit
        instructions rather than a single raw JSON blob in the user turn.
        """
        try:
            from core.prompt_request import PromptRequest
            from core.prompt_renderers import OpenAIRenderer

            if isinstance(prompt, PromptRequest):
                renderer = OpenAIRenderer(prompt)
                mm_parts = self._build_mm_parts_from_prompt_request(prompt)
                if mm_parts:
                    supports_vision = self._supports_vision_for_mm_parts(mm_parts)
                    return renderer.render_with_multimodal(
                        mm_parts,
                        supports_vision=supports_vision,
                    )
                return renderer.render()
        except Exception as exc:
            log_debug(
                f"[cortex_bridge] direct PromptRequest rendering fallback to text: {exc}"
            )

        if not isinstance(prompt, dict):
            _parsed_prompt: Any = None
            if isinstance(prompt, str):
                try:
                    _parsed_prompt = json.loads(prompt)
                except Exception:
                    pass
            if isinstance(_parsed_prompt, dict):
                prompt = _parsed_prompt
            else:
                content: str = prompt if isinstance(prompt, str) else str(prompt)
                return [{"role": "user", "content": content}]

        if "system_message" in prompt:
            return self._build_correction_messages(prompt)

        prompt_request = prompt.get("__prompt_request")
        if prompt_request is not None:
            try:
                from core.prompt_renderers import OpenAIRenderer
                from core.prompt_request import PromptRequest

                if isinstance(prompt_request, PromptRequest):
                    renderer = OpenAIRenderer(prompt_request)
                    mm_parts = self._build_mm_parts_from_prompt_request(prompt_request)
                    if mm_parts:
                        supports_vision = self._supports_vision_for_mm_parts(mm_parts)
                        return renderer.render_with_multimodal(
                            mm_parts,
                            supports_vision=supports_vision,
                        )
                    return renderer.render()
            except Exception as exc:
                log_debug(
                    f"[cortex_bridge] PromptRequest rendering fallback to dict path: {exc}"
                )

        instructions: str = (
            prompt.get("instructions_verbose") or prompt.get("instructions") or ""
        )
        # Strip keys elevated to system; sanitize the rest (handles non-serializable
        # objects like the PromptRequest dataclass via __dict__ conversion).
        from core.json_utils import sanitize_for_json

        _skip = {"instructions", "instructions_verbose", "__prompt_request"}
        user_dict = {k: v for k, v in prompt.items() if k not in _skip}

        # Extract multimodal attachments before serialising to text so that
        # base64 blobs don't waste context tokens on the text side.
        redacted, mm_parts = _extract_attachments_and_redact(user_dict)
        redacted = sanitize_for_json(redacted)
        user_content = json.dumps(redacted, ensure_ascii=False)

        # Build user message — multipart if we have attachments
        if mm_parts:
            content_parts: list[dict[str, Any]] = [
                {"type": "text", "text": user_content}
            ]
            for p in mm_parts:
                content_parts.append(self._format_mm_part(p))
            log_debug(
                f"[cortex_bridge] _build_messages: extracted {len(mm_parts)} "
                f"multimodal part(s)"
            )
            user_msg_content: Any = content_parts
        else:
            user_msg_content = user_content

        if instructions:
            return [
                {"role": "system", "content": str(instructions)},
                {"role": "user", "content": user_msg_content},
            ]
        return [{"role": "user", "content": user_msg_content}]

    def _build_correction_messages(
        self, payload: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Convert a corrector payload into properly role-separated messages.

        The corrector sends ``{"system_message": {...}}`` as a flat JSON blob.
        Splitting it into system / assistant / user roles gives the LLM clear
        signal about what each part is, which meaningfully improves coherence
        compared to receiving a single large user-role JSON string.
        """
        sm = payload.get("system_message") or {}
        correction_instruction = str(sm.get("message") or "")
        your_reply = str(sm.get("your_reply") or "")
        original_user_message = str(sm.get("original_user_message") or "")
        required_format = sm.get("required_format")
        strict_requirements = sm.get("strict_requirements") or []

        user_parts: list[str] = []
        if original_user_message:
            user_parts.append(f"Original user message:\n{original_user_message}")
        if required_format:
            user_parts.append(
                f"Required format:\n{json.dumps(required_format, ensure_ascii=False)}"
            )
        if strict_requirements:
            reqs = "\n".join(f"- {r}" for r in strict_requirements)
            user_parts.append(f"Strict requirements:\n{reqs}")
        user_parts.append("Respond with ONLY valid JSON.")
        user_content = (
            "\n\n".join(user_parts)
            if user_parts
            else "Provide a corrected JSON response."
        )

        messages: list[dict[str, Any]] = [
            {"role": "system", "content": correction_instruction},
        ]
        if your_reply:
            messages.append({"role": "assistant", "content": your_reply})
        messages.append({"role": "user", "content": user_content})
        log_debug(
            f"[cortex_bridge] correction payload split into {len(messages)} role-separated messages"
        )
        return messages

    async def handle_incoming_message(
        self, bot: Any, message: Any, prompt: Any
    ) -> str | None:
        """Process a pre-built SyntH JSON prompt and return the LLM response text.

        The message_chain (plugin_instance) drives the full pipeline; this method
        only handles the LLM call — same contract as openrouter/gemini engine.
        Correction prompts are forwarded to the engine like any other prompt;
        the corrector loop is managed entirely by the message chain.
        """
        return await self.generate_response(prompt)

    # NOTE: generate_response already uses _extra_api_kwargs(), so all call
    # paths (Recon via generate_response, main LLM via handle_incoming_message)
    # benefit from extra_config settings such as ``disable_thinking``.

    # ------------------------------------------------------------------
    # Model / capability info
    # ------------------------------------------------------------------

    @property
    def model_limits_map(self) -> dict[str, int]:
        """Return max_chars budget per model — read from extra_config or use a safe default."""
        extra = self._endpoint.extra_config or {}
        max_chars = int(extra.get("max_chars", 100_000))
        return {"default": max_chars}

    def get_supported_models(self) -> list[str]:
        if self._endpoint.available_models:
            return list(self._endpoint.available_models)
        if self._endpoint.default_model:
            return [self._endpoint.default_model]
        return []

    def get_supported_action_types(self) -> list[str]:
        return []

    @staticmethod
    def get_supported_actions() -> dict[str, Any]:
        return {}

    def get_rate_limit(self) -> tuple[int, int, float]:
        # Conservative default; users can adjust via extra_config
        extra = self._endpoint.extra_config or {}
        return (
            int(extra.get("rate_limit_requests", 60)),
            int(extra.get("rate_limit_window", 60)),
            float(extra.get("rate_limit_min_interval", 0.0)),
        )

    # ------------------------------------------------------------------
    # Health check (optional, used by core_initializer)
    # ------------------------------------------------------------------

    def get_health_status(self) -> tuple[bool, str]:
        """Sync wrapper — always returns (True, '') to avoid blocking startup."""
        return True, ""

    # ------------------------------------------------------------------
    # Streaming
    # ------------------------------------------------------------------

    def set_current_model(self, model: str) -> None:
        """Set the active model used for completions (called by the WebUI Components tab)."""
        if not model:
            raise ValueError("model name cannot be empty")
        models = self.get_supported_models()
        if models and model not in models:
            raise ValueError(f"Model '{model}' is not in the available list: {models}")
        self._endpoint.default_model = model

    def get_current_model(self) -> str | None:
        """Return the currently active model name."""
        return self._endpoint.default_model

    async def stream_response(self, messages: list[dict[str, Any]]):
        """Yield text chunks from a streaming completion."""
        model = self._endpoint.default_model or None
        request_timeout = self._get_request_timeout()
        try:
            async for chunk in self._adapter.stream_chat_completion(
                messages,
                model=model,
                timeout=request_timeout,
            ):
                yield chunk
        except asyncio.TimeoutError:
            log_warning(
                f"[cortex_bridge:{self._endpoint.name}] stream_response timed out "
                f"after {request_timeout}s"
            )
            raise TimeoutError(
                f"LLM streaming request timed out after {request_timeout}s"
            )
        except Exception as exc:
            log_warning(
                f"[cortex_bridge:{self._endpoint.name}] stream_response failed: {exc}"
            )
            raise


# Required by CortexRegistry::load_engine() when loading via module path
PLUGIN_CLASS = ExternalCortexEngine
