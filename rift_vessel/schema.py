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
    """Minimal world state.  Each vessel subclass extends this with
    game-specific fields (health, avatar params, etc.)."""

    environment: str
    entity_id: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def to_prompt_block(self) -> str:
        """Override in subclass for game-specific rendering."""
        return f"[World: {self.environment}]"

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "timestamp": self.timestamp.isoformat(),
            "entity_id": self.entity_id,
        }


@dataclass
class WorldEvent:
    """An event the game adapter pushes to SyntH."""

    event_type: str
    source: str
    data: dict[str, Any]
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
