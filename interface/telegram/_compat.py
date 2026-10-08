# interface/telegram/_compat.py
"""python-telegram-bot shaped wrappers around a Telethon user client.

The rest of SyntH (``message_queue``, ``mention_utils``, ``message_send_utils``,
``command_registry``, ``message_sender`` ...) was written against the Bot API
objects of python-telegram-bot: ``bot.send_message(...)``, ``message.chat.type``,
``message.from_user.full_name`` and so on.  Instead of teaching all of those
about MTProto, the ``telegram`` (user-account) interface exposes the same
surface through the small classes below, and translates Telethon errors into
the PTB exception types the shared retry helpers already classify.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

from telethon import errors as tl_errors

from core.logging_utils import log_debug, log_warning

try:
    from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
except Exception:  # pragma: no cover - PTB is a hard dependency, tests may stub it

    class NetworkError(Exception):
        pass

    class BadRequest(NetworkError):
        pass

    class TimedOut(NetworkError):
        pass

    class RetryAfter(Exception):
        def __init__(self, retry_after: float = 0) -> None:
            super().__init__(f"Flood control exceeded. Retry in {retry_after} seconds")
            self.retry_after = retry_after


INTERFACE_ID = "telegram"

# Telethon RPC error codes -> the wording the shared helpers key their
# stale-identifier handling on (see interface/message_send_utils.py).
_RPC_TO_BAD_REQUEST: tuple[tuple[tuple[str, ...], str], ...] = (
    (
        ("MSG_ID_INVALID", "MESSAGE_ID_INVALID", "REPLY_MESSAGE", "REPLY_TO_INVALID"),
        "Message to be replied not found",
    ),
    (("TOPIC_ID_INVALID", "TOPIC_DELETED", "TOPIC_CLOSED"), "Message thread not found"),
    (
        (
            "PEER_ID_INVALID",
            "CHAT_ID_INVALID",
            "CHANNEL_INVALID",
            "USER_ID_INVALID",
            "INPUT_USER_DEACTIVATED",
        ),
        "Chat not found",
    ),
    (("MESSAGE_TOO_LONG",), "Message is too long"),
    (("ENTITIES_TOO_LONG", "ENTITY_BOUNDS_INVALID"), "can't parse entities"),
)


def translate_error(exc: BaseException) -> Exception:
    """Map a Telethon/network failure to the PTB exception the helpers expect."""
    if isinstance(exc, tl_errors.FloodWaitError):
        return RetryAfter(int(getattr(exc, "seconds", 0) or 0))
    if isinstance(exc, tl_errors.RPCError):
        # Telethon keeps the RPC code in the class name (``MsgIdInvalidError``);
        # ``exc.message`` is the human description.
        code = re.sub(r"(?<!^)(?=[A-Z])", "_", type(exc).__name__)
        code = code.removesuffix("_ERROR").removesuffix("Error").upper()
        code = f"{code} {str(getattr(exc, 'message', '') or '').upper()}".strip()
        for needles, wording in _RPC_TO_BAD_REQUEST:
            if any(n in code for n in needles):
                return BadRequest(f"{wording} ({code})")
        return BadRequest(f"{code}: {exc}")
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return TimedOut(str(exc) or "Timed out")
    if isinstance(exc, ConnectionError):
        return NetworkError(str(exc) or "Connection error")
    if isinstance(exc, ValueError) and "input entity" in str(exc).lower():
        # Telethon cannot resolve a peer it has never seen.
        return BadRequest(f"Chat not found ({exc})")
    return exc if isinstance(exc, Exception) else Exception(str(exc))


# ---------------------------------------------------------------------------
# Lightweight data classes mirroring the PTB objects SyntH reads
# ---------------------------------------------------------------------------


@dataclass
class TgUser:
    id: int
    username: Optional[str] = None
    first_name: Optional[str] = None
    last_name: Optional[str] = None
    is_bot: bool = False

    @property
    def full_name(self) -> str:
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) or self.username or str(self.id)


@dataclass
class TgChat:
    id: int
    type: str = "private"  # private | group | supergroup | channel
    title: Optional[str] = None
    username: Optional[str] = None
    is_forum: bool = False

    @property
    def effective_name(self) -> Optional[str]:
        return self.title or self.username


@dataclass
class TgFileRef:
    """Attachment descriptor; ``file_id`` is ``"<chat_id>:<message_id>"``."""

    file_id: str
    file_unique_id: str = ""
    file_name: Optional[str] = None
    mime_type: Optional[str] = None
    file_size: Optional[int] = None


@dataclass
class TgUpdate:
    """Minimal ``telegram.Update`` stand-in (used by command handlers)."""

    message: "TgMessage"
    effective_message: "TgMessage" = field(init=False)
    effective_chat: TgChat = field(init=False)
    effective_user: Optional[TgUser] = field(init=False)

    def __post_init__(self) -> None:
        self.effective_message = self.message
        self.effective_chat = self.message.chat
        self.effective_user = self.message.from_user


@dataclass
class TgContext:
    """Minimal ``ContextTypes.DEFAULT_TYPE`` stand-in."""

    bot: "TelethonBot"
    args: list[str] = field(default_factory=list)


class TgDownload:
    """Result of ``bot.get_file`` — downloads the attachment on demand."""

    def __init__(self, bot: "TelethonBot", file_id: str) -> None:
        self._bot = bot
        self.file_id = file_id

    async def download_as_bytearray(self) -> bytearray:
        tl_message = await self._bot.fetch_message(self.file_id)
        if tl_message is None:
            raise BadRequest("Message to be replied not found (attachment gone)")
        try:
            data = await self._bot.client.download_media(tl_message, file=bytes)
        except Exception as exc:
            raise translate_error(exc) from exc
        return bytearray(data or b"")

    async def download_to_drive(self, custom_path: str) -> str:
        tl_message = await self._bot.fetch_message(self.file_id)
        if tl_message is None:
            raise BadRequest("Message to be replied not found (attachment gone)")
        try:
            path = await self._bot.client.download_media(tl_message, file=custom_path)
        except Exception as exc:
            raise translate_error(exc) from exc
        return str(path or custom_path)


class TgMessage:
    """PTB ``Message`` look-alike built from a Telethon message."""

    def __init__(
        self,
        bot: "TelethonBot",
        tl_message: Any,
        *,
        chat: TgChat,
        from_user: Optional[TgUser],
        thread_id: Optional[int],
        is_topic_message: bool,
        reply_to_message: Optional["TgMessage"] = None,
    ) -> None:
        self._bot = bot
        self._tl = tl_message
        self.chat = chat
        self.chat_id: int = chat.id
        self.from_user = from_user
        self.message_id: int = int(tl_message.id)
        self.date: datetime = tl_message.date
        self.thread_id = thread_id
        self.message_thread_id = thread_id
        self.is_topic_message = is_topic_message
        self.reply_to_message = reply_to_message
        self.text: Optional[str] = (
            (tl_message.raw_text or None) if not tl_message.media else None
        )
        self.caption: Optional[str] = (
            (tl_message.raw_text or None) if tl_message.media else None
        )
        self.forum_topic_created = None
        self.human_count: Optional[int] = None

        ref_id = f"{chat.id}:{self.message_id}"

        def _ref(doc: Any) -> Optional[TgFileRef]:
            if doc is None:
                return None
            mime = getattr(doc, "mime_type", None)
            name = None
            for attr in getattr(doc, "attributes", None) or []:
                name = getattr(attr, "file_name", None) or name
            return TgFileRef(
                file_id=ref_id,
                file_unique_id=str(getattr(doc, "id", "")),
                file_name=name,
                mime_type=mime,
                file_size=getattr(doc, "size", None),
            )

        self.voice = _ref(tl_message.voice)
        self.video_note = _ref(tl_message.video_note)
        self.video = _ref(tl_message.video)
        self.audio = _ref(tl_message.audio)
        self.sticker = _ref(tl_message.sticker)
        photo = tl_message.photo
        self.photo = (
            [TgFileRef(file_id=ref_id, file_unique_id=str(photo.id))] if photo else None
        )
        # PTB only sets ``document`` for generic files (not voice/video/audio/sticker).
        generic = tl_message.document
        if generic is not None and any(
            (self.voice, self.video_note, self.video, self.audio, self.sticker)
        ):
            generic = None
        self.document = _ref(generic)

        fwd = getattr(tl_message, "fwd_from", None)
        self.forward_from_chat: Optional[TgChat] = None
        self.forward_from_message_id: Optional[int] = None
        self.forward_origin = None
        if fwd is not None:
            from_id = getattr(fwd, "from_id", None)
            channel_id = getattr(from_id, "channel_id", None)
            post = getattr(fwd, "channel_post", None)
            if channel_id is not None and post is not None:
                self.forward_from_chat = TgChat(
                    id=int(f"-100{channel_id}"), type="channel"
                )
                self.forward_from_message_id = int(post)

    # -- PTB conveniences -------------------------------------------------

    async def reply_text(
        self, text: str, parse_mode: Optional[str] = None, **kwargs: Any
    ) -> "TgSent":
        return await self._bot.send_message(
            chat_id=self.chat_id,
            text=text,
            parse_mode=parse_mode,
            reply_to_message_id=self.message_id,
            message_thread_id=self.thread_id,
            **kwargs,
        )

    async def set_reaction(self, reaction: str, is_big: bool = False) -> bool:
        # Reactions are decoration: never let a failure abort message handling.
        try:
            return await self._bot.set_message_reaction(
                chat_id=self.chat_id,
                message_id=self.message_id,
                reaction=reaction,
                is_big=is_big,
            )
        except Exception as exc:
            log_debug(f"[telegram] reaction {reaction!r} failed: {exc}")
            return False


@dataclass
class TgSent:
    """Return value of the send_* methods (PTB returns the sent ``Message``)."""

    message_id: int
    chat_id: int
    date: Optional[datetime] = None
    text: Optional[str] = None


# ---------------------------------------------------------------------------
# Building TgMessage objects from Telethon events
# ---------------------------------------------------------------------------


def chat_type_of(entity: Any) -> str:
    from telethon import types

    if isinstance(entity, types.User):
        return "private"
    if isinstance(entity, types.Channel):
        return "supergroup" if getattr(entity, "megagroup", False) else "channel"
    return "group"


def user_from_entity(entity: Any) -> Optional[TgUser]:
    from telethon import types

    if not isinstance(entity, types.User):
        return None
    return TgUser(
        id=int(entity.id),
        username=entity.username,
        first_name=entity.first_name,
        last_name=entity.last_name,
        is_bot=bool(entity.bot),
    )


def chat_from_entity(entity: Any, chat_id: int) -> TgChat:
    from telethon import types

    title = getattr(entity, "title", None)
    if title is None and isinstance(entity, types.User):
        title = " ".join(p for p in (entity.first_name, entity.last_name) if p) or None
    return TgChat(
        id=int(chat_id),
        type=chat_type_of(entity),
        title=title,
        username=getattr(entity, "username", None),
        is_forum=bool(getattr(entity, "forum", False)),
    )


def thread_of(tl_message: Any) -> tuple[Optional[int], bool]:
    """Return ``(thread_id, is_topic_message)`` for a forum-topic message."""
    reply = getattr(tl_message, "reply_to", None)
    if reply is None or not getattr(reply, "forum_topic", False):
        return None, False
    top = getattr(reply, "reply_to_top_id", None)
    if top:
        return int(top), True
    base = getattr(reply, "reply_to_msg_id", None)
    return (int(base) if base else None), True


async def wrap_message(
    bot: "TelethonBot", tl_message: Any, *, with_reply: bool = True
) -> TgMessage:
    chat_entity = await tl_message.get_chat()
    sender_entity = await tl_message.get_sender()
    chat = chat_from_entity(chat_entity, tl_message.chat_id)
    thread_id, is_topic = thread_of(tl_message)

    reply_msg: Optional[TgMessage] = None
    reply = getattr(tl_message, "reply_to", None)
    if with_reply and reply is not None and getattr(reply, "reply_to_msg_id", None):
        # A plain forum-topic message "replies" to the topic header; that is an
        # implicit attachment, not a quote (same rule as the Bot API handler).
        header_attachment = is_topic and not getattr(reply, "reply_to_top_id", None)
        try:
            replied = await tl_message.get_reply_message()
        except Exception as exc:  # network / deleted message
            log_debug(f"[telegram] could not fetch replied message: {exc}")
            replied = None
        if replied is not None and not header_attachment:
            reply_msg = await wrap_message(bot, replied, with_reply=False)
        elif replied is not None:
            reply_msg = await wrap_message(bot, replied, with_reply=False)

    return TgMessage(
        bot,
        tl_message,
        chat=chat,
        from_user=user_from_entity(sender_entity),
        thread_id=thread_id,
        is_topic_message=is_topic,
        reply_to_message=reply_msg,
    )


# ---------------------------------------------------------------------------
# Markdown dialect conversion
# ---------------------------------------------------------------------------

_BOLD_RE = re.compile(r"(?<![\*\w])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\*\w])")
_ITALIC_RE = re.compile(r"(?<![_\w])_(?!\s)([^_\n]+?)(?<!\s)_(?![_\w])")


def legacy_markdown_to_telethon(text: str) -> str:
    """Convert Bot-API *legacy Markdown* (``*b*``/``_i_``) to Telethon's ``**b**``/``__i__``."""
    text = _BOLD_RE.sub(r"**\1**", text)
    return _ITALIC_RE.sub(r"__\1__", text)


