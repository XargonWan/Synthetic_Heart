"""FastAPI interface exposing the Synthetic Heart with an Ollama-compatible API."""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from collections import deque
from datetime import datetime, timezone
import time
from typing import Any, AsyncIterator, Deque, Dict, Iterable, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.core_initializer import register_interface
from core.logging_utils import log_debug, log_error, log_info, log_warning
import core.plugin_instance as plugin_instance


def _now_iso() -> str:
    """Return an ISO formatted UTC timestamp."""

    return datetime.now(tz=timezone.utc).isoformat()


class OllamaCompatServer:
    """Expose the synth message chain through a REST API compatible with Ollama."""

    display_name = "Ollama Compat Server"

    interface_id = "ollama_serve"

    def __init__(self) -> None:
        self.app = FastAPI(title="synth Freedom Serve", version="1.0")

        self._server_task: Optional[asyncio.Task[None]] = None
        self._startup_pending = False
        self.default_model_name = os.getenv("OLLAMA_DEFAULT_MODEL", "SyntH")
        self.default_model_display = os.getenv(
            "OLLAMA_DEFAULT_MODEL_DISPLAY", "Syntethic Heart"
        )

        # Prefer the configured Base Cortex as the default model exposed to
        # Ollama/OpenAI clients so `/api/tags` and `/v1/models` reflect
        # available Cortex engines and the configured active engine.
        try:
            from core.config_manager import config_registry

            base_cortex = config_registry.get_value("BASE_CORTEX", "")
            if base_cortex:
                self.default_model_name = base_cortex
        except Exception:
            # Keep env/default fallback if config_registry isn't available
            pass

        # Context memory mirrors the structure used by other interfaces so that
        # build_json_prompt() can reuse the chat history.
        self.context_memory: Dict[str, Deque[dict[str, Any]]] = {}
        self.max_history = int(os.getenv("OLLAMA_MAX_HISTORY", "20"))

        # Track pending responses for each chat so send_message() can deliver
        # data to the HTTP handlers.
        self._pending_streams: Dict[str, asyncio.Queue[Optional[dict[str, Any]]]] = {}
        self._response_buffers: Dict[str, list[str]] = {}
        self._completion_events: Dict[str, asyncio.Event] = {}
        self._request_start: Dict[str, float] = {}
        # Stable request identifiers for streaming compatibility (OpenAI-style)
        self._request_ids: Dict[str, str] = {}

        # Map external conversation identifiers to internal chat ids used by
        # the core. When no identifier is provided we still create a temporary
        # chat id so the request can be processed consistently.
        self._conversation_map: Dict[str, str] = {}

        # Message counters per chat to generate deterministic message ids.
        self._message_counters: Dict[str, int] = {}

        self.app.get("/")(self._index)
        self.app.get("/api/tags")(self._list_models)
        self.app.post("/api/chat")(self._chat_endpoint)
        self.app.post("/api/generate")(self._generate_endpoint)

        # Compatibility aliases (many clients — e.g. AIRI — use a /v1/ base path)
        self.app.get("/v1/")(self._index)
        self.app.get("/v1/api/tags")(self._list_models)
        self.app.post("/v1/api/chat")(self._chat_endpoint)
        self.app.post("/v1/api/generate")(self._generate_endpoint)

        # Common OpenAI-style aliases expected by some clients
        self.app.post("/v1/chat/completions")(self._chat_endpoint)
        self.app.post("/v1/completions")(self._generate_endpoint)
        # OpenAI-compatible model list (returns `data: [...]`) — many clients use `resp.data.map(...)`
        self.app.get("/v1/models")(self._list_models_v1)
        # Single-model lookup (returns model descriptor)
        self.app.get("/v1/models/{model_name}")(self._get_model)

        register_interface(self.interface_id, self)
        log_info("[ollama_serve] Interface registered")

        self.stream_timeout = float(os.getenv("OLLAMA_STREAM_TIMEOUT", "60.0"))
        self.completion_timeout = float(os.getenv("OLLAMA_COMPLETION_TIMEOUT", "0.0"))

        # Check if server should be enabled (default: true for backward compatibility)
        self.server_enabled = os.getenv("OLLAMA_SERVER_ENABLED", "true").lower() in (
            "true",
            "1",
            "yes",
            "on",
        )

        if self.server_enabled:
            self._schedule_server_startup()
        else:
            log_info(
                "[ollama_serve] Server disabled via OLLAMA_SERVER_ENABLED=false. "
                "Interface actions still available but HTTP server will not start."
            )

    # ------------------------------------------------------------------
    # Interface metadata
    # ------------------------------------------------------------------
    @staticmethod
    def get_interface_id() -> str:
        return OllamaCompatServer.interface_id

    @staticmethod
    def get_supported_actions() -> dict[str, dict[str, Any]]:
        return {
            "message_ollama_serve": {
                "description": "Send a text message through the Ollama-compatible HTTP interface.",
                "required_fields": ["text", "target"],
                "optional_fields": ["conversation_id"],
            }
        }

    @staticmethod
    def get_prompt_instructions(action_name: str) -> dict[str, Any]:
        if action_name == "message_ollama_serve":
            return {
                "description": "Send a message back to an Ollama-compatible HTTP client.",
                "payload": {
                    "text": {
                        "type": "string",
                        "description": "Message content to deliver to the client.",
                    },
                    "target": {
                        "type": "string",
                        "description": "Internal chat identifier associated with the HTTP session.",
                    },
                    "conversation_id": {
                        "type": "string",
                        "description": "Optional external conversation identifier provided by the client.",
                        "optional": True,
                    },
                },
            }
        return {}

    @staticmethod
    def get_interface_instructions() -> str:
        return (
            "Use interface: ollama_serve to converse with clients using the Ollama HTTP protocol. "
            "The target field must contain the internal chat identifier provided by the request handler."
        )

    # ------------------------------------------------------------------
    # FastAPI route handlers
    # ------------------------------------------------------------------
    async def _index(self) -> JSONResponse:
        return JSONResponse({"status": "ok", "interface": self.interface_id})

    async def _list_models(self) -> JSONResponse:
        models = self._build_model_catalog()
        # Try to surface the _runtime_ active cortex as the first/default model
        try:
            from core.config import get_active_cortex_engine

            active = await get_active_cortex_engine()
            if active and not any(m.get("name") == active for m in models):
                models.insert(
                    0,
                    self._format_model_descriptor(
                        active, display=self.default_model_display
                    ),
                )
        except Exception:
            # Non-fatal if we cannot determine runtime active engine
            pass
        payload = {"models": models}
        return JSONResponse(payload)

    async def _get_model(self, model_name: str) -> JSONResponse:
        """Return a single model descriptor or 404 if not found.

        Accept both `id` and `name` as lookup keys (compatibility).
        """
        for desc in self._build_model_catalog():
            if str(desc.get("name")) == str(model_name) or str(desc.get("id")) == str(
                model_name
            ):
                return JSONResponse(desc)
        raise HTTPException(status_code=404, detail="Model not found")

    async def _list_models_v1(self, request: Request) -> JSONResponse:
        """OpenAI-style `/v1/models` response (returns `data: [...]`)."""
        data = self._build_model_catalog()
        try:
            from core.config import get_active_cortex_engine

            active = await get_active_cortex_engine()
            if active and not any(m.get("name") == active for m in data):
                data.insert(
                    0,
                    self._format_model_descriptor(
                        active, display=self.default_model_display
                    ),
                )
        except Exception:
            pass
        payload = {"object": "list", "data": data}
        return JSONResponse(payload)

    async def _chat_endpoint(self, request: Request):
        data = await request.json()
        return await self._handle_chat_payload(data)

    async def _generate_endpoint(self, request: Request):
        data = await request.json()
        prompt = data.get("prompt")
        if not isinstance(prompt, str) or not prompt.strip():
            raise HTTPException(
                status_code=400, detail="'prompt' must be a non-empty string"
            )

        payload = dict(data)
        payload["messages"] = [{"role": "user", "content": prompt}]
        payload.setdefault("stream", data.get("stream", True))
        return await self._handle_chat_payload(payload)

    async def _handle_chat_payload(self, data: dict[str, Any]):
        messages = data.get("messages") or []
        if not messages:
            raise HTTPException(
                status_code=400, detail="'messages' must be a non-empty list"
            )

        last_message = messages[-1]
        if not isinstance(last_message, dict) or last_message.get("role") != "user":
            raise HTTPException(
                status_code=400, detail="Last message must be a user message"
            )

        history_messages = messages[:-1]

        stream = data.get("stream", True)
        model = data.get("model") or self.default_model_name
        conversation_id = self._extract_conversation_id(data, messages)
        chat_id = self._resolve_chat_id(conversation_id)

        queue: asyncio.Queue[Optional[dict[str, Any]]] = asyncio.Queue()
        self._pending_streams[chat_id] = queue
        self._response_buffers[chat_id] = []
        completion_event = asyncio.Event()
        self._completion_events[chat_id] = completion_event
        self._request_start[chat_id] = time.monotonic()
        # Create a stable per-request completion id so streamed chunks can
        # include a consistent `id`/`object` pair (OpenAI-compatible).
        self._request_ids[chat_id] = f"chatcmpl-{uuid.uuid4().hex[:12]}"

        # Emit an initial "processing" chunk for streaming clients that expect
        # an early typing indicator (AIRI and other Ollama-like frontends).
        # Include the stable `id` and an empty OpenAI-style `choices[].delta` so
        # clients that parse streaming deltas immediately show typing.
        if stream:
            try:
                await self._publish_chunk(
                    chat_id,
                    {
                        "id": self._request_ids.get(chat_id),
                        "type": "processing",
                        "processing": True,
                        "message": {"role": "assistant", "content": ""},
                        "done": False,
                        "conversation_id": conversation_id,
                        "choices": [{"delta": {}, "index": 0}],
                    },
                )
            except Exception:
                # Don't fail the request if the helper chunk cannot be queued
                log_warning("[ollama_serve] Failed to emit initial processing chunk")

        log_info(
            f"[ollama_serve] Received chat request chat_id={chat_id} conv_id={conversation_id} "
            f"model={model} stream={stream}"
        )

        task = asyncio.create_task(
            self._process_chat_request(
                chat_id=chat_id,
                conversation_id=conversation_id,
                model=model,
                history_messages=history_messages,
                last_message=last_message,
                completion_event=completion_event,
            )
        )

        if stream:
            return StreamingResponse(
                self._stream_response(queue, task),
                media_type="application/x-ndjson",
            )

        await task
        result_chunks = await self._collect_queue(queue)
        if not result_chunks:
            raise HTTPException(status_code=500, detail="No response from LLM")

        final_chunk = result_chunks[-1]
        if final_chunk.get("error"):
            return JSONResponse(final_chunk, status_code=500)

        aggregated_response = "".join(
            chunk.get("response", "")
            for chunk in result_chunks
            if not chunk.get("done")
        )
        if not aggregated_response:
            aggregated_response = final_chunk.get("final_response", "")

        payload = {
            "model": final_chunk.get("model", model or self.default_model_name),
            "created_at": final_chunk.get("created_at", _now_iso()),
            "message": {"role": "assistant", "content": aggregated_response},
            "response": aggregated_response,
            "final_response": aggregated_response,
            "done": True,
            "done_reason": final_chunk.get("done_reason", "stop"),
            "context": final_chunk.get("context", []),
        }
        # OpenAI/Ollama-compatible metadata to satisfy clients that expect
        # `resp.choices.map(...)` or `/v1/chat/completions` style payloads.
        payload_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
        payload_created = int(datetime.now(tz=timezone.utc).timestamp())
        # Prefer the per-request stable id (streaming) when available
        payload.setdefault("id", self._request_ids.get(chat_id, payload_id))
        payload.setdefault("object", "chat.completion")
        payload.setdefault("created", payload_created)
        payload.setdefault(
            "choices",
            [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": aggregated_response},
                    "finish_reason": payload.get("done_reason", "stop"),
                }
            ],
        )
        # OpenAI-style compatibility: include `text` on completion choices for
        # clients that call `/v1/completions`.
        try:
            payload["choices"][0].setdefault("text", aggregated_response)
        except Exception:
            pass

        payload.setdefault(
            "usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        )

        if "total_duration" in final_chunk:
            payload["total_duration"] = final_chunk["total_duration"]
        payload["load_duration"] = final_chunk.get("load_duration", 0)
        payload["prompt_eval_duration"] = final_chunk.get("prompt_eval_duration", 0)
        payload["eval_duration"] = final_chunk.get(
            "eval_duration", final_chunk.get("total_duration", 0)
        )
        payload["prompt_eval_count"] = final_chunk.get("prompt_eval_count", 0)
        payload["eval_count"] = final_chunk.get("eval_count", 0)
        return JSONResponse(payload)

    # ------------------------------------------------------------------
    # Core processing helpers
    # ------------------------------------------------------------------
    async def _process_chat_request(
        self,
        *,
        chat_id: str,
        conversation_id: Optional[str],
        model: Optional[str],
        history_messages: Iterable[dict[str, Any]],
        last_message: dict[str, Any],
        completion_event: asyncio.Event,
    ) -> None:
        try:
            # Build interface_path for Ollama
            from core.interface_path_utils import build_interface_path

            interface_path = build_interface_path("ollama_serve", chat_id)
            log_debug(f"[ollama_serve] Generated interface_path: {interface_path}")

            # Mark session as processing so clients that poll session_meta (or
            # expect server-side session flags) see an immediate "typing"
            # indicator for Ollama-originated requests.
            try:
                from core.session_meta import set_session_meta

                await set_session_meta(interface_path, {"processing": True})
            except Exception:
                log_debug(
                    "[ollama_serve] Unable to set session_meta.processing for interface_path"
                )

            # Track context using centralized manager
            from core.chat_context_manager import add_message_to_context

            try:
                await add_message_to_context(
                    interface_path=interface_path,
                    message_text=last_message.get("content", ""),
                    sender_name="user",
                    sender_id=f"ollama-user-{chat_id}",
                    timestamp=None,
                )
            except Exception as e:
                log_warning(f"[ollama_serve] Failed to add message to context: {e}")

            self._populate_history(chat_id, history_messages)
            message_obj = self._build_message(chat_id, last_message)

            # Add interface_path to message object
            message_obj.interface_path = interface_path

            # Update context memory with the latest user message so the prompt
            # reflects the current conversation state.
            history = self.context_memory.setdefault(
                chat_id, deque(maxlen=self.max_history)
            )
            history.append(
                self._history_entry_from_message(
                    "user", last_message["content"], chat_id
                )
            )

            try:
                from core import message_queue

                response = await message_queue.enqueue_and_wait(
                    bot=self,
                    message=message_obj,
                    context_memory=self.context_memory,
                    history_scope="local",
                    priority=False,
                    interface_id=self.interface_id,
                    skip_mention_check=True,
                    original_message=message_obj,
                    timeout=self.completion_timeout if self.completion_timeout > 0 else None,
                )
            except Exception as exc:
                log_error(f"[ollama_serve] Error while processing message: {exc}")
                await self._fail_stream(chat_id, conversation_id, model, str(exc))
                return

            if isinstance(response, str):
                # Stream the (possibly partial) text as OpenAI-style deltas.
                await self._stream_text(
                    chat_id=chat_id,
                    model=model,
                    conversation_id=conversation_id,
                    text=response,
                )
                await self._finalize_stream(
                    chat_id=chat_id,
                    model=model,
                    conversation_id=conversation_id,
                )

            if completion_event:
                timeout = self.stream_timeout
                max_wait = max(self.completion_timeout, 0.0)
                waited = 0.0
                while True:
                    if timeout <= 0:
                        await completion_event.wait()
                        break
                    try:
                        await asyncio.wait_for(completion_event.wait(), timeout=timeout)
                        break
                    except asyncio.TimeoutError:
                        waited += timeout
                        if max_wait and waited >= max_wait:
                            if not completion_event.is_set():
                                await self._fail_stream(
                                    chat_id,
                                    conversation_id,
                                    model,
                                    "No deliverable actions produced by LLM",
                                )
                            await completion_event.wait()
                            break
                        log_debug(
                            f"[ollama_serve] Awaiting completion for chat_id={chat_id} "
                            f"(waited ~{waited:.1f}s, timeout={timeout}s)"
                        )
        finally:
            # Clear any session-level processing flag we set earlier. Use a
            # guarded call in case interface_path was not successfully set.
            try:
                if "interface_path" in locals():
                    from core.session_meta import set_session_meta

                    await set_session_meta(interface_path, {"processing": False})
            except Exception:
                log_debug(
                    "[ollama_serve] Failed to clear session_meta.processing for interface_path"
                )

            # Clean up stored request id
            try:
                self._request_ids.pop(chat_id, None)
            except Exception:
                pass

            self._pending_streams.pop(chat_id, None)
            self._response_buffers.pop(chat_id, None)
            self._completion_events.pop(chat_id, None)
            self._request_start.pop(chat_id, None)

    async def _stream_response(
        self,
        queue: asyncio.Queue[Optional[dict[str, Any]]],
        task: asyncio.Task[None],
    ) -> AsyncIterator[bytes]:
        try:
            while True:
                chunk = await queue.get()
                if chunk is None:
                    break
                yield (json.dumps(chunk) + "\n").encode("utf-8")
        finally:
            await task

    async def _collect_queue(
        self, queue: asyncio.Queue[Optional[dict[str, Any]]]
    ) -> list[dict[str, Any]]:
        chunks: list[dict[str, Any]] = []
        while True:
            chunk = await queue.get()
            if chunk is None:
                break
            chunks.append(chunk)
        return chunks

    def _extract_conversation_id(
        self, data: dict[str, Any], messages: Iterable[dict[str, Any]]
    ) -> Optional[str]:
        fields = [
            "conversation",
            "session",
            "id",
            "thread_id",
        ]
        for field in fields:
            value = data.get(field)
            if value:
                return str(value)

        for message in messages:
            value = message.get("id")
            if value:
                return str(value)

        return None

    def _resolve_chat_id(self, conversation_id: Optional[str]) -> str:
        if not conversation_id:
            return f"ollama:{uuid.uuid4()}"
        conversation_id = str(conversation_id)
        if conversation_id not in self._conversation_map:
            self._conversation_map[conversation_id] = f"ollama:{conversation_id}"
        return self._conversation_map[conversation_id]

    def _populate_history(
        self, chat_id: str, messages: Iterable[dict[str, Any]]
    ) -> None:
        history = deque(maxlen=self.max_history)
        for message in messages:
            role = message.get("role")
            content = message.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if role not in {"user", "assistant", "system"}:
                continue
            history.append(self._history_entry_from_message(role, content, chat_id))
        self.context_memory[chat_id] = history

    def _build_message(self, chat_id: str, message: dict[str, Any]):
        from types import SimpleNamespace

        self._message_counters.setdefault(chat_id, 0)
        self._message_counters[chat_id] += 1
        message_id = self._message_counters[chat_id]

        text = message.get("content", "")

        return SimpleNamespace(
            chat_id=chat_id,
            message_id=message_id,
            text=text,
            date=datetime.utcnow(),
            from_user=SimpleNamespace(
                id=f"ollama-user-{chat_id}",
                username="ollama_user",
                first_name="Ollama",
                full_name="Ollama Client",
            ),
            chat=SimpleNamespace(id=chat_id, type="ollama"),
            reply_to_message=None,
        )

    def _history_entry_from_message(
        self, role: str, content: str, chat_id: str
    ) -> dict[str, Any]:
        return {
            "role": role,
            "text": content,
            "timestamp": _now_iso(),
            "chat_id": chat_id,
        }

    async def _publish_chunk(self, chat_id: str, chunk: dict[str, Any]) -> None:
        queue = self._pending_streams.get(chat_id)
        if not queue:
            return
        chunk.setdefault("model", self.default_model_name)
        chunk.setdefault("created_at", _now_iso())
        message = chunk.get("message")
        if not isinstance(message, dict):
            message = {"role": "assistant", "content": ""}
            chunk["message"] = message
        else:
            message.setdefault("role", "assistant")
            message.setdefault("content", "")

        if message.get("content"):
            chunk.setdefault("response", message["content"])

        # OpenAI-compatible metadata: stable id/object/created on chunks
        chunk.setdefault(
            "id", self._request_ids.get(chat_id, f"chatcmpl-{uuid.uuid4().hex[:12]}")
        )
        chunk.setdefault(
            "object",
            "chat.completion.chunk" if not chunk.get("done") else "chat.completion",
        )
        chunk.setdefault("created", int(datetime.now(tz=timezone.utc).timestamp()))

        if chunk.get("done"):
            chunk.setdefault(
                "done_reason", "stop" if not chunk.get("error") else "error"
            )
            chunk.setdefault("context", [])
            duration_ns = self._compute_duration_ns(chat_id)
            if duration_ns is not None:
                chunk.setdefault("total_duration", duration_ns)
            total_duration = chunk.get("total_duration", 0)
            chunk.setdefault("load_duration", total_duration)
            chunk.setdefault("prompt_eval_duration", 0)
            chunk.setdefault("eval_duration", total_duration)
            response_text = chunk.get("response")
            if response_text is None:
                response_text = "".join(self._response_buffers.get(chat_id, []))
                chunk["response"] = response_text
            final_text = chunk.get("final_response", response_text) or ""
            eval_count = len(final_text.strip())
            chunk.setdefault("eval_count", eval_count)
            chunk.setdefault("prompt_eval_count", 0)
            chunk.setdefault("final_response", response_text or "")
            chunk["message"] = {"role": "assistant", "content": response_text or ""}

            # OpenAI-compatible final choices + usage
            chunk.setdefault(
                "choices",
                [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": final_text},
                        "finish_reason": chunk.get("done_reason", "stop"),
                        "text": final_text,
                    }
                ],
            )
            chunk.setdefault(
                "usage", {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            )
        else:
            # Streaming (partial) chunk: include OpenAI-style delta
            response_text = chunk.get("response", "") or message.get("content", "")
            delta_obj: dict[str, str] = {}
            if response_text:
                # Provide both `content` (chat) and `text` (completion) to
                # satisfy a wider range of clients.
                delta_obj["content"] = response_text
                delta_obj["text"] = response_text
            chunk.setdefault(
                "choices",
                [
                    {
                        "delta": delta_obj,
                        "index": 0,
                        "finish_reason": None,
                    }
                ],
            )

        # Normalize error shape to OpenAI-style object when present
        if chunk.get("error") and not isinstance(chunk.get("error"), dict):
            chunk["error"] = {
                "message": str(chunk.get("error")),
                "type": "server_error",
                "param": None,
                "code": None,
            }

        log_debug(f"[ollama_serve] Publishing chunk for chat_id={chat_id}: {chunk}")
        await queue.put(chunk)
        if chunk.get("done"):
            await queue.put(None)

    async def _stream_text(
        self,
        *,
        chat_id: str,
        model: Optional[str],
        conversation_id: Optional[str],
        text: str,
    ) -> None:
        if not text:
            return

        queue = self._pending_streams.get(chat_id)
        if not queue:
            log_warning(f"[ollama_serve] No active stream found for chat_id={chat_id}")
            return

        self._response_buffers.setdefault(chat_id, []).append(text)
        await self._publish_chunk(
            chat_id,
            {
                "model": model,
                "message": {"role": "assistant", "content": text},
                "done": False,
                "conversation_id": conversation_id,
                "response": text,
            },
        )

    async def _finalize_stream(
        self,
        *,
        chat_id: str,
        model: Optional[str],
        conversation_id: Optional[str],
    ) -> None:
        completion_event = self._completion_events.get(chat_id)
        if completion_event and completion_event.is_set():
            return

        aggregated = "".join(self._response_buffers.get(chat_id, []))
        await self._publish_chunk(
            chat_id,
            {
                "model": model,
                "done": True,
                "conversation_id": conversation_id,
                "response": "",
                "final_response": aggregated,
            },
        )

        if aggregated:
            history = self.context_memory.setdefault(
                chat_id, deque(maxlen=self.max_history)
            )
            history.append(
                self._history_entry_from_message("assistant", aggregated, chat_id)
            )

        if completion_event and not completion_event.is_set():
            completion_event.set()

    async def _fail_stream(
        self,
        chat_id: str,
        conversation_id: Optional[str],
        model: Optional[str],
        error: str,
    ) -> None:
        aggregated = "".join(self._response_buffers.get(chat_id, []))
        await self._publish_chunk(
            chat_id,
            {
                "model": model,
                "error": error,
                "done": True,
                "conversation_id": conversation_id,
                "response": "",
                "final_response": aggregated,
            },
        )
        completion_event = self._completion_events.get(chat_id)
        if completion_event and not completion_event.is_set():
            completion_event.set()

    def _compute_duration_ns(self, chat_id: str) -> Optional[int]:
        start = self._request_start.get(chat_id)
        if start is None:
            return None
        elapsed = time.monotonic() - start
        if elapsed < 0:
            return None
        return int(elapsed * 1_000_000_000)

    def _build_model_catalog(self) -> list[dict[str, Any]]:
        """Return a model catalog derived from available Cortex engines.

        - Expose all registered cortex engines as `models` so external clients
          (AIRI, Ollama front-ends) can choose engines directly.
        - Ensure the configured `BASE_CORTEX` is used as the default model.
        """
        descriptors: list[dict[str, Any]] = []

        # Prefer listing available Cortex engines (synchronous helper)
        try:
            from core.config import list_available_cortex_engines

            engines = list(list_available_cortex_engines(None) or [])
        except Exception:
            # Fallback to plugin-provided models for backward compatibility
            try:
                engines = plugin_instance.get_supported_models() or []
            except Exception as exc:  # pragma: no cover - defensive guard
                log_warning(f"[ollama_serve] Failed to list cortex engines: {exc}")
                engines = []

        # Try to enrich display name from CortexRegistry when available
        display_map: dict[str, str] = {}
        try:
            from core.cortex_registry import get_cortex_registry

            reg = get_cortex_registry()
            for name in engines:
                label = reg._engine_meta.get(name, {}).get("label")
                if label:
                    display_map[name] = label
        except Exception:
            # Not critical — keep going
            pass

        seen = set()
        for name in engines:
            if not isinstance(name, str) or not name:
                continue
            descriptor = self._format_model_descriptor(
                name, display=display_map.get(name)
            )
            descriptors.append(descriptor)
            seen.add(descriptor.get("name") or name)

        # Ensure configured BASE_CORTEX is present and used as default if missing
        try:
            from core.config_manager import config_registry

            base_cortex = (
                config_registry.get_value("BASE_CORTEX", "") or self.default_model_name
            )
        except Exception:
            base_cortex = self.default_model_name

        if base_cortex and base_cortex not in seen:
            descriptors.insert(
                0,
                self._format_model_descriptor(
                    base_cortex, display=self.default_model_display
                ),
            )

        return descriptors

    def _format_model_descriptor(
        self, name: str, *, display: Optional[str] = None
    ) -> dict[str, Any]:
        created = _now_iso()
        display_name = display or name
        # Provide OpenAI/Ollama-style descriptor fields plus a lightweight
        # provider metadata blob so external clients (AIRI, Ollama front-ends)
        # can discover provider information reliably.
        return {
            "id": name,
            "object": "model",
            "name": name,
            "model": name,
            "owned_by": "synthetic-heart",
            "provider": {
                "name": "synthetic-heart",
                "type": "local",
                "display_name": display_name,
            },
            "provider_metadata": {
                "format": "SyntH",
                "family": "synthetic-heart"
                if name == self.default_model_name
                else "generic",
                "parameter_size": "dynamic",
                "quantization_level": "adaptive",
            },
            "modified_at": created,
            "size": 0,
            "digest": "",
            "details": {
                "format": "SyntH",
                "family": "synthetic-heart"
                if name == self.default_model_name
                else "generic",
                "parameter_size": "dynamic",
                "quantization_level": "adaptive",
                "display_name": display_name,
            },
        }

    # ------------------------------------------------------------------
    # Server lifecycle helpers
    # ------------------------------------------------------------------
    def _schedule_server_startup(self) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            log_debug(
                "[ollama_serve] Event loop not running yet; deferring HTTP server startup"
            )
            self._startup_pending = True
            return

        if self._server_task and not self._server_task.done():
            log_debug("[ollama_serve] HTTP server task already active")
            return

        self._startup_pending = False
        self._server_task = loop.create_task(self._run_http_server())
        log_info("[ollama_serve] HTTP server startup scheduled")

    async def _run_http_server(self) -> None:
        import uvicorn

        host = os.getenv("OLLAMA_HOST", "0.0.0.0")
        port = int(os.getenv("OLLAMA_PORT", "11435"))
        config = uvicorn.Config(self.app, host=host, port=port, log_level="info")
        server = uvicorn.Server(config)

        log_info(f"[ollama_serve] Starting HTTP server on {host}:{port}")
        try:
            await server.serve()
        except asyncio.CancelledError:  # pragma: no cover - cancellation path
            log_info("[ollama_serve] HTTP server task cancelled")
            raise
        except OSError as exc:  # pragma: no cover - port conflict
            if "address already in use" in str(exc).lower() or exc.errno == 48:
                log_error(
                    f"[ollama_serve] ❌ Port {port} already in use! "
                    f"Either disable this server with OLLAMA_SERVER_ENABLED=false "
                    f"or change the port with OLLAMA_PORT=<other_port>. "
                    f"If you want to CONNECT to llama.cpp/ollama, use the 'openapi' cortex engine instead."
                )
            else:
                log_error(f"[ollama_serve] HTTP server failed to start: {exc}")
            raise
        except Exception as exc:  # pragma: no cover - defensive logging
            log_error(f"[ollama_serve] HTTP server crashed: {exc}")
            raise
        finally:
            log_info("[ollama_serve] HTTP server stopped")
            self._server_task = None

    async def serve(self) -> None:
        """Public entrypoint to start the HTTP server manually."""
        await self._run_http_server()

    # ------------------------------------------------------------------
    # Methods used by transport layer / actions
    # ------------------------------------------------------------------
    async def send_message(
        self,
        payload_or_chat_id: Optional[Any] = None,
        text: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        payload: dict[str, Any] = {}
        if isinstance(payload_or_chat_id, dict):
            payload = payload_or_chat_id
            text = payload.get("text", text)
            chat_id = payload.get("target") or payload.get("chat_id")
            model = payload.get("model")
            conversation_id = payload.get("conversation_id")
        else:
            chat_id = payload_or_chat_id or kwargs.get("chat_id")
            model = kwargs.get("model")
            conversation_id = kwargs.get("conversation_id")

        if chat_id is None:
            log_warning("[ollama_serve] send_message missing chat_id")
            return

        finalize_flag = kwargs.get("final")
        if finalize_flag is None:
            finalize_flag = payload.get("final")
        if finalize_flag is None:
            finalize_flag = payload.get("done")
        if finalize_flag is None:
            finalize_flag = True

        model = model or self.default_model_name
        chat_id = str(chat_id)
        if text is None and not finalize_flag:
            log_warning(
                "[ollama_serve] send_message missing text and final flag not set"
            )
            return

        if text:
            await self._stream_text(
                chat_id=chat_id,
                model=model,
                conversation_id=conversation_id,
                text=text,
            )

            # Save SyntH's response via core chat_context_manager
            try:
                from core.chat_context_manager import save_response_message
                from core.interface_path_utils import build_interface_path

                msg_interface_path = build_interface_path("ollama_serve", str(chat_id))
                await save_response_message(msg_interface_path, text)
            except Exception as e:
                log_debug(
                    f"[ollama_serve] Failed to save response via context_manager: {e}"
                )

        if finalize_flag:
            await self._finalize_stream(
                chat_id=chat_id,
                model=model,
                conversation_id=conversation_id,
            )

    async def execute_action(
        self,
        action: dict[str, Any],
        context: dict[str, Any],
        bot: Any,
        original_message: Any,
    ) -> None:
        if action.get("type") != "message_ollama_serve":
            return
        payload = action.get("payload", {})
        await self.send_message(payload)


# Expose class and instance for auto discovery
INTERFACE_CLASS = OllamaCompatServer
ollama_serve_interface = OllamaCompatServer()


async def start_server() -> None:
    """Compatibility wrapper for external starters."""
    await ollama_serve_interface.serve()


def initialize_interface():
    """Called by the core initializer to ensure the HTTP server is started.

    The core calls `initialize_interface()` for all interface modules after
    configuration has been loaded. If the module was imported before the
    main event loop existed we may have a pending startup; this function
    makes sure the uvicorn task is scheduled when the loop is available.
    """
    global ollama_serve_interface

    if ollama_serve_interface is None:
        log_info(
            "[ollama_serve] Creating interface instance via initialize_interface()"
        )
        ollama_serve_interface = OllamaCompatServer()

    # Re-register (idempotent) so core_initializer sees the interface
    try:
        register_interface("ollama_serve", ollama_serve_interface)
    except Exception:
        # register_interface may already have been called at import time
        pass

    try:
        ollama_serve_interface._schedule_server_startup()
    except Exception as e:
        log_warning(f"[ollama_serve] Failed to schedule HTTP server startup: {e}")

    return ollama_serve_interface


def shutdown_interface():
    """Shutdown and cleanup the Ollama-compatible HTTP server."""
    global ollama_serve_interface

    if ollama_serve_interface is None:
        log_debug("[ollama_serve] No interface instance to shutdown")
        return

    log_info("[ollama_serve] Shutting down Ollama-compatible HTTP server...")
    try:
        task = getattr(ollama_serve_interface, "_server_task", None)
        if task is not None and not task.done():
            try:
                task.cancel()
            except Exception:
                pass
    except Exception as e:
        log_warning(f"[ollama_serve] Error while cancelling server task: {e}")

    # Unregister from global registry if present
    try:
        from core.core_initializer import INTERFACE_REGISTRY

        INTERFACE_REGISTRY.pop("ollama_serve", None)
    except Exception:
        pass

    ollama_serve_interface = None
    log_info("[ollama_serve] Shutdown complete")


def reload_interface():
    """Reload the interface (used for config reload handlers)."""
    log_info("[ollama_serve] Reloading interface")
    shutdown_interface()
    return initialize_interface()


# Export lifecycle helpers so core_initializer can call them
__all__ = [
    "initialize_interface",
    "shutdown_interface",
    "reload_interface",
    "start_server",
    "OllamaCompatServer",
]

if __name__ == "__main__":
    asyncio.run(start_server())
