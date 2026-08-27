# core/plugin_instance.py

from core.prompt_engine import build_prompt_request
from core.cortex_registry import get_cortex_registry
import asyncio
import contextvars
from contextlib import asynccontextmanager
from types import SimpleNamespace
from datetime import datetime
import json
import base64
import os
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.json_utils import (
    dumps as json_dumps,
    redact_multimodal_for_logging,
    sanitize_for_json,
)
from core.image_processor import get_image_processor, process_image_message
from core.abstract_context import AbstractContext, AbstractUser, AbstractMessage
from core.mention_utils import is_message_for_bot
from core.config_manager import config_registry
from core.multimodal_attachment import (
    extract_multimodal_from_telegram,
    extract_multimodal_from_discord,
    get_mime_type,
    is_supported_type,
)

# Plugin managed centrally in initialize_core_components
plugin = None

if TYPE_CHECKING:
    from plugins.iris_base import IrisResult

# Max characters of inbound text-attachment content injected into the current
# turn, so the full-context Fast Lane model can read attached documents
# directly (no agent/tool needed just to read what the user attached).
_TEXT_ATTACHMENT_MAX_CHARS = 16000


def _get_grillo_engine_label(plugin_obj: Any) -> str | None:
    """Return a short engine label for Grillo activity diagnostics."""
    endpoint = getattr(plugin_obj, "_endpoint", None)
    for candidate in (
        getattr(endpoint, "name", None),
        getattr(plugin_obj, "display_name", None),
        getattr(plugin_obj, "_engine_label", None),
    ):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()

    if plugin_obj is None:
        return None
    return plugin_obj.__class__.__name__


def _build_empty_grillo_response_text(
    response_metadata: dict[str, Any] | None,
    engine_label: str | None,
) -> str:
    """Render a diagnostic placeholder for empty Grillo LLM replies."""
    details: list[str] = []
    if isinstance(engine_label, str) and engine_label.strip():
        details.append(f"engine={engine_label.strip()}")

    metadata = response_metadata if isinstance(response_metadata, dict) else {}
    for key in ("model", "finish_reason", "block_reason", "error"):
        value = metadata.get(key)
        if value in (None, ""):
            continue
        if key == "model" and isinstance(value, str) and value == engine_label:
            continue
        details.append(f"{key}={value}")

    if details:
        return "[EMPTY LLM RESPONSE] " + " ".join(details)
    return "[EMPTY LLM RESPONSE]"


def _compute_prompt_signature(prompt_for_engine: Any) -> str | None:
    """Return a short, bounded fingerprint of a prompt for the cached-safe-response store.

    Best-effort: any failure returns ``None`` (which disables caching for the
    turn). Only a bounded prefix of the serialized prompt is hashed so large
    prompts never cost a full re-serialization on the hot path.
    """
    try:
        import hashlib

        if isinstance(prompt_for_engine, bytes):
            blob = prompt_for_engine
        elif isinstance(prompt_for_engine, str):
            blob = prompt_for_engine.encode("utf-8", "replace")
        else:
            blob = json_dumps(prompt_for_engine).encode("utf-8", "replace")
        return hashlib.sha256(blob[:8192]).hexdigest()[:16]
    except Exception:
        return None


def _restore_plugin_instance(instance: object) -> None:
    """Directly restore the module-level plugin reference from a saved instance."""
    global plugin  # noqa: PLW0603
    plugin = instance
    from core.logging_utils import log_info as _log_info

    _log_info(
        f"[plugin_instance] Restored cortex instance directly: {instance.__class__.__name__}"
    )


# Global lease to serialize LLM chains (Recon + prompt + actions) across tasks
_llm_chain_lock = asyncio.Lock()
_llm_chain_depth = contextvars.ContextVar("llm_chain_depth", default=0)

try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "LLM_CHAIN_LEASE_TIMEOUT_SEC",
        label="LLM chain lease timeout (s)",
        default=2400,
        value_type=int,
        ui_type="number",
        description=(
            "Force-release the global LLM chain lease after this many seconds to "
            "avoid deadlocks. Set to 0 to disable. Keep this above "
            "LLM_GENERATION_TIMEOUT_SEC so the lease is not released while a slow "
            "generation is still running (default 2400s / 40 min)."
        ),
        scope="agent",
        component="agent",
        advanced=True,
        needs_component_reload=False,
    )
except Exception:
    pass


@asynccontextmanager
async def _llm_chain_lease():
    depth = _llm_chain_depth.get()
    if depth > 0:
        _llm_chain_depth.set(depth + 1)
        try:
            yield
        finally:
            _llm_chain_depth.set(max(depth - 1, 0))
        return

    try:
        timeout = int(
            config_registry.get_value(
                "LLM_CHAIN_LEASE_TIMEOUT_SEC", 2400, value_type=int
            )
            or 2400
        )
    except Exception:
        timeout = 2400

    acquired = False
    try:
        if timeout > 0:
            await asyncio.wait_for(_llm_chain_lock.acquire(), timeout=timeout)
        else:
            await _llm_chain_lock.acquire()
        acquired = True
    except asyncio.TimeoutError:
        log_warning(
            f"[plugin_instance] LLM chain lease acquire timed out after {timeout}s; proceeding without lock"
        )
        yield
        return

    _llm_chain_depth.set(1)
    watchdog_task = None

    async def _lease_watchdog() -> None:
        try:
            await asyncio.sleep(timeout)
            if _llm_chain_lock.locked():
                log_error(
                    f"[plugin_instance] LLM chain lease timed out after {timeout}s; forcing release"
                )
                try:
                    _llm_chain_lock.release()
                except Exception:
                    pass
        except asyncio.CancelledError:
            pass

    if timeout > 0:
        watchdog_task = asyncio.create_task(_lease_watchdog())

    try:
        yield
    finally:
        _llm_chain_depth.set(0)
        if watchdog_task:
            watchdog_task.cancel()
        if acquired and _llm_chain_lock.locked():
            try:
                _llm_chain_lock.release()
            except Exception:
                pass


