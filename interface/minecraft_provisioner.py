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
# ``mineflayer-collectblock`` composes navigate → dig → pick-up so ``mine`` and
# ``collect_block`` reliably land the drop in the inventory (without it the raw
# dig fallback often "mines" a block but collects nothing). ``mineflayer-auto-
# eat`` is the reflexive hunger handler that keeps the body from starving. Both
# are loaded best-effort by the bridge, so they MUST be in this install list —
# the provisioner installs exactly this explicit set (not a bare ``npm install``
# from package.json), and the missing-check below only re-installs when one of
# these is absent, so a dep omitted here is never installed on a fresh deploy.
_BRIDGE_NPM_DEPS = [
    "mineflayer",
    "mineflayer-pathfinder",
    "minecraft-data",
    "mineflayer-collectblock",
    "mineflayer-auto-eat",
]


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
        # Serialise :meth:`start` against itself. At boot the reattach flow and
        # the first ``connect_world`` can both call ``start()`` before the very
        # first bridge has bound ``/health`` with mineflayer:true (login+spawn
        # takes several seconds). Without a lock the second caller sees no
        # healthy bridge and launches a DUPLICATE process; the two bridges then
        # both stream the spawn chunk burst and one balloons off-heap (worker
        # native chunk memory) to ~3.2GB and OOMs, while the other stays a
        # healthy ~149MB. The lock makes concurrent callers await the first
        # launch so they observe mineflayer:true and adopt it instead. Created
        # lazily so the provisioner can be constructed off the event loop.
        self._start_lock: asyncio.Lock | None = None

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
        # Bound the Node heap against the mineflayer chunk-cache memory growth
        # (see the viewDistance / chunk-prune notes in the bridge script).
        # Mineflayer caches every chunk column the server streams and never
        # evicts it, so on a busy world V8's old-space climbs toward the ~4 GB
        # default limit where mark-compact becomes ineffective and the process
        # OOM-crashes. Two knobs work together here:
        #   * --expose-gc lets the bridge's periodic chunk pruner call
        #     global.gc() to actually reclaim evicted columns (without it the
        #     prune only drops references and V8 reclaims them lazily, letting
        #     the heap creep toward the cap between GCs). This is the enabler
        #     for the real fix (active chunk eviction), not just a delay tactic.
        #   * --max-old-space-size caps the ceiling as a safety belt. It must
        #     leave headroom for the INITIAL world-load transient: on a
        #     chunk-dense world (e.g. an ocean spawn) the first seconds after
        #     spawn spike well past the steady-state while the initial chunk
        #     burst decodes and BEFORE the pruner's first pass runs. Observed:
        #     512 MB OOM'd at ~500 MB pre-spawn; 1024 MB OOM'd at ~990 MB just
        #     after spawn while the chunk burst was still growing. 1536 MB
        #     STILL OOM'd at ~1490 MB after spawn even with active pruning: on a
        #     large server-authoritative world the retained working set (entity
        #     tracking + block/physics caches that outlive an evicted column)
        #     plateaus near 1.5 GB, so the cap must sit clearly above that
        #     plateau. 3072 MB STILL OOM'd at ~2955 MB during the very FIRST
        #     prune pass ("evicted 164 distant column(s)") right after spawn on
        #     the target server: the server ignores viewDistance:'tiny' and
        #     streams ~164 columns at once, and their synchronous decode burst
        #     (prismarine-chunk BitArray + block-state palettes for the whole
        #     wide radius) peaks past 2.9 GB BEFORE the pruner's first eviction
        #     can free anything. Escalating the cap alone repeatedly failed
        #     (512 → 1024 → 1536 → 3072 → 6144 MB each OOM'd) because a slow
        #     timer sweep cannot keep up once GC thrashing starves setInterval:
        #     observed the timer pruner firing only ~7 times in ~80 s of life.
        #   * THE REAL FIX now lives in the bridge: an EVENT-DRIVEN eviction
        #     hooked to the bot's `chunkColumnLoad` event drops every distant
        #     column the instant the server streams it in, so the resident set
        #     is capped to ~(2*radius+1)^2 (49 at radius 3) at ALL times,
        #     independent of the timer and unaffected by GC pressure. CRUCIAL
        #     TIMING FIX: this listener is now wired in wireBotEvents the moment
        #     createBot returns (BEFORE 'login'/'spawn'), not at markSpawned().
        #     Previously it attached only at spawn — AFTER the spawn-time column
        #     burst had already been decompressed and made resident — so the
        #     eviction never ran during the burst and every cap OOM'd at spawn
        #     regardless of size. With early wiring the out-of-radius columns are
        #     freed as they arrive, capping the resident working set.
        #   * --max-old-space-size headroom: even with early eviction, when the
        #     server floods ~164 columns in a single burst prismarine-chunk
        #     decodes them synchronously (BitArray + palettes) before the event
        #     loop can process the eviction callbacks, so a transient decode peak
        #     is unavoidable. Observed ~1.6 GB resident at the moment of the 2048
        #     MB crash, i.e. the transient peak overshoots 2048. 3072 MB leaves
        #     clear headroom above that transient decode spike while the early
        #     event-driven eviction keeps the steady-state resident set small
        #     (the timer sweep — radius 3, every 1s, CHUNK_PRUNE_INTERVAL_MS —
        #     plus forced global.gc() reclaim the rest between bursts).
        #     RE-OBSERVED (live, 2026-07-27): raising the cap does NOT help and
        #     is the WRONG lever. Both 3072 MB (OOM at ~2955 MB) and 6144 MB
        #     (OOM at ~5850 MB) crashed at spawn on the target server. Growth is
        #     UNBOUNDED — it fills whatever cap is given — so headroom can never
        #     be enough. Diagnostics: across every crashed run the timer pruner
        #     logged ZERO "evicted N distant column(s)" lines and mineflayer
        #     logged exactly ~164 "Ignoring block entities as chunk failed to
        #     load" per connect attempt. So the resident growth is NOT the
        #     decoded chunk columns (those fail to load / are evicted) — it is
        #     the raw protocol/block-entity packet backlog that piles up because
        #     the synchronous spawn-burst decode saturates the event loop and
        #     the socket read queue drains slower than the server floods it. The
        #     event-driven eviction cannot help because the pressure is upstream
        #     of the world column store. A cap of 3072 MB is kept as the sane
        #     default (no point burning 6 GB to still OOM); the real fix must
        #     bound the INBOUND packet flood (packet-layer filter of distant
        #     map_chunk / block_entity data before prismarine-chunk decodes it,
        #     or a correct pinned protocol version — the server is joined with
        #     "auto version" and no MC_VERSION is configured, so a version
        #     mismatch could be corrupting the decode).
        # Preserve any caller-provided NODE_OPTIONS.
        existing_node_options = env.get("NODE_OPTIONS", "").strip()
        heap_opt = "--max-old-space-size=3072 --expose-gc"
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
        """Return whether every required npm dep is installed AND intact.

        A bare top-level-folder check is not enough: an interrupted or partial
        ``npm install`` can leave ``node_modules/mineflayer`` present while a
        critical transitive dependency (e.g. ``protodef``) is truncated — its
        datatypes directory ends up with a single stub file instead of the full
        set. The bridge then loads but throws "mineflayer not installed" at
        connect time, and the presence-only check would never trigger a repair.
        We therefore also verify the integrity of the deep dependency that has
        been observed to corrupt (see :meth:`_deps_intact`).
        """
        top_level_present = all(
            (self._bridge_root / "node_modules" / dep).exists()
            for dep in _BRIDGE_NPM_DEPS
        )
        return top_level_present and self._deps_intact()

    def _deps_intact(self) -> bool:
        """Best-effort integrity probe for the mineflayer dependency tree.

        Returns ``False`` when a required package is present-but-incomplete so
        the caller can force a clean reinstall. Purely structural (file
        existence / directory population) — no version or content parsing, and
        never raises.

        Two corruption classes have been seen in the field, both from an
        interrupted ``npm install`` (e.g. the container being recreated
        mid-install):

        * A declared top-level dependency folder exists but is missing its
          ``package.json``/entry point — npm extracted a few files (LICENSE,
          docs/, examples/) then was killed before writing the manifest. Node
          then cannot resolve the module at all ("Cannot find module
          'mineflayer'"), yet the folder-presence check passes.
        * A deep transitive package (``protodef``) has a truncated datatypes
          directory (a lone stub instead of the full codec set), which surfaces
          as "mineflayer not installed" at connect time.
        """
        try:
            nm = self._bridge_root / "node_modules"
            # (1) Every declared dep must have a readable package.json — the
            # definitive marker that npm finished extracting that package.
            for dep in _BRIDGE_NPM_DEPS:
                pkg = nm / dep / "package.json"
                if not pkg.is_file():
                    return False
            # (2) protodef ships mineflayer's binary datatype codecs; a partial
            # install leaves its datatypes dir with a lone stub. mineflayer
            # cannot decode a single packet without the full set.
            protodef = nm / "protodef"
            if not protodef.exists():
                return False
            datatypes = protodef / "src" / "datatypes"
            if datatypes.is_dir():
                # A healthy protodef ships several codec modules here; a
                # truncated install leaves only one. Require a plausible set.
                files = [p for p in datatypes.iterdir() if p.suffix == ".js"]
                if len(files) < 3:
                    return False
            return True
        except Exception:
            # If we cannot even probe, assume intact and let the normal
            # connect-time error surface rather than looping on reinstall.
            return True

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
        # Even when every top-level dep folder exists, a partial/interrupted
        # install can leave a fragile transitive dep (protodef) truncated,
        # producing the "mineflayer not installed" failure at connect time. A
        # bare ``npm install`` will NOT repair an already-present-but-corrupt
        # tree, so when we detect corruption we wipe node_modules + lockfile and
        # do a clean install instead of trusting npm's incremental resolution.
        corrupt = not missing and not self._deps_intact()
        if not missing and not corrupt:
            return {
                "ok": True,
                "detail": "already installed",
                "bridge_root": str(self._bridge_root),
            }
        if corrupt:
            log_warning(
                f"{LOG_PREFIX} bridge node_modules corrupt (incomplete "
                f"protodef) — wiping and reinstalling clean"
            )
            try:
                shutil.rmtree(self._bridge_root / "node_modules", ignore_errors=True)
                lockfile = self._bridge_root / "package-lock.json"
                if lockfile.exists():
                    lockfile.unlink()
            except Exception as exc:
                log_warning(f"{LOG_PREFIX} failed to wipe corrupt node_modules: {exc}")

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
        """Start the bridge subprocess if not already running.

        Serialised via :attr:`_start_lock` so concurrent callers (boot reattach
        + first ``connect_world``) can never spawn duplicate bridges — the
        second caller awaits the first launch and then adopts the now-healthy
        bridge (``already running``) instead of racing a second process onto the
        port. See the lock's construction comment for the OOM/duplicate history.
        """
        if self._start_lock is None:
            # Lazily create the lock bound to the running loop (the provisioner
            # is constructed off the event loop).
            self._start_lock = asyncio.Lock()
        async with self._start_lock:
            return await self._start_locked()

    async def _start_locked(self) -> Dict[str, Any]:
        """Start the bridge subprocess if not already running (lock held)."""
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
