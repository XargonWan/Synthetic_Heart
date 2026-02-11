from core import response_proxy
import core.plugin_instance as plugin_instance
import traceback
from core.logging_utils import log_debug, log_warning, log_error


async def send_content(bot, chat_id, message, content_type, reply_to_message_id=None):
    log_debug(
        f"Sending content: {content_type}, reply_message_id={reply_to_message_id}"
    )

    try:
        # Let clients know we're about to write/send a response so they can show 'write' animation
        try:
            from core.persona_manager import get_persona_manager

            pm = get_persona_manager()
            if pm:
                await pm.set_animation_state("write", session_id=None)
        except Exception:
            pass
        if content_type == "audio":
            try:
                log_debug("Sending audio...")
                file_id = (
                    message.audio.file_id if message.audio else message.document.file_id
                )
                log_debug(f"Detected file_id: {file_id}")
                result = await bot.send_audio(
                    chat_id=chat_id,
                    audio=file_id,
                    reply_to_message_id=reply_to_message_id,
                )
                try:
                    log_debug(
                        f"send_audio result message_id={getattr(result, 'message_id', None)}"
                    )
                except Exception:
                    log_debug("send_audio returned (unable to repr result)")
                return True, "\u2705 Audio sent successfully."
            except Exception as e_audio:
                log_warning(f"Audio send failed: {e_audio}")
                traceback.print_exc()
                if message.document:
                    try:
                        log_debug("Retrying send as document (fallback)...")
                        result = await bot.send_document(
                            chat_id=chat_id,
                            document=message.document.file_id,
                            reply_to_message_id=reply_to_message_id,
                        )
                        log_debug(
                            f"send_document (fallback) result message_id={getattr(result, 'message_id', None)}"
                        )
                        return True, "\u2705 Sent as document (audio fallback)."
                    except Exception as e_fallback:
                        log_error(f"Document fallback also failed: {e_fallback}")
                        traceback.print_exc()
                        return False, f"\u274c Double error: {e_audio} / {e_fallback}"
                return False, f"\u274c Audio send error: {e_audio}"

        elif content_type == "document":
            try:
                log_debug("Sending document...")
                result = await bot.send_document(
                    chat_id=chat_id,
                    document=message.document.file_id,
                    reply_to_message_id=reply_to_message_id,
                )
                log_debug(
                    f"send_document result message_id={getattr(result, 'message_id', None)}"
                )
                return True, "\u2705 Document sent successfully."
            except Exception as e_doc:
                log_warning(f"Document send failed: {e_doc}")
                traceback.print_exc()
                mime = message.document.mime_type or ""
                filename = message.document.file_name or ""
                if mime.startswith("audio/") or filename.lower().endswith(".mp3"):
                    try:
                        log_debug("Retrying send as audio (fallback)...")
                        result = await bot.send_audio(
                            chat_id=chat_id,
                            audio=message.document.file_id,
                            reply_to_message_id=reply_to_message_id,
                        )
                        log_debug(
                            f"send_audio (fallback) result message_id={getattr(result, 'message_id', None)}"
                        )
                        return True, "\u2705 Sent as audio (document fallback)."
                    except Exception as e_audio:
                        log_error(f"Audio fallback also failed: {e_audio}")
                        traceback.print_exc()
                        return False, f"\u274c Double error: {e_doc} / {e_audio}"
                return False, f"\u274c Document send error: {e_doc}"

        elif content_type == "voice":
            log_debug("Sending voice...")
            result = await bot.send_voice(
                chat_id=chat_id,
                voice=message.voice.file_id,
                reply_to_message_id=reply_to_message_id,
            )
            log_debug(
                f"send_voice result message_id={getattr(result, 'message_id', None)}"
            )

        elif content_type == "photo":
            log_debug("Sending photo...")
            result = await bot.send_photo(
                chat_id=chat_id,
                photo=message.photo[-1].file_id,
                reply_to_message_id=reply_to_message_id,
            )
            log_debug(
                f"send_photo result message_id={getattr(result, 'message_id', None)}"
            )

        elif content_type == "video":
            log_debug("Sending video...")
            result = await bot.send_video(
                chat_id=chat_id,
                video=message.video.file_id,
                reply_to_message_id=reply_to_message_id,
            )
            log_debug(
                f"send_video result message_id={getattr(result, 'message_id', None)}"
            )

        elif content_type == "sticker":
            log_debug("Sending sticker...")
            result = await bot.send_sticker(
                chat_id=chat_id,
                sticker=message.sticker.file_id,
                reply_to_message_id=reply_to_message_id,
            )
            log_debug(
                f"send_sticker result message_id={getattr(result, 'message_id', None)}"
            )

        elif content_type == "text":
            log_debug("Sending text...")
            kwargs = {"chat_id": chat_id, "text": message.text}
            if reply_to_message_id:
                kwargs["reply_to_message_id"] = reply_to_message_id
            # Use getattr to protect against bots lacking specific methods or attributes
            send_fn = getattr(bot, "send_message", None)
            if send_fn is None:
                log_error("[message_sender] Bot instance has no send_message method")
                return False, "Bot missing send method"
            sent = await send_fn(**kwargs)
            # Log returned message identifier when available
            try:
                msg_id = (
                    getattr(sent, "message_id", None)
                    or getattr(sent, "id", None)
                    or sent
                )
                log_debug(f"[message_sender] send_message result: {msg_id}")
            except Exception:
                log_debug(
                    "[message_sender] send_message returned an uninspectable result"
                )

        elif content_type == "file":
            log_debug("Sending file...")
            result = await bot.send_document(
                chat_id=chat_id,
                document=message.document.file_id,
                reply_to_message_id=reply_to_message_id,
            )
            log_debug(
                f"send_document(result) message_id={getattr(result, 'message_id', None)}"
            )

        else:
            log_error(f"Unhandled content type: {content_type}")
            return False, "\u274c Unsupported content type."

        # After sending, return to idle animation
        try:
            from core.persona_manager import get_persona_manager

            pm = get_persona_manager()
            if pm:
                await pm.set_animation_state("idle", session_id=None)
        except Exception:
            pass

        return True, "\u2705 Content sent successfully."

    except Exception as e:
        log_error(f"Error sending content: {repr(e)}")
        traceback.print_exc()
        return False, f"\u274c Error: {e}"


