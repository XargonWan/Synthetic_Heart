"""The only module that talks to Telethon.

``Account`` wraps a connected ``TelegramClient`` with what the extension needs:
reaching a chat by id or ``@username`` (a user account can only write to people it
has *seen*), sending text and files, chat actions, reactions, the profile picture
and reading a chat back after downtime. Everything raises Telethon's own errors;
the caller classifies them with :func:`convert.classify_error`.
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import convert

log = logging.getLogger("telegram.tl")

#: How long an unreachable user id is remembered, so a repeated send does not rescan
#: every group again.
PEER_MISS_TTL = 600.0
#: Bounds of the one-off search for a user the account has never addressed.
MAX_GROUPS_SCANNED = 25
MAX_PARTICIPANTS_SCANNED = 1000

_CHAT_ACTIONS = {
    "typing": "SendMessageTypingAction",
    "record_voice": "SendMessageRecordAudioAction",
    "upload_voice": "SendMessageUploadAudioAction",
    "upload_document": "SendMessageUploadDocumentAction",
}


class UnreachablePeer(ValueError):
    """The account has never seen this user, so it cannot write to them by id."""


class Account:
    def __init__(
        self, client: Any, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.client = client
        self._misses: dict[int, float] = {}
        self._clock = clock
        self.me: Any = None

    async def refresh_me(self) -> Any:
        self.me = await self.client.get_me()
        return self.me

    # ---------------------------------------------------------------- peers

    async def _addressable(self, user_id: int) -> bool:
        try:
            await self.client.get_input_entity(user_id)
            return True
        except ValueError:
            return False

    async def _learn_user(self, user_id: int) -> bool:
        """Find ``user_id`` so Telethon has its access hash. True when found.

        A user account can only message a bare user id it has *seen*: in a chat list,
        or as a member of a group it shares with them. Telethon caches every user a
        request returns, so listing the members of the shared groups is enough."""
        try:
            dialogs = list(await self.client.get_dialogs(limit=500))
        except Exception:  # noqa: BLE001
            dialogs = []
        if await self._addressable(user_id):
            return True
        scanned = 0
        for dialog in dialogs:
            entity = getattr(dialog, "entity", None)
            is_group = getattr(dialog, "is_group", False)
            is_megagroup = getattr(dialog, "is_channel", False) and getattr(
                entity, "megagroup", False
            )
            if not (is_group or is_megagroup):
                continue
            if scanned >= MAX_GROUPS_SCANNED:
                break
            scanned += 1
            try:
                async for member in self.client.iter_participants(
                    entity, limit=MAX_PARTICIPANTS_SCANNED
                ):
                    if getattr(member, "id", None) == user_id:
                        break
            except Exception:  # noqa: BLE001 - no rights to list members, flood wait...
                continue
            if await self._addressable(user_id):
                return True
        return await self._addressable(user_id)

    async def resolve_username(self, username: str) -> int | None:
        """A public ``@username`` as its numeric peer id (``None`` if unknown)."""
        match = convert.USERNAME_RE.match(str(username or "").strip())
        if not match:
            return None
        from telethon import utils

        try:
            entity = await self.client.get_entity(f"@{match.group(1)}")
        except Exception as exc:  # noqa: BLE001
            unknown = ("UsernameNotOccupiedError", "UsernameInvalidError")
            if isinstance(exc, ValueError) or type(exc).__name__ in unknown:
                return None
            raise
        return int(utils.get_peer_id(entity))

    async def ensure_peer(self, chat: Any) -> Any:
        """``chat`` as Telethon wants it, learning an unseen user first.

        An ``@username`` becomes its numeric id, so history and replies stay under one
        conversation. Groups and channels (negative ids) are left to Telethon. For a
        positive id the account has not met, the shared groups are searched once; a
        miss is remembered for ``PEER_MISS_TTL``."""
        if isinstance(chat, str):
            resolved = await self.resolve_username(chat)
            if resolved is None:
                raise UnreachablePeer(
                    f"Chat not found: no public Telegram account is named {chat!r}"
                )
            return resolved
        if not isinstance(chat, int) or chat <= 0:
            return chat
        if await self._addressable(chat):
            return chat
        missed = self._misses.get(chat)
        recent = missed is not None and self._clock() - missed < PEER_MISS_TTL
        if not recent and await self._learn_user(chat):
            self._misses.pop(chat, None)
            return chat
        self._misses[chat] = self._clock()
        raise UnreachablePeer(
            f"Chat not found: this account has never seen Telegram user {chat} (no "
            "chat, no shared group): they must message it once, or send to their @username"
        )

    # -------------------------------------------------------------- sending

    @staticmethod
    def _reply_target(reply: int | None, thread: int | None) -> int | None:
        for candidate in (reply, thread):
            if candidate is not None and candidate > 0:
                return candidate
        return None

    async def send_text(
        self,
        chat: Any,
        text: str,
        *,
        reply: int | None,
        thread: int | None,
        markdown: bool,
    ) -> Any:
        kwargs: dict[str, Any] = {"parse_mode": None}
        if markdown:
            text = convert.legacy_markdown_to_telethon(text)
            kwargs["parse_mode"] = "md"
        return await self.client.send_message(
            await self.ensure_peer(chat),
            text,
            reply_to=self._reply_target(reply, thread),
            **kwargs,
        )

    async def send_file(
        self,
        chat: Any,
        path: str,
        *,
        caption: str | None,
        reply: int | None,
        thread: int | None,
        kind: str,
        voice: bool,
    ) -> Any:
        extra: dict[str, Any] = {}
        if kind == "audio" and voice:
            extra["voice_note"] = True
        elif kind == "video":
            extra["supports_streaming"] = True
        elif kind not in ("image", "audio", "video"):
            extra["force_document"] = True
        return await self.client.send_file(
            await self.ensure_peer(chat),
            path,
            caption=caption or None,
            reply_to=self._reply_target(reply, thread),
            **extra,
        )

    async def chat_action(self, chat: Any, action: str, thread: int | None) -> None:
        from telethon import functions, types

        cls_name = _CHAT_ACTIONS.get(action, "SendMessageTypingAction")
        action_cls = getattr(types, cls_name)
        peer = await self.client.get_input_entity(chat)
        instance = (
            action_cls(0) if cls_name.startswith("SendMessageUpload") else action_cls()
        )
        kwargs: dict[str, Any] = {"top_msg_id": thread} if thread else {}
        await self.client(
            functions.messages.SetTypingRequest(peer=peer, action=instance, **kwargs)
        )

    async def react(self, chat: Any, message_id: int, emoji: str) -> None:
        from telethon import functions, types

        peer = await self.client.get_input_entity(chat)
        await self.client(
            functions.messages.SendReactionRequest(
                peer=peer,
                msg_id=int(message_id),
                reaction=[types.ReactionEmoji(emoticon=emoji)] if emoji else [],
                big=False,
            )
        )

    async def chat_title(self, chat: int) -> str | None:
        entity = await self.client.get_entity(await self.ensure_peer(chat))
        return convert.title_of(entity)

    # --------------------------------------------------------------- avatar

    async def set_avatar(self, image: bytes) -> None:
        from telethon import functions

        uploaded = await self.client.upload_file(image, file_name="avatar.png")
        await self.client(functions.photos.UploadProfilePhotoRequest(file=uploaded))

    async def remove_avatar(self) -> None:
        from telethon import functions, utils

        photos = await self.client.get_profile_photos("me", limit=1)
        if photos:
            await self.client(
                functions.photos.DeletePhotosRequest(
                    id=[utils.get_input_photo(photos[0])]
                )
            )

    # ------------------------------------------------------------- reading

    async def download(self, tl_message: Any, target: Path) -> Path | None:
        saved = await self.client.download_media(tl_message, file=str(target))
        return Path(saved) if saved else None

    async def warm(self) -> None:
        """Load the newest dialogs so sends to known chats resolve by id."""
        try:
            await self.client.get_dialogs(limit=200)
        except Exception:  # noqa: BLE001
            return

    async def recent_incoming(
        self, since: Any, dialogs: int = 200, per_dialog: int = 40
    ) -> AsyncIterator[Any]:
        """Incoming messages newer than ``since`` in the recently active chats, each
        chat oldest first."""
        async for dialog in self.client.iter_dialogs(limit=dialogs):
            last = getattr(dialog, "date", None)
            if last is not None and last < since:
                continue  # pinned chats come first, so no early exit
            pending: list[Any] = []
            try:
                async for raw in self.client.iter_messages(
                    dialog.entity, limit=per_dialog
                ):
                    if raw.date is not None and raw.date < since:
                        break
                    if not getattr(raw, "out", False):
                        pending.append(raw)
            except Exception as exc:  # noqa: BLE001
                log.debug("recovery of a chat failed: %r", exc)
                continue
            for raw in reversed(pending):
                yield raw
