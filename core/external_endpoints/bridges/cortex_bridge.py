# core/external_endpoints/bridges/cortex_bridge.py
"""Cortex (LLM) bridge for external endpoints.

Wraps a :class:`BaseProtocolAdapter` in the ``AIPluginBase`` interface so
that any external endpoint registered as a Cortex engine behaves identically
to a built-in engine from the perspective of the SyntH core.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
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

# Hard downstream character budget for the fully assembled OpenAI-style messages
# sent to an endpoint, enforced by ``_clamp_messages_to_char_budget``. Kept
# safely below the ``selenium-llm-engine`` 32000-char chunking threshold so the
# chunking path — and its "reply only OK" protocol contamination / empty-actions
# garbling — is never triggered, regardless of how large the injected action
# catalog is.
#
# IMPORTANT — serialization headroom: the clamp measures the *content* length of
# each message (``_message_content_len``), but the payload the endpoint actually
# receives is the role-separated serialization of those messages, which is
# meaningfully larger (role labels, JSON framing/escaping, chat-template glue).
# Measured LIVE (2026-07-28) on the selenium-llm-engine: a content-sum clamped to
# exactly 27000 chars produced a real selenium prompt of 32232 chars — i.e.
# ~5200 chars of serialization/template overhead — which STILL exceeded the
# 32000 limit and triggered 2-part chunking (garbled request → empty
# ``{"actions": []}`` → corrector loop → "😵" fallback). The overhead is far
# larger than the ~2600 originally assumed. The budget is therefore set to 24000
# (32000 − ~5200 measured overhead − ~2800 safety margin) so the final serialized
# payload stays comfortably under 32000 even on the heaviest turns. Applies to
# every openai-protocol endpoint; harmless for endpoints that accept larger
# prompts (their payloads are already below this) unless overridden via
# ``extra_config["downstream_char_budget"]`` (a non-positive value disables the
# clamp entirely). Unlike the up-front ``max_chars`` reducer, this clamp only
# trims the *user* body and never the system message (instructions + action
# catalog), so protected content is never lost even when it alone exceeds the
# budget.
_DEFAULT_DOWNSTREAM_CHAR_BUDGET = 24000

# Native tools remain off by default for compatibility with endpoints/models
# that do not support function calling. Individual endpoints may opt in with
# ``extra_config.enable_tools: true``.
_NATIVE_TOOLS_ENABLED = False

# Venice currently rejects requests containing more than 20 function
# definitions for the affected Gemma endpoint.  Keep this as a provider
# default rather than making every OpenAI-compatible endpoint pay the same
# restriction; arbitrary endpoints can opt into their own limit with the
# ``max_tools`` extra-config key.
_VENICE_MAX_NATIVE_TOOLS = 20

# When a connected Vessel catalog exceeds a provider's native-tool limit, keep
# the actions needed for the embodiment loop available. These are action-name
# suffixes, not intent words: the connected world's prefix remains dynamic.
_VESSEL_CORE_TOOL_SUFFIXES: tuple[str, ...] = (
    "say",
    "move",
    "look",
    "use",
    "attack",
    "follow",
    "unfollow",
    "respawn",
    "status",
    "observe",
)
_VESSEL_CONNECTED_PRIORITY_SUFFIXES: tuple[str, ...] = (
    "craft",
    "inventory",
    "place",
    "drop",
    "mine",
    "collect_block",
    "goto",
    "equip",
    "climb_staircase",
)

# This marker is appended to the system instruction only when the endpoint is
# actually sending native function declarations.  The normal prompt still
# contains the legacy JSON-action format for endpoints that do not support
# tools; without an explicit transport instruction, tool-capable small models
# tend to obey that older format and emit a large ``{"actions": [...]}``
# content response instead of making a function call.
_NATIVE_TOOL_MODE_MARKER = "=== SYNTH NATIVE TOOL MODE ==="

# Appended to the current user message on JSON-protocol turns so the format
# requirement is adjacent to the content being answered (the huge system prompt
# buries it, and a capable chat model replies with plain prose instead of JSON,
# forcing a correction round-trip every turn).
_JSON_FORMAT_REMINDER = (
    "\n\nRespond with ONLY valid JSON — your ENTIRE reply must be the JSON "
    "actions object. No prose, no markdown, no text before or after the JSON. "
    'Format: {"actions": [{"type": "action_name", "payload": { ... }}]}'
)


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
        # Last transport/provider error seen inside generate_response (cleared
        # at the start of each call, set on every failed attempt). Callers such
        # as the agent loop read this after a cancelled/failed call to report
        # the real cause ("endpoint offline") instead of a bare timeout/empty.
        self._last_attempt_error: str | None = None
        # Transient, per-call model override applied by scope-aware call sites
        # (see ``scope_model_override``). Unlike ``_endpoint.default_model`` this
        # is NOT persisted and is scoped to a single ``generate_response`` call,
        # because a bridge is a singleton shared across every cortex scope.
        self._scope_model_override: str | None = None

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

        * ``enable_thinking`` (bool) — opt into thinking. The bridge keeps this
          as an internal setting; the OpenAI-compatible adapter translates it
          to Venice's nested provider-facing ``disable_thinking`` key. Thinking
          is off by default.
        * ``disable_thinking`` (bool) — legacy compatibility alias for the
          provider-facing setting.
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
        if self._endpoint.protocol is EndpointProtocol.OPENAI:
            # Thinking is opt-in. Keep the readable alias as the internal
            # setting and translate it at the adapter boundary; old configs
            # using disable_thinking are inverted here for compatibility.
            if "enable_thinking" in extra:
                kwargs["enable_thinking"] = extra.get("enable_thinking") is True
            elif "disable_thinking" in extra:
                kwargs["enable_thinking"] = not bool(extra.get("disable_thinking"))
            else:
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

    def _retry_on_empty(self) -> bool:
        """True when an empty-content LLM response should be re-requested.

        Providers (notably Venice's OpenAI-compat endpoint with thinking
        models) occasionally return HTTP 200 with non-zero completion tokens
        but an empty ``message.content``. The turn then fails with no reply
        and no error. Retrying the *same request* inside the bridge is safe:
        the user message was already persisted upstream by the interface, and
        the empty response is never logged or sent — so a retry neither
        duplicates the input in the DB nor writes a spurious empty reply.
        """
        extra = self._endpoint.extra_config or {}
        return bool(extra.get("retry_on_empty", True))

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

    def _native_tools_enabled(self) -> bool:
        """Return whether this endpoint opts into native tool declarations."""
        if self._disable_tools():
            return False
        extra = self._endpoint.extra_config or {}
        return bool(_NATIVE_TOOLS_ENABLED or extra.get("enable_tools") is True)

    def _parallel_tools_enabled(self) -> bool:
        """Return whether this endpoint opts into PARALLEL native tool-calling.

        Most LLMs cannot emit more than one tool call per turn, so this is
        strictly opt-in via the endpoint's Extra Config JSON (WebUI). Both
        ``enable_tools_parallel`` (canonical) and ``parallel_tool_calls``
        (wire-level alias some configs use) are accepted. A model that natively
        supports parallel calls (e.g. deepseek-4-flash) can emit the outward
        reply plus bookkeeping actions (emotion state, diary) as parallel tool
        calls in a single turn — the exact "message + reasoning + context at
        once" shape the single-native-tool contract (vessel-only) could never
        express. When enabled it also applies native tools to ordinary
        (non-vessel) chat turns; Vessel turns keep the reliable single-call
        contract.
        """
        extra = self._endpoint.extra_config or {}
        return bool(
            extra.get("enable_tools_parallel")
            or extra.get("parallel_tool_calls") is True
        )

    def _max_native_tools(self) -> int | None:
        """Return the native tool cap for this endpoint, if one is known.

        ``max_tools`` (endpoint Extra Config JSON) is the escape hatch: a
        positive value caps the tool count for any provider (Venice included —
        the operator knows their model's real limit); ``0`` disables the cap
        entirely so the full scoped tool set is sent.

        Without an explicit value, the conservative Venice-20 default (a hard
        limit of the old Gemma endpoint) applies ONLY to Venice endpoints that
        have NOT opted into parallel native tool-calling.  A parallel-capable
        model (``enable_tools_parallel``/``parallel_tool_calls``) sends the
        full scoped set so structurally important actions are never crowded out
        by alphabetical ordering — e.g. ``vessel_connect`` sits at "v" and was
        silently dropped from Telegram turns, leaving the model unable to
        honour a "connect to the vessel" request.
        """
        extra = self._endpoint.extra_config or {}
        explicit = extra.get("max_tools")
        if explicit is not None and not isinstance(explicit, bool):
            try:
                parsed = int(explicit)
                if parsed > 0:
                    return parsed
                if parsed == 0:
                    log_warning(
                        f"[cortex_bridge:{self._endpoint.name}] max_tools=0: "
                        "native tool cap disabled (full scoped tool set sent)"
                    )
                    return None
                log_warning(
                    f"[cortex_bridge:{self._endpoint.name}] max_tools={parsed} "
                    "is invalid (must be >0 or 0); ignoring it"
                )
            except (TypeError, ValueError):
                log_warning(
                    f"[cortex_bridge:{self._endpoint.name}] invalid max_tools="
                    f"{explicit!r}; ignoring it"
                )
        if self._parallel_tools_enabled():
            return None
        is_venice = False
        detector = getattr(self._adapter, "_is_venice_endpoint", None)
        if callable(detector):
            try:
                is_venice = bool(detector())
            except Exception:
                is_venice = False
        if not is_venice:
            is_venice = str(self._endpoint.name or "").lower().startswith("venice")
        return _VENICE_MAX_NATIVE_TOOLS if is_venice else None

    @staticmethod
    def _is_vessel_prompt_request(prompt_request: Any) -> bool:
        """Detect an embodied turn from routing metadata only."""
        runtime_ctx = getattr(prompt_request, "runtime_ctx", None)
        interface_name = str(getattr(runtime_ctx, "interface_name", "") or "").strip()
        interface_path = str(getattr(runtime_ctx, "interface_path", "") or "").strip()
        return (
            interface_name == "vessel"
            or interface_path == "vessel"
            or interface_path.startswith("vessel/")
        )

    @staticmethod
    def _vessel_tool_suffix_matches(name: str, suffix: str) -> bool:
        """Match a namespaced Vessel action by its structural verb suffix."""

        return name == f"vessel_{suffix}" or name.endswith(f"_{suffix}")

    @classmethod
    def _connected_vessel_tool_sort_key(
        cls, item: tuple[int, Any]
    ) -> tuple[int, int, int]:
        """Sort connected Vessel tools by embodiment usefulness, stably."""

        index, manifest = item
        name = str(getattr(manifest, "name", "") or "").strip()
        if name == "vessel_disconnect":
            return (0, 0, index)
        for rank, suffix in enumerate(_VESSEL_CORE_TOOL_SUFFIXES):
            if cls._vessel_tool_suffix_matches(name, suffix):
                return (1, rank, index)
        for rank, suffix in enumerate(_VESSEL_CONNECTED_PRIORITY_SUFFIXES):
            if cls._vessel_tool_suffix_matches(name, suffix):
                return (2, rank, index)
        # Keep the remaining world-specific actions in their registry order.
        return (3, 0, index)

    @staticmethod
    def _is_other_interface_delivery_action(name: str, interface_name: str) -> bool:
        """True when ``name`` is a message-delivery action for a different channel.

        The prompt engine's scope may expose several delivery families
        (``message_*``, ``send_file_*``, ``audio_*``, ``send_<iface>_message``)
        alongside the current chat's reply action.  Offering the other
        interfaces' delivery actions in a normal chat turn makes a capable
        parallel-calling model pick e.g. ``send_mate_message`` to "reply",
        silently routing the reply to the wrong channel.  Only the current
        interface's delivery action is kept (``message_<interface>``,
        ``send_file_<interface>``, ``audio_<interface>``,
        ``send_<interface>_message``).  Structural name matching only.
        """
        if not interface_name:
            return False
        lower = name.lower()
        expected_by_prefix = {
            "message_": f"message_{interface_name}",
            "send_file_": f"send_file_{interface_name}",
            "audio_": f"audio_{interface_name}",
            "send_": f"send_{interface_name}_message",
        }
        for prefix, expected in expected_by_prefix.items():
            if lower.startswith(prefix):
                return name != expected
        return False

    def _select_native_tool_manifests(self, prompt_request: Any) -> list[Any]:
        """Scope and cap manifests before rendering provider tool schemas.

        The prompt engine's action whitelist protects textual prompt size, but
        provider tool arrays have a separate limit.  Keep this final boundary
        check here because it also covers stale/live action catalogs and any
        caller that constructs a ``PromptRequest`` directly.

        Vessel turns are intentionally vessel-only when native tools are on:
        ``say`` is the in-world reply path, so unrelated chat, scheduling, and
        developer actions only compete with the embodiment verbs.  For ordinary
        interfaces, message actions for other interfaces are removed while the
        rest of the scoped action set is preserved.
        """
        manifests = list(getattr(prompt_request, "tool_declarations", None) or [])
        if not manifests:
            return []

        runtime_ctx = getattr(prompt_request, "runtime_ctx", None)
        interface_name = str(getattr(runtime_ctx, "interface_name", "") or "").strip()
        is_vessel = self._is_vessel_prompt_request(prompt_request)
        filtered: list[tuple[int, Any]] = []
        for index, manifest in enumerate(manifests):
            name = str(getattr(manifest, "name", "") or "").strip()
            if not name:
                continue
            if is_vessel and not name.startswith("vessel_"):
                continue
            if self._is_other_interface_delivery_action(name, interface_name):
                continue
            filtered.append((index, manifest))

        # Only a live/connected Vessel needs gameplay prioritisation. The
        # connection-driven catalog exposes ``vessel_disconnect`` alongside the
        # connected world's verbs; a disconnected catalog should remain a
        # simple ``vessel_connect`` entry and must not be treated as embodied.
        connected_vessel = is_vessel and any(
            getattr(manifest, "name", "") == "vessel_disconnect"
            for _, manifest in filtered
        )
        if connected_vessel:
            filtered.sort(key=self._connected_vessel_tool_sort_key)

        selected = [manifest for _, manifest in filtered]
        limit = self._max_native_tools()
        if limit is not None and len(selected) > limit:
            original_count = len(manifests)
            log_warning(
                f"[cortex_bridge:{self._endpoint.name}] native tool set reduced "
                f"from {original_count} submitted/{len(selected)} scoped to "
                f"{limit}; provider/model tool limit"
            )
            selected = selected[:limit]
        elif len(selected) != len(manifests):
            log_debug(
                f"[cortex_bridge:{self._endpoint.name}] native tool set scoped "
                f"from {len(manifests)} to {len(selected)} for "
                f"interface={interface_name or 'unknown'}"
            )
        return selected

    @staticmethod
    def _add_native_tool_instruction(
        prompt_request: Any, parallel: bool = False
    ) -> None:
        """Tell the model which response transport is active for this turn.

        ``PromptRequest.system_instruction`` is also used by the OpenAI
        renderer, so this keeps the transport contract adjacent to the tool
        declarations.  It is deliberately structural: it does not try to
        infer intent from the user's words or maintain a keyword list.

        ``parallel`` (from ``enable_tools_parallel``) lets a tool-capable model
        emit the outward reply plus bookkeeping actions in one turn; the
        default single-call wording is kept otherwise because most models
        cannot make several parallel calls.
        """
        instruction = str(getattr(prompt_request, "system_instruction", "") or "")
        if _NATIVE_TOOL_MODE_MARKER in instruction:
            return
        if parallel:
            instruction_text = (
                "The API tools supplied with this turn are the only action "
                "interface. In a human chat turn you MUST include the outward "
                "reply (a message_* action for the current chat, or the vessel "
                "say action when embodied) among your tool calls — never respond "
                "with bookkeeping actions alone. You may call several functions "
                "in one turn: the reply plus update_emotion_state (with at least "
                "one emotion) and create_personal_diary_entry. Do not emit an "
                "actions JSON object, action list, tool name, or tool arguments "
                "as message text. Do not invent a function name."
            )
        else:
            instruction_text = (
                "The API tools supplied with this turn are the only action interface. "
                "Call exactly one supplied function for the single most important "
                "action in this turn. Do not emit an actions JSON object, action list, "
                "tool name, or tool arguments as message text. Do not invent a function "
                "name. The next turn will provide the result and any follow-up action."
            )
        prompt_request.system_instruction = instruction + (
            f"\n\n{_NATIVE_TOOL_MODE_MARKER}\n{instruction_text}"
        )

    def _guard_plain_native_action_response(
        self,
        content: Any,
        selected_manifests: list[Any] | None,
    ) -> str:
        """Contain a model that ignored native tools and emitted action JSON.

        A few tool-capable model/provider combinations return HTTP 200 with a
        plain ``{"actions": [...]}`` completion instead of ``tool_calls``.
        Passing that list through lets one response enqueue dozens of repeated
        world actions.  Keep the first action that was actually offered to the
        model and discard the rest; native tool responses are already normalized
        by the adapter and do not need this fallback.
        """
        text = str(content or "").strip()
        if not text or not selected_manifests:
            return text
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            return text
        if not isinstance(parsed, dict):
            return text
        actions = parsed.get("actions")
        if not isinstance(actions, list) or len(actions) <= 1:
            return text

        offered = {
            str(getattr(manifest, "name", "") or "").strip()
            for manifest in selected_manifests
        }
        valid_actions = [
            action
            for action in actions
            if isinstance(action, dict)
            and str(action.get("type", "") or "").strip() in offered
        ]
        parsed["actions"] = valid_actions[:1]
        log_warning(
            f"[cortex_bridge:{self._endpoint.name}] native tool endpoint returned "
            f"plain action JSON ({len(actions)} actions); keeping "
            f"{len(parsed['actions'])} offered action and discarding the rest"
        )
        return json.dumps(parsed, ensure_ascii=False)

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
            # model only needs the one matching its current interface. The
            # unified send_message is interface-agnostic and always kept.
            _rtx = getattr(prompt_request, "runtime_ctx", None)
            _iface: str = str(getattr(_rtx, "interface_name", "") or "").strip()
            if _iface:
                names = {
                    n
                    for n in names
                    if n == "send_message"
                    or not n.startswith("message_")
                    or n == f"message_{_iface}"
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

            # ``build_prompt_request`` forces every Vessel turn into lite mode
            # because the embodiment catalog is unusually large.  That state
            # is per-turn, while PROMPT_LITE_MODE is a global setting; relying
            # on the latter here silently re-expanded Vessel actions after the
            # prompt engine had already compacted them.  Detect the Vessel
            # route structurally from the typed request so the bridge uses the
            # same compact representation for both paths.
            runtime_ctx = getattr(prompt_request, "runtime_ctx", None)
            runtime_interface = str(
                getattr(runtime_ctx, "interface_path", "") or ""
            ).strip()
            runtime_name = str(getattr(runtime_ctx, "interface_name", "") or "").strip()
            is_vessel_turn = (
                runtime_interface == "vessel"
                or runtime_interface.startswith("vessel/")
                or runtime_name == "vessel"
            )
            is_lite: bool = is_vessel_turn or bool(
                config_registry.get_value("PROMPT_LITE_MODE", False)
            )
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
                    else list(spec.get("payload_keys") or [])
                )
                required = list(spec.get("required_payload_keys") or [])
                line = f"- {name}"
                if brief:
                    line += f": {brief}"
                if props:
                    line += f" (payload keys: {', '.join(str(p) for p in props)})"
                if required:
                    line += f" (required: {', '.join(str(p) for p in required)})"
                lines.append(line)

            block = (
                "\n\n=== AVAILABLE ACTIONS ===\n"
                'Reply ONLY as {"actions":[{"type":"<action_name>","payload":{...}}]}. '
                'Each "type" MUST be exactly one of the action names below — do not '
                "invent, combine, or abbreviate names, and never use a payload key as "
                "a type:\n" + "\n".join(lines)
            )
            _base = getattr(prompt_request, "system_instruction", "") or ""
            prompt_request.system_instruction = _base + block
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

            # Native tools are globally shelved (see _NATIVE_TOOLS_ENABLED) until
            # the agentic feature lands. When disabled — or when an endpoint opts
            # out via disable_tools/force_action_grammar — force the legacy
            # in-prompt JSON-action protocol: keep native tools off, fold the
            # action catalog into the system prompt, and leave
            # supports_tool_calling False so the renderer expects a
            # JSON-in-content reply rather than tool_calls.
            #
            # The single-native-tool contract (tool_choice required + one call)
            # is only suitable for a single-action embodiment loop (the Vessel).
            # Ordinary user-facing chat must return BOTH an outward reply and
            # bookkeeping (emotion/diary) in one turn, which that contract can
            # never express — with "call exactly one function" a small model
            # picks the bookkeeping action, never emits a reply, and every
            # manual turn trips the missing-reply corrector (which re-runs with
            # no chat history, losing all context).  So even when an endpoint
            # opts into native tools, they are applied only to Vessel turns;
            # ordinary chat keeps the in-prompt JSON-action protocol.
            #
            # The PARALLEL native-tool opt-in (``enable_tools_parallel`` in Extra
            # Config) remains supported for Vessel turns' tool-call transport,
            # but it does NOT enable native tools for ordinary chat: in practice
            # the current Venice model returns a single tool call per response,
            # so a chat first attempt degenerates to a lone bookkeeping call
            # (``update_emotion_state {}``) and every turn needs a correction —
            # while the in-prompt JSON protocol with the concrete response
            # example reliably produces the full reply + emotion + diary triple
            # in one shot. Vessel turns keep the single-call contract.
            use_native = self._native_tools_enabled() and (
                _NATIVE_TOOLS_ENABLED or self._is_vessel_prompt_request(prompt_request)
            )
            if not use_native:
                prompt_request.supports_tool_calling = False
                self._inject_actions_into_prompt(prompt_request)
                grammar = self._build_action_grammar(prompt_request)
                if grammar:
                    return {"extra_body": {"grammar": grammar}}
                return {}

            prompt_request.supports_tool_calling = True
            selected_manifests = self._select_native_tool_manifests(prompt_request)
            if not selected_manifests:
                prompt_request.supports_tool_calling = False
                return {}

            self._add_native_tool_instruction(prompt_request, parallel=False)

            # Render a shallow request copy so the caller retains its complete
            # action registry for dispatch/validation.  Only the provider-facing
            # tool declaration list is scoped/capped.
            render_request = copy.copy(prompt_request)
            render_request.tool_declarations = selected_manifests
            render_request.supports_tool_calling = True

            if self._endpoint.protocol is EndpointProtocol.GEMINI:
                rendered = GeminiRenderer(render_request).render()
                tools = rendered.get("tools") or []
                return {"tools": tools} if tools else {}

            if self._endpoint.protocol is EndpointProtocol.OPENAI:
                tools = OpenAIRenderer(render_request).tool_schemas()
                if not tools:
                    return {}
                # SyntH's LLM-to-interface contract requires an action.  This
                # branch only runs for Vessel turns (ordinary chat uses the
                # in-prompt JSON protocol), which keep the single-call contract
                # — parallel_tool_calls stays off so a model cannot turn one
                # embodiment turn into an unbounded action batch.  Both fields
                # are standard Chat Completions options; the adapter still owns
                # provider-specific translation (and degrades the controls
                # gracefully when a proxy rejects them).
                return {
                    "tools": tools,
                    "tool_choice": "required",
                    "parallel_tool_calls": False,
                }

            if self._endpoint.protocol is EndpointProtocol.ANTHROPIC:
                rendered = AnthropicRenderer(render_request).render()
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

    @contextlib.contextmanager
    def scope_model_override(self, model: str | None):
        """Temporarily override the model used by the next ``generate_response``.

        Scope-aware call sites resolve a per-scope model (via
        :func:`core.config.get_active_cortex_scope`) and wrap the generation call
        with this context manager. The override is applied only for that single
        call and restored afterwards — the shared endpoint ``default_model`` is
        never mutated, so concurrent calls on other scopes are unaffected.

        A ``None``/empty model, or one not present in the endpoint's
        ``available_models``, is ignored (falls back to the endpoint default).
        """
        model = str(model or "").strip()
        available = getattr(self._endpoint, "available_models", None) or []
        if model and available and model not in available:
            log_debug(
                f"[cortex_bridge:{self._endpoint.name}] scope model "
                f"'{model}' not in available_models -- ignoring override"
            )
            model = ""
        previous = self._scope_model_override
        self._scope_model_override = model or None
        try:
            yield
        finally:
            self._scope_model_override = previous

    def _bind_langfuse_turn_context(self, messages: Any) -> None:
        """Attach per-turn attribution context for Langfuse tracing.

        Pulls structural turn metadata (interface, user, mode, scope) from a
        typed ``PromptRequest`` so every cortex trace for this turn — including
        retries and nested TTS calls — groups under the same Langfuse session
        and user. The call site is structural: it never reads message text.
        """
        try:
            from core.cortex_api_logger import set_langfuse_turn_context
            from core.prompt_request import PromptRequest

            prompt_request: PromptRequest | None = None
            if isinstance(messages, PromptRequest):
                prompt_request = messages
            elif isinstance(messages, dict):
                candidate = messages.get("__prompt_request")
                if isinstance(candidate, PromptRequest):
                    prompt_request = candidate

            if prompt_request is None:
                set_langfuse_turn_context(None)
                return

            runtime_ctx = prompt_request.runtime_ctx
            context: dict[str, Any] = {
                "interface_path": runtime_ctx.interface_path,
                "interface_name": runtime_ctx.interface_name,
                "chat_type": runtime_ctx.chat_type,
                "username": runtime_ctx.username,
                "usertag": runtime_ctx.usertag,
                "scope": runtime_ctx.scope,
                "mode": prompt_request.mode,
                "input_source": runtime_ctx.input_source,
            }
            set_langfuse_turn_context(
                {k: v for k, v in context.items() if v not in (None, "")}
            )
        except Exception:
            try:
                from core.cortex_api_logger import set_langfuse_turn_context

                set_langfuse_turn_context(None)
            except Exception:
                pass

    async def generate_response(
        self,
        messages: list[dict[str, Any]] | Any,
        **extra_request_kwargs: Any,
    ) -> str:
        """Forward ``messages`` to the external endpoint and return the response text.

        Accepts either a list of OpenAI-style message dicts (e.g. from recon or
        the agent loop) or a SyntH JSON-prompt dict/str — same flexible
        contract as the built-in engines. ``extra_request_kwargs`` are merged
        into the provider request (e.g. the Agent Lane passes
        ``enable_thinking`` / ``tools`` / ``tool_choice`` /
        ``parallel_tool_calls``); ordinary callers pass none.
        """
        prompt_extra_kwargs: dict[str, Any] = dict(extra_request_kwargs)
        if isinstance(messages, list):
            msg_list = messages
            # Ensure sufficient output tokens for structured responses (e.g. Recon JSON).
            # Most adapters default to 1024 which can truncate multi-field JSON output.
            prompt_extra_kwargs.setdefault("max_tokens", 4096)
        else:
            prompt_extra_kwargs = self._tool_api_kwargs(messages)
            msg_list = self._build_messages(messages)
            msg_list = self._clamp_messages_to_char_budget(msg_list)

        self._bind_langfuse_turn_context(messages)

        model = self._scope_model_override or self._endpoint.default_model
        if not model and self._endpoint.available_models:
            model = self._endpoint.available_models[0]
        self._last_response_metadata = {}
        self._last_attempt_error = None
        max_retries, backoff = self._get_retry_settings()
        request_timeout = self._get_request_timeout()
        retry_on_timeout = self._retry_on_timeout()
        retry_on_empty = self._retry_on_empty()
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
                response_content = chat_resp.content
                adapter_response_metadata = getattr(
                    self._adapter,
                    "_last_completion_metadata",
                    None,
                )
                native_tool_calls = (
                    adapter_response_metadata.get("native_tool_calls")
                    if isinstance(adapter_response_metadata, dict)
                    else None
                )
                if "tools" in extra_kwargs and native_tool_calls is not True:
                    # The provider returned content instead of a structured
                    # tool call.  Re-derive the exact provider-facing set so
                    # unsupported plain-content actions cannot leak into the
                    # normal action parser either.
                    from core.prompt_request import PromptRequest

                    prompt_request = (
                        messages if isinstance(messages, PromptRequest) else None
                    )
                    if prompt_request is None and isinstance(messages, dict):
                        candidate = messages.get("__prompt_request")
                        if isinstance(candidate, PromptRequest):
                            prompt_request = candidate
                    selected_manifests = (
                        self._select_native_tool_manifests(prompt_request)
                        if prompt_request is not None
                        else None
                    )
                    response_content = self._guard_plain_native_action_response(
                        response_content,
                        selected_manifests,
                    )
                # Empty-content fallback: some providers return 200 with
                # non-zero completion tokens but an empty message.content.
                # Retry the identical request in-bridge — the user message was
                # already persisted upstream by the interface, and this empty
                # result is discarded (never logged to the DB nor sent), so a
                # retry neither duplicates the input nor writes an empty reply.
                if not response_content and retry_on_empty and attempt < max_retries:
                    delay = backoff * (2 ** (attempt - 1))
                    log_warning(
                        f"[cortex_bridge:{self._endpoint.name}] empty response from "
                        f"{model} (attempt {attempt}/{max_retries}); re-requesting "
                        f"in {delay:.1f}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                response_metadata: dict[str, Any] = {
                    "model": getattr(chat_resp, "model", None) or model,
                    "finish_reason": getattr(chat_resp, "finish_reason", None)
                    or "stop",
                    "empty_response": not bool(response_content),
                }
                if isinstance(adapter_response_metadata, dict):
                    for key, value in adapter_response_metadata.items():
                        if value is None or value == "":
                            continue
                        response_metadata[key] = value
                self._last_response_metadata = response_metadata
                return response_content
            except asyncio.TimeoutError:
                self._last_attempt_error = (
                    f"TimeoutError: LLM request timed out after {request_timeout}s"
                )
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
                self._last_attempt_error = f"{type(exc).__name__}: {exc}"
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

    def _downstream_char_budget(self) -> int:
        """Resolve the hard downstream char budget for assembled messages.

        Endpoints may override the default via
        ``extra_config["downstream_char_budget"]``; a non-positive value
        disables the clamp entirely.
        """
        extra = self._endpoint.extra_config or {}
        try:
            return int(
                extra.get("downstream_char_budget", _DEFAULT_DOWNSTREAM_CHAR_BUDGET)
            )
        except (TypeError, ValueError):
            return _DEFAULT_DOWNSTREAM_CHAR_BUDGET

    @staticmethod
    def _message_content_len(content: Any) -> int:
        """Character length of a message's content (string or multipart list)."""
        if isinstance(content, str):
            return len(content)
        if isinstance(content, list):
            total = 0
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    total += len(str(part.get("text", "")))
            return total
        return len(str(content))

    @staticmethod
    def _truncate_message_content(content: Any, remove_chars: int) -> Any:
        """Trim ``remove_chars`` from the tail of a message's text content.

        Only text is trimmed; multimodal (image) parts are left intact so the
        clamp never corrupts an attachment. A short marker is appended so the
        model knows the body was shortened.
        """
        if remove_chars <= 0:
            return content
        marker = "\n\n[...context trimmed to fit the model's input budget...]"
        if isinstance(content, str):
            keep = max(0, len(content) - remove_chars - len(marker))
            if keep <= 0:
                return content[: max(0, len(content) - remove_chars)]
            return content[:keep] + marker
        if isinstance(content, list):
            remaining = remove_chars
            out: list[Any] = []
            for part in content:
                if (
                    remaining > 0
                    and isinstance(part, dict)
                    and part.get("type") == "text"
                ):
                    text = str(part.get("text", ""))
                    if len(text) <= remaining:
                        remaining -= len(text)
                        continue  # drop this text part entirely
                    new_text = text[: len(text) - remaining] + marker
                    remaining = 0
                    out.append({**part, "text": new_text})
                else:
                    out.append(part)
            return out
        return content

    def _clamp_messages_to_char_budget(
        self, messages: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Hard-cap the assembled messages below the downstream char budget.

        The up-front ``max_chars`` reducer in ``plugin_instance`` runs BEFORE the
        action catalog is folded into the system message and before the messages
        are serialised into role-separated JSON, so the real payload can be much
        larger than the reduced context (dominated, for vessel turns, by the
        injected ``vessel_minecraft_*`` catalog). This clamp operates on the
        fully assembled messages and trims older non-system content first. The
        latest user turn is protected so the current command cannot disappear;
        system instructions and image parts are also preserved. When the
        protected content alone (system + current turn) already exceeds the
        budget — i.e. the budget is unreachable even by dropping every
        trimmable message — the method logs the overflow and sends the
        assembled messages as-is rather than destroying the conversation
        history for no gain (serialization headroom keeps such payloads below
        the downstream engine's hard limit anyway).
        """
        budget = self._downstream_char_budget()
        if budget <= 0 or not isinstance(messages, list) or not messages:
            return messages

        total = sum(self._message_content_len(m.get("content")) for m in messages)
        if total <= budget:
            return messages

        overflow = total - budget
        # The final user message is the current turn for the typed OpenAI
        # renderers. Keep it intact and trim preceding history/result messages
        # first. If there is no user message, protect the last non-system
        # message as the safest equivalent fallback.
        protected_index: int | None = None
        for index in range(len(messages) - 1, -1, -1):
            if messages[index].get("role") == "user":
                protected_index = index
                break
        if protected_index is None:
            for index in range(len(messages) - 1, -1, -1):
                if messages[index].get("role") != "system":
                    protected_index = index
                    break

        trimmable = sum(
            self._message_content_len(message.get("content"))
            for index, message in enumerate(messages)
            if message.get("role") != "system" and index != protected_index
        )
        # Budget unreachable: even dropping EVERY trimmable message cannot get
        # under the budget (the protected system + current-turn baseline alone
        # already exceeds it). Trimming anyway would only destroy the
        # conversation history with zero budget benefit — observed live as
        # system+current-only prompts with all history dropped (Langfuse
        # 04247e00 / 3e3bd8eb, where a ~24.4k baseline vs a 24000 budget left
        # "Remaining overflow: 164" after wiping every history turn) and as
        # mid-sentence history stubs (39112b42). Keep the assembled messages
        # and warn instead. Serialization headroom keeps such payloads safely
        # below the downstream engine limit even when the conservative budget
        # is missed (measured overhead ~5.2k chars; a 24.7k payload serializes
        # to ~29.9k, well under the 32000 chunking threshold).
        if overflow >= trimmable:
            log_warning(
                f"[cortex_bridge:{self._endpoint.name}] downstream payload "
                f"{total} chars exceeds budget {budget} and the budget is "
                f"unreachable (protected system + current turn alone: "
                f"{total - trimmable} chars); sending the assembled messages "
                f"as-is to preserve conversation context."
            )
            return messages

        remaining_to_remove = overflow
        # Fully-consumed messages are DROPPED, never left behind as empty
        # ``"content": ""`` turns. The upstream empty-turn filters (prompt
        # assembly) run BEFORE this clamp, so a blank user/assistant message
        # here would be a brand-new empty-content message in the provider
        # payload (observed as blank blocks in Langfuse — e.g. d3e58a80,
        # where the 8 short history turns were each trimmed to ""). Dropping
        # keeps the provider history well-formed.
        kept_messages: list[dict[str, Any]] = []
        for index, msg in enumerate(messages):
            if remaining_to_remove <= 0:
                kept_messages.extend(messages[index:])
                break
            if msg.get("role") == "system" or index == protected_index:
                kept_messages.append(msg)
                continue
            content = msg.get("content")
            content_len = self._message_content_len(content)
            if content_len <= 0:
                kept_messages.append(msg)
                continue
            if remaining_to_remove >= content_len:
                # The entire message would be consumed by the trim: drop it
                # instead of emitting an empty-content turn.
                remaining_to_remove -= content_len
                continue
            take = min(content_len, remaining_to_remove)
            msg["content"] = self._truncate_message_content(content, take)
            remaining_to_remove -= take
            kept_messages.append(msg)
        messages = kept_messages

        if remaining_to_remove > 0:
            log_warning(
                f"[cortex_bridge:{self._endpoint.name}] downstream payload {total} "
                f"chars exceeded budget {budget}; trimmed "
                f"~{overflow - remaining_to_remove} chars from older context but "
                f"kept the latest user turn intact. Remaining overflow: "
                f"{remaining_to_remove} chars."
            )
        else:
            log_warning(
                f"[cortex_bridge:{self._endpoint.name}] downstream payload {total} "
                f"chars exceeded budget {budget}; trimmed ~{overflow} chars from "
                f"older context while preserving the latest user turn."
            )
        return messages

    def _build_messages(self, prompt: Any) -> list[dict[str, Any]]:
        """Convert a SyntH prompt into role-separated provider messages.

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
                    messages = renderer.render_with_multimodal(
                        mm_parts,
                        supports_vision=supports_vision,
                    )
                else:
                    messages = renderer.render()
                self._append_json_format_reminder(messages, prompt)
                return messages
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
                        messages = renderer.render_with_multimodal(
                            mm_parts,
                            supports_vision=supports_vision,
                        )
                    else:
                        messages = renderer.render()
                    self._append_json_format_reminder(messages, prompt_request)
                    return messages
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

    @staticmethod
    def _append_json_format_reminder(
        messages: list[dict[str, Any]], prompt: Any
    ) -> None:
        """Append a salient JSON-format reminder to the current user message.

        The JSON-action protocol is enforced only via the (huge) system prompt,
        so a capable chat model routinely replies with plain in-character prose
        instead of the required JSON on the FIRST attempt — then the corrector
        repairs it, costing an extra round-trip every turn. The corrector works
        because its format requirements sit in the last user message; mirror
        that salience for ordinary JSON-protocol turns. Native-tool turns
        declare the contract via tools (``supports_tool_calling`` True) and are
        skipped.
        """
        try:
            if getattr(prompt, "supports_tool_calling", True):
                return
            if not messages:
                return
            last = messages[-1]
            if not isinstance(last, dict) or last.get("role") != "user":
                return
            content = last.get("content")
            if isinstance(content, str):
                last["content"] = content + _JSON_FORMAT_REMINDER
        except Exception as exc:
            log_debug(f"[cortex_bridge] format reminder skip: {exc}")

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
            # Include WHO sent the message — the corrector is stripped of the
            # routing prefix and history, so a multi-person persona otherwise
            # makes the model guess the sender (Papa's message answered as
            # "mama"). Structural field from the original message, never text.
            sender = str(sm.get("sender") or "").strip()
            if sender:
                user_parts.append(
                    f"Original user message (from {sender}):\n{original_user_message}"
                )
            else:
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
    # benefit from extra_config settings such as ``enable_thinking``.

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
