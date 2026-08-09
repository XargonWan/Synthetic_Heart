"""Generic per-mob combat *strategy-override* mechanism for Rift Vessel.

This is the **world-agnostic core** of the special-creature combat override
described in ``TODO - Rift Vessel.md`` §17. The generic power-ratio reflex
(own combat power vs the mob's) is a good default, but some creatures need a
tactic the raw ratio cannot express — e.g. a Minecraft **creeper** must never
be chased (it explodes on contact, so its "power" is badly under-rated by
health/attack alone) and an **enderman** must not be stared at / cornered.

Rather than hard-coding those cases inline in the connector (the way mindcraft
does with ``entity.name === 'creeper'`` checks — REFERENCE ONLY, never
modified), the Vessel core provides this small, generic registry:

  * The **mechanism** (registry + resolver + dispatch) lives here in the core
    and is completely world-agnostic — it knows nothing about Minecraft, mobs,
    or specific creatures.
  * The **content** (which creature id gets which tactic) is supplied by each
    world's connector via :func:`register_combat_strategy`, keyed on the
    connector's **canonical structural entity id** (the game enum id the bridge
    reports, e.g. ``"creeper"``) — NEVER a display name or a keyword table, so
    it works in any client language.

A strategy is a pure callable ``(entity, extra) -> plan | None`` returning the
same ``{"threat", "verb", "payload", "reason"}`` plan shape the survival reflex
uses, or ``None`` to fall through to the generic power-ratio decision. It must
be Fast-Lane only — it never declares external effects and never runs cognition.

Golden rule (AGENTS.md): removing any connector or this module must not break
the rest of the system. An unknown creature simply has no override and uses the
generic power-ratio reflex.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from core.logging_utils import log_debug

# A combat strategy: given the structural entity dict and the body's telemetry
# (``WorldState.extra``), return a reflex plan dict or ``None`` to fall through
# to the generic power-ratio decision. Pure/structural — no LLM, no keywords.
CombatStrategy = Callable[[Dict[str, Any], Dict[str, Any]], Optional[Dict[str, Any]]]


class CombatStrategyRegistry:
    """World-scoped registry of per-creature combat-strategy overrides.

    Overrides are keyed by ``(world, entity_id)`` where both are the connector's
    canonical structural identifiers (game enum ids), never display text. The
    registry is a passive lookup table: it holds no game logic itself, only the
    callables a connector registers.
    """

    def __init__(self) -> None:
        # {world: {entity_id: strategy}}
        self._strategies: Dict[str, Dict[str, CombatStrategy]] = {}

    def register(self, world: str, entity_id: str, strategy: CombatStrategy) -> None:
        """Register a combat strategy for one creature id in one world.

        Args:
            world:     The connector's world token (e.g. ``"minecraft"``).
            entity_id: The canonical structural creature id (game enum, e.g.
                       ``"creeper"``) — never a display name.
            strategy:  Pure callable ``(entity, extra) -> plan | None``.
        """
        w = str(world).strip().lower()
        eid = str(entity_id).strip().lower()
        if not w or not eid or not callable(strategy):
            return
        self._strategies.setdefault(w, {})[eid] = strategy
        log_debug(f"[combat_strategy] Registered override for '{eid}' in world '{w}'")

    def resolve(self, world: str, entity_id: str) -> Optional[CombatStrategy]:
        """Return the registered strategy for ``(world, entity_id)`` or ``None``.

        Structural lookup only — exact match on the canonical ids. No keyword
        matching, no fuzzy/partial matching.
        """
        if not world or not entity_id:
            return None
        w = str(world).strip().lower()
        eid = str(entity_id).strip().lower()
        return self._strategies.get(w, {}).get(eid)

    def apply(
        self,
        world: str,
        entity: Dict[str, Any],
        extra: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Resolve and run the override for an entity, returning its plan.

        Returns the strategy's plan dict, or ``None`` when there is no override
        for this creature OR the override chose to defer to the generic
        reflex. Fully fail-safe: any error in a strategy degrades to ``None``
        (generic power-ratio takes over), never breaking the motor tick.
        """
        try:
            entity_id = str((entity or {}).get("name") or "")
        except Exception:
            return None
        strategy = self.resolve(world, entity_id)
        if strategy is None:
            return None
        try:
            return strategy(entity, extra)
        except Exception as exc:  # pragma: no cover - defensive
            log_debug(
                f"[combat_strategy] strategy for '{entity_id}' in '{world}' "
                f"failed: {exc}"
            )
            return None

    def has_overrides(self, world: str) -> bool:
        """True when at least one override is registered for the world."""
        return bool(self._strategies.get(str(world).strip().lower()))


# Module-level singleton, mirroring the vessel/iris/auris registry pattern.
combat_strategy_registry = CombatStrategyRegistry()


def register_combat_strategy(
    world: str, entity_id: str, strategy: CombatStrategy
) -> None:
    """Module-level convenience wrapper for :meth:`CombatStrategyRegistry.register`."""
    combat_strategy_registry.register(world, entity_id, strategy)


def resolve_combat_strategy(world: str, entity_id: str) -> Optional[CombatStrategy]:
    """Module-level convenience wrapper for :meth:`CombatStrategyRegistry.resolve`."""
    return combat_strategy_registry.resolve(world, entity_id)


def apply_combat_strategy(
    world: str, entity: Dict[str, Any], extra: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    """Module-level convenience wrapper for :meth:`CombatStrategyRegistry.apply`."""
    return combat_strategy_registry.apply(world, entity, extra)