async def load_plugin(
    name: str,
    notify_fn=None,
    *,
    ensure_started: bool = False,
    start_timeout: float = 30.0,
):
    global plugin

    # 🔁 If already loaded but different, replace it or update notify_fn
    if plugin is not None:
        current_plugin_name = plugin.__class__.__module__.split(".")[-1]
        # For engines registered as direct instances (e.g. ExternalCortexEngine),
        # the module name ("cortex_bridge") differs from the registry key
        # ("ext_xyz").  If the registry already holds this exact instance under
        # the requested name, there is nothing to reload.
        try:
            _reg = get_cortex_registry()
            if _reg.get_engine(name) is plugin:
                # Same instance already in registry under the requested name.
                if notify_fn and hasattr(plugin, "set_notify_fn"):
                    try:
                        plugin.set_notify_fn(notify_fn)
                        log_debug("[plugin] ✅ notify_fn updated dynamically")
                    except Exception as e:
                        log_error(f"[plugin] ❌ Unable to update notify_fn: {e}", e)
                else:
                    log_debug(
                        f"[plugin] ⚠️ Plugin already loaded: {plugin.__class__.__name__}"
                    )
                return
        except Exception:
            pass

        if current_plugin_name != name:
            log_debug(
                f"[plugin] 🔄 Changing plugin from {current_plugin_name} to {name}"
            )
            # Wait for any ongoing response to complete before cleanup
            if (
                hasattr(plugin, "_worker_task")
                and plugin._worker_task
                and not plugin._worker_task.done()
            ):
                log_debug(
                    f"[plugin] ⏳ Waiting for ongoing response to complete in {current_plugin_name}"
                )
                try:
                    # Use a reasonable timeout for plugin switching (30 seconds), not the full LLM response timeout
                    await asyncio.wait_for(plugin._worker_task, timeout=30.0)
                    log_debug(
                        f"[plugin] ✅ Ongoing response completed in {current_plugin_name}"
                    )
                except asyncio.TimeoutError:
                    # Prefer to avoid force-cancelling an in-progress LLM response here;
                    # rely on the engine's own waiting/recovery logic (Selenium already
                    # implements robust wait/retry). Log and proceed with the hotswap
                    # without cancelling the worker task to prevent premature stop.
                    log_warning(
                        f"[plugin] ⏰ Timeout waiting for response completion in {current_plugin_name}; continuing without cancelling the worker (will let engine finish)"
                    )
                except Exception as e:
                    log_warning(
                        f"[plugin] ⚠️ Error waiting for response completion: {e}"
                    )
            # Cleanup the previous plugin before loading the new one
            if hasattr(plugin, "cleanup"):
                try:
                    plugin.cleanup()
                    log_debug(
                        f"[plugin] ✅ Previous plugin {current_plugin_name} cleaned up"
                    )
                except Exception as e:
                    log_error(
                        f"[plugin] ❌ Error cleaning up previous plugin {current_plugin_name}: {e}"
                    )
            elif hasattr(plugin, "stop"):
                try:
                    if asyncio.iscoroutinefunction(plugin.stop):
                        await plugin.stop()
                    else:
                        plugin.stop()
                    log_debug(
                        f"[plugin] ✅ Previous plugin {current_plugin_name} stopped"
                    )
                except Exception as e:
                    log_error(
                        f"[plugin] ❌ Error stopping previous plugin {current_plugin_name}: {e}"
                    )
            # Clear the global plugin reference
            plugin = None
        else:
            # 🔁 Even if it's the same plugin, update notify_fn if provided
            if notify_fn and hasattr(plugin, "set_notify_fn"):
                try:
                    plugin.set_notify_fn(notify_fn)
                    log_debug("[plugin] ✅ notify_fn updated dynamically")
                except Exception as e:
                    log_error(f"[plugin] ❌ Unable to update notify_fn: {e}", e)
            else:
                log_debug(
                    f"[plugin] ⚠️ Plugin already loaded: {plugin.__class__.__name__}"
                )
            return

    try:
        registry = get_cortex_registry()
        plugin_instance = registry.load_engine(name, notify_fn)
    except Exception as e:
        log_error(f"[plugin] ❌ Cortex registry load failed for {name}: {e}", e)
        raise

    plugin = plugin_instance
    log_debug(f"[plugin] Plugin initialized: {plugin.__class__.__name__}")

    if hasattr(plugin, "start"):
        try:
            start_fn = plugin.start
            if asyncio.iscoroutinefunction(start_fn):
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = None

                if loop and loop.is_running():
                    if ensure_started:
                        # Await the coroutine start to ensure plugin initialized properly
                        try:
                            log_debug(
                                "[plugin] Awaiting async plugin start (ensure_started=True)"
                            )
                            await asyncio.wait_for(start_fn(), timeout=start_timeout)
                            log_debug(
                                "[plugin] ✅ Async plugin start awaited and completed"
                            )
                        except asyncio.TimeoutError:
                            log_error(
                                f"[plugin] ❌ Async plugin start timed out after {start_timeout}s"
                            )
                            raise
                        except Exception as e:
                            log_error(f"[plugin] ❌ Async plugin start failed: {e}", e)
                            raise
                    else:
                        # Schedule start as background task but attach callback to handle exceptions
                        task = loop.create_task(start_fn())

                        def _on_start_done(t):
                            try:
                                exc = t.exception()
                                if exc:
                                    log_error(
                                        f"[plugin] Async plugin start failed: {exc}"
                                    )
                                else:
                                    log_debug(
                                        "[plugin] Async plugin start completed successfully"
                                    )
                            except asyncio.CancelledError:
                                log_warning(
                                    "[plugin] Async plugin start task was cancelled"
                                )
                            except Exception as e:
                                log_error(
                                    f"[plugin] Error handling plugin start completion: {e}"
                                )

                        task.add_done_callback(_on_start_done)
                        log_debug("[plugin] Plugin start scheduled on running loop.")
                else:
                    if ensure_started:
                        # We cannot await start() without a running loop in this context
                        log_error(
                            "[plugin] ❌ Cannot ensure async plugin start: no running event loop available"
                        )
                        raise RuntimeError(
                            "No running event loop to await plugin start"
                        )
                    log_debug(
                        "[plugin] No running loop; plugin start will be invoked later."
                    )
            else:
                # Synchronous start
                if ensure_started:
                    start_fn()
                    log_debug(
                        "[plugin] ✅ Synchronous plugin start executed (ensure_started=True)"
                    )
                else:
                    start_fn()
                    log_debug("[plugin] Plugin start executed.")
        except Exception as e:
            log_error(f"[plugin] Error during plugin start: {e}", e)
            if ensure_started:
                # Propagate errors when the caller requested a guaranteed start
                raise

    # Default model
    if hasattr(plugin, "get_supported_models"):
        try:
            models = plugin.get_supported_models()
            if models:
                from core.config import get_current_model, set_current_model

                current = get_current_model()
                if not current:
                    set_current_model(models[0])
                    log_debug(f"[plugin] Default model set: {models[0]}")
        except Exception as e:
            log_warning(f"[plugin] Error during model setup: {e}")

    # NOTE: Do NOT call set_base_cortex() here - it overwrites the DB value during startup
    # The active LLM is already persisted when changed via WebUI/commands, and should be
    # loaded from DB during initialization, not overwritten with the current plugin name


