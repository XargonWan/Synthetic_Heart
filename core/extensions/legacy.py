"""Carry an installation's old plugin settings over to the extensions that replaced them.

Telegram (bot and userbot), Fluxer and the OpenAI/Ollama API server used to be Core code
whose settings sit in the ``config`` table under their old names (``BOTFATHER_TOKEN``,
``TELEGRAM_*``, ``FLUXER_*``, ``OLLAMA_*``, the ``INTERFACE_ENABLED__*`` flags and the
Telegram login session). The extensions read their own keys, so without this step an
update would leave a working installation silent until someone typed everything again.

The rules, from the operator's brief:

* **What was really used is migrated**: an old component whose *mandatory* settings are
  missing never worked, so there is nothing to carry over and a package that is not a
  default is not fetched for it.
* **What was switched off stays off**: the extension is installed if it is a default but
  turned off.
* **A component that only had its defaults** is judged one by one (the API server ran
  without any setting, so it stays on; an interface without a token has nothing to do).
* **Nothing is overwritten**: a setting the extension already has a non-default value
  for is the operator's newer choice.

The decisions are pure (:func:`decide`); :func:`migrate` applies them through a small
port, so both are tested without a database. It runs once (``State.legacy_done``).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Protocol

FALSE_WORDS = frozenset({"false", "0", "no", "off", "disabled"})


@dataclass(frozen=True)
class Component:
    """An old in-tree component and the extension that replaces it."""

    extension_id: str
    #: Old config key -> the extension's own key.
    settings: Mapping[str, str]
    #: Old keys that must hold a value for the component to have ever worked.
    required: tuple[str, ...] = ()
    #: Old flags; any of them false means the operator switched it off.
    enabled_keys: tuple[str, ...] = ()
    #: ``(old config key, extension storage key)`` of a login session to move.
    session: tuple[str, str] | None = None
    #: Shipped in the image (installed by the first-start defaults). Others are fetched
    #: from a repository, and only for a component that was configured and on.
    default_package: bool = True


COMPONENTS: tuple[Component, ...] = (
    Component(
        "official/telegram_bot",
        {"BOTFATHER_TOKEN": "BOTFATHER_TOKEN"},
        required=("BOTFATHER_TOKEN",),
        enabled_keys=("INTERFACE_ENABLED__telegram_bot",),
    ),
    Component(
        "official/telegram",
        {
            "TELEGRAM_API_ID": "TELEGRAM_API_ID",
            "TELEGRAM_API_HASH": "TELEGRAM_API_HASH",
            "TELEGRAM_PHONE": "TELEGRAM_PHONE",
            "TELEGRAM_USE_SYNTH_AVATAR": "TELEGRAM_USE_SYNTH_AVATAR",
            "TELEGRAM_BACKLOG_MAX_AGE_SECONDS": "TELEGRAM_BACKLOG_MAX_AGE_SECONDS",
            "TELEGRAM_RECOVERY_WINDOW_SECONDS": "TELEGRAM_RECOVERY_WINDOW_SECONDS",
            "TELEGRAM_CALLS_ENABLED": "TELEGRAM_CALLS_ENABLED",
            "TELEGRAM_CALL_ALLOWED_IDS": "TELEGRAM_CALL_ALLOWED_IDS",
            "TELEGRAM_CALL_RX_RATE": "TELEGRAM_CALL_RX_RATE",
            "TELEGRAM_CALL_RX_CHANNELS": "TELEGRAM_CALL_RX_CHANNELS",
            "TELEGRAM_CALL_VIDEO_ENABLED": "TELEGRAM_CALL_VIDEO_ENABLED",
        },
        required=("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION"),
        enabled_keys=("INTERFACE_ENABLED__telegram",),
        session=("TELEGRAM_SESSION", "session"),
        default_package=False,
    ),
    Component(
        "official/fluxer",
        {
            "FLUXER_TOKEN": "FLUXER_TOKEN",
            "FLUXER_API_BASE_URL": "FLUXER_API_BASE_URL",
            "FLUXER_GATEWAY_URL": "FLUXER_GATEWAY_URL",
            "FLUXER_API_VERSION": "FLUXER_API_VERSION",
        },
        required=("FLUXER_TOKEN",),
        enabled_keys=("INTERFACE_ENABLED__fluxer_bot",),
    ),
    Component(
        "official/openai_api",
        {
            "OLLAMA_HOST": "OPENAI_API_HOST",
            "OLLAMA_MAX_HISTORY": "OPENAI_API_MAX_HISTORY",
            "OLLAMA_COMPLETION_TIMEOUT": "OPENAI_API_TIMEOUT",
        },
        # No mandatory setting: the server ran on its defaults unless it was switched off.
        enabled_keys=("OLLAMA_SERVER_ENABLED", "INTERFACE_ENABLED__ollama_serve"),
    ),
)


def old_keys() -> list[str]:
    """Every old config key the migration looks at."""
    keys: set[str] = set()
    for component in COMPONENTS:
        keys.update(component.settings)
        keys.update(component.required)
        keys.update(component.enabled_keys)
        if component.session:
            keys.add(component.session[0])
    return sorted(keys)


@dataclass
class Decision:
    extension_id: str
    #: Carry anything over at all (the old component was really used).
    migrate: bool = False
    #: Install it from a repository if it is not installed (non-default packages).
    install: bool = False
    #: The operator had switched it off: install it if needed, but leave it off.
    switch_off: bool = False
    settings: dict[str, str] = field(default_factory=dict)  # new key -> value
    session: tuple[str, str] | None = None  # (storage key, value)
    reason: str = ""


def _has(value: str | None) -> bool:
    return value is not None and value.strip() != ""


def decide(component: Component, rows: Mapping[str, str]) -> Decision:
    """What to do for one old component, given the old config rows."""
    decision = Decision(component.extension_id)
    off = any(
        (rows.get(key) or "").strip().lower() in FALSE_WORDS
        for key in component.enabled_keys
    )
    configured = all(_has(rows.get(key)) for key in component.required)
    if not configured:
        decision.reason = "its mandatory settings were never filled in: it never worked"
        return decision
    decision.migrate = True
    decision.switch_off = off
    decision.install = not component.default_package and not off
    if off and not component.default_package:
        decision.migrate = False
        decision.reason = "it was switched off: not fetched"
        return decision
    decision.settings = {
        new: rows[old] for old, new in component.settings.items() if _has(rows.get(old))
    }
    if component.session and _has(rows.get(component.session[0])):
        decision.session = (component.session[1], rows[component.session[0]])
    decision.reason = "switched off" if off else "in use"
    return decision


class LegacyPort(Protocol):
    def installed(self, extension_id: str) -> bool: ...

    def install(self, extension_id: str) -> str | None:
        """Install a non-default package from a trusted repository and grant what it
        declares. Returns an error text, or ``None``."""
        ...

    def set_enabled(self, extension_id: str, enabled: bool) -> None: ...

    async def copy_setting(self, extension_id: str, key: str, value: str) -> bool:
        """Set the extension's ``key`` unless it already holds a non-default value.
        Returns whether it was written."""
        ...

    async def copy_session(self, extension_id: str, key: str, value: str) -> bool:
        """Store the login session unless one is already there."""
        ...


@dataclass
class Report:
    lines: list[str] = field(default_factory=list)

    def add(self, line: str) -> None:
        self.lines.append(line)


async def migrate(
    rows: Mapping[str, str],
    port: LegacyPort,
    components: Iterable[Component] = COMPONENTS,
) -> Report:
    """Apply :func:`decide` for every component. Never raises for one component."""
    report = Report()
    for component in components:
        decision = decide(component, rows)
        ext = decision.extension_id
        if not decision.migrate:
            report.add(f"{ext}: nothing to carry over ({decision.reason})")
            continue
        try:
            if decision.install and not port.installed(ext):
                error = port.install(ext)
                if error:
                    report.add(
                        f"{ext}: was in use but could not be installed ({error}); "
                        "install it from Plugins > Store and its settings will be picked up"
                    )
                    continue
                report.add(f"{ext}: installed (it was in use)")
            if not port.installed(ext):
                report.add(f"{ext}: not installed, nothing carried over")
                continue
            written = [
                key
                for key, value in decision.settings.items()
                if await port.copy_setting(ext, key, value)
            ]
            if decision.session and await port.copy_session(ext, *decision.session):
                written.append("session")
            if decision.switch_off:
                port.set_enabled(ext, False)
            report.add(
                f"{ext}: {decision.reason}; carried over "
                f"{', '.join(written) if written else 'nothing new'}"
            )
        except Exception as exc:  # noqa: BLE001 - one component must not stop the others
            report.add(f"{ext}: migration failed ({type(exc).__name__}: {exc})")
    return report
