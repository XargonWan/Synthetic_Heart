"""A fake ``telethon`` package, written into an extension's ``lib/`` for tests.

The extension runs as a real process, so the fake cannot share memory with the test. It
talks through a control directory instead (its path is baked into the package):

``state.json``      what the "account" is: ``{"authorized", "me", "known_users"}``
``in_<n>.json``     a message the test delivers (consumed and deleted by the client)
``out_<n>.json``    every call the extension made: ``{"method", ...arguments}``
``fail.json``       errors to raise on the next calls: ``[{"method", "error", "seconds"}]``
``files/<name>``    media the test makes downloadable (``download_media``)

Only the part of Telethon the extension uses is implemented, with Telethon's own names.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PACKAGE = {
    "__init__.py": """
from . import errors, events, functions, sessions, types, utils
from ._client import TelegramClient

__all__ = ["TelegramClient", "errors", "events", "functions", "sessions", "types", "utils"]
""",
    "_control.py": """
import json, os, glob, itertools, time
DIR = {control!r}
_counter = itertools.count(1)

def state():
    try:
        with open(os.path.join(DIR, "state.json")) as f:
            return json.load(f)
    except FileNotFoundError:
        return {{}}

def record(method, **data):
    n = next(_counter)
    name = os.path.join(DIR, "out_%06d_%d.json" % (n, os.getpid()))
    with open(name + ".tmp", "w") as f:
        json.dump(dict(method=method, **data), f)
    os.replace(name + ".tmp", name)

def pop_failure(method):
    path = os.path.join(DIR, "fail.json")
    try:
        with open(path) as f:
            queue = json.load(f)
    except FileNotFoundError:
        return None
    for i, item in enumerate(queue):
        if item["method"] == method:
            del queue[i]
            with open(path, "w") as f:
                json.dump(queue, f)
            return item
    return None

def incoming():
    return sorted(glob.glob(os.path.join(DIR, "in_*.json")))
""",
    "errors.py": """
class RPCError(Exception):
    def __init__(self, message="", seconds=0):
        super().__init__(message)
        self.message = message
        self.seconds = seconds

class FloodWaitError(RPCError): pass
class SessionPasswordNeededError(RPCError): pass
class MsgIdInvalidError(RPCError): pass
class TopicIdInvalidError(RPCError): pass
class PeerIdInvalidError(RPCError): pass
class EntitiesTooLongError(RPCError): pass
class PhoneCodeInvalidError(RPCError): pass
class PhoneNumberInvalidError(RPCError): pass
class PasswordHashInvalidError(RPCError): pass
class UsernameNotOccupiedError(RPCError): pass

def make(name, seconds=0):
    return globals()[name]("", seconds)
""",
    "events.py": """
class NewMessage:
    def __init__(self, incoming=None, **kw):
        self.incoming = incoming
""",
    "types.py": """
class User:
    def __init__(self, id, first_name=None, last_name=None, username=None, bot=False):
        self.id, self.first_name, self.last_name = id, first_name, last_name
        self.username, self.bot = username, bot

class Channel:
    def __init__(self, id, title=None, username=None, megagroup=True, forum=False):
        self.id, self.title, self.username = id, title, username
        self.megagroup, self.forum = megagroup, forum

class Chat:
    def __init__(self, id, title=None, username=None):
        self.id, self.title, self.username = id, title, username

class ReactionEmoji:
    def __init__(self, emoticon): self.emoticon = emoticon

def _action(name):
    class A:
        def __init__(self, *a): self.args = a
    A.__name__ = name
    return A

SendMessageTypingAction = _action("SendMessageTypingAction")
SendMessageRecordAudioAction = _action("SendMessageRecordAudioAction")
SendMessageUploadAudioAction = _action("SendMessageUploadAudioAction")
SendMessageUploadDocumentAction = _action("SendMessageUploadDocumentAction")
""",
    "functions.py": """
from . import _control

def _request(name):
    class R:
        def __init__(self, **kw): self.kw = kw
    R.__name__ = name
    return R

class _Ns: pass
messages = _Ns()
messages.SetTypingRequest = _request("SetTypingRequest")
messages.SendReactionRequest = _request("SendReactionRequest")
photos = _Ns()
photos.UploadProfilePhotoRequest = _request("UploadProfilePhotoRequest")
photos.DeletePhotosRequest = _request("DeletePhotosRequest")
""",
    "utils.py": """
def get_peer_id(entity):
    return entity.id

def get_input_photo(photo):
    return photo
""",
    "sessions.py": """
class StringSession:
    def __init__(self, string=None):
        self.string = string or ""
    @staticmethod
    def save(session):
        return session.string or "SESSION:new"
""",
    "_client.py": """
import asyncio, json, os, datetime
from . import _control, errors, types

