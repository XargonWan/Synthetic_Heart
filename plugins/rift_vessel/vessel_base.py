# plugins/rift_vessel/vessel_base.py
"""Base class and data types for Rift Vessel connectors.

A Vessel connector bridges SyntH to a single game world (Minecraft, Skyrim,
VRChat, ...). It is responsible only for **embodiment** — translating in-world
events into normalized :class:`PerceptionEvent` objects, and translating
SyntH's normalized actions into world-specific commands. Cognition, memory and
identity stay in the SyntH core (see the Rift Vessel decision boundary in
``docs/rift_vessel.rst``).

All connectors must subclass :class:`VesselConnectorBase` and implement the
abstract methods. Register a connector at module import time::

    from core.vessel_registry import register_vessel_connector
    from plugins.rift_vessel.vessel_base import VesselConnectorBase

    class MyWorldConnector(VesselConnectorBase):
        display_name = "My World"
        ...

    CONNECTOR_CLASS = MyWorldConnector
    register_vessel_connector("myworld", __name__, label="My world connector")

Design constraints (per project decisions):

* Connectors **must not** create agentic tasks (no Agent Lane / Drone spawn).
  Vessel actions run on the normal action path.
* Connectors **must not** write diary/memory entries mid-session. The Vessel
  interface buffers experiences and flushes a single autobiographical entry at
  end-of-session (explicit logout or long inactivity cooldown).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List


@dataclass
class WorldState:
    """A normalized snapshot of the vessel's current environment.

    Kept deliberately technical (not narrative) so the cognition layer retains
    decision fidelity — see the perception discussion in issue #310.

    Attributes:
        environment:      Short world identifier, e.g. ``"minecraft"``.
        health:           Optional current health, engine-defined scale.
        position:         Optional ``{"x", "y", "z"}`` (or world-specific) coords.
        possible_actions: Normalized action names the vessel can currently take
                          (e.g. ``["move", "attack", "use"]``).
        flags:            Free-form technical state flags (combat, daytime, ...).
        extra:            Connector-specific payload, opaque to the core.
    """

    environment: str
    health: float | None = field(default=None)
    position: Dict[str, Any] | None = field(default=None)
    possible_actions: List[str] = field(default_factory=list)
    flags: Dict[str, Any] = field(default_factory=dict)
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PerceptionEvent:
    """A single normalized event perceived from the world.

    The Vessel interface applies lightweight salience filtering (dedup +
    rate-limit) before injecting selected events into the message chain. A
    future perception/salience worker may replace this simple filter without
    changing this contract.

    Attributes:
        environment:  Short world identifier, e.g. ``"minecraft"``.
        event_type:   Normalized type, e.g. ``"chat"``, ``"proximity"``,
                      ``"damage"``, ``"spawn"``.
        summary:      Short human-readable summary of the event.
        actor:        Optional originating actor id/name (player, mob, ...).
        salience:     Optional connector hint in [0.0, 1.0]; higher = more
                      likely to reach cognition. ``None`` = let the interface
                      decide.
        data:         Structured technical payload (vectors, distances, ...).
    """

    environment: str
    event_type: str
    summary: str
    actor: str | None = field(default=None)
    salience: float | None = field(default=None)
    data: Dict[str, Any] = field(default_factory=dict)


@dataclass
class VesselActionResult:
    """Result of dispatching a normalized action to a Vessel connector.

    Attributes:
        ok:      Whether the action was accepted/executed by the world.
        detail:  Optional human-readable detail or error message.
        data:    Optional structured result payload from the connector.
    """

    ok: bool
    detail: str | None = field(default=None)
    data: Dict[str, Any] = field(default_factory=dict)


# ----------------------------------------------------------------------------
# Perception durability — telemetry log vs game-experience.
# ----------------------------------------------------------------------------
# Not every perceived event is worth persisting to the durable conversational
# history (``chat_history_cache``). A world streams a constant flow of ambient
# telemetry — things it *sees*, blocks it *gathers*, entities that come into
# *proximity*, mobs that *spawn* — which is useful only as live ambient
# grounding (the in-memory perception ring, capped in the prompt) and never as
# a lasting record. Persisting it bloats the vessel history with pure log noise
# and, at scale, drowns the genuinely memorable moments (player conversation,
# taking damage, dying) and Synth's own volition, while also being re-loaded at
# every restart.
#
# ``EPHEMERAL_EVENT_TYPES`` is the world-agnostic set of event kinds treated as
# *pure log*: recorded in the activity log and surfaced live via the perception
# ring, but NOT written to the durable chat history. Any event kind not in this
# set is treated as *durable game-experience* and persisted normally — a safe
# default so a new/unknown event kind is never silently lost.
#
# Classification is purely STRUCTURAL: it keys off the normalized
# ``event_type`` (a contract enum on :class:`PerceptionEvent`), never the free
# text of the summary — so it works identically in any language and any world.
# A real in-world player ``chat`` carries an actor and is always durable; that
# distinction is enforced separately at the ingestion point (actor presence).
EPHEMERAL_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "sighting",  # "you notice a block/entity nearby" — ambient scenery
        "gather",  # picked up / mined a block — routine motor telemetry
        "proximity",  # an entity drifted into range — ambient presence
        "spawn",  # a mob spawned nearby — ambient presence
        "movement",  # the body moved — pure motor telemetry
        "status",  # periodic self-status snapshot — pure telemetry
    }
)


def is_ephemeral_event(event_type: str | None) -> bool:
    """Return ``True`` for pure-log telemetry that must NOT persist to history.

    Structural classification on the normalized ``event_type`` only (never the
    summary text), so it is language- and world-agnostic. Unknown/None event
    kinds default to durable (``False``) so nothing new is silently dropped.
    """
    if not event_type:
        return False
    return event_type in EPHEMERAL_EVENT_TYPES


# Callback the interface passes to the connector so inbound world events can be
# injected into the message chain. The connector calls it for every perceived
# event; the interface applies salience filtering + enqueue.
PerceptionCallback = Callable[[PerceptionEvent], Any]


class VesselConnectorBase(ABC):
    """Abstract base for all Rift Vessel connectors.

    The Vessel core plugin/interface calls these methods and handles everything
    else (salience filtering, chain injection, session tracking, activity
    logging, end-of-session diary flush).
    """

    display_name: str = "Unnamed Vessel"

    # Human-readable reason for the most recent failed :meth:`connect`. A
    # connector should set this (and clear it on success / at the start of a
    # new attempt) so the core plugin can tell Synth — and the requester —
    # WHY entering the world failed, instead of a generic error. ``None`` when
    # there is no failure to report.
    last_error: str | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    @abstractmethod
    async def connect(
        self,
        settings: Dict[str, Any],
        on_event: PerceptionCallback,
    ) -> bool:
        """Connect to the world and start streaming perception events.

        Args:
            settings:  Connector-specific configuration (host, port, creds, ...),
                       typically parsed from ``VESSEL_SETTINGS``.
            on_event:  Callback the connector must invoke for every perceived
                       :class:`PerceptionEvent`. The interface handles salience
                       filtering and message-chain injection.

        Returns:
            ``True`` if the connection succeeded and the vessel is embodied.
            On failure return ``False`` **and** set :attr:`last_error` to a
            human-readable reason so the caller can surface it.
        """

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the world and release resources (idempotent)."""

    # ------------------------------------------------------------------
    # Action + perception
    # ------------------------------------------------------------------

    @abstractmethod
    async def act(
        self,
        action: str,
        payload: Dict[str, Any],
    ) -> VesselActionResult:
        """Execute a normalized action inside the world.

        Args:
            action:   Normalized action name without the ``vessel_`` prefix,
                      e.g. ``"say"``, ``"move"``, ``"look"``, ``"use"``.
            payload:  Action fields (text, target, direction, ...).

        Returns:
            A :class:`VesselActionResult`. Must never raise for a normal
            in-world failure — return ``ok=False`` with a ``detail`` instead.
        """

    async def get_world_state(self) -> WorldState | None:
        """Return the current :class:`WorldState`, or ``None`` if unavailable.

        Optional. Connectors that cannot cheaply snapshot state may skip this.
        """
        return None

    def describe_capabilities(self) -> Dict[str, Any]:
        """Return capability flags / metadata for the WebUI and prompt context.

        Optional override. Defaults to an empty dict.
        """
        return {}

    def get_world_actions(self) -> Dict[str, Dict[str, Any]]:
        """Return **world-specific** embodiment actions this connector adds.

        The core Vessel plugin already exposes the world-agnostic *core set*
        (``connect``, ``disconnect``, ``say``, ``move``, ``look``, ``use``,
        ``status``) that every embodied world shares. A connector may override
        this hook to declare **extra** verbs that only make sense in its world
        (e.g. Minecraft ``craft`` / ``mine``, Skyrim ``cast_spell`` / ``sneak``).

        The returned mapping uses the same schema shape as a plugin's
        ``get_supported_actions`` — keyed by the **bare verb** (no prefix), each
        value a dict with ``description`` / ``required_fields`` /
        ``optional_fields`` / ``security_level``. The core Vessel plugin
        namespaces the key as ``vessel_<world>_<verb>`` when exposing it and
        dispatches the verb back to :meth:`act`. Do **not** declare
        ``external_effects`` here — Vessel actions must stay on the normal
        (Fast-Lane) action path.

        Optional override. Defaults to an empty dict (core set only).
        """
        return {}

    def get_knowledge_sources(self) -> List[Dict[str, Any]]:
        """Return this world's curated **knowledge base** entries.

        A world's *knowledge base* is a small curated set of reference facts
        about how that game/world works (e.g. Minecraft "iron ore must be mined
        with a stone pickaxe or better"). It exists so Synth can reason about a
        goal using real game rules instead of hallucinating them, and it is
        deliberately **reference material, never a script** — it informs
        cognition but never dictates the actions Synth takes (see the
        spontaneity rule in AGENTS.md §5c).

        Each entry is a plain dict with at least a ``title`` and ``text`` and an
        optional structural ``tags`` list (lowercase game ids / concepts) used
        for keyword-free matching against a goal's structural fields — the match
        is on the goal's own ``target_name`` / id tokens, never on free-text
        natural-language keywords, so it works across languages. An optional
        ``url`` may point at the upstream wiki page the entry was distilled
        from.

        Optional override. Defaults to an empty list (no knowledge base).
        """
        return []

    def get_knowledge_wiki_sources(self) -> List[Any]:
        """Return this world's live **wiki knowledge sources**.

        Each entry is a
        :class:`plugins.rift_vessel.knowledge_client.WikiSource` descriptor that
        tells the world-agnostic knowledge client *which* wiki(s) to consult for
        this game (the source URLs are declared per-world here, never hardcoded
        in the core client). The client drives the local-first precedence
        ``local cache → per-game wiki(s) → generic web search`` and summarises +
        caches each page once. This is the modern counterpart of
        :meth:`get_knowledge_sources` (a static curated list) — a connector
        typically overrides this instead, and routes :meth:`lookup_knowledge`
        through :func:`plugins.rift_vessel.knowledge_client.lookup`.

        Optional override. Defaults to an empty list (no wiki sources).
        """
        return []

    def get_progression_stage(self) -> Dict[str, Any] | None:
        """Return the current progression stage + a *typical* next milestone.

        This is the world-agnostic **mechanism** side of the virtual-quest
        feature (AGENTS.md §5c, the Scope rule): the core asks "what stage am I
        at, and what usually comes next?" and each world supplies the answer
        from its own *content* — a game-specific tech-tree (e.g. the Minecraft
        adapter's ``quests.py``). It exists because worlds without a built-in
        quest system (like Minecraft) still benefit from a sense of direction
        toward their natural end-game.

        The return shape mirrors
        :func:`plugins.rift_vessel.minecraft.quests.detect_stage`::

            {
                "stage_id": str,
                "stage_title": str,
                "next_id": str | None,
                "next_hint": str,      # plain-language next milestone
                "query": list[str],    # structural KB-query seed for next step
                "endgame": str,        # the ultimate-goal framing
            }

        **Spontaneity rule.** Whatever a world returns here is surfaced to
        cognition purely as *reference context* — never an engine that executes
        steps, never a dictated goal. Synth still authors its own goal freely
        and may skip, reorder or ignore the milestone entirely.

        Optional override. Defaults to ``None`` (the world has no tech-tree, so
        no progression context is surfaced).
        """
        return None

    async def lookup_knowledge(
        self, query: str, limit: int = 5, *, cache_only: bool = False
    ) -> List[Dict[str, Any]]:
        """Return knowledge-base entries relevant to ``query``.

        ``query`` is a structural token (a goal ``target_name``, an item/block
        id, or a whitespace-joined set of such ids) — **not** a natural-language
        sentence — so matching stays keyword-free and language-agnostic. The
        default implementation filters :meth:`get_knowledge_sources` by simple
        structural overlap between the query tokens and each entry's ``tags``
        (falling back to a substring test on the entry ``title``), and returns
        at most ``limit`` entries. A connector may override this to consult a
        live wiki with a local fallback.

        ``cache_only`` is a hint to any live-wiki override that the caller is on
        the automatic will/action-beat path and must not touch the network or
        the LLM (serve only already-cached pages). The static-source default
        implementation is already offline, so it ignores the flag.

        Optional override. Defaults to a structural filter over
        :meth:`get_knowledge_sources`.
        """
        try:
            sources = self.get_knowledge_sources()
        except Exception:
            return []
        if not sources:
            return []
        tokens = {tok for tok in str(query or "").lower().split() if tok}
        if not tokens:
            return list(sources)[: max(0, int(limit))]
        matched: List[Dict[str, Any]] = []
        for entry in sources:
            if not isinstance(entry, dict):
                continue
            tags = entry.get("tags") or []
            tag_set = {str(t).lower() for t in tags if t}
            title = str(entry.get("title") or "").lower()
            if tokens & tag_set or any(tok in title for tok in tokens):
                matched.append(entry)
        return matched[: max(0, int(limit))]

    async def motor_step(self, goal: Dict[str, Any] | None) -> Dict[str, Any]:
        """Take **one** fast, reflexive step of the body toward ``goal``.

        This is the *motorics* half of autonomy (see ``core.vessel_beat`` and
        AGENTS.md §5c): it is called on a short timer by the interface
        scheduler and must run with **no prompt and no cognition turn** — it
        just picks the single most sensible in-world move toward the active
        goal from the current :class:`WorldState` (using purely structural
        rules over affordances, never keyword/text matching) and performs it
        directly via :meth:`act`.

        The volition layer (the "will beat") decides *what* the goal is; this
        hook only decides *how* to inch toward it between will beats, so
        embodiment stays snappy and responsive. It must **never** create an
        Agent Lane task, a Drone, or a diary entry.

        Args:
            goal: The current active goal dict (``{"description", "note", ...}``)
                  or ``None`` when Synth has not set one yet.

        Returns:
            A small status dict, e.g. ``{"acted": True, "action": "wander"}`` or
            ``{"acted": False, "reason": "no_goal"}``. Optional override;
            defaults to a no-op so a connector without motorics never breaks the
            scheduler.
        """
        return {"acted": False, "reason": "no_motorics"}

    @property
    def is_connected(self) -> bool:
        """Whether the connector currently holds a live world connection."""
        return False

    async def probe_external_liveness(self) -> bool:
        """Whether an **external body** for this world is already embodied.

        Some worlds keep the actual embodiment in a separate, longer-lived
        process than SyntH itself — e.g. the Minecraft Mineflayer bridge is a
        Node subprocess that survives a SyntH restart (and a brief connector
        drop) while the bot stays logged into the world. In that state the
        connector instance reports ``is_connected == False`` (it was just
        loaded and has no live socket), yet the world still holds Synth's body.

        This probe lets the interface's boot-time reattach adopt such an
        already-embodied body **without** starting anything: it must be a cheap,
        read-only check of the external body's own liveness (never spawn a
        bridge, never issue a connect). Default ``False`` — a world with no
        separate external process has nothing to adopt. Fully fail-safe:
        implementations must never raise (return ``False`` on any error).
        """
        return False

    def get_in_world_name(self) -> str | None:
        """Return the name Synth's body carries **inside this world**.

        This is the in-world nickname other players use to address her (e.g.
        the Minecraft bot username). It is used purely so that direct-address
        detection (:func:`core.mention_utils.is_synth_mentioned`) can recognise
        an in-world chat line that names her by her world nickname — the same
        activation mechanism the chat interfaces use, driven by a name rather
        than by keyword/intent matching, so it works in any language.

        Optional override. Defaults to ``None`` (no world-specific name, so
        only the persona name/aliases apply).
        """
        return None

    def get_world_identity(self) -> str | None:
        """Return a stable structural token identifying the **specific world /
        server** this connector is currently embodied in.

        A single game (``environment``) can host many distinct worlds/servers —
        two Minecraft servers, two Skyrim save files, etc. This token
        distinguishes them so that progression (goals) is scoped per concrete
        world: when Synth logs back into *the same* server she resumes where she
        was, and a different server starts its own progression.

        It becomes the ``<world>`` level of the canonical vessel interface path
        ``vessel/<game>/<world>[/<character>]`` and the ``world`` scope of the
        goal store. It must be:

        * **structural, not keyword-derived** — a server/level name the world
          itself reports, or a deterministic slug of the connection target
          (e.g. ``host_port``); never inferred from chat text;
        * **stable** across reconnects to the same world so goals resume;
        * **slug-safe** — no ``/`` (the interface path separator); the caller
          slugifies defensively, but return a clean token when possible.

        Optional override. Defaults to ``None`` (no per-world identity, so all
        of this game's worlds share the single ``world="none"`` scope — the
        legacy behaviour). Fully fail-safe: implementations must never raise.
        """
        return None

    def get_progression_context(self) -> list[str] | None:
        """Return **structural** query tokens describing Synth's current
        progression stage in this world, or ``None`` when unavailable.

        This is the *starter-goal* hook. In a quest-less game (Minecraft, a
        sandbox), when Synth logs in with **no active goal** there is nothing to
        seed the knowledge base with — so the will beat would author a goal
        blind. This hook lets an adapter surface a small set of structural
        tokens that describe *where Synth is in the game's progression* (derived
        from live telemetry only — inventory ids, tier markers, numeric
        state — **never** from chat text or free-text goal descriptions). Those
        tokens seed a knowledge-base lookup so the will beat can author a
        *progression-appropriate* first goal instead of a random one.

        The result is **reference material only**: it orients the suggestion,
        it is never a scripted objective — Synth still authors and may edit the
        goal freely (the spontaneity rule, AGENTS.md §5c).

        Optional override. Defaults to ``None`` (no progression signal, so the
        starter-goal seeding is skipped and the will beat authors from persona
        alone — the legacy behaviour). Fully fail-safe: implementations must
        never raise.
        """
        return None

    # ------------------------------------------------------------------
    # Setup hooks (optional)
    # ------------------------------------------------------------------

    def setup(self) -> None:
        """Called once when the connector is loaded. Warm up, probe, etc."""

    def teardown(self) -> None:
        """Called when the connector is unloaded or the app shuts down."""
