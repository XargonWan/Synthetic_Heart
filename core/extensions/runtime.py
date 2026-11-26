"""The extension runtime: everything wired together (platform milestone M2).

:class:`ExtensionRuntime` owns the pieces built in M1 and M2 (manager, gateway,
launcher, supervisor) and gives the host two operations: :meth:`start` and
:meth:`stop`. It is **off by default** at run time: :func:`start_extension_runtime`
does nothing unless the ``EXTENSIONS_ENABLED`` configuration key is true, so
installing this code changes nothing for an existing deployment.

Directory layout under the runtime root (``<data root>/extensions``)::

    installed/<publisher>/<name>/synth-extension.toml   code of an extension
    data/<publisher>/<name>/                            its private data
    grants.json                                         what the user granted
    uids.json                                           numeric user per extension

"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from core.extensions.api import register_core_api
from core.extensions.avatar import AvatarLoader, register_avatar_api
from core.extensions.endpoints import EndpointPort, register_endpoint_api
from core.extensions.info import InfoLoader, register_info_api
from core.extensions.recon import ReconHost, attach_recon
from core.extensions.turns import TurnPort, register_turn_api
from core.extensions.voice import VoicePort, register_voice_api
from core.extensions.web import WebSocketHub, register_web_api
from core.extensions.calls import CallPorts, register_call_api
from core.extensions.gateway import AuditRecord, Gateway
from core.extensions.grants import FileGrantStore, GrantStore
from core.extensions.interfaces import InterfacePorts, register_interface_api
from core.extensions.panels import PanelStore, register_panel_api
from core.extensions.repo.config import StateStore
from core.extensions.launcher import Launcher
from core.extensions.media import (
    Classifier,
    MediaBridge,
    MediaLimits,
    OutboundResolver,
)
from core.extensions.manager import ExtensionManager
from core.extensions.ports import ApiPorts
from core.extensions.sandbox import RuntimeLayout, SandboxPolicy, secure_runtime_tree
from core.extensions.supervisor import RestartPolicy, State, Supervisor

LogFn = Callable[[str], None]


@dataclass
class StartReport:
    """What :meth:`ExtensionRuntime.start` did."""

    started: list[str] = field(default_factory=list)
    excluded: dict[str, list[str]] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    load_problems: list[str] = field(default_factory=list)


class ExtensionRuntime:
    """Scans, resolves, launches and supervises native extensions."""

    def __init__(
        self,
        root: Path,
        ports: ApiPorts,
        *,
        interface_ports: InterfacePorts | None = None,
        call_ports: CallPorts | None = None,
        avatar_loader: AvatarLoader | None = None,
        info_loader: InfoLoader | None = None,
        endpoint_port: EndpointPort | None = None,
        turn_port: TurnPort | None = None,
        voice_port: VoicePort | None = None,
        recon_host: ReconHost | None = None,
        media_root: Path | None = None,
        outbound_resolver: OutboundResolver | None = None,
        classifier: Classifier | None = None,
        media_limits: MediaLimits | None = None,
        grants: GrantStore | None = None,
        policy: SandboxPolicy | None = None,
        restart: RestartPolicy | None = None,
        extra_roots: Iterable[Path] = (),
        log: LogFn | None = None,
        warn: LogFn | None = None,
        on_stderr: Callable[[str, str], None] | None = None,
    ) -> None:
        self.layout = RuntimeLayout(root)
        self._log = log or (lambda _m: None)
        self._warn = warn or (lambda _m: None)
        self.grants = grants or FileGrantStore(root / "grants.json")
        self.manager = ExtensionManager()
        self.gateway = Gateway(
            grants=self.grants,
            on_audit=self._on_audit,
            on_error=lambda method, exc: self._warn(
                f"extension API {method} failed: {type(exc).__name__}"
            ),
        )
        register_core_api(self.gateway, ports)
        self.launcher = Launcher(
            self.gateway,
            self.layout,
            policy or SandboxPolicy(),
            on_stderr=on_stderr,
            on_warning=self._warn,
        )
        self.media: MediaBridge | None = None
        self.panels = PanelStore()
        #: What is installed, and whether it is on (``installed.json``, written by the
        #: installer). Extensions it does not list (a development folder) are on.
        self.installed = StateStore(root / "installed.json")
        #: Tells subscribers the avatar changed (``None`` until an avatar API exists).
        self.announce_avatar: Callable[[str | None], None] | None = None
        if avatar_loader is not None:
            self.announce_avatar = register_avatar_api(self.gateway, avatar_loader)
        if info_loader is not None:
            register_info_api(self.gateway, info_loader)
        if endpoint_port is not None:
            register_endpoint_api(self.gateway, endpoint_port)
        if turn_port is not None:
            register_turn_api(self.gateway, turn_port)
        if recon_host is not None:
            attach_recon(self.gateway, recon_host, warn=self._warn)
        self.web_hub = WebSocketHub()
        register_web_api(self.gateway, self.web_hub)
        self.gateway.on_attach.append(
            lambda session: session.on_close(
                lambda: self.web_hub.drop_extension(session.extension_id)
            )
        )
        if interface_ports is not None:
            if media_root is not None and outbound_resolver is not None:
                self.media = MediaBridge(
                    data_dir=self.layout.data_dir,
                    uid=self.launcher.uid_for,
                    inbound_root=media_root,
                    resolve_outbound=outbound_resolver,
                    classify=classifier,
                    limits=media_limits,
                )
                interface_ports.media = self.media
            register_interface_api(self.gateway, interface_ports)
            if voice_port is not None and self.media is not None:
                register_voice_api(self.gateway, voice_port, self.media)
            if call_ports is not None:
                register_call_api(
                    self.gateway,
                    call_ports,
                    interface_ports.messaging,
                    interface_ports.media,
                )
            register_panel_api(self.gateway, self.panels)
        self.supervisor = Supervisor(
            self.launcher,
            restart or RestartPolicy(),
            on_event=lambda ext, kind, detail: self._log(
                f"[extensions] {ext}: {kind}{' - ' + detail if detail else ''}"
            ),
        )
        self._extra_roots = tuple(extra_roots)
        self.started = False

    def _on_audit(self, record: AuditRecord) -> None:
        self._warn(
            f"[extensions] {record.extension_id} {record.outcome}: "
            f"{record.method} {record.detail}".rstrip()
        )

    # -------------------------------------------------------------- discovery

    def scan(self) -> list[str]:
        """Read every extension under ``installed/<publisher>/`` and extra roots."""
        installed = self.layout.root / "installed"
        roots: list[Path] = []
        if installed.is_dir():
            roots.extend(sorted(p for p in installed.iterdir() if p.is_dir()))
        roots.extend(self._extra_roots)
        for root in roots:
            self.manager.scan_directory(root)
        return [f"{i.source}: {i.message}" for i in self.manager.load_issues]

    # ------------------------------------------------------------------ run

    async def start(self) -> StartReport:
        """Scan, resolve and start every native extension that can run."""
        for change in secure_runtime_tree(self.layout):
            self._log(f"[extensions] tightened permissions: {change}")
        report = StartReport(load_problems=self.scan())
        if self.media is not None:
            try:
                pruned = await self.media.prune(list(self.manager.manifests))
                if any(pruned.values()):
                    self._log(f"[extensions] removed expired attachments: {pruned}")
            except Exception as exc:  # noqa: BLE001 - housekeeping must not block start-up
                self._warn(f"[extensions] attachment cleanup failed: {exc}")
        resolution = self.manager.resolve()
        for ext_id, issues in sorted(resolution.excluded.items()):
            report.excluded[ext_id] = [f"{i.code}: {i.message}" for i in issues]
        order = [
            ext_id
            for ext_id in resolution.order
            if self.manager.manifests[ext_id].entry is not None
            and self.installed.is_enabled(ext_id)
        ]
        for ext_id in order:
            source = self.manager.source_of(ext_id)
            install_dir = Path(source) if source and Path(source).is_dir() else None
            self.supervisor.register(self.manager.manifests[ext_id], install_dir)
        states = await self.supervisor.start_all(order)
        for ext_id, state in states.items():
            if state in (State.RUNNING, State.FINISHED):
                report.started.append(ext_id)
            else:
                status = self.supervisor.status()[ext_id]
                report.failed[ext_id] = str(status["last_error"] or state.value)
        self.started = True
        return report

    # --------------------------------------------------- live changes (Store)

    async def deactivate(self, extension_id: str) -> None:
        """Stop an extension and forget it (it is off or was uninstalled)."""
        if self.supervisor.known(extension_id):
            await self.supervisor.stop(extension_id)
            self.supervisor.forget(extension_id)
        self.manager.forget(extension_id)
        self.panels.drop_extension(extension_id)

    async def activate(self, extension_id: str) -> str:
        """(Re)read an installed extension and start it if it is on and can run.

        Returns ``"started"`` or why it was not started. Used after an install, an
        update, a rollback or switching an extension on.
        """
        await self.deactivate(extension_id)
        manifest = self.manager.add_directory(self.layout.install_dir(extension_id))
        if manifest is None:
            return "its manifest could not be read"
        if manifest.entry is None:
            return "it has no entry point"
        if not self.installed.is_enabled(extension_id):
            return "it is switched off"
        resolution = self.manager.resolve()
        if extension_id in resolution.excluded:
            return "; ".join(i.message for i in resolution.excluded[extension_id])
        self.supervisor.register(manifest, self.layout.install_dir(extension_id))
        state = await self.supervisor.start(extension_id)
        if state in (State.RUNNING, State.FINISHED):
            return "started"
        return str(self.supervisor.status()[extension_id]["last_error"] or state.value)

    async def stop(self) -> None:
        """Stop every extension and close every session."""
        await self.supervisor.stop_all()
        await self.gateway.close_all()
        self.started = False


# ---------------------------------------------------------------- host helpers

_runtime: ExtensionRuntime | None = None
_mount_task: asyncio.Future[bool] | None = None
_web_task: asyncio.Future[bool] | None = None
_store_task: asyncio.Future[bool] | None = None


def bundled_repository() -> Path | None:
    """The read-only repository baked into the image, if there is one.

    ``SYNTH_EXT_BUNDLED`` names it; otherwise ``<app>/extensions_bundled`` is used when it
    exists. First-start defaults install from it with no network."""
    import os

    override = os.environ.get("SYNTH_EXT_BUNDLED", "").strip()
    if override:
        return Path(override)
    candidate = Path(__file__).resolve().parents[2] / "extensions_bundled"
    return candidate if candidate.is_dir() else None


async def _migrate_legacy(root: Path) -> str:
    """Carry the old plugins' settings over to their extensions, once. Never raises."""
    try:
        from core.extensions.legacy import migrate, old_keys
        from core.extensions.legacy_port import CoreLegacyPort, read_rows
        from core.extensions.repo.installer import Installer

        installer = Installer(root, bundled=bundled_repository())
        state = installer.state.load()
        if state.legacy_done:
            return ""
        report = await migrate(await read_rows(old_keys()), CoreLegacyPort(installer))
        state = installer.state.load()
        state.legacy_done = True
        installer.state.save(state)
        return "[extensions] settings of the old plugins: " + "; ".join(report.lines)
    except Exception as exc:  # noqa: BLE001 - never stop start-up (and retry next time)
        return f"[extensions] legacy settings not migrated: {type(exc).__name__}: {exc}"