class _File:
    def __init__(self, d):
        self.size = d.get("size")

def _attr(name):
    class A: pass
    a = A(); a.file_name = name
    return a

class _Doc:
    def __init__(self, d):
        self.mime_type = d.get("mime")
        self.size = d.get("size")
        self.id = d.get("id", 1)
        self.attributes = [_attr(d["name"])] if d.get("name") else []

class _ReplyTo:
    def __init__(self, d):
        self.reply_to_msg_id = d.get("msg_id")
        self.reply_to_top_id = d.get("top_id")
        self.forum_topic = d.get("forum_topic", False)

def _entity(d):
    kind = d.get("type", "user")
    if kind == "user":
        return types.User(d["id"], d.get("first_name"), d.get("last_name"), d.get("username"), d.get("bot", False))
    return types.Channel(d["id"], d.get("title"), d.get("username"), True, d.get("forum", False))

class Message:
    def __init__(self, d):
        self._d = d
        self.id = d["id"]
        self.chat_id = d["chat"]["id"]
        self.date = datetime.datetime.fromisoformat(d["date"])
        self.raw_text = d.get("text", "")
        self.out = d.get("out", False)
        self.reply_to = _ReplyTo(d["reply_to"]) if d.get("reply_to") else None
        media = d.get("media")
        self.media = media
        self.photo = _Doc(media) if media and media.get("kind") == "photo" else None
        for k in ("voice", "video_note", "video", "audio", "sticker"):
            setattr(self, k, _Doc(media) if media and media.get("kind") == k else None)
        self.document = _Doc(media) if media and media.get("kind") in ("document", "voice", "audio") else None
        if media and media.get("kind") in ("voice", "audio"):
            self.document = _Doc(media)
        self.file = _File(media) if media else None
    async def get_chat(self):
        return _entity(self._d["chat"])
    async def get_sender(self):
        return _entity(self._d["sender"]) if self._d.get("sender") else None
    async def get_reply_message(self):
        r = self._d.get("replied")
        return Message(r) if r else None

class _Session:
    string = ""

