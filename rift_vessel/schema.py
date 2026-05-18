from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class EntityRef:
    """Reference to another entity in the world."""

    id: str
    name: str
    relationship: str = "neutral"
    health_pct: float | None = None
    distance: float | None = None


@dataclass
class ActionDef:
    """An action the entity CAN perform at this moment."""

    name: str
    target_required: bool = False
    description: str = ""
    parameters: dict[str, Any] = field(default_factory=dict)


@dataclass
class WorldState:
    """Canonical representation of the environment at a point in time."""

    environment: str
    entity_id: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    health: float | None = None
    max_health: float | None = None
    magicka: float | None = None
    stamina: float | None = None
    location: str | None = None
    position: tuple[float, float, float] | None = None

    combat_state: bool = False
    is_sneaking: bool = False
    is_mounted: bool = False
    current_weapon: str | None = None
    current_spell: str | None = None

    visible_entities: list[EntityRef] = field(default_factory=list)
    audible_events: list[str] = field(default_factory=list)
    recent_dialogue: list[str] = field(default_factory=list)
    possible_actions: list[ActionDef] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_prompt_block(self) -> str:
        """Compact prompt representation (<300 chars typical)."""
        lines = [f"[World: {self.environment}]"]
        if self.location:
            lines.append(f"  Location: {self.location}")
        health_str = ""
        if self.health is not None:
            health_str = f"{self.health:.0f}"
            if self.max_health:
                health_str += f"/{self.max_health:.0f}"
            lines.append(f"  Health: {health_str}")
        if self.combat_state:
            lines.append("  \u2694 Combat")
        if self.visible_entities:
            names = ", ".join(e.name for e in self.visible_entities[:5])
            lines.append(f"  Nearby: {names}")
        if self.possible_actions:
            names = ", ".join(a.name for a in self.possible_actions[:8])
            lines.append(f"  Can do: {names}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "timestamp": self.timestamp.isoformat(),
            "entity_id": self.entity_id,
            "health": self.health,
            "max_health": self.max_health,
            "magicka": self.magicka,
            "stamina": self.stamina,
            "location": self.location,
            "position": list(self.position) if self.position else None,
            "combat_state": self.combat_state,
            "is_sneaking": self.is_sneaking,
            "is_mounted": self.is_mounted,
            "current_weapon": self.current_weapon,
            "current_spell": self.current_spell,
            "visible_entities": [
                {
                    "id": e.id,
                    "name": e.name,
                    "relationship": e.relationship,
                    "health_pct": e.health_pct,
                    "distance": e.distance,
                }
                for e in self.visible_entities
            ],
            "possible_actions": [
                {
                    "name": a.name,
                    "target_required": a.target_required,
                    "description": a.description,
                }
                for a in self.possible_actions
            ],
        }


@dataclass
class WorldEvent:
    """An event the game adapter pushes to SyntH."""

    event_type: str
    source: str
    data: dict[str, Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
