# core/plugin_instance.py

from core.config import get_active_llm, set_active_llm
from core.prompt_engine import build_json_prompt
from core.llm_registry import get_llm_registry
import asyncio
from types import SimpleNamespace
from datetime import datetime
from core.logging_utils import log_debug, log_info, log_warning, log_error
from core.action_parser import parse_action
from core.json_utils import dumps as json_dumps, sanitize_for_json
from core.image_processor import get_image_processor, process_image_message
from core.abstract_context import AbstractContext, AbstractUser, AbstractMessage
from core.mention_utils import is_message_for_bot

# Plugin managed centrally in initialize_core_components
plugin = None

async def load_plugin(name: str, notify_fn=None):
    global plugin

    # 🔁 If already loaded but different, replace it or update notify_fn
    if plugin is not None:
        current_plugin_name = plugin.__class__.__module__.split(".")[-1]
        if current_plugin_name != name:
            log_debug(f"[plugin] 🔄 Changing plugin from {current_plugin_name} to {name}")
            # Wait for any ongoing response to complete before cleanup
            if hasattr(plugin, '_worker_task') and plugin._worker_task and not plugin._worker_task.done():
                log_debug(f"[plugin] ⏳ Waiting for ongoing response to complete in {current_plugin_name}")
                try:
                    # Use a reasonable timeout for plugin switching (30 seconds), not the full LLM response timeout
                    await asyncio.wait_for(plugin._worker_task, timeout=30.0)
                    log_debug(f"[plugin] ✅ Ongoing response completed in {current_plugin_name}")
                except asyncio.TimeoutError:
                    log_warning(f"[plugin] ⏰ Timeout waiting for response completion in {current_plugin_name}, cancelling task")
                    plugin._worker_task.cancel()
                    try:
                        await asyncio.wait_for(plugin._worker_task, timeout=5.0)
                    except (asyncio.TimeoutError, Exception):
                        pass  # Task cancelled or already done
                except Exception as e:
                    log_warning(f"[plugin] ⚠️ Error waiting for response completion: {e}")
            # Cleanup the previous plugin before loading the new one
            if hasattr(plugin, 'cleanup'):
                try:
                    plugin.cleanup()
                    log_debug(f"[plugin] ✅ Previous plugin {current_plugin_name} cleaned up")
                except Exception as e:
                    log_error(f"[plugin] ❌ Error cleaning up previous plugin {current_plugin_name}: {e}")
            elif hasattr(plugin, 'stop'):
                try:
                    if asyncio.iscoroutinefunction(plugin.stop):
                        await plugin.stop()
                    else:
                        plugin.stop()
                    log_debug(f"[plugin] ✅ Previous plugin {current_plugin_name} stopped")
                except Exception as e:
                    log_error(f"[plugin] ❌ Error stopping previous plugin {current_plugin_name}: {e}")
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
                log_debug(f"[plugin] ⚠️ Plugin already loaded: {plugin.__class__.__name__}")
            return

    try:
        registry = get_llm_registry()
        plugin_instance = registry.load_engine(name, notify_fn)
    except Exception as e:
        log_error(f"[plugin] ❌ Failed to load plugin {name}: {e}", e)
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
                    loop.create_task(start_fn())
                    log_debug("[plugin] Plugin start executed on running loop.")
                else:
                    log_debug(
                        "[plugin] No running loop; plugin start will be invoked later."
                    )
            else:
                start_fn()
                log_debug("[plugin] Plugin start executed.")
        except Exception as e:
            log_error(f"[plugin] Error during plugin start: {e}", e)

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

    await set_active_llm(name)