class TelegramClient:
    def __init__(self, session, api_id, api_hash, **kw):
        self.session = session if hasattr(session, "string") else _Session()
        self._handlers = []
        self._stop = asyncio.Event()
        self._task = None
        self._connected = False

    def _fail(self, method):
        f = _control.pop_failure(method)
        if f:
            raise errors.make(f["error"], f.get("seconds", 0))

    async def connect(self):
        self._connected = True
        _control.record("connect")
        self._task = asyncio.ensure_future(self._pump())

    async def disconnect(self):
        self._stop.set()
        _control.record("disconnect")

    async def _pump(self):
        while not self._stop.is_set():
            for path in _control.incoming():
                try:
                    with open(path) as f:
                        data = json.load(f)
                    os.remove(path)
                except (FileNotFoundError, ValueError):
                    continue
                msg = Message(data)
                event = type("Event", (), {"message": msg})()
                for handler in list(self._handlers):
                    await handler(event)
            await asyncio.sleep(0.03)

    def add_event_handler(self, callback, event):
        self._handlers.append(callback)

    async def run_until_disconnected(self):
        await self._stop.wait()

    async def is_user_authorized(self):
        return bool(_control.state().get("authorized", True))

    async def get_me(self):
        me = _control.state().get("me", {"id": 1, "first_name": "Synth", "username": "synth_user"})
        return _entity({"type": "user", **me})

    async def get_input_entity(self, peer):
        known = _control.state().get("known_users")
        if isinstance(peer, int) and peer > 0 and known is not None and peer not in known:
            raise ValueError("Could not find the input entity for %s" % peer)
        return peer

    async def get_entity(self, peer):
        _control.record("get_entity", peer=str(peer))
        ents = _control.state().get("entities", {})
        if str(peer) in ents:
            return _entity(ents[str(peer)])
        if isinstance(peer, str) and peer.startswith("@"):
            raise errors.UsernameNotOccupiedError("no such user")
        return types.User(peer if isinstance(peer, int) else 0, "Someone")

    async def get_dialogs(self, limit=None):
        _control.record("get_dialogs")
        return []

    def iter_dialogs(self, limit=None):
        async def gen():
            for d in _control.state().get("dialogs", []):
                entity = _entity(d["entity"])
                yield type("Dialog", (), {"entity": entity, "date": datetime.datetime.fromisoformat(d["date"]), "is_group": False, "is_channel": False})()
        return gen()

    def iter_messages(self, entity, limit=None):
        async def gen():
            for d in _control.state().get("history", {}).get(str(entity.id), []):
                yield Message(d)
        return gen()

    def iter_participants(self, entity, limit=None):
        async def gen():
            if False:
                yield None
        return gen()

    async def send_message(self, peer, text, reply_to=None, parse_mode=None, **kw):
        self._fail("send_message")
        _control.record("send_message", peer=str(peer), text=text, reply_to=reply_to, parse_mode=parse_mode)
        return type("Sent", (), {"id": 5000, "chat_id": peer, "date": datetime.datetime.now(datetime.timezone.utc)})()

    async def send_file(self, peer, path, caption=None, reply_to=None, **kw):
        self._fail("send_file")
        with open(path, "rb") as f:
            data = f.read()
        _control.record("send_file", peer=str(peer), name=os.path.basename(path), size=len(data), data_hex=data[:64].hex(), caption=caption, reply_to=reply_to, extra=sorted(kw))
        return type("Sent", (), {"id": 5001, "chat_id": peer, "date": datetime.datetime.now(datetime.timezone.utc)})()

    async def download_media(self, message, file=None):
        self._fail("download_media")
        name = (message._d.get("media") or {}).get("file")
        source = os.path.join(_control.DIR, "files", name) if name else None
        if not source or not os.path.exists(source):
            raise errors.RPCError("file gone")
        with open(source, "rb") as f, open(file, "wb") as out:
            out.write(f.read())
        return file

    async def upload_file(self, data, file_name=None):
        _control.record("upload_file", size=len(data), name=file_name)
        return "uploaded"

    async def get_profile_photos(self, who, limit=1):
        return ["photo"] if _control.state().get("has_photo") else []

    async def __call__(self, request):
        kind = type(request).__name__
        self._fail(kind)
        kw = {k: (v if isinstance(v, (int, float, str, bool, type(None))) else type(v).__name__) for k, v in request.kw.items()}
        if kind == "SendReactionRequest":
            kw["emoji"] = [r.emoticon for r in request.kw["reaction"]]
            kw.pop("reaction", None)
        if kind == "SetTypingRequest":
            kw["action"] = type(request.kw["action"]).__name__
        _control.record("request", request=kind, **kw)

    async def send_code_request(self, phone):
        self._fail("send_code_request")
        _control.record("send_code_request", phone=phone)
        return type("Sent", (), {"phone_code_hash": "hash123"})()

    async def sign_in(self, phone=None, code=None, phone_code_hash=None, password=None):
        st = _control.state()
        if password is not None:
            _control.record("sign_in_password")
            if password != st.get("password", "pw"):
                raise errors.PasswordHashInvalidError("bad")
        else:
            _control.record("sign_in_code", phone=phone, hash=phone_code_hash)
            if code != st.get("code", "12345"):
                raise errors.PhoneCodeInvalidError("bad")
            if st.get("two_step"):
                raise errors.SessionPasswordNeededError("2fa")
        self.session.string = "SESSION:" + str(st.get("session_user", "alice"))
""",
}


def install(lib_dir: Path, control_dir: Path) -> None:
    """Write the fake ``telethon`` package into ``lib_dir`` (an extension's ``lib/``)."""
    package = lib_dir / "telethon"
    package.mkdir(parents=True, exist_ok=True)
    for name, source in PACKAGE.items():
        text = (
            source.format(control=str(control_dir)) if name == "_control.py" else source
        )
        (package / name).write_text(text, encoding="utf-8")
    (control_dir / "files").mkdir(parents=True, exist_ok=True)


class Control:
    """The test's side of the control directory."""

    def __init__(self, directory: Path) -> None:
        self.dir = directory
        self._n = 0
        self.state: dict[str, Any] = {"authorized": True}
        self.write_state()

    def write_state(self) -> None:
        (self.dir / "state.json").write_text(json.dumps(self.state), encoding="utf-8")

    def deliver(self, **message: Any) -> None:
        """Hand the account an incoming message."""
        self._n += 1
        message.setdefault("id", 100 + self._n)
        message.setdefault("date", "2026-01-01T00:00:00+00:00")
        path = self.dir / f"in_{self._n:06d}.json"
        path.write_text(json.dumps(message), encoding="utf-8")

    def fail_next(self, method: str, error: str, seconds: int = 0) -> None:
        path = self.dir / "fail.json"
        queue = json.loads(path.read_text()) if path.exists() else []
        queue.append({"method": method, "error": error, "seconds": seconds})
        path.write_text(json.dumps(queue), encoding="utf-8")

    def make_file(self, name: str, data: bytes) -> None:
        (self.dir / "files" / name).write_bytes(data)

    def calls(self, method: str | None = None) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for path in sorted(self.dir.glob("out_*.json")):
            try:
                item = json.loads(path.read_text())
            except ValueError:
                continue
            if method is None or item["method"] == method:
                out.append(item)
        return out

    def requests(self, kind: str) -> list[dict[str, Any]]:
        return [c for c in self.calls("request") if c.get("request") == kind]