async def handle_incoming_message(
    bot: Any,
    message: Any,
    context_memory_or_prompt: Any,
    interface: str | None = None,
) -> Any:
    """Process incoming messages or pre-built prompts."""

    async with _llm_chain_lease():
        # Check if plugin is loaded
        if plugin is None:
            log_error(
                "[plugin_instance] No LLM plugin loaded! Cannot handle incoming message."
            )
            log_error(f"[plugin_instance] Available plugins: {dir()}")
            raise ValueError("No LLM plugin loaded")

        # Normalize message user/date fields to avoid AttributeErrors later
        try:
            from core.user_utils import ensure_message_user_fields

            ensure_message_user_fields(message)
        except Exception:
            pass

        # Save both the instance and its registry name so we can restore cleanly.
        # The module-path tail (e.g. "cortex_bridge") is NOT a registry key and
        # cannot be passed back to load_plugin — keep the instance as a fallback.
        original_plugin_instance = plugin  # may be None
        original_plugin_name: str | None = None
        if plugin is not None:
            try:
                reg = get_cortex_registry()
                # Prefer the registered name (reverse-lookup by identity)
                for _k, _v in reg._engines.items():
                    if _v is plugin:
                        original_plugin_name = _k
                        break
            except Exception:
                pass
            # Last resort: derive from module path (may be wrong for bridge engines)
            if not original_plugin_name:
                original_plugin_name = plugin.__class__.__module__.split(".")[-1]

        desired_plugin_name = original_plugin_name
        should_restore_plugin = False

        try:
            if isinstance(context_memory_or_prompt, dict):
                if context_memory_or_prompt.get("is_trainer"):
                    from core.config import get_active_cortex_engine

                    desired_plugin_name = await get_active_cortex_engine(
                        scope="trainer"
                    )
        except Exception as e:
            log_warning(f"[plugin_instance] Failed to resolve trainer cortex: {e}")

        if (
            desired_plugin_name
            and original_plugin_name
            and desired_plugin_name != original_plugin_name
        ):
            log_info(
                f"[plugin_instance] Switching cortex for trainer message: {original_plugin_name} -> {desired_plugin_name}"
            )
            await load_plugin(desired_plugin_name, ensure_started=True)
            should_restore_plugin = True

        if message is None and isinstance(context_memory_or_prompt, dict):
            prompt = context_memory_or_prompt
            payload = prompt.get("input", {}).get("payload", {})
            message = SimpleNamespace(
                chat_id="TARDIS / system / events",
                message_id=int(datetime.utcnow().timestamp() * 1000) % 1_000_000,
                text=payload.get("text") or payload.get("description") or "",
                date=datetime.utcnow(),
                from_user=SimpleNamespace(id=0, full_name="system", username="system"),
                reply_to_message=None,
                chat=SimpleNamespace(id="TARDIS / system / events", type="private"),
            )
            log_debug("[plugin_instance] Handling pre-built event prompt")
        else:
            # If this is a structured 'event' system prompt, enqueue it into the
            # central message queue with high priority so it is processed ASAP.
            try:
                # Prefer explicit context dict (pre-built prompts)
                maybe_ctx = (
                    context_memory_or_prompt
                    if isinstance(context_memory_or_prompt, dict)
                    else None
                )
                sys_type = None
                if maybe_ctx and isinstance(maybe_ctx.get("system_message"), dict):
                    sys_type = maybe_ctx["system_message"].get("type")
                # Also accept messages that carry a system-like from_user (id==0)
                if sys_type == "event" or (
                    hasattr(message, "from_user")
                    and getattr(message.from_user, "id", None) == 0
                    and isinstance(context_memory_or_prompt, dict)
                    and context_memory_or_prompt.get("system_message", {}).get("type")
                    == "event"
                ):
                    try:
                        # Import lazily to avoid circular imports at module load
                        from core import message_queue

                        event_id = None
                        if maybe_ctx:
                            event_id = maybe_ctx.get("system_message", {}).get(
                                "event_id"
                            )
                        await message_queue.enqueue_event(
                            bot, context_memory_or_prompt, event_id=event_id
                        )
                        log_debug(
                            f"[plugin_instance] Enqueued system event for processing: chat_id={getattr(message, 'chat_id', None)} event_id={event_id}"
                        )
                        return None
                    except Exception as e:
                        log_warning(
                            f"[plugin_instance] Failed to enqueue event prompt: {e}"
                        )
                        # Fall through and let the plugin handle it directly
                        pass
            except Exception:
                pass

        message_text = getattr(message, "text", "")
        log_debug(f"[plugin_instance] Received message: {message_text}")
        log_debug(f"[plugin_instance] Context memory: {context_memory_or_prompt}")
        user_id = message.from_user.id if message.from_user else "unknown"
        interface_name = (
            interface
            if interface
            else (
                bot.get_interface_id()
                if hasattr(bot, "get_interface_id")
                else bot.__class__.__name__
            )
        )
        log_debug(
            f"[plugin] Incoming for {plugin.__class__.__name__}: chat_id={message.chat_id}, user_id={user_id}, text={message_text!r} via {interface_name}"
        )

        image_data, has_image_trigger = await _extract_image_data_from_message(
            message, interface_name
        )

        processed_image_data = None
        if image_data:
            log_info(
                f"[plugin_instance] Message contains image: {image_data['type']} from user {user_id}"
            )

            # Create abstract context for image processing
            abstract_user = AbstractUser(id=user_id, interface_name=interface_name)
            abstract_message = AbstractMessage(
                id=getattr(message, "message_id", None),
                text=getattr(message, "text", "") or getattr(message, "caption", ""),
                chat_id=getattr(message, "chat_id", None),
                interface_name=interface_name,
            )
            abstract_context = AbstractContext(
                interface_name=interface_name,
                user=abstract_user,
                message=abstract_message,
            )

            # Check if message has text trigger (mentions, keywords, etc.)
            text_has_trigger = False
            if message_text:
                directed, reason = await is_message_for_bot(
                    message, bot, human_count=None
                )
                text_has_trigger = directed

            # Combine image trigger with text trigger
            combined_trigger = has_image_trigger or text_has_trigger

            # Process the image (but don't auto-forward to LLM here)
            processed_image_data = await process_image_message(
                image_data,
                abstract_context,
                has_trigger=combined_trigger,
                forward_to_llm=False,  # We'll include it in the prompt instead
            )

            if processed_image_data:
                log_info(
                    f"[plugin_instance] Image processed successfully for user {user_id}"
                )
            else:
                log_debug(
                    f"[plugin_instance] Image not processed (access denied or error) for user {user_id}"
                )

        # Unified multimodal extraction (extract BEFORE prompt building so
        # attachments flow through the pipeline with full context).
        # Skip extraction ONLY when the voice was already transcribed to text
        # (e.g. by Auris STT). If the message has is_voice_input=True but NO
        # text, it means transcription failed/was skipped and we need the raw
        # audio attachment so the LLM engine can process it natively.
        _is_voice_input = isinstance(context_memory_or_prompt, dict) and bool(
            context_memory_or_prompt.get("is_voice_input", False)
        )
        _has_transcribed_text = bool(getattr(message, "text", None))
        # In Auris 'inline' mode no transcription happens, so always extract the
        # raw audio attachment (even when a caption supplies message text) so it
        # can be forwarded inline to the Cortex engine.
        _auris_inline = _get_active_auris_engine() == "inline"
        if _is_voice_input and _has_transcribed_text and not _auris_inline:
            attachments: list[dict] = []
            log_debug(
                "[plugin_instance] Skipping multimodal extraction: is_voice_input=True and text present (already transcribed)"
            )
        else:
            attachments = await _extract_multimodal_attachments(
                bot, message, interface_name
            )
        if attachments:
            log_info(
                f"[plugin_instance] Message contains {len(attachments)} attachments from user {user_id}"
            )
            # Materialize inbound document/audio attachments onto a sandbox path
            # the agent tools (agent_read_file) and Auris (stt_transcribe) can
            # actually resolve. The multimodal extractor only keeps base64 bytes
            # in memory — a downstream tool call then receives a bare filename
            # that does not exist ("File not found: Untitled document.pdf",
            # Langfuse 11feca6f). Images/videos stay in-memory for Iris/inline
            # vision; only non-visual media is persisted. Fail-safe: any error
            # skips persistence, the turn proceeds unchanged.
            _sandbox_paths: list[str] = []
            try:
                _sandbox_paths = _persist_attachments_to_sandbox(attachments)
                if _sandbox_paths:
                    log_info(
                        f"[plugin_instance] Persisted {len(_sandbox_paths)} "
                        f"attachment(s) for agent/auris access: {_sandbox_paths}"
                    )
            except Exception as _persist_exc:
                log_warning(
                    f"[plugin_instance] Attachment persistence skipped: {_persist_exc}"
                )
            # Surface the persisted paths on the shared context so the Agent
            # Lane prompt can point the model at the actual file instead of a
            # nonexistent bare filename (the recon/agent-intent path inherits
            # context_memory into the router ctx).
            if _sandbox_paths and isinstance(context_memory_or_prompt, dict):
                _existing = context_memory_or_prompt.get("attachment_paths") or []
                if isinstance(_existing, list):
                    context_memory_or_prompt["attachment_paths"] = (
                        _existing + _sandbox_paths
                    )
                else:
                    context_memory_or_prompt["attachment_paths"] = _sandbox_paths
            # 'inline' is a hardcoded Iris pseudo-engine: skip the description
            # step entirely and let image/video bytes flow through to the Cortex
            # engine so a vision-capable multimodal model can see them directly.
            # (The cortex bridge drops image parts — with a warning — when the
            # active endpoint is not marked vision-capable.)
            if _get_active_iris_engine() == "inline":
                log_info(
                    "[plugin_instance] Iris inline mode: forwarding image/video "
                    "attachments to the Cortex engine without description"
                )
            else:
                # Do NOT pass the user's text as the Iris prompt — Iris must
                # receive a neutral plain-text instruction so the vision engine
                # returns a description rather than formatted/structured output.
                # The user's actual question is answered by the main LLM after the
                # Iris description is injected into the context.  The instruction
                # is user-editable via IRIS_DEFAULT_PROMPT (Engines tab); no
                # prompt is passed here so describe_media falls back to it.
                iris_result = await _describe_attachment_images_with_iris(attachments)
                if iris_result is not None:
                    try:
                        original_text = getattr(message, "text", "") or ""
                        # Build a first-person "current perception" block.  The
                        # description is what the synth is *seeing right now* in
                        # the image the user just shared — framing it as live
                        # perception (rather than a neutral "[Iris vision: ...]"
                        # metadata record) stops the model from treating a fresh
                        # image as a distant/faded memory ("old analysis logs").
                        # Kept language-neutral: static English framing text, no
                        # keyword/phrase detection on the user's message.
                        parts: list[str] = [iris_result.description]
                        if iris_result.language:
                            parts.append(f"language: {iris_result.language}")
                        if iris_result.confidence is not None:
                            parts.append(f"confidence: {iris_result.confidence:.2f}")
                        media_label = (
                            "Sticker"
                            if getattr(iris_result, "is_sticker", False)
                            else "Image"
                        )
                        description_block = (
                            f"[{media_label} the user just shared with you — this is what "
                            "you can see in it right now: " + " | ".join(parts) + "]"
                        )
                        if original_text:
                            augmented_text = f"{original_text}\n\n{description_block}"
                        else:
                            augmented_text = description_block
                        setattr(message, "text", augmented_text)
                        log_info(
                            "[plugin_instance] Appended Iris vision analysis to prompt text"
                        )

                        # Persist the enriched text into the already-saved
                        # chat-history row so the description survives across
                        # turns (the raw caption was saved upstream by the
                        # interface).  Also record the durable cache path in
                        # metadata so the synth can re-inspect the media later.
                        try:
                            msg_iface = getattr(message, "interface_path", None)
                            raw_ts = getattr(message, "date", None) or getattr(
                                message, "timestamp", None
                            )
                            msg_ts = (
                                raw_ts.isoformat()
                                if hasattr(raw_ts, "isoformat")
                                else raw_ts
                            )
                            if msg_iface and msg_ts:
                                update_meta: dict | None = None
                                if getattr(iris_result, "cached_path", None):
                                    _cached_mime = None
                                    for _att in attachments:
                                        _m = str(
                                            _att.get("mime_type")
                                            or _att.get("content_type")
                                            or _att.get("type")
                                            or ""
                                        )
                                        if _m.startswith(("image/", "video/")):
                                            _cached_mime = _m
                                            break
                                    update_meta = {
                                        "iris_cached_path": iris_result.cached_path,
                                        "iris_cached_mime": _cached_mime,
                                    }
                                    if getattr(iris_result, "is_sticker", False):
                                        update_meta["iris_is_sticker"] = True
                                from core.chat_context_manager import (
                                    update_message_in_context,
                                )

                                await update_message_in_context(
                                    interface_path=msg_iface,
                                    timestamp=msg_ts,
                                    message_text=augmented_text,
                                    metadata=update_meta,
                                )
                            else:
                                log_debug(
                                    "[plugin_instance] Iris persist skipped: missing "
                                    f"interface_path/timestamp (iface={msg_iface!r}, ts={msg_ts!r})"
                                )
                        except Exception as exc:
                            log_warning(
                                f"[plugin_instance] Could not persist Iris description to history: {exc}"
                            )
                    except Exception as exc:
                        log_warning(
                            f"[plugin_instance] Could not append Iris description to message text: {exc}"
                        )

                # Strip image/video base64 data from attachments so the Cortex
                # engine does not receive raw vision bytes.  Iris already provided
                # a textual description (or a placeholder).  Audio and document
                # attachments are kept intact for engines that support them natively.
                attachments = [
                    att
                    for att in attachments
                    if not (
                        att.get("mime_type")
                        or att.get("content_type")
                        or att.get("type")
                        or ""
                    ).startswith(("image/", "video/"))
                ]

                # Inline bounded content of inbound TEXT attachments into the
                # current turn (see _inject_text_attachment_content) so the
                # full-context model can read what the user attached without
                # needing the Agent Lane's filesystem tools.
                _inject_text_attachment_content(message, attachments)

        if isinstance(context_memory_or_prompt, str):
            try:
                import json

                prompt = json.loads(context_memory_or_prompt)
            except Exception as e:
                log_warning(f"[plugin_instance] Failed to parse direct prompt: {e}")
                # Get model's max chars limit - try plugin, then fallback to DEFAULT
                max_chars = None
                try:
                    if plugin and hasattr(plugin, "model_limits_map"):
                        current_model = (
                            await plugin.get_current_model()
                            if hasattr(plugin, "get_current_model")
                            else None
                        )
                        if current_model and current_model in plugin.model_limits_map:
                            max_chars = plugin.model_limits_map[current_model]
                except Exception:
                    pass

                # Fallback: get from plugin's default model or use engine limits
                if not max_chars:
                    try:
                        if (
                            plugin
                            and hasattr(plugin, "model_limits_map")
                            and "default" in plugin.model_limits_map
                        ):
                            max_chars = plugin.model_limits_map["default"]
                            log_debug(
                                f"[plugin_instance] Using default max_chars from plugin: {max_chars}"
                            )
                    except Exception as e:
                        log_warning(
                            f"[plugin_instance] Failed to get default max_chars: {e}"
                        )

                log_debug(f"[plugin_instance] Exception path - max_chars={max_chars}")
                prompt = await build_prompt_request(
                    message,
                    {},
                    interface_name,
                    image_data=processed_image_data,
                    attachments=attachments,
                    max_chars=max_chars,
                )

            # ── Delivery-turn structural scoping (search-loop fix, 2026-08-17) ──
            # A delivery turn is enqueued as a JSON string ({system_message,
            # allowed_action_types}) and parsed successfully above, so it BYPASSES
            # build_prompt_request — the prior allowed_action_types allowlist was
            # therefore never applied to the action catalog the weak selenium model
            # saw, and it re-emitted the producing action (e.g.
            # search_current_knowledge) in a loop. Here we rebuild the delivery
            # turn as a PromptRequest whose tool_declarations are message_* only;
            # cortex_bridge._inject_actions_into_prompt folds exactly those into the
            # system prompt, so the model literally cannot see the producing action.
            # The original dict (system_message + allowed_action_types) is kept intact
            # so the corrector metadata and allowlist still reach message_chain
            # unchanged (plugin_instance pops __prompt_request before sanitizing and
            # routes it to PromptRequest-aware engines below).
            if isinstance(prompt, dict):
                _pr = await _scope_delivery_prompt_request(
                    prompt, interface_name, getattr(message, "interface_path", None)
                )
                if _pr is not None:
                    prompt["__prompt_request"] = _pr
        else:
            # Get model's max chars limit - try plugin, then fallback to DEFAULT
            max_chars = None

            # Try plugin's model_limits_map
            try:
                if plugin and hasattr(plugin, "model_limits_map"):
                    current_model = None
                    if hasattr(plugin, "get_current_model"):
                        try:
                            current_model = await plugin.get_current_model()
                        except Exception:
                            pass

                    if current_model and current_model in plugin.model_limits_map:
                        max_chars = plugin.model_limits_map[current_model]
            except Exception:
                pass

            # Fallback: get from plugin's default model
            if not max_chars:
                try:
                    if (
                        plugin
                        and hasattr(plugin, "model_limits_map")
                        and "default" in plugin.model_limits_map
                    ):
                        max_chars = plugin.model_limits_map["default"]
                        log_debug(
                            f"[plugin_instance] Using default max_chars from plugin: {max_chars}"
                        )
                except Exception as e:
                    log_warning(
                        f"[plugin_instance] Failed to get default max_chars: {e}"
                    )

            if max_chars is None:
                log_error(
                    f"[plugin_instance] max_chars is STILL None! plugin={plugin}, has model_limits_map={hasattr(plugin, 'model_limits_map') if plugin else False}"
                )

            log_debug(
                f"[plugin_instance] Passing max_chars={max_chars} to build_prompt_request()"
            )
            prompt = await build_prompt_request(
                message,
                context_memory_or_prompt,
                interface_name,
                image_data=processed_image_data,
                attachments=attachments,
                max_chars=max_chars,
            )

    # Multimodal attachments already extracted and passed to build_prompt_request above
    if attachments:
        log_info(
            f"[plugin_instance] Processing {len(attachments)} multimodal attachment(s)"
        )

    # Pull typed PromptRequest out of the transport dict before sanitization.
    # Only PromptRequest-aware engines receive this object directly; legacy
    # engines keep the sanitized transport dict until they opt in.
    prompt_request_obj: object | None = None
    _reason_trail_dict: dict | None = None
    if isinstance(prompt, dict):
        prompt_request_obj = prompt.pop("__prompt_request", None)
        # Per-turn reason trail ("why did I say that") is a diagnostics payload
        # only. It must never reach the engine, so pop it here exactly like
        # ``__prompt_request`` before sanitization (see core/turn_reason.py).
        _reason_trail_dict = prompt.pop("__reason_trail", None)
        if not isinstance(_reason_trail_dict, dict):
            _reason_trail_dict = None
        if _reason_trail_dict is not None and isinstance(
            context_memory_or_prompt, dict
        ):
            # Thread the reason to the send path: the llm_context built below
            # inherits from context_memory_or_prompt, so message_chain records
            # exactly one row per turn once the reply text is known (and fills
            # reply_preview). The key is internal and never part of a prompt.
            context_memory_or_prompt["_reason_trail"] = _reason_trail_dict
        elif _reason_trail_dict is not None:
            # Non-dict context: there is no ctx thread to message_chain, so
            # record directly here (the reply preview is not known yet).
            try:
                from core.turn_reason import record_reason

                await record_reason(
                    interface_path=getattr(message, "interface_path", None),
                    reply_preview=None,
                    memories=_reason_trail_dict.get("memories"),
                    diary_sources=_reason_trail_dict.get("diary_sources"),
                    emotion=_reason_trail_dict.get("emotion"),
                    goal=_reason_trail_dict.get("goal"),
                    beat_type=_reason_trail_dict.get("beat_type"),
                    history_scope=_reason_trail_dict.get("history_scope"),
                )
            except Exception as _reason_exc:
                log_debug(
                    f"[plugin_instance] Reason trail fallback record skipped: {_reason_exc}"
                )

    prompt = sanitize_for_json(prompt)
    log_debug("🌐 JSON PROMPT built for the plugin:")
    try:
        prompt_json = json_dumps(prompt)
        # Log in full without truncation for debugging
        log_debug(prompt_json)
    except Exception as e:
        log_error(f"Failed to serialize prompt: {e}")

    # Trace handoff to LLM plugin
    try:
        # If prompt contains pre-reduction size metadata, include it in logs for debugging
        pre_size = None
        try:
            if isinstance(prompt, dict):
                # Prefer explicit pre_reduction_size if provided by build_prompt_request
                pre_size = prompt.get("__pre_reduction_size", None)
                # If not present, compute a fallback (note: this is post-reduction size)
                if pre_size is None:
                    try:
                        pre_size = len(json_dumps(prompt))
                        log_debug(
                            f"[flow] pre_reduction_size missing, using computed size={pre_size} (post-reduction)"
                        )
                    except Exception:
                        pre_size = None
        except Exception:
            pre_size = None

        log_info(
            f"[flow] -> LLM plugin: handing off chat_id={getattr(message, 'chat_id', None)} interface={interface} prompt_len={len(json_dumps(prompt)) if isinstance(prompt, (dict, list)) else len(str(prompt))} pre_reduction_size={pre_size}"
        )
        # debug log of the full prompt content for reconstruction
        try:
            log_debug(
                "[flow] prompt content: "
                + json_dumps(redact_multimodal_for_logging(prompt))
            )
        except Exception:
            pass
    except Exception:
        log_info(
            f"[flow] -> LLM plugin: handing off chat_id={getattr(message, 'chat_id', None)} interface={interface}"
        )

    # ── Scope-based engine resolution ───────────────────────────────
    # Always resolve the active engine via the registry so that the main LLM
    # call, Recon, and Debrief all use the same engine for a given request.
    # derive_cortex_scope() is the single authoritative mapping from context
    # flags (is_trainer, grillo_beat) to scope strings.
    effective_plugin = plugin
    _scope_model: str | None = None
    _scope: str | None = None
    _primary_engine_name: str | None = original_plugin_name
    try:
        from core.config import derive_cortex_scope, get_active_cortex_scope

        _scope = derive_cortex_scope(
            context_memory_or_prompt
            if isinstance(context_memory_or_prompt, dict)
            else None
        )

        log_debug(
            f"[plugin_instance] Scope routing: _scope={_scope}, "
            f"is_trainer={context_memory_or_prompt.get('is_trainer') if isinstance(context_memory_or_prompt, dict) else 'N/A'}, "
            f"global_plugin={plugin.__class__.__name__ if plugin else None}"
        )

        active_engine_name, _scope_model = await get_active_cortex_scope(scope=_scope)
        _primary_engine_name = active_engine_name
        reg = get_cortex_registry()
        resolved = reg.get_engine(active_engine_name)
        if resolved is None:
            resolved = reg.load_engine(active_engine_name)
        if resolved is not None and resolved is not plugin:
            effective_plugin = resolved
            log_info(
                f"[plugin_instance] Engine resolved from registry: '{active_engine_name}' "
                f"(scope={_scope!r}, model={_scope_model!r})"
            )
    except Exception as scope_exc:
        log_warning(
            f"[plugin_instance] Scope routing failed, falling back to global plugin: {scope_exc}"
        )

    try:
        if effective_plugin is None:
            log_error("[plugin_instance] No LLM plugin loaded, cannot process message")
            raise ValueError("No LLM plugin loaded")

        prompt_for_engine = prompt
        if prompt_request_obj is not None and bool(
            getattr(effective_plugin, "supports_prompt_request", False)
        ):
            prompt_for_engine = prompt_request_obj
        from core.config import scope_model_override

        # ── Staged cortex fallback (primary → local → cached/safe) ─────
        # This is the single choke point where every chat turn generates text,
        # for both built-in engines (which implement handle_incoming_message)
        # and external engines (which implement generate_response). Route the
        # generation through core.cortex_fallback so an empty or timed-out
        # primary engine degrades to a configured fallback engine and then to a
        # cached safe response. Everything is fail-open: if the module cannot be
        # imported, or the wrapper raises, the primary outcome is preserved so
        # the direct call is indistinguishable from the pre-fallback behavior.
        async def _call_engine(engine_name: str) -> Any:
            instance = effective_plugin
            if engine_name != _primary_engine_name:
                _fallback_reg = get_cortex_registry()
                instance = _fallback_reg.get_engine(engine_name)
                if instance is None:
                    instance = _fallback_reg.load_engine(engine_name)
                if instance is None:
                    raise ValueError(f"Cortex engine {engine_name!r} is not registered")
            with scope_model_override(instance, _scope_model):
                return await instance.handle_incoming_message(
                    bot, message, prompt_for_engine
                )

        _prompt_signature = _compute_prompt_signature(prompt_for_engine)

        try:
            from core import cortex_fallback
        except Exception as _import_exc:  # pragma: no cover - defensive
            log_warning(
                f"[plugin_instance] core.cortex_fallback import failed: "
                f"{_import_exc}; using direct call"
            )
            cortex_fallback = None

        if cortex_fallback is not None:
            try:
                result = await cortex_fallback.run_cortex_with_fallback(
                    engine_name=_primary_engine_name or "default",
                    scope=_scope,
                    call_engine=_call_engine,
                    prompt_signature=_prompt_signature,
                )
            except Exception as _fb_exc:
                # The wrapper is fail-open and only propagates the primary
                # engine's own outcome (a TimeoutError or the primary's original
                # exception). Re-running the direct call here would double the
                # primary call (and double a timeout), so propagate it exactly
                # as the direct call would have.
                log_warning(
                    f"[plugin_instance] Staged cortex fallback raised; "
                    f"propagating primary outcome: {_fb_exc}"
                )
                raise
        else:
            with scope_model_override(effective_plugin, _scope_model):
                result = await effective_plugin.handle_incoming_message(
                    bot, message, prompt_for_engine
                )
        try:
            _log_llm_traffic(prompt, result, interface)
        except Exception as e:
            log_error(f"[plugin_instance] Failed to log LLM traffic: {e}")
        # debug log response for full transaction replay
        try:
            redacted_result = redact_multimodal_for_logging(result)
            if isinstance(redacted_result, (dict, list)):
                log_debug("[flow] LLM raw response: " + json_dumps(redacted_result))
            else:
                log_debug(f"[flow] LLM raw response: {redacted_result}")
        except Exception:
            pass

        # Update Grillo activity log if this was a Grillo beat
        # This ensures the raw LLM response is persisted even if actions fail or don't write back
        try:
            activity_log_id = None
            if isinstance(context_memory_or_prompt, dict):
                activity_log_id = context_memory_or_prompt.get("activity_log_id")

            if activity_log_id is not None:
                response_metadata = getattr(
                    effective_plugin,
                    "_last_response_metadata",
                    None,
                )
                if not result:
                    log_warning(
                        "[plugin_instance] Empty LLM response for Grillo activity "
                        f"{activity_log_id}; persisting diagnostic marker"
                    )
                await _update_grillo_response(
                    activity_log_id,
                    result,
                    response_metadata=response_metadata,
                    engine_label=_get_grillo_engine_label(effective_plugin),
                )
        except Exception as e:
            log_warning(f"[plugin_instance] Failed to update Grillo log: {e}")
        # Log that plugin finished processing
        try:
            log_info(
                f"[flow] <- LLM plugin: completed for chat_id={getattr(message, 'chat_id', None)} result_type={type(result)}"
            )
        except Exception:
            log_info(
                f"[flow] <- LLM plugin: completed for chat_id={getattr(message, 'chat_id', None)}"
            )

        # If the LLM plugin returned a response, pass it through the message chain for validation/correction
        # This ensures ALL LLM responses go through proper JSON validation before being sent to interfaces
        if result:
            log_info(
                f"[plugin_instance] 📥 LLM→INTERFACE: LLM returned response ({len(str(result)) if result else 0} chars), passing to message chain for llm_to_interface validation/correction"
            )
            log_info(
                "[plugin_instance] 🔄 BEFORE IMPORT: about to import message_chain_handle"
            )
            from core.message_chain import (
                handle_incoming_message as message_chain_handle,
            )

            log_info(
                "[plugin_instance] ✓ AFTER IMPORT: message_chain_handle imported successfully"
            )

            # Build context from the original message and context_memory if available
            llm_context = {}

            # First, inherit from the original context_memory (which has interface_path from message_queue)
            if isinstance(context_memory_or_prompt, dict):
                llm_context.update(context_memory_or_prompt)

            # Then add message attributes (these take precedence if they exist)
            if hasattr(message, "chat_id") and message.chat_id:
                llm_context["chat_id"] = message.chat_id
            if hasattr(message, "interface_path") and message.interface_path:
                llm_context["interface_path"] = message.interface_path

            # Preserve original user message for corrector (scope-safe)
            try:
                if isinstance(prompt, dict):
                    llm_context["original_user_message"] = (
                        prompt.get("input", {}).get("payload", {}).get("text", "")
                    )
                else:
                    llm_context["original_user_message"] = (
                        getattr(message, "text", "") or ""
                    )
            except Exception:
                llm_context["original_user_message"] = (
                    getattr(message, "text", "") or ""
                )

            # Scope-aware correction: only allow actions that were present in the prompt
            try:
                if isinstance(prompt, dict) and isinstance(prompt.get("actions"), dict):
                    llm_context["allowed_action_types"] = list(prompt["actions"].keys())
                elif isinstance(prompt, dict) and prompt.get("allowed_action_types"):
                    # Delivery / scoped turns carry an explicit allowlist (e.g.
                    # message_* only, set by auto_response.py) so the corrector and
                    # the leaked-action filter in message_chain stay in scope. This
                    # is the structural search-loop fix (2026-08-17): the delivery
                    # LLM must never re-emit the producing action.
                    llm_context["allowed_action_types"] = list(
                        prompt["allowed_action_types"]
                    )
                else:
                    llm_context["allowed_action_types"] = None
            except Exception:
                llm_context["allowed_action_types"] = None

            # Propagate the delivery system_message (when present) into the
            # corrector context so message_chain honours its metadata (e.g.
            # max_correction_attempts / is_action_result_delivery). For a
            # delivery turn context_memory_or_prompt is the enqueued JSON string,
            # so the update above (which only inherits from a dict context) never
            # carried it — copy it here so the delivery corrector stays bounded.
            try:
                if isinstance(prompt, dict) and isinstance(
                    prompt.get("system_message"), dict
                ):
                    llm_context.setdefault("system_message", prompt["system_message"])
            except Exception:
                pass

            # Explicitly tag the action scope for the corrector
            llm_context["action_scope"] = "main"

            # Ensure action_parser/plugins can reliably detect the originating interface
            if interface:
                llm_context["interface"] = interface
                # If interface_path is still not set, build it from interface + chat_id
                if not llm_context.get("interface_path"):
                    chat_id = llm_context.get("chat_id") or getattr(
                        message, "chat_id", None
                    )
                    if chat_id:
                        llm_context["interface_path"] = f"{interface}/{chat_id}"
                        log_debug(
                            f"[plugin_instance] Built interface_path from interface+chat_id: {llm_context['interface_path']}"
                        )

            # Pass to message chain with source="llm" to mark it as LLM-origin
            # The message chain will validate JSON, auto-correct if needed, and execute actions
            # This implements the llm_to_interface transport layer standard with corrector middleware
            log_info(
                "[plugin_instance] 📥 LLM→INTERFACE: Entering message_chain with source=llm (will use llm_to_interface transport)"
            )
            log_info(
                "[plugin_instance] ⏳ About to await message_chain_handle (this may freeze)"
            )
            chain_result = None
            try:
                chain_result = await message_chain_handle(
                    bot=bot,
                    message=message,
                    text=result,
                    source="llm",  # Mark as LLM-origin so corrector can intervene (llm_to_interface standard)
                    context=llm_context,
                )
                log_info("[plugin_instance] ✅ message_chain_handle completed")
            except asyncio.TimeoutError:
                log_error(
                    "[plugin_instance] ❌ message_chain_handle TIMEOUT (deadlock suspected)"
                )
                raise
            except Exception as e:
                log_error(
                    f"[plugin_instance] ❌ message_chain_handle raised exception: {e}"
                )
            log_debug(
                f"[plugin_instance] Message chain processed LLM response: {chain_result}"
            )
            log_info(
                f"[plugin_instance] ✅ LLM→INTERFACE: message_chain completed, result={chain_result}"
            )

            # Debrief (postflight) for LLM responses without action execution.
            # If actions were executed, Debrief already ran inside action_parser.
            try:
                if isinstance(llm_context, dict) and not llm_context.get("debrief_ran"):
                    if chain_result != "ACTIONS_EXECUTED":
                        from core.debrief import run_debrief

                        llm_context["llm_response_text"] = (
                            str(result) if result is not None else ""
                        )
                        await run_debrief(
                            processed_actions=[],
                            failed_actions=[],
                            results={
                                "chain_result": chain_result,
                                "llm_response_text": llm_context.get(
                                    "llm_response_text"
                                ),
                            },
                            context=llm_context,
                            original_message=message,
                        )
            except Exception as e:
                log_debug(f"[plugin_instance] Debrief-after-chain failed: {e}")
            # Don't return ACTIONS_EXECUTED as a message to the webui
            if chain_result == "ACTIONS_EXECUTED":
                return None

            return chain_result

        return result
    except Exception as e:
        log_error(f"[plugin_instance] LLM plugin raised an exception: {e}")
        raise
    finally:
        if should_restore_plugin and original_plugin_name:
            try:
                await load_plugin(original_plugin_name, ensure_started=True)
                log_info(
                    f"[plugin_instance] Restored cortex after trainer message: {original_plugin_name}"
                )
            except Exception as e:
                log_warning(
                    f"[plugin_instance] Failed to restore cortex via name '{original_plugin_name}': {e}"
                    " — restoring saved instance directly"
                )
                # Restore the saved instance directly; the registry name was wrong
                # (e.g. bridge engines register under a key different from their module).
                if original_plugin_instance is not None:
                    _restore_plugin_instance(original_plugin_instance)