async def handle_incoming_message(bot, message, context_memory_or_prompt, interface: str = None):
    """Process incoming messages or pre-built prompts."""

    # Check if plugin is loaded
    if plugin is None:
        log_error("[plugin_instance] No LLM plugin loaded! Cannot handle incoming message.")
        log_error(f"[plugin_instance] Available plugins: {dir()}")
        # Try to load manual plugin as fallback
        try:
            log_warning("[plugin_instance] Attempting to load manual plugin as fallback...")
            await load_plugin("manual")
            if plugin is None:
                raise ValueError("Manual plugin failed to load")
            log_info("[plugin_instance] Manual plugin loaded successfully as fallback")
        except Exception as fallback_e:
            log_error(f"[plugin_instance] Fallback plugin loading failed: {fallback_e}")
            raise ValueError("No LLM plugin loaded and fallback failed")

    if message is None and isinstance(context_memory_or_prompt, dict):
        prompt = context_memory_or_prompt
        message = SimpleNamespace(
            chat_id="TARDIS / system / events",
            message_id=int(datetime.utcnow().timestamp() * 1000) % 1_000_000,
            text=prompt.get("input", {}).get("payload", {}).get("description", ""),
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
            maybe_ctx = context_memory_or_prompt if isinstance(context_memory_or_prompt, dict) else None
            sys_type = None
            if maybe_ctx and isinstance(maybe_ctx.get("system_message"), dict):
                sys_type = maybe_ctx["system_message"].get("type")
            # Also accept messages that carry a system-like from_user (id==0)
            if sys_type == "event" or (hasattr(message, "from_user") and getattr(message.from_user, "id", None) == 0 and isinstance(context_memory_or_prompt, dict) and context_memory_or_prompt.get("system_message", {}).get("type") == "event"):
                try:
                    # Import lazily to avoid circular imports at module load
                    from core import message_queue

                    event_id = None
                    if maybe_ctx:
                        event_id = maybe_ctx.get("system_message", {}).get("event_id")
                    await message_queue.enqueue_event(bot, context_memory_or_prompt, event_id=event_id)
                    log_debug(f"[plugin_instance] Enqueued system event for processing: chat_id={getattr(message,'chat_id',None)} event_id={event_id}")
                    return None
                except Exception as e:
                    log_warning(f"[plugin_instance] Failed to enqueue event prompt: {e}")
                    # Fall through and let the plugin handle it directly
                    pass
        except Exception:
            pass

        message_text = getattr(message, "text", "")
        log_debug(f"[plugin_instance] Received message: {message_text}")
        log_debug(f"[plugin_instance] Context memory: {context_memory_or_prompt}")
        user_id = message.from_user.id if message.from_user else "unknown"
        interface_name = interface if interface else (
            bot.get_interface_id() if hasattr(bot, "get_interface_id") else bot.__class__.__name__
        )
        log_debug(
            f"[plugin] Incoming for {plugin.__class__.__name__}: chat_id={message.chat_id}, user_id={user_id}, text={message_text!r} via {interface_name}"
        )
        
        image_data, has_image_trigger = await _extract_image_data_from_message(message, interface_name)
        
        processed_image_data = None
        if image_data:
            log_info(f"[plugin_instance] Message contains image: {image_data['type']} from user {user_id}")
            
            # Create abstract context for image processing
            abstract_user = AbstractUser(id=user_id, interface_name=interface_name)
            abstract_message = AbstractMessage(
                id=getattr(message, 'message_id', None),
                text=getattr(message, 'text', '') or getattr(message, 'caption', ''),
                chat_id=getattr(message, 'chat_id', None),
                interface_name=interface_name
            )
            abstract_context = AbstractContext(
                interface_name=interface_name,
                user=abstract_user,
                message=abstract_message
            )
            
            # Check if message has text trigger (mentions, keywords, etc.)
            text_has_trigger = False
            if message_text:
                directed, reason = await is_message_for_bot(message, bot, human_count=None)
                text_has_trigger = directed
            
            # Combine image trigger with text trigger
            combined_trigger = has_image_trigger or text_has_trigger
            
            # Process the image (but don't auto-forward to LLM here)
            processed_image_data = await process_image_message(
                image_data, 
                abstract_context, 
                has_trigger=combined_trigger,
                forward_to_llm=False  # We'll include it in the prompt instead
            )
            
            if processed_image_data:
                log_info(f"[plugin_instance] Image processed successfully for user {user_id}")
            else:
                log_debug(f"[plugin_instance] Image not processed (access denied or error) for user {user_id}")
        
        if isinstance(context_memory_or_prompt, str):
            try:
                import json

                prompt = json.loads(context_memory_or_prompt)
            except Exception as e:
                log_warning(f"[plugin_instance] Failed to parse direct prompt: {e}")
                prompt = await build_json_prompt(message, {}, interface_name, image_data=processed_image_data)
        else:
            prompt = await build_json_prompt(message, context_memory_or_prompt, interface_name, image_data=processed_image_data)

    prompt = sanitize_for_json(prompt)
    log_debug("🌐 JSON PROMPT built for the plugin:")
    try:
        log_debug(json_dumps(prompt))
    except Exception as e:
        log_error(f"Failed to serialize prompt: {e}")

    # Trace handoff to LLM plugin
    try:
        log_info(f"[flow] -> LLM plugin: handing off chat_id={getattr(message, 'chat_id', None)} interface={interface} prompt_len={len(json_dumps(prompt)) if isinstance(prompt, (dict, list)) else len(str(prompt))}")
    except Exception:
        log_info(f"[flow] -> LLM plugin: handing off chat_id={getattr(message, 'chat_id', None)} interface={interface}")

    try:
        if plugin is None:
            log_error("[plugin_instance] No LLM plugin loaded, cannot process message")
            raise ValueError("No LLM plugin loaded")
            
        result = await plugin.handle_incoming_message(bot, message, prompt)
        # Log that plugin finished processing
        try:
            log_info(f"[flow] <- LLM plugin: completed for chat_id={getattr(message, 'chat_id', None)} result_type={type(result)}")
        except Exception:
            log_info(f"[flow] <- LLM plugin: completed for chat_id={getattr(message, 'chat_id', None)}")
        return result
    except Exception as e:
        log_error(f"[plugin_instance] LLM plugin raised an exception: {e}")
        raise


def get_supported_models():
    if plugin and hasattr(plugin, "get_supported_models"):
        return plugin.get_supported_models()
    return []


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
                    log_debug("[plugin] Nessun loop in esecuzione; il plugin sarà avviato successivamente.")
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
    if hasattr(message, 'photo') and message.photo:
        # DEBUG: Log the photo object BEFORE any processing
        log_debug(f"[plugin_instance] message.photo type BEFORE processing: {type(message.photo)}")
        log_debug(f"[plugin_instance] message.photo value BEFORE processing: {message.photo}")
        log_debug(f"[plugin_instance] message.photo is list: {isinstance(message.photo, list)}")
        log_debug(f"[plugin_instance] message.photo is tuple: {isinstance(message.photo, tuple)}")
        
        # Handle list of photos (multiple resolutions)
        if isinstance(message.photo, list):
            photo = message.photo[-1]  # Last element is typically highest resolution
        else:
            photo = message.photo
        
        # Debug: Log photo object type and attributes
        log_debug(f"[plugin_instance] Photo object type: {type(photo)}")
        log_debug(f"[plugin_instance] Photo object attributes: {dir(photo)}")
        log_debug(f"[plugin_instance] Photo file_id: {getattr(photo, 'file_id', None)}")
        log_debug(f"[plugin_instance] Photo file_unique_id: {getattr(photo, 'file_unique_id', None)}")
            
        image_data = {
            "type": "photo", 
            "file_id": getattr(photo, 'file_id', None),
            "file_unique_id": getattr(photo, 'file_unique_id', None),
            "width": getattr(photo, 'width', 0),
            "height": getattr(photo, 'height', 0),
            "file_size": getattr(photo, 'file_size', 0),
            "caption": getattr(message, 'caption', ''),
            "mime_type": getattr(photo, 'mime_type', "image/jpeg")  # Default to JPEG
        }
        log_info(f"[plugin_instance] Extracted image_data: {image_data}")
        has_trigger = True  # Photos are always considered as having trigger for now
        
    elif hasattr(message, 'document') and message.document:
        # Check if document is an image
        mime_type = getattr(message.document, 'mime_type', '')
        if mime_type and mime_type.startswith('image/'):
            image_data = {
                "type": "document",
                "file_id": message.document.file_id,
                "file_unique_id": message.document.file_unique_id,
                "file_name": getattr(message.document, 'file_name', ''),
                "mime_type": mime_type,
                "file_size": getattr(message.document, 'file_size', 0),
                "caption": getattr(message, 'caption', '')
            }
            has_trigger = True  # Documents with images are considered as having trigger
    
    # Check for attachment-based interfaces
    elif hasattr(message, 'attachments'):
        # Handle generic attachments
        for attachment in message.attachments:
            if hasattr(attachment, 'content_type') and attachment.content_type and attachment.content_type.startswith('image/'):
                image_data = {
                    "type": "attachment",
                    "url": attachment.url,
                    "filename": attachment.filename,
                    "content_type": attachment.content_type,
                    "size": getattr(attachment, 'size', 0),
                    "caption": getattr(message, 'content', '')
                }
                has_trigger = True
                break
    
    return image_data, has_trigger

