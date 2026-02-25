"""Base module for Cortex Live engines.

Also hosts LiveSessionManager — the single authority for automatic live-voice
routing logic. Keeps the per-engine plugins (gemini_live, …) free of
policy decisions.
"""

from __future__ import annotations

import asyncio
import importlib
import os
import pkgutil
from typing import Any, Callable, ClassVar, Coroutine, Optional

from core.logging_utils import log_debug, log_info, log_warning
from core.cortex_registry import Capabilities
from core.config_manager import config_registry

ENGINE_KIND = "live"
ENGINE_LABEL = "Live multimodal engines"
CAPABILITIES: Optional[Capabilities] = None


def discover_and_register(registry: Any, dev_enabled: bool = False) -> None:
    """Register the Live cortex kind and its child engines."""
    registry.register_cortex_kind(ENGINE_KIND, ENGINE_LABEL, CAPABILITIES)

    base_path = os.path.dirname(__file__)

    for _importer, module_name, is_pkg in pkgutil.iter_modules([base_path]):
        if is_pkg or module_name.startswith("_") or module_name.endswith("_base"):
            continue
        module_path = f"cortex.live.{module_name}"
        try:
            mod = importlib.import_module(module_path)
        except Exception as exc:
            log_warning(f"[live_base] Failed to import {module_path}: {exc}")
            continue
        if not hasattr(mod, "PLUGIN_CLASS"):
            continue
        label = getattr(mod, "ENGINE_LABEL", None)
        caps = getattr(mod, "CAPABILITIES", None)
        registry.register_engine_module(
            module_name,
            module_path,
            cortex=ENGINE_KIND,
            capabilities=caps,
            label=label or None,
        )
        log_debug(f"[live_base] Registered engine: {module_name} ({module_path})")

    # Instantiate the manager early so its exposed vars are registered at boot.
    LiveSessionManager.get_instance()


# ---------------------------------------------------------------------------
# LiveSessionManager
# ---------------------------------------------------------------------------


