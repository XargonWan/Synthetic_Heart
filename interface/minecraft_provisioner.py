# interface/minecraft_provisioner.py
"""Provisioner for the Minecraft Vessel bridge.

Manages the lifecycle of the Node.js Mineflayer bridge
(:mod:`plugins/rift_vessel/minecraft/minecraft_bridge.js`) as a child
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

# The Minecraft Vessel folder inside the repo. Every Minecraft-specific asset
# (the bridge script *and*, now, its Node runtime) lives here so the plugin is
# self-contained: a future plugin store can ship/remove the whole folder as one
# unit without touching a shared system path (see TODO — "il binario mineflayer
# deve essere nella cartella del plugin e chiamato da li").
_MINECRAFT_PLUGIN_DIR = Path("plugins") / "rift_vessel" / "minecraft"

# Name of the bridge script within the plugin folder.
_BRIDGE_SCRIPT_NAME = "minecraft_bridge.js"

# The bridge's Node runtime lives in a ``mineflayer`` sub-folder *inside the
# plugin folder* (``plugins/rift_vessel/minecraft/mineflayer/``) so the plugin
# is a self-contained, distributable package: a plugin store can ship the whole
# ``plugins/rift_vessel/minecraft/`` tree (e.g. as a zip) with its Node runtime
# already inside, and the bridge is always "called from there" — never a shared
# ``/opt`` path that is lost on container recreate. The folder's ``package.json``
# is committed as source (part of the package); only its ``node_modules`` are a
# build artefact (covered by the global ``node_modules/`` gitignore rule).
_BRIDGE_RUNTIME_SUBDIR = "mineflayer"

# Source of the bridge script inside the repo. It lives next to the Minecraft
# connector (``plugins/rift_vessel/minecraft/``) so all Minecraft Vessel assets
# stay together.
_BRIDGE_SRC_RELATIVE = _MINECRAFT_PLUGIN_DIR / _BRIDGE_SCRIPT_NAME

# npm dependencies required by the bridge. ``mineflayer-pathfinder`` powers the
# navigation verbs (``follow``/``goto``/``wander``); without it the bot connects
# but cannot pathfind ("navigation unavailable"). ``minecraft-data`` powers the
# pathfinder Movements block/tool costs (it is normally a transitive dependency
# of mineflayer, but the bridge ``require``s it directly, so pin it explicitly).
_BRIDGE_NPM_DEPS = ["mineflayer", "mineflayer-pathfinder", "minecraft-data"]


class BridgeProvisioner:
    """Install and control the Minecraft Vessel bridge subprocess."""

    def __init__(self, bridge_root: str | None = None) -> None:
        # Default bridge root = the plugin's own ``mineflayer`` folder, so the
        # Node runtime is self-contained within the plugin package. Overridable
        # via ``MINECRAFT_BRIDGE_ROOT`` (tests / non-container hosts) for a
        # writable location.
        env_override = os.environ.get("MINECRAFT_BRIDGE_ROOT")
        # ``_explicit_root`` marks tests / hosts that pin their own writable
        # location; in that mode we preserve the historical contract where the
        # bridge script is *copied* into the root and executed from there.
        self._explicit_root = bool(bridge_root or env_override)
        if bridge_root:
            self._bridge_root = Path(bridge_root)
        elif env_override:
            self._bridge_root = Path(env_override)
        else:
            self._bridge_root = (
                Path(__file__).resolve().parent.parent
                / _MINECRAFT_PLUGIN_DIR
                / _BRIDGE_RUNTIME_SUBDIR
            )
        self._state_file = self._bridge_root / "bridge.json"
        self._log_file = self._bridge_root / "bridge.log"
        # Default mode: run the bridge script *in place* from the plugin folder
        # (the runtime dir only holds Node deps). Explicit-root mode: the script
        # lives inside the root, copied there by :meth:`install` (historical).
        if self._explicit_root:
            self._bridge_script = self._bridge_root / _BRIDGE_SCRIPT_NAME
        else:
            self._bridge_script = self._bridge_src()
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

        # The bridge script lives in the plugin folder while its node_modules
        # live in the ``mineflayer`` sub-folder, so Node's default require
        # resolution (which walks up from the *script's* directory) would miss
        # them. Point NODE_PATH at the runtime's node_modules so ``require`` of
        # mineflayer & friends resolves regardless of where the script sits.
        node_modules = self._bridge_root / "node_modules"
        existing_node_path = env.get("NODE_PATH", "")
        env["NODE_PATH"] = (
            f"{node_modules}{os.pathsep}{existing_node_path}"
            if existing_node_path
            else str(node_modules)
        )
        # Cap the Node heap as a safety belt against the mineflayer chunk-cache
        # memory growth (see the viewDistance note in the bridge script). Without
        # a cap V8 lets the old-space climb toward the ~4 GB default limit where
        # mark-compact becomes ineffective and the process OOM-crashes; a tight
        # cap forces aggressive GC well before that. Preserve any caller-provided
        # NODE_OPTIONS.
        existing_node_options = env.get("NODE_OPTIONS", "").strip()
        heap_opt = "--max-old-space-size=512"
        env["NODE_OPTIONS"] = (
            f"{existing_node_options} {heap_opt}".strip()
            if existing_node_options
            else heap_opt
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

    @staticmethod
    def _pid_is_bridge(pid: int) -> bool:
        """Return whether ``pid`` is actually *our* Mineflayer bridge process.

        A bare :meth:`_pid_alive` check is not enough: after the container is
        recreated (``docker compose up -d --build``) the old bridge PID recorded
        in ``bridge.json`` no longer belongs to the bridge, yet the same numeric
        PID is almost always reassigned to an unrelated init/system process in
        the fresh container — so ``os.kill(pid, 0)`` succeeds and ``start()``
        wrongly reports "already running", never launching the bridge (the exact
        `Cannot connect to host 127.0.0.1:8137` failure). We therefore confirm
        the process command line references the bridge script via
        ``/proc/<pid>/cmdline`` (Linux). On platforms without ``/proc`` we
        conservatively fall back to bare liveness so behaviour is unchanged.
        """
        if pid <= 0:
            return False
        cmdline_path = Path("/proc") / str(pid) / "cmdline"
        if not cmdline_path.exists():
            # No /proc (non-Linux) — cannot inspect; fall back to liveness.
            return BridgeProvisioner._pid_alive(pid)
        try:
            raw = cmdline_path.read_bytes()
        except (ProcessLookupError, FileNotFoundError):
            return False
        except Exception:
            # Unreadable (e.g. owned by another user) — fall back to liveness.
            return BridgeProvisioner._pid_alive(pid)
        cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
        return "minecraft_bridge.js" in cmdline

    @staticmethod
    def _find_bridge_pids() -> list[int]:
        """Return every PID on /proc whose cmdline runs the bridge script.

        Used by the reaper as a fuser-free fallback: the slim synth container
        ships no ``fuser``/``psmisc``, so when a stale orphan bridge holds the
        port under a PID we no longer track, scanning ``/proc`` is the only way
        to find and kill it before a fresh launch dies with ``EADDRINUSE``.
        Best-effort — non-Linux hosts (no ``/proc``) simply yield an empty list.
        """
        proc_root = Path("/proc")
        if not proc_root.exists():
            return []
        pids: list[int] = []
        try:
            entries = list(proc_root.iterdir())
        except Exception:
            return []
        for entry in entries:
            name = entry.name
            if not name.isdigit():
                continue
            try:
                raw = (entry / "cmdline").read_bytes()
            except Exception:
                continue
            cmdline = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
            if "minecraft_bridge.js" in cmdline:
                pids.append(int(name))
        return pids

    def _running_pid(self) -> int | None:
        if self._proc is not None and self._proc.returncode is None:
            return self._proc.pid
        state = self._read_state()
        pid = int(state.get("pid", 0) or 0)
        if pid and self._pid_is_bridge(pid):
            return pid
        return None

    def _deps_present(self) -> bool:
        """Return whether every required npm dep is installed in the runtime."""
        return all(
            (self._bridge_root / "node_modules" / dep).exists()
            for dep in _BRIDGE_NPM_DEPS
        )

    def _bridge_port(self) -> int:
        try:
            return int(self._bridge_env().get("BRIDGE_PORT", "8137"))
        except (TypeError, ValueError):
            return 8137

    def _bridge_host(self) -> str:
        return self._bridge_env().get("BRIDGE_HOST", "127.0.0.1") or "127.0.0.1"

    async def _probe_health(self) -> Dict[str, Any] | None:
        """GET ``/health`` from a bridge already listening on our port.

        Returns the parsed JSON dict, or ``None`` if nothing answers (no live
        bridge on the port). Best-effort — any error yields ``None``.
        """
        host = self._bridge_host()
        port = self._bridge_port()
        try:
            import aiohttp

            timeout = aiohttp.ClientTimeout(total=2)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"http://{host}:{port}/health") as resp:
                    return await resp.json()
        except Exception:
            return None

    async def _reap_orphan_bridge(self) -> bool:
        """Kill any orphan/unhealthy bridge holding our port, so we can restart.

        The recurring "missing the 'mineflayer' Node module" failure that forced
        a container restart is caused by a *stale* bridge process: an earlier
        bridge started before ``npm install`` finished (or before the deps
        existed), so its one-shot ``require('mineflayer')`` failed permanently.
        That process keeps ``/health`` answering ``mineflayer:false`` and holds
        the port, while our recorded PID in ``bridge.json`` points elsewhere —
        so :meth:`start` never recognises it, launches a fresh process that dies
        with ``EADDRINUSE``, and the connector keeps reading the sick bridge.

        This reaper detects that situation structurally — a live ``/health``
        that reports ``mineflayer:false`` — and terminates it via ``fuser``/PID
        so a clean bridge can bind. A bridge answering ``mineflayer:true`` is
        healthy and is NEVER reaped, regardless of whether its PID is tracked
        (an empty ``bridge.json`` must not be mistaken for an orphan). Returns
        ``True`` if something was reaped.
        """
        health = await self._probe_health()
        if health is None:
            return False  # nothing answering — port is free for a fresh start.

        if health.get("mineflayer", False):
            # A bridge answering /health with mineflayer:true is HEALTHY — its
            # Node module loaded and it is (or is about to be) embodied. NEVER
            # reap it, whether or not we currently track its PID.
            #
            # The recurring ~30s session death (bridge.log: ``logged in as
            # Rekku`` immediately followed by ``shutting down``) was caused here:
            # ``bridge.json`` frequently comes back empty across provisioner
            # calls, so ``_running_pid()`` returns None and the old
            # ``if tracked and mineflayer`` guard fell through to the kill branch
            # — SIGTERM-ing a freshly-logged-in, perfectly healthy bridge as if
            # it were an untracked orphan. A healthy bridge is defined solely by
            # its /health reporting mineflayer:true, not by whether we track it.
            return False

        # The bridge is sick (answers /health but mineflayer:false — its
        # one-shot require('mineflayer') failed). Kill whatever holds the port
        # so start() can bind cleanly.
        port = self._bridge_port()
        killed = False

        fuser = shutil.which("fuser")
        if fuser is not None:
            try:
                proc = await asyncio.create_subprocess_exec(
                    fuser,
                    "-k",
                    f"{port}/tcp",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await asyncio.wait_for(proc.wait(), timeout=10)
                killed = True
            except Exception as exc:
                log_warning(f"{LOG_PREFIX} fuser reap failed: {exc}")

        if not killed:
            # Fallback when fuser is unavailable (e.g. the slim synth container
            # ships no fuser/psmisc). Kill the PID we track AND every process on
            # /proc whose cmdline runs the bridge script — a stale orphan bridge
            # left over from a prior connect can hold the port under a PID we no
            # longer track, which is exactly what triggers the EADDRINUSE crash
            # on the next launch.
            pids = set(self._find_bridge_pids())
            tracked = self._running_pid()
            if tracked:
                pids.add(tracked)
            for pid in pids:
                try:
                    os.kill(pid, signal.SIGTERM)
                    killed = True
                except Exception:
                    pass

        # Give the OS a moment to release the socket.
        for _ in range(20):
            if await self._probe_health() is None:
                break
            await asyncio.sleep(0.25)

        if killed:
            log_info(
                f"{LOG_PREFIX} reaped stale/unhealthy bridge on port {port} "
                "(mineflayer not loaded)"
            )
            self._proc = None
            self._write_state({})
        return killed

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
            # In default (self-contained) mode the script is executed in place
            # from the plugin folder, so no copy is needed. In explicit-root
            # mode (tests / pinned host path) copy it into the root, preserving
            # the historical contract.
            if self._explicit_root:
                shutil.copyfile(src, self._bridge_script)
        except Exception as exc:
            return {"ok": False, "detail": f"failed to copy bridge script: {exc}"}

        # The ``mineflayer`` package folder ships a committed ``package.json``
        # (part of the distributable plugin package), so npm install is
        # well-behaved and reproducible. Only write one as a defensive fallback
        # if it is somehow absent (e.g. explicit-root mode / a partial copy).
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
                            "main": "minecraft_bridge.js",
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

        # Detect an already-running bridge by probing the PORT, not our tracked
        # PID. ``bridge.json`` frequently comes back empty across provisioner
        # calls (the child is start_new_session and self._proc is lost between
        # process lifetimes), so a PID-gated check would miss a perfectly
        # healthy bridge and spawn a duplicate that dies with EADDRINUSE — the
        # churn behind the recurring ~30s session death. A bridge answering
        # /health with mineflayer:true is authoritative: adopt it.
        health = await self._probe_health()
        if health is not None and health.get("mineflayer", False):
            pid = self._running_pid()
            return {"ok": True, "detail": "already running", "pid": pid}
        if health is not None:
            # Something answers but reports mineflayer:false — a sick bridge.
            # Reap it so we relaunch a healthy one.
            await self._reap_orphan_bridge()

        node = shutil.which("node")
        if not node:
            return {"ok": False, "detail": "node not found in PATH"}

        # Ensure the bridge is fully provisioned before launch. Previously
        # install() ran only when the *script* was missing, so a runtime with
        # the script present but missing/incomplete node_modules would launch a
        # bridge whose require('mineflayer') fails — the exact recurring error.
        # We now (re)install whenever the script OR any npm dep is missing.
        if not self._bridge_script.exists() or not self._deps_present():
            install_res = await self.install()
            if not install_res.get("ok"):
                return install_res

        # Reap any stale/unhealthy bridge still holding the port before we bind,
        # so a fresh launch never dies with EADDRINUSE onto a sick predecessor.
        await self._reap_orphan_bridge()

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
            "installed": self._bridge_script.exists() and self._deps_present(),
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