def get_supported_models():
    if plugin and hasattr(plugin, "get_supported_models"):
        return plugin.get_supported_models()
    return []


def get_current_model():
    if plugin and hasattr(plugin, "get_current_model"):
        try:
            return plugin.get_current_model()
        except Exception:
            pass
    try:
        from core.config import get_current_model as _get_current_model

        return _get_current_model()
    except Exception:
        return None


def set_current_model(model: str) -> None:
    from core.config import set_current_model as _set_current_model

    _set_current_model(model)
    if plugin and hasattr(plugin, "set_current_model"):
        try:
            plugin.set_current_model(model)
        except Exception:
            pass


async def _scope_delivery_prompt_request(
    prompt: object,
    interface_name: str | None,
    interface_path: str | None,
) -> object | None:
    """If ``prompt`` is a delivery-turn dict, build a message_*-only PromptRequest.

    Delivery turns are enqueued as a JSON string (``{system_message,
    allowed_action_types}``) and parsed to a dict that BYPASSES
    ``build_prompt_request``, so the action catalog was never trimmed for the
    weak model and it re-emitted the producing action (search-loop fix,
    2026-08-17). Rebuilding the delivery turn as a ``PromptRequest`` whose
    ``tool_declarations`` are ``message_*`` only hides the producing action
    from the model. Returns ``None`` for any non-delivery turn or on any error
    (fail-closed to the legacy path).
    """
    if not isinstance(prompt, dict):
        return None
    _prompt_dict = cast(dict[str, Any], prompt)
    try:
        _sm = _prompt_dict.get("system_message")
        if not (
            isinstance(_sm, dict)
            and _sm.get("is_action_result_delivery") is True
            and _sm.get("action_type")
            and _sm.get("action_outputs") is not None
        ):
            return None
        from core.prompt_engine import build_delivery_request

        _pr = await build_delivery_request(
            str(_sm.get("action_type")),
            _sm.get("action_outputs") or [],
            interface_name,
            interface_path,
        )
        log_info(
            f"[plugin_instance] Delivery turn '{_sm.get('action_type')}' "
            f"scoped to message_* via PromptRequest"
        )
        return _pr
    except Exception as _de:
        log_warning(f"[plugin_instance] Delivery PromptRequest scoping skipped: {_de}")
        return None