class LiveSessionManager:
    """Singleton that manages automatic live-voice cortex routing.

    Responsibilities:
    - Register configuration exposed vars for the whole 'live' sub-system.
    - Activate/deactivate per-path cortex overrides (calls config helpers).
    - Schedule and cancel the auto-rejoin timer (1 min before MAX_SESSION_SECONDS).
    - Remain fully agnostic to *which* live engine is in use.

    Interfaces (discord_interface) call activate_live_for_path / deactivate_live_for_path;
    they do NOT handle timer logic themselves.
    """

    _instance: ClassVar["LiveSessionManager | None"] = None

    def __init__(self) -> None:
        self.auto_switch: bool = bool(
            config_registry.get_value(
                "LIVE_AUTO_SWITCH",
                True,
                label="Auto Switch al Cortex Live",
                description=(
                    "Quando attivo, Synth switcha automaticamente al Cortex Live"
                    " durante sessioni vocali Discord."
                ),
                value_type=bool,
                group="live",
                component="cortex_live",
            )
        )
        self.trainer_only_voice: bool = bool(
            config_registry.get_value(
                "LIVE_TRAINER_ONLY_VOICE",
                True,
                label="Solo Trainer può invitare in vocale",
                description=(
                    "Se attivo, Synth entra in chiamata vocale Discord solo su"
                    " invito del Trainer. Disattivare espone a consumo API illimitato."
                ),
                value_type=bool,
                group="live",
                component="cortex_live",
                advanced=True,
            )
        )
        self.auto_rejoin: bool = bool(
            config_registry.get_value(
                "LIVE_AUTO_REJOIN",
                True,
                label="Auto Rejoin alla scadenza sessione",
                description=(
                    "Esce e rientra automaticamente nel canale vocale 60 s prima"
                    " del limite massimo di sessione dichiarato dall'engine live."
                ),
                value_type=bool,
                group="live",
                component="cortex_live",
                advanced=True,
            )
        )
        # guild_id → asyncio.Task (auto-rejoin timer)
        self._session_timers: dict[int, asyncio.Task] = {}

        # Keep config in sync with live listener updates
        config_registry.add_listener(
            "LIVE_AUTO_SWITCH", lambda v: setattr(self, "auto_switch", bool(v))
        )
        config_registry.add_listener(
            "LIVE_TRAINER_ONLY_VOICE",
            lambda v: setattr(self, "trainer_only_voice", bool(v)),
        )
        config_registry.add_listener(
            "LIVE_AUTO_REJOIN", lambda v: setattr(self, "auto_rejoin", bool(v))
        )

    # ------------------------------------------------------------------
    # Singleton accessor
    # ------------------------------------------------------------------

    @classmethod
    def get_instance(cls) -> "LiveSessionManager":
        """Return (and lazily create) the global LiveSessionManager instance."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def is_auto_switch(self) -> bool:
        """Return the current value of LIVE_AUTO_SWITCH."""
        return bool(config_registry.get_value("LIVE_AUTO_SWITCH", self.auto_switch))

    def is_trainer_only_voice(self) -> bool:
        """Return the current value of LIVE_TRAINER_ONLY_VOICE."""
        return bool(
            config_registry.get_value(
                "LIVE_TRAINER_ONLY_VOICE", self.trainer_only_voice
            )
        )

    async def activate_live_for_path(
        self,
        interface_path: str,
        guild_id: int,
        rejoin_callback: Callable[[], Coroutine[Any, Any, None]] | None = None,
    ) -> None:
        """Activate live cortex routing for *interface_path* and arm the rejoin timer.

        No-op if LIVE_AUTO_SWITCH is False.
        """
        if not self.is_auto_switch():
            log_debug(
                f"[live_session] auto_switch off — skipping activation for {interface_path}"
            )
            return

        live_engine = await self._resolve_live_engine()
        if not live_engine:
            log_warning(
                "[live_session] No live engine available for auto-switch — routing unchanged"
            )
            return

        from core.config import set_path_cortex_override

        set_path_cortex_override(interface_path, live_engine)

        auto_rejoin = bool(
            config_registry.get_value("LIVE_AUTO_REJOIN", self.auto_rejoin)
        )
        if auto_rejoin and rejoin_callback is not None:
            max_secs = self._get_max_session_seconds(live_engine)
            if max_secs and max_secs > 60:
                self._arm_rejoin_timer(guild_id, max_secs - 60, rejoin_callback)

        log_info(
            f"[live_session] ✅ Live routing activated: {interface_path} → {live_engine}"
        )

    async def deactivate_live_for_path(
        self, interface_path: str, guild_id: int
    ) -> None:
        """Remove the per-path cortex override and cancel the rejoin timer."""
        from core.config import clear_path_cortex_override

        clear_path_cortex_override(interface_path)
        self._cancel_rejoin_timer(guild_id)
        log_info(f"[live_session] 🔴 Live routing deactivated for {interface_path}")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _resolve_live_engine(self) -> str | None:
        """Return the configured live engine or the first registered 'live' engine."""
        from core.cortex_registry import get_cortex_registry

        override = config_registry.get_value("LIVE_CORTEX", "Default")
        if override not in (None, "", "Default", "None"):
            reg = get_cortex_registry()
            if override in reg.get_available_engines():
                return override
            log_warning(
                f"[live_session] LIVE_CORTEX='{override}' not in registry — falling back"
            )

        # Fallback: first registered 'live' engine
        reg = get_cortex_registry()
        engines = reg.get_engines_by_cortex("live")
        if engines:
            return engines[0]
        return None

    def _get_max_session_seconds(self, engine_name: str) -> int | None:
        """Read MAX_SESSION_SECONDS from the engine class via reflection."""
        from core.cortex_registry import get_cortex_registry

        reg = get_cortex_registry()
        engine = reg.get_engine(engine_name)
        if engine is None:
            try:
                engine = reg.load_engine(engine_name)
            except Exception:
                return None
        return getattr(engine, "MAX_SESSION_SECONDS", None) or getattr(
            type(engine), "MAX_SESSION_SECONDS", None
        )

    def _arm_rejoin_timer(
        self,
        guild_id: int,
        delay_secs: int,
        callback: Callable[[], Coroutine[Any, Any, None]],
    ) -> None:
        """Cancel any existing timer and start a new one."""
        self._cancel_rejoin_timer(guild_id)

        async def _timer() -> None:
            await asyncio.sleep(delay_secs)
            log_info(
                f"[live_session] ⏱️ Auto-rejoin triggered for guild {guild_id} "
                f"after {delay_secs}s"
            )
            try:
                await callback()
            except Exception as exc:
                log_warning(
                    f"[live_session] Auto-rejoin callback failed for guild {guild_id}: {exc}"
                )

        self._session_timers[guild_id] = asyncio.create_task(_timer())
        log_debug(
            f"[live_session] Rejoin timer armed for guild {guild_id} in {delay_secs}s"
        )

    def _cancel_rejoin_timer(self, guild_id: int) -> None:
        """Cancel and discard the rejoin timer for *guild_id*."""
        task = self._session_timers.pop(guild_id, None)
        if task is not None and not task.done():
            task.cancel()
            log_debug(f"[live_session] Rejoin timer cancelled for guild {guild_id}")