_CHAT_ACTIONS = {
    "typing": "SendMessageTypingAction",
    "record_voice": "SendMessageRecordAudioAction",
    "record_video": "SendMessageRecordVideoAction",
    "record_video_note": "SendMessageRecordRoundAction",
    "upload_voice": "SendMessageUploadAudioAction",
    "upload_photo": "SendMessageUploadPhotoAction",
    "upload_video": "SendMessageUploadVideoAction",
    "upload_document": "SendMessageUploadDocumentAction",
}


class TelethonBot:
    """Bot-API-compatible facade over a connected ``TelegramClient``."""

    def __init__(self, client: Any) -> None:
        self.client = client
        self.me: Optional[TgUser] = None
        self.id: Optional[int] = None
        self.username: Optional[str] = None
        self.first_name: Optional[str] = None

    # -- identity -----------------------------------------------------------

    async def refresh_me(self) -> TgUser:
        entity = await self.client.get_me()
        user = user_from_entity(entity) or TgUser(id=int(entity.id))
        self.me = user
        self.id = user.id
        self.username = user.username
        self.first_name = user.first_name
        return user

    async def get_me(self) -> TgUser:
        return self.me or await self.refresh_me()

    # -- helpers --------------------------------------------------------------

    @staticmethod
    def _peer(chat_id: Any) -> Any:
        if isinstance(chat_id, str) and chat_id.strip().lstrip("-").isdigit():
            return int(chat_id)
        return chat_id

    async def fetch_message(self, file_id: str) -> Any:
        chat_part, _, msg_part = str(file_id).partition(":")
        if not msg_part.isdigit():
            return None
        try:
            return await self.client.get_messages(
                self._peer(chat_part), ids=int(msg_part)
            )
        except Exception as exc:
            raise translate_error(exc) from exc

    async def _media_arg(self, value: Any) -> Any:
        """Resolve a ``"<chat>:<msg>"`` file_id to the original media; pass paths/files through."""
        if isinstance(value, str) and re.fullmatch(r"-?\d+:\d+", value):
            tl = await self.fetch_message(value)
            if tl is not None and tl.media is not None:
                return tl.media
        return value

    @staticmethod
    def _reply_target(
        reply_to_message_id: Any, message_thread_id: Any
    ) -> Optional[int]:
        for candidate in (reply_to_message_id, message_thread_id):
            try:
                if candidate is not None and int(candidate) > 0:
                    return int(candidate)
            except (TypeError, ValueError):
                continue
        return None

    # -- sending --------------------------------------------------------------

    async def send_message(
        self,
        chat_id: Any,
        text: str,
        parse_mode: Optional[str] = None,
        reply_to_message_id: Any = None,
        message_thread_id: Any = None,
        disable_web_page_preview: Optional[bool] = None,
        **_ignored: Any,
    ) -> TgSent:
        kwargs: dict[str, Any] = {}
        if parse_mode:
            mode = str(parse_mode).lower()
            if mode.startswith("markdown"):
                text = legacy_markdown_to_telethon(text)
                kwargs["parse_mode"] = "md"
            elif mode == "html":
                kwargs["parse_mode"] = "html"
        else:
            kwargs["parse_mode"] = None
        if disable_web_page_preview:
            kwargs["link_preview"] = False
        try:
            sent = await self.client.send_message(
                self._peer(chat_id),
                text,
                reply_to=self._reply_target(reply_to_message_id, message_thread_id),
                **kwargs,
            )
        except Exception as exc:
            raise translate_error(exc) from exc
        return TgSent(
            message_id=int(sent.id),
            chat_id=int(sent.chat_id),
            date=sent.date,
            text=text,
        )

    async def _send_file(
        self,
        chat_id: Any,
        media: Any,
        caption: Optional[str],
        reply_to_message_id: Any,
        message_thread_id: Any,
        **file_kwargs: Any,
    ) -> TgSent:
        try:
            payload = await self._media_arg(media)
            sent = await self.client.send_file(
                self._peer(chat_id),
                payload,
                caption=caption or None,
                reply_to=self._reply_target(reply_to_message_id, message_thread_id),
                **file_kwargs,
            )
        except Exception as exc:
            raise translate_error(exc) from exc
        return TgSent(
            message_id=int(sent.id), chat_id=int(sent.chat_id), date=sent.date
        )

    async def send_photo(
        self,
        chat_id: Any,
        photo: Any,
        caption: Optional[str] = None,
        reply_to_message_id: Any = None,
        message_thread_id: Any = None,
        **_: Any,
    ) -> TgSent:
        return await self._send_file(
            chat_id, photo, caption, reply_to_message_id, message_thread_id
        )

    async def send_video(
        self,
        chat_id: Any,
        video: Any,
        caption: Optional[str] = None,
        reply_to_message_id: Any = None,
        message_thread_id: Any = None,
        **_: Any,
    ) -> TgSent:
        return await self._send_file(
            chat_id,
            video,
            caption,
            reply_to_message_id,
            message_thread_id,
            supports_streaming=True,
        )

    async def send_audio(
        self,
        chat_id: Any,
        audio: Any,
        caption: Optional[str] = None,
        reply_to_message_id: Any = None,
        message_thread_id: Any = None,
        **_: Any,
    ) -> TgSent:
        return await self._send_file(
            chat_id, audio, caption, reply_to_message_id, message_thread_id
        )

    async def send_voice(
        self,
        chat_id: Any,
        voice: Any,
        caption: Optional[str] = None,
        reply_to_message_id: Any = None,
        message_thread_id: Any = None,
        **_: Any,
    ) -> TgSent:
        return await self._send_file(
            chat_id,
            voice,
            caption,
            reply_to_message_id,
            message_thread_id,
            voice_note=True,
        )

    async def send_document(
        self,
        chat_id: Any,
        document: Any,
        caption: Optional[str] = None,
        reply_to_message_id: Any = None,
        message_thread_id: Any = None,
        **_: Any,
    ) -> TgSent:
        return await self._send_file(
            chat_id,
            document,
            caption,
            reply_to_message_id,
            message_thread_id,
            force_document=True,
        )

    async def send_sticker(
        self,
        chat_id: Any,
        sticker: Any,
        reply_to_message_id: Any = None,
        message_thread_id: Any = None,
        **_: Any,
    ) -> TgSent:
        return await self._send_file(
            chat_id, sticker, None, reply_to_message_id, message_thread_id
        )

    # -- chat state ---------------------------------------------------------

    async def send_chat_action(self, chat_id: Any, action: str, **_: Any) -> bool:
        from telethon import functions, types

        cls_name = _CHAT_ACTIONS.get(str(action), "SendMessageTypingAction")
        action_cls = getattr(types, cls_name)
        try:
            peer = await self.client.get_input_entity(self._peer(chat_id))
            instance = (
                action_cls(0)
                if cls_name.startswith("SendMessageUpload")
                else action_cls()
            )
            await self.client(
                functions.messages.SetTypingRequest(peer=peer, action=instance)
            )
            return True
        except Exception as exc:
            log_debug(f"[telegram] send_chat_action({action}) failed: {exc}")
            return False

    async def set_message_reaction(
        self,
        chat_id: Any,
        message_id: Any,
        reaction: Any = None,
        is_big: bool = False,
        **_: Any,
    ) -> bool:
        from telethon import functions, types

        emoji = reaction if isinstance(reaction, str) else None
        if isinstance(reaction, (list, tuple)) and reaction:
            emoji = (
                reaction[0]
                if isinstance(reaction[0], str)
                else getattr(reaction[0], "emoji", None)
            )
        try:
            peer = await self.client.get_input_entity(self._peer(chat_id))
            await self.client(
                functions.messages.SendReactionRequest(
                    peer=peer,
                    msg_id=int(message_id),
                    reaction=[types.ReactionEmoji(emoticon=emoji)] if emoji else [],
                    big=bool(is_big),
                )
            )
            return True
        except Exception as exc:
            raise translate_error(exc) from exc

    async def get_chat(self, chat_id: Any) -> TgChat:
        try:
            entity = await self.client.get_entity(self._peer(chat_id))
        except Exception as exc:
            raise translate_error(exc) from exc
        return chat_from_entity(entity, int(chat_id))

    getChat = get_chat  # PTB camelCase alias used by the name resolver

    async def get_file(self, file_id: str) -> TgDownload:
        return TgDownload(self, file_id)

    async def edit_message_text(
        self,
        chat_id: Any,
        message_id: Any,
        text: str,
        parse_mode: Optional[str] = None,
        **_: Any,
    ) -> bool:
        if parse_mode and str(parse_mode).lower().startswith("markdown"):
            text = legacy_markdown_to_telethon(text)
        try:
            await self.client.edit_message(
                self._peer(chat_id),
                int(message_id),
                text,
                parse_mode="md" if parse_mode else None,
            )
            return True
        except Exception as exc:
            log_warning(f"[telegram] edit_message_text failed: {exc}")
            raise translate_error(exc) from exc

    async def delete_message(self, chat_id: Any, message_id: Any) -> bool:
        try:
            await self.client.delete_messages(self._peer(chat_id), [int(message_id)])
            return True
        except Exception as exc:
            raise translate_error(exc) from exc

    def __repr__(self) -> str:  # never leak the session through repr()
        return f"<TelethonBot id={self.id} username={self.username}>"