def detect_media_type(message):
    if message.sticker:
        return "sticker"
    elif message.photo:
        return "photo"
    elif message.voice:
        return "voice"
    elif message.audio:
        return "audio"
    elif message.video:
        return "video"
    elif message.document:
        mime = message.document.mime_type or ""
        filename = message.document.file_name or ""
        if mime.startswith("audio/") or filename.lower().endswith(".mp3"):
            log_debug(
                f"Documento rilevato come audio: mime={mime}, filename={filename}"
            )
            return "audio"
        log_debug(f"Documento generico: mime={mime}, filename={filename}")
        return "document"
    elif message.text:
        return "text"
    return "unknown"


def extract_response_target(message, user_id):
    log_debug(f"Extracting target for user_id={user_id}")

    # 1. Check via proxy (e.g. /photo...)
    target = response_proxy.get_target(user_id)
    log_debug(f"Initial target from proxy: {target}")

    # 2. Reply to a message
    if not target and message.reply_to_message:
        replied = message.reply_to_message
        log_debug(f"Reply to message: {replied.message_id}")
        log_debug("Checking mapping in plugin")

        for attempt in [
            replied.message_id,
            getattr(replied.reply_to_message, "message_id", None),
        ]:
            if attempt:
                tracked = plugin_instance.get_target(attempt)
                if tracked:
                    log_debug(f"Found target from reply: {tracked}")
                    return {
                        "chat_id": tracked["chat_id"],
                        "message_id": tracked["message_id"],
                        "type": detect_media_type(message),
                    }



    log_debug(f"Final target = {target}")
    return target


__all__ = ["send_content", "detect_media_type", "extract_response_target"]