async def _install_defaults(root: Path) -> str:
    """Install the ``default_install`` extensions once. Never raises."""
    try:
        from core.extensions.repo.installer import Installer

        installer = Installer(root, bundled=bundled_repository())
        await asyncio.to_thread(installer.ensure_official_repository)
        installed, failed = await asyncio.to_thread(installer.ensure_defaults)
    except Exception as exc:  # noqa: BLE001 - defaults must never stop start-up
        return f"[extensions] default install skipped: {type(exc).__name__}: {exc}"
    if failed:
        return f"[extensions] default install: {', '.join(installed) or 'none'} ok; failed: {'; '.join(failed)}"
    return f"[extensions] default install: {', '.join(installed)}" if installed else ""


def get_extension_runtime() -> ExtensionRuntime | None:
    """The running runtime, or ``None`` if it was not started."""
    return _runtime


def extensions_enabled() -> bool:
    """The ``EXTENSIONS_ENABLED`` configuration flag (default: on)."""
    from core.config_manager import config_registry

    return bool(
        config_registry.get_value(
            "EXTENSIONS_ENABLED",
            True,
            label="Enable extensions",
            description=(
                "Run installed extensions in their own processes. "
                "On by default; switching it off stops every extension "
                "(interfaces such as Telegram and the OpenAI API are extensions)."
            ),
            value_type=bool,
            group="extensions",
            component="extensions",
        )
    )