def _log_llm_traffic(prompt, response, interface_name):
    """Log raw LLM traffic to a JSONL file (optional)."""
    try:
        enabled = config_registry.get_value(
            "LOG_LLM_TRAFFIC_ENABLED",
            False,
            value_type=bool,
            group="logging",
            component="core",
        )
        if not enabled:
            return
        log_path = config_registry.get_value(
            "LOG_LLM_TRAFFIC_PATH",
            "logs/llm_traffic.jsonl",
            value_type=str,
            group="logging",
            component="core",
        )
        redact_actions = config_registry.get_value(
            "LOG_LLM_TRAFFIC_REDACT_ACTIONS",
            True,
            value_type=bool,
            group="logging",
            component="core",
        )
    except Exception:
        return

    log_prompt = prompt
    if redact_actions and isinstance(prompt, dict):
        try:
            log_prompt = prompt.copy()
            log_prompt.pop("actions", None)
        except Exception:
            pass
    try:
        log_prompt = redact_multimodal_for_logging(log_prompt)
        log_response = redact_multimodal_for_logging(response)
    except Exception:
        log_response = response

    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "interface": interface_name,
        "input_context": log_prompt,
        "response": log_response,
    }

    try:
        log_dir = os.path.dirname(os.path.abspath(log_path)) or "logs"
        os.makedirs(log_dir, exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, indent=2, default=str) + "\n\n")
        log_debug(f"[plugin_instance] 📝 Logged LLM traffic to {log_path}")
    except Exception as e:
        log_error(f"[plugin_instance] Failed to write to traffic log: {e}")


