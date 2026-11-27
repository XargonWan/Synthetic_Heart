"""What the Core needs to know about an interface, declared by the interface.

The Core used to keep lists of interface names (``telegram_bot``, ``discord_bot``,
...) in the message chain, the action parser and several helpers, each answering
one question about an interface: is it a place a person talks to, does it only get
speech for voice input, which action sends to it, does the sign of a chat id say
group or private, which accounts are bots. Naming an interface in the Core means
every new interface needs a Core change.

Now each interface **declares** those answers as :class:`InterfaceTraits` when it
registers (an extension declares them in its manifest), and the Core asks
:func:`traits_for`. An interface that declares nothing gets the plain defaults: a
user-facing chat that is sent to with ``send_message``.

The few interfaces that ship inside the Core and are not extensions are declared
in :data:`CORE_INTERFACES` below, the only place their names appear.
"""

from __future__ import annotations

from dataclasses import dataclass

#: The verb that sends to an ordinary chat interface.
SEND_MESSAGE = "send_message"


@dataclass(frozen=True)
class InterfaceTraits:
    """Answers about one interface. Every field has a safe default."""

    #: A person is on the other end: replies are expected, an empty turn is a miss.
    user_facing: bool = True
    #: Speech is added to a reply only when the incoming message was spoken.
    voice_input_tts_only: bool = False
    #: The action that delivers a message to this interface.
    message_action: str = SEND_MESSAGE
    #: Chat ids are numbers where a negative one is a group, a positive one a person.
    negative_chat_id_is_group: bool = False
    #: Account names ending in this (case-insensitive) belong to bots.
    bot_username_suffix: str | None = None
    #: The ids in ``SYNTH_PEERS`` are user ids of this interface's platform.
    peer_ids: bool = False
    #: A request/response interface (an HTTP API): the only place a turn may answer is
    #: the conversation the request came from.
    reply_only: bool = False
    #: A synthesised reply is delivered to this interface as a media file sent with
    #: ``send_message`` (a voice note, an audio attachment). Lip-sync does not depend on
    #: it: the Karada state server plays the speech on every connected avatar client.
    speech_as_media: bool = False
    #: The audio format this interface's chats accept for a spoken message (a voice note):
    #: ``ogg_opus``, ``mp3`` or ``m4a``. Empty: the clip is sent as the engine made it.
    #: Applies whenever speech goes to a chat rather than into a live call.
    voice_format: str = ""


DEFAULT = InterfaceTraits()

#: Interfaces that ship inside the Core. Extensions declare their own traits.
CORE_INTERFACES: dict[str, InterfaceTraits] = {
    "discord_bot": InterfaceTraits(voice_input_tts_only=True, speech_as_media=True),
    "synth_webui": InterfaceTraits(message_action="message_synth_webui"),
}

_declared: dict[str, InterfaceTraits] = {}


def register_traits(interface_id: str, traits: InterfaceTraits) -> None:
    """Declare the traits of ``interface_id`` (replaces an earlier declaration)."""
    _declared[interface_id] = traits


def unregister_traits(interface_id: str) -> None:
    """Forget a declaration (the interface went away)."""
    _declared.pop(interface_id, None)


def clear_declared_traits() -> None:
    """Forget every declaration. For tests."""
    _declared.clear()


def known_interface(interface_id: str) -> bool:
    """True when the Core knows ``interface_id`` through a declaration or as its own."""
    return interface_id in _declared or interface_id in CORE_INTERFACES


def traits_for(interface_id: str | None) -> InterfaceTraits:
    """The traits of ``interface_id``; the defaults for one that declared none."""
    if not interface_id:
        return DEFAULT
    declared = _declared.get(interface_id)
    if declared is not None:
        return declared
    return CORE_INTERFACES.get(interface_id, DEFAULT)


def interface_prefix(path_or_id: str | None) -> str:
    """The interface id of a path (``name/chat/thread``) or of a bare id."""
    return (path_or_id or "").split("/", 1)[0]


def is_user_facing(path_or_id: str | None) -> bool:
    """Whether a person is on the other end. Unknown interfaces are not."""
    prefix = interface_prefix(path_or_id)
    return known_interface(prefix) and traits_for(prefix).user_facing


def user_facing_ids() -> list[str]:
    """Ids of every known user-facing interface, sorted."""
    return sorted(
        name for name in {*_declared, *CORE_INTERFACES} if traits_for(name).user_facing
    )


def message_action_for(path_or_id: str | None) -> str:
    """The action that sends to the interface; ``send_message`` when undeclared."""
    return traits_for(interface_prefix(path_or_id)).message_action


def message_action_if_known(path_or_id: str | None) -> str | None:
    """Like :func:`message_action_for`, but ``None`` for an interface the Core does
    not know (so a made-up name is not mistaken for a real destination)."""
    prefix = interface_prefix(path_or_id)
    return traits_for(prefix).message_action if known_interface(prefix) else None


def message_actions() -> set[str]:
    """The sending action of every known interface."""
    return {traits_for(n).message_action for n in {*_declared, *CORE_INTERFACES}}


def is_voice_input_tts_only(path_or_id: str | None) -> bool:
    return traits_for(interface_prefix(path_or_id)).voice_input_tts_only


def peer_interface_ids() -> list[str]:
    """Ids of the known interfaces whose users ``SYNTH_PEERS`` entries identify."""
    return sorted(n for n in {*_declared, *CORE_INTERFACES} if traits_for(n).peer_ids)


def is_reply_only(path_or_id: str | None) -> bool:
    """Whether the interface can only be answered on the conversation it came from."""
    return traits_for(interface_prefix(path_or_id)).reply_only


def has_peer_ids(path_or_id: str | None) -> bool:
    return traits_for(interface_prefix(path_or_id)).peer_ids


def is_bot_username(path_or_id: str | None, username: object) -> bool:
    """True when ``username`` ends with the interface's bot-account suffix."""
    suffix = traits_for(interface_prefix(path_or_id)).bot_username_suffix
    return (
        bool(suffix)
        and isinstance(username, str)
        and username.lower().endswith(str(suffix).lower())
    )


def chat_kind_for_path(path: str | None) -> str | None:
    """``"group"`` or ``"dm"`` from the sign of the chat id, for interfaces that
    declared that convention; ``None`` when it does not apply or cannot be read."""
    if not path:
        return None
    parts = path.split("/")
    if len(parts) < 2 or not traits_for(parts[0]).negative_chat_id_is_group:
        return None
    try:
        chat_id = int(parts[1])
    except ValueError:
        return None
    return "group" if chat_id < 0 else "dm"
