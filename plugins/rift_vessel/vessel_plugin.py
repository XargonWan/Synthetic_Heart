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
config_registry.get_value(
    "VESSEL_DISCONNECT_GRACE_SEC",
    30,
    value_type=int,
    label="Vessel Disconnect Grace (s)",
    description=(
        "Short grace period after the world client/bridge drops before a still-"
        "'active' Vessel session is force-closed. Distinct from the (much "
        "longer) inactivity cooldown: this unblocks the message flow quickly "
        "when the client simply disconnects, so autonomous beats stop "
        "accumulating. Clamped to 5–3600."
    ),
    group="plugins",
    component="vessel_plugin",
    advanced=True,
)
config_registry.get_value(
    "VESSEL_AUTONOMY_ENABLED",
    False,
    value_type=bool,
    label="Autonomous In-World Play",
    description=(
        "When enabled, Synth plays on its own while embodied: a periodic "
        "decision beat lets it wander, set and pursue goals, and interact with "
        "the world without waiting for a chat prompt. The beat runs a normal "
        "Fast-Lane cognition turn — never the Agent Lane, and no diary is "
        "written mid-session."
    ),
    group="plugins",
    component="vessel_plugin",
)
config_registry.get_value(
    "VESSEL_BEAT_INTERVAL_SEC",
    45,
    value_type=int,
    label="Autonomous Play Beat Interval (s)",
    description=(
        "Legacy fallback for the will-beat interval. Seconds between autonomous "
        "will beats while a session is active (clamped to 10–3600). Superseded "
        "by 'Autonomous Will Beat Interval'; only used if that is unset. Only "
        "used when Autonomous In-World Play is enabled."
    ),
    group="plugins",
    component="vessel_plugin",
    advanced=True,
)
config_registry.get_value(
    "VESSEL_WILL_INTERVAL_SEC",
    45,
    value_type=int,
    label="Autonomous Will Beat Interval (s)",
    description=(
        "Seconds between slow 'will beats' — the LLM turn where Synth reflects "
        "and decides/updates its own free-text goal and plan (clamped to "
        "10–3600). This is the deliberate 'what do I want' half of autonomy; "
        "lower is more reflective but costs more cognition turns. Falls back to "
        "the legacy 'Autonomous Play Beat Interval' when unset. Only used when "
        "Autonomous In-World Play is enabled."
    ),
    group="plugins",
    component="vessel_plugin",
    advanced=True,
)
config_registry.get_value(
    "VESSEL_WILL_QUIET_SEC",
    60,
    value_type=int,
    label="Will Beat Quiet Window (s)",
    description=(
        "Seconds of quiet required before an autonomous will beat may fire "
        "after a player interacts with Synth in-world (clamped to 0–3600). The "
        "will beat is framed as 'a quiet moment to reflect on your own', so it "
        "is deferred while a player is actively present/talking — otherwise a "
        "direct address gets swallowed by the 'you are alone' prompt and Synth "
        "seems to ignore the player. Set to 0 to disable the deferral. Only "
        "used when Autonomous In-World Play is enabled."
    ),
    group="plugins",
    component="vessel_plugin",
    advanced=True,
)
config_registry.get_value(
    "VESSEL_ACTION_BEAT_ENABLED",
    True,
    value_type=bool,
    label="Autonomous Action Beat (concrete doing)",
    description=(
        "When enabled, a periodic cognition turn asks Synth to take one "
        "concrete step toward its current goal (gather, craft, place, …). This "
        "is the layer that turns an authored goal into accomplished work: only "
        "cognition can map the goal's free-text meaning onto the right verb, "
        "since the fast reflex must never read that text. Disable to keep only "
        "the slow will beat (volition) and the fast motor tick (motion). Only "
        "used when Autonomous In-World Play is enabled."
    ),
    group="plugins",
    component="vessel_plugin",
    advanced=True,
)
config_registry.get_value(
    "VESSEL_ACTION_INTERVAL_SEC",
    20,
    value_type=int,
    label="Autonomous Action Beat Interval (s)",
    description=(
        "Seconds between action beats — the concrete-doing cognition turns that "
        "advance the current goal (clamped to 3–300). Paced faster than the "
        "will beat (which authors intent) but slower than the motor tick (which "
        "just moves the body), so play stays productive without spamming "
        "cognition. Only used when Autonomous In-World Play and the Autonomous "
        "Action Beat are both enabled."
    ),
    group="plugins",
    component="vessel_plugin",
    advanced=True,
)
config_registry.get_value(
    "VESSEL_MOTOR_ENABLED",
    True,
    value_type=bool,
    label="Autonomous Motorics (fast reflex)",
    description=(
        "When enabled, a fast reflex loop moves Synth's body toward its current "
        "goal with no LLM between will beats, so embodiment stays snappy and "
        "responsive. Disable to make Synth only move when a will beat runs. "
        "Only used when Autonomous In-World Play is enabled."
    ),
    group="plugins",
    component="vessel_plugin",
    advanced=True,
)
config_registry.get_value(
    "VESSEL_MOTOR_INTERVAL_SEC",
    3,
    value_type=int,
    label="Autonomous Motor Tick Interval (s)",
    description=(
        "Seconds between fast motor ticks that move the body toward the current "
        "goal with no LLM (clamped to 1–60). Lower is more responsive; this is "
        "cheap because it runs no cognition. Only used when Autonomous In-World "
        "Play and Autonomous Motorics are both enabled."
    ),
    group="plugins",
    component="vessel_plugin",
    advanced=True,
)
config_registry.get_value(
    "VESSEL_DRONE_PLAN_INTERVAL_SEC",
    120,
    value_type=int,
    label="Directionless-Goal Planner Interval (s)",
    description=(
        "Per-world cooldown between out-of-band Drone dispatches that translate "
        "a directionless goal into a concrete, reachable target/destination "
        "(clamped to 30–3600). When a will beat authors a goal but names no "
        "block/entity target and no coordinates, the body can only march in a "
        "straight line (looks like circling); a short-lived Drone then looks "
        "around and commits one waypoint via the world's set_goal/update_goal. "
        "Runs off the scheduler, never inside an embodiment turn. Only used "
        "when Autonomous In-World Play is enabled."
    ),
    group="plugins",
    component="vessel_plugin",
    advanced=True,
)
config_registry.get_value(
    "VESSEL_KNOWLEDGE_ENABLED",
    True,
    value_type=bool,
    label="Game Knowledge Base",
    description=(
        "When enabled, Synth can consult the real game wiki (for Minecraft, the "
        "live minecraft.wiki) for how the world works (e.g. 'iron ore needs a "
        "stone pickaxe'). Pages are fetched on demand, summarised once by the "
        "LLM into a compact factual note, and cached on disk so later lookups "
        "are instant and work offline. Relevant facts are surfaced into the "
        "will/action beats and a short-lived Drone uses them to expand a fresh "
        "goal into an ordered plan of sub-steps. Reference material only — it "
        "informs Synth's reasoning but never scripts its actions."
    ),
    group="plugins",
    component="vessel_plugin",
)
config_registry.get_value(
    "VESSEL_KNOWLEDGE_MAX_SNIPPETS",
    5,
    value_type=int,
    label="Game Knowledge: Max Snippets",
    description=(
        "Maximum number of knowledge-base facts surfaced into a single "
        "will/action beat (clamped to 1–20). Higher gives more context at the "
        "cost of a longer prompt. Only used when the Game Knowledge Base is "
        "enabled."
    ),
    group="plugins",
    component="vessel_plugin",
    advanced=True,
)
config_registry.get_value(
    "VESSEL_KNOWLEDGE_LIVE_FETCH",
    True,
    value_type=bool,
    label="Game Knowledge: Live Wiki Fetch",
    description=(
        "When enabled, the explicit knowledge lookup may fetch pages from the "
        "live game wiki (minecraft.wiki) and summarise them once via the LLM, "
        "caching the result on disk. When disabled, lookups are served purely "
        "from the on-disk cache (fully offline). The automatic will/action "
        "beats always read from cache regardless of this setting, so a beat "
        "never blocks on the network. Only used when the Game Knowledge Base "
        "is enabled."
    ),
    group="plugins",
    component="vessel_plugin",
    advanced=True,
)
config_registry.get_value(
    "VESSEL_KNOWLEDGE_FETCH_TIMEOUT_SEC",
    4,
    value_type=int,
    label="Game Knowledge: Fetch Timeout (s)",
    description=(
        "Per-request timeout for live game-wiki fetches (clamped to 1–30). "
        "Kept short so a slow or unreachable wiki degrades gracefully to "
        "whatever is already cached rather than delaying a lookup. Only used "
        "when Live Wiki Fetch is enabled."
    ),
    group="plugins",
    component="vessel_plugin",
    advanced=True,
)
config_registry.get_value(
    "VESSEL_KNOWLEDGE_SUMMARY_MAX_CHARS",
    600,
    value_type=int,
    label="Game Knowledge: Summary Max Chars",
    description=(
        "Maximum length of the compact factual note the LLM distils from each "
        "fetched wiki page (clamped to 120–4000), cached on disk. Smaller "
        "keeps prompts lean; larger retains more detail. Only used when Live "
        "Wiki Fetch is enabled."
    ),
    group="plugins",
    component="vessel_plugin",
    advanced=True,
)
config_registry.get_value(
    "VESSEL_GOAL_EXPAND_ENABLED",
    True,
    value_type=bool,
    label="Goal Plan Expansion (Drone)",
    description=(
        "When enabled, each time Synth authors a fresh goal a short-lived Drone "
        "runs out of band to expand it into an ordered plan of concrete "
        "sub-steps, consulting the game knowledge base for real rules. The plan "
        "is written back onto the goal and re-sent to Synth via a will beat so "
        "it can act on it. Never runs inside an embodiment turn (Fast Lane "
        "only). Only used when Autonomous In-World Play is enabled."
    ),
    group="plugins",
    component="vessel_plugin",
    advanced=True,
)
config_registry.get_value(
    "VESSEL_SELF_PRESERVATION_ENABLED",
    True,
    value_type=bool,
    label="Self-Preservation Reflex",
    description=(
        "When enabled, a fast structural reflex protects Synth's body between "
        "cognition turns: it swims to the surface when running out of air, "
        "steps out of fire/lava, fights back or flees from a hostile creature, "
        "and respawns after death. This runs at the top of every motor tick "
        "with no LLM (the slow will beat cannot react in time), stays on the "
        "Fast Lane, and never writes a diary entry mid-session."
    ),
    group="plugins",
    component="vessel_plugin",
)
config_registry.get_value(
    "VESSEL_SP_LOW_OXYGEN",
    6,
    value_type=int,
    label="Self-Preservation: Low Oxygen Threshold",
    description=(
        "Air-bubble level at or below which the reflex swims to the surface. "
        "Mineflayer's bot.oxygenLevel is the 0..20 air-bubble scale (20 = full "
        "lungs), NOT the raw 0..300 air-tick counter, so this threshold must be "
        "on 0..20. The default 6 leaves a few bubbles of margin to reach the "
        "surface before drowning; a too-high value (e.g. the old 200) makes "
        "oxygen <= threshold always true and fires the reflex on every tick the "
        "head merely touches water. Higher reacts earlier."
    ),
    group="plugins",
    component="vessel_plugin",
    advanced=True,
)
config_registry.get_value(
    "VESSEL_SP_LOW_HEALTH",
    6,
    value_type=int,
    label="Self-Preservation: Flee Health Threshold",
    description=(
        "Health at or below which the reflex stops fighting a hostile creature "
        "and flees instead. Higher makes Synth more cautious."
    ),
    group="plugins",
    component="vessel_plugin",
    advanced=True,
)
config_registry.get_value(
    "VESSEL_SP_HOSTILE_DIST",
    8,
    value_type=int,
    label="Self-Preservation: Hostile Detection Range (blocks)",
    description=(
        "Distance in blocks within which a hostile creature triggers the "
        "defend/flee reflex."
    ),
    group="plugins",
    component="vessel_plugin",
    advanced=True,
)
config_registry.get_value(
    "VESSEL_SP_FIGHT_BACK",
    True,
    value_type=bool,
    label="Self-Preservation: Fight Back",
    description=(
        "When enabled, Synth fights a nearby hostile creature (while health is "
        "above the flee threshold) instead of always fleeing. Disable to make "
        "Synth always run from danger."
    ),
    group="plugins",
    component="vessel_plugin",
    advanced=True,
)
config_registry.get_value(
    "VESSEL_SP_FIGHT_MAX_FAILS",
    3,
    value_type=int,
    label="Self-Preservation: Max Fight Attempts",
    description=(
        "How many failed attack attempts against the same hostile before the "
        "reflex escalates from fighting to fleeing."
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

        # Suppress a verbatim self-repeat of the last thing Synth already said
        # in this world. Slow reasoning turns (and especially the autonomous
        # will beat, which fires on its own timer) re-read the scrollback and
        # frequently re-emit the *exact* same ``say`` line, so the world sees
        # the identical sentence over and over. This is a structural identity
        # check against Synth's OWN most recent self-line (not keyword/content
        # matching): if the text is byte-for-byte equal (after trimming) to what
        # it just said, we skip the dispatch and report success so the rest of
        # the turn proceeds normally. Only ``say`` is gated; physical verbs are
        # never deduplicated.
        if action == "say" and self._is_repeat_of_last_self_say(name, payload):
            log_info(
                f"[vessel_plugin] Suppressed verbatim self-repeat 'say' in "
                f"'{name}' (identical to last self line)"
            )
            return VesselActionResult(ok=True, detail="suppressed_self_repeat")

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
            if result.ok:
                await self._log_outbound_action(name, action, payload)
                await self._persist_self_speech(name, action, payload)
            return result
        except Exception as exc:
            log_error(f"[vessel_plugin] act('{action}') error ({name}): {exc}")
            return VesselActionResult(ok=False, detail=f"act_error: {exc}")

    @staticmethod
    def _is_repeat_of_last_self_say(environment: str, payload: dict[str, Any]) -> bool:
        """Return ``True`` when this ``say`` repeats Synth's own last line.

        Reads the in-memory world chat context (the same deque
        :meth:`_persist_self_speech` writes to) and compares the pending text
        against the most recent ``self``-authored line. The comparison is a
        trimmed exact-string identity check on Synth's OWN output — never a
        keyword or semantic match — so it only ever suppresses a byte-for-byte
        self-repeat and never a genuinely new sentence or a reply to someone
        else. Fully guarded: any lookup failure returns ``False`` (never blocks
        a real action).
        """
        text = str(payload.get("text") or "").strip()
        if not text:
            return False
        try:
            from core.chat_context_manager import get_or_create_chat_context
            from core.interface_path_utils import build_interface_path

            interface_path = build_interface_path("vessel", environment, None)
            context = get_or_create_chat_context(interface_path)
            for msg in reversed(context):
                if not isinstance(msg, dict):
                    continue
                if str(msg.get("username")) != "self":
                    continue
                return str(msg.get("text") or "").strip() == text
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"[vessel_plugin] self-repeat check failed: {exc}")
        return False

    async def _persist_self_speech(
        self,
        environment: str,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        """Echo Synth's own in-world *speech* into the world chat history.

        When Synth speaks in a world (the ``say`` verb) its line must appear in
        the shared world conversation just like on Telegram/Discord, attributed
        to itself. We persist it with the canonical ``sender_name="self"``
        convention (the same one every chat interface uses for Synth's own
        turns), so the history/prompt layer renders it as a ``Self:`` line and
        the will beat sees what Synth already said instead of repeating itself.

        Only ``say`` is echoed — other verbs (``move``/``look``/…) are physical
        acts, already surfaced in the Activities tab, and would only add noise
        to the conversation. This is a structural gate on the verb name, never
        on the message content. Fully guarded: a persistence failure never
        affects the action itself, and it deliberately does **not** re-enqueue
        the line into cognition (that would be a self-reply loop) — it only
        writes to history.
        """
        if action != "say":
            return
        text = str(payload.get("text") or "").strip()
        if not text:
            return
        try:
            from core.chat_context_manager import add_message_to_context
            from core.interface_path_utils import build_interface_path

            interface_path = build_interface_path("vessel", environment, None)
            await add_message_to_context(
                interface_path=interface_path,
                message_text=text,
                sender_name="self",
                sender_id="self",
            )
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"[vessel_plugin] self-speech persist failed: {exc}")

    async def _log_outbound_action(
        self,
        environment: str,
        action: str,
        payload: dict[str, Any],
    ) -> None:
        """Log a successful outbound in-world action to the Activities tab.

        Makes Synth's own in-world responses (``say``/``move``/``look``/...)
        visible in the WebUI Vessel Activities tab alongside the incoming
        perceptions. The summary is built structurally from the action name and
        payload fields — never from keyword/content matching. Fully guarded so a
        logging failure never affects the action itself.
        """
        iface = self._get_vessel_interface()
        if iface is None or not hasattr(iface, "log_outbound_action"):
            return
        try:
            summary = self._describe_outbound_action(action, payload)
            await iface.log_outbound_action(
                environment=environment,
                action=action,
                summary=summary,
                metadata={k: v for k, v in payload.items() if v is not None},
            )
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(f"[vessel_plugin] outbound activity log failed: {exc}")

    @staticmethod
    def _describe_outbound_action(action: str, payload: dict[str, Any]) -> str:
        """Build a human-readable summary of an outbound action.

        Structural only: it labels the action by its verb and appends the most
        salient non-empty payload fields. It never inspects the *content* for
        keywords — it just surfaces whatever fields the payload carries.
        """
        parts: list[str] = []
        for key, value in payload.items():
            if value in (None, "", [], {}):
                continue
            text = str(value)
            # Guard only against pathological payloads. A ``say`` reply is the
            # whole point of the Vessel log entry, so a 120-char clip mangled
            # every real in-world sentence — keep the full text up to a generous
            # ceiling (well above any interface message limit) and only trim
            # runaway blobs. Structural, no content inspection.
            if len(text) > 2000:
                text = text[:1997] + "..."
            parts.append(f"{key}={text}")
        detail = ", ".join(parts)
        return f"{action}({detail})" if detail else action

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

    @staticmethod
    def _refresh_actions_block(reason: str) -> None:
        """Rebuild the cached actions block after entering/leaving a world.

        ``get_supported_actions`` is connection-driven: the gameplay verbs
        (``vessel_<world>_say``/``move``/...) only appear once a world is
        connected. Because the core caches the actions block, a connect/
        disconnect must trigger a rebuild or the new/hidden verbs never reach
        the prompt. Fail-safe: never raises.
        """
        try:
            from core.core_initializer import core_initializer

            core_initializer.schedule_actions_block_refresh(reason)
        except Exception as exc:  # pragma: no cover - defensive
            log_warning(
                f"[vessel_plugin] Could not refresh actions block ({reason}): {exc}"
            )

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
            # Ensure gameplay verbs are exposed even if the block was cached
            # while disconnected (e.g. reattach right after a restart).
            self._refresh_actions_block(f"vessel_connect:{name}")
            # Reconcile a live connector whose DB session has since ended (the
            # inactivity cooldown, or a disconnect-grace close while the Node
            # bridge stayed embodied): without a tracked session all three
            # autonomy beats gate off (has_active_session() is false) and the
            # bot sits inert forever. If nothing is tracked for this world,
            # re-open a session so volition/motorics resume on the live body.
            iface = self._get_vessel_interface()
            reopened_id: str | None = None
            if (
                iface is not None
                and hasattr(iface, "has_local_session")
                and hasattr(iface, "begin_session")
                and not iface.has_local_session(name)
            ):
                try:
                    reopened_id = await iface.begin_session(name)
                    log_info(
                        f"[vessel_plugin] connect_world: reopened session for "
                        f"already-connected '{name}' (session={reopened_id})"
                    )
                except Exception as exc:
                    log_warning(
                        f"[vessel_plugin] session reopen failed for '{name}': {exc}"
                    )
            return VesselActionResult(
                ok=True,
                detail="already_connected",
                data={"environment": name, "session_id": reopened_id},
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
        # Now that a world is connected, the connection-driven gameplay verbs
        # (vessel_<world>_say/move/...) are exposed by get_supported_actions().
        # Rebuild the cached actions block so they reach the prompt.
        self._refresh_actions_block(f"vessel_connect:{name}")
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
        # Gameplay verbs are hidden again once disconnected; drop them from the
        # cached actions block so the prompt only exposes vessel_connect.
        self._refresh_actions_block(f"vessel_disconnect:{name}")
        return VesselActionResult(
            ok=True, detail="disconnected", data={"environment": name}
        )

    async def teardown(self) -> None:
        """Shut down any active embodiment when the core plugin is disabled.

        Called by the runtime plugin toggle (``POST /api/components/toggle`` →
        :meth:`core_initializer.disable_plugin`, which invokes the first of
        ``stop``/``teardown``/``shutdown`` it finds). Disabling the Rift Vessel
        core while a world is connected must not leave a phantom session alive
        (it would keep deprioritising ordinary chat and running will/motor
        beats until the cooldown). This ends every locally-tracked session so
        the lived experience is flushed to a single diary entry, then drops the
        connection. Fully fail-safe: teardown must never raise.
        """
        iface = self._get_vessel_interface()
        environments: set[str] = set()
        if iface is not None and hasattr(iface, "_active_session_environments"):
            try:
                environments = iface._active_session_environments()
            except Exception as exc:  # pragma: no cover - defensive
                log_warning(
                    f"[vessel_plugin] teardown: enumerate sessions failed: {exc}"
                )

        # Also cover a live connection with no locally-tracked session.
        connected = self._connected_world()
        if connected:
            environments.add(connected)

        for name in environments:
            try:
                await self.disconnect_world(connector_name=name)
            except Exception as exc:  # pragma: no cover - defensive
                log_warning(
                    f"[vessel_plugin] teardown: disconnect '{name}' failed: {exc}"
                )

        log_info("[vessel_plugin] teardown complete (sessions closed on disable)")

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
        "observe": {
            "description": (
                "Look around you in the {world} world and take stock of your "
                "surroundings: what and who is near you, and — crucially — what "
                "you could interact with and how. Returns the things around you "
                "together with the interaction each one affords, so you can "
                "decide what to do next. Use this to explore and to evaluate "
                "whether to approach, use, or engage an object or presence "
                "before acting. Purely perceptual — it changes nothing."
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

    async def _observe_surroundings(self) -> dict[str, Any]:
        """Generic "look around and evaluate" — the world-agnostic observe verb.

        Reads the current :class:`WorldState` (which the active connector
        enriches) and surfaces the surroundings together with the *affordances*
        each one offers — the ``{kind, target, verb, distance}`` records telling
        Synth what it could interact with and how. The core owns no world
        knowledge: it never inspects names or types for meaning; it only relays
        the structured affordance contract the connector populated in
        ``WorldState.extra``. A world that provides no such data still yields a
        valid (possibly empty) observation.
        """
        state = await self.get_world_state(connector_name=self._connected_world())
        if state is None:
            return {"status": "error", "message": "no_world_state"}
        extra = state.extra if isinstance(state.extra, dict) else {}
        affordances = extra.get("affordances") or []
        entities = extra.get("entities") or []
        blocks = extra.get("blocks") or []
        return {
            "status": "ok",
            "data": {
                "environment": state.environment,
                "position": state.position,
                "health": state.health,
                "flags": state.flags,
                # The core relays these verbatim — it does not interpret them.
                "affordances": affordances,
                "entities": entities,
                "blocks": blocks,
                "possible_actions": state.possible_actions,
                "summary": (
                    f"{len(entities)} presence(s) and {len(blocks)} notable "
                    f"thing(s) around you; {len(affordances)} possible "
                    "interaction(s)."
                ),
            },
        }

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
        if verb == "observe":
            return await self._observe_surroundings()
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
