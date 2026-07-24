# plugins/rift_vessel/vessel_plugin.py
"""Vessel — core Rift Vessel plugin.

Exposes SyntH's embodiment actions and routes them to the active world
connector via the Vessel registry. The Vessel itself is *infrastructure*: the
active game owns the concrete actions, so the exposed names are namespaced per
world as ``vessel_<world>_<verb>`` (``vessel_minecraft_connect``,
``vessel_minecraft_say``, …) driven by ``ACTIVE_VESSEL``. Two tiers of verbs are
exposed under that prefix:

* the world-agnostic **core set** owned by the Vessel and shared by every world
  (``connect``, ``disconnect``, ``say``, ``move``, ``look``, ``use``,
  ``status``); and
* any **world-specific** verbs the active connector adds via
  :meth:`VesselConnectorBase.get_world_actions` (e.g. a hypothetical Minecraft
  ``craft`` or Skyrim ``cast_spell``).

Other plugins and the Vessel interface call :meth:`VesselPlugin.act` instead of
touching connectors directly.

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
import time
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
config_registry.get_value(
    "VESSEL_SESSION_COOLDOWN_SEC",
    3600,
    value_type=int,
    label="Vessel Session Cooldown (s)",
    description=(
        "Inactivity window before a Vessel session is closed and its buffered "
        "experience is flushed to a single diary entry."
    ),
    group="plugins",
    component="vessel_plugin",
    advanced=True,
)


class VesselPlugin(AIPluginBase):
    """Core embodiment plugin. Registers vessel actions and delegates to the
    active world connector."""

    display_name = "Rift Vessel"

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
                "pluggable connectors (Minecraft shipped) while identity, "
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

        # Measure the in-world action round-trip. The Rift Vessel is primarily
        # used in games, where responsiveness matters: this INFO metric lets us
        # confirm actions reach the world quickly (the single-HTTP Fast-Lane
        # path) rather than being stuck behind slow reasoning. Timing only wraps
        # the connector call — it never changes behaviour.
        _act_started = time.monotonic()
        try:
            result = connector.act(action, payload)
            if asyncio.iscoroutine(result):
                result = await result
            _act_elapsed_ms = (time.monotonic() - _act_started) * 1000.0
            log_info(
                f"[vessel_plugin] act('{action}') dispatched via '{name}' in "
                f"{_act_elapsed_ms:.0f} ms"
            )
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
    # Embodiment lifecycle (enter / leave a world)
    # ------------------------------------------------------------------

    @staticmethod
    def _get_vessel_interface() -> Any | None:
        """Return the live Vessel interface instance, or ``None``.

        The interface owns perception routing (salience + message-chain
        injection) and session bookkeeping (experience buffer + diary flush).
        The plugin needs it to wire the connector's perception callback and to
        open/close a session on connect/disconnect.
        """
        try:
            from core.core_initializer import INTERFACE_REGISTRY

            return INTERFACE_REGISTRY.get("vessel")
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"[vessel_plugin] Vessel interface unavailable: {exc}")
            return None

    async def connect_world(
        self,
        connector_name: str | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> VesselActionResult:
        """Enter (connect to) the active world through its connector.

        This is the missing piece that lets Synth actually *embody* a world:
        it loads the active connector, opens an embodiment session on the
        Vessel interface, and calls ``connector.connect(settings, on_event)``
        with a perception callback that forwards world events into the message
        chain. Idempotent-friendly: connecting an already-connected connector
        simply re-confirms the session.

        ``overrides`` are per-connect settings (e.g. a world's server
        ``host``/``port``) that take precedence over the saved defaults for
        this connect only. Empty values are ignored so partial overrides keep
        the configured defaults.
        """
        self.refresh_config()

        # Merge per-connect overrides on top of the saved settings. The plugin
        # config remains the default; the action payload can steer a single
        # connect (e.g. join a different Minecraft server on request).
        settings = dict(self._settings)
        if overrides:
            for key, value in overrides.items():
                if value not in (None, ""):
                    settings[key] = value

        name = connector_name or self._active_connector_name
        if not name or name == "disabled":
            available = ", ".join(self._enabled_worlds()) or "(none enabled)"
            return VesselActionResult(
                ok=False,
                detail=(
                    "no_world_selected: choose a world via the 'game' field. "
                    f"Available games: {available}."
                ),
            )

        # Guard: the requested world must be registered and its sub-plugin
        # enabled (an operator can disable a world without touching ACTIVE_VESSEL).
        if name not in self._enabled_worlds():
            available = ", ".join(self._enabled_worlds()) or "(none enabled)"
            return VesselActionResult(
                ok=False,
                detail=(
                    f"world_unavailable: '{name}' is not an enabled world. "
                    f"Available games: {available}."
                ),
            )

        try:
            connector = VESSEL_REGISTRY.load_connector(name)
        except ValueError as exc:
            log_error(f"[vessel_plugin] Cannot load connector '{name}': {exc}")
            return VesselActionResult(ok=False, detail=f"connector_load_failed: {exc}")

        if getattr(connector, "is_connected", False):
            log_info(f"[vessel_plugin] connect_world: '{name}' already connected")
            return VesselActionResult(
                ok=True, detail="already_connected", data={"environment": name}
            )

        iface = self._get_vessel_interface()
        session_id: str | None = None

        async def _on_event(event: Any) -> None:
            """Forward a connector PerceptionEvent into the message chain."""
            if iface is None:
                return
            try:
                await iface.on_world_event(
                    environment=getattr(event, "environment", name),
                    event_type=getattr(event, "event_type", "event"),
                    summary=getattr(event, "summary", ""),
                    entity=getattr(event, "actor", None),
                    session_id=session_id,
                    data=getattr(event, "data", None),
                )
            except Exception as exc:  # pragma: no cover - defensive
                log_warning(f"[vessel_plugin] perception forward failed: {exc}")

        # Open a session first so buffered experience is captured from the very
        # first event the connector emits during connect().
        if iface is not None and hasattr(iface, "begin_session"):
            try:
                session_id = await iface.begin_session(name)
            except Exception as exc:
                log_warning(f"[vessel_plugin] begin_session failed: {exc}")
                session_id = None

        # Reason surfaced to Synth when the connect fails. Prefer the concrete
        # cause the connector recorded (bridge/server error, version mismatch,
        # timeout, ...) so Synth can tell the requester WHY, not just "failed".
        failure_reason: str | None = None
        try:
            ok = await connector.connect(settings, _on_event)
        except Exception as exc:
            log_error(f"[vessel_plugin] connect_world error ('{name}'): {exc}")
            failure_reason = str(exc)
            ok = False

        if not ok:
            # Roll back the session we optimistically opened.
            if iface is not None and session_id and hasattr(iface, "end_session"):
                try:
                    await iface.end_session(session_id, reason="connect_failed")
                except Exception:
                    pass
            # A connector-recorded reason wins over a raised-exception message.
            connector_error = getattr(connector, "last_error", None)
            reason = connector_error or failure_reason
            detail = (
                f"Could not enter '{name}': {reason}" if reason else "connect_failed"
            )
            return VesselActionResult(
                ok=False,
                detail=detail,
                data={"environment": name, "reason": reason},
            )

        log_info(f"[vessel_plugin] Entered world '{name}' (session={session_id})")
        return VesselActionResult(
            ok=True,
            detail="connected",
            data={"environment": name, "session_id": session_id},
        )

    async def disconnect_world(
        self, connector_name: str | None = None
    ) -> VesselActionResult:
        """Leave (disconnect from) the active world through its connector."""
        self.refresh_config()

        name = connector_name or self._active_connector_name
        if name == "disabled":
            return VesselActionResult(ok=False, detail="vessel_disabled")

        try:
            connector = VESSEL_REGISTRY.load_connector(name)
        except ValueError as exc:
            return VesselActionResult(ok=False, detail=f"connector_load_failed: {exc}")

        try:
            await connector.disconnect()
        except Exception as exc:
            log_error(f"[vessel_plugin] disconnect_world error ('{name}'): {exc}")
            return VesselActionResult(ok=False, detail=f"disconnect_error: {exc}")

        # End the interface-side session(s) for this world so the lived
        # experience is flushed to a single diary entry.
        iface = self._get_vessel_interface()
        if iface is not None and hasattr(iface, "end_sessions_for_environment"):
            try:
                await iface.end_sessions_for_environment(name, reason="logout")
            except Exception as exc:
                log_warning(
                    f"[vessel_plugin] end_sessions_for_environment failed: {exc}"
                )

        log_info(f"[vessel_plugin] Left world '{name}'")
        return VesselActionResult(
            ok=True, detail="disconnected", data={"environment": name}
        )

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    #: World-agnostic action verbs. The *core* owns the behaviour; the exposed
    #: action names are namespaced per active world (``vessel_<world>_<verb>``),
    #: e.g. ``vessel_minecraft_connect`` when ACTIVE_VESSEL == "minecraft".
    #: NOTE: none of these declare ``external_effects`` — the Vessel must stay
    #: on the normal action path and never trigger Agent-Lane routing.
    _ACTION_VERBS: dict[str, dict[str, Any]] = {
        "connect": {
            "description": (
                "Enter (connect to) the {world} world you are configured to "
                "inhabit, embodying your presence there. Do this before any "
                "other {world} vessel action when you are not yet in the world. "
                "Optionally override the world server address for this connect "
                "only via 'host' and 'port' (the configured defaults are used "
                "when omitted)."
            ),
            "required_fields": [],
            "optional_fields": ["host", "port"],
            "security_level": "low",
        },
        "disconnect": {
            "description": (
                "Leave (disconnect from) the {world} world you are currently "
                "inhabiting, ending your embodied session."
            ),
            "required_fields": [],
            "optional_fields": [],
            "security_level": "low",
        },
        "say": {
            "description": (
                "Speak or emote in the {world} world you are currently "
                "inhabiting through your Vessel. By default the text is sent as "
                "an in-world text chat message from your embodied presence. Set "
                "'audio' to true to speak it out loud with your voice where the "
                "world supports voice chat (falls back to text otherwise)."
            ),
            "required_fields": ["text"],
            "optional_fields": ["audio"],
            "security_level": "low",
        },
        "move": {
            "description": (
                "Move your embodied presence in the {world} world. Provide a "
                "direction (e.g. 'forward', 'north') and optional distance, "
                "or a target position/entity to move toward."
            ),
            "required_fields": [],
            "optional_fields": ["direction", "distance", "target"],
            "security_level": "low",
        },
        "look": {
            "description": (
                "Turn your gaze / orientation in the {world} world toward a "
                "direction, coordinate, or nearby entity."
            ),
            "required_fields": [],
            "optional_fields": ["direction", "target", "yaw", "pitch"],
            "security_level": "low",
        },
        "use": {
            "description": (
                "Interact benignly with the {world} world through your Vessel: "
                "use/activate/open an object, pick something up, or trigger a "
                "non-hostile interaction on a target. For combat use 'attack'."
            ),
            "required_fields": ["interaction"],
            "optional_fields": ["target"],
            "security_level": "low",
        },
        "attack": {
            "description": (
                "Attack a target in the {world} world through your Vessel — a "
                "hostile action against a nearby entity or mob. Provide the "
                "'target' to strike (defaults to the closest attackable entity "
                "when omitted)."
            ),
            "required_fields": [],
            "optional_fields": ["target"],
            "security_level": "low",
        },
        "follow": {
            "description": (
                "Start following an entity in the {world} world (a player, an "
                "NPC, a creature) so your embodied presence keeps moving to "
                "stay near it. Provide the 'target' to follow. Fails if there "
                "is nothing to follow."
            ),
            "required_fields": [],
            "optional_fields": ["target"],
            "security_level": "low",
        },
        "unfollow": {
            "description": (
                "Stop following whatever entity you are currently following in "
                "the {world} world, halting your embodied presence."
            ),
            "required_fields": [],
            "optional_fields": [],
            "security_level": "low",
        },
        "respawn": {
            "description": (
                "Respawn (come back to life) in the {world} world after your "
                "embodied presence has died, returning to a spawn point. Use "
                "this when you are dead and want to resume being present in the "
                "world. Does nothing where the world has no death/respawn "
                "concept or when you are already alive."
            ),
            "required_fields": [],
            "optional_fields": [],
            "security_level": "low",
        },
        "status": {
            "description": (
                "Report your current embodied state in the {world} world "
                "(health, position, what you can currently do). Use this to "
                "orient yourself before acting."
            ),
            "required_fields": [],
            "optional_fields": [],
            "security_level": "low",
        },
    }

    def _action_world(self) -> str:
        """Return the world token used to namespace action names.

        This is the currently *connected* world (runtime state) when a session
        is live, else the active connector name (e.g. ``"minecraft"``). When no
        world is active it falls back to ``"vessel"`` so a generic name still
        exists.
        """
        connected = self._connected_world()
        if connected:
            return connected
        self.refresh_config()
        world = (self._active_connector_name or "").strip()
        if not world or world == "disabled":
            return "vessel"
        return world

    @staticmethod
    def _world_enabled(world: str) -> bool:
        """Return whether a world is available to connect to.

        A world is available when its dedicated sub-plugin (``<world>_vessel``)
        is enabled. Worlds without a sub-plugin are always available (default
        ``True``). Fully fail-safe: any error resolves to available.
        """
        try:
            from core.config_manager import config_registry

            return bool(
                config_registry.get_value(
                    f"PLUGIN_ENABLED__{world}_vessel",
                    True,
                    value_type=bool,
                    component=f"{world}_vessel",
                    group="plugins",
                    hidden=True,
                )
            )
        except Exception:  # pragma: no cover - defensive
            return True

    def _enabled_worlds(self) -> list[str]:
        """Return registered connector names whose world sub-plugin is enabled.

        This is the set of games offered in the ``vessel_connect`` ``game``
        enum. Fully fail-safe: registry/config errors yield an empty list.
        """
        try:
            names = VESSEL_REGISTRY.get_available_connectors()
        except Exception:  # pragma: no cover - defensive
            return []
        return [name for name in names if self._world_enabled(name)]

    def _connected_world(self) -> str | None:
        """Return the name of the world with a live connection, or ``None``.

        Runtime, side-effect-free probe: only meaningful while a Vessel session
        is active. Iterates the *already-loaded* connector instances (cached in
        the registry, so ``is_connected`` reflects the real socket state) and
        returns the first one reporting a live connection. Never loads a fresh
        connector and never raises — safe to call from the pure
        :meth:`get_supported_actions`.
        """
        try:
            from core.vessel_session_manager import vessel_session_manager

            if not vessel_session_manager.has_active_session():
                return None
        except Exception:  # pragma: no cover - defensive
            return None
        try:
            instances = getattr(VESSEL_REGISTRY, "_instances", {}) or {}
        except Exception:  # pragma: no cover - defensive
            return None
        for name, connector in instances.items():
            try:
                if getattr(connector, "is_connected", False):
                    return name
            except Exception:  # pragma: no cover - defensive
                continue
        return None

    def _world_extra_verbs_for(self, world: str) -> dict[str, dict[str, Any]]:
        """Return a specific connector's **world-specific** extra action verbs.

        The core set (:attr:`_ACTION_VERBS`) is world-agnostic and owned by the
        Vessel. Each connector may add its own verbs via
        :meth:`VesselConnectorBase.get_world_actions` (e.g. Minecraft ``craft``,
        Skyrim ``cast_spell``). This reads them from the given connector,
        skipping any that collide with a core verb. Fully fail-safe: if the
        connector is disabled/unloadable or raises, the core set is used alone.
        """
        if not world or world == "vessel":
            return {}
        try:
            connector = VESSEL_REGISTRY.load_connector(world)
        except ValueError:
            return {}
        try:
            extra = connector.get_world_actions()
        except Exception as exc:  # never let a connector break action exposure
            log_warning(f"[vessel_plugin] get_world_actions error ({world}): {exc}")
            return {}
        if not isinstance(extra, dict):
            return {}
        return {
            verb: schema
            for verb, schema in extra.items()
            if verb not in self._ACTION_VERBS and isinstance(schema, dict)
        }

    def _world_extra_verbs(self) -> dict[str, dict[str, Any]]:
        """Backward-compatible wrapper reading extras for the active world."""
        return self._world_extra_verbs_for(self._action_world())

    def get_supported_actions(self) -> dict:
        """Expose embodiment actions driven by the live connection state.

        This method is **pure** (no side effects, no ``await``) — the core calls
        it on every prompt build/dispatch/validation, so its output reflects the
        *current* connection state and the exposed action set changes
        automatically on the next prompt:

        * **Disconnected** — only a single ``vessel_connect`` action is exposed.
          Its required ``game`` enum lists every enabled world, and optional
          ``host``/``port`` override the server address for that connect only.
          The gameplay verbs are hidden until Synth actually enters a world.
        * **Connected to world W** — the world-agnostic **core set**
          (:attr:`_ACTION_VERBS`, minus ``connect``) plus W's own
          :meth:`VesselConnectorBase.get_world_actions` extras are exposed,
          namespaced ``vessel_<W>_<verb>``, together with ``vessel_disconnect``.
          ``vessel_connect`` disappears while embodied.

        When the Vessel is disabled entirely (no enabled worlds) the action set
        is empty. This keeps the prompt clean: Synth only ever sees the verbs it
        can actually use right now.
        """
        connected = self._connected_world()
        actions: dict[str, dict[str, Any]] = {}

        if not connected:
            # Disconnected: offer only the entry point.
            enabled = self._enabled_worlds()
            if not enabled:
                return {}
            connect_schema = dict(self._ACTION_VERBS["connect"])
            games = ", ".join(enabled)
            connect_schema["description"] = (
                "Enter (connect to) an external game/virtual world to embody "
                "your presence there. Choose the world via the 'game' field. "
                f"Available games: {games}. Optionally override the world "
                "server address for this connect only via 'host' and 'port' "
                "(the configured defaults are used otherwise). Once connected, "
                "the world's own actions (say/move/look/use/attack/follow/…) "
                "become available and this action disappears until you "
                "disconnect."
            )
            connect_schema["required_fields"] = ["game"]
            optional = list(connect_schema.get("optional_fields") or [])
            for extra in ("host", "port"):
                if extra not in optional:
                    optional.append(extra)
            connect_schema["optional_fields"] = optional
            connect_schema["game_choices"] = enabled
            actions["vessel_connect"] = connect_schema
            return actions

        # Connected: expose the world's verbs (core set minus connect) + extras,
        # namespaced by the connected world, plus a plain disconnect.
        prefix = f"vessel_{connected}_"
        for verb, schema in self._ACTION_VERBS.items():
            if verb in ("connect", "disconnect"):
                continue
            entry = dict(schema)
            entry["description"] = entry["description"].format(world=connected)
            actions[f"{prefix}{verb}"] = entry
        for verb, schema in self._world_extra_verbs_for(connected).items():
            entry = dict(schema)
            desc = entry.get("description", "")
            if isinstance(desc, str) and "{world}" in desc:
                entry["description"] = desc.format(world=connected)
            # World actions must stay on the Fast Lane like the core set.
            entry.pop("external_effects", None)
            # Anti double-prefix: a connector may already namespace its verbs.
            name = verb if verb.startswith("vessel_") else f"{prefix}{verb}"
            actions[name] = entry
        disconnect_entry = dict(self._ACTION_VERBS["disconnect"])
        disconnect_entry["description"] = disconnect_entry["description"].format(
            world=connected
        )
        actions["vessel_disconnect"] = disconnect_entry
        return actions

    def _parse_action_verb(self, action_type: str) -> str | None:
        """Extract the world-agnostic verb from a namespaced action name.

        Accepts both the namespaced form (``vessel_minecraft_say``) and the
        plain form (``vessel_connect`` / ``vessel_disconnect``). Recognizes both
        the core set (:attr:`_ACTION_VERBS`) and the connected world's
        world-specific verbs. Returns the bare verb (``connect``, ``say``,
        ``craft``, …) or ``None`` when it is not a Vessel action.
        """
        if not action_type.startswith("vessel_"):
            return None
        connected = self._connected_world()
        extra_verbs = list(self._world_extra_verbs_for(connected)) if connected else []
        known_verbs = list(self._ACTION_VERBS) + extra_verbs
        for verb in known_verbs:
            if action_type == f"vessel_{verb}" or action_type.endswith(f"_{verb}"):
                return verb
        return None

    def is_enabled(self) -> bool:
        """Vessel is available whenever at least one world sub-plugin is enabled.

        In the connection-driven model the entry action (``vessel_connect``)
        must be offered whenever *some* world can be entered, regardless of the
        legacy ``ACTIVE_VESSEL`` default connector. When embodied, the connected
        world's own verbs are exposed instead.
        """
        return bool(self._connected_world()) or bool(self._enabled_worlds())

    async def handle_custom_action(
        self, action_type: str, payload: dict
    ) -> dict[str, Any]:
        verb = self._parse_action_verb(action_type)
        if verb == "connect":
            # The world to enter is chosen via the 'game' field (the enum in the
            # exposed action). Fall back to the legacy namespaced form
            # (vessel_<world>_connect) or the configured ACTIVE_VESSEL default.
            game = payload.get("game")
            if not game and action_type != "vessel_connect":
                game = action_type[len("vessel_") : -len("_connect")] or None
            overrides = {
                k: payload[k]
                for k in ("host", "port")
                if payload.get(k) not in (None, "")
            }
            result = await self.connect_world(
                connector_name=game or None, overrides=overrides or None
            )
            return {
                "status": "ok" if result.ok else "error",
                "message": result.detail or "",
                "data": result.data,
            }
        if verb == "disconnect":
            result = await self.disconnect_world(connector_name=self._connected_world())
            return {
                "status": "ok" if result.ok else "error",
                "message": result.detail or "",
                "data": result.data,
            }
        if verb in (
            "say",
            "move",
            "look",
            "use",
            "attack",
            "follow",
            "unfollow",
            "respawn",
        ):
            result = await self.act(
                verb, payload, connector_name=self._connected_world()
            )
            return {
                "status": "ok" if result.ok else "error",
                "message": result.detail or "",
                "data": result.data,
            }
        if verb == "status":
            state = await self.get_world_state(connector_name=self._connected_world())
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
        if verb is not None:
            # World-specific verb declared by the connected connector's
            # get_world_actions() — dispatch it straight to the connector.
            result = await self.act(
                verb, payload, connector_name=self._connected_world()
            )
            return {
                "status": "ok" if result.ok else "error",
                "message": result.detail or "",
                "data": result.data,
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
