"""Resolve application-owned paths without assuming the container layout.

The Docker image mounts the application at ``/app`` and keeps persistent state
under ``/config``.  Native installs (Windows, bare-metal Linux) have neither
path, so every default that names them silently resolves to ``C:\\app`` or
``C:\\config`` and fails: the agent's file tools sandbox themselves to a
directory that does not exist, the radio plugin cannot create its audio
directory, TLS generation cannot create its certificate directory.

This module is the single place that decides those fallbacks.  It mirrors the
pattern already used in :func:`core.outbound_file_utils.allowed_file_roots` and
:func:`core.external_endpoints.crypto._resolve_secret_file`: prefer the explicit
environment override, keep the container path when it is actually writable, and
otherwise fall back inside the application root.

Resolution is entirely structural (environment first, then writability), never
derived from message content or any keyword list.
"""

from __future__ import annotations

import os
from pathlib import Path

#: The application root: the directory that contains ``core/``, ``plugins/`` and
#: ``main.py``.  Inside the container this is ``/app``; in a checkout it is the
#: repository root; in an installed tree it is the install directory.
APP_ROOT: Path = Path(__file__).resolve().parent.parent

#: The container's persistent-state mount.
CONTAINER_DATA_ROOT: Path = Path("/config")


def app_root() -> Path:
    """Return the resolved application root directory."""
    override = (os.getenv("SYNTH_APP_ROOT") or "").strip()
    if override:
        try:
            return Path(override).expanduser().resolve()
        except Exception:
            pass
    return APP_ROOT


def in_container() -> bool:
    """Return True when SyntH is running inside its container image.

    ``SYNTH_IN_CONTAINER`` wins when set.  Otherwise detection is structural: the
    image mounts the application at ``/app``, and a native install never does.
    On Windows ``"/app"`` is not even absolute, so the comparison cannot match by
    accident (the drive-relative ``\\app`` trap).

    Used to decide the deployment-appropriate default for host bindings and TLS:
    a container wants ``0.0.0.0`` and HTTPS, a desktop wants loopback and plain
    HTTP (no firewall prompt, no self-signed certificate warning).
    """
    override = (os.getenv("SYNTH_IN_CONTAINER") or "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    try:
        return str(app_root()).replace("\\", "/").rstrip("/") == "/app"
    except Exception:
        return False


def default_bind_host(env_var: str = "SYNTH_WEBUI_HOST") -> str:
    """Return the deployment-appropriate default bind address.

    ``0.0.0.0`` inside the container (the port is published anyway) and
    ``127.0.0.1`` natively, where binding every interface trips the Windows
    firewall prompt and exposes the WebUI to the local network.
    """
    override = (os.getenv(env_var) or "").strip()
    if override:
        return override
    return "0.0.0.0" if in_container() else "127.0.0.1"


def usable_container_dir(path: Path) -> bool:
    """Return True when *path* is a real, writable container directory.

    Deliberately read-only: resolving a path must never create anything. It also
    requires the path to be genuinely absolute, which rules out Windows: there
    ``Path("/config")`` is *drive-relative* (``\\config``, i.e. ``D:\\config``),
    so a Docker-era default silently captured state into the root of whatever
    drive the process happened to start on.
    """
    try:
        if not path.is_absolute():
            return False
        if not path.is_dir():
            return False
        return os.access(str(path), os.W_OK)
    except Exception:
        return False


def data_root() -> Path:
    """Return the directory for persistent generated state.

    ``SYNTH_DATA_ROOT`` wins when set.  Otherwise the container's ``/config`` is
    used when it exists and is writable, and ``<app_root>/data`` when it does not
    (a native install, where the container mount is absent).
    """
    override = (os.getenv("SYNTH_DATA_ROOT") or "").strip()
    if override:
        try:
            return Path(override).expanduser()
        except Exception:
            pass
    if usable_container_dir(CONTAINER_DATA_ROOT):
        return CONTAINER_DATA_ROOT
    return app_root() / "data"


def log_dir() -> Path:
    """Return the application log directory.

    ``SYNTH_LOG_DIR`` wins when set (the container sets it), then the container
    path when it exists, then ``<app_root>/logs``.
    """
    override = (os.getenv("SYNTH_LOG_DIR") or "").strip()
    if override:
        try:
            return Path(override).expanduser()
        except Exception:
            pass
    container_logs = Path("/app/logs")
    if usable_container_dir(container_logs):
        return container_logs
    return app_root() / "logs"


def agent_fs_roots() -> list[Path]:
    """Return the sandbox roots for the agent's filesystem tools.

    Order of precedence, matching :func:`core.outbound_file_utils.allowed_file_roots`:

    1. ``AGENT_FS_ROOTS`` (os.pathsep-separated list)
    2. ``AGENT_FS_ROOT`` and ``SYNTH_LOG_DIR``
    3. the application root and its ``logs`` directory

    Never the literal ``/app``: on Windows that resolves to ``C:\\app``, which
    does not exist, so every file tool call reports an empty sandbox.
    """
    raw = (os.getenv("AGENT_FS_ROOTS") or "").strip()
    if raw:
        candidates = [part.strip() for part in raw.split(os.pathsep) if part.strip()]
    else:
        root_override = (os.getenv("AGENT_FS_ROOT") or "").strip()
        log_override = (os.getenv("SYNTH_LOG_DIR") or "").strip()
        candidates = [
            root_override or str(app_root()),
            log_override or str(app_root() / "logs"),
        ]

    resolved: list[Path] = []
    for candidate in candidates:
        try:
            resolved.append(Path(candidate).expanduser().resolve())
        except Exception:
            continue
    return resolved


def cert_dir() -> Path:
    """Return the directory for the WebUI's self-signed TLS material."""
    override = (os.getenv("SYNTH_WEBUI_CERT_DIR") or "").strip()
    if override:
        try:
            return Path(override).expanduser()
        except Exception:
            pass
    return data_root() / "ssl"


def exposed_storage_root() -> Path:
    """Return the directory exposed for outbound file serving."""
    override = (os.getenv("SYNTH_EXPOSED_STORAGE_ROOT") or "").strip()
    if override:
        try:
            return Path(override).expanduser()
        except Exception:
            pass
    return data_root() / "storage"


def state_path(name: str) -> Path:
    """Return a path for a small JSON state file under the data root."""
    override = (os.getenv("SYNTH_STATE_DIR") or "").strip()
    base = Path(override).expanduser() if override else data_root()
    return base / name


__all__ = [
    "APP_ROOT",
    "CONTAINER_DATA_ROOT",
    "agent_fs_roots",
    "app_root",
    "cert_dir",
    "data_root",
    "default_bind_host",
    "exposed_storage_root",
    "in_container",
    "log_dir",
    "state_path",
    "usable_container_dir",
]
