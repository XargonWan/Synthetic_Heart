"""Extension manifest: model, parser and validator (platform milestone M1).

An extension is described by a ``synth-extension.toml`` file (see
``docs/plugin_platform/manifest_proposal.rst``). This module turns that file into
an immutable :class:`Manifest` and reports **every** problem it finds, so an
author fixes a manifest in one pass instead of one error at a time.

This module is intentionally dependency-free: standard library plus
``packaging``. It does not import anything else from ``core`` so the same code can
be reused by the extension SDK and by the packaging command.

The schema is **strict**: an unknown key is an error. A typo must not silently
disable a permission or a dependency.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version
from synth_sdk.paths import expand_pattern

MANIFEST_FILENAME = "synth-extension.toml"
SCHEMA_VERSION = 1

#: ``publisher/name``: lowercase letters, digits, ``_`` and ``-``.
EXTENSION_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*/[a-z0-9][a-z0-9_-]*$")
#: Dotted lowercase identifier, for example ``interfaces.register``.
CAPABILITY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")
#: Interface identity (the first segment of an Interface Path).
INTERFACE_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")
#: Name of an external endpoint, as the Engines page shows it.
ENDPOINT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{1,62}$")
#: Generic lowercase identifier (action, hook, UI slot, service names).
IDENT_RE = re.compile(r"^[a-z][a-z0-9_.-]*$")
#: Configuration key, as in ``config_registry``.
CONFIG_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
#: ``host:port`` with an optional leading ``*.`` wildcard on the host.
NETWORK_RE = re.compile(r"^(\*\.)?[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?:[0-9]{1,5}$")
#: Module path that the extension process will import.
MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")

#: Audio formats an interface may ask spoken messages in.
VOICE_FORMATS = frozenset({"ogg_opus", "mp3", "m4a"})
ENGINE_KINDS = frozenset({"llm", "vox", "auris", "iris", "live"})
CONFIG_TYPES = frozenset({"string", "int", "float", "bool"})

_TOP_LEVEL_KEYS = frozenset(
    {
        "schema",
        "extension",
        "compat",
        "entry",
        "provides",
        "requires",
        "permissions",
        "python",
        "config",
    }
)


@dataclass(frozen=True)
class InterfaceProvision:
    """An interface identity an extension registers, and its declared paths."""

    id: str
    paths: tuple[str, ...] = ()
    #: What the Core needs to know about the interface (see ``core.interface_traits``).
    user_facing: bool = True
    voice_input_tts_only: bool = False
    negative_chat_id_is_group: bool = False
    bot_username_suffix: str = ""
    peer_ids: bool = False
    reply_only: bool = False
    #: A synthesised reply is delivered as a media file through ``send_message``.
    speech_as_media: bool = False
    #: The audio format the interface's chats accept for a spoken message (see
    #: ``core.interface_traits.InterfaceTraits.voice_format``); empty: as the engine made it.
    voice_format: str = ""
    #: Names of the extra actions the interface may offer besides ``send_message``.
    actions: tuple[str, ...] = ()
    #: The one of those actions that is how the interface *speaks* (a radio's "say on
    #: air"), when it is not ``send_message``. Empty: ``send_message``.
    message_action: str = ""


#: The fixed places in the WebUI an extension may put a window in. ``activity`` and
#: ``logs`` are sub-tabs of those two sections; ``more`` is the "More" menu, which
#: opens each registered window. Nothing else in the WebUI takes a contribution.
UI_SLOTS = ("activity", "logs", "more")


@dataclass(frozen=True)
class UiEntry:
    """A window an extension puts in one of the fixed WebUI slots.

    The window is a page of the extension's own static files (``page``, relative to
    ``provides.web.static``), shown in a frame.
    """

    slot: str
    id: str
    title: str
    icon: str = ""
    page: str = "index.html"


@dataclass(frozen=True)
class WebProvision:
    """Web content an extension adds to the Core's web server.

    ``static`` is a directory of the package served read-only at ``/x/<id>/``;
    ``alias`` also serves it at ``/<alias>/``. ``routes`` lets the extension answer
    HTTP requests under ``/x/<id>/api/`` (the Core forwards them over the gateway) and
    ``websockets`` WebSocket connections under ``/x/<id>/ws/``. Each needs the matching
    capability (``web.static``, ``web.routes``, ``web.ws``), declared and granted.
    """

    static: str = ""
    alias: str = ""
    routes: bool = False
    websockets: bool = False
    #: Run the pages with the Core's own origin (needs ``web.origin``). Without it the
    #: pages are sandboxed: no cookies, no local storage, no way to use the WebUI session.
    same_origin: bool = False


@dataclass(frozen=True)
class ReconProvision:
    """A question the extension adds to the Recon step of every turn.

    Recon asks the model, once per turn and for all the plugins at once, a few small
    structured questions about the message. This is one of them: ``key`` names the
    answer, ``instruction`` is the question (written for the model, language-neutral),
    ``priority`` orders what the extension's answer adds to the prompt. When the model
    answers, the Core calls ``recon.contribute`` on the extension with that answer and
    puts what it returns into the prompt.
    """

    key: str
    instruction: str
    priority: int = 0


@dataclass(frozen=True)
class ProtocolProvision:
    """A wire protocol for external endpoints that the extension speaks.

    An external endpoint (an LLM, speech or vision service) names a ``protocol``; the Core
    asks the adapter of that protocol to list models, synthesise speech, transcribe, and so
    on. When the extension declares one here and holds ``endpoints.protocol``, the Core
    asks **the extension** for the operations in ``ops``. Operations not listed fall back to
    the ``base`` protocol's adapter when one is named (a dialect that differs from the base
    only in a few places, as Harmony differs from OpenAI), and are unsupported otherwise.
    The extension is given the API key of the endpoints that use its protocol, and of no
    others.
    """

    name: str
    #: Protocol whose adapter answers the operations this one does not.
    base: str = ""
    ops: tuple[str, ...] = ()


#: Operations an extension may implement for a protocol.
PROTOCOL_OPS = frozenset(
    {
        "chat",
        "models",
        "capabilities",
        "health",
        "ping",
        "tts",
        "stt",
        "vision",
        "list_speakers",
        "fetch_model_detail",
        "parse_model_id_from_url",
    }
)
MAX_PROTOCOLS = 4
PROTOCOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")


@dataclass(frozen=True)
class VesselProvision:
    """A Rift Vessel connector: the body this extension gives the synth in one world.

    The Vessel (in the Core) owns the session, the salience filter, the beats and the diary.
    The connector only embodies: it connects to the world, turns what happens into
    perception events, and carries out the actions the Vessel asks for. An extension that
    declares one here and holds ``vessel.connector`` is offered to the Vessel under ``name``
    next to the built-in connectors.
    """

    name: str
    #: Shown where the Vessel lists its connectors.
    label: str = ""
    #: The world's chat is still there on the next visit (a person in a room), so ending
    #: a session does not clear it.
    remembers_chat: bool = False
    #: Capability flags: ``movement``, ``chat``, ``perception``, ``interaction``, ``local``.
    capabilities: tuple[str, ...] = ()


VESSEL_CAPABILITIES = frozenset(
    {"movement", "chat", "perception", "interaction", "local"}
)
MAX_VESSELS = 4
VESSEL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

#: Recon questions one extension may add.
MAX_RECON = 4
RECON_KEY_RE = re.compile(r"^[a-z][a-z0-9_]{1,39}$")


@dataclass(frozen=True)
class Provides:
    """What the extension contributes to the system."""

    interfaces: tuple[InterfaceProvision, ...] = ()
    actions: tuple[str, ...] = ()
    engines: tuple[str, ...] = ()
    hooks: tuple[str, ...] = ()
    ui: tuple[UiEntry, ...] = ()
    web: WebProvision | None = None
    #: Names of the external endpoints (``external_endpoints``) the extension may create
    #: and keep up to date. Any other endpoint is out of its reach.
    endpoints: tuple[str, ...] = ()
    #: Questions added to the Recon step (see :class:`ReconProvision`).
    recon: tuple[ReconProvision, ...] = ()
    #: Wire protocols for external endpoints (see :class:`ProtocolProvision`).
    protocols: tuple[ProtocolProvision, ...] = ()
    #: Rift Vessel connectors (see :class:`VesselProvision`).
    vessels: tuple[VesselProvision, ...] = ()


@dataclass(frozen=True)
class ExtensionDependency:
    """A dependency on another extension. ``version`` is a PEP 440 specifier."""

    id: str
    version: str = ""
    optional: bool = False


@dataclass(frozen=True)
class Requires:
    """Other extensions and external services the extension needs."""

    extensions: tuple[ExtensionDependency, ...] = ()
    services: tuple[str, ...] = ()


@dataclass(frozen=True)
class Permissions:
    """Capabilities and network destinations the extension asks for.

    Declaring a permission does not grant it: the gateway checks the declaration
    **and** the user's grant on every call (milestone M2).
    """

    capabilities: tuple[str, ...] = ()
    network: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConfigField:
    """One configuration key owned by the extension."""

    key: str
    type: str = "string"
    label: str = ""
    description: str = ""
    sensitive: bool = False
    required: bool = False
    default: str | int | float | bool | None = None


@dataclass(frozen=True)
class Entry:
    """Where the extension process starts. Provisional until M2 fixes the SDK."""

    module: str
    function: str = "main"


@dataclass(frozen=True)
class Manifest:
    """A validated extension manifest."""

    id: str
    name: str
    version: str
    description: str = ""
    #: Who wrote it: ``author = "Name"`` or ``author = ["Name", "Other Name"]`` in the manifest.
    authors: tuple[str, ...] = ()
    license: str = ""
    experimental: bool = False
    #: PEP 440 specifier of the Core API versions this extension works with. The
    #: parser requires it; an empty value (only possible for a manifest built in
    #: code) means "any version".
    core_api: str = ""
    entry: Entry | None = None
    provides: Provides = field(default_factory=Provides)
    requires: Requires = field(default_factory=Requires)
    permissions: Permissions = field(default_factory=Permissions)
    python_requirements: tuple[str, ...] = ()
    config: tuple[ConfigField, ...] = ()

    @property
    def author(self) -> str:
        """The authors as one line (``"A, B"``)."""
        return ", ".join(self.authors)

    @property
    def interface_ids(self) -> tuple[str, ...]:
        """Interface identities the extension provides."""
        return tuple(item.id for item in self.provides.interfaces)


class ManifestError(ValueError):
    """The manifest is invalid. ``errors`` lists every problem found."""

    def __init__(self, source: str, errors: Sequence[str]) -> None:
        self.source = source
        self.errors: tuple[str, ...] = tuple(errors)
        lines = "\n".join(f"  - {item}" for item in self.errors)
        super().__init__(f"Invalid extension manifest ({source}):\n{lines}")


class _Collector:
    """Collects validation errors with a dotted path prefix."""

    def __init__(self) -> None:
        self.errors: list[str] = []

    def add(self, path: str, message: str) -> None:
        self.errors.append(f"{path}: {message}")

    def table(self, data: Any, path: str) -> Mapping[str, Any] | None:
        if data is None:
            return {}
        if not isinstance(data, Mapping):
            self.add(path, f"must be a table, got {type(data).__name__}")
            return None
        return data

    def unknown_keys(
        self, data: Mapping[str, Any], allowed: frozenset[str], path: str
    ) -> None:
        for key in sorted(set(data) - allowed):
            self.add(
                f"{path}.{key}" if path else key,
                f"unknown key (allowed: {', '.join(sorted(allowed))})",
            )

    def string(
        self,
        data: Mapping[str, Any],
        key: str,
        path: str,
        *,
        required: bool = False,
        default: str = "",
    ) -> str:
        if key not in data:
            if required:
                self.add(f"{path}.{key}", "is required")
            return default
        value = data[key]
        if not isinstance(value, str):
            self.add(f"{path}.{key}", f"must be a string, got {type(value).__name__}")
            return default
        if required and not value.strip():
            self.add(f"{path}.{key}", "must not be empty")
        return value

    def boolean(
        self, data: Mapping[str, Any], key: str, path: str, *, default: bool = False
    ) -> bool:
        if key not in data:
            return default
        value = data[key]
        if not isinstance(value, bool):
            self.add(
                f"{path}.{key}", f"must be true or false, got {type(value).__name__}"
            )
            return default
        return value

    def string_list(
        self, data: Mapping[str, Any], key: str, path: str
    ) -> tuple[str, ...]:
        if key not in data:
            return ()
        value = data[key]
        if not isinstance(value, list) or not all(isinstance(i, str) for i in value):
            self.add(f"{path}.{key}", "must be a list of strings")
            return ()
        duplicates = sorted({i for i in value if value.count(i) > 1})
        if duplicates:
            self.add(f"{path}.{key}", f"duplicate entries: {', '.join(duplicates)}")
        return tuple(value)

    def matching(
        self,
        values: Sequence[str],
        pattern: re.Pattern[str],
        path: str,
        what: str,
    ) -> None:
        for item in values:
            if not pattern.match(item):
                self.add(path, f"invalid {what} {item!r}")


def _valid_version(value: str) -> bool:
    try:
        Version(value)
    except InvalidVersion:
        return False
    return True


def _valid_specifier(value: str) -> bool:
    if not value:
        return True
    try:
        SpecifierSet(value)
    except InvalidSpecifier:
        return False
    return True


def _parse_extension(data: Mapping[str, Any], c: _Collector) -> dict[str, Any]:
    table = c.table(data.get("extension"), "extension")
    if table is None or "extension" not in data:
        if "extension" not in data:
            c.add("extension", "table is required")
        return {}
    c.unknown_keys(
        table,
        frozenset(
            {
                "id",
                "name",
                "version",
                "description",
                "author",
                "license",
                "experimental",
            }
        ),
        "extension",
    )
    ext_id = c.string(table, "id", "extension", required=True)
    if ext_id and not EXTENSION_ID_RE.match(ext_id):
        c.add(
            "extension.id",
            f"must look like 'publisher/name' (lowercase letters, digits, '_' or '-'), got {ext_id!r}",
        )
    version = c.string(table, "version", "extension", required=True)
    if version and not _valid_version(version):
        c.add("extension.version", f"is not a valid version: {version!r}")
    return {
        "id": ext_id,
        "name": c.string(table, "name", "extension", required=True),
        "version": version,
        "description": c.string(table, "description", "extension"),
        "authors": _parse_authors(table, c),
        "license": c.string(table, "license", "extension"),
        "experimental": c.boolean(table, "experimental", "extension"),
    }


def _parse_authors(table: Mapping[str, Any], c: _Collector) -> tuple[str, ...]:
    """``author`` is a name or a list of names (an extension can have several authors)."""
    raw = table.get("author", "")
    if isinstance(raw, str):
        return (raw.strip(),) if raw.strip() else ()
    if isinstance(raw, list) and all(isinstance(i, str) and i.strip() for i in raw):
        names = tuple(i.strip() for i in raw)
        if len(set(names)) != len(names):
            c.add("extension.author", "the same author is listed twice")
        return names
    c.add("extension.author", "must be a name or a list of names")
    return ()


def _parse_compat(data: Mapping[str, Any], c: _Collector) -> str:
    table = c.table(data.get("compat"), "compat")
    if table is None:
        return ""
    c.unknown_keys(table, frozenset({"core_api"}), "compat")
    spec = c.string(table, "core_api", "compat", required=True)
    if spec and not _valid_specifier(spec):
        c.add("compat.core_api", f"is not a valid version specifier: {spec!r}")
    return spec


def _parse_entry(data: Mapping[str, Any], c: _Collector) -> Entry | None:
    if "entry" not in data:
        return None
    table = c.table(data["entry"], "entry")
    if table is None:
        return None
    c.unknown_keys(table, frozenset({"module", "function"}), "entry")
    module = c.string(table, "module", "entry", required=True)
    function = c.string(table, "function", "entry", default="main")
    if module and not MODULE_RE.match(module):
        c.add("entry.module", f"is not a dotted module path: {module!r}")
    if function and not function.isidentifier():
        c.add("entry.function", f"is not a valid function name: {function!r}")
    return Entry(module=module, function=function or "main")


def _parse_provides(data: Mapping[str, Any], c: _Collector) -> Provides:
    table = c.table(data.get("provides"), "provides")
    if table is None:
        return Provides()
    c.unknown_keys(
        table,
        frozenset(
            {
                "interfaces",
                "actions",
                "engines",
                "hooks",
                "ui",
                "web",
                "endpoints",
                "recon",
                "protocols",
                "vessels",
            }
        ),
        "provides",
    )
    interfaces: list[InterfaceProvision] = []
    raw = table.get("interfaces", [])
    if not isinstance(raw, list):
        c.add("provides.interfaces", "must be a list of tables")
        raw = []
    for index, item in enumerate(raw):
        path = f"provides.interfaces[{index}]"
        entry = c.table(item, path)
        if entry is None or not isinstance(item, Mapping):
            continue
        c.unknown_keys(
            entry,
            frozenset(
                {
                    "id",
                    "paths",
                    "user_facing",
                    "voice_input_tts_only",
                    "negative_chat_id_is_group",
                    "bot_username_suffix",
                    "peer_ids",
                    "reply_only",
                    "speech_as_media",
                    "voice_format",
                    "actions",
                    "message_action",
                }
            ),
            path,
        )
        iface_id = c.string(entry, "id", path, required=True)
        if iface_id and not INTERFACE_ID_RE.match(iface_id):
            c.add(
                f"{path}.id",
                f"must be a lowercase identifier (letters, digits, '_'), got {iface_id!r}",
            )
        paths = c.string_list(entry, "paths", path)
        for pattern in paths:
            if pattern.split("/", 1)[0] != iface_id:
                c.add(
                    f"{path}.paths",
                    f"path {pattern!r} must start with the interface id {iface_id!r}",
                )
            try:
                expand_pattern(pattern)
            except ValueError as exc:
                c.add(f"{path}.paths", f"invalid path pattern {pattern!r}: {exc}")
        suffix = c.string(entry, "bot_username_suffix", path)
        voice_format = c.string(entry, "voice_format", path) or ""
        if voice_format and voice_format not in VOICE_FORMATS:
            c.add(
                f"{path}.voice_format",
                f"must be one of {', '.join(sorted(VOICE_FORMATS))}, got {voice_format!r}",
            )
        iface_actions = c.string_list(entry, "actions", path)
        c.matching(iface_actions, IDENT_RE, f"{path}.actions", "action name")
        speaking = c.string(entry, "message_action", path)
        if speaking and speaking not in iface_actions:
            c.add(
                f"{path}.message_action",
                f"must be one of the interface's own actions, got {speaking!r}",
            )
        interfaces.append(
            InterfaceProvision(
                id=iface_id,
                paths=paths,
                user_facing=c.boolean(entry, "user_facing", path, default=True),
                voice_input_tts_only=c.boolean(entry, "voice_input_tts_only", path),
                negative_chat_id_is_group=c.boolean(
                    entry, "negative_chat_id_is_group", path
                ),
                bot_username_suffix=suffix or "",
                peer_ids=c.boolean(entry, "peer_ids", path),
                reply_only=c.boolean(entry, "reply_only", path),
                speech_as_media=c.boolean(entry, "speech_as_media", path),
                voice_format=voice_format,
                actions=iface_actions,
                message_action=speaking,
            )
        )
    ids = [item.id for item in interfaces]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        c.add("provides.interfaces", f"interface id {dup!r} is declared twice")

    actions = c.string_list(table, "actions", "provides")
    engines = c.string_list(table, "engines", "provides")
    hooks = c.string_list(table, "hooks", "provides")
    ui = _parse_ui(table, c)
    c.matching(actions, IDENT_RE, "provides.actions", "action name")
    c.matching(hooks, IDENT_RE, "provides.hooks", "hook name")
    endpoints = c.string_list(table, "endpoints", "provides")
    c.matching(endpoints, ENDPOINT_NAME_RE, "provides.endpoints", "endpoint name")
    for kind in engines:
        if kind not in ENGINE_KINDS:
            c.add(
                "provides.engines",
                f"unknown engine kind {kind!r} (allowed: {', '.join(sorted(ENGINE_KINDS))})",
            )
    recon = _parse_recon(table, c)
    protocols = _parse_protocols(table, c)
    vessels = _parse_vessels(table, c)
    web = _parse_web(table, c)
    if ui and (web is None or not web.static):
        c.add(
            "provides.ui",
            "windows are pages of the package: declare provides.web.static",
        )
    return Provides(
        interfaces=tuple(interfaces),
        actions=actions,
        engines=engines,
        hooks=hooks,
        ui=ui,
        web=web,
        endpoints=endpoints,
        recon=recon,
        protocols=protocols,
        vessels=vessels,
    )


#: Top-level paths of the Core's web server an alias must never take.
RESERVED_ALIASES = frozenset(
    {
        "api",
        "ws",
        "x",
        "static",
        "avatars",
        "templates",
        "res",
        "assets",
        "docs",
        "login",
        "logout",
        "favicon",
        "robots",
        "openapi",
        "redoc",
        "health",
    }
)
ALIAS_RE = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")


MAX_UI_ENTRIES = 8
UI_PAGE_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,127}$")


def _parse_ui(provides: Mapping[str, Any], c: _Collector) -> tuple[UiEntry, ...]:
    raw = provides.get("ui", [])
    if not isinstance(raw, list):
        c.add("provides.ui", "must be a list of tables ([[provides.ui]])")
        return ()
    if len(raw) > MAX_UI_ENTRIES:
        c.add("provides.ui", f"at most {MAX_UI_ENTRIES} windows")
    entries: list[UiEntry] = []
    for index, item in enumerate(raw[:MAX_UI_ENTRIES]):
        path = f"provides.ui[{index}]"
        table = c.table(item, path)
        if table is None or not isinstance(item, Mapping):
            continue
        c.unknown_keys(item, frozenset({"slot", "id", "title", "icon", "page"}), path)
        slot = c.string(item, "slot", path, required=True) or ""
        ident = c.string(item, "id", path, required=True) or ""
        title = c.string(item, "title", path, required=True) or ""
        icon = c.string(item, "icon", path) or ""
        page = c.string(item, "page", path) or "index.html"
        if slot and slot not in UI_SLOTS:
            c.add(f"{path}.slot", f"must be one of {', '.join(UI_SLOTS)}, got {slot!r}")
        if ident and not IDENT_RE.match(ident):
            c.add(f"{path}.id", f"must be a lowercase identifier, got {ident!r}")
        if not 0 < len(title) <= 40:
            c.add(f"{path}.title", "must be 1 to 40 characters")
        if len(icon) > 8:
            c.add(f"{path}.icon", "must be at most 8 characters (an emoji)")
        if not UI_PAGE_RE.match(page) or ".." in page.split("/"):
            c.add(f"{path}.page", f"must be a plain relative file path, got {page!r}")
        entries.append(UiEntry(slot, ident, title, icon, page))
    keys = [(e.slot, e.id) for e in entries]
    for dup in sorted({k for k in keys if keys.count(k) > 1}):
        c.add("provides.ui", f"window {dup[1]!r} is declared twice in slot {dup[0]!r}")
    return tuple(entries)


def _parse_recon(
    provides: Mapping[str, Any], c: _Collector
) -> tuple[ReconProvision, ...]:
    raw = provides.get("recon", [])
    if not isinstance(raw, list):
        c.add("provides.recon", "must be a list of tables ([[provides.recon]])")
        return ()
    if len(raw) > MAX_RECON:
        c.add("provides.recon", f"at most {MAX_RECON} questions")
    found: list[ReconProvision] = []
    for index, item in enumerate(raw[:MAX_RECON]):
        path = f"provides.recon[{index}]"
        table = c.table(item, path)
        if table is None or not isinstance(item, Mapping):
            continue
        c.unknown_keys(item, frozenset({"key", "instruction", "priority"}), path)
        key = c.string(item, "key", path, required=True)
        instruction = c.string(item, "instruction", path, required=True)
        priority = item.get("priority", 0)
        if key and not RECON_KEY_RE.match(key):
            c.add(f"{path}.key", f"must be a lowercase identifier, got {key!r}")
        if not len(instruction) <= 1500:
            c.add(f"{path}.instruction", "must be at most 1500 characters")
        if (
            isinstance(priority, bool)
            or not isinstance(priority, int)
            or not 0 <= priority <= 10
        ):
            c.add(f"{path}.priority", "must be an integer from 0 to 10")
            priority = 0
        found.append(ReconProvision(key, instruction, priority))
    keys = [r.key for r in found]
    for dup in sorted({k for k in keys if keys.count(k) > 1}):
        c.add("provides.recon", f"question {dup!r} is declared twice")
    return tuple(found)


def _parse_protocols(
    provides: Mapping[str, Any], c: _Collector
) -> tuple[ProtocolProvision, ...]:
    raw = provides.get("protocols", [])
    if not isinstance(raw, list):
        c.add("provides.protocols", "must be a list of tables ([[provides.protocols]])")
        return ()
    if len(raw) > MAX_PROTOCOLS:
        c.add("provides.protocols", f"at most {MAX_PROTOCOLS} protocols")
    found: list[ProtocolProvision] = []
    for index, item in enumerate(raw[:MAX_PROTOCOLS]):
        path = f"provides.protocols[{index}]"
        table = c.table(item, path)
        if table is None or not isinstance(item, Mapping):
            continue
        c.unknown_keys(item, frozenset({"name", "base", "ops"}), path)
        name = c.string(item, "name", path, required=True)
        base = c.string(item, "base", path)
        ops = c.string_list(item, "ops", path)
        if name and not PROTOCOL_NAME_RE.match(name):
            c.add(f"{path}.name", f"must be a lowercase identifier, got {name!r}")
        if base and not PROTOCOL_NAME_RE.match(base):
            c.add(f"{path}.base", f"must be a lowercase identifier, got {base!r}")
        if base and base == name:
            c.add(f"{path}.base", "a protocol cannot be its own base")
        unknown = sorted(set(ops) - PROTOCOL_OPS)
        if unknown:
            c.add(
                f"{path}.ops",
                f"unknown operations {unknown} (allowed: {', '.join(sorted(PROTOCOL_OPS))})",
            )
        if not ops:
            c.add(f"{path}.ops", "list at least one operation")
        found.append(ProtocolProvision(name, base, ops))
    names = [p.name for p in found]
    for dup in sorted({n for n in names if names.count(n) > 1}):
        c.add("provides.protocols", f"protocol {dup!r} is declared twice")
    return tuple(found)


def _parse_vessels(
    provides: Mapping[str, Any], c: _Collector
) -> tuple[VesselProvision, ...]:
    raw = provides.get("vessels", [])
    if not isinstance(raw, list):
        c.add("provides.vessels", "must be a list of tables ([[provides.vessels]])")
        return ()
    if len(raw) > MAX_VESSELS:
        c.add("provides.vessels", f"at most {MAX_VESSELS} connectors")
    found: list[VesselProvision] = []
    for index, item in enumerate(raw[:MAX_VESSELS]):
        path = f"provides.vessels[{index}]"
        table = c.table(item, path)
        if table is None or not isinstance(item, Mapping):
            continue
        c.unknown_keys(
            item, frozenset({"name", "label", "remembers_chat", "capabilities"}), path
        )
        name = c.string(item, "name", path, required=True)
        if name and not VESSEL_NAME_RE.match(name):
            c.add(f"{path}.name", f"must be a lowercase identifier, got {name!r}")
        flags = c.string_list(item, "capabilities", path)
        unknown = sorted(set(flags) - VESSEL_CAPABILITIES)
        if unknown:
            c.add(
                f"{path}.capabilities",
                f"unknown flags {unknown} (allowed: {', '.join(sorted(VESSEL_CAPABILITIES))})",
            )
        found.append(
            VesselProvision(
                name=name or "",
                label=c.string(item, "label", path) or "",
                remembers_chat=c.boolean(item, "remembers_chat", path),
                capabilities=flags,
            )
        )
    names = [v.name for v in found]
    for dup in sorted({n for n in names if names.count(n) > 1}):
        c.add("provides.vessels", f"connector {dup!r} is declared twice")
    return tuple(found)


def _parse_web(provides: Mapping[str, Any], c: _Collector) -> WebProvision | None:
    if "web" not in provides:
        return None
    table = c.table(provides.get("web"), "provides.web")
    if table is None:
        return None
    c.unknown_keys(
        table,
        frozenset({"static", "alias", "routes", "websockets", "same_origin"}),
        "provides.web",
    )
    static = c.string(table, "static", "provides.web") or ""
    alias = c.string(table, "alias", "provides.web") or ""
    routes = c.boolean(table, "routes", "provides.web")
    websockets = c.boolean(table, "websockets", "provides.web")
    same_origin = c.boolean(table, "same_origin", "provides.web")
    parts = static.replace("\\", "/").split("/") if static else []
    if static and (
        static.startswith("/") or ".." in parts or any(p in ("", ".") for p in parts)
    ):
        c.add(
            "provides.web.static", f"must be a plain relative directory, got {static!r}"
        )
    if alias:
        if not static:
            c.add("provides.web.alias", "an alias needs 'static'")
        if not ALIAS_RE.match(alias):
            c.add(
                "provides.web.alias",
                f"must be one lowercase path segment (2-32 of a-z, 0-9, '_', '-'), got {alias!r}",
            )
        elif alias in RESERVED_ALIASES:
            c.add("provides.web.alias", f"{alias!r} is reserved by the Core")
    if not (static or routes or websockets):
        c.add("provides.web", "declares neither 'static', 'routes' nor 'websockets'")
    return WebProvision(
        static=static,
        alias=alias,
        routes=routes,
        websockets=websockets,
        same_origin=same_origin,
    )


def _parse_requires(data: Mapping[str, Any], c: _Collector, own_id: str) -> Requires:
    table = c.table(data.get("requires"), "requires")
    if table is None:
        return Requires()
    c.unknown_keys(table, frozenset({"extensions", "services"}), "requires")
    deps: list[ExtensionDependency] = []
    raw = table.get("extensions", [])
    if not isinstance(raw, list):
        c.add("requires.extensions", "must be a list of tables")
        raw = []
    for index, item in enumerate(raw):
        path = f"requires.extensions[{index}]"
        if not isinstance(item, Mapping):
            c.add(path, "must be a table with an 'id'")
            continue
        c.unknown_keys(item, frozenset({"id", "version", "optional"}), path)
        dep_id = c.string(item, "id", path, required=True)
        if dep_id and not EXTENSION_ID_RE.match(dep_id):
            c.add(f"{path}.id", f"must look like 'publisher/name', got {dep_id!r}")
        if dep_id and dep_id == own_id:
            c.add(f"{path}.id", "an extension cannot depend on itself")
        spec = c.string(item, "version", path)
        if spec and not _valid_specifier(spec):
            c.add(f"{path}.version", f"is not a valid version specifier: {spec!r}")
        deps.append(
            ExtensionDependency(
                id=dep_id, version=spec, optional=c.boolean(item, "optional", path)
            )
        )
    dep_ids = [d.id for d in deps]
    for dup in sorted({i for i in dep_ids if dep_ids.count(i) > 1}):
        c.add("requires.extensions", f"dependency {dup!r} is declared twice")
    services = c.string_list(table, "services", "requires")
    c.matching(services, IDENT_RE, "requires.services", "service name")
    return Requires(extensions=tuple(deps), services=services)


def _parse_permissions(data: Mapping[str, Any], c: _Collector) -> Permissions:
    table = c.table(data.get("permissions"), "permissions")
    if table is None:
        return Permissions()
    c.unknown_keys(table, frozenset({"capabilities", "network"}), "permissions")
    capabilities = c.string_list(table, "capabilities", "permissions")
    network = c.string_list(table, "network", "permissions")
    c.matching(capabilities, CAPABILITY_RE, "permissions.capabilities", "capability")
    for item in network:
        match = NETWORK_RE.match(item)
        port = int(item.rsplit(":", 1)[1]) if match else 0
        if not match or not 1 <= port <= 65535:
            c.add(
                "permissions.network",
                f"invalid destination {item!r} (expected 'host:port', port 1-65535)",
            )
    return Permissions(capabilities=capabilities, network=network)


def _parse_python(data: Mapping[str, Any], c: _Collector) -> tuple[str, ...]:
    table = c.table(data.get("python"), "python")
    if table is None:
        return ()
    c.unknown_keys(table, frozenset({"requirements"}), "python")
    requirements = c.string_list(table, "requirements", "python")
    for item in requirements:
        try:
            Requirement(item)
        except InvalidRequirement:
            c.add("python.requirements", f"invalid requirement {item!r}")
    return requirements


_PY_TYPES: dict[str, tuple[type, ...]] = {
    "string": (str,),
    "int": (int,),
    "float": (int, float),
    "bool": (bool,),
}


def _parse_config(data: Mapping[str, Any], c: _Collector) -> tuple[ConfigField, ...]:
    raw = data.get("config")
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        c.add("config", "must be a table of tables, one per key")
        return ()
    fields: list[ConfigField] = []
    for key in sorted(raw):
        path = f"config.{key}"
        if not CONFIG_KEY_RE.match(key):
            c.add(path, "key must be UPPER_SNAKE_CASE")
        item = raw[key]
        if not isinstance(item, Mapping):
            c.add(path, "must be a table")
            continue
        c.unknown_keys(
            item,
            frozenset(
                {"type", "label", "description", "sensitive", "required", "default"}
            ),
            path,
        )
        kind = c.string(item, "type", path, default="string")
        if kind not in CONFIG_TYPES:
            c.add(
                f"{path}.type",
                f"unknown type {kind!r} (allowed: {', '.join(sorted(CONFIG_TYPES))})",
            )
            kind = "string"
        default = item.get("default")
        if default is not None:
            ok = isinstance(default, _PY_TYPES[kind]) and not (
                kind != "bool" and isinstance(default, bool)
            )
            if not ok:
                c.add(f"{path}.default", f"does not match type {kind!r}")
                default = None
        fields.append(
            ConfigField(
                key=key,
                type=kind,
                label=c.string(item, "label", path),
                description=c.string(item, "description", path),
                sensitive=c.boolean(item, "sensitive", path),
                required=c.boolean(item, "required", path),
                default=default,
            )
        )
    return tuple(fields)


def parse_manifest(data: Mapping[str, Any], *, source: str = "<manifest>") -> Manifest:
    """Validate parsed TOML data and return a :class:`Manifest`.

    Raises :class:`ManifestError` listing every problem found.
    """
    c = _Collector()
    c.unknown_keys(data, _TOP_LEVEL_KEYS, "")
    schema = data.get("schema", SCHEMA_VERSION)
    if schema != SCHEMA_VERSION or isinstance(schema, bool):
        c.add(
            "schema",
            f"unsupported schema version {schema!r} (supported: {SCHEMA_VERSION})",
        )

    ext = _parse_extension(data, c)
    core_api = _parse_compat(data, c)
    entry = _parse_entry(data, c)
    provides = _parse_provides(data, c)
    requires = _parse_requires(data, c, str(ext.get("id", "")))
    permissions = _parse_permissions(data, c)
    python_requirements = _parse_python(data, c)
    config = _parse_config(data, c)

    if c.errors:
        raise ManifestError(source, c.errors)
    return Manifest(
        id=ext["id"],
        name=ext["name"],
        version=ext["version"],
        description=ext["description"],
        authors=ext["authors"],
        license=ext["license"],
        experimental=ext["experimental"],
        core_api=core_api,
        entry=entry,
        provides=provides,
        requires=requires,
        permissions=permissions,
        python_requirements=python_requirements,
        config=config,
    )


def parse_manifest_text(text: str, *, source: str = "<manifest>") -> Manifest:
    """Parse TOML ``text`` and validate it."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ManifestError(source, [f"not valid TOML: {exc}"]) from exc
    return parse_manifest(data, source=source)


def load_manifest(path: Path) -> Manifest:
    """Read and validate the manifest file at ``path``."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise ManifestError(str(path), [f"cannot read file: {exc}"]) from exc
    return parse_manifest_text(text, source=str(path))