def get_target(message_id):
    if plugin and hasattr(plugin, "get_target"):
        return plugin.get_target(message_id)
    return None


def get_plugin():
    return plugin


def load_generic_plugin(name: str, notify_fn=None):
    global plugin

    # 🔁 Se il plugin è già caricato, verifica se è lo stesso
    if plugin is not None:
        current_plugin_name = plugin.__class__.__module__.split(".")[-1]
        if current_plugin_name == name:
            log_debug(f"[plugin] ⚠️ Plugin già caricato: {plugin.__class__.__name__}")
            return

    try:
        import importlib

        module = importlib.import_module(f"plugins.{name}_plugin")
        log_debug(f"[plugin] Modulo plugins.{name}_plugin importato con successo.")
    except ModuleNotFoundError as e:
        log_error(f"[plugin] ❌ Impossibile importare plugins.{name}_plugin: {e}", e)
        raise ValueError(f"Plugin non valido: {name}")

    if not hasattr(module, "PLUGIN_CLASS"):
        raise ValueError(f"Il plugin `{name}` non definisce `PLUGIN_CLASS`.")

    plugin_class = getattr(module, "PLUGIN_CLASS")

    try:
        plugin = plugin_class(notify_fn=notify_fn) if notify_fn else plugin_class()
        log_debug(f"[plugin] Plugin inizializzato: {plugin.__class__.__name__}")
    except Exception as e:
        log_error(f"[plugin] ❌ Errore durante l'inizializzazione del plugin: {e}", e)
        raise

    if hasattr(plugin, "start"):
        try:
            if asyncio.iscoroutinefunction(plugin.start):
                loop = asyncio.get_running_loop()
                if loop and loop.is_running():
                    loop.create_task(plugin.start())
                    log_debug("[plugin] Plugin avviato nel loop esistente.")
                else:
                    log_debug(
                        "[plugin] Nessun loop in esecuzione; il plugin sarà avviato successivamente."
                    )
            else:
                plugin.start()
                log_debug("[plugin] Plugin avviato.")
        except Exception as e:
            log_error(f"[plugin] ❌ Errore durante l'avvio del plugin: {e}", e)


async def _extract_image_data_from_message(message, interface_name: str):
    """Extract image data from a message if it contains images."""
    if not message:
        return None, None

    image_data = None
    has_trigger = False

    # Check for photo attachments (generic interface)
    if hasattr(message, "photo") and message.photo:
        # DEBUG: Log the photo object BEFORE any processing
        log_debug(
            f"[plugin_instance] message.photo type BEFORE processing: {type(message.photo)}"
        )
        log_debug(
            f"[plugin_instance] message.photo value BEFORE processing: {message.photo}"
        )
        log_debug(
            f"[plugin_instance] message.photo is list: {isinstance(message.photo, list)}"
        )
        log_debug(
            f"[plugin_instance] message.photo is tuple: {isinstance(message.photo, tuple)}"
        )

        # Handle list/tuple of photos (multiple resolutions)
        if isinstance(message.photo, (list, tuple)):
            photo = message.photo[-1]  # Last element is typically highest resolution
        else:
            photo = message.photo

        # Debug: Log photo object type and attributes
        log_debug(f"[plugin_instance] Photo object type: {type(photo)}")
        log_debug(f"[plugin_instance] Photo object attributes: {dir(photo)}")
        log_debug(f"[plugin_instance] Photo file_id: {getattr(photo, 'file_id', None)}")
        log_debug(
            f"[plugin_instance] Photo file_unique_id: {getattr(photo, 'file_unique_id', None)}"
        )

        image_data = {
            "type": "photo",
            "file_id": getattr(photo, "file_id", None),
            "file_unique_id": getattr(photo, "file_unique_id", None),
            "width": getattr(photo, "width", 0),
            "height": getattr(photo, "height", 0),
            "file_size": getattr(photo, "file_size", 0),
            "caption": getattr(message, "caption", ""),
            "mime_type": getattr(photo, "mime_type", "image/jpeg"),  # Default to JPEG
        }
        log_info(f"[plugin_instance] Extracted image_data: {image_data}")
        has_trigger = True  # Photos are always considered as having trigger for now

    elif hasattr(message, "document") and message.document:
        # Check if document is an image
        mime_type = getattr(message.document, "mime_type", "")
        if mime_type and mime_type.startswith("image/"):
            image_data = {
                "type": "document",
                "file_id": message.document.file_id,
                "file_unique_id": message.document.file_unique_id,
                "file_name": getattr(message.document, "file_name", ""),
                "mime_type": mime_type,
                "file_size": getattr(message.document, "file_size", 0),
                "caption": getattr(message, "caption", ""),
            }
            has_trigger = True  # Documents with images are considered as having trigger

    # Check for attachment-based interfaces
    elif hasattr(message, "attachments"):
        # Handle generic attachments
        for attachment in message.attachments:
            mime_type = ""
            filename = ""
            size = 0
            url = None
            if isinstance(attachment, dict):
                mime_type = str(
                    attachment.get("content_type")
                    or attachment.get("mime_type")
                    or attachment.get("type")
                    or ""
                )
                filename = attachment.get("filename") or attachment.get("name") or ""
                size = attachment.get("size", 0)
                url = attachment.get("url") or attachment.get("path")
            elif hasattr(attachment, "content_type") and attachment.content_type:
                mime_type = attachment.content_type
                filename = getattr(attachment, "filename", "") or getattr(
                    attachment, "name", ""
                )
                size = getattr(attachment, "size", 0)
                url = getattr(attachment, "url", None) or getattr(
                    attachment, "path", None
                )
            else:
                continue

            if not mime_type and isinstance(attachment, dict):
                mime_type = get_mime_type(attachment.get("path"), filename)

            if mime_type.startswith("image/"):
                image_data = {
                    "type": "attachment",
                    "url": url,
                    "filename": filename,
                    "content_type": mime_type,
                    "size": size,
                    "caption": getattr(message, "caption", "")
                    or getattr(message, "text", "")
                    or "",
                }
                has_trigger = True
                break

    return image_data, has_trigger


