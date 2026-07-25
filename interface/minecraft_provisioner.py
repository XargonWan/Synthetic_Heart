# interface/minecraft_provisioner.py
"""Provisioner for the Minecraft Vessel bridge.

Manages the lifecycle of the Node.js Mineflayer bridge
(:mod:`plugins/rift_vessel/minecraft/minecraft_bridge_minimal.js`) as a child
subprocess:

* :meth:`BridgeProvisioner.install` — ensure the bridge deps (``mineflayer``)
  are present in the bridge working directory (``npm install`` if missing).
* :meth:`BridgeProvisioner.start` / :meth:`stop` / :meth:`status` /
  :meth:`logs` — control the running bridge process.

Design constraints (see ``docs/rift_vessel.rst`` and issue #60):

* **Opt-in.** All actions are gated by whether the ``minecraft_vessel`` plugin
  is enabled (its WebUI card toggle, persisted as
  ``PLUGIN_ENABLED__minecraft_vessel``); when the plugin is disabled the
  provisioner refuses to install or start.
* **Non-root.** The subprocess inherits the current (non-root) user; we never
  escalate privileges.
* **Single-container PoC.** The bridge runs inside the same container as SyntH.
  Node LTS is provisioned conditionally (see the Dockerfile Node stage). If
  ``node``/``npm`` are missing the provisioner returns a clear error instead of
  crashing.
* **Cross-platform-guarded.** Linux-container-first; process control uses
  ``asyncio.create_subprocess_exec`` and POSIX signals, with a Windows fallback
  only where trivially safe.

State (PID, ports, started_at) is persisted to ``bridge.json`` in the bridge
working directory so ``status`` survives a SyntH restart.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict

from core.config_manager import config_registry
from core.logging_utils import log_error, log_info, log_warning

LOG_PREFIX = "[minecraft_provisioner]"

# Default install root for the bridge inside the container. Overridable so tests
# and non-container hosts can point it at a writable location.
_DEFAULT_BRIDGE_ROOT = "/opt/minecraft_bridge"

# Source of the bridge script inside the repo. It lives next to the Minecraft
# connector (``plugins/rift_vessel/minecraft/``) so all Minecraft Vessel assets
# stay together; the provisioner copies it into the bridge working directory.
_BRIDGE_SRC_RELATIVE = (
    Path("plugins") / "rift_vessel" / "minecraft" / "minecraft_bridge_minimal.js"
)

# npm dependencies required by the bridge. ``mineflayer-pathfinder`` powers the
# navigation verbs (``follow``/``goto``/``wander``); without it the bot connects
# but cannot pathfind ("navigation unavailable"). ``minecraft-data`` powers the
# pathfinder Movements block/tool costs (it is normally a transitive dependency
# of mineflayer, but the bridge ``require``s it directly, so pin it explicitly).
_BRIDGE_NPM_DEPS = ["mineflayer", "mineflayer-pathfinder", "minecraft-data"]


class BridgeProvisioner:
    """Install and control the Minecraft Vessel bridge subprocess."""

    def __init__(self, bridge_root: str | None = None) -> None:
        self._bridge_root = Path(
            bridge_root or os.environ.get("MINECRAFT_BRIDGE_ROOT", _DEFAULT_BRIDGE_ROOT)
        )
        self._state_file = self._bridge_root / "bridge.json"
        self._log_file = self._bridge_root / "bridge.log"
        self._bridge_script = self._bridge_root / "minecraft_bridge_minimal.js"
        self._proc: asyncio.subprocess.Process | None = None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _repo_root(self) -> Path:
        # interface/minecraft_provisioner.py -> repo root is parent of "interface".
        return Path(__file__).resolve().parent.parent

    def _bridge_src(self) -> Path:
        return self._repo_root() / _BRIDGE_SRC_RELATIVE

    def _is_enabled(self) -> bool:
        """Return whether the ``minecraft_vessel`` plugin is enabled.

        The bridge lifecycle is gated by the plugin's own WebUI card toggle
        (``PLUGIN_ENABLED__minecraft_vessel``) rather than a dedicated key, so a
        single switch controls both the plugin and its bridge. Defaults to
        ``True`` (the plugin card toggle defaults to enabled).
        """
        try:
            return bool(
                config_registry.get_value(
                    "PLUGIN_ENABLED__minecraft_vessel",
                    True,
                    value_type=bool,
                    component="minecraft_vessel",
                    group="plugins",
                    hidden=True,
                )
            )
        except Exception:
            return True

    def _bridge_env(self) -> Dict[str, str]:
        env = dict(os.environ)

        def _cfg(key: str, default: str) -> str:
            try:
                val = config_registry.get_value(key, default)
            except Exception:
                val = None
            return str(val) if val is not None else default

        # The bot's in-world username defaults to Synth's configured name; an
        # explicit MINECRAFT_BOT_USERNAME_OVERRIDE (when non-empty) wins.
        username_override = _cfg("MINECRAFT_BOT_USERNAME_OVERRIDE", "").strip()
        bot_username = username_override or _cfg("SYNTH_NAME", "Synth")

        env.update(
            {
                "BRIDGE_HOST": _cfg("MINECRAFT_BRIDGE_HOST", "127.0.0.1"),
                "BRIDGE_PORT": _cfg("MINECRAFT_BRIDGE_PORT", "8137"),
                "MC_SERVER_HOST": _cfg("MINECRAFT_SERVER_HOST", "127.0.0.1"),
                "MC_SERVER_PORT": _cfg("MINECRAFT_SERVER_PORT", "44383"),
                "MC_BOT_USERNAME": bot_username,
                "MC_AUTH": "offline",
                # Optional protocol-version pin (empty = Mineflayer auto-detect).
                # Set MINECRAFT_SERVER_VERSION when the server announces a
                # version the bundled minecraft-data doesn't know.
                "MC_VERSION": _cfg("MINECRAFT_SERVER_VERSION", ""),
            }
        )
        return env

    def _read_state(self) -> Dict[str, Any]:
        try:
            if self._state_file.exists():
                return json.loads(self._state_file.read_text(encoding="utf-8"))
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} Failed to read state file: {exc}")
        return {}

    def _write_state(self, state: Dict[str, Any]) -> None:
        try:
            self._bridge_root.mkdir(parents=True, exist_ok=True)
            self._state_file.write_text(json.dumps(state, indent=2), encoding="utf-8")
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} Failed to write state file: {exc}")

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        if pid <= 0:
            return False
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            # Exists but owned by another user.
            return True
        except OSError:
            return False

    def _running_pid(self) -> int | None:
        if self._proc is not None and self._proc.returncode is None:
            return self._proc.pid
        state = self._read_state()
        pid = int(state.get("pid", 0) or 0)
        if pid and self._pid_alive(pid):
            return pid
        return None

    # ------------------------------------------------------------------
    # install
    # ------------------------------------------------------------------

    async def install(self) -> Dict[str, Any]:
        """Copy the bridge script and install its npm deps into the bridge root."""
        if not self._is_enabled():
            return {
                "ok": False,
                "detail": "minecraft_vessel plugin is disabled (enable it in the WebUI)",
            }

        npm = shutil.which("npm")
        node = shutil.which("node")
        if not node or not npm:
            return {
                "ok": False,
                "detail": (
                    "node/npm not found in PATH — the runtime image needs the "
                    "conditional Node stage enabled for the Minecraft Vessel"
                ),
            }

        src = self._bridge_src()
        if not src.exists():
            return {"ok": False, "detail": f"bridge source missing: {src}"}

        try:
            self._bridge_root.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, self._bridge_script)
        except Exception as exc:
            return {"ok": False, "detail": f"failed to copy bridge script: {exc}"}

        # Ensure a package.json exists so npm install is well-behaved.
        pkg = self._bridge_root / "package.json"
        if not pkg.exists():
            try:
                pkg.write_text(
                    json.dumps(
                        {
                            "name": "synth-minecraft-bridge",
                            "version": "0.1.0",
                            "private": True,
                            "description": "SyntH Rift Vessel Minecraft bridge",
                            "main": "minecraft_bridge_minimal.js",
                        },
                        indent=2,
                    ),
                    encoding="utf-8",
                )
            except Exception as exc:
                return {
                    "ok": False,
                    "detail": f"failed to write package.json: {exc}",
                }

        # Reinstall unless *every* required dependency is already present.
        # Checking only ``mineflayer`` would leave older bridge installs (which
        # predate ``mineflayer-pathfinder``) permanently without navigation, so
        # the bot would connect but never move/follow.
        missing = [
            dep
            for dep in _BRIDGE_NPM_DEPS
            if not (self._bridge_root / "node_modules" / dep).exists()
        ]
        if not missing:
            return {
                "ok": True,
                "detail": "already installed",
                "bridge_root": str(self._bridge_root),
            }

        proc = await asyncio.create_subprocess_exec(
            npm,
            "install",
            "--no-audit",
            "--no-fund",
            *_BRIDGE_NPM_DEPS,
            cwd=str(self._bridge_root),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=600)
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "detail": "npm install timed out (600s)"}

        out = (stdout or b"").decode("utf-8", errors="replace")[-4000:]
        if proc.returncode != 0:
            log_error(f"{LOG_PREFIX} npm install failed:\n{out}")
            return {
                "ok": False,
                "detail": f"npm install failed (exit {proc.returncode})",
                "output": out,
            }

        log_info(f"{LOG_PREFIX} bridge installed in {self._bridge_root}")
        return {
            "ok": True,
            "detail": "installed",
            "bridge_root": str(self._bridge_root),
            "output": out,
        }

    # ------------------------------------------------------------------
    # start / stop / status / logs
    # ------------------------------------------------------------------

    async def start(self) -> Dict[str, Any]:
        """Start the bridge subprocess if not already running."""
        if not self._is_enabled():
            return {
                "ok": False,
                "detail": "minecraft_vessel plugin is disabled (enable it in the WebUI)",
            }

        pid = self._running_pid()
        if pid:
            return {"ok": True, "detail": "already running", "pid": pid}

        node = shutil.which("node")
        if not node:
            return {"ok": False, "detail": "node not found in PATH"}

        if not self._bridge_script.exists():
            install_res = await self.install()
            if not install_res.get("ok"):
                return install_res

        try:
            self._bridge_root.mkdir(parents=True, exist_ok=True)
            log_fh = open(self._log_file, "ab")  # noqa: SIM115 - kept open for child
        except Exception as exc:
            return {"ok": False, "detail": f"failed to open log file: {exc}"}

        try:
            self._proc = await asyncio.create_subprocess_exec(
                node,
                str(self._bridge_script),
                cwd=str(self._bridge_root),
                env=self._bridge_env(),
                stdout=log_fh,
                stderr=log_fh,
                start_new_session=(sys.platform != "win32"),
            )
        except Exception as exc:
            try:
                log_fh.close()
            except Exception:
                pass
            return {"ok": False, "detail": f"failed to start bridge: {exc}"}
        finally:
            # The child inherits the fd; the parent handle can be closed.
            try:
                log_fh.close()
            except Exception:
                pass

        state = {
            "pid": self._proc.pid,
            "started_at": time.time(),
            "bridge_host": self._bridge_env().get("BRIDGE_HOST"),
            "bridge_port": self._bridge_env().get("BRIDGE_PORT"),
        }
        self._write_state(state)
        log_info(f"{LOG_PREFIX} bridge started (pid={self._proc.pid})")
        return {"ok": True, "detail": "started", "pid": self._proc.pid}

    async def stop(self) -> Dict[str, Any]:
        """Stop the running bridge subprocess (idempotent)."""
        pid = self._running_pid()
        if not pid:
            self._write_state({})
            return {"ok": True, "detail": "not running"}

        try:
            if self._proc is not None and self._proc.returncode is None:
                self._proc.terminate()
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=10)
                except asyncio.TimeoutError:
                    self._proc.kill()
            else:
                # Started in a previous process lifetime — signal by PID.
                os.kill(pid, signal.SIGTERM)
                for _ in range(20):
                    if not self._pid_alive(pid):
                        break
                    await asyncio.sleep(0.5)
                if self._pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        except Exception as exc:
            log_warning(f"{LOG_PREFIX} error stopping bridge: {exc}")

        self._proc = None
        self._write_state({})
        log_info(f"{LOG_PREFIX} bridge stopped")
        return {"ok": True, "detail": "stopped"}

    def status(self) -> Dict[str, Any]:
        """Return a snapshot of the bridge status."""
        pid = self._running_pid()
        state = self._read_state()
        return {
            "ok": True,
            "enabled": self._is_enabled(),
            "running": bool(pid),
            "pid": pid,
            "bridge_root": str(self._bridge_root),
            "started_at": state.get("started_at"),
            "installed": self._bridge_script.exists()
            and all(
                (self._bridge_root / "node_modules" / dep).exists()
                for dep in _BRIDGE_NPM_DEPS
            ),
        }

    def logs(self, lines: int = 100) -> Dict[str, Any]:
        """Return the last ``lines`` lines of the bridge log file."""
        lines = max(1, min(int(lines), 2000))
        if not self._log_file.exists():
            return {"ok": True, "lines": [], "detail": "no log file yet"}
        try:
            content = self._log_file.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            return {"ok": False, "detail": f"failed to read log: {exc}"}
        tail = content.splitlines()[-lines:]
        return {"ok": True, "lines": tail}


# Module-level singleton.
bridge_provisioner = BridgeProvisioner()


def get_bridge_provisioner() -> BridgeProvisioner:
    """Return the shared :class:`BridgeProvisioner` singleton."""
    return bridge_provisioner
