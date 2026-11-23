"""Install an engine's Python package the first time it is needed.

Local voice engines (Vosk STT, ...) are deliberately not base dependencies: they
are large and many deployments use a cloud engine instead. Selecting one must not
leave the user with "package is not installed, run uv add ..." — like the models,
which are downloaded on first use, the package is installed on first use.

Rules:

* Only requirements in :data:`ALLOWED_REQUIREMENTS` are ever installed. The module
  and requirement come from engine code, never from user input or a config value.
* The package goes into a persistent directory under the data root
  (``<data_root>/pylibs``, i.e. ``/config/pylibs`` in the container), which is
  appended — never prepended — to ``sys.path``, so the application environment is
  never shadowed. The app environment is not written to (it is often not writable
  by the runtime user) and ``pyproject.toml`` / ``uv.lock`` are never touched.
* ``AUTO_INSTALL_ENGINE_PACKAGES`` turns the behaviour off.
* A failed install is not retried for ``_RETRY_COOLDOWN_S`` seconds, so a missing
  network does not make every transcription wait for a doomed install.

The functions block (a package install takes seconds to minutes): call them from
a worker thread — or use :func:`ensure_package_async`.
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

from core.logging_utils import log_error, log_info, log_warning
from core.variables_engine import register_exposed_var

register_exposed_var(
    "AUTO_INSTALL_ENGINE_PACKAGES",
    label="Install Engine Packages On First Use",
    default=True,
    value_type=bool,
    ui_type="bool",
    description=(
        "When an engine whose Python package is not installed (e.g. the Vosk "
        "speech-to-text engine) is used for the first time, install the package "
        "automatically, like its model is downloaded."
    ),
    scope="core",
    component="package_installer",
    advanced=True,
)

#: module import name -> pip requirement. The only things this module installs.
ALLOWED_REQUIREMENTS: dict[str, str] = {
    "vosk": "vosk>=0.3.45",
    "faster_whisper": "faster-whisper>=0.8.1",
    # Telegram user-account calls (interface/telegram/telegram_call.py); pulls ntgcalls.
    "pytgcalls": "py-tgcalls>=3.0.0",
}

_INSTALL_TIMEOUT_S = 900
_RETRY_COOLDOWN_S = 600

_lock = threading.Lock()
_failed_at: dict[str, float] = {}


def extra_packages_dir() -> Path:
    """Directory on-demand packages are installed into (persistent with the data root)."""
    from core.app_paths import data_root

    return data_root() / "pylibs"


def activate_extra_packages() -> None:
    """Make packages installed on demand importable (appended to ``sys.path``)."""
    try:
        path = extra_packages_dir()
        if path.is_dir() and str(path) not in sys.path:
            sys.path.append(str(path))
            importlib.invalidate_caches()
    except Exception as exc:  # pragma: no cover - defensive
        log_warning(
            f"[package_installer] could not activate the extra packages dir: {exc}"
        )


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False


def _enabled() -> bool:
    try:
        from core.config_manager import config_registry

        return bool(
            config_registry.get_value(
                "AUTO_INSTALL_ENGINE_PACKAGES",
                True,
                value_type=bool,
                component="package_installer",
            )
        )
    except Exception:
        return True


def ensure_package(module: str) -> bool:
    """Make ``module`` importable, installing its allowlisted requirement if needed.

    Returns ``True`` when the module can be imported afterwards. Never raises.
    """
    activate_extra_packages()
    if _importable(module):
        return True

    requirement = ALLOWED_REQUIREMENTS.get(module)
    if requirement is None:
        log_warning(
            f"[package_installer] '{module}' is not an installable engine package"
        )
        return False
    if not _enabled():
        log_info(
            f"[package_installer] '{module}' is missing and AUTO_INSTALL_ENGINE_PACKAGES "
            "is off; not installing"
        )
        return False

    with _lock:
        # Another thread may have installed it while we waited for the lock.
        activate_extra_packages()
        if _importable(module):
            return True
        failed = _failed_at.get(module)
        if failed is not None and (time.monotonic() - failed) < _RETRY_COOLDOWN_S:
            return False

        uv = shutil.which("uv")
        if uv is None:
            log_error(
                f"[package_installer] cannot install '{requirement}': the 'uv' tool is "
                "not available in this environment"
            )
            _failed_at[module] = time.monotonic()
            return False

        target = extra_packages_dir()
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            log_error(f"[package_installer] cannot create {target}: {exc}")
            _failed_at[module] = time.monotonic()
            return False

        log_info(
            f"[package_installer] installing '{requirement}' into {target} "
            "(first use of this engine)..."
        )
        cmd = [
            uv,
            "pip",
            "install",
            "--python",
            sys.executable,
            "--target",
            str(target),
            requirement,
        ]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_INSTALL_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log_error(f"[package_installer] installing '{requirement}' failed: {exc}")
            _failed_at[module] = time.monotonic()
            return False

        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
            log_error(
                f"[package_installer] installing '{requirement}' failed "
                f"(exit {proc.returncode}): {' | '.join(tail)}"
            )
            _failed_at[module] = time.monotonic()
            return False

        activate_extra_packages()
        importlib.invalidate_caches()
        ok = _importable(module)
        if ok:
            _failed_at.pop(module, None)
            log_info(f"[package_installer] '{requirement}' installed")
        else:
            log_error(
                f"[package_installer] '{requirement}' installed but '{module}' still "
                "cannot be imported"
            )
            _failed_at[module] = time.monotonic()
        return ok


async def ensure_package_async(module: str) -> bool:
    """:func:`ensure_package` on a worker thread (keeps the event loop free)."""
    return await asyncio.to_thread(ensure_package, module)


# Packages installed by an earlier run are importable from the moment this module
# is imported (the core initializer imports it before any engine loads).
activate_extra_packages()


def reset_for_tests() -> None:
    """Forget remembered failures (tests)."""
    _failed_at.clear()