def _build_unviewable_media_placeholder(
    mime_type: str | None,
    reason: str = "unavailable",
    is_sticker: bool = False,
) -> str:
    """Build a text placeholder informing the Cortex that media was received but not analysed.

    Args:
        mime_type: MIME type of the attachment (e.g. ``"image/png"``).
        reason:    Why the vision analysis could not run.
                   Typical values: ``"disabled"``, ``"unavailable"``, ``"error"``.
        is_sticker: Whether the attachment is a sticker.
    """
    if is_sticker:
        return (
            "The user sent a sticker that could not be analysed. "
            "Acknowledge that a sticker was shared and that you cannot "
            "see its content right now."
        )
    media_label = mime_type or "media file"
    return (
        f"The user sent a {media_label} attachment. "
        f"The Iris vision subsystem is currently {reason}, so the image content "
        "cannot be analysed. Acknowledge the attachment and let the user know "
        "that vision capabilities are not available right now."
    )


def _get_active_iris_engine() -> str:
    """Return the configured active Iris engine name.

    May be a registered engine name, ``"disabled"`` (vision off), or the
    hardcoded ``"inline"`` pseudo-engine (forward image/video bytes straight to
    the Cortex engine instead of producing a textual description).  Falls back to
    ``"disabled"`` on any error.
    """
    try:
        from core.core_initializer import PLUGIN_REGISTRY

        iris = PLUGIN_REGISTRY.get("iris_plugin")
        if iris is not None:
            try:
                iris.refresh_config()
            except Exception:
                pass
            return str(getattr(iris, "_active_engine_name", "disabled") or "disabled")
    except Exception:
        pass
    try:
        from core.config_manager import config_registry

        return str(
            config_registry.get_value(
                "ACTIVE_IRIS_ENGINE",
                "disabled",
                value_type=str,
                group="plugins",
                component="iris_plugin",
            )
            or "disabled"
        )
    except Exception:
        return "disabled"


def _get_active_auris_engine() -> str:
    """Return the configured active Auris (STT) engine name.

    May be a registered engine name, ``"disabled"`` (STT off), or the hardcoded
    ``"inline"`` pseudo-engine (forward audio bytes straight to the Cortex engine
    instead of transcribing).  Falls back to ``"disabled"`` on any error.
    """
    try:
        from core.core_initializer import PLUGIN_REGISTRY

        auris = PLUGIN_REGISTRY.get("auris_plugin")
        if auris is not None:
            try:
                auris.refresh_config()
            except Exception:
                pass
            return str(getattr(auris, "_active_engine_name", "disabled") or "disabled")
    except Exception:
        pass
    try:
        from core.config_manager import config_registry

        return str(
            config_registry.get_value(
                "ACTIVE_AURIS_ENGINE",
                "disabled",
                value_type=str,
                group="plugins",
                component="auris_plugin",
            )
            or "disabled"
        )
    except Exception:
        return "disabled"


async def _describe_attachment_images_with_iris(
    attachments: list[dict],
    prompt: str | None = None,
) -> "IrisResult | None":  # noqa: F821
    """Use the configured Iris engine to describe the first image/video attachment.

    Returns the full :class:`IrisResult` (including *language* and *confidence*
    metadata) rather than a bare description string.  When the engine is
    disabled or the analysis fails, a synthetic ``IrisResult`` is returned
    whose *description* contains an informative placeholder for the Cortex.
    """
    from plugins.iris_base import IrisResult

    if not attachments:
        return None

    first_media_mime_type = None
    first_media_is_sticker = False
    for attachment in attachments:
        mime_type = str(
            attachment.get("mime_type")
            or attachment.get("content_type")
            or attachment.get("type")
            or ""
        )
        if mime_type.startswith(("image/", "video/")):
            first_media_mime_type = mime_type
            first_media_is_sticker = bool(attachment.get("is_sticker"))
            break

    if first_media_mime_type is None:
        return None

    try:
        from core.core_initializer import PLUGIN_REGISTRY

        iris = PLUGIN_REGISTRY.get("iris_plugin")
        if iris is None:
            log_info(
                "[plugin_instance] Iris skip: iris_plugin not found in PLUGIN_REGISTRY"
            )
            return IrisResult(
                description=_build_unviewable_media_placeholder(
                    first_media_mime_type,
                    reason="unavailable",
                    is_sticker=first_media_is_sticker,
                ),
                is_sticker=first_media_is_sticker,
            )
        # Refresh config before reading _active_engine_name so we get the
        # DB-loaded value rather than the hard-coded startup default ("disabled").
        try:
            iris.refresh_config()
        except Exception:
            pass
        active_engine = getattr(iris, "_active_engine_name", "disabled")
        if active_engine in ("disabled", "inline"):
            # 'inline' is handled upstream (images flow to the Cortex engine
            # untouched); reaching here means there is no description engine.
            log_info(f"[plugin_instance] Iris skip: active engine is '{active_engine}'")
            return IrisResult(
                description=_build_unviewable_media_placeholder(
                    first_media_mime_type,
                    reason="disabled",
                    is_sticker=first_media_is_sticker,
                ),
                is_sticker=first_media_is_sticker,
            )
        log_info(
            f"[plugin_instance] Iris active engine: '{active_engine}', processing {len(attachments)} attachment(s)"
        )
    except Exception as exc:
        log_debug(f"[plugin_instance] Iris plugin lookup failed: {exc}")
        return IrisResult(
            description=_build_unviewable_media_placeholder(
                first_media_mime_type,
                reason="unavailable",
                is_sticker=first_media_is_sticker,
            ),
            is_sticker=first_media_is_sticker,
        )

    for attachment in attachments:
        mime_type = str(
            attachment.get("mime_type")
            or attachment.get("content_type")
            or attachment.get("type")
            or ""
        )
        if not mime_type.startswith(("image/", "video/")):
            log_debug(
                f"[plugin_instance] Iris skip attachment: mime_type={mime_type!r} not image/video"
            )
            continue

        data_b64 = attachment.get("data")
        if not data_b64 or not isinstance(data_b64, str):
            # Try to read from path if data is missing
            file_path = attachment.get("path") or attachment.get("file_path")
            if file_path:
                try:
                    p = Path(file_path)
                    if p.exists() and p.is_file():
                        data_b64 = base64.b64encode(p.read_bytes()).decode("utf-8")
                        log_info(
                            f"[plugin_instance] Iris: read file from path for attachment ({mime_type})"
                        )
                    else:
                        log_warning(
                            f"[plugin_instance] Iris skip: no data and file not found at {file_path!r}"
                        )
                        continue
                except Exception as exc:
                    log_warning(
                        f"[plugin_instance] Iris skip: failed to read {file_path!r}: {exc}"
                    )
                    continue
            else:
                log_warning(
                    f"[plugin_instance] Iris skip: attachment has no data and no path (mime={mime_type!r})"
                )
                continue

        try:
            image_bytes = base64.b64decode(data_b64)
        except Exception as exc:
            log_warning(
                f"[plugin_instance] Failed to decode attachment data for Iris: {exc}"
            )
            continue

        if not image_bytes:
            continue

        # Persist the media into the Iris durable cache instead of an
        # immediately-deleted temp file, so the synth can re-inspect it on
        # later turns via the vision_describe action.  Cache management (naming,
        # TTL pruning) lives at the Iris subsystem level.
        cached_path = None
        try:
            cached_path = iris.cache_media_bytes(image_bytes, mime_type)
        except Exception as exc:
            log_warning(f"[plugin_instance] Iris cache write failed: {exc}")

        analysis_path = cached_path
        fallback_tmp = None
        try:
            if analysis_path is None:
                suffix = ""
                if "/" in mime_type:
                    suffix = f".{mime_type.split('/', 1)[1].split(';')[0]}"
                with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                    tmp.write(image_bytes)
                    fallback_tmp = tmp.name
                analysis_path = fallback_tmp

            log_info(
                f"[plugin_instance] Iris: calling describe_media for {mime_type} ({len(image_bytes)} bytes)"
            )
            attachment_is_sticker = bool(attachment.get("is_sticker"))
            effective_prompt = prompt
            if attachment_is_sticker:
                sticker_hint = "\n\nThis is a sticker image. Describe what is depicted in the sticker."
                base_prompt = prompt or getattr(iris, "_default_prompt", "") or ""
                effective_prompt = base_prompt + sticker_hint
            try:
                result = await asyncio.wait_for(
                    iris.describe_media(analysis_path, mime_type, effective_prompt),
                    timeout=120.0,
                )
            except asyncio.TimeoutError:
                log_warning(
                    f"[plugin_instance] Iris: describe_media timed out after 120s for {mime_type}"
                )
                result = None
            if result and result.description:
                # Record the durable cache location on the result so callers can
                # persist it into chat-history metadata for re-inspection.
                result.cached_path = cached_path
                result.is_sticker = attachment_is_sticker
                log_info(
                    f"[plugin_instance] Iris: got description ({len(result.description)} chars)"
                )
                return result
            log_info(
                f"[plugin_instance] Iris: describe_media returned empty result={result!r}"
            )
        except Exception as exc:
            log_warning(f"[plugin_instance] Iris description failed: {exc}")
        finally:
            # Only the transient fallback temp file is removed; the durable
            # cache copy is intentionally retained (pruned by TTL).
            if fallback_tmp:
                try:
                    os.remove(fallback_tmp)
                except Exception:
                    pass

    return IrisResult(
        description=_build_unviewable_media_placeholder(
            first_media_mime_type, reason="error", is_sticker=first_media_is_sticker
        ),
        is_sticker=first_media_is_sticker,
    )