async def start_extension_runtime() -> StartReport | None:
    """Start the runtime if ``EXTENSIONS_ENABLED`` is true. Idempotent.

    Never raises: a failure is logged and the Core carries on without extensions.
    """
    global _runtime
    from core.logging_utils import log_info, log_warning

    try:
        if not extensions_enabled():
            return None
        if _runtime is not None and _runtime.started:
            return None
        from core.app_paths import data_root
        from core.extensions.core_interfaces import (
            CoreReconHost,
            CoreTurns,
            CoreVoice,
            core_avatar,
            core_call_ports,
            core_classifier,
            core_endpoint_port,
            core_info,
            core_interface_ports,
            core_outbound_resolver,
        )
        from core.extensions.core_ports import core_api_ports

        root = data_root() / "extensions"
        defaults_note = await _install_defaults(root)
        legacy_note = await _migrate_legacy(root)
        runtime = ExtensionRuntime(
            root,
            core_api_ports(),
            interface_ports=core_interface_ports(),
            call_ports=core_call_ports(),
            avatar_loader=core_avatar,
            info_loader=core_info,
            endpoint_port=core_endpoint_port(),
            turn_port=CoreTurns(),
            voice_port=CoreVoice(),
            recon_host=CoreReconHost(),
            media_root=data_root() / "media" / "extensions",
            outbound_resolver=core_outbound_resolver,
            classifier=core_classifier,
            log=log_info,
            warn=log_warning,
            on_stderr=lambda ext, line: log_info(f"[ext:{ext}:stderr] {line}"),
        )
        report = await runtime.start()
        _runtime = runtime
        if runtime.announce_avatar is not None:
            from core import synth_avatar

            synth_avatar.add_change_listener(runtime.announce_avatar)
        from core.extensions.repo.installer import Installer
        from core.extensions.webui_panels import mount_when_ready
        from core.extensions.webui_web import mount_when_ready as mount_web
        from core.extensions.webui_store import mount_when_ready as mount_store

        global _mount_task
        _mount_task = asyncio.ensure_future(
            mount_when_ready(runtime.gateway, runtime.panels)
        )
        global _web_task
        _web_task = asyncio.ensure_future(
            mount_web(runtime.gateway, runtime.layout.install_dir, runtime.web_hub)
        )
        global _store_task
        _store_task = asyncio.ensure_future(
            mount_store(Installer(root, bundled=bundled_repository()), lambda: _runtime)
        )
        if defaults_note:
            log_info(defaults_note)
        if legacy_note:
            log_info(legacy_note)
        log_info(
            f"[extensions] started {len(report.started)} extension(s), "
            f"{len(report.excluded)} excluded, {len(report.failed)} failed"
        )
        return report
    except Exception as exc:  # noqa: BLE001 - extensions must never stop the Core
        log_warning(
            f"[extensions] runtime could not start: {type(exc).__name__}: {exc}"
        )
        return None


async def stop_extension_runtime() -> None:
    """Stop the runtime if it is running. Never raises."""
    global _runtime, _mount_task, _store_task, _web_task
    runtime, _runtime = _runtime, None
    if _web_task is not None:
        _web_task.cancel()
        _web_task = None
    if _store_task is not None:
        _store_task.cancel()
        _store_task = None
    if runtime is not None and runtime.announce_avatar is not None:
        from core import synth_avatar

        synth_avatar.remove_change_listener(runtime.announce_avatar)
    if _mount_task is not None:
        _mount_task.cancel()
        _mount_task = None
    if runtime is None:
        return
    try:
        await runtime.stop()
    except Exception as exc:  # noqa: BLE001
        from core.logging_utils import log_warning

        log_warning(f"[extensions] error while stopping: {type(exc).__name__}: {exc}")
