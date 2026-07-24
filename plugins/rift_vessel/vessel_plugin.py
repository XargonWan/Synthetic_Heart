# plugins/rift_vessel/vessel_plugin.py
"""Vessel — core Rift Vessel plugin.

Exposes SyntH's embodiment actions (``vessel_say``, ``vessel_move``,
``vessel_look``, ``vessel_use``, ``vessel_status``) and routes them to the
active world connector via the Vessel registry. Other plugins and the Vessel
interface call :meth:`VesselPlugin.act` instead of touching connectors directly.

Design constraints (project decisions — see ``docs/rift_vessel.rst``):

* Vessel actions declare **no** ``external_effects`` — they must stay on the
  normal action path and never be promoted to the Agent Lane / spawn Drones.
* This plugin never writes diary/memory entries. The Vessel *interface* buffers
  lived experience and flushes a single autobiographical entry at end-of-session.

Connectors register themselves by importing their modules; this plugin imports
any built-in connectors on startup.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from core.ai_plugin_base import AIPluginBase
from core.config_manager import config_registry
from core.core_initializer import register_plugin
from core.logging_utils import log_error, log_info, log_warning
from core.vessel_registry import VESSEL_REGISTRY
from plugins.rift_vessel.vessel_base import VesselActionResult, WorldState

# ---------------------------------------------------------------------------
# Config variables (hidden from Settings — the Vessel is configured via the
# Engines/Vessel tab, not the Settings page).
# ---------------------------------------------------------------------------

config_registry.get_value(
    "ACTIVE_VESSEL",
    "disabled",
    value_type=str,
    group="plugins",
    component="vessel_plugin",
    hidden=True,
)
config_registry.get_value(
    "VESSEL_SETTINGS",
    "{}",
    value_type=str,
    group="plugins",
    component="vessel_plugin",
    hidden=True,
)


class VesselPlugin(AIPluginBase):
    """Core embodiment plugin. Registers vessel actions and delegates to the
    active world connector."""

    display_name = "Vessel (Embodiment)"

    def get_metadata(self) -> dict:
        """Return declarative metadata for the WebUI plugin panel and docs.

        Explicitly declares the ``Vessels`` category and the conventional
        ``icon.svg`` / ``guide.md`` assets shipped alongside this plugin. The
        loader falls back to the SyntH logo when the icon file is missing.
        """
        return {
            "name": "vessel_plugin",
            "display_name": "Rift Vessel",
            "description": (
                "Lets Synth inhabit external game/virtual worlds through "
                "pluggable connectors (Minecraft PoC shipped) while identity, "
                "memory, and personality persist across worlds and chats."
            ),
            "category": "Vessels",
            "icon": "icon.svg",
            "guide": "guide.md",
        }

    # ------------------------------------------------------------------
    # Constructor
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        super().__init__()
        self._active_connector_name: str = "disabled"
        self._settings: dict[str, Any] = {}

        # Import built-in connector modules so they self-register.
        self._import_builtin_connectors()

        self.refresh_config()
        register_plugin("vessel_plugin", self)
        log_info("[vessel_plugin] Initialized.")

    # ------------------------------------------------------------------
    # Connector discovery
    # ------------------------------------------------------------------

    @staticmethod
    def _import_builtin_connectors() -> None:
        """Import shipping connector modules so they self-register.

        Failures are non-fatal: a missing/broken connector must never break the
        rest of the system (AGENTS.md golden rule).
        """
        builtin = [
            "plugins.rift_vessel.minecraft.minecraft",
        ]
        for module_path in builtin:
            try:
                __import__(module_path)
            except Exception as exc:  # pragma: no cover - defensive
                log_warning(
                    f"[vessel_plugin] Skipped built-in connector '{module_path}': {exc}"
                )

    # ------------------------------------------------------------------
    # Public API — used by the Vessel interface and other plugins
    # ------------------------------------------------------------------

    async def act(
        self,
        action: str,
        payload: dict[str, Any],
        connector_name: str | None = None,
    ) -> VesselActionResult:
        """Dispatch a normalized embodiment action to the active connector.

        Args:
            action:         Normalized action name without the ``vessel_``
                            prefix (``"say"``, ``"move"``, ``"look"``, ``"use"``).
            payload:        Action fields.
            connector_name: Override the active connector for this call.

        Returns:
            A :class:`VesselActionResult`. Returns ``ok=False`` when the vessel
            is disabled or the connector cannot be loaded — never raises for a
            normal in-world failure.
        """
        self.refresh_config()

        name = connector_name or self._active_connector_name
        if name == "disabled":
            return VesselActionResult(ok=False, detail="vessel_disabled")

        try:
            connector = VESSEL_REGISTRY.load_connector(name)
        except ValueError as exc:
            log_error(f"[vessel_plugin] Cannot load connector '{name}': {exc}")
            return VesselActionResult(ok=False, detail=f"connector_load_failed: {exc}")

        try:
            result = connector.act(action, payload)
            if asyncio.iscoroutine(result):
                result = await result
            if not isinstance(result, VesselActionResult):
                # Be permissive: connectors that return a bool/dict get wrapped.
                if isinstance(result, bool):
                    result = VesselActionResult(ok=result)
                elif isinstance(result, dict):
                    result = VesselActionResult(
                        ok=bool(result.get("ok", True)),
                        detail=result.get("detail"),
                        data=result.get("data", {}),
                    )
                else:
                    result = VesselActionResult(ok=True)
            return result
        except Exception as exc:
            log_error(f"[vessel_plugin] act('{action}') error ({name}): {exc}")
            return VesselActionResult(ok=False, detail=f"act_error: {exc}")

    async def get_world_state(
        self, connector_name: str | None = None
    ) -> WorldState | None:
        """Return the active connector's current :class:`WorldState`."""
        self.refresh_config()
        name = connector_name or self._active_connector_name
        if name == "disabled":
            return None
        try:
            connector = VESSEL_REGISTRY.load_connector(name)
        except ValueError:
            return None
        try:
            state = connector.get_world_state()
            if asyncio.iscoroutine(state):
                state = await state
            return state
        except Exception as exc:
            log_warning(f"[vessel_plugin] get_world_state error ({name}): {exc}")
            return None

    @property
    def active_connector_name(self) -> str:
        return self._active_connector_name

    @property
    def settings(self) -> dict[str, Any]:
        return dict(self._settings)

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    @staticmethod
    def get_supported_actions() -> dict:
        # NOTE: none of these declare ``external_effects`` — the Vessel must
        # stay on the normal action path and never trigger Agent-Lane routing.
        return {
            "vessel_say": {
                "description": (
                    "Speak or emote in the game world you are currently "
                    "inhabiting through your Vessel. The text is sent as an "
                    "in-world chat message from your embodied presence."
                ),
                "required_fields": ["text"],
                "optional_fields": [],
                "security_level": "low",
            },
            "vessel_move": {
                "description": (
                    "Move your embodied presence in the game world. Provide a "
                    "direction (e.g. 'forward', 'north') and optional distance, "
                    "or a target position/entity to move toward."
                ),
                "required_fields": [],
                "optional_fields": ["direction", "distance", "target"],
                "security_level": "low",
            },
            "vessel_look": {
                "description": (
                    "Turn your gaze / orientation in the game world toward a "
                    "direction, coordinate, or nearby entity."
                ),
                "required_fields": [],
                "optional_fields": ["direction", "target", "yaw", "pitch"],
                "security_level": "low",
            },
            "vessel_use": {
                "description": (
                    "Interact with the world through your Vessel: use/activate "
                    "an object, attack, pick up, or trigger a world-specific "
                    "interaction on a target."
                ),
                "required_fields": ["interaction"],
                "optional_fields": ["target"],
                "security_level": "low",
            },
            "vessel_status": {
                "description": (
                    "Report your current embodied state in the game world "
                    "(health, position, what you can currently do). Use this to "
                    "orient yourself before acting."
                ),
                "required_fields": [],
                "optional_fields": [],
                "security_level": "low",
            },
        }

    def is_enabled(self) -> bool:
        self.refresh_config()
        return self._active_connector_name != "disabled"

    async def handle_custom_action(
        self, action_type: str, payload: dict
    ) -> dict[str, Any]:
        mapping = {
            "vessel_say": "say",
            "vessel_move": "move",
            "vessel_look": "look",
            "vessel_use": "use",
        }
        if action_type in mapping:
            result = await self.act(mapping[action_type], payload)
            return {
                "status": "ok" if result.ok else "error",
                "message": result.detail or "",
                "data": result.data,
            }
        if action_type == "vessel_status":
            state = await self.get_world_state()
            if state is None:
                return {"status": "error", "message": "no_world_state"}
            return {
                "status": "ok",
                "data": {
                    "environment": state.environment,
                    "health": state.health,
                    "position": state.position,
                    "possible_actions": state.possible_actions,
                    "flags": state.flags,
                },
            }
        return {"status": "error", "message": f"Unknown action: {action_type}"}

    async def execute_action(
        self,
        action: dict,
        context: dict,
        bot: Any,
        original_message: Any,
    ) -> dict[str, Any]:
        action_type = action.get("type", "")
        payload = action.get("payload", {})
        return await self.handle_custom_action(action_type, payload)

    # ------------------------------------------------------------------
    # Config
    # ------------------------------------------------------------------

    def refresh_config(self) -> None:
        """Re-read exposed variables (allows WebUI hot-changes)."""
        try:
            self._active_connector_name = str(
                config_registry.get_value(
                    "ACTIVE_VESSEL",
                    "disabled",
                    value_type=str,
                    group="plugins",
                    component="vessel_plugin",
                )
            )
            raw_settings = config_registry.get_value(
                "VESSEL_SETTINGS",
                "{}",
                value_type=str,
                group="plugins",
                component="vessel_plugin",
            )
            try:
                self._settings = json.loads(raw_settings or "{}")
            except Exception:
                self._settings = {}
        except Exception as exc:
            log_warning(f"[vessel_plugin] refresh_config failed: {exc}")


PLUGIN_CLASS = VesselPlugin