def _inject_text_attachment_content(message: Any, attachments: list) -> bool:
    """Append bounded content of inbound ``text/*`` attachments to ``message.text``.

    The multimodal extractor keeps document bytes as base64 in memory and only
    the Agent Lane can read them from disk — the full-context Fast Lane model
    never saw attached documents, so "read this md and tell me what you think"
    was escalated to the Agent Lane (Langfuse 2a09c706-era: recon judged the
    request agentic because the main model had no way to read the file).
    Inlining the content (bounded) into the current turn lets the main model
    answer directly. Images/videos are handled by Iris, audio by Auris; only
    ``text/*`` is inlined here. Language-neutral framing, no keyword logic.
    Fail-safe: any decode error skips that attachment; never raises.

    Returns True when content was injected.
    """
    try:
        blocks: list[str] = []
        for att in attachments or []:
            if not isinstance(att, dict):
                continue
            mime = str(att.get("mime_type") or att.get("content_type") or "")
            if not mime.startswith("text/"):
                continue
            data_b64 = att.get("data")
            if not isinstance(data_b64, str) or not data_b64.strip():
                continue
            try:
                text_content = base64.b64decode(data_b64).decode(
                    "utf-8", errors="replace"
                )
            except Exception:
                continue
            if not text_content.strip():
                continue
            name = str(att.get("filename") or "attachment").strip()
            blocks.append(f"[Attachment: {name}]\n{text_content}")
        if not blocks:
            return False
        joined = "\n\n".join(blocks)[:_TEXT_ATTACHMENT_MAX_CHARS]
        original = getattr(message, "text", "") or ""
        setattr(message, "text", f"{original}\n\n{joined}" if original else joined)
        log_info(
            f"[plugin_instance] Injected {len(joined)} chars of text "
            "attachment content into the current turn"
        )
        return True
    except Exception as exc:
        log_warning(f"[plugin_instance] Text attachment injection failed: {exc}")
        return False


def _persist_attachments_to_sandbox(attachments: list[dict]) -> list[str]:
    """Persist non-visual attachment bytes onto an agent-sandbox-readable path.

    The multimodal extractor keeps document/audio bytes base64-in-memory only,
    so a downstream tool call (``agent_read_file``, ``stt_transcribe``) receives
    a bare filename that does not exist on disk — observed live as "File not
    found: Untitled document.pdf" (Langfuse 11feca6f). This helper writes those
    bytes under the first configured agent filesystem root (``AGENT_FS_ROOTS`` /
    ``AGENT_FS_ROOT``, falling back to ``/app``) so the Agent Lane and Auris can
    resolve the real path. Images/videos are intentionally skipped: they flow
    through Iris/inline vision instead. Fail-safe: any error skips that
    attachment and never raises.

    Args:
        attachments: Extracted attachment dicts (``mime_type``, ``data`` base64,
            optional ``filename``).

    Returns:
        List of persisted absolute paths (possibly empty).
    """
    import base64 as _b64
    import re as _re
    import time as _time

    roots_raw = os.getenv("AGENT_FS_ROOTS") or ""
    if roots_raw.strip():
        roots = [p.strip() for p in roots_raw.split(":") if p.strip()]
    else:
        roots = [os.getenv("AGENT_FS_ROOT", "/app")]
    if not roots:
        return []

    root = roots[0]
    try:
        sandbox_root = Path(root).resolve()
    except Exception:
        return []

    persisted: list[str] = []
    for att in attachments or []:
        try:
            if not isinstance(att, dict):
                continue
            mime = str(att.get("mime_type") or att.get("content_type") or "")
            # Only non-visual media benefits from a sandbox path.
            if mime.startswith(("image/", "video/")):
                continue
            data_b64 = att.get("data")
            if not isinstance(data_b64, str) or not data_b64.strip():
                continue
            raw_bytes = _b64.b64decode(data_b64)
            if not raw_bytes:
                continue

            filename = str(att.get("filename") or "").strip() or "attachment"
            # Sanitize: keep only safe filename characters (never a path).
            safe_name = _re.sub(r"[^A-Za-z0-9._-]+", "_", filename).strip("._")
            if not safe_name:
                safe_name = "attachment"
            target_dir = sandbox_root / "attachments"
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{int(_time.time() * 1000)}_{safe_name}"
            target.write_bytes(raw_bytes)
            att["path"] = str(target)
            persisted.append(str(target))
        except Exception as exc:
            log_warning(
                f"[plugin_instance] Failed to persist attachment to sandbox: {exc}"
            )
            continue
    return persisted


async def _extract_multimodal_attachments(
    bot, message, interface_name: str
) -> list[dict]:
    """Extract multimodal attachments (images, audio, documents) from a message.

    This function bridges interface-specific messages to the unified multimodal
    format expected by LLM engines like gemini_api.

    Args:
        bot: The bot instance (used for downloading files)
        message: The message object from the interface
        interface_name: The interface identifier (e.g., 'telegram_bot', 'discord_bot')

    Returns:
        List of attachment dicts with keys: {mime_type, data, filename}
    """
    try:
        image_processor = get_image_processor()

        if interface_name == "telegram_bot":
            return await extract_multimodal_from_telegram(bot, message, image_processor)
        elif interface_name == "discord_bot":
            return await extract_multimodal_from_discord(message, image_processor)
        else:
            # Generic fallback for other interfaces
            log_debug(
                f"[plugin_instance] No multimodal extractor for interface: {interface_name}"
            )

            attachments = []
            if hasattr(message, "attachments"):
                for attachment in getattr(message, "attachments") or []:
                    if not isinstance(attachment, dict):
                        continue

                    mime_type = str(
                        attachment.get("content_type")
                        or attachment.get("mime_type")
                        or ""
                    )
                    filename = (
                        attachment.get("filename") or attachment.get("name") or ""
                    )
                    if not mime_type:
                        mime_type = get_mime_type(attachment.get("path"), filename)
                    if not mime_type or not is_supported_type(mime_type):
                        continue

                    normalized = dict(attachment)
                    normalized["mime_type"] = mime_type

                    if "data" not in normalized and normalized.get("path"):
                        try:
                            path = Path(str(normalized["path"]))
                            if path.exists() and path.is_file():
                                normalized["data"] = base64.b64encode(
                                    path.read_bytes()
                                ).decode("utf-8")
                        except Exception as exc:
                            log_warning(
                                f"[plugin_instance] Failed to inline attachment data: {exc}"
                            )

                    attachments.append(normalized)
            return attachments
    except Exception as e:
        log_warning(f"[plugin_instance] Failed to extract multimodal attachments: {e}")
        return []


async def _update_grillo_response(
    activity_log_id: Any,
    response_text: Any,
    *,
    response_metadata: dict[str, Any] | None = None,
    engine_label: str | None = None,
) -> None:
    """Update the grillo_activity_log with the raw response text."""
    if not activity_log_id:
        return

    try:
        from core.db import _get_db_type, get_conn_ctx
        from plugins.grillo.grillo_response_recorder import (
            build_grillo_response_append_expression,
            extract_response_text_from_cortex_response,
        )

        normalized_response_text = await extract_response_text_from_cortex_response(
            response_text
        )
        if not normalized_response_text.strip():
            normalized_response_text = _build_empty_grillo_response_text(
                response_metadata,
                engine_label,
            )

        append_expression = build_grillo_response_append_expression(_get_db_type())

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                # Logic similar to GrilloPlugin.set_activity_response_text: append if exists
                # We use append because sometimes multiple messages/chunks might be associated
                await cur.execute(
                    f"""
                    UPDATE grillo_activity_log
                    SET response_text = {append_expression}
                    WHERE id=%s
                    """,
                    (
                        normalized_response_text,
                        normalized_response_text,
                        activity_log_id,
                    ),
                )
                await conn.commit()
        log_debug(
            "[plugin_instance] Updated grillo_activity_log "
            f"{activity_log_id} with response ({len(normalized_response_text)} chars)"
        )
    except Exception as e:
        log_error(f"[plugin_instance] Failed to update Grillo log: {e}")
