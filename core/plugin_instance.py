# core/plugin_instance.py

from core.prompt_engine import build_json_prompt
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
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.json_utils import dumps as json_dumps, sanitize_for_json
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

# Global lease to serialize LLM chains (Recon + prompt + actions) across tasks
_llm_chain_lock = asyncio.Lock()
_llm_chain_depth = contextvars.ContextVar("llm_chain_depth", default=0)

try:
    from core.variables_engine import register_exposed_var

    register_exposed_var(
        "LLM_CHAIN_LEASE_TIMEOUT_SEC",
        label="LLM chain lease timeout (s)",
        default=300,
        value_type=int,
        ui_type="number",
        description=(
            "Force-release the global LLM chain lease after this many seconds to "
            "avoid deadlocks. Set to 0 to disable."
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
                "LLM_CHAIN_LEASE_TIMEOUT_SEC", 300, value_type=int
            )
            or 300
        )
    except Exception:
        timeout = 300

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
    bot, message, context_memory_or_prompt, interface: str | None = None
):
    """Process incoming messages or pre-built prompts."""

    async with _llm_chain_lease():
        # Check if plugin is loaded
        if plugin is None:
            log_error(
                "[plugin_instance] No LLM plugin loaded! Cannot handle incoming message."
            )
            log_error(f"[plugin_instance] Available plugins: {dir()}")
            # Try to load manual plugin as fallback
            try:
                log_warning(
                    "[plugin_instance] Attempting to load manual plugin as fallback..."
                )
                await load_plugin("manual")
                if plugin is None:
                    raise ValueError("Manual plugin failed to load")
                log_info(
                    "[plugin_instance] Manual plugin loaded successfully as fallback"
                )
            except Exception as fallback_e:
                log_error(
                    f"[plugin_instance] Fallback plugin loading failed: {fallback_e}"
                )
                raise ValueError("No LLM plugin loaded and fallback failed")

        # Normalize message user/date fields to avoid AttributeErrors later
        try:
            from core.user_utils import ensure_message_user_fields

            ensure_message_user_fields(message)
        except Exception:
            pass

        original_plugin_name = (
            plugin.__class__.__module__.split(".")[-1] if plugin is not None else None
        )
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
        if _is_voice_input and _has_transcribed_text:
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
            # Do NOT pass the user's text as the Iris prompt — Iris must use
            # IRIS_DEFAULT_PROMPT (a neutral "describe this image" instruction).
            # The user's actual question is answered by the main LLM after the
            # Iris description is injected into the context.
            iris_description = await _describe_attachment_images_with_iris(
                attachments, prompt=None
            )
            if iris_description:
                try:
                    original_text = getattr(message, "text", "") or ""
                    description_block = f"[Iris image description: {iris_description}]"
                    if original_text:
                        setattr(
                            message, "text", f"{original_text}\n\n{description_block}"
                        )
                    else:
                        setattr(message, "text", description_block)
                    log_info(
                        "[plugin_instance] Appended Iris image description to prompt text"
                    )
                except Exception as exc:
                    log_warning(
                        f"[plugin_instance] Could not append Iris image description to message text: {exc}"
                    )

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
                prompt = await build_json_prompt(
                    message,
                    {},
                    interface_name,
                    image_data=processed_image_data,
                    attachments=attachments,
                    max_chars=max_chars,
                )
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
                f"[plugin_instance] Passing max_chars={max_chars} to build_json_prompt()"
            )
            prompt = await build_json_prompt(
                message,
                context_memory_or_prompt,
                interface_name,
                image_data=processed_image_data,
                attachments=attachments,
                max_chars=max_chars,
            )

    # Multimodal attachments already extracted and passed to build_json_prompt above
    if attachments:
        log_info(
            f"[plugin_instance] Processing {len(attachments)} multimodal attachment(s)"
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
                # Prefer explicit pre_reduction_size if provided by build_json_prompt
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
            log_debug(f"[flow] prompt content: {json_dumps(prompt)}")
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
    try:
        from core.config import derive_cortex_scope, get_active_cortex_engine

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

        active_engine_name = await get_active_cortex_engine(scope=_scope)
        reg = get_cortex_registry()
        resolved = reg.get_engine(active_engine_name)
        if resolved is None:
            resolved = reg.load_engine(active_engine_name)
        if resolved is not None and resolved is not plugin:
            effective_plugin = resolved
            log_info(
                f"[plugin_instance] Engine resolved from registry: '{active_engine_name}' "
                f"(scope={_scope!r})"
            )
    except Exception as scope_exc:
        log_warning(
            f"[plugin_instance] Scope routing failed, falling back to global plugin: {scope_exc}"
        )

    try:
        if effective_plugin is None:
            log_error("[plugin_instance] No LLM plugin loaded, cannot process message")
            raise ValueError("No LLM plugin loaded")

        result = await effective_plugin.handle_incoming_message(bot, message, prompt)
        try:
            _log_llm_traffic(prompt, result, interface)
        except Exception as e:
            log_error(f"[plugin_instance] Failed to log LLM traffic: {e}")
        # debug log response for full transaction replay
        try:
            log_debug(f"[flow] LLM raw response: {result}")
        except Exception:
            pass

        # Update Grillo activity log if this was a Grillo beat
        # This ensures the raw LLM response is persisted even if actions fail or don't write back
        try:
            activity_log_id = None
            if isinstance(context_memory_or_prompt, dict):
                activity_log_id = context_memory_or_prompt.get("activity_log_id")

            if activity_log_id and result:
                await _update_grillo_response(activity_log_id, result)
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
                else:
                    llm_context["allowed_action_types"] = None
            except Exception:
                llm_context["allowed_action_types"] = None

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
                    f"[plugin_instance] Failed to restore cortex {original_plugin_name}: {e}"
                )


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

    entry = {
        "timestamp": datetime.utcnow().isoformat(),
        "interface": interface_name,
        "input_context": log_prompt,
        "response": response,
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


def _build_unviewable_media_placeholder(mime_type: str | None) -> str:
    if mime_type:
        return (
            f"User attached a {mime_type} but the vision engine could not see it."
        )
    return "User attached a media file but the vision engine could not see it."


async def _describe_attachment_images_with_iris(
    attachments: list[dict],
    prompt: str | None = None,
) -> str | None:
    """Use the configured Iris engine to describe the first image/video attachment."""
    if not attachments:
        return None

    first_media_mime_type = None
    for attachment in attachments:
        mime_type = str(
            attachment.get("mime_type")
            or attachment.get("content_type")
            or attachment.get("type")
            or ""
        )
        if mime_type.startswith(("image/", "video/")):
            first_media_mime_type = mime_type
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
            return _build_unviewable_media_placeholder(first_media_mime_type)
        # Refresh config before reading _active_engine_name so we get the
        # DB-loaded value rather than the hard-coded startup default ("disabled").
        try:
            iris.refresh_config()
        except Exception:
            pass
        active_engine = getattr(iris, "_active_engine_name", "disabled")
        if active_engine == "disabled":
            log_info("[plugin_instance] Iris skip: active engine is 'disabled'")
            return _build_unviewable_media_placeholder(first_media_mime_type)
        log_info(
            f"[plugin_instance] Iris active engine: '{active_engine}', processing {len(attachments)} attachment(s)"
        )
    except Exception as exc:
        log_debug(f"[plugin_instance] Iris plugin lookup failed: {exc}")
        return _build_unviewable_media_placeholder(first_media_mime_type)

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

        suffix = ""
        if "/" in mime_type:
            suffix = f".{mime_type.split('/', 1)[1].split(';')[0]}"

        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
                tmp.write(image_bytes)
                tmp_path = tmp.name

            log_info(
                f"[plugin_instance] Iris: calling describe_media for {mime_type} ({len(image_bytes)} bytes)"
            )
            result = await iris.describe_media(tmp_path, mime_type, prompt)
            if result and result.description:
                log_info(
                    f"[plugin_instance] Iris: got description ({len(result.description)} chars)"
                )
                return result.description
            log_info(
                f"[plugin_instance] Iris: describe_media returned empty result={result!r}"
            )
        except Exception as exc:
            log_warning(f"[plugin_instance] Iris description failed: {exc}")
        finally:
            if tmp_path:
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    return _build_unviewable_media_placeholder(first_media_mime_type)


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


async def _update_grillo_response(activity_log_id, response_text):
    """Update the grillo_activity_log with the raw response text."""
    if not activity_log_id or not response_text:
        return

    try:
        from core.db import get_conn_ctx

        async with get_conn_ctx() as conn:
            async with conn.cursor() as cur:
                # Logic similar to GrilloPlugin.set_activity_response_text: append if exists
                # We use append because sometimes multiple messages/chunks might be associated
                await cur.execute(
                    """
                    UPDATE grillo_activity_log
                    SET response_text = CASE
                        WHEN response_text IS NULL OR response_text = '' THEN %s
                        ELSE CONCAT(response_text, '\n\n', %s)
                    END
                    WHERE id=%s
                    """,
                    (response_text, response_text, activity_log_id),
                )
                await conn.commit()
        log_debug(
            f"[plugin_instance] Updated grillo_activity_log {activity_log_id} with response ({len(response_text)} chars)"
        )
    except Exception as e:
        log_error(f"[plugin_instance] Failed to update Grillo log: {e}")
